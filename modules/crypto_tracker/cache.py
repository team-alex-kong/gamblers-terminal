"""In-memory cache + this module's scheduler.

Computing fetches live + historical Binance prices, so we refresh on a schedule
and serve the cached result instantly. Refreshes are single-flight; a failed
refresh keeps the last known-good result and records the error.
"""

import threading
from contextlib import AbstractContextManager

from core.module import periodic_job

from . import config, twr

_lock = threading.Lock()
_refresh_lock = threading.Lock()
_state: dict[str, object] = {
    "computed_at": None,
    "as_of": None,
    "total_value": None,
    "holdings": [],
    "ranges": [],
    "stale": True,
    "last_error": None,
}


class Busy(Exception):
    """A refresh is already in progress."""


def get() -> dict[str, object]:
    with _lock:
        return dict(_state)


def refresh() -> dict[str, object]:
    if not _refresh_lock.acquire(blocking=False):
        raise Busy()
    try:
        result = twr.compute()
        with _lock:
            _state.update(result, stale=False, last_error=None)
            return dict(_state)
    except Exception as e:
        with _lock:
            _state.update(stale=True, last_error=f"refresh failed: {e}")
            return dict(_state)
    finally:
        _refresh_lock.release()


def scheduler_lifespan() -> AbstractContextManager[None]:
    return periodic_job("crypto_tracker", refresh, config.REFRESH_INTERVAL_SECONDS, Busy)
