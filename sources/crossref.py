"""Crossref REST API for journal articles."""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Iterator
from datetime import date

from .common import ISSN_FIELD, fetch_json, is_reusable_license, issn_filter, publication_date, record, web_url
from .net import get_bytes
from .types import Candidate, Getter, SourceSpec


SPEC: SourceSpec = {
    "kind": "crossref",
    "label": "Crossref",
    "description": "Поиск журнальных статей по DOI-метаданным издателей.",
    "mode": "search",
    "fields": [ISSN_FIELD],
}

_ENDPOINT = "https://api.crossref.org/works"
_REQUEST_LOCK = threading.Lock()
_last_query_started = 0.0


def _fetch_works(get: Getter, params: dict) -> dict:
    # Crossref's public list API supports one request per second.
    if get is not get_bytes:
        return fetch_json(get, _ENDPOINT, params)
    global _last_query_started
    with _REQUEST_LOCK:
        remaining = _last_query_started + 1.05 - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        _last_query_started = time.monotonic()
        return fetch_json(get, _ENDPOINT, params)


def _crossref_date(item):
    for name in ("published", "published-online", "published-print", "issued"):
        parts = ((item.get(name) or {}).get("date-parts") or [[]])[0]
        if len(parts) == 3:
            try:
                return date(*parts).isoformat()
            except (TypeError, ValueError):
                pass
    return ""


def _crossref_pdf(item) -> tuple[str, str]:
    licenses = {}
    for entry in item.get("license") or []:
        version = entry.get("content-version")
        license_url = entry.get("URL", "")
        start = publication_date((entry.get("start") or {}).get("date-time"))
        if version in {"vor", "am"} and is_reusable_license(license_url):
            if start and start <= date.today().isoformat():
                licenses[version] = license_url
    for link in item.get("link") or []:
        license_url = licenses.get(link.get("content-version"))
        if license_url and link.get("content-type") == "application/pdf" and web_url(link.get("URL")):
            return link["URL"], license_url
    return "", ""


def _query_params(source: dict, keyword: str, since: date, limit: int) -> dict:
    filters = ["until-pub-date:" + date.today().isoformat(), "type:journal-article"]
    if since != date.min:
        filters.append("from-pub-date:" + since.isoformat())
    if issn := issn_filter(source):
        filters.append("issn:" + issn)
    params = {
        "query.bibliographic": keyword, "filter": ",".join(filters),
        "rows": max(1, min(limit, 1000)), "sort": "relevance",
        "select": ("DOI,title,abstract,published,published-online,published-print,issued,"
                   "URL,license,link,container-title,author"),
    }
    if email := os.getenv("CROSSREF_MAILTO"):
        params["mailto"] = email
    return params


def _records(items: list) -> list[Candidate]:
    rows = []
    for item in items:
        pdf, license_url = _crossref_pdf(item)
        authors = [
            " ".join(filter(None, (a.get("given"), a.get("family")))) or a.get("name")
            for a in item.get("author") or []
        ]
        rows.append(record(
            "crossref", title=(item.get("title") or [""])[0], doi=item.get("DOI"),
            abstract=item.get("abstract"), published=_crossref_date(item),
            url=item.get("URL"), pdf_url=pdf, license=license_url,
            venue=(item.get("container-title") or [""])[0], authors=authors, external_id=item.get("DOI"),
        ))
    return rows


def _message(get: Getter, params: dict) -> dict:
    result = _fetch_works(get, params)
    message = result.get("message") if isinstance(result, dict) else None
    if not isinstance(message, dict) or not isinstance(message.get("items"), list):
        raise ValueError("Crossref вернул ответ без списка публикаций.")
    return message


def search(source: dict, keyword: str, since: date, limit: int, get: Getter) -> list[Candidate]:
    return _records(_message(get, _query_params(source, keyword, since, limit))["items"])


def pages(source: dict, keyword: str, since: date, limit: int, get: Getter) -> Iterator[list[Candidate]]:
    params = _query_params(source, keyword, since, limit)
    params["cursor"] = "*"
    seen_cursors = {"*"}
    read = 0
    while True:
        message = _message(get, params)
        items = message["items"]
        read += len(items)
        yield _records(items)
        if len(items) < params["rows"]:
            return
        cursor = message.get("next-cursor")
        total = message.get("total-results")
        if cursor is None and isinstance(total, int) and total <= read:
            return
        if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
            raise ValueError("Crossref не вернул новый курсор для следующей страницы.")
        seen_cursors.add(cursor)
        params["cursor"] = cursor
