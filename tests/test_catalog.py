import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from catalog import Catalog


DOI = "10.1234/load.pull"
PERMALINK = "https://example.org/journal/42"
COPY = "https://example.org/article.pdf"


class CatalogTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "scisearcher.sqlite3"
        self.catalog = Catalog(self.path)
        self.library = self.catalog.save_library({"name": "Load-pull", "enabled": False})["id"]
        self.other = self.catalog.save_library({"name": "GaN", "enabled": False})["id"]

    def add(self, library, source, **metadata):
        paper = {"title": "Load-pull measurements", "authors": [], "doi": "", "abstract": "", "venue": "",
                 "published": "", "url": "", "pdf_url": "", "external_id": "", "license": "",
                 "source": source, "origin": source} | metadata
        return self.catalog.add_article(library, paper)[0]

    def file(self, data):
        return self.catalog.files.save([data])

    def test_new_alias_joins_records_and_preserves_library_content(self):
        older = self.add(self.library, "rss", url=PERMALINK, external_id="item-42")
        self.catalog.attach(older, "pdf", self.file(b"scan bytes"), "", error="No text")
        newer = self.add(self.other, "openalex", doi=DOI, abstract="An abstract", external_id="work-42")
        self.catalog.attach(newer, "txt", self.file(b"Readable text"), "Readable text")

        self.assertEqual(self.add(self.library, "crossref", doi=DOI, url=PERMALINK), older)
        self.assertEqual(self.add(self.other, "rss", external_id="item-42"), older)
        self.assertEqual(self.add(self.other, "openalex", external_id="work-42"), older)
        self.assertIsNone(self.catalog.article(newer))
        article = self.catalog.article(older)
        self.assertEqual((article["doi"], article["abstract"], article["body"], article["download_status"]),
                         (DOI, "An abstract", "Readable text", "downloaded"))
        self.assertEqual(self.catalog.attachment(older, "pdf"), self.file(b"scan bytes"))
        self.assertEqual(self.catalog.attachment(older, "txt"), self.file(b"Readable text"))

        for library in (self.library, self.other):
            rows = list(self.catalog.export_articles(library, full_text_only=True))
            self.assertEqual([row["id"] for row in rows], [older])
            self.assertEqual(set(rows[0]["sources"].split(",")), {"crossref", "openalex", "rss"})
            self.assertEqual(set(map(int, rows[0]["library_ids"].split(","))), {self.library, self.other})
        self.assertGreater(self.add(self.library, "rss", url=PERMALINK + "/new"), newer)

    def test_distinct_files_of_the_same_format_prevent_destructive_merge(self):
        older = self.add(self.library, "rss", url=PERMALINK)
        self.catalog.attach(older, "txt", self.file(b"Old text"), "Old text")
        newer = self.add(self.other, "openalex", doi=DOI)
        self.catalog.attach(newer, "txt", self.file(b"New text"), "New text")

        with self.assertLogs(level="WARNING"):
            self.assertEqual(self.add(self.other, "crossref", doi=DOI, url=PERMALINK), newer)
        self.assertEqual(self.catalog.attachment(older, "txt"), self.file(b"Old text"))
        self.assertEqual(self.catalog.attachment(newer, "txt"), self.file(b"New text"))

    def test_failed_write_rolls_back_a_merge(self):
        older = self.add(self.library, "rss", url=PERMALINK)
        newer = self.add(self.other, "openalex", doi=DOI)
        self.catalog.attach(newer, "txt", self.file(b"Readable text"), "Readable text")

        with self.assertRaises(sqlite3.IntegrityError):
            self.add(99, "crossref", doi=DOI, url=PERMALINK)

        self.assertEqual(self.add(self.library, "rss", url=PERMALINK), older)
        self.assertEqual(self.add(self.other, "openalex", doi=DOI), newer)
        self.assertEqual(self.catalog.attachment(newer, "txt"), self.file(b"Readable text"))
        self.assertEqual(self.catalog.libraries(self.library)[0]["full_text_count"], 0)
        self.assertEqual(self.catalog.libraries(self.other)[0]["full_text_count"], 1)

    def test_duplicate_attachment_bytes_allow_merge(self):
        older = self.add(self.library, "rss", url=PERMALINK)
        newer = self.add(self.other, "openalex", doi=DOI)
        for article_id in (older, newer):
            self.catalog.attach(article_id, "txt", self.file(b"Same content"), "Same content")

        self.assertEqual(self.add(self.library, "crossref", doi=DOI, url=PERMALINK), older)
        self.assertEqual(self.catalog.attachment(older, "txt"), self.file(b"Same content"))
        self.assertIsNone(self.catalog.article(newer))

    def test_shared_url_does_not_merge_different_dois(self):
        older = self.add(self.library, "rss", doi="10.1234/other", url=PERMALINK)
        newer = self.add(self.other, "openalex", doi=DOI, external_id="work-42")

        self.assertEqual(self.add(self.other, "openalex", doi=DOI, url=PERMALINK, external_id="work-42"), newer)
        self.add(self.other, "openalex", url=PERMALINK, external_id="work-42")
        self.assertEqual(self.catalog.article(older)["doi"], "10.1234/other")
        self.assertEqual(self.catalog.article(newer)["doi"], DOI)

    def test_merge_preserves_retry_and_new_copy_can_be_claimed(self):
        older = self.add(self.library, "rss", url=PERMALINK)
        newer = self.add(self.other, "openalex", doi=DOI, pdf_url=COPY, license="cc-by")
        self.catalog.download_failed(newer, "Unavailable")
        retry_at = self.catalog.article(newer)["retry_at"]

        self.assertEqual(self.add(self.library, "crossref", doi=DOI, url=PERMALINK), older)
        article = self.catalog.article(older)
        self.assertEqual((article["download_status"], article["retry_at"], article["download_attempts"]),
                         ("failed", retry_at, 1))
        self.assertEqual(self.catalog.pending(self.library, 40), [])
        replacement = "https://example.org/repository.pdf"
        self.add(self.library, "crossref", doi=DOI, pdf_url=replacement, license="cc0")
        self.assertEqual(self.catalog.article(older)["license"], "cc0")
        self.assertEqual([(row["id"], row["pdf_url"]) for row in self.catalog.pending(self.library, 40)],
                         [(older, replacement)])

    def test_pending_processes_oldest_eligible_first_as_new_copies_arrive(self):
        first_run = datetime(2020, 1, 1, 12, tzinfo=timezone.utc)
        with patch("catalog.datetime") as clock:
            clock.now.return_value = first_run
            first = self.add(self.library, "rss", url=PERMALINK + "/1", pdf_url=COPY + "?id=1")
            second = self.add(self.library, "rss", url=PERMALINK + "/2", pdf_url=COPY + "?id=2")
            self.assertEqual(self.catalog.pending(self.library, 1)[0]["id"], first)
            self.catalog.download_failed(first, "Unavailable")
            self.assertEqual(self.catalog.pending(self.library, 1)[0]["id"], second)

            clock.now.return_value = first_run + timedelta(hours=2)
            third = self.add(self.library, "rss", url=PERMALINK + "/3", pdf_url=COPY + "?id=3")
            self.assertEqual([row["id"] for row in self.catalog.pending(self.library, 2)], [second, first])
            self.catalog.attach(second, "pdf", self.file(b"second"), "Second PDF")
            self.assertEqual([row["id"] for row in self.catalog.pending(self.library, 2)], [first, third])

    def test_files_kept_inside_an_old_database_move_to_the_file_store(self):
        path = self.path.with_name("old.sqlite3")
        with sqlite3.connect(path) as db:
            db.executescript("""
                CREATE TABLE articles (id INTEGER PRIMARY KEY, title TEXT NOT NULL, doi TEXT NOT NULL,
                    abstract TEXT NOT NULL, abstract_source TEXT NOT NULL, authors TEXT NOT NULL, venue TEXT NOT NULL,
                    published TEXT NOT NULL, url TEXT NOT NULL, pdf_url TEXT NOT NULL, license TEXT NOT NULL,
                    search_text TEXT NOT NULL, body TEXT NOT NULL DEFAULT '', body_format TEXT NOT NULL DEFAULT '',
                    download_status TEXT NOT NULL DEFAULT 'unavailable', download_error TEXT NOT NULL DEFAULT '',
                    retry_at TEXT NOT NULL DEFAULT '', download_attempts INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE attachments (article_id INTEGER NOT NULL REFERENCES articles(id), format TEXT NOT NULL,
                    data BLOB NOT NULL, PRIMARY KEY (article_id, format));
                INSERT INTO articles (id, title, doi, abstract, abstract_source, authors, venue, published, url,
                    pdf_url, license, search_text, body) VALUES (7, 'Old', '', '', '', '', '', '', '', '', '', '', 'T');
                INSERT INTO attachments VALUES (7, 'pdf', X'255044462D312E34');""")
        db.close()
        catalog = Catalog(path)
        digest = catalog.attachment(7, "pdf")
        self.assertEqual(catalog.files.path(digest).read_bytes(), b"%PDF-1.4")
        with catalog.connection() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name = 'attachments_blob'").fetchone())
        self.assertEqual(Catalog(path).attachment(7, "pdf"), digest)

    def test_file_store_keeps_one_copy_per_content_and_rejects_foreign_names(self):
        first = self.catalog.files.save([b"same ", b"bytes"])
        self.assertEqual(self.catalog.files.save([b"same bytes"]), first)
        self.assertEqual([item.name for item in self.catalog.files.root.iterdir()], [first])
        with self.assertRaises(ValueError):
            self.catalog.files.path("../catalog.sqlite3")

    def test_library_without_sources_is_never_scheduled(self):
        manual = self.catalog.save_library({"name": "Datasheets", "enabled": True, "sources": []})["id"]
        with self.catalog.connection() as db:
            self.assertEqual(db.execute("SELECT enabled FROM libraries WHERE id = ?", (manual,)).fetchone()[0], 0)
        self.assertIsNone(self.catalog.claim())

    def test_catalog_identity_migrates_and_persists(self):
        article_id = self.add(self.library, "rss", url=PERMALINK)
        with self.catalog.connection(write=True) as db:
            db.execute("DROP TABLE catalog_metadata")
        self.catalog = Catalog(self.path)
        identity = self.catalog.identity
        self.assertEqual(UUID(identity).version, 4)
        self.assertTrue(self.catalog.available())
        self.assertEqual(Catalog(self.path).identity, identity)
        self.assertNotEqual(Catalog(self.path.with_name("empty.sqlite3")).identity, identity)
        self.assertEqual(self.catalog.article(article_id)["url"], PERMALINK)
        self.assertGreater(self.add(self.library, "rss", url=PERMALINK + "/new"), article_id)
