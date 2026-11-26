"""Shared metadata normalization for source adapters."""

from __future__ import annotations

import html
import json
import re
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import quote, unquote, urlencode, urljoin, urlsplit

from .types import Candidate, Field, Getter


METADATA_LIMIT = 8 * 1024 * 1024
_DOI = re.compile(r"10\.\d{4,9}/[^\s<>\"']+", re.IGNORECASE)

ISSN_FIELD: Field = {
    "key": "issn", "label": "ISSN журнала", "type": "text",
    "required": False, "max_length": 9, "placeholder": "1234-567X",
}


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks = []

    def handle_data(self, data):
        self.blocks.append(data)


def clean_text(value) -> str:
    if not isinstance(value, str):
        return ""
    if "<" in value and ">" in value:
        # DOI spans, JATS paragraphs, and HTML summaries are provider metadata.
        parser = _Text()
        parser.feed(value)
        parser.close()
        value = " ".join(parser.blocks)
    return " ".join(html.unescape(value).split())


def web_url(value, base: str = "") -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        url = urljoin(base, value.strip())
        parts = urlsplit(url)
        if (
            parts.scheme in {"http", "https"} and parts.hostname
            and parts.username is None and parts.password is None
            and all(ord(char) > 32 and ord(char) != 127 for char in url)
        ):
            return url
    except ValueError:
        pass
    return ""


def normalize_doi(value) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if value.lower().startswith("doi:"):
        value = value[4:].strip()
    url = web_url(value)
    if url:
        parts = urlsplit(url)
        if parts.hostname in {"doi.org", "dx.doi.org", "www.doi.org"}:
            value = parts.path.lstrip("/")
        else:
            return ""
    value = unquote(value)
    return value.lower() if _DOI.fullmatch(value) else ""


def _doi_url(doi: str) -> str:
    return "https://doi.org/" + quote(doi, safe="/()") if doi else ""


def publication_date(value) -> str:
    if not isinstance(value, str):
        return ""
    value = value.strip()
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(value).date().isoformat()
    except (TypeError, ValueError, OverflowError):
        pass
    for pattern in ("%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(value, pattern).date().isoformat()
        except ValueError:
            continue
    return ""


def record(kind: str, *, title, abstract="", doi="", url="", pdf_url="", published="",
           authors=(), venue="", external_id="", license="", keywords=()) -> Candidate:
    doi = normalize_doi(doi)
    result: Candidate = {
        "source": kind, "title": clean_text(title), "abstract": clean_text(abstract), "doi": doi,
        "url": web_url(url) or _doi_url(doi), "pdf_url": web_url(pdf_url),
        "published": publication_date(published),
        "authors": [name for author in authors if (name := clean_text(author))],
        "venue": clean_text(venue), "external_id": str(external_id or ""),
        "license": clean_text(license),
    }
    reported_keywords = [word for item in keywords if (word := clean_text(item))]
    if reported_keywords:
        result["keywords"] = reported_keywords
    return result


def fetch_json(get: Getter, endpoint: str, params: dict) -> dict:
    payload = json.loads(get(endpoint + "?" + urlencode(params), METADATA_LIMIT, accept="application/json"))
    if not isinstance(payload, dict):
        raise ValueError("Источник вернул неожиданный JSON")
    return payload


def issn_filter(source: dict) -> str:
    issn = str(source.get("issn") or "").strip().upper()
    if issn and not re.fullmatch(r"\d{4}-\d{3}[\dX]", issn):
        raise ValueError("ISSN должен иметь вид 1234-567X")
    return issn


def is_reusable_license(value: str) -> bool:
    url = web_url(value)
    if not url:
        return False
    parts = urlsplit(url)
    return parts.hostname == "creativecommons.org" and (
        parts.path.startswith("/licenses/by/") or parts.path.startswith("/publicdomain/zero/")
    )
