"""Public publisher RSS and Atom feeds."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date

from .common import METADATA_LIMIT, clean_text, is_reusable_license, normalize_doi, publication_date, record, web_url
from .types import Candidate, Getter, SourceSpec


SPEC: SourceSpec = {
    "kind": "rss",
    "label": "RSS / Atom",
    "description": "Статьи из публичной ленты журнала, издателя или репозитория.",
    "mode": "feed",
    "fields": [{
        "key": "url", "label": "Адрес ленты", "type": "url", "required": True,
        "max_length": 2000, "placeholder": "https://journal.example/rss",
    }],
}


def _tag(node):
    return node.tag.rsplit("}", 1)[-1].lower()


def _children(element, name):
    return [child for child in element if _tag(child) == name]


def _field(element, *names):
    for name in names:
        for child in _children(element, name):
            if value := clean_text(" ".join(child.itertext())):
                return value
    return ""


def _feed_links(entry, base):
    article_url = ""
    pdf = ""
    license_url = ""
    for link in _children(entry, "link"):
        href = web_url(link.get("href") or link.text, base)
        relation = link.get("rel", "alternate")
        if relation == "alternate" and not article_url:
            article_url = href
        elif relation == "license":
            license_url = href
        elif relation == "enclosure" and link.get("type") == "application/pdf":
            pdf = href
    for enclosure in _children(entry, "enclosure"):
        if enclosure.get("type") == "application/pdf":
            pdf = pdf or web_url(enclosure.get("url"), base)
    for element in _children(entry, "license"):
        license_url = license_url or web_url(element.get("{http://www.w3.org/1999/02/22-rdf-syntax-ns#}resource"), base)
    license_url = license_url or _field(entry, "license", "rights")
    return article_url, pdf if is_reusable_license(license_url) else "", license_url


def search(source: dict, _keyword: str, since: date, limit: int, get: Getter) -> list[Candidate]:
    url = web_url(source.get("url"))
    if not url:
        raise ValueError("Для RSS/Atom нужен URL ленты")
    payload = get(url, METADATA_LIMIT, accept="application/atom+xml, application/rss+xml, application/xml, text/xml, */*")
    if b"<!DOCTYPE" in payload.replace(b"\0", b"").upper():
        raise ValueError("Объявления DOCTYPE в лентах запрещены")
    feed = ET.fromstring(payload)
    if _tag(feed) not in {"rss", "rdf", "feed"}:
        raise ValueError("URL не вернул ленту RSS или Atom")
    base = web_url(feed.get("{http://www.w3.org/XML/1998/namespace}base"), url) or url
    channel = next(iter(_children(feed, "channel")), feed)
    venue = _field(channel, "title")
    rows = []
    cutoff = since.isoformat()
    latest = date.today().isoformat()
    for entry in feed.iter():
        if _tag(entry) not in {"item", "entry"}:
            continue
        entry_base = web_url(entry.get("{http://www.w3.org/XML/1998/namespace}base"), base) or base
        href, pdf, license_url = _feed_links(entry, entry_base)
        published = publication_date(_field(entry, "pubdate", "published", "date", "updated"))
        if published and not cutoff <= published <= latest:
            continue
        guid = _field(entry, "guid", "id")
        title = _field(entry, "title")
        authors = [
            _field(author, "name") or clean_text(" ".join(author.itertext()))
            for tag in ("author", "creator")
            for author in _children(entry, tag)
        ]
        keywords = [
            element.get("term") or " ".join(element.itertext())
            for tag in ("category", "subject")
            for element in _children(entry, tag)
        ]
        rows.append(record(
            "rss", title=title, doi=next(filter(None, map(normalize_doi, (
                _field(entry, "doi", "identifier"), guid, href,
            ))), ""),
            abstract=_field(entry, "summary", "description", "encoded", "content"),
            url=href or (web_url(guid) if guid else ""), pdf_url=pdf,
            published=published, authors=authors, venue=venue, keywords=keywords,
            external_id=f"{url}#{guid or href or title}", license=license_url,
        ))
    rows.sort(key=lambda row: row["published"], reverse=True)
    return rows[:limit]
