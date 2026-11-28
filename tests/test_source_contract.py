import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from app import Indexer, library_config, make_server, standardized
from catalog import Catalog
from sources import _REGISTRY, _load_registry, allows_abstract_export, resolved_pdf
from test_workflow import pdf_sample


SPEC = {"kind": "sample", "mode": "search", "label": "Sample journal", "description": "RF measurements",
        "fields": [{"key": "url", "label": "Collection", "type": "url", "required": True, "max_length": 200}]}


class SourceContractTest(unittest.TestCase):
    def test_export_rule_comes_from_adapter_and_preserves_public_abstract_on_merge(self):
        public = SimpleNamespace(SPEC=SPEC)
        restricted = SimpleNamespace(SPEC=SPEC | {"kind": "private", "export_abstract": False})
        with tempfile.TemporaryDirectory() as directory, patch.dict(_REGISTRY, {"sample": public, "private": restricted}):
            self.assertTrue(allows_abstract_export("sample"))
            self.assertFalse(allows_abstract_export("private"))
            catalog = Catalog(Path(directory) / "catalog.sqlite3")
            lid = catalog.save_library(library_config({"name": "Test"}))["id"]
            for kind, abstract in [("private", "Long private abstract"), ("sample", "Public")]:
                aid, _ = catalog.add_article(lid, standardized(
                    {"title": "Load-pull", "doi": "10.1234/sample", "abstract": abstract}, {"kind": kind}))
            self.assertEqual(catalog.article(aid)["abstract"], "Public")

    def test_bad_import_or_spec_does_not_disable_a_working_adapter(self):
        good = SimpleNamespace(SPEC=SPEC, search=Mock())
        broken = SimpleNamespace(SPEC=SPEC | {"fields": [{"key": "kind"}]}, search=Mock())
        with (patch("sources._BUILTIN_MODULES", ("bad_import", "bad_spec", "good", "duplicate")),
              patch("sources.import_module", side_effect=[ImportError("hidden secret"), broken, good, good]),
              self.assertLogs(level="WARNING") as log):
            self.assertEqual(_load_registry(), {"sample": good})
        self.assertEqual(len(log.output), 3)
        self.assertIn("bad_import", log.output[0])
        self.assertIn("bad_spec", log.output[1])
        self.assertIn("duplicate", log.output[2])
        self.assertNotIn("hidden secret", str(log.output))

    def test_error_details_do_not_expose_module_secrets_and_not_found_is_normal(self):
        modules = {
            "missing": SimpleNamespace(resolve=Mock(side_effect=ValueError("HTTP 404 hidden secret"))),
            "denied": SimpleNamespace(resolve=Mock(side_effect=ValueError("HTTP 403 hidden secret"))),
            "broken": SimpleNamespace(resolve=Mock(side_effect=RuntimeError("hidden secret")))}
        with patch.dict(_REGISTRY, modules, clear=True):
            errors = []
            self.assertIsNone(resolved_pdf("10.1234/x", lambda kind: [], Mock(), Mock(), errors))
        self.assertEqual(errors, ["denied: HTTP 403", "broken: ошибка получения PDF"])

    def test_new_source_http_configuration_search_and_pdf_after_another_adapter_fails(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as directory:
            catalog = Catalog(Path(directory) / "catalog.sqlite3")
            data = pdf_sample()
            getter = Mock(return_value=data)
            indexer = Indexer(catalog, download=getter)
            server = make_server("127.0.0.1", 0, indexer)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            def request(path, payload=None):
                req = Request(f"http://127.0.0.1:{server.server_port}"+path,
                              data=json.dumps(payload).encode() if payload is not None else None,
                              headers={"Content-Type":"application/json"})
                with urlopen(req, timeout=5) as response:
                    return json.load(response) if response.headers.get_content_type()=="application/json" else response.read()

            collection = "https://sample.test/collections/rf"
            pdf = "https://sample.test/articles/7.pdf"
            download_url = pdf + "?ticket=fixture"
            def search(source, keyword, since, limit, get):
                self.assertEqual((source["url"], keyword), (collection, "load-pull"))
                return [{"title": "Load-pull at 10 GHz", "external_id": "paper7", "doi": "10.1234/test"}]

            def resolve(doi, external_id, get):
                self.assertEqual((doi, external_id, get), ("10.1234/test", "paper7", getter))
                yield download_url, "cc-by", pdf

            broken = SimpleNamespace(resolve=Mock(side_effect=RuntimeError("do not log this token")))
            module = SimpleNamespace(SPEC=SPEC, search=search, resolve=resolve)
            config = {"name": "Load-pull", "keywords": ["load-pull"], "enabled": False, "sources": [{"kind":"sample"}]}
            try:
                # A real module reaches the registry through exactly this import boundary.
                with (patch("sources._BUILTIN_MODULES", ("sample",)),
                      patch("sources.import_module", return_value=module)):
                    installed = _load_registry()
                with patch.dict(_REGISTRY, {"broken": broken, **installed}, clear=True):
                    broken.SPEC = SPEC | {"kind": "broken"}
                    self.assertIn(SPEC, request("/api/sources"))
                    with self.assertRaises(HTTPError) as exc:
                        request("/api/libraries", config)
                    self.assertEqual(exc.exception.code, 400)
                    exc.exception.close()
                    config["sources"][0]["url"] = collection
                    library = request("/api/libraries", config)["id"]
                    request(f"/api/libraries/{library}/run", {})
                    self.assertTrue(indexer.run_next())
                    article = request(f"/api/libraries/{library}/articles")["items"][0]["id"]
                    request(f"/api/libraries/{library}/articles/{article}/resolve", {})
                    self.assertTrue(indexer.discovery.run_once())
                    self.assertEqual(request(f"/api/articles/{article}/pdf"), data)
                    broken.resolve.assert_called_once()
                    self.assertEqual(getter.call_args.args[0], download_url)
                    self.assertEqual(catalog.article(article)["pdf_url"], pdf)
                    self.assertEqual(catalog.article(article)["license"], "cc-by")
            finally:
                server.shutdown()
                server.server_close()
                worker.join(5)
