"""OpenAlex Works API."""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from datetime import date
from urllib.parse import quote, urlencode

from .common import ISSN_FIELD, fetch_json, is_reusable_license, issn_filter, normalize_doi, record, web_url
from .types import Candidate, Getter, SourceSpec


SPEC: SourceSpec = {
    "kind": "openalex",
    "label": "OpenAlex",
    "description": "Поиск по научным публикациям и ссылки на открытые копии.",
    "mode": "search",
    "fields": [ISSN_FIELD],
}

_ENDPOINT = "https://api.openalex.org/works"
_CONTENT_ENDPOINT = "https://content.openalex.org/works"
_WORK_ID = re.compile(r"(?:https://openalex\.org/)?(W[1-9][0-9]*)")
_BASIC_PAGING_LIMIT = 10_000


def _licensed(location):
    if not isinstance(location, dict) or location.get("is_oa") is not True:
        return False
    value = location.get("license")
    return value in ("cc-by", "cc0") or is_reusable_license(value)


def _cache_location(work, key):
    work_id = _WORK_ID.fullmatch(work.get("id") or "")
    best = work.get("best_oa_location")
    available, urls = work.get("has_content"), work.get("content_urls")
    if not (key and work_id and _licensed(best) and isinstance(available, dict) and isinstance(urls, dict)):
        return None
    endpoint = f"{_CONTENT_ENDPOINT}/{work_id[1]}.pdf"
    if available.get("pdf") is True and urls.get("pdf") == endpoint:
        return endpoint + "?" + urlencode({"api_key": key}), best["license"], endpoint
    return None


def _abstract(index) -> str:
    if not isinstance(index, dict):
        return ""
    words = [
        (position, word)
        for word, positions in index.items()
        if isinstance(positions, list)
        for position in positions
        if isinstance(position, int)
    ]
    return " ".join(word for _, word in sorted(words))


def _query_params(source: dict, keyword: str, since: date, limit: int) -> dict:
    filters = [
        "to_publication_date:" + date.today().isoformat(),
        "type:article",
        "primary_location.source.type:journal",
    ]
    if since != date.min:
        filters.append("from_publication_date:" + since.isoformat())
    if issn := issn_filter(source):
        filters.append("primary_location.source.issn:" + issn)
    phrase = keyword.replace('"', " ").strip()
    params = {
        "search.title_abstract_keywords": f'"{phrase}"',
        "filter": ",".join(filters), "per_page": max(1, min(limit, 100)),
        "sort": "relevance_score:desc,publication_date:desc", "select": (
            "id,doi,display_name,publication_date,abstract_inverted_index,authorships,"
            "primary_location,best_oa_location,locations,keywords"
        ),
    }
    if key := os.getenv("OPENALEX_API_KEY"):
        params["api_key"] = key
    return params


def _rows(items: list) -> list[Candidate]:
    rows = []
    for item in items:
        primary = item.get("primary_location") or {}
        locations = [item.get("best_oa_location")] + (item.get("locations") or [])
        open_pdf = next((
            entry for entry in locations
            if _licensed(entry) and web_url(entry.get("pdf_url"))
        ), {})
        rows.append(record(
            "openalex", title=item.get("display_name"), doi=item.get("doi"),
            abstract=_abstract(item.get("abstract_inverted_index")), published=item.get("publication_date"),
            url=primary.get("landing_page_url") or item.get("doi") or item.get("id"),
            pdf_url=open_pdf.get("pdf_url"), license=open_pdf.get("license"),
            venue=(primary.get("source") or {}).get("display_name"),
            authors=[(row.get("author") or {}).get("display_name") for row in item.get("authorships") or []],
            keywords=[row.get("display_name") for row in item.get("keywords") or []],
            external_id=item.get("id"),
        ))
    return rows


def search(source: dict, keyword: str, since: date, limit: int, get: Getter) -> list[Candidate]:
    response = fetch_json(get, _ENDPOINT, _query_params(source, keyword, since, limit))
    return _rows(response.get("results") or [])


def resolve(doi, external_id, get):
    """Return every eligible location for this exact work, including the cache."""
    match = _WORK_ID.fullmatch(external_id or "")
    if not match and not doi:
        return
    lookup = match[1] if match else "https://doi.org/" + quote(doi, safe="/")
    params = {"select": "id,doi,best_oa_location,locations,content_urls,has_content"}
    if key := os.getenv("OPENALEX_API_KEY", "").strip():
        params["api_key"] = key
    work = fetch_json(get, f"{_ENDPOINT}/{lookup}", params)
    if (match and work.get("id") != f"https://openalex.org/{match[1]}") or (
        doi and normalize_doi(work.get("doi")) != doi
    ):
        return
    best = work.get("best_oa_location") or {}
    for loc in [best, *(work.get("locations") or [])]:
        if _licensed(loc) and (url := web_url(loc.get("pdf_url"))):
            yield url, loc["license"], url
    if location := _cache_location(work, key):
        yield location


def cached_pdf(external_id: str, limit: int, get: Getter) -> bytes | None:
    """Fetch the licensed PDF cached by OpenAlex for one known work."""
    key = os.getenv("OPENALEX_API_KEY", "").strip()
    match = _WORK_ID.fullmatch(external_id) if isinstance(external_id, str) else None
    if not key or not match:
        return None
    work_id = match[1]
    try:
        work = fetch_json(get, f"{_ENDPOINT}/{work_id}", {
            "select": "id,has_content,content_urls,best_oa_location",
            "api_key": key,
        })
        if work.get("id") != f"https://openalex.org/{work_id}":
            return None
        location = _cache_location(work, key)
        return get(location[0], limit, accept="application/pdf") if location else None
    except (OSError, ValueError) as error:
        status = re.fullmatch(r"(?:api|content)\.openalex\.org: HTTP ([1-5][0-9]{2})", str(error))
        if status:
            if status[1] in ("404", "410"):
                return None
            raise ValueError(f"Запрос к кэшу OpenAlex вернул HTTP {status[1]}.") from None
        # The request URL contains the key. Keep it out of the job journal.
        raise ValueError("Не удалось получить PDF из кэша OpenAlex.") from None


def pages(source: dict, keyword: str, since: date, limit: int, get: Getter) -> Iterator[list[Candidate]]:
    params = _query_params(source, keyword, since, limit)
    if "api_key" in params:
        params["cursor"] = "*"
    else:
        # OpenAlex requires a key for cursor requests beyond the first page.
        params["page"] = 1
    seen_cursors = {"*"}
    page_size = params["per_page"]
    while True:
        response = fetch_json(get, _ENDPOINT, params)
        items = response.get("results") or []
        if not items:
            return
        yield _rows(items)
        meta = response.get("meta") or {}
        if "cursor" in params:
            cursor = meta.get("next_cursor")
            if cursor is None:
                return
            if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
                raise ValueError("OpenAlex вернул некорректный курсор следующей страницы.")
            seen_cursors.add(cursor)
            params["cursor"] = cursor
        else:
            page = params["page"]
            count = meta.get("count")
            if len(items) < page_size or (isinstance(count, int) and page * page_size >= count):
                return
            if (page + 1) * page_size > _BASIC_PAGING_LIMIT:
                raise ValueError("Достигнут предел анонимной выдачи OpenAlex. Для продолжения нужен OPENALEX_API_KEY.")
            params["page"] = page + 1
