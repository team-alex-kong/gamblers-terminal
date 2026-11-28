"""The interface every plugin under modules/ exposes as a `MODULE` constant.

The host (core/main.py) discovers these, mounts each router at /<slug>, mounts
its static dir, and enters its `lifespan` for the life of the app. Everything
else — data, schedulers, templates — stays private to the module.
"""

import threading
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass

from fastapi import APIRouter


@dataclass(frozen=True)
class Module:
    slug: str                 # URL segment, e.g. "pe-monitor"
    name: str                 # display name, e.g. "P/E Monitor"
    description: str          # one-line landing-card text
    router: APIRouter         # included at prefix=/<slug>, tags=[name]
    order: int = 100          # lower sorts earlier on the landing page
    icon: str | None = None
    static_dir: str | None = None   # abs path mounted at /<slug>/static
    static_name: str | None = None  # url_for name (match the template)
    # Resource setup (e.g. create/migrate the DB). Entered on every instance.
    lifespan: Callable[[], AbstractContextManager[None]] | None = None
    # Background jobs. Skipped when the host runs with enable_schedulers=false,
    # so replicas still initialise but jobs run on exactly one instance.
    scheduler: Callable[[], AbstractContextManager[None]] | None = None


@contextmanager
def periodic_job(
    name: str,
    fn: Callable[[], object],
    interval_seconds: float,
    busy: type[Exception] | None = None,
) -> Iterator[None]:
    stop = threading.Event()

    def loop() -> None:
        while not stop.is_set():
            try:
                fn()
            except Exception as exc:
                if not (busy and isinstance(exc, busy)):
                    print(f"  {name}: scheduled refresh failed ({exc})")
            stop.wait(interval_seconds)

    threading.Thread(target=loop, name=f"periodic-{name}", daemon=True).start()
    try:
        yield
    finally:
        stop.set()
