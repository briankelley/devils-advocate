"""Shared test fixtures."""

import pytest
import yaml as _yaml
from datetime import datetime, timezone

from devils_advocate.types import ReviewContext
from helpers import make_model_config, make_review_group


# ─── Live Test Gate ──────────────────────────────────────────────────────────


def _is_live_testing_enabled() -> bool:
    """Check models.yaml for settings.live_testing flag."""
    try:
        from devils_advocate.config import find_config
        config_path = find_config()
        with open(config_path) as f:
            raw = _yaml.safe_load(f)
        return bool(raw.get("settings", {}).get("live_testing", False))
    except Exception:
        return False


def pytest_collection_modifyitems(config, items):
    """Auto-skip @pytest.mark.live tests unless explicitly opted in."""
    markexpr = config.option.markexpr
    if markexpr and "live" in markexpr:
        return
    if _is_live_testing_enabled():
        return

    skip_live = pytest.mark.skip(
        reason="Live tests require: -m live flag, or settings.live_testing: true in models.yaml"
    )
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


# ─── live_testing Guard ──────────────────────────────────────────────────────


def _get_live_testing_value() -> tuple[str | None, bool]:
    """Read the current live_testing value from models.yaml.

    Returns (config_path_str, value).  config_path_str is None when the
    config file cannot be located.
    """
    try:
        from devils_advocate.config import find_config
        config_path = find_config()
        with open(config_path) as f:
            raw = _yaml.safe_load(f)
        return str(config_path), bool(raw.get("settings", {}).get("live_testing", False))
    except Exception:
        return None, False


@pytest.fixture(autouse=True, scope="session")
def _guard_live_testing_flag():
    """Snapshot live_testing before the session and restore it at teardown.

    Prevents tests that mutate models.yaml from leaving live_testing
    enabled, which would silently turn on live API tests in subsequent
    runs.
    """
    config_path, original_value = _get_live_testing_value()
    yield
    if config_path is None:
        return
    _, current_value = _get_live_testing_value()
    if current_value != original_value:
        # Restore original value
        from pathlib import Path
        from ruamel.yaml import YAML
        yaml = YAML()
        yaml.preserve_quotes = True
        path = Path(config_path)
        data = yaml.load(path.read_text())
        if "settings" not in data:
            data["settings"] = {}
        data["settings"]["live_testing"] = original_value
        from io import StringIO
        stream = StringIO()
        yaml.dump(data, stream)
        path.write_text(stream.getvalue())


# ─── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def fixed_time():
    """A fixed datetime for deterministic ID testing."""
    return datetime(2026, 2, 14, 18, 26, 0, tzinfo=timezone.utc)


@pytest.fixture
def review_context(fixed_time):
    return ReviewContext(
        project="test-project",
        review_id="test_review",
        review_start_time=fixed_time,
        id_suffix="abcd",
    )


@pytest.fixture
def sample_group():
    return make_review_group()


@pytest.fixture
def sample_model():
    return make_model_config()
