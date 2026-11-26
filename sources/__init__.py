"""Registry and unified interface for independent literature source modules."""

from __future__ import annotations

from copy import deepcopy
from datetime import date
from importlib import import_module
from json import JSONDecodeError
from xml.etree.ElementTree import ParseError

from .net import get_bytes
from .types import Candidate, Getter, SourceSpec

__all__ = ["fetch", "get_bytes", "source_specs"]

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
    return [row for row in rows if row["title"]][:limit]
