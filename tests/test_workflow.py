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
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app import Indexer, make_server, matcher, standardized
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
        self.worker = threading.Thread(target=self.indexer.supervise, daemon=True)
        self.server = make_server("127.0.0.1", 0, self.indexer, worker=self.worker)
        self.http = threading.Thread(target=self.server.serve_forever, daemon=True)
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
        general_stats = next(row for row in self.request("/api/libraries") if row["id"] == general)
        self.assertEqual((general_stats["article_count"], general_stats["full_text_count"], general_stats["pdf_count"]), (2, 2, 1))
        full_text = self.request(f"/api/libraries/{general}/articles?scope=full_text&search=Silicon")
        self.assertEqual([item["id"] for item in full_text["items"]], [other_id])
        with_pdf = self.request(f"/api/libraries/{general}/articles?scope=pdf")
        self.assertEqual([item["id"] for item in with_pdf["items"]], [paper_id])

        self.request(f"/api/libraries/{general}/run", "POST", expected=202)
        self.assertEqual(self.wait_for_run(general, minimum_runs=2)["added"], 0)
        self.assertEqual(self.downloads, [COPY])

    def test_local_match_equivalence(self):
        matches = matcher({"keywords": ["load-pull", "GaN"], "match": "all", "exclude": ["tutorial"]})
        for spelling in ("load-pull", "load pull", "load‑pull", "load–pull"):
            self.assertTrue(matches({"title": f"{spelling} methods", "abstract": "Analysis of GaN."}))
        self.assertFalse(matches({"title": "GaN payload-pull"}))
        self.assertFalse(matches({"title": "GaN load-pull tutorial"}))

    def test_openalex_cache_is_tried_after_a_failed_pdf_link(self):
        library = self.add_library("Load-pull", ["load-pull"], [{"kind": "openalex"}], enabled=False)
        def failed_download(url, limit, *, accept):
            self.assertEqual((url, accept), (COPY, "application/pdf"))
            raise ValueError("Publisher returned HTTP 502")

        self.indexer.download = failed_download
        with (patch.dict("os.environ", {"OPENALEX_API_KEY": "test-key"}),
              patch("app.cached_pdf", return_value=self.pdf) as cache):
            self.request(f"/api/libraries/{library}/run", "POST", expected=202)
            run = self.wait_for_run(library)
            self.assertEqual(run["downloaded"], 1)
            self.assertEqual(cache.call_args.args[0], "https://openalex.org/W42")
        [article] = self.request(f"/api/libraries/{library}/articles")["items"]
        self.assertEqual(self.request(f"/api/articles/{article['id']}/pdf"), self.pdf)

    def test_health_checks_catalog_and_worker_without_basic_auth(self):
        self.server.password = "test-password"
        self.request("/api/libraries", expected=401)
        self.assertEqual(self.request("/healthz"), {"status": "ok"})
        catalog_file = self.catalog.path
        self.catalog.path = catalog_file.with_name("empty.sqlite3")
        try:
            self.assertEqual(self.request("/healthz", expected=503), {"status": "unavailable"})
        finally:
            self.catalog.path = catalog_file
        self.indexer.stop.set()
        self.indexer.wake.set()
        self.worker.join(5)
        self.assertEqual(self.request("/healthz", expected=503), {"status": "unavailable"})

    def test_export_point_id_survives_new_doi_and_catalog_reopen(self):
        library = self.add_library("Load-pull", ["load-pull"], [{"kind": "openalex"}], enabled=False)
        source = {"kind": "rss", "url": "https://journal.example.net/feed.xml"}
        candidate = {"title": "Load-pull", "url": "https://journal.example.net/load-pull"}
        article_id, _ = self.catalog.add_article(library, standardized(candidate, source))
        self.request(f"/api/articles/{article_id}/file?format=md", "PUT", b"# Measurement note", mime="text/markdown")
        path = f"/api/libraries/{library}/export.jsonl?full_text=1"
        point = json.loads(self.request(path))
        self.assertIn("Measurement note", point["text"])
        new_id, _ = self.catalog.add_article(library, standardized(candidate | {"doi": SAMPLE_DOI}, source))
        self.assertEqual(new_id, article_id)
        self.indexer.catalog = Catalog(self.catalog.path)
        updated = json.loads(self.request(path))
        self.assertEqual((updated["id"], updated["text"]), (point["id"], point["text"]))
        self.assertEqual(updated["doi"], SAMPLE_DOI)

    def test_edits_library_settings_and_reads_them_back(self):
        first = self.add_library("Load-pull", ["load-pull"], [{"kind": "crossref"}], enabled=False)
        second = self.add_library("GaN", ["GaN"], [{"kind": "openalex"}], enabled=False)
        config = {"name": "Load-pull GaN", "keywords": ["load-pull", "GaN"], "exclude": ["tutorial"],
                  "match": "all", "lookback_days": 730, "interval_hours": 24,
                  "sources": [{"kind": "rss", "url": "https://journal.example.net/feed.xml"}],
                  "enabled": False, "download_open_access": False}
        self.request(f"/api/libraries/{first}", "PUT", config)
        libraries = {row["id"]: row for row in self.request("/api/libraries")}
        self.assertEqual({key: libraries[first][key] for key in config}, config)
        self.assertEqual(libraries[second]["keywords"], ["GaN"])
        self.assertEqual(self.request(f"/api/libraries/{first}/runs"), [])

    def test_disappeared_article_returns_error_when_upload_cannot_be_saved(self):
        library = self.add_library("Load-pull", ["load-pull"], [{"kind": "openalex"}], enabled=False)
        article_id, _ = self.catalog.add_article(library, standardized({"title": "Load-pull"}, {"kind": "openalex"}))
        with patch.object(self.catalog, "attach", return_value=False):
            self.request(f"/api/articles/{article_id}/file?format=txt", "PUT", b"Test text", expected=404,
                         mime="text/plain")

    def upload(self, library_id, name, data, expected=201):
        return self.request(f"/api/libraries/{library_id}/files?name={quote(name)}", "POST", data, expected,
                            mime="application/octet-stream")

    def test_library_without_search_takes_files_and_reports_repeats(self):
        self.request("/api/libraries", "POST", {"name": "No phrases", "sources": [{"kind": "openalex"}]}, 400)
        manual = self.request("/api/libraries", "POST", {"name": "Datasheets", "keywords": [], "sources": []}, 201)
        notes = self.upload(manual["id"], "notes.md", b"# Pinout\n\nVDD is pin 1.")
        self.assertEqual((notes["duplicate"], notes["content_kind"]), (False, "full_text"))
        again = self.upload(manual["id"], "renamed-copy.md", b"# Pinout\n\nVDD is pin 1.")
        self.assertEqual((again["id"], again["duplicate"]), (notes["id"], True))
        sheet = self.upload(manual["id"], "LM5170 datasheet.pdf", self.pdf)
        self.assertEqual((sheet["content_kind"], sheet["local_pdf"]), ("full_text", True))
        self.upload(manual["id"], "book.djvu", b"AT&TFORM pages", expected=422)  # no MinerU in this test
        self.upload(manual["id"], "fake.pdf", b"<html>not a pdf</html>", expected=422)
        self.upload(manual["id"], "tool.exe", b"MZ", expected=415)

        items = self.request(f"/api/libraries/{manual['id']}/articles")["items"]
        self.assertEqual(sorted(item["title"] for item in items), ["LM5170 datasheet", "notes"])
        self.assertEqual({tuple(item["sources"]) for item in items}, {("upload",)})
        self.assertEqual(self.request(f"/api/articles/{sheet['id']}/pdf"), self.pdf)
        self.assertIn(b"10 GHz", self.request(f"/api/libraries/{manual['id']}/export.md?full_text=1"))
        stats = next(row for row in self.request("/api/libraries") if row["id"] == manual["id"])
        self.assertEqual((stats["article_count"], stats["full_text_count"], stats["pdf_count"]), (2, 2, 1))
        self.request(f"/api/libraries/{manual['id']}/run", "POST", expected=202)
        self.assertEqual(self.wait_for_run(manual["id"])["found"], 0)

    def test_uploaded_pdf_and_djvu_wait_for_mineru_in_the_parse_queue(self):
        self.indexer.parser = object()  # any parser: these files go to the queue, not to pypdf
        library = self.request("/api/libraries", "POST", {"name": "Books"}, 201)["id"]
        book = self.upload(library, "book.djvu", b"AT&TFORM pages")
        self.assertEqual((book["parse_status"], book["local_pdf"]), ("pending", True))
        self.assertEqual(self.catalog.next_parse()[:2], (book["id"], "djvu"))
        sheet = self.upload(library, "sheet.pdf", self.pdf)
        self.assertEqual(self.catalog.next_parse(), (sheet["id"], "pdf", self.catalog.attachment(sheet["id"], "pdf")))
        self.assertEqual(self.request(f"/api/articles/{book['id']}/pdf"), b"AT&TFORM pages")

    def test_stored_pictures_are_served_by_digest(self):
        digest = self.catalog.files.save([b"\xff\xd8\xff picture"])
        self.assertEqual(self.request(f"/api/files/{digest}.jpg"), b"\xff\xd8\xff picture")
        self.request(f"/api/files/{'0' * 64}.jpg", expected=404)
        self.request(f"/api/files/{digest}.svg", expected=404)

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
