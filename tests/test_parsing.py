import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from app import Indexer
from catalog import Catalog
from documents import chunks, from_mineru
from mineru import result_parts


def span(text):
    return {"type": "text", "content": text}


# Two pages shaped like MinerU 4.2.3 middle JSON (schema docvortex.middle 2.0).
MIDDLE = {"pages": [
    {"page_idx": 0, "blocks": [
        {"type": "header", "content": [span("IEEE MICROWAVE LETTERS, VOL. 35")]},
        {"type": "doc_title", "content": [span("Active Load-Pull Setup")]},
        {"type": "paragraph_title", "content": [span("II. Method")]},
        {"type": "text", "content": [span("The load reflection is"), {"type": "equation_inline", "content": "\\Gamma_L"},
                                     span("at the fundamental.")]},
        {"type": "equation", "content": "\\Gamma_L = \\frac{a_2}{b_2}"},
        {"type": "page_number", "content": [span("2121")]},
    ]},
    {"page_idx": 1, "blocks": [
        {"type": "text", "content": [span("Measured PAE stays above 60 %.")]},
        {"type": "table", "content": [
            {"type": "table_caption", "content": [span("TABLE I. Results")]},
            {"type": "table_body", "content": "<table><tr><td>PAE</td><td>62 %</td></tr></table>"}]},
        {"type": "chart", "content": [
            {"type": "chart_body", "image_path": "images/page_2_chart_3.jpg", "content": ""},
            {"type": "chart_caption", "content": [span("Fig. 3. Load-pull contours.")]}]},
        {"type": "ref_text", "content": [span("[1] D. Root, X-parameters, 2013.")]},
        {"type": "footer", "content": [span("Authorized licensed use limited")]},
    ]},
]}


PICTURE = b"\xff\xd8\xff\xe0 chart picture"


