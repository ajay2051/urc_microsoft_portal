"""Run blocking, transactional ORM work from async handlers.

Single-statement reads use Django's native async ORM (``afirst``, ``acount``,
``async for``). Multi-statement units of work that must be atomic are written as
ordinary sync functions wrapped in ``transaction.atomic`` and executed on
django-bolt's ORM executor, so the event loop is never blocked and a request's
writes commit or roll back together with their audit record.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import ParamSpec, TypeVar

from django_bolt.concurrency import run_in_orm_executor

P = ParamSpec("P")
T = TypeVar("T")


async def run_atomic_unit(fn: Callable[[], T]) -> T:
    """Execute a zero-argument sync callable (which opens its own atomic block) off the event loop."""
    return await run_in_orm_executor(fn)
