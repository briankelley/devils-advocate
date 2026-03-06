"""Shared fixtures for paranoid test suite.

All paranoid tests operate on COPIES of data in temp directories.
No test writes to source-controlled paths or the real user config.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient

from paranoid_unit_helpers import (
    MINIMAL_VALID_YAML,
    SAMPLE_ENV_CONTENT,
    SAMPLE_LEDGER,
    StateSnapshot,
    make_temp_config_dir,
    make_temp_review_dir,
)


@pytest.fixture
def temp_config_dir():
    """Temp directory with models.yaml + .env. Cleaned up after test."""
    tmpdir = make_temp_config_dir()
    yield tmpdir
    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
def temp_review_env():
    """Temp review data directory with a pre-populated review ledger.

    Returns (data_dir, review_dir, review_id).
    """
    review_id = "test-review-001"
    data_dir, review_dir = make_temp_review_dir(review_id=review_id)
    yield data_dir, review_dir, review_id
    shutil.rmtree(data_dir, ignore_errors=True)


@pytest.fixture
def paranoid_app(temp_config_dir, temp_review_env):
    """FastAPI app configured to use temp directories for ALL state.

    This is the keystone fixture: it ensures no paranoid test touches
    the real config or data directories.
    """
    config_path = str(temp_config_dir / "models.yaml")
    data_dir, review_dir, review_id = temp_review_env

    # Set env vars that config loading needs
    os.environ["TEST_KEY"] = "sk-test-paranoid-key-1234567890"

    from devils_advocate.gui.app import build_app

    app = build_app(config_path=config_path)

    # Patch get_gui_storage to use our temp data dir
    from devils_advocate.storage import StorageManager
    mock_storage = StorageManager(
        project_dir=Path(tempfile.mkdtemp(prefix="dvad-paranoid-proj-")),
        data_dir=data_dir,
    )

    with patch("devils_advocate.gui.api.get_gui_storage", return_value=mock_storage), \
         patch("devils_advocate.gui.pages.get_gui_storage", return_value=mock_storage):
        yield app

    # Cleanup env var
    os.environ.pop("TEST_KEY", None)


@pytest.fixture
def paranoid_client(paranoid_app):
    """TestClient connected to the paranoid app."""
    return TestClient(paranoid_app)


@pytest.fixture
def csrf_token(paranoid_app):
    """CSRF token for the paranoid app."""
    return paranoid_app.state.csrf_token


@pytest.fixture
def config_snapshot(temp_config_dir) -> StateSnapshot:
    """StateSnapshot pre-targeted at the temp config directory."""
    return StateSnapshot(temp_config_dir)


@pytest.fixture
def review_snapshot(temp_review_env) -> StateSnapshot:
    """StateSnapshot pre-targeted at the temp review directory."""
    data_dir, review_dir, _ = temp_review_env
    return StateSnapshot(review_dir)
