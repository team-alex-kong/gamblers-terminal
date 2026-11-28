import logging
import threading
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import core.main as main
import core.module as mod
import modules.ai_ratios.cache as ai_cache
import modules.crypto_tracker.cache as crypto_cache
import modules.pe_monitor.scheduler as pe_sched
from modules.pe_monitor import storage

T = TypeVar("T")


def _capture(store: list[T], fn: Callable[..., T]) -> Callable[..., T]:
    def wrap(*a: object, **k: object) -> T:
        obj = fn(*a, **k)
        store.append(obj)
        return obj
    return wrap


def test_lifespan_starts_and_stops_schedulers(make_app: Callable[..., FastAPI], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pe_sched, "snapshot_all", lambda *a, **k: None)
    monkeypatch.setattr(ai_cache, "refresh", lambda: {})
    monkeypatch.setattr(crypto_cache, "refresh", lambda: {})
    threads: list[threading.Thread] = []
    monkeypatch.setattr(threading, "Thread", _capture(threads, threading.Thread))

    with TestClient(make_app()) as c:
        assert c.get("/").status_code == 200
    assert {t.name for t in threads} == {"periodic-pe_monitor", "periodic-ai_ratios", "periodic-crypto_tracker"}
    for t in threads:
        t.join(timeout=2)
        assert not t.is_alive()


def test_schedulers_disabled_still_initialises(make_app: Callable[..., FastAPI], monkeypatch: pytest.MonkeyPatch) -> None:
    threads: list[threading.Thread] = []
    monkeypatch.setattr(threading, "Thread", _capture(threads, threading.Thread))
    inited: list[None] = []
    monkeypatch.setattr(storage, "init_db", _capture(inited, storage.init_db))

    with TestClient(make_app(enable_schedulers=False)) as c:
        assert c.get("/").status_code == 200
    assert not threads
    assert inited


def test_periodic_job_skips_busy_and_recovers(caplog: pytest.LogCaptureFixture) -> None:
    class _Busy(Exception):
        pass

    done = threading.Event()
    calls = 0

    def tick() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise _Busy()
        if calls == 2:
            raise RuntimeError("boom")
        done.set()

    with caplog.at_level(logging.ERROR, logger="core.module"), mod.periodic_job("demo", tick, 0.01, _Busy):
        assert done.wait(timeout=2)
    assert "demo: scheduled refresh failed (boom)" in caplog.text
    assert "_Busy" not in caplog.text


def test_configure_logging_daily_rotation(tmp_path: Path) -> None:
    root = logging.getLogger()
    uv_loggers = [logging.getLogger(n) for n in ("uvicorn", "uvicorn.access")]
    before, level = list(root.handlers), root.level
    log_dir = tmp_path / "var" / "log"
    handler = main.configure_logging(log_dir)
    try:
        assert handler.when == "MIDNIGHT"
        assert handler.backupCount == 7
        assert main.configure_logging(log_dir) is handler
        logging.getLogger("test.logging").info("hello rotation")
        handler.flush()
        assert "hello rotation" in (log_dir / "app.log").read_text(encoding="utf-8")
    finally:
        for lg in [root, *uv_loggers]:
            if handler in lg.handlers:
                lg.removeHandler(handler)
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)
        handler.close()
        root.setLevel(level)
