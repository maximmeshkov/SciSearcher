"""OpenAlex Works API."""

from __future__ import annotations

import os
from datetime import date

from .common import ISSN_FIELD, fetch_json, is_reusable_license, issn_filter, record, web_url
from .types import Candidate, Getter, SourceSpec


SPEC: SourceSpec = {
    "kind": "openalex",
    "label": "OpenAlex",
    "description": "Поиск по научным публикациям и ссылки на открытые копии.",
    "mode": "search",
    "fields": [ISSN_FIELD],
}


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


def search(source: dict, keyword: str, since: date, limit: int, get: Getter) -> list[Candidate]:
    filters = [
        "from_publication_date:" + since.isoformat(),
        "to_publication_date:" + date.today().isoformat(),
        "type:article",
        "primary_location.source.type:journal",
    ]
    if issn := issn_filter(source):
        filters.append("primary_location.source.issn:" + issn)
    phrase = keyword.replace('"', " ").strip()
    params = {
        "search.title_abstract_keywords": f'"{phrase}"',
        "filter": ",".join(filters), "per_page": limit,
        "sort": "relevance_score:desc,publication_date:desc", "select": (
            "id,doi,display_name,publication_date,abstract_inverted_index,authorships,"
            "primary_location,best_oa_location,locations,keywords"
        ),
    }
    if key := os.getenv("OPENALEX_API_KEY"):
        params["api_key"] = key
    response = fetch_json(get, "https://api.openalex.org/works", params)
    rows = []
    for item in response.get("results") or []:
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
