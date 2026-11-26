import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import Indexer, library_config
from catalog import Catalog
from sources import _REGISTRY, fetch
from sources.types import Candidate, Field


class AdapterResultsTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1])
        self.addCleanup(temp.cleanup)
        self.catalog = Catalog(Path(temp.name) / "catalog.sqlite3")
        self.library = self.catalog.save_library(library_config({
            "name": "Load-pull", "keywords": ["load-pull"],
            "sources": [{"kind": "openalex"}, {"kind": "crossref"}],
            "download_open_access": False,
        }))

    def run_with(self, first, second):
        with (
            patch.object(_REGISTRY["openalex"], "search", return_value=first),
            patch.object(_REGISTRY["crossref"], "search", return_value=second),
        ):
            self.assertTrue(Indexer(self.catalog, search=fetch).run_next())
        return self.catalog.runs(self.library["id"])[0]

    def test_candidate_only_requires_title_and_source_field_supports_pattern(self):
        self.assertEqual(Candidate.__required_keys__, {"title"})
        self.assertIn("pattern", Field.__optional_keys__)

    def test_bad_candidate_keeps_valid_results_in_same_and_later_sources(self):
        run = self.run_with([
            {"title": "Load-pull fixture A"},
            None,
            {"abstract": "Load-pull fixture without title"},
            {"title": "Load-pull with malformed authors", "authors": {"name": "Test"}},
            {"title": "Load-pull with malformed keywords", "keywords": 123},
            {"title": "Load-pull with invalid UTF-8 \ud800"},
            {"title": "Load-pull fixture B", "source": "ignored"},
        ], [{"title": "Load-pull fixture C"}])

        self.assertEqual((run["status"], run["found"], run["added"]), ("partial", 3, 3))
        self.assertEqual(len(run["errors"]), 5)
        articles, _ = self.catalog.articles(self.library["id"], "", 50, 0)
        self.assertEqual({paper["title"]: paper["sources"] for paper in articles}, {
            "Load-pull fixture A": ["openalex"], "Load-pull fixture B": ["openalex"],
            "Load-pull fixture C": ["crossref"],
        })

    def test_bad_batch_does_not_stop_next_adapter(self):
        run = self.run_with({"title": "Load-pull fixture A"}, [{"title": "Load-pull fixture B"}])
        self.assertEqual((run["status"], run["found"], run["added"]), ("partial", 1, 1))
        self.assertIn("OpenAlex", run["errors"][0])

    def test_catalogue_failure_is_reported_as_a_run_failure(self):
        with (
            patch.object(self.catalog, "add_article", side_effect=sqlite3.OperationalError("disk I/O error")),
            self.assertLogs("root", level="ERROR") as logged,
        ):
            run = self.run_with([{"title": "Load-pull fixture A"}], [{"title": "Load-pull fixture B"}])

        self.assertEqual((run["status"], run["found"], run["added"]), ("error", 0, 0))
        self.assertEqual(len(run["errors"]), 1)
        self.assertIn("disk I/O error", run["errors"][0])
        self.assertIn("sqlite3.OperationalError", logged.output[0])

    def test_archive_keeps_pages_beyond_one_hundred_and_filters_duplicate_dois(self):
        def pages(source, keyword, since, limit, get):
            self.assertEqual((keyword, since), ("load-pull", date.min))
            works = [{"title": f"Load-pull measurements {i}", "doi": f"10.1000/{i}"}
                     for i in range(140)]
            yield works[:100]
            yield [*works[90:], {"title": "Unrelated measurement"}]

        kind = "openalex"
        module = SimpleNamespace(SPEC=_REGISTRY[kind].SPEC, pages=pages)
        self.catalog.save_library(library_config({
            "name": "Load-pull archive", "keywords": ["load-pull"], "sources": [{"kind": kind}],
            "lookback_days": 0, "download_open_access": False,
        }), self.library["id"])
        with patch.dict(_REGISTRY, {kind: module}):
            indexer = Indexer(self.catalog)
            self.assertTrue(indexer.run_next())
            self.catalog.queue(self.library["id"])
            self.assertTrue(indexer.run_next())
        first, second = reversed(self.catalog.runs(self.library["id"]))
        self.assertEqual((first["status"], first["found"], first["added"]), ("ok", 140, 140))
        self.assertEqual((second["status"], second["found"], second["added"]), ("ok", 140, 0))
        self.assertEqual(self.catalog.libraries(self.library["id"])[0]["article_count"], 140)

    def test_later_page_error_keeps_earlier_results_and_allows_next_source(self):
        def incomplete(*args):
            yield [{"title": "Load-pull from page one", "doi": "10.1000/a"}]
            raise ValueError("Request failed after the first page")

        def complete(*args):
            yield [{"title": "Load-pull from Crossref", "doi": "10.1000/b"}]

        overrides = {kind: SimpleNamespace(SPEC=_REGISTRY[kind].SPEC, pages=pages)
                     for kind, pages in (("openalex", incomplete), ("crossref", complete))}
        with patch.dict(_REGISTRY, overrides):
            self.assertTrue(Indexer(self.catalog).run_next())
        run, = self.catalog.runs(self.library["id"])
        self.assertEqual((run["status"], run["found"], run["added"]), ("partial", 2, 2))
        self.assertIn("Request failed after the first page", run["errors"][0])
