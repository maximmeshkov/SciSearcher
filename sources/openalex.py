"""OpenAlex Works API."""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from datetime import date
from urllib.parse import urlencode

from .common import ISSN_FIELD, fetch_json, is_reusable_license, issn_filter, record, web_url
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
            if entry and entry.get("is_oa") and web_url(entry.get("pdf_url"))
            and (entry.get("license") in {"cc-by", "cc0"} or is_reusable_license(entry.get("license")))
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
        location = work.get("best_oa_location")
        available = work.get("has_content")
        urls = work.get("content_urls")
        if not all(isinstance(field, dict) for field in (location, available, urls)):
            return None
        license_value = location.get("license")
        if location.get("is_oa") is not True or not (
            license_value in ("cc-by", "cc0") or is_reusable_license(license_value)
        ):
            return None
        endpoint = f"{_CONTENT_ENDPOINT}/{work_id}.pdf"
        if available.get("pdf") is not True or urls.get("pdf") != endpoint:
            return None
        return get(f"{endpoint}?{urlencode({'api_key': key})}", limit, accept="application/pdf")
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
