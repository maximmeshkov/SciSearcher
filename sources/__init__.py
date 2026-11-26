"""Registry and unified interface for independent literature source modules."""

from __future__ import annotations

from collections.abc import Iterator
from copy import deepcopy
from datetime import date
from importlib import import_module
from json import JSONDecodeError
from xml.etree.ElementTree import ParseError

from .net import get_bytes
from .types import Candidate, Getter, SourceSpec

__all__ = ["fetch", "fetch_pages", "get_bytes", "source_specs"]

# Trusted module names are selected here in code, never from library configuration.
_BUILTIN_MODULES = (
    "openalex",
    "crossref",
    "rss",
    "ieee",
)


def _load_registry():
    registry = {}
    for name in _BUILTIN_MODULES:
        module = import_module("." + name, __package__)
        kind = module.SPEC["kind"]
        if kind in registry:
            raise RuntimeError(f"Duplicate source kind: {kind}")
        registry[kind] = module
    return registry


_REGISTRY = _load_registry()


def source_specs() -> list[SourceSpec]:
    """Return the descriptions and configuration fields for all registered adapters."""
    return deepcopy([module.SPEC for module in _REGISTRY.values()])


def fetch(source: dict, keyword: str, since: date, limit: int, *, get: Getter | None = None) -> list[Candidate]:
    """Fetch up to 100 bibliographic candidates through one registered adapter."""
    kind = source.get("kind")
    if not isinstance(kind, str) or kind not in _REGISTRY:
        raise ValueError("Неизвестный источник")
    limit = max(1, min(int(limit), 100))
    getter = get_bytes if get is None else get
    try:
        rows = _REGISTRY[kind].search(source, keyword, since, limit, getter)
    except (ParseError, JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError(f"{kind}: источник вернул данные в неожиданном формате") from error
    if not isinstance(rows, list):
        raise ValueError("Адаптер должен вернуть список публикаций.")
    return rows[:limit]


def fetch_pages(source: dict, keyword: str, since: date, limit: int,
                *, get: Getter | None = None) -> Iterator[list[Candidate]]:
    """Read one source in pages. Adapters with only search still work."""
    kind = source.get("kind")
    if not isinstance(kind, str) or kind not in _REGISTRY:
        raise ValueError("Неизвестный источник")
    limit = max(1, min(int(limit), 1000))
    module = _REGISTRY[kind]
    if not hasattr(module, "pages"):
        limit = min(limit, 100)
        rows = fetch(source, keyword, since, limit, get=get)
        yield rows
        if len(rows) == limit and module.SPEC["mode"] == "search":
            raise ValueError(f"Адаптер выдал первые {limit} результатов и не поддерживает следующие страницы.")
        return
    getter = get_bytes if get is None else get
    try:
        for rows in module.pages(source, keyword, since, limit, getter):
            if not isinstance(rows, list) or len(rows) > limit:
                raise ValueError(f"Адаптер должен вернуть список до {limit} публикаций на странице.")
            yield rows
    except (ParseError, JSONDecodeError, UnicodeDecodeError) as error:
        raise ValueError(f"{kind}: источник вернул данные в неожиданном формате") from error
