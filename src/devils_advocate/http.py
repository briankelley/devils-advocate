"""Shared HTTP client factory."""

from __future__ import annotations

import os

import httpx


def make_async_client(**kwargs) -> httpx.AsyncClient:
    """Create an httpx.AsyncClient, respecting DVAD_SSL_VERIFY=0 for self-signed certs."""
    if os.environ.get("DVAD_SSL_VERIFY", "1") == "0":
        kwargs.setdefault("verify", False)
    return httpx.AsyncClient(**kwargs)
