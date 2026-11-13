"""Offset pagination primitives shared by list endpoints."""

from __future__ import annotations

from typing import Annotated, Generic, TypeVar

import msgspec
from msgspec import Meta

T = TypeVar("T")

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 25

PageNumber = Annotated[int, Meta(ge=1, le=1_000_000, description="1-based page number")]
PageSize = Annotated[int, Meta(ge=1, le=MAX_PAGE_SIZE, description="Items per page (max 100)")]


class Page(msgspec.Struct, Generic[T]):
    items: list[T]
    page: int
    page_size: int
    total: int
    total_pages: int


def paginate_bounds(page: int, page_size: int) -> tuple[int, int]:
    """Return ``(offset, limit)`` for a validated page request."""
    return (page - 1) * page_size, page_size


def build_page(items: list[T], *, page: int, page_size: int, total: int) -> Page[T]:
    total_pages = max(1, -(-total // page_size))
    return Page(items=items, page=page, page_size=page_size, total=total, total_pages=total_pages)


def choice(*values: str) -> object:
    """Annotated ``str`` constrained to ``values``.

    django-bolt 0.13 does not enforce ``Literal``/``Enum`` annotations on *query
    parameters* (an unknown value would be passed through silently), but it does
    enforce msgspec ``Meta(pattern=...)``. Use this for every enumerated query param.
    """
    import re

    return Annotated[str, Meta(pattern="^(" + "|".join(re.escape(v) for v in values) + ")$", max_length=32)]
