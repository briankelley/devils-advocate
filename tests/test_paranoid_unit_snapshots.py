"""Approach 2: State Snapshot Diffing.

Wraps critical write paths with before/after file-system verification.
Catches cases where a "no-op" action (submitting without changes,
read-only requests) unexpectedly mutates on-disk state.
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

pytest_plugins = ["conftest_paranoid_unit"]


# ── Read-only endpoints must not mutate disk ────────────────────────────────


class TestReadOnlyEndpointsNoMutation:
    """GET endpoints must never modify the config or review data on disk."""

    def test_get_config_json_no_mutation(
        self, paranoid_client, config_snapshot, temp_config_dir,
    ):
        """GET /api/config must not mutate models.yaml."""
        config_snapshot.capture("before")
        paranoid_client.get("/api/config")
        config_snapshot.capture("after")
        assert config_snapshot.is_identical(), (
            f"GET /api/config mutated the config directory:\n"
            f"{config_snapshot.diff_report()}"
        )

    def test_get_config_readiness_no_mutation(
        self, paranoid_client, config_snapshot,
    ):
        config_snapshot.capture("before")
        paranoid_client.get("/api/config/readiness")
        config_snapshot.capture("after")
        assert config_snapshot.is_identical()

    def test_get_config_env_no_mutation(
        self, paranoid_client, config_snapshot,
    ):
        config_snapshot.capture("before")
        paranoid_client.get("/api/config/env")
        config_snapshot.capture("after")
        assert config_snapshot.is_identical()

    def test_get_review_json_no_mutation(
        self, paranoid_client, review_snapshot, temp_review_env,
    ):
        _, _, review_id = temp_review_env
        review_snapshot.capture("before")
        paranoid_client.get(f"/api/review/{review_id}")
        review_snapshot.capture("after")
        assert review_snapshot.is_identical()

    def test_dashboard_no_mutation(
        self, paranoid_client, config_snapshot,
    ):
        config_snapshot.capture("before")
        paranoid_client.get("/")
        config_snapshot.capture("after")
        assert config_snapshot.is_identical()

    def test_fs_ls_no_mutation(
        self, paranoid_client, config_snapshot,
    ):
        """Filesystem browser must not write anything."""
        config_snapshot.capture("before")
        paranoid_client.get("/api/fs/ls")
        config_snapshot.capture("after")
        assert config_snapshot.is_identical()


# ── Validation endpoint must not mutate disk ────────────────────────────────


class TestValidateEndpointNoMutation:
    """POST /api/config/validate is logically read-only despite being POST.
    It must not leave any files behind.
    """

    def test_validate_does_not_mutate_config(
        self, paranoid_client, csrf_token, config_snapshot,
    ):
        config_snapshot.capture("before")

        paranoid_client.post(
            "/api/config/validate",
            json={"yaml": "models:\n  m: {}\nroles:\n  author: m\n"},
            headers={"X-DVAD-Token": csrf_token},
        )

        config_snapshot.capture("after")
        assert config_snapshot.is_identical(), (
            f"POST /api/config/validate mutated the config directory:\n"
            f"{config_snapshot.diff_report()}"
        )

    def test_validate_with_garbage_does_not_mutate_config(
        self, paranoid_client, csrf_token, config_snapshot,
    ):
        config_snapshot.capture("before")

        paranoid_client.post(
            "/api/config/validate",
            json={"yaml": "{{{{not yaml at all"},
            headers={"X-DVAD-Token": csrf_token},
        )

        config_snapshot.capture("after")
        assert config_snapshot.is_identical()


# ── Failed write operations must not leave partial state ────────────────────


class TestFailedWritesNoPartialState:
    """When a write operation fails validation, on-disk state must be unchanged."""

    def test_save_bad_yaml_leaves_config_intact(
        self, paranoid_client, csrf_token, config_snapshot,
    ):
        """Saving invalid YAML must not overwrite the existing config."""
        config_snapshot.capture("before")

        resp = paranoid_client.post(
            "/api/config",
            json={"yaml": "{{{{not yaml"},
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code == 400

        config_snapshot.capture("after")
        assert config_snapshot.is_identical(), (
            f"Failed config save left partial state:\n"
            f"{config_snapshot.diff_report()}"
        )

    def test_save_yaml_missing_models_leaves_config_intact(
        self, paranoid_client, csrf_token, config_snapshot,
    ):
        config_snapshot.capture("before")

        resp = paranoid_client.post(
            "/api/config",
            json={"yaml": "foo: bar\n"},
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code == 400

        config_snapshot.capture("after")
        assert config_snapshot.is_identical()

    def test_save_yaml_missing_roles_leaves_config_intact(
        self, paranoid_client, csrf_token, config_snapshot,
    ):
        config_snapshot.capture("before")

        resp = paranoid_client.post(
            "/api/config",
            json={"yaml": "models:\n  m:\n    provider: openai\n"},
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code == 400

        config_snapshot.capture("after")
        assert config_snapshot.is_identical()

    def test_override_nonexistent_review_no_mutation(
        self, paranoid_client, csrf_token, review_snapshot,
    ):
        """Override on a nonexistent review must not create any files."""
        review_snapshot.capture("before")

        resp = paranoid_client.post(
            "/api/review/nonexistent_abc/override",
            json={"group_id": "grp-001", "resolution": "overridden"},
            headers={"X-DVAD-Token": csrf_token},
        )
        # Should fail (review not found or storage error)
        assert resp.status_code != 200

        review_snapshot.capture("after")
        assert review_snapshot.is_identical()

    def test_set_timeout_bad_model_leaves_config_intact(
        self, paranoid_client, csrf_token, config_snapshot,
    ):
        """Setting timeout on a nonexistent model must not mutate config."""
        config_snapshot.capture("before")

        resp = paranoid_client.post(
            "/api/config/model-timeout",
            json={"model_name": "DOES_NOT_EXIST", "timeout": 120},
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code != 200

        config_snapshot.capture("after")
        assert config_snapshot.is_identical()


# ── Successful write operations must preserve unrelated state ───────────────


class TestSuccessfulWritesPreserveUnrelated:
    """When a write succeeds, only the targeted state should change."""

    def test_override_only_mutates_targeted_point(
        self, paranoid_client, csrf_token, temp_review_env,
    ):
        """Overriding one point must not alter any other point's resolution."""
        data_dir, review_dir, review_id = temp_review_env
        ledger_path = review_dir / "review-ledger.json"

        # Read before state
        before_ledger = json.loads(ledger_path.read_text())
        before_pt2 = before_ledger["points"][1].copy()

        resp = paranoid_client.post(
            f"/api/review/{review_id}/override",
            json={"group_id": "grp-001", "resolution": "overridden"},
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code == 200

        # Read after state
        after_ledger = json.loads(ledger_path.read_text())

        # Point 1 should be changed
        assert after_ledger["points"][0]["final_resolution"] == "overridden"
        assert "overrides" in after_ledger["points"][0]

        # Point 2 must be EXACTLY the same
        after_pt2 = after_ledger["points"][1]
        assert after_pt2["final_resolution"] == before_pt2["final_resolution"]
        assert after_pt2.get("overrides") == before_pt2.get("overrides")

    def test_timeout_change_preserves_other_model_settings(
        self, paranoid_client, csrf_token, temp_config_dir,
    ):
        """Changing one model's timeout must not alter other models."""
        config_path = temp_config_dir / "models.yaml"
        import yaml

        before_raw = yaml.safe_load(config_path.read_text())
        before_reviewer = before_raw["models"]["reviewer-model"].copy()

        resp = paranoid_client.post(
            "/api/config/model-timeout",
            json={"model_name": "test-model", "timeout": 300},
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code == 200

        after_raw = yaml.safe_load(config_path.read_text())

        # test-model should have new timeout
        assert after_raw["models"]["test-model"]["timeout"] == 300

        # reviewer-model must be unchanged
        for key in before_reviewer:
            assert after_raw["models"]["reviewer-model"].get(key) == before_reviewer[key], (
                f"reviewer-model.{key} changed from {before_reviewer[key]} "
                f"to {after_raw['models']['reviewer-model'].get(key)}"
            )


# ── Config backup verification ──────────────────────────────────────────────


class TestConfigBackupCreated:
    """Verify that raw YAML config save creates a .bak file."""

    def test_raw_yaml_save_creates_backup(
        self, paranoid_client, csrf_token, temp_config_dir,
    ):
        """POST /api/config (raw yaml) must create models.yaml.bak."""
        config_path = temp_config_dir / "models.yaml"
        backup_path = config_path.with_suffix(".yaml.bak")
        original_content = config_path.read_text()

        assert not backup_path.exists(), "Backup should not exist before first save"

        resp = paranoid_client.post(
            "/api/config",
            json={"yaml": original_content},
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code == 200

        assert backup_path.exists(), (
            "POST /api/config did not create a .bak backup before overwriting"
        )
        assert backup_path.read_text() == original_content, (
            "Backup content does not match the original config"
        )
