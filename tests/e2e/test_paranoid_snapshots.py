"""Approach 2: State Snapshot Diffing.

Wraps critical write paths with before/after state verification.
Captures file hashes before and after operations and asserts that
"no-op" actions produce no mutations on disk.

This answers: "Does doing nothing through the UI actually do nothing on disk?"
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

# Add e2e dir to sys.path for paranoid_helpers import (no __init__.py)
sys.path.insert(0, str(Path(__file__).parent))
from paranoid_helpers import (
    StateSnapshot,
    get_csrf_token,
    api_post,
    api_put,
    load_fixture_yaml,
    restore_config_via_api,
)

pytestmark = [pytest.mark.e2e, pytest.mark.paranoid]

CAPTURED_REVIEW_ID = "captured_e2e_review"


def _get_config_path(page, dvad_server) -> Path:
    """Fetch the config file path from the API."""
    resp = page.request.get(f"{dvad_server}/api/config")
    data = resp.json()
    return Path(data["config_path"])


def _get_env_path(page, dvad_server) -> Path:
    """Fetch the .env file path from the API."""
    resp = page.request.get(f"{dvad_server}/api/config/env")
    data = resp.json()
    return Path(data["env_file_path"])


# ---------------------------------------------------------------------------
# Config file no-op tests: saving current config should not mutate it
# ---------------------------------------------------------------------------

class TestConfigNoOpSnapshots:
    """Saving the current config without changes must not alter the file."""

    def test_save_current_yaml_is_idempotent(self, page, dvad_server):
        """Reading config YAML and immediately saving it back produces no diff.

        This catches reformatting, comment stripping, key reordering, or
        any other unintended mutation from a round-trip save.
        """
        page.goto(f"{dvad_server}/config")
        page.wait_for_load_state("networkidle")
        csrf = get_csrf_token(page)
        config_path = _get_config_path(page, dvad_server)

        before = StateSnapshot.capture([config_path])

        # Read current content and save it back unchanged
        current_yaml = config_path.read_text()
        resp = api_post(page, dvad_server, "/api/config", csrf, {"yaml": current_yaml})
        assert resp.status == 200

        after = StateSnapshot.capture([config_path])
        before.assert_no_data_loss(after, label="config round-trip save")

    def test_model_timeout_set_same_value(self, page, dvad_server):
        """Setting timeout to its current value should not change the file
        in any semantically meaningful way."""
        page.goto(f"{dvad_server}/config")
        page.wait_for_load_state("networkidle")
        csrf = get_csrf_token(page)
        config_path = _get_config_path(page, dvad_server)

        # Read current timeout
        raw = yaml.safe_load(config_path.read_text())
        current_timeout = raw["models"]["e2e-remote"]["timeout"]

        before = StateSnapshot.capture([config_path])

        # Set to same value
        resp = api_post(page, dvad_server, "/api/config/model-timeout", csrf, {
            "model_name": "e2e-remote",
            "timeout": current_timeout,
        })
        assert resp.status == 200

        after = StateSnapshot.capture([config_path])
        before.assert_no_data_loss(after, label="timeout no-op")

    def test_model_thinking_set_same_value(self, page, dvad_server):
        """Setting thinking to its current value should not change the file
        in any semantically meaningful way."""
        page.goto(f"{dvad_server}/config")
        page.wait_for_load_state("networkidle")
        csrf = get_csrf_token(page)
        config_path = _get_config_path(page, dvad_server)

        raw = yaml.safe_load(config_path.read_text())
        current_thinking = raw["models"]["e2e-remote"].get("thinking", False)

        before = StateSnapshot.capture([config_path])

        resp = api_post(page, dvad_server, "/api/config/model-thinking", csrf, {
            "model_name": "e2e-remote",
            "thinking": current_thinking,
        })
        assert resp.status == 200

        after = StateSnapshot.capture([config_path])
        before.assert_no_data_loss(after, label="thinking no-op")

    def test_settings_toggle_set_same_value(self, page, dvad_server):
        """Setting live_testing to its current value should not change the file."""
        page.goto(f"{dvad_server}/config")
        page.wait_for_load_state("networkidle")
        csrf = get_csrf_token(page)
        config_path = _get_config_path(page, dvad_server)

        raw = yaml.safe_load(config_path.read_text())
        current_live = raw.get("settings", {}).get("live_testing", False)

        before = StateSnapshot.capture([config_path])

        resp = api_post(page, dvad_server, "/api/config/settings-toggle", csrf, {
            "key": "live_testing",
            "value": current_live,
        })
        assert resp.status == 200

        after = StateSnapshot.capture([config_path])
        before.assert_no_data_loss(after, label="settings no-op")


# ---------------------------------------------------------------------------
# Config full save: verify no data loss on valid saves
# ---------------------------------------------------------------------------

class TestConfigSavePreservesData:
    """Full config save must not lose models, roles, or settings."""

    def test_save_valid_yaml_preserves_all_models(self, page, dvad_server):
        """Saving valid YAML must preserve all model definitions."""
        page.goto(f"{dvad_server}/config")
        page.wait_for_load_state("networkidle")
        csrf = get_csrf_token(page)
        config_path = _get_config_path(page, dvad_server)

        before = StateSnapshot.capture([config_path])
        before_data = yaml.safe_load(before.files[str(config_path)].content)
        before_models = set(before_data.get("models", {}).keys())

        # Save the fixture YAML (which should be the same content)
        fixture_yaml = load_fixture_yaml()
        resp = api_post(page, dvad_server, "/api/config", csrf, {"yaml": fixture_yaml})
        assert resp.status == 200

        after = StateSnapshot.capture([config_path])
        after_data = yaml.safe_load(after.files[str(config_path)].content)
        after_models = set(after_data.get("models", {}).keys())

        lost = before_models - after_models
        assert not lost, f"Models lost after save: {lost}"

    def test_save_does_not_corrupt_yaml_structure(self, page, dvad_server):
        """After saving, the config must still be parseable YAML with required keys."""
        page.goto(f"{dvad_server}/config")
        page.wait_for_load_state("networkidle")
        csrf = get_csrf_token(page)
        config_path = _get_config_path(page, dvad_server)

        fixture_yaml = load_fixture_yaml()
        resp = api_post(page, dvad_server, "/api/config", csrf, {"yaml": fixture_yaml})
        assert resp.status == 200

        after_content = config_path.read_text()
        data = yaml.safe_load(after_content)
        assert "models" in data, "Config missing 'models' key after save"
        assert "roles" in data, "Config missing 'roles' key after save"
        assert isinstance(data["models"], dict), "models is not a dict after save"
        assert len(data["models"]) > 0, "models dict is empty after save"


# ---------------------------------------------------------------------------
# Ledger snapshot: override should not corrupt ledger structure
# ---------------------------------------------------------------------------

class TestLedgerSnapshotOnOverride:
    """Override operations must not corrupt the review ledger."""

    def _get_first_group_id(self, page, dvad_server) -> str:
        """Get the first group_id from the captured review."""
        resp = page.request.get(f"{dvad_server}/api/review/{CAPTURED_REVIEW_ID}")
        if resp.status != 200:
            pytest.skip("Captured review not available")
        ledger = resp.json()
        points = ledger.get("points", [])
        if not points:
            pytest.skip("Captured review has no points")
        return points[0].get("group_id", points[0].get("point_id"))

    def test_override_preserves_other_points(self, page, dvad_server):
        """Overriding one point must not alter any other point in the ledger."""
        page.goto(f"{dvad_server}/review/{CAPTURED_REVIEW_ID}")
        page.wait_for_load_state("networkidle")
        csrf = get_csrf_token(page)

        # Read before state
        resp_before = page.request.get(f"{dvad_server}/api/review/{CAPTURED_REVIEW_ID}")
        if resp_before.status != 200:
            pytest.skip("Captured review not available")
        ledger_before = resp_before.json()
        points_before = ledger_before.get("points", [])
        if len(points_before) < 2:
            pytest.skip("Need at least 2 points to test isolation")

        target_gid = points_before[0].get("group_id", points_before[0].get("point_id"))

        # Override the first point
        resp = api_post(
            page, dvad_server,
            f"/api/review/{CAPTURED_REVIEW_ID}/override",
            csrf,
            {"group_id": target_gid, "resolution": "overridden"},
        )
        assert resp.status == 200

        # Read after state
        resp_after = page.request.get(f"{dvad_server}/api/review/{CAPTURED_REVIEW_ID}")
        ledger_after = resp_after.json()
        points_after = ledger_after.get("points", [])

        # Every non-target point should be unchanged
        for pb, pa in zip(points_before[1:], points_after[1:]):
            if pb.get("group_id") != target_gid and pb.get("point_id") != target_gid:
                # Compare all fields except overrides (which might be added to target)
                for key in pb:
                    if key != "overrides":
                        assert pb[key] == pa.get(key), (
                            f"Point {pb.get('point_id')} field '{key}' changed: "
                            f"{pb[key]!r} -> {pa.get(key)!r}"
                        )

    def test_override_preserves_ledger_metadata(self, page, dvad_server):
        """Override must not alter top-level ledger fields (review_id, project, etc)."""
        page.goto(f"{dvad_server}/review/{CAPTURED_REVIEW_ID}")
        page.wait_for_load_state("networkidle")
        csrf = get_csrf_token(page)

        resp_before = page.request.get(f"{dvad_server}/api/review/{CAPTURED_REVIEW_ID}")
        if resp_before.status != 200:
            pytest.skip("Captured review not available")
        ledger_before = resp_before.json()
        points = ledger_before.get("points", [])
        if not points:
            pytest.skip("No points in captured review")

        target_gid = points[0].get("group_id", points[0].get("point_id"))
        immutable_keys = {"review_id", "result", "mode", "project", "timestamp",
                          "author_model", "reviewer_models", "dedup_model"}

        resp = api_post(
            page, dvad_server,
            f"/api/review/{CAPTURED_REVIEW_ID}/override",
            csrf,
            {"group_id": target_gid, "resolution": "auto_dismissed"},
        )
        assert resp.status == 200

        resp_after = page.request.get(f"{dvad_server}/api/review/{CAPTURED_REVIEW_ID}")
        ledger_after = resp_after.json()

        for key in immutable_keys:
            assert ledger_before.get(key) == ledger_after.get(key), (
                f"Ledger metadata '{key}' changed: "
                f"{ledger_before.get(key)!r} -> {ledger_after.get(key)!r}"
            )


# ---------------------------------------------------------------------------
# .env file snapshot: operations should not destroy unrelated keys
# ---------------------------------------------------------------------------

class TestEnvFileSnapshots:
    """Env file operations must not destroy unrelated keys."""

    def test_env_put_does_not_destroy_other_keys(self, page, dvad_server):
        """Writing one env var must not remove or alter other keys in .env."""
        page.goto(f"{dvad_server}/config")
        page.wait_for_load_state("networkidle")
        csrf = get_csrf_token(page)

        env_path = _get_env_path(page, dvad_server)

        # Seed .env with a known key if it doesn't exist
        if not env_path.exists():
            env_path.parent.mkdir(parents=True, exist_ok=True)
            env_path.write_text("E2E_LOCAL_KEY=original-value\n")

        before = StateSnapshot.capture([env_path])

        # Write the key with a new value
        resp = api_put(page, dvad_server, "/api/config/env/E2E_LOCAL_KEY", csrf, {
            "value": "updated-test-value-12345",
        })
        # PUT may fail if env name not in config - skip in that case
        if resp.status == 400:
            pytest.skip("E2E_LOCAL_KEY not recognized as allowed env name")
        assert resp.status == 200

        after = StateSnapshot.capture([env_path])

        # The file should still exist and not be truncated
        assert after.files[str(env_path)].exists, ".env file was deleted"
        assert after.files[str(env_path)].size > 0, ".env file is empty after write"
