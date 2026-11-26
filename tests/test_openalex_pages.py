import json
import unittest
from datetime import date
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from sources import openalex


class OpenAlexPagesTest(unittest.TestCase):
    def getter(self, *responses):
        self.queries = []
        remaining = iter(responses)

        def get(url, _limit, *, accept):
            self.assertEqual(urlsplit(url).netloc, "api.openalex.org")
            self.assertEqual(accept, "application/json")
            self.queries.append({key: values[0] for key, values in parse_qs(urlsplit(url).query).items()})
            items, meta = next(remaining)
            results = [{"id": f"https://openalex.org/W{number}", "display_name": f"Load-pull {number}"}
                       for number in items]
            return json.dumps({"results": results, "meta": meta}).encode()

        return get

    @patch.dict("os.environ", {"OPENALEX_API_KEY": ""})
    def test_anonymous_load_pull_uses_numbered_pages(self):
        get = self.getter(([1, 2], {"count": 5}), ([3, 4], {"count": 5}), ([5], {"count": 5}))
        batches = list(openalex.pages({"issn": "1234-567X"}, "load-pull", date.min, 2, get))
        self.assertEqual([[row["external_id"] for row in batch] for batch in batches], [
            ["https://openalex.org/W1", "https://openalex.org/W2"],
            ["https://openalex.org/W3", "https://openalex.org/W4"],
            ["https://openalex.org/W5"],
        ])
        self.assertEqual([query["page"] for query in self.queries], ["1", "2", "3"])
        for query in self.queries:
            self.assertEqual(query["search.title_abstract_keywords"], '"load-pull"')
            self.assertIn("primary_location.source.type:journal", query["filter"])
            self.assertIn("primary_location.source.issn:1234-567X", query["filter"])
            self.assertNotIn("from_publication_date", query["filter"])
            self.assertNotIn("cursor", query)

    @patch.dict("os.environ", {"OPENALEX_API_KEY": "test-key"})
    def test_authenticated_cursor_follows_nonempty_short_page(self):
        get = self.getter(([1], {"next_cursor": "next+/="}), ([2], {"next_cursor": None}))
        batches = list(openalex.pages({}, "load-pull", date(1980, 1, 1), 150, get))
        self.assertEqual([batch[0]["title"] for batch in batches], ["Load-pull 1", "Load-pull 2"])
        self.assertEqual([query["cursor"] for query in self.queries], ["*", "next+/="])
        for query in self.queries:
            self.assertEqual(query["api_key"], "test-key")
            self.assertEqual(query["per_page"], "100")
            self.assertIn("from_publication_date:1980-01-01", query["filter"])
            self.assertNotIn("page", query)

    @patch.dict("os.environ", {"OPENALEX_API_KEY": "test-key"})
    def test_repeated_cursor_reports_partial_results(self):
        get = self.getter(([1], {"next_cursor": "repeat"}), ([2], {"next_cursor": "repeat"}))
        batches = openalex.pages({}, "load-pull", date.min, 1, get)
        self.assertEqual(next(batches)[0]["title"], "Load-pull 1")
        self.assertEqual(next(batches)[0]["title"], "Load-pull 2")
        with self.assertRaisesRegex(ValueError, "некорректный курсор"):
            next(batches)

    @patch.dict("os.environ", {"OPENALEX_API_KEY": ""})
    @patch.object(openalex, "_BASIC_PAGING_LIMIT", 2)
    def test_anonymous_limit_is_reported_after_last_page(self):
        get = self.getter(([1], {"count": 3}), ([2], {"count": 3}))
        batches = openalex.pages({}, "load-pull", date.min, 1, get)
        next(batches)
        next(batches)
        with self.assertRaisesRegex(ValueError, "OPENALEX_API_KEY"):
            next(batches)

    @patch.dict("os.environ", {"OPENALEX_API_KEY": ""})
    def test_single_batch_search_still_returns_one_page(self):
        get = self.getter(([1], {"count": 20}))
        results = openalex.search({}, "load-pull", date.min, 1, get)
        self.assertEqual([row["title"] for row in results], ["Load-pull 1"])
        self.assertNotIn("page", self.queries[0])
