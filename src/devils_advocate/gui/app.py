"""App assembly: FastAPI app, static mount, templates, lifespan."""

from __future__ import annotations

import logging
import os
import secrets
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from .runner import ReviewRunner

_HERE = Path(__file__).parent
STATIC_DIR = _HERE / "static"
TEMPLATE_DIR = _HERE / "templates"

logger = logging.getLogger("dvad.app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage app startup/shutdown lifecycle."""
    # Log version and process details at startup for diagnostics
    from devils_advocate import __version__
    logger.warning(
        "dvad starting: version=%s pid=%d python=%s dist=%s",
        __version__,
        os.getpid(),
        sys.executable,
        _find_dist_info(),
    )
    yield
    logger.info("dvad shutting down: pid=%d", os.getpid())
    # Shutdown: cancel any running review
    runner: ReviewRunner = app.state.runner
    if runner.current_task and not runner.current_task.done():
        runner.current_task.cancel()
        try:
            await runner.current_task
        except Exception:
            pass


def _find_dist_info() -> str:
    """Find the dist-info path for diagnostics."""
    for p in Path(sys.prefix, "lib").rglob("devils_advocate-*.dist-info"):
        return str(p)
    return "(not found)"


def build_app(config_path: str | None = None) -> FastAPI:
    """Assemble the FastAPI application with all routes and middleware."""
    app = FastAPI(title="Devil's Advocate", lifespan=lifespan)

    # Force browser to revalidate all responses
    class NoCacheMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            response: Response = await call_next(request)
            if request.url.path.startswith("/static/") or request.url.path == "/" or "text/html" in response.headers.get("content-type", ""):
                response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
                response.headers["Pragma"] = "no-cache"
                response.headers["Expires"] = "0"
            return response

    app.add_middleware(NoCacheMiddleware)

    # Static files
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # Templates
    templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

    # Custom Jinja filters
    def human_date(value: str) -> str:
        from datetime import datetime
        try:
            dt = datetime.fromisoformat(value)
            return dt.strftime("%-d %b %Y, %H:%M")
        except (ValueError, TypeError):
            return str(value)

    templates.env.filters["human_date"] = human_date

    # Global template variables
    from devils_advocate import __version__
    templates.env.globals["version"] = __version__

    # CSRF token (startup-generated)
    csrf_token = secrets.token_urlsafe(32)

    # Shared state
    app.state.templates = templates
    app.state.csrf_token = csrf_token
    app.state.config_path = config_path
    app.state.runner = ReviewRunner()

    # Register routers
    from .pages import router as pages_router
    from .api import router as api_router

    app.include_router(pages_router)
    app.include_router(api_router, prefix="/api")

    return app
