"""Types shared by the core and source adapters."""

from typing import Literal, NotRequired, Protocol, Required, TypedDict


class Field(TypedDict):
    key: str
    label: str
    type: Literal["text", "url"]
    required: bool
    max_length: int
    placeholder: NotRequired[str]
    pattern: NotRequired[str]


class SourceSpec(TypedDict):
    kind: str
    label: str
    description: str
    mode: Literal["search", "feed"]
    fields: list[Field]


class Candidate(TypedDict, total=False):
    source: str
    title: Required[str]
    abstract: str
    doi: str
    url: str
    pdf_url: str
    published: str
    authors: list[str]
    venue: str
    external_id: str
    license: str
    keywords: list[str]


class Getter(Protocol):
    def __call__(self, url: str, limit: int, *, accept: str = "*/*") -> bytes: ...
