"""Devil's Advocate web GUI — app factory."""

from __future__ import annotations

from pathlib import Path


def create_app(config_path: str | None = None):
    """Create and return the FastAPI application."""
    from .app import build_app
    return build_app(config_path=config_path)
