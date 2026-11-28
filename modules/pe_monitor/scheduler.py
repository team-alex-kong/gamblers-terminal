"""Periodic crawler: dispatches each ticker to the right fetcher and persists.

Snapshots are single-flight — a scheduled tick overlapping a running snapshot is
skipped; a manual refresh that overlaps raises Busy (surfaced as HTTP 409).
"""

import logging
import threading
from typing import Any

from . import fetcher, storage

logger = logging.getLogger(__name__)
_snapshot_lock = threading.Lock()


class Busy(Exception):
    """A snapshot is already in progress."""


def _has_usable_data(snap: dict[str, Any]) -> bool:
    price = snap.get("price")
    return price is not None and price > 0


def snapshot_all(tickers: list[str], db_path: str) -> None:
    if not _snapshot_lock.acquire(blocking=False):
        raise Busy()
    try:
        for t in tickers:
            try:
                snap = fetcher.get_fetcher(t).fetch_pe(t)
                if _has_usable_data(snap):
                    storage.append_snapshot(db_path, t, snap)
                else:
                    logger.warning("Skipping %s: no usable price in response", t)
            except Exception as e:
                logger.warning("Could not fetch %s: %s", t, e)
    finally:
        _snapshot_lock.release()
