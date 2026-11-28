"""Host app factory: discovers modules, mounts each under its slug, serves the
landing page.

The only things unified here are the interface, the port, and authentication.
Each module brings its own routes, static, templates and scheduler. `build_app`
is a pure factory (no import-time work); `create_app` is the zero-arg entry that
uvicorn calls via `--factory`.
"""

import logging
import os
from collections.abc import AsyncIterator
from contextlib import ExitStack, asynccontextmanager
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from core import config as host_config
from core.auth import make_auth_gate
from core.module import Module
from core.registry import discover_modules

_HERE = Path(__file__).resolve().parent
SESSION_MAX_AGE = 7 * 24 * 60 * 60  # 7 days
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
logger = logging.getLogger(__name__)


def configure_logging(log_dir: Path = Path("var/log")) -> TimedRotatingFileHandler:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    existing = next((h for h in root.handlers if isinstance(h, TimedRotatingFileHandler)), None)
    if existing is not None:
        return existing

    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(LOG_FORMAT)
    file_handler = TimedRotatingFileHandler(
        log_dir / "app.log",
        when="midnight",
        interval=1,
        backupCount=7,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    if not any(type(h) is logging.StreamHandler for h in root.handlers):
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        root.addHandler(stream_handler)

    for name in ("uvicorn", "uvicorn.access"):
        uv_logger = logging.getLogger(name)
        if not any(isinstance(h, TimedRotatingFileHandler) for h in uv_logger.handlers):
            uv_logger.addHandler(file_handler)
    return file_handler


def _log_config(config: host_config.HostConfig, modules: list[Module]) -> None:
    show = os.environ.get("GAMBLERS_TOOLBOX_LOG_SECRETS", "").lower() in ("1", "true", "yes")
    tokens = config.auth_tokens
    secret_disp = config.secret_key if show else ("set" if config.secret_key else "unset")
    if show:
        tokens_disp = tokens or "[]  (auth DISABLED)"
    else:
        tokens_disp = f"{len(tokens)} configured" if tokens else "none (auth DISABLED)"
    logger.info("config:      %s", host_config.config_source())
    logger.info("bind:        %s:%s", config.host, config.port)
    logger.info("secret_key:  %s", secret_disp)
    logger.info("auth_tokens: %s", tokens_disp)
    logger.info("modules:     %s", ", ".join(m.slug for m in modules) or "(none)")
    logger.info("schedulers:  %s", "on" if config.enable_schedulers else "off")
    if tokens and not show:
        logger.info("(set GAMBLERS_TOOLBOX_LOG_SECRETS=1 to log token/secret values)")


def _validate_modules(modules: list[Module]) -> None:
    slugs = [m.slug for m in modules]
    dupes = sorted({s for s in slugs if slugs.count(s) > 1})
    if dupes:
        raise RuntimeError(f"duplicate module slugs: {dupes}")
    names = [m.static_name or f"{m.slug}_static" for m in modules if m.static_dir]
    dupe_names = sorted({n for n in names if names.count(n) > 1})
    if dupe_names:
        raise RuntimeError(f"duplicate static mount names: {dupe_names}")


def build_app(config: host_config.HostConfig, modules: list[Module]) -> FastAPI:
    host_config.check_secret(config)
    _validate_modules(modules)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # `lifespan` (resource setup, e.g. DB init) runs on every instance.
        # `scheduler` (background jobs) is skipped when enable_schedulers is off,
        # so web replicas still initialise but jobs run on exactly one instance.
        # Teardown is reverse order: schedulers stop before resources close.
        with ExitStack() as stack:
            for m in modules:
                if m.lifespan is not None:
                    stack.enter_context(m.lifespan())
            if config.enable_schedulers:
                for m in modules:
                    if m.scheduler is not None:
                        stack.enter_context(m.scheduler())
            yield

    app = FastAPI(title="Gambler's Terminal", lifespan=lifespan)

    # Gate first, then SessionMiddleware last so it sits outermost and
    # request.session is populated before the gate reads it.
    app.middleware("http")(make_auth_gate(config.auth_tokens))
    app.add_middleware(SessionMiddleware, secret_key=config.secret_key, session_cookie=config.session_cookie or f"gt_session_{config.port}", max_age=SESSION_MAX_AGE)

    templates = Jinja2Templates(directory=str(_HERE / "templates"))
    app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")

    for m in modules:
        app.include_router(m.router, prefix=f"/{m.slug}", tags=[m.name])
        if m.static_dir:
            app.mount(
                f"/{m.slug}/static",
                StaticFiles(directory=m.static_dir),
                name=m.static_name or f"{m.slug}_static",
            )

    @app.get("/", include_in_schema=False)
    def landing(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "landing.html", {"modules": modules})

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> FileResponse:
        return FileResponse(_HERE / "static" / "favicon.ico")

    return app


def create_app() -> FastAPI:
    """Zero-arg factory for uvicorn (`uvicorn --factory core.main:create_app`)."""
    configure_logging()
    config = host_config.load_config()
    modules = discover_modules()
    _log_config(config, modules)
    return build_app(config, modules)
