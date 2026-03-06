"""Approach 4: Permutation Fuzzing on Form State.

For every write endpoint in the registry, systematically tests input strategies:
ALL_EMPTY, PARTIALLY_FILLED, FILLED_THEN_CLEARED, PREEXISTING_WITH_EMPTY_SUBMIT,
BOUNDARY_MIN, BOUNDARY_MAX, BOUNDARY_OVER, BOUNDARY_UNDER, TYPE_CONFUSION.

The highest-value strategy is PREEXISTING_WITH_EMPTY_SUBMIT: existing good data
on disk, user submits empty input, backend interprets empty as "clear everything."
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import yaml
from fastapi.testclient import TestClient

from paranoid_unit_helpers import (
    MINIMAL_VALID_YAML,
    SAMPLE_LEDGER,
    StateSnapshot,
    make_temp_config_dir,
    make_temp_review_dir,
)

pytest_plugins = ["conftest_paranoid_unit"]


# ═════════════════════════════════════════════════════════════════════════════
# Config Save (Raw YAML) — highest risk: replaces entire config file
# ═════════════════════════════════════════════════════════════════════════════


class TestConfigSaveYamlFuzzing:
    """Fuzz POST /api/config (raw YAML path)."""

    def test_all_empty_yaml(self, paranoid_client, csrf_token):
        """ALL_EMPTY: empty string YAML must be rejected."""
        resp = paranoid_client.post(
            "/api/config",
            json={"yaml": ""},
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code == 400

    def test_whitespace_only_yaml(self, paranoid_client, csrf_token):
        """ALL_EMPTY: whitespace-only YAML."""
        resp = paranoid_client.post(
            "/api/config",
            json={"yaml": "   \n  \n   "},
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code == 400

    def test_empty_models_dict_yaml(self, paranoid_client, csrf_token):
        """TYPE_CONFUSION: models key present but empty dict."""
        resp = paranoid_client.post(
            "/api/config",
            json={"yaml": "models: {}\nroles:\n  author: foo\n"},
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code == 400

    def test_models_null_yaml(self, paranoid_client, csrf_token):
        """TYPE_CONFUSION: models key present but null."""
        resp = paranoid_client.post(
            "/api/config",
            json={"yaml": "models: null\nroles:\n  author: foo\n"},
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code == 400

    def test_models_as_list_yaml(self, paranoid_client, csrf_token):
        """TYPE_CONFUSION: models as list instead of dict."""
        resp = paranoid_client.post(
            "/api/config",
            json={"yaml": "models:\n  - item1\n  - item2\nroles:\n  author: foo\n"},
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code == 400

    def test_missing_roles_key_yaml(self, paranoid_client, csrf_token):
        """PARTIALLY_FILLED: models present but no roles."""
        resp = paranoid_client.post(
            "/api/config",
            json={"yaml": "models:\n  m:\n    provider: openai\n"},
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code == 400

    def test_empty_roles_block_yaml(self, paranoid_client, csrf_token):
        """PARTIALLY_FILLED: roles key present but empty."""
        resp = paranoid_client.post(
            "/api/config",
            json={"yaml": "models:\n  m:\n    provider: openai\nroles: {}\n"},
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code == 400

    def test_preexisting_config_with_garbage_yaml(
        self, paranoid_client, csrf_token, temp_config_dir,
    ):
        """PREEXISTING_WITH_EMPTY_SUBMIT: good config exists, save garbage."""
        config_path = temp_config_dir / "models.yaml"
        before_content = config_path.read_text()

        resp = paranoid_client.post(
            "/api/config",
            json={"yaml": "this is not yaml: [[["},
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code == 400

        # Verify config was NOT overwritten
        assert config_path.read_text() == before_content

    def test_null_yaml_field(self, paranoid_client, csrf_token):
        """TYPE_CONFUSION: yaml field is null instead of string.

        FINDING: yaml.safe_load(None) raises AttributeError inside the handler,
        causing a 500 instead of a clean 400 rejection. The handler does not
        guard against None before passing to yaml.safe_load().
        """
        try:
            resp = paranoid_client.post(
                "/api/config",
                json={"yaml": None},
                headers={"X-DVAD-Token": csrf_token},
            )
            # If we get here, check status
            if resp.status_code == 500:
                pytest.xfail(
                    "FINDING: POST /api/config with yaml=null causes "
                    "AttributeError (yaml.safe_load(None)). Should return 400, got 500."
                )
            assert resp.status_code == 400
        except Exception:
            # TestClient raises the server exception by default
            pytest.xfail(
                "FINDING: POST /api/config with yaml=null causes unhandled "
                "AttributeError (yaml.safe_load(None)). The handler does not "
                "guard against None before passing to yaml.safe_load()."
            )

    def test_no_yaml_key_at_all(self, paranoid_client, csrf_token):
        """TYPE_CONFUSION: JSON body has no 'yaml' key."""
        resp = paranoid_client.post(
            "/api/config",
            json={"content": "models: {}"},
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code == 400


# ═════════════════════════════════════════════════════════════════════════════
# Config Save (Structured Roles) — can clear all role assignments
# ═════════════════════════════════════════════════════════════════════════════


class TestConfigSaveStructuredFuzzing:
    """Fuzz POST /api/config (structured roles path)."""

    def test_empty_roles_and_thinking(self, paranoid_client, csrf_token, temp_config_dir):
        """PREEXISTING_WITH_EMPTY_SUBMIT: send empty roles over existing config.

        This is the exact shape of the most dangerous bug class.
        Existing good role assignments on disk, UI sends {roles: {}, thinking: {}}.
        """
        config_path = temp_config_dir / "models.yaml"
        before_raw = yaml.safe_load(config_path.read_text())
        before_roles = before_raw.get("roles", {})
        had_author = bool(before_roles.get("author"))
        had_reviewers = bool(before_roles.get("reviewers"))

        resp = paranoid_client.post(
            "/api/config",
            json={"roles": {}, "thinking": {}},
            headers={"X-DVAD-Token": csrf_token},
        )

        if resp.status_code == 200:
            # The endpoint accepted empty roles. Verify what happened.
            after_raw = yaml.safe_load(config_path.read_text())
            after_roles = after_raw.get("roles", {})

            # FINDING: if roles were cleared, this is a data loss vector.
            if had_author and not after_roles.get("author"):
                pytest.xfail(
                    "FINDING: Structured save with empty roles cleared the author "
                    "assignment. No server-side rejection of empty roles payload."
                )
            if had_reviewers and not after_roles.get("reviewers"):
                pytest.xfail(
                    "FINDING: Structured save with empty roles cleared reviewer "
                    "assignments. No server-side rejection of empty roles payload."
                )

    def test_all_roles_null(self, paranoid_client, csrf_token):
        """ALL_EMPTY: every role field explicitly null."""
        resp = paranoid_client.post(
            "/api/config",
            json={
                "roles": {
                    "author": None,
                    "reviewer1": None,
                    "reviewer2": None,
                    "dedup": None,
                    "normalization": None,
                    "revision": None,
                    "integration": None,
                },
                "thinking": {},
            },
            headers={"X-DVAD-Token": csrf_token},
        )
        # This may succeed (setting all to null) or fail.
        # If it succeeds, it's a FINDING because it destroys config.
        if resp.status_code == 200:
            pytest.xfail(
                "FINDING: Structured save accepted all-null roles, "
                "potentially clearing the entire configuration."
            )

    def test_thinking_with_unknown_model(self, paranoid_client, csrf_token):
        """TYPE_CONFUSION: thinking key references a model not in config."""
        resp = paranoid_client.post(
            "/api/config",
            json={
                "roles": {"author": "test-model"},
                "thinking": {"nonexistent-model": True},
            },
            headers={"X-DVAD-Token": csrf_token},
        )
        # Should not crash. Unknown models in thinking should be silently ignored.
        # Not a 500.
        assert resp.status_code != 500


# ═════════════════════════════════════════════════════════════════════════════
# Model Timeout — boundary values
# ═════════════════════════════════════════════════════════════════════════════


class TestModelTimeoutFuzzing:
    """Fuzz POST /api/config/model-timeout."""

    @pytest.mark.parametrize("timeout", [
        0,       # BOUNDARY_UNDER (below min of 10)
        1,       # BOUNDARY_UNDER
        9,       # BOUNDARY_UNDER (just below 10)
        10,      # BOUNDARY_MIN
        7200,    # BOUNDARY_MAX
        7201,    # BOUNDARY_OVER
        -1,      # BOUNDARY_UNDER (negative)
        -999,    # BOUNDARY_UNDER (extreme negative)
        999999,  # BOUNDARY_OVER (extreme)
    ])
    def test_timeout_boundary_values(self, paranoid_client, csrf_token, timeout):
        """Boundary values for timeout must be properly validated."""
        resp = paranoid_client.post(
            "/api/config/model-timeout",
            json={"model_name": "test-model", "timeout": timeout},
            headers={"X-DVAD-Token": csrf_token},
        )
        if timeout < 10 or timeout > 7200:
            assert resp.status_code == 400, (
                f"Timeout {timeout} should be rejected (out of range 10-7200), "
                f"got {resp.status_code}"
            )

    @pytest.mark.parametrize("timeout", [
        None, "abc", "", True, False, [], {}, 1.5,
    ])
    def test_timeout_type_confusion(self, paranoid_client, csrf_token, timeout):
        """TYPE_CONFUSION: non-integer timeout values."""
        resp = paranoid_client.post(
            "/api/config/model-timeout",
            json={"model_name": "test-model", "timeout": timeout},
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code == 400, (
            f"Timeout value {timeout!r} (type {type(timeout).__name__}) "
            f"should be rejected, got {resp.status_code}"
        )

    def test_timeout_1_operationally_destructive(self, paranoid_client, csrf_token):
        """BOUNDARY_MIN: timeout=10 is technically valid but operationally risky.

        NOTE: 10 is the minimum. This test documents the boundary.
        Any LLM call with a 10-second timeout will almost certainly fail.
        """
        resp = paranoid_client.post(
            "/api/config/model-timeout",
            json={"model_name": "test-model", "timeout": 10},
            headers={"X-DVAD-Token": csrf_token},
        )
        # Technically valid, so 200 is acceptable
        # This documents that the app allows operationally dangerous values


# ═════════════════════════════════════════════════════════════════════════════
# Model Max Tokens — boundary values
# ═════════════════════════════════════════════════════════════════════════════


class TestModelMaxTokensFuzzing:
    """Fuzz POST /api/config/model-max-tokens."""

    @pytest.mark.parametrize("max_tokens", [
        0,        # BOUNDARY_UNDER
        -1,       # BOUNDARY_UNDER
        1,        # BOUNDARY_MIN (technically valid but operationally destructive)
        1000000,  # BOUNDARY_MAX
        1000001,  # BOUNDARY_OVER
        -999,     # BOUNDARY_UNDER (extreme)
    ])
    def test_max_tokens_boundary_values(self, paranoid_client, csrf_token, max_tokens):
        resp = paranoid_client.post(
            "/api/config/model-max-tokens",
            json={"model_name": "test-model", "max_out_configured": max_tokens},
            headers={"X-DVAD-Token": csrf_token},
        )
        if max_tokens < 1 or max_tokens > 1000000:
            assert resp.status_code == 400, (
                f"max_out_configured={max_tokens} should be rejected, "
                f"got {resp.status_code}"
            )

    def test_max_tokens_null_without_clear_flag(self, paranoid_client, csrf_token):
        """Sending null max_out_configured without clear=true should be rejected."""
        resp = paranoid_client.post(
            "/api/config/model-max-tokens",
            json={"model_name": "test-model", "max_out_configured": None},
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code == 400

    def test_max_tokens_null_with_clear_flag(self, paranoid_client, csrf_token):
        """Sending null max_out_configured with clear=true should clear the field."""
        resp = paranoid_client.post(
            "/api/config/model-max-tokens",
            json={"model_name": "test-model", "max_out_configured": None, "clear": True},
            headers={"X-DVAD-Token": csrf_token},
        )
        # Should succeed (clearing the field)
        assert resp.status_code == 200

    @pytest.mark.parametrize("max_tokens", [
        None, "abc", "", False, [], {},
    ])
    def test_max_tokens_type_confusion(self, paranoid_client, csrf_token, max_tokens):
        """TYPE_CONFUSION: non-integer values (without clear flag)."""
        resp = paranoid_client.post(
            "/api/config/model-max-tokens",
            json={"model_name": "test-model", "max_out_configured": max_tokens},
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code == 400

    def test_max_tokens_bool_true_coercion(self, paranoid_client, csrf_token):
        """TYPE_CONFUSION: True used to coerce to int(True)==1, now rejected.

        Booleans are explicitly rejected before int() coercion to prevent
        silent type confusion (bool is a subclass of int in Python).
        """
        resp = paranoid_client.post(
            "/api/config/model-max-tokens",
            json={"model_name": "test-model", "max_out_configured": True},
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code == 400


# ═════════════════════════════════════════════════════════════════════════════
# Settings Toggle — fuzzing
# ═════════════════════════════════════════════════════════════════════════════


class TestSettingsToggleFuzzing:
    """Fuzz POST /api/config/settings-toggle."""

    @pytest.mark.parametrize("key", [
        "",
        "unknown_key",
        "LIVE_TESTING",     # wrong case
        "live_Testing",     # mixed case
        "admin",
        "debug",
        "models",           # YAML key collision
        "../../../etc/passwd",  # path traversal attempt
    ])
    def test_invalid_keys_rejected(self, paranoid_client, csrf_token, key):
        """Only keys in the valid set should be accepted."""
        resp = paranoid_client.post(
            "/api/config/settings-toggle",
            json={"key": key, "value": True},
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code == 400

    @pytest.mark.parametrize("value", [
        None, "true", "false", 1, 0, "yes", "no", [], {},
    ])
    def test_value_coercion_is_safe(self, paranoid_client, csrf_token, value):
        """Non-boolean values should be safely coerced, not crash."""
        resp = paranoid_client.post(
            "/api/config/settings-toggle",
            json={"key": "live_testing", "value": value},
            headers={"X-DVAD-Token": csrf_token},
        )
        # Should either succeed (coercing to bool) or fail gracefully
        assert resp.status_code != 500


# ═════════════════════════════════════════════════════════════════════════════
# Override — fuzzing
# ═════════════════════════════════════════════════════════════════════════════


class TestOverrideFuzzing:
    """Fuzz POST /api/review/{id}/override."""

    @pytest.mark.parametrize("resolution", [
        "",
        "OVERRIDDEN",    # wrong case
        "accepted",      # not in valid set (overridden, auto_dismissed, escalated)
        "rejected",
        "pending",
        "null",
        None,
    ])
    def test_invalid_resolution_rejected(
        self, paranoid_client, csrf_token, temp_review_env, resolution,
    ):
        _, _, review_id = temp_review_env
        resp = paranoid_client.post(
            f"/api/review/{review_id}/override",
            json={"group_id": "grp-001", "resolution": resolution},
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code == 400

    def test_empty_group_id(self, paranoid_client, csrf_token, temp_review_env):
        _, _, review_id = temp_review_env
        resp = paranoid_client.post(
            f"/api/review/{review_id}/override",
            json={"group_id": "", "resolution": "overridden"},
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code == 400

    def test_nonexistent_group_id(self, paranoid_client, csrf_token, temp_review_env):
        _, _, review_id = temp_review_env
        resp = paranoid_client.post(
            f"/api/review/{review_id}/override",
            json={"group_id": "grp-DOES-NOT-EXIST", "resolution": "overridden"},
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code == 400

    def test_preexisting_data_override_idempotency(
        self, paranoid_client, csrf_token, temp_review_env,
    ):
        """PREEXISTING_WITH_EMPTY_SUBMIT: override same point twice.

        Second override should still succeed and create a second overrides entry.
        """
        _, review_dir, review_id = temp_review_env

        # First override
        resp1 = paranoid_client.post(
            f"/api/review/{review_id}/override",
            json={"group_id": "grp-001", "resolution": "overridden"},
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp1.status_code == 200

        # Second override on same group
        resp2 = paranoid_client.post(
            f"/api/review/{review_id}/override",
            json={"group_id": "grp-001", "resolution": "auto_dismissed"},
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp2.status_code == 200

        # Verify both overrides recorded
        ledger = json.loads((review_dir / "review-ledger.json").read_text())
        pt = ledger["points"][0]
        assert len(pt["overrides"]) == 2
        assert pt["final_resolution"] == "auto_dismissed"


# ═════════════════════════════════════════════════════════════════════════════
# Env Var (Single PUT) — fuzzing
# ═════════════════════════════════════════════════════════════════════════════


class TestEnvVarPutFuzzing:
    """Fuzz PUT /api/config/env/{name}."""

    def test_empty_value_rejected(self, paranoid_client, csrf_token):
        resp = paranoid_client.put(
            "/api/config/env/TEST_KEY",
            json={"value": ""},
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code == 400

    def test_whitespace_only_value_rejected(self, paranoid_client, csrf_token):
        resp = paranoid_client.put(
            "/api/config/env/TEST_KEY",
            json={"value": "   "},
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code == 400

    def test_value_with_newlines_rejected(self, paranoid_client, csrf_token):
        """Newlines in env var values would corrupt the .env file format."""
        resp = paranoid_client.put(
            "/api/config/env/TEST_KEY",
            json={"value": "sk-test\nINJECTED_VAR=evil"},
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code == 400

    def test_value_with_null_bytes_rejected(self, paranoid_client, csrf_token):
        resp = paranoid_client.put(
            "/api/config/env/TEST_KEY",
            json={"value": "sk-test\x00evil"},
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code == 400

    def test_value_exceeding_length_limit_rejected(self, paranoid_client, csrf_token):
        resp = paranoid_client.put(
            "/api/config/env/TEST_KEY",
            json={"value": "x" * 4097},
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code == 400

    def test_unknown_env_name_rejected(self, paranoid_client, csrf_token):
        resp = paranoid_client.put(
            "/api/config/env/UNKNOWN_VAR_NOT_IN_CONFIG",
            json={"value": "sk-test"},
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code == 400

    @pytest.mark.parametrize("env_name", [
        "lowercase_var",
        "123_STARTS_WITH_NUMBER",
        "HAS SPACE",
        "HAS-DASH",
    ])
    def test_invalid_env_name_format_rejected(self, paranoid_client, csrf_token, env_name):
        """Env var names must match ^[A-Z_][A-Z0-9_]*$."""
        resp = paranoid_client.put(
            f"/api/config/env/{env_name}",
            json={"value": "sk-test"},
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code == 400 or resp.status_code == 422

    def test_empty_env_name_rejected(self, paranoid_client, csrf_token):
        """Empty env name results in PUT /api/config/env/ which hits batch POST route (405)."""
        resp = paranoid_client.put(
            "/api/config/env/",
            json={"value": "sk-test"},
            headers={"X-DVAD-Token": csrf_token},
        )
        # 405 Method Not Allowed is acceptable (PUT on the batch POST endpoint)
        assert resp.status_code in (400, 405, 422)


# ═════════════════════════════════════════════════════════════════════════════
# Env Var (Batch POST) — fuzzing
# ═════════════════════════════════════════════════════════════════════════════


class TestEnvVarBatchFuzzing:
    """Fuzz POST /api/config/env."""

    def test_empty_env_vars_dict(self, paranoid_client, csrf_token):
        """Empty dict should be rejected."""
        resp = paranoid_client.post(
            "/api/config/env",
            json={"env_vars": {}},
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code == 400

    def test_no_env_vars_key(self, paranoid_client, csrf_token):
        """Missing env_vars key entirely."""
        resp = paranoid_client.post(
            "/api/config/env",
            json={},
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code == 400

    def test_batch_with_empty_values_requires_confirm(self, paranoid_client, csrf_token):
        """Empty values (which DELETE keys) require X-Confirm-Destructive header."""
        resp = paranoid_client.post(
            "/api/config/env",
            json={"env_vars": {"TEST_KEY": ""}},
            headers={"X-DVAD-Token": csrf_token},
            # Intentionally omit X-Confirm-Destructive
        )
        assert resp.status_code == 400
        assert "destructive" in resp.json().get("detail", "").lower()

    def test_batch_with_unknown_key(self, paranoid_client, csrf_token):
        resp = paranoid_client.post(
            "/api/config/env",
            json={"env_vars": {"TOTALLY_UNKNOWN": "value"}},
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code == 400

    def test_batch_newline_injection(self, paranoid_client, csrf_token):
        resp = paranoid_client.post(
            "/api/config/env",
            json={"env_vars": {"TEST_KEY": "value\nINJECTED=evil"}},
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code == 400


# ═════════════════════════════════════════════════════════════════════════════
# Review Start — fuzzing
# ═════════════════════════════════════════════════════════════════════════════


class TestReviewStartFuzzing:
    """Fuzz POST /api/review/start."""

    def test_all_empty_form(self, paranoid_client, csrf_token):
        """ALL_EMPTY: completely empty form submission."""
        resp = paranoid_client.post(
            "/api/review/start",
            data={"mode": "", "project": ""},
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code == 400

    def test_missing_project(self, paranoid_client, csrf_token):
        resp = paranoid_client.post(
            "/api/review/start",
            data={"mode": "plan", "project": ""},
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code == 400

    def test_whitespace_project(self, paranoid_client, csrf_token):
        resp = paranoid_client.post(
            "/api/review/start",
            data={"mode": "plan", "project": "   "},
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code == 400

    @pytest.mark.parametrize("mode", [
        "", "PLAN", "Plan", "exec", "shell", "admin",
        "../../../", "plan; rm -rf /",
    ])
    def test_invalid_mode_rejected(self, paranoid_client, csrf_token, mode):
        resp = paranoid_client.post(
            "/api/review/start",
            data={"mode": mode, "project": "test"},
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code == 400

    def test_invalid_input_paths_json(self, paranoid_client, csrf_token):
        resp = paranoid_client.post(
            "/api/review/start",
            data={
                "mode": "plan",
                "project": "test",
                "input_paths": "{not valid json",
            },
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code == 400

    def test_invalid_reference_paths_json(self, paranoid_client, csrf_token):
        """Invalid JSON in reference_paths must be rejected."""
        resp = paranoid_client.post(
            "/api/review/start",
            data={
                "mode": "plan",
                "project": "test",
                "input_paths": '["dummy"]',
                "reference_paths": "{invalid json",
            },
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code == 400

    def test_nonexistent_input_path_rejected(self, paranoid_client, csrf_token):
        resp = paranoid_client.post(
            "/api/review/start",
            data={
                "mode": "plan",
                "project": "test",
                "input_paths": json.dumps(["/nonexistent/path/file.md"]),
            },
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code == 400

    def test_directory_as_input_path_rejected(self, paranoid_client, csrf_token):
        """Passing a directory where a file is expected."""
        resp = paranoid_client.post(
            "/api/review/start",
            data={
                "mode": "plan",
                "project": "test",
                "input_paths": json.dumps(["/tmp"]),
            },
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code == 400

    def test_max_cost_type_confusion(self, paranoid_client, csrf_token):
        resp = paranoid_client.post(
            "/api/review/start",
            data={
                "mode": "plan",
                "project": "test",
                "max_cost": "not_a_number",
            },
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code == 400


# ═════════════════════════════════════════════════════════════════════════════
# Filesystem Browser — path traversal and boundary testing
# ═════════════════════════════════════════════════════════════════════════════


class TestFilesystemBrowserFuzzing:
    """Fuzz GET /api/fs/ls for path traversal and edge cases."""

    def test_nonexistent_path(self, paranoid_client):
        resp = paranoid_client.get(
            "/api/fs/ls",
            params={"dir": "/nonexistent/path/that/does/not/exist"},
        )
        assert resp.status_code == 400

    def test_file_not_directory(self, paranoid_client):
        """Passing a file path instead of directory."""
        # /etc/hostname is a file on most Linux systems
        resp = paranoid_client.get(
            "/api/fs/ls",
            params={"dir": "/etc/hostname"},
        )
        assert resp.status_code == 400

    def test_root_directory_works(self, paranoid_client):
        """Root directory should be listable."""
        resp = paranoid_client.get("/api/fs/ls", params={"dir": "/"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["current_dir"] == "/"
        assert data["parent_dir"] is None  # root has no parent

    def test_tilde_resolves_to_home(self, paranoid_client):
        """~ should resolve to the user's home directory."""
        resp = paranoid_client.get("/api/fs/ls", params={"dir": "~"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["current_dir"] == str(Path.home())

    def test_symlink_resolution(self, paranoid_client):
        """Path with .. components should be resolved."""
        resp = paranoid_client.get(
            "/api/fs/ls",
            params={"dir": "/tmp/../tmp"},
        )
        assert resp.status_code == 200
        data = resp.json()
        # Should resolve to /tmp
        assert data["current_dir"] == "/tmp"

    def test_empty_dir_param(self, paranoid_client):
        """Empty dir param should be handled gracefully."""
        resp = paranoid_client.get("/api/fs/ls", params={"dir": ""})
        # Should either default to home or return an error
        assert resp.status_code in (200, 400)
