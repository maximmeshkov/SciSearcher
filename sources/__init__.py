"""Registry and unified interface for independent literature source modules."""

from __future__ import annotations

from collections.abc import Iterator
from copy import deepcopy
from datetime import date
from importlib import import_module
from json import JSONDecodeError, dumps
import logging
import re
from xml.etree.ElementTree import ParseError

from .net import get_bytes
from .common import web_url
from .types import Candidate, Getter, SourceSpec

__all__ = ["allows_abstract_export", "fetch", "fetch_pages", "get_bytes", "pdf_error", "resolved_pdf", "source_specs"]

# Trusted module names are selected here in code, never from library configuration.
_BUILTIN_MODULES = (
    "openalex",
    "crossref",
    "rss",
    "ieee",
)


def _validate(module):
    spec = module.SPEC
    dumps(spec, allow_nan=False)  # The same schema is returned by GET /api/sources.
    if not (isinstance(spec, dict) and re.fullmatch(r"[a-z][a-z0-9_\-]*", spec.get("kind", ""))
            and isinstance(spec.get("label"), str) and spec["label"].strip()
            and isinstance(spec.get("description"), str) and spec.get("mode") in ("feed", "search")
            and isinstance(spec.get("fields"), list) and type(spec.get("export_abstract", True)) is bool
            and callable(getattr(module, "search", None))):
        raise ValueError("SPEC/search")
    keys = {"kind"}
    for field in spec["fields"]:
        key = field["key"]
        if (not re.fullmatch(r"[a-z][a-z0-9_]*", key) or key in keys
                or not isinstance(field["label"], str) or not field["label"].strip()
                or field["type"] not in ("text", "url") or type(field["required"]) is not bool
                or type(field["max_length"]) is not int or field["max_length"] <= 0):
            raise ValueError("SPEC.fields")
        if "pattern" in field:
            if not isinstance(field["pattern"], str):
                raise ValueError("SPEC.fields.pattern")
            re.compile(field["pattern"])
        keys.add(key)
    for name in ("pages", "resolve"):
        if hasattr(module, name) and not callable(getattr(module, name)):
            raise ValueError(name)
    return spec["kind"]


def _load_registry():
    registry = {}
    for name in _BUILTIN_MODULES:
        try:
            module = import_module("." + name, __package__)
            kind = _validate(module)
            if kind in registry:
                raise ValueError("duplicate kind")
            registry[kind] = module
        except Exception as error:
            message = f"Модуль {name} пропущен: {type(error).__name__} при загрузке или проверке SPEC."
            logging.warning("%s", message)
    return registry


_REGISTRY = _load_registry()


def source_specs() -> list[SourceSpec]:
    """Return the descriptions and configuration fields for all registered adapters."""
    return deepcopy([module.SPEC for module in _REGISTRY.values()])


def allows_abstract_export(kind):
    module = _REGISTRY.get(kind)
    return module is not None and module.SPEC.get("export_abstract", True)


def pdf_error(source, error):
    """Report a status without exposing adapter-supplied URLs, tokens or response bodies."""
    status = re.search(r"\bHTTP\s+([1-5]\d\d)\b", str(error), re.I)
    if status:
        return "" if status[1] in ("404", "410") else f"{source}: HTTP {status[1]}"
    return f"{source}: ошибка получения PDF"


def resolved_pdf(doi, external_ids, get, read_pdf, errors, *, skip=(), fallback=None):
    """Try independent resolver modules, returning (bytes, (public URL, license)) or None."""
    seen = set(skip)

    def read(location, kind):
        url, license_value, public_url = location
        if not web_url(url) or not web_url(public_url) or not isinstance(license_value, str):
            raise ValueError("Некорректное описание источника файла.")
        if url in seen:
            return None
        seen.add(url)
        try:
            return read_pdf(url), (public_url, license_value)
        except Exception as error:
            if message := pdf_error(kind, error):
                errors.append(message)

    for kind, module in _REGISTRY.items():
        if not hasattr(module, "resolve"):
            continue
        ids = external_ids(kind) or [""]
        if not doi and ids == [""]:
            continue
        for external_id in ids:
            try:
                for location in module.resolve(doi, external_id, get):
                    if found := read(location, kind):
                        return found
            except Exception as error:
                if message := pdf_error(kind, error):
                    errors.append(message)
    if fallback:
        return read(fallback, "PDF")
    return None


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
