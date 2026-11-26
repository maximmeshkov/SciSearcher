"""IEEE Xplore Metadata API."""

from __future__ import annotations

import os
from datetime import date

from .common import ISSN_FIELD, fetch_json, issn_filter, record
from .types import Candidate, Getter, SourceSpec


SPEC: SourceSpec = {
    "kind": "ieee",
    "label": "IEEE Xplore",
    "description": "Поиск по метаданным IEEE Xplore. Требуется ключ IEEE_API_KEY.",
    "mode": "search",
    "fields": [ISSN_FIELD],
}


def _index_terms(item: dict) -> list[str]:
    index_terms = item.get("index_terms") or {}
    if not isinstance(index_terms, dict):
        return []
    keywords = []
    for name in ("author_terms", "ieee_terms"):
        group = index_terms.get(name)
        if isinstance(group, dict) and isinstance(group.get("terms"), list):
            keywords.extend(term for term in group["terms"] if isinstance(term, str))
    return keywords


def search(source: dict, keyword: str, since: date, limit: int, get: Getter) -> list[Candidate]:
    key = os.getenv("IEEE_API_KEY")
    if not key:
        raise ValueError("IEEE Xplore Metadata API требует IEEE_API_KEY")
    params = {
        "apikey": key, "format": "json", "querytext": keyword, "start_year": since.year, "end_year": date.today().year,
        "max_records": limit, "sort_field": "article_number", "sort_order": "desc",
    }
    if issn := issn_filter(source):
        params["issn"] = issn.replace("-", "")
    response = fetch_json(get, "https://ieeexploreapi.ieee.org/api/v1/search/articles", params)
    return [
        record(
            "ieee", title=item.get("title"), doi=item.get("doi"), abstract=item.get("abstract"),
            published=item.get("publication_date"), url=item.get("abstract_url"),
            venue=item.get("publication_title"), external_id=item.get("article_number"),
            authors=[a.get("full_name") for a in (item.get("authors") or {}).get("authors") or []],
            keywords=_index_terms(item),
        )
        for item in response.get("articles") or []
    ]
