import json
import os
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from sources.openalex import cached_pdf


WORK_ID = "W3038351021"
WORK_URL = f"https://openalex.org/{WORK_ID}"
PDF_URL = f"https://content.openalex.org/works/{WORK_ID}.pdf"
KEY = "test+key&value"


def license_work():
    return {
        "id": WORK_URL,
        "has_content": {"pdf": True},
        "content_urls": {"pdf": PDF_URL},
        "best_oa_location": {"is_oa": True, "license": "cc-by"},
    }


class OpenAlexCachedPdfTest(unittest.TestCase):
    def test_skips_blank_key_and_unsafe_work_ids_without_network(self):
        get = Mock()
        with patch.dict(os.environ, OPENALEX_API_KEY=""):
            self.assertIsNone(cached_pdf(WORK_URL, 1234, get))
        with patch.dict(os.environ, OPENALEX_API_KEY=KEY):
            for invalid in ("", "W0", "W3038351021?x=1", "https://other.test/" + WORK_ID):
                with self.subTest(invalid=invalid):
                    self.assertIsNone(cached_pdf(invalid, 1234, get))
        get.assert_not_called()

    def test_looks_up_and_downloads_matching_licensed_pdf(self):
        get = Mock(side_effect=[json.dumps(license_work()).encode(), b"%PDF-example"])
        with patch.dict(os.environ, OPENALEX_API_KEY=KEY):
            self.assertEqual(cached_pdf(WORK_URL, 1234, get), b"%PDF-example")
        metadata, pdf = get.call_args_list
        self.assertEqual(
            urlsplit(metadata.args[0])._replace(query="").geturl(),
            f"https://api.openalex.org/works/{WORK_ID}",
        )
        self.assertEqual(urlsplit(pdf.args[0])._replace(query="").geturl(), PDF_URL)
        for call in (metadata, pdf):
            self.assertEqual(parse_qs(urlsplit(call.args[0]).query)["api_key"], [KEY])
        self.assertEqual(pdf.args[1], 1234)
        self.assertEqual(pdf.kwargs, {"accept": "application/pdf"})

    def test_rejects_unlicensed_missing_or_mismatched_cache(self):
        checks = [
            {"id": "https://openalex.org/W100"},
            {"best_oa_location": {"is_oa": False, "license": "cc-by"}},
            {"best_oa_location": {"is_oa": True, "license": "cc-by-nc"}},
            {"has_content": {"pdf": False}},
            {"content_urls": {"pdf": "https://content.openalex.org/works/W100.pdf"}},
            {"content_urls": {"pdf": PDF_URL.replace("content.openalex.org", "other.test")}},
        ]
        for changed in checks:
            with self.subTest(changed=changed), patch.dict(os.environ, OPENALEX_API_KEY=KEY):
                get = Mock(return_value=json.dumps(license_work() | changed).encode())
                self.assertIsNone(cached_pdf(WORK_ID, 1234, get))
                self.assertEqual(get.call_count, 1)

    def test_hides_secret_in_network_failures(self):
        get = Mock(side_effect=[json.dumps(license_work()).encode(), ValueError(f"Fetched {PDF_URL}?api_key={KEY}")])
        with patch.dict(os.environ, OPENALEX_API_KEY=KEY), self.assertRaises(ValueError) as error:
            cached_pdf(WORK_URL, 1234, get)
        self.assertNotIn(KEY, str(error.exception))
        self.assertTrue(error.exception.__suppress_context__)

    def test_distinguishes_missing_content_from_provider_denial(self):
        with patch.dict(os.environ, OPENALEX_API_KEY=KEY):
            get = Mock(side_effect=[json.dumps(license_work()).encode(), ValueError("content.openalex.org: HTTP 404")])
            self.assertIsNone(cached_pdf(WORK_ID, 1234, get))
            get = Mock(side_effect=ValueError("api.openalex.org: HTTP 401"))
            with self.assertRaisesRegex(ValueError, "HTTP 401"):
                cached_pdf(WORK_ID, 1234, get)
