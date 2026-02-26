"""Devil's Advocate web GUI — app factory."""

from __future__ import annotations

import os


def create_app(config_path: str | None = None):
    """Create and return the FastAPI application."""
    from .app import build_app
    return build_app(config_path=config_path)


def create_app_from_env():
    """Factory for uvicorn --factory that reads config from env."""
    config_path = os.environ.get("DVAD_E2E_CONFIG") or None
    return create_app(config_path=config_path)
