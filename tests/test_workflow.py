import io
import json
import tempfile
import threading
import time
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app import Indexer, make_server, matcher
from catalog import Catalog
from sources import _REGISTRY, fetch


SAMPLE_DOI = "10.1234/load-pull.1"
OTHER_DOI = "10.1234/load-pull.2"
EXCLUDED_DOI = "10.1234/load-pull.3"
COPY = "https://papers.example.net/gallium.pdf"


def pdf_sample():
    writer = PdfWriter()
    page = writer.add_blank_page(400, 500)
    name = NameObject
    page[name("/Resources")] = DictionaryObject({name("/Font"): DictionaryObject({
        name("/F1"): DictionaryObject({name("/Type"): name("/Font"), name("/Subtype"): name("/Type1"),
                                      name("/BaseFont"): name("/Helvetica")})})})
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 40 450 Td (Load-pull was measured at 10 GHz.) Tj ET")
    page.replace_contents(stream)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(self.temp.cleanup)
        self.catalog = Catalog(Path(self.temp.name) / "catalog.sqlite3")
        self.downloads = []
        self.queries = []
        self.pdf = pdf_sample()

        def fake_download(url, limit, *, accept):
            self.downloads.append(url)
            self.assertEqual((url, accept), (COPY, "application/pdf"))
            return self.pdf

        def search(source, keyword, since, limit):
            self.queries.append((source["kind"], keyword))
            return fetch(source, keyword, since, limit, get=self.fixture)

        self.indexer = Indexer(self.catalog, search=search, download=fake_download)
        self.server = make_server("127.0.0.1", 0, self.indexer)
        self.http = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker = threading.Thread(target=self.indexer.supervise, daemon=True)
        self.http.start()
        self.worker.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.indexer.stop.set()
        self.indexer.wake.set()
        self.worker.join(5)
        self.server.shutdown()
        self.server.server_close()
        self.http.join(5)

    def fixture(self, url, _limit, *, accept):
        host = urlsplit(url).hostname
        stamp = date.today()
        if host == "api.openalex.org":
            return json.dumps({"results": [{"id": "https://openalex.org/W42", "doi": "https://doi.org/" + SAMPLE_DOI,
                  "display_name": "GaN Load‑pull characterization", "publication_date": stamp.isoformat(),
                  "abstract_inverted_index": {"GaN": [0], "amplifier": [1]},
                  "primary_location": {"source": {"display_name": "RF Letters"}},
                  "best_oa_location": {"is_oa": True, "pdf_url": COPY, "license": "cc-by"}}]}).encode()
        if host == "api.crossref.org":
            def item(doi, title):
                return {"DOI": doi, "title": [title], "abstract": "<p>Microwave measurement.</p>",
                        "published": {"date-parts": [[stamp.year, stamp.month, stamp.day]]}}
            entries = [item(SAMPLE_DOI, "GaN load pull characterization"),
                       item(OTHER_DOI, "Silicon load pull techniques"),
                       item(EXCLUDED_DOI, "Tutorial on GaN load-pull"),
                       item("10.1234/unrelated", "Pull load optimization")]
            return json.dumps({"message": {"items": entries}}).encode()
        self.assertEqual(host, "journal.example.net")
        return f"""<rss version="2.0"><channel><title>RF Letters</title>
        <item><title>GaN load-pull characterization</title><guid>{SAMPLE_DOI}</guid>
        <link>https://doi.org/{SAMPLE_DOI}</link><description>Load-pull measurement</description></item>
        </channel></rss>""".encode()

    def request(self, path, method="GET", data=None, expected=200, mime="application/json"):
        if isinstance(data, dict):
            data = json.dumps(data, ensure_ascii=False).encode()
        request = Request(self.base + path, data=data, method=method, headers={"Content-Type": mime})
        try:
            response = urlopen(request, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read()
            self.assertEqual(response.status, expected, raw[:300])
            if response.headers.get_content_type() == "application/json":
                return json.loads(raw)
            return raw

    def add_library(self, name, keywords, sources, **options):
        library = self.request("/api/libraries", "POST", {
            "name": name, "keywords": keywords, "exclude": ["tutorial"], "sources": sources,
            "match": "all", "interval_hours": 1, "lookback_days": 7, **options,
        }, 201)
        self.assertEqual(library["name"], name)
        return library["id"]

    def wait_for_run(self, library_id, minimum_runs=1):
        for _ in range(100):
            runs = self.request(f"/api/libraries/{library_id}/runs")
            if len(runs) >= minimum_runs and runs[0]["status"] not in ("queued", "running"):
                self.assertEqual(runs[0]["status"], "ok", runs[0]["errors"])
                return runs[0]
            time.sleep(0.04)
        self.fail("Фоновый запуск не завершился")

    def test_multisource_routes_and_exports_load_pull(self):
        self.assertEqual({item["kind"] for item in self.request("/api/sources")},
                         {"openalex", "crossref", "rss", "ieee"})
        self.assertIn(b"SciSearcher", self.request("/"))
        source_config = [{"kind": "openalex"}, {"kind": "crossref"},
                         {"kind": "rss", "url": "https://journal.example.net/feed.xml"}]
        general = self.add_library("Load-pull", ["load-pull"], source_config)
        gan = self.add_library("GaN", ["load-pull", "GaN"], source_config, enabled=False)
        self.wait_for_run(general)
        self.assertEqual(self.request(f"/api/libraries/{gan}/runs"), [])
        self.request(f"/api/libraries/{gan}/run", "POST", expected=202)
        self.wait_for_run(gan)

        general_items = self.request(f"/api/libraries/{general}/articles")["items"]
        gan_items = self.request(f"/api/libraries/{gan}/articles")["items"]
        self.assertEqual({item["doi"] for item in general_items}, {SAMPLE_DOI, OTHER_DOI})
        self.assertEqual([item["doi"] for item in gan_items], [SAMPLE_DOI])
        self.assertEqual(self.request(f"/api/libraries/{general}/articles?search=Silicon")["total"], 1)
        self.assertEqual(self.downloads, [COPY])
        gan_article = gan_items[0]
        paper_id = gan_article["id"]
        self.assertEqual(set(gan_article["sources"]), {"openalex", "crossref", "rss"})
        self.assertTrue(gan_article["local_pdf"])
        self.assertEqual(self.request(f"/api/articles/{paper_id}/pdf"), self.pdf)

        def exported(library_id):
            raw = self.request(f"/api/libraries/{library_id}/export.jsonl?full_text=1")
            return [json.loads(line) for line in raw.splitlines()]

        general_export, gan_export = exported(general), exported(gan)
        self.assertEqual(len(general_export), 1)
        self.assertEqual(len(gan_export), 1)
        piece = gan_export[0]
        self.assertEqual(piece["id"], general_export[0]["id"])
        self.assertEqual(piece["library_ids"], [general, gan])
        self.assertEqual((piece["doi"], piece["page"], piece["content_kind"]), (SAMPLE_DOI, 1, "full_text"))
        self.assertIn("10 GHz", piece["text"])

        other_id = next(item["id"] for item in general_items if item["doi"] == OTHER_DOI)
        xml = b"<article><body><sec><title>Methods</title><p>Matched load impedance measured at 3 GHz.</p></sec></body></article>"
        self.request(f"/api/articles/{other_id}/file?format=xml", "PUT", xml, mime="application/xml")
        self.assertEqual(len(exported(general)), 2)
        self.assertIn(b"3 GHz", self.request(f"/api/libraries/{general}/export.md?full_text=1"))

        self.request(f"/api/libraries/{general}/run", "POST", expected=202)
        self.assertEqual(self.wait_for_run(general, minimum_runs=2)["added"], 0)
        self.assertEqual(self.downloads, [COPY])

    def test_local_match_equivalence(self):
        matches = matcher({"keywords": ["load-pull", "GaN"], "match": "all", "exclude": ["tutorial"]})
        for spelling in ("load-pull", "load pull", "load‑pull", "load–pull"):
            self.assertTrue(matches({"title": f"{spelling} methods", "abstract": "Analysis of GaN."}))
        self.assertFalse(matches({"title": "GaN payload-pull"}))
        self.assertFalse(matches({"title": "GaN load-pull tutorial"}))

    def test_new_adapter_uses_registry_and_generic_fields(self):
        kind = "rf_catalog"
        spec = {"kind": kind, "label": "RF Catalog", "description": "Test catalog", "mode": "search",
                "fields": [{"key": "section", "label": "Section", "type": "text",
                            "required": True, "max_length": 30}]}

        def search(source, keyword, since, limit, get):
            self.assertEqual(source["section"], "measurements")
            self.assertEqual(keyword, "load-pull")
            return [{"title": "On-wafer load-pull", "doi": SAMPLE_DOI, "authors": ["Author"]}]

        with patch.dict(_REGISTRY, {kind: SimpleNamespace(SPEC=spec, search=search)}):
            self.assertIn(spec, self.request("/api/sources"))
            self.request("/api/libraries", "POST", {
                "name": "Invalid", "keywords": ["load-pull"], "sources": [{"kind": kind}]
            }, 400)
            library_id = self.add_library("Plugin library", ["load-pull"], [
                {"kind": kind, "section": "measurements"}
            ])
            self.wait_for_run(library_id)
            [article] = self.request(f"/api/libraries/{library_id}/articles")["items"]
            self.assertEqual(article["sources"], [kind])
