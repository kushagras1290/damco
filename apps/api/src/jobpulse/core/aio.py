"""Event-loop helpers. psycopg's async mode cannot use Windows' default Proactor loop."""

from __future__ import annotations

import asyncio
import selectors
import sys
from collections.abc import Callable, Coroutine
from typing import Any


def loop_factory() -> Callable[[], asyncio.AbstractEventLoop] | None:
    if sys.platform == "win32":
        return lambda: asyncio.SelectorEventLoop(selectors.SelectSelector())
    return None


def run[T](coro: Coroutine[Any, Any, T]) -> T:
    """``asyncio.run`` with a psycopg-compatible loop on every platform."""
    return asyncio.run(coro, loop_factory=loop_factory())