class PreparedTextTest(unittest.TestCase):
    def test_builds_markdown_without_page_furniture_and_keeps_references_apart(self):
        prepared = from_mineru(MIDDLE)
        self.assertEqual(prepared.pages, 2)
        self.assertEqual(prepared.references, "[1] D. Root, X-parameters, 2013.")
        self.assertIn("# Active Load-Pull Setup\n\n## II. Method", prepared.body)
        self.assertIn("The load reflection is $\\Gamma_L$ at the fundamental.", prepared.body)
        self.assertIn("$$\n\\Gamma_L = \\frac{a_2}{b_2}\n$$", prepared.body)
        self.assertIn("TABLE I. Results\n\n<table><tr><td>PAE</td><td>62 %</td></tr></table>", prepared.body)
        self.assertIn("Fig. 3. Load-pull contours.", prepared.body)
        for furniture in ("IEEE MICROWAVE LETTERS", "2121", "Authorized licensed", "X-parameters"):
            self.assertNotIn(furniture, prepared.body)

    def test_chunks_keep_the_section_across_pages(self):
        pieces = list(chunks(from_mineru(MIDDLE).body))
        self.assertEqual([(piece.get("page"), piece.get("section")) for piece in pieces],
                         [(1, "Active Load-Pull Setup / II. Method"), (2, "Active Load-Pull Setup / II. Method")])

    def test_rejects_results_without_text(self):
        with self.assertRaises(ValueError):
            from_mineru({"pages": [{"page_idx": 0, "blocks": [{"type": "page_number", "content": "1"}]}]})
        with self.assertRaises(ValueError):
            from_mineru({"pages": []})

    def test_reads_middle_json_and_pictures_from_the_result_archive(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as files:
            files.writestr("markdown.md", "# ignored")
            files.writestr("structured_content.json", json.dumps({"blocks": []}))
            files.writestr("middle_json.json", json.dumps(MIDDLE))
            files.writestr("images/", "")
            files.writestr("images/page_2_chart_3.jpg", PICTURE)
        self.assertEqual(result_parts(archive.getvalue()), (MIDDLE, {"images/page_2_chart_3.jpg": PICTURE}))
        self.assertEqual(result_parts(json.dumps(MIDDLE).encode()), (MIDDLE, {}))
        with self.assertRaises(ValueError):
            result_parts(json.dumps({"blocks": []}).encode())

    def test_figures_link_to_their_stored_pictures(self):
        prepared = from_mineru(MIDDLE, {"images/page_2_chart_3.jpg": PICTURE})
        digest = hashlib.sha256(PICTURE).hexdigest()
        self.assertIn(f"![chart](/api/files/{digest}.jpg)\n\nFig. 3. Load-pull contours.", prepared.body)
        self.assertEqual(prepared.figures, {digest: PICTURE})
        self.assertNotIn("![chart]", from_mineru(MIDDLE).body)  # no picture in the result: caption only


class FakeMinerU:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.files = []

    def parse(self, path, file_name):
        self.files.append((Path(path).read_bytes(), file_name))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome if isinstance(outcome, tuple) else (outcome, {})


class ParseQueueTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(temp.cleanup)
        self.catalog = Catalog(Path(temp.name) / "catalog.sqlite3")
        self.library = self.catalog.save_library({"name": "Load-pull", "enabled": False})["id"]

    def add(self, **metadata):
        paper = {"title": "Active load-pull", "authors": [], "doi": "", "abstract": "", "venue": "",
                 "published": "", "url": "", "pdf_url": "", "external_id": "", "license": "",
                 "source": "openalex", "origin": "openalex"} | metadata
        return self.catalog.add_article(self.library, paper)[0]

    def file(self, data):
        return self.catalog.files.save([data])

    def queued_pdf(self, **metadata):
        article = self.add(**metadata)
        self.catalog.attach(article, "pdf", self.file(b"%PDF-1.7 bytes"), "")
        self.catalog.queue_parse(article)
        return article

    def parse_state(self, article):
        with self.catalog.connection() as db:
            return db.execute("SELECT status, attempts, error FROM parse_queue WHERE article_id = ?",
                              (article,)).fetchone()

    def test_successful_parse_stores_text_and_references_and_leaves_the_queue(self):
        article = self.queued_pdf()
        parser = FakeMinerU(MIDDLE)
        self.assertTrue(Indexer(self.catalog, parser=parser).parse_next())
        self.assertEqual(parser.files, [(b"%PDF-1.7 bytes", f"article-{article}.pdf")])
        stored = self.catalog.article(article)
        self.assertEqual((stored["body_format"], stored["download_status"]), ("mineru", "downloaded"))
        self.assertIn("## II. Method", stored["body"])
        self.assertIsNone(self.parse_state(article))
        with self.catalog.connection() as db:
            self.assertEqual(db.execute("SELECT text FROM article_references").fetchone()[0],
                             "[1] D. Root, X-parameters, 2013.")
        self.assertEqual(self.catalog.libraries(self.library)[0]["full_text_count"], 1)
        self.assertFalse(Indexer(self.catalog, parser=FakeMinerU()).parse_next())

    def test_parse_error_is_retried_later_but_an_unreachable_server_costs_nothing(self):
        article = self.queued_pdf()
        Indexer(self.catalog, parser=FakeMinerU(ValueError("MinerU: HTTP 500"))).parse_next()
        self.assertEqual(tuple(self.parse_state(article)), ("failed", 1, "MinerU: HTTP 500"))
        self.assertIsNone(self.catalog.next_parse())  # retry waits for its pause
        listed, _ = self.catalog.articles(self.library, "", 50, 0)
        self.assertEqual((listed[0]["parse_status"], listed[0]["parse_error"]), ("failed", "MinerU: HTTP 500"))
        self.assertEqual(self.catalog.libraries(self.library)[0]["parse_failed"], 1)

        other = self.queued_pdf(doi="10.1234/other")
        indexer = Indexer(self.catalog, parser=FakeMinerU(ConnectionError("MinerU недоступен")))
        self.assertFalse(indexer.parse_next())
        self.assertEqual(tuple(self.parse_state(other)), ("pending", 0, ""))

    def test_text_supplied_by_the_user_wins_over_a_late_parse(self):
        article = self.queued_pdf()
        self.catalog.attach(article, "xml", self.file(b"<article/>"), "Publisher text")
        Indexer(self.catalog, parser=FakeMinerU(MIDDLE)).parse_next()
        self.assertEqual(self.catalog.article(article)["body"], "Publisher text")
        self.assertIsNone(self.parse_state(article))

    def test_old_pypdf_texts_are_queued_once_and_interrupted_parses_resume(self):
        old = self.add(doi="10.1234/old")
        self.catalog.attach(old, "pdf", self.file(b"%PDF-1.4"), "pypdf text")
        typed = self.add(doi="10.1234/typed")
        self.catalog.attach(typed, "pdf", self.file(b"%PDF-1.4"), "")
        self.catalog.attach(typed, "md", self.file(b"# Notes"), "# Notes")
        self.assertEqual(self.catalog.queue_unparsed_pdfs(), 1)
        self.assertEqual(self.catalog.next_parse(), (old, "pdf", self.file(b"%PDF-1.4")))
        self.assertEqual(self.parse_state(old)["status"], "parsing")
        self.assertEqual(self.catalog.queue_unparsed_pdfs(), 0)
        self.assertEqual(self.parse_state(old)["status"], "pending")

    def test_merging_a_queued_duplicate_moves_its_queue_entry(self):
        survivor = self.add(doi="10.1234/merged")
        duplicate = self.queued_pdf(url="https://example.org/article/7")
        merged = self.add(doi="10.1234/merged", url="https://example.org/article/7")
        self.assertEqual(merged, survivor)
        self.assertIsNone(self.catalog.article(duplicate))
        self.assertEqual(self.parse_state(survivor)["status"], "pending")
        self.assertEqual(self.catalog.next_parse(), (survivor, "pdf", self.file(b"%PDF-1.7 bytes")))

    def test_parse_stores_the_pictures_its_text_links_to(self):
        article = self.queued_pdf()
        Indexer(self.catalog, parser=FakeMinerU((MIDDLE, {"images/page_2_chart_3.jpg": PICTURE}))).parse_next()
        digest = hashlib.sha256(PICTURE).hexdigest()
        self.assertIn(f"/api/files/{digest}.jpg", self.catalog.article(article)["body"])
        self.assertEqual(self.catalog.files.path(digest).read_bytes(), PICTURE)

    def test_djvu_is_rendered_to_pdf_before_parsing(self):
        article = self.add()
        self.catalog.attach(article, "djvu", self.file(b"AT&TFORM book"), "")
        self.catalog.queue_parse(article)

        def render(source, target):
            self.assertEqual(Path(source).read_bytes(), b"AT&TFORM book")
            Path(target).write_bytes(b"%PDF-rendered pages")
            return target

        parser = FakeMinerU(MIDDLE)
        with patch("app.djvu_to_pdf", side_effect=render):
            self.assertTrue(Indexer(self.catalog, parser=parser).parse_next())
        self.assertEqual(parser.files, [(b"%PDF-rendered pages", f"article-{article}.pdf")])
        self.assertEqual(self.catalog.article(article)["body_format"], "mineru")

    def test_pdf_is_preferred_when_an_article_has_pdf_and_djvu(self):
        article = self.add()
        self.catalog.attach(article, "djvu", self.file(b"AT&TFORM book"), "")
        self.catalog.attach(article, "pdf", self.file(b"%PDF-1.7 book"), "")
        self.catalog.queue_parse(article)
        self.assertEqual(self.catalog.next_parse(), (article, "pdf", self.file(b"%PDF-1.7 book")))


if __name__ == "__main__":
    unittest.main()
