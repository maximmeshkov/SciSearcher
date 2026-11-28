import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app import Indexer, library_config, standardized
from catalog import Catalog
from discovery import Discovery


class DiscoveryQueueTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.catalog = Catalog(Path(directory.name) / "catalog.sqlite3")
        self.indexer = Indexer(self.catalog)
        self.queue = self.indexer.discovery
        self.library = self.catalog.save_library(library_config({
            "name": "Load-pull", "keywords": ["load-pull"], "sources": [{"kind": "crossref"}],
        }))["id"]

    def add(self, suffix):
        record = standardized({"title": "Load-pull " + suffix, "doi": "10.1234/" + suffix}, {"kind": "crossref"})
        return self.catalog.add_article(self.library, record)[0]

    def test_manual_priority_and_retry_after_restart(self):
        first, chosen = self.add("first"), self.add("chosen")
        self.assertEqual(self.queue.queue(self.library)["queued"], 2)
        self.queue.queue(self.library, chosen)
        self.assertEqual(self.queue._claim()["id"], chosen)
        restarted = Discovery(self.indexer)
        with patch.object(restarted, "_lookup", return_value=("no_copy", "")) as run:
            self.assertTrue(restarted.run_once())
            self.assertTrue(restarted.run_once())
            self.assertFalse(restarted.run_once())
            self.assertEqual([call.args[0]["id"] for call in run.call_args_list], [chosen, first])
        self.assertEqual(restarted.status(self.library)["no_copy"], 2)
        self.catalog.exclude(self.library, chosen)
        with self.assertRaises(ValueError):
            restarted.queue(self.library, chosen)
        self.assertEqual(restarted.queue(self.library)["queued"], 1)
        self.assertEqual(restarted._claim()["id"], first)

    def test_disabled_scheduled_library_accepts_an_explicit_request(self):
        paper = self.add("manual")
        self.catalog.save_library(library_config({"name": "Load-pull", "enabled": False}), self.library)
        self.assertFalse(self.queue.run_once())
        self.queue.queue(self.library, paper)
        with patch.object(self.queue, "_lookup", return_value=("failed", "Crossref: HTTP 503")):
            self.assertTrue(self.queue.run_once())
        items = [{"id": paper}]
        self.queue.annotate(items)
        self.assertEqual(items[0]["discovery_detail"], "Crossref: HTTP 503")
        self.assertGreater(items[0]["discovery_next_try"], 0)
        self.assertFalse(self.queue.run_once())

    def test_local_pdf_awaiting_mineru_is_not_downloaded_again(self):
        paper = self.add("ready")
        self.indexer.parser = Mock()
        self.assertTrue(self.indexer.store_pdf(paper, b"%PDF-1.4 example"))
        self.assertFalse(self.queue.run_once())
        self.assertEqual(self.queue.queue(self.library), {"queued": 0})
        self.assertEqual(self.catalog.next_parse()[0], paper)
