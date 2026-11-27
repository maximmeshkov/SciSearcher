import json
import unittest
from datetime import date
from urllib.parse import parse_qs, urlsplit

from sources.crossref import pages, search


def item(number):
    return {"DOI": f"10.1234/load-pull.{number}", "title": [f"Load-pull study {number}"],
            "published": {"date-parts": [[2000, 4, number]]}}


class CrossrefPagesTest(unittest.TestCase):
    def getter(self, *messages):
        calls = []
        responses = iter(messages)

        def get(url, _max_bytes, *, accept):
            self.assertEqual(urlsplit(url).hostname, "api.crossref.org")
            self.assertEqual(accept, "application/json")
            calls.append(parse_qs(urlsplit(url).query))
            return json.dumps({"message": next(responses)}).encode()

        return calls, get

    def test_preserves_filters_and_passes_cursor_until_last_short_page(self):
        calls, get = self.getter(
            {"items": [item(1), item(2)], "next-cursor": "page+/=2", "total-results": 3},
            {"items": [item(3)], "next-cursor": "ignored", "total-results": 3},
        )
        records = list(pages({"issn": "0018-9480"}, "load-pull", date(2000, 1, 1), 2, get))
        self.assertEqual([[p["doi"] for p in batch] for batch in records],
                         [["10.1234/load-pull.1", "10.1234/load-pull.2"], ["10.1234/load-pull.3"]])
        self.assertEqual([call.pop("cursor") for call in calls], [["*"], ["page+/=2"]])
        self.assertEqual(calls[0], calls[1])
        self.assertIn("from-pub-date:2000-01-01", calls[0]["filter"][0])
        self.assertIn("issn:0018-9480", calls[0]["filter"][0])
        self.assertEqual(calls[0]["query.bibliographic"], ["load-pull"])

    def test_passes_no_start_date_for_all_years_and_accepts_exact_last_page(self):
        calls, get = self.getter({"items": [item(1), item(2)], "total-results": 2})
        self.assertEqual([len(batch) for batch in pages({}, "load-pull", date.min, 2, get)], [2])
        self.assertNotIn("from-pub-date:", calls[0]["filter"][0])

    def test_a_stuck_cursor_reports_partial_results(self):
        _, get = self.getter(
            {"items": [item(1)], "next-cursor": "next", "total-results": 3},
            {"items": [item(2)], "next-cursor": "next", "total-results": 3},
        )
        records = pages({}, "load-pull", date.min, 1, get)
        self.assertEqual(next(records)[0]["doi"], "10.1234/load-pull.1")
        self.assertEqual(next(records)[0]["doi"], "10.1234/load-pull.2")
        with self.assertRaisesRegex(ValueError, "Crossref"):
            next(records)

    def test_existing_search_still_fetches_one_batch(self):
        calls, get = self.getter({"items": [item(1)], "next-cursor": "unused", "total-results": 12})
        self.assertEqual([p["doi"] for p in search({}, "load-pull", date.min, 1, get)], ["10.1234/load-pull.1"])
        self.assertNotIn("cursor", calls[0])

    def test_pads_partial_dates_and_prefers_a_full_one(self):
        _, get = self.getter({"items": [
            {"DOI": "10.1234/a", "title": ["A"], "published": {"date-parts": [[2026, 6]]}},
            {"DOI": "10.1234/b", "title": ["B"], "published": {"date-parts": [[2026]]}},
            {"DOI": "10.1234/c", "title": ["C"], "published": {"date-parts": [[2026, 6]]},
             "published-online": {"date-parts": [[2026, 6, 15]]}},
            {"DOI": "10.1234/d", "title": ["D"], "issued": {"date-parts": [[None]]}},
        ]})
        self.assertEqual([p["published"] for p in search({}, "load-pull", date.min, 4, get)],
                         ["2026-06-01", "2026-01-01", "2026-06-15", ""])

    def test_invalid_message_marks_query_as_failed(self):
        _, get = self.getter({"items": "malformed"})
        with self.assertRaises(ValueError):
            next(pages({}, "load-pull", date.min, 1, get))
