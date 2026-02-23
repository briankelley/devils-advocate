"""Shared helpers for GUI modules."""

from __future__ import annotations

from pathlib import Path

from ..storage import StorageManager


def get_gui_storage() -> StorageManager:
    """Instantiate a read-oriented StorageManager with a stable project_dir."""
    return StorageManager(Path.home())
