import hashlib
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from catalog import Catalog


class LibraryStorageTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "catalog.sqlite3"
        self.cat = Catalog(self.path)
        self.a, self.b = [self.cat.save_library({"name": name, "enabled": False})["id"]
                          for name in ("Load-pull", "Other")]

    def paper(self, library, source="crossref", **values):
        data = {"title": "Load-pull / измерения?", "doi": "", "authors": ["И. Иванов"], "abstract": "",
                "venue": "Journal", "published": "2011-03", "url": "", "pdf_url": "", "license": "",
                "external_id": "", "origin": source, "source": source}
        return self.cat.add_article(library, data | values)

    def folder(self, article):
        with self.cat.connection() as db:
            name = db.execute("SELECT folder FROM article_folders WHERE article_id=?", (article,)).fetchone()[0]
        return self.root / "articles" / name

    def attach(self, article, data=b"%PDF-demo", text="", **options):
        digest = self.cat.files.save([data])
        self.assertTrue(self.cat.attach(article, "pdf", digest, text, **options))
        return digest

    def items(self, library, scope):
        rows, total = self.cat.articles(library, "", 50, 0, scope=scope)
        self.assertEqual(total, len(rows))
        return [row["id"] for row in rows]

    def test_disk_bundle_with_portable_links_sources_and_replacement(self):
        article, _ = self.paper(self.a, doi="10.1234/test")
        self.assertFalse((self.root / "articles").exists())  # no folder for a search hit alone
        first = self.attach(article, provenance=("https://example.org/oa.pdf", "cc-by"))
        folder = self.folder(article)
        self.assertEqual(folder.relative_to(self.root / "articles").as_posix(),
                         f"2011/{article}-и-иванов-load-pull-измерения")
        self.assertEqual((folder / "original/source.pdf").read_bytes(), b"%PDF-demo")
        self.paper(self.b, source="rss", doi="10.1234/test")
        meta = json.loads((folder / "metadata/article.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["library_ids"], [self.a, self.b])
        self.assertEqual(meta["license"], "cc-by")
        self.assertEqual(meta["pdf_url"], "https://example.org/oa.pdf")
        self.assertEqual([s["origin"] for s in meta["sources"]], ["crossref", "rss"])
        image = b"example figure"
        key = hashlib.sha256(image).hexdigest()
        parsed = SimpleNamespace(body=f"## Method\n\n![Figure 1](/api/files/{key}.jpg)",
                                 references="1. Article", figures={key: image})
        self.assertTrue(self.cat.save_parsed(article, parsed, expected_digest=first))
        body = (folder / "text/article.md").read_text(encoding="utf-8")
        image_ref = body.split("](")[1].rstrip(")")
        self.assertEqual((folder / "text" / image_ref).read_bytes(), image)
        self.assertEqual((folder / "text/references.md").read_text(encoding="utf-8"), "1. Article")
        self.assertTrue(self.cat.visible_figure(key, "jpg"))
        self.attach(article, data=b"%PDF-new", text="new scan")
        self.assertFalse(self.cat.save_parsed(article, parsed, expected_digest=first))
        self.assertFalse(self.cat.visible_figure(key, "jpg"))
        self.assertFalse(self.cat.files.path(first).exists())
        self.assertEqual(list((folder / "figures").iterdir()), [])
        self.assertFalse((folder / "text/references.md").exists())
        self.assertEqual((folder / "original/source.pdf").read_bytes(), b"%PDF-new")
        self.assertEqual((folder / "text/article.md").read_text(), "new scan")

    def test_scoped_exclusion_is_immediate_learns_aliases_survives_restart_and_undo(self):
        article, _ = self.paper(self.a, doi="10.1234/test")
        self.paper(self.b, doi="10.1234/test")
        self.attach(article, text="Load-pull", provenance=("https://example.org/full", "cc0"))
        self.cat.exclude(self.a, article)
        self.assertEqual(self.items(self.a, "full_text"), [])
        self.assertEqual(self.items(self.a, "excluded"), [article])
        self.assertFalse(self.cat.visible(article, self.a))
        self.assertEqual(list(self.cat.export_articles(self.a, True)), [])
        exported = list(self.cat.export_articles(self.b, True))
        self.assertEqual(exported[0]["library_ids"], str(self.b))
        self.assertEqual(self.cat.libraries(self.a)[0]["excluded_count"], 1)
        self.assertEqual(self.paper(self.a, "rss", doi="10.1234/test", external_id="new"), (None, 0))
        self.cat = Catalog(self.path)
        self.assertEqual(self.paper(self.a, "rss", external_id="new"), (None, 0))
        self.cat.restore(self.a, article)
        self.assertEqual(self.items(self.a, "full_text"), [article])

    def test_purge_protects_shared_libraries_and_shared_file_content(self):
        article, _ = self.paper(self.a, doi="10.1234/one", pdf_url="https://example.org/oa")
        self.paper(self.b, doi="10.1234/one")
        separate, _ = self.paper(self.b, doi="10.1234/two")
        digest = self.attach(article, text="one")
        self.attach(separate, text="two")
        folder = self.folder(article)
        for global_scope in (False, True):
            with self.assertRaises(ValueError):
                self.cat.exclude(self.a, article, global_scope=global_scope, purge=True)
            self.assertTrue(self.cat.visible(article, self.a))  # both changes rolled back
        self.cat.exclude(self.a, article, global_scope=True)
        self.cat.exclude(self.a, article, global_scope=True, purge=True)
        self.assertFalse(folder.exists())
        self.assertEqual(self.cat.article(article)["body"], "")
        self.assertTrue(self.cat.files.path(digest).exists())
        self.assertEqual((self.folder(separate) / "original/source.pdf").read_bytes(), b"%PDF-demo")
        self.assertEqual(self.paper(self.b, doi="10.1234/one"), (None, 0))
        self.assertEqual(self.cat.pending(self.b, 5), [])
        self.cat.restore(self.a, article, global_scope=True)
        self.assertEqual([row["id"] for row in self.cat.pending(self.a, 5)], [article])
        self.assertEqual(self.items(self.a, "full_text"), [])
        self.cat.exclude(self.b, separate, purge=True)
        self.assertFalse(self.cat.files.path(digest).exists())

    def test_merged_aliases_keep_blocks_and_remove_duplicate_bundle(self):
        first, _ = self.paper(self.a, "rss", url="https://example.org/one", published="")
        second, _ = self.paper(self.b, "openalex", doi="10.1234/one")
        self.attach(second, text="measurements")
        duplicate = self.folder(second)
        self.cat.exclude(self.b, second)
        result, _ = self.paper(self.a, doi="10.1234/one", url="https://example.org/one")
        self.assertEqual(result, first)
        self.assertFalse(duplicate.exists())
        self.assertFalse(self.cat.visible(first, self.b))
        self.assertEqual(self.items(self.b, "excluded"), [first])
        self.assertTrue((self.folder(first) / "text/article.md").is_file())
        self.assertEqual(self.folder(first).parent.name, "2011")

    def test_purge_between_stage_and_attach_cannot_lose_the_other_upload(self):
        removed, _ = self.paper(self.a, doi="10.1234/discard")
        retained, _ = self.paper(self.b, doi="10.1234/keep")
        content = b"%PDF-1.7 overlapping upload"
        digest = self.attach(removed, data=content, text="old")
        staged, resume = threading.Event(), threading.Event()

        def upload():
            with self.cat.files.stage([content]) as (new_digest, path):
                staged.set()
                self.assertTrue(resume.wait(10))
                self.assertEqual(path.read_bytes(), content)
                return self.cat.attach(retained, "pdf", new_digest, "new", staged=path)

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(upload)
            try:
                self.assertTrue(staged.wait(10))
                self.cat.exclude(self.a, removed, purge=True)
                self.assertFalse(self.cat.files.path(digest).exists())
            finally:
                resume.set()
            self.assertTrue(future.result(timeout=10))
        self.assertEqual(self.cat.files.path(digest).read_bytes(), content)
        self.assertEqual((self.folder(retained) / "original/source.pdf").read_bytes(), content)
        self.assertEqual(self.cat.article(retained)["body"], "new")
        self.assertIsNone(self.cat.attachment(removed, "pdf"))

    def test_status_scopes_and_worker_skip_invisible_items(self):
        active, _ = self.paper(self.a, doi="10.1234/fetch", pdf_url="https://example.org/oa")
        manual, _ = self.paper(self.a, doi="10.1234/manual")
        self.attach(manual)
        self.cat.queue_parse(manual)
        self.cat.download_failed(active, "timeout")
        self.assertEqual(set(self.items(self.a, "metadata_only")), {manual, active})
        self.assertEqual(self.items(self.a, "no_pdf"), [active])
        self.assertEqual(self.items(self.a, "parse_pending"), [manual])
        self.assertEqual(self.items(self.a, "download_failed"), [active])
        self.cat.exclude(self.a, manual)
        self.assertIsNone(self.cat.next_parse())
        self.cat.restore(self.a, manual)
        self.assertEqual(self.cat.next_parse()[0], manual)
        self.assertFalse(self.cat.attach(manual, "pdf", self.cat.files.save([b"other"]), "", only_missing=True))
        counts = self.cat.libraries(self.a)[0]
        self.assertEqual((counts["metadata_only_count"],counts["no_pdf_count"],counts["pdf_count"]), (2,1,1))

    def test_unread_pdf_precedes_a_reparse_and_due_time_is_respected(self):
        old, _ = self.paper(self.a, doi="10.1234/reparse")
        self.attach(old, text="pypdf text")
        self.cat.queue_unparsed_pdfs()
        new, _ = self.paper(self.a, doi="10.1234/unread")
        self.attach(new)
        self.cat.queue_parse(new)
        self.assertEqual(self.cat.next_parse()[0], new)
        self.cat.parse_failed(new, "wait")
        self.assertEqual(self.cat.next_parse()[0], old)

    def test_hash_and_blob_catalogs_backfill_and_resume_an_interrupted_projection(self):
        for old_layout in ("hash", "blob"):
            with self.subTest(old_layout=old_layout):
                article, _ = self.paper(self.a, doi=f"10.1234/{old_layout}")
                data = old_layout.encode()
                digest = hashlib.sha256(data).hexdigest()
                if old_layout == "hash":
                    self.cat.files.save([data])
                with self.cat.connection(write=True) as db:
                    if old_layout == "blob":
                        db.execute("CREATE TABLE attachments_blob(article_id,format,data)")
                        db.execute("INSERT INTO attachments_blob VALUES (?,?,?)", (article,"pdf",data))
                    else:
                        db.execute("INSERT INTO attachments VALUES (?,?,?)", (article,"pdf",digest))
                    db.execute("UPDATE articles SET body='legacy text' WHERE id=?", (article,))
                    db.execute("DELETE FROM catalog_metadata WHERE key='article_folders_v2'")
                with patch("catalog.project", side_effect=OSError("disk interrupted")):
                    with self.assertRaises(OSError):
                        Catalog(self.path)
                self.cat = Catalog(self.path)
                self.assertEqual((self.folder(article) / "text/article.md").read_text(), "legacy text")
                self.assertEqual((self.folder(article) / "original/source.pdf").read_bytes(), data)
                self.assertEqual(self.cat.attachment(article, "pdf"), digest)
