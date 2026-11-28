import threading
from collections.abc import Callable
from typing import TypeVar

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

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


def test_periodic_job_skips_busy_and_recovers(capsys: pytest.CaptureFixture[str]) -> None:
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

    with mod.periodic_job("demo", tick, 0.01, _Busy):
        assert done.wait(timeout=2)
    out = capsys.readouterr().out
    assert "demo: scheduled refresh failed (boom)" in out
    assert "_Busy" not in out
