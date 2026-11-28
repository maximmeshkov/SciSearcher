import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject

from app import Indexer, standardized
from catalog import Catalog
from test_workflow import pdf_sample


def scanned_page():
    writer = PdfWriter()
    page = writer.add_blank_page(100, 100)
    image = DecodedStreamObject()
    image.set_data(bytes([127]))
    image.update({NameObject(key): value for key, value in {
        "/Type": NameObject("/XObject"), "/Subtype": NameObject("/Image"), "/Width": NumberObject(1),
        "/Height": NumberObject(1), "/ColorSpace": NameObject("/DeviceGray"), "/BitsPerComponent": NumberObject(8)
    }.items()})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/XObject"): DictionaryObject({
        NameObject("/Img"): writer._add_object(image)})})
    content = DecodedStreamObject()
    content.set_data(b"q 100 0 0 100 0 0 cm /Img Do Q")
    page.replace_contents(content)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


class LocalBackfillTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "catalog.sqlite3"
        self.cat = Catalog(self.path)
        self.library = self.cat.save_library({"name": "Load-pull", "enabled": False})["id"]

    def stored(self, name, data):
        article = self.cat.add_article(self.library, standardized({"title": name}, {"kind": "rss"}))[0]
        digest = self.cat.files.save([data])
        self.cat.attach(article, "pdf", digest, "")
        self.cat.queue_parse(article)
        return article, digest

    def restart_local(self):
        self.cat = Catalog(self.path)
        self.cat.queue_unparsed_pdfs(mineru=False)
        return Indexer(self.cat)

    def test_existing_text_layer_is_indexable_and_image_only_file_waits_for_ocr_across_restart(self):
        real, digest = self.stored("Load-pull experiment", pdf_sample())
        scan, _ = self.stored("Scanned measurement", scanned_page())
        key = (real, digest, "http://mineru", "basic")
        self.cat.mineru_task(key, created=("existing-remote", 1000))
        local = self.restart_local()
        self.assertTrue(local.parse_next())
        self.assertTrue(local.parse_next())
        self.assertFalse(self.restart_local().parse_next())
        self.assertIn("10 GHz", self.cat.article(real)["body"])
        self.assertEqual(self.cat.article(real)["body_format"], "pdf")
        self.assertIn("OCR", self.cat.article(scan)["download_error"])
        self.assertEqual(self.cat.article(scan)["body"], "")
        self.assertEqual(self.cat.libraries(self.library)[0]["parse_waiting"], 0)
        self.assertEqual([row["id"] for row in self.cat.export_articles(self.library, True)], [real])
        self.assertEqual(local.rag.status(self.library)["pending"], 1)
        self.cat.queue_unparsed_pdfs()
        self.assertEqual(self.cat.next_parse()[0], scan)
        self.assertEqual(self.cat.next_parse()[0], real)
        self.assertEqual(self.cat.mineru_task(key)[0], "existing-remote")

    def test_stale_read_cannot_overwrite_new_file_manual_text_or_exclusion(self):
        for outcome in ("replace", "manual", "hide"):
            with self.subTest(outcome=outcome):
                article, _ = self.stored(outcome, pdf_sample())
                local = self.restart_local()
                def during_extraction(data, fmt):
                    if outcome == "replace":
                        self.cat.attach(article, "pdf", self.cat.files.save([scanned_page()]), "")
                    elif outcome == "manual":
                        self.cat.attach(article, "md", self.cat.files.save([b"authored text"]), "authored text")
                    else:
                        self.cat.exclude(self.library, article)
                    return "out-of-date read", 1
                with patch("app.extract", side_effect=during_extraction):
                    self.assertTrue(local.parse_next())
                self.assertEqual(self.cat.article(article)["body"], "authored text" if outcome == "manual" else "")
                if outcome == "replace":
                    self.assertTrue(local.parse_next())
                self.assertFalse(local.parse_next())
