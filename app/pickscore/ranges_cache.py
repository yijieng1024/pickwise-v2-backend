"""
In-process cache of the PickScore scoring data: the laptop ranges plus both
benchmark lists, built together because every scorer needs all three.

Building it fuzzy-matches every catalog CPU and GPU against ~9,000 benchmark
rows: 9 s on a desktop, far more on a small Render instance. It used to expire
every 5 minutes and be rebuilt inside whichever request came next, holding that
request's database connection idle for the whole build - which is how
calculate-score died with "SSL connection has been closed unexpectedly" on its
next query, and why every other request stalled behind it (the build holds the
GIL).

So a request never builds it when a copy exists. A stale copy is served while
ONE background thread rebuilds. Stale means a write to the source tables was
committed (invalidate(), from the session listeners in pickscore_adapter.py),
or the copy is older than CACHE_TTL - which only exists to catch writes this
process cannot see: scripts, hand-written SQL, another instance.

A generation counter keeps a rebuild that started before a write from being
filed as current: it stores under the generation it read at, so the copy stays
stale and the next request starts another rebuild.
"""

import logging
import threading
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

CACHE_TTL = 6 * 3600

_lock = threading.Lock()
_data: Any = None
_built_at = 0.0
_built_generation = -1
_generation = 0
_refreshing = False


def generation() -> int:
    return _generation


def invalidate() -> None:
    """Mark the cached copy stale. Call after the write is committed."""
    global _generation
    with _lock:
        _generation += 1


def store(data: Any, built_at_generation: int) -> None:
    global _data, _built_at, _built_generation
    with _lock:
        if built_at_generation >= _built_generation:
            _data, _built_at, _built_generation = data, time.time(), built_at_generation


def get(build: Callable[[], Any]) -> Optional[Any]:
    """
    The cached copy, or None if none was ever built (the caller builds it
    then - there is nothing to serve meanwhile). A stale copy is returned and
    a background rebuild with `build` started, which must open its own
    database session.
    """
    with _lock:
        stale = _built_generation != _generation or time.time() - _built_at > CACHE_TTL
        if stale and _data is not None:
            _start_rebuild(build)
        return _data


def warm(build: Callable[[], Any]) -> None:
    """Build in the background now, so the first request does not have to."""
    with _lock:
        _start_rebuild(build)


def _start_rebuild(build: Callable[[], Any]) -> None:
    """Caller holds _lock. One rebuild at a time."""
    global _refreshing
    if not _refreshing:
        _refreshing = True
        threading.Thread(target=_rebuild, args=(build, _generation), daemon=True).start()


def _rebuild(build: Callable[[], Any], at_generation: int) -> None:
    global _refreshing
    try:
        store(build(), at_generation)
    except Exception:
        # The stale copy keeps serving; the next request tries again.
        logger.exception("PickScore scoring cache rebuild failed")
    finally:
        with _lock:
            _refreshing = False
