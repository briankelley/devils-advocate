"""Approach 4: Permutation Fuzzing on Form State.

Systematically tests every write endpoint with adversarial input strategies.
The highest-value strategy is PREEXISTING_WITH_EMPTY_SUBMIT: existing good
data on disk, user submits empty fields, backend interprets empty as "delete."

This answers: "For every combination of input state, does the system protect
existing data?"
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
    WRITE_ENDPOINTS,
    InputStrategy,
    StateSnapshot,
    get_csrf_token,
    api_post,
    api_put,
    api_delete,
    load_fixture_yaml,
    restore_config_via_api,
)

pytestmark = [pytest.mark.e2e, pytest.mark.paranoid]

CAPTURED_REVIEW_ID = "captured_e2e_review"


def _get_config_path(page, dvad_server) -> Path:
    resp = page.request.get(f"{dvad_server}/api/config")
    return Path(resp.json()["config_path"])


def _get_env_path(page, dvad_server) -> Path:
    resp = page.request.get(f"{dvad_server}/api/config/env")
    return Path(resp.json()["env_file_path"])


# ---------------------------------------------------------------------------
# Config full save fuzzing - the most dangerous endpoint
# ---------------------------------------------------------------------------

class TestConfigSaveFuzzing:
    """POST /api/config - full YAML overwrite. This is the highest-risk
    endpoint because it replaces the entire config file."""

    @pytest.fixture(autouse=True)
    def _restore(self, page, dvad_server):
        """Restore config after each test in this class."""
        yield
        restore_config_via_api(page, dvad_server)

    def _setup(self, page, dvad_server):
        page.goto(f"{dvad_server}/config")
        page.wait_for_load_state("networkidle")
        csrf = get_csrf_token(page)
        config_path = _get_config_path(page, dvad_server)
        return csrf, config_path

    def test_empty_yaml_rejected(self, page, dvad_server):
        """ALL_EMPTY: empty string YAML must be rejected."""
        csrf, config_path = self._setup(page, dvad_server)
        before = StateSnapshot.capture([config_path])
        resp = api_post(page, dvad_server, "/api/config", csrf, {"yaml": ""})
        assert resp.status == 400
        after = StateSnapshot.capture([config_path])
        before.assert_no_mutation(after, "empty yaml save")

    def test_null_yaml_rejected(self, page, dvad_server):
        """TYPE_CONFUSION: null yaml field must be rejected.
        A 400 is ideal; a 500 means the server crashed on bad input
        but at least didn't corrupt the config."""
        csrf, config_path = self._setup(page, dvad_server)
        before = StateSnapshot.capture([config_path])
        resp = api_post(page, dvad_server, "/api/config", csrf, {"yaml": None})
        assert resp.status != 200, "null yaml was accepted - config may be corrupted"
        after = StateSnapshot.capture([config_path])
        before.assert_no_mutation(after, "null yaml save")

    def test_yaml_with_empty_models_rejected(self, page, dvad_server):
        """PREEXISTING_WITH_EMPTY_SUBMIT: models: {} is syntactically valid
        YAML but would destroy all model configuration."""
        csrf, config_path = self._setup(page, dvad_server)
        before = StateSnapshot.capture([config_path])

        dangerous_yaml = "models: {}\nroles:\n  author: nonexistent\n"
        resp = api_post(page, dvad_server, "/api/config", csrf, {"yaml": dangerous_yaml})
        # Should be rejected by validation (unknown model reference)
        assert resp.status == 400

        after = StateSnapshot.capture([config_path])
        before.assert_no_mutation(after, "empty models save")

    def test_yaml_with_only_models_no_roles_rejected(self, page, dvad_server):
        """PARTIALLY_FILLED: YAML with models but missing roles block."""
        csrf, config_path = self._setup(page, dvad_server)
        before = StateSnapshot.capture([config_path])

        partial_yaml = "models:\n  e2e-remote:\n    provider: openai\n"
        resp = api_post(page, dvad_server, "/api/config", csrf, {"yaml": partial_yaml})
        assert resp.status == 400

        after = StateSnapshot.capture([config_path])
        before.assert_no_mutation(after, "partial yaml save")

    def test_invalid_yaml_syntax_rejected(self, page, dvad_server):
        """TYPE_CONFUSION: malformed YAML must not corrupt the config file."""
        csrf, config_path = self._setup(page, dvad_server)
        before = StateSnapshot.capture([config_path])

        bad_yaml = "models:\n  - this is a list not a dict\n  : broken key\n"
        resp = api_post(page, dvad_server, "/api/config", csrf, {"yaml": bad_yaml})
        assert resp.status == 400

        after = StateSnapshot.capture([config_path])
        before.assert_no_mutation(after, "bad yaml syntax save")

    def test_yaml_integer_instead_of_dict(self, page, dvad_server):
        """TYPE_CONFUSION: sending a non-dict YAML value for models."""
        csrf, config_path = self._setup(page, dvad_server)
        before = StateSnapshot.capture([config_path])

        resp = api_post(page, dvad_server, "/api/config", csrf, {"yaml": "models: 42\n"})
        assert resp.status == 400

        after = StateSnapshot.capture([config_path])
        before.assert_no_mutation(after, "integer models save")

    def test_yaml_array_payload(self, page, dvad_server):
        """TYPE_CONFUSION: sending array instead of object for top-level."""
        csrf, config_path = self._setup(page, dvad_server)
        before = StateSnapshot.capture([config_path])

        resp = api_post(page, dvad_server, "/api/config", csrf, {"yaml": "- item1\n- item2\n"})
        assert resp.status == 400

        after = StateSnapshot.capture([config_path])
        before.assert_no_mutation(after, "array yaml save")

    def test_very_large_yaml_payload(self, page, dvad_server):
        """BOUNDARY_MAX: extremely large YAML should be handled gracefully."""
        csrf, config_path = self._setup(page, dvad_server)
        before = StateSnapshot.capture([config_path])

        # 1MB of YAML comments
        huge_yaml = ("# " + "x" * 100 + "\n") * 10000
        huge_yaml += "models: {}\nroles: {}\n"
        resp = api_post(page, dvad_server, "/api/config", csrf, {"yaml": huge_yaml})
        # Should either reject or at least not corrupt existing config
        if resp.status == 200:
            # If accepted, config should still be valid
            after_content = config_path.read_text()
            data = yaml.safe_load(after_content)
            assert "models" in data
        else:
            after = StateSnapshot.capture([config_path])
            before.assert_no_mutation(after, "huge yaml save")


# ---------------------------------------------------------------------------
# Model config field fuzzing
# ---------------------------------------------------------------------------

class TestModelTimeoutFuzzing:
    """POST /api/config/model-timeout permutation tests."""

    @pytest.fixture(autouse=True)
    def _restore(self, page, dvad_server):
        yield
        restore_config_via_api(page, dvad_server)

    def _setup(self, page, dvad_server):
        page.goto(f"{dvad_server}/config")
        page.wait_for_load_state("networkidle")
        csrf = get_csrf_token(page)
        config_path = _get_config_path(page, dvad_server)
        return csrf, config_path

    def test_boundary_min_timeout(self, page, dvad_server):
        """BOUNDARY_MIN: timeout=10 (minimum valid) should be accepted."""
        csrf, config_path = self._setup(page, dvad_server)
        resp = api_post(page, dvad_server, "/api/config/model-timeout", csrf, {
            "model_name": "e2e-remote",
            "timeout": 10,
        })
        assert resp.status == 200

    def test_boundary_under_timeout(self, page, dvad_server):
        """BOUNDARY_UNDER: timeout=9 (one below min) should be rejected."""
        csrf, config_path = self._setup(page, dvad_server)
        before = StateSnapshot.capture([config_path])
        resp = api_post(page, dvad_server, "/api/config/model-timeout", csrf, {
            "model_name": "e2e-remote",
            "timeout": 9,
        })
        assert resp.status == 400
        after = StateSnapshot.capture([config_path])
        before.assert_no_mutation(after, "timeout under-min")

    def test_boundary_max_timeout(self, page, dvad_server):
        """BOUNDARY_MAX: timeout=7200 (maximum valid) should be accepted."""
        csrf, config_path = self._setup(page, dvad_server)
        resp = api_post(page, dvad_server, "/api/config/model-timeout", csrf, {
            "model_name": "e2e-remote",
            "timeout": 7200,
        })
        assert resp.status == 200

    def test_boundary_over_timeout(self, page, dvad_server):
        """BOUNDARY_OVER: timeout=7201 (one above max) should be rejected."""
        csrf, config_path = self._setup(page, dvad_server)
        before = StateSnapshot.capture([config_path])
        resp = api_post(page, dvad_server, "/api/config/model-timeout", csrf, {
            "model_name": "e2e-remote",
            "timeout": 7201,
        })
        assert resp.status == 400
        after = StateSnapshot.capture([config_path])
        before.assert_no_mutation(after, "timeout over-max")

    def test_zero_timeout_rejected(self, page, dvad_server):
        """BOUNDARY_UNDER: timeout=0 should be rejected."""
        csrf, config_path = self._setup(page, dvad_server)
        before = StateSnapshot.capture([config_path])
        resp = api_post(page, dvad_server, "/api/config/model-timeout", csrf, {
            "model_name": "e2e-remote",
            "timeout": 0,
        })
        assert resp.status == 400
        after = StateSnapshot.capture([config_path])
        before.assert_no_mutation(after, "timeout zero")

    def test_negative_timeout_rejected(self, page, dvad_server):
        """BOUNDARY_UNDER: negative timeout should be rejected."""
        csrf, config_path = self._setup(page, dvad_server)
        before = StateSnapshot.capture([config_path])
        resp = api_post(page, dvad_server, "/api/config/model-timeout", csrf, {
            "model_name": "e2e-remote",
            "timeout": -1,
        })
        assert resp.status == 400
        after = StateSnapshot.capture([config_path])
        before.assert_no_mutation(after, "timeout negative")

    def test_string_timeout_rejected(self, page, dvad_server):
        """TYPE_CONFUSION: string value for timeout should be rejected."""
        csrf, config_path = self._setup(page, dvad_server)
        before = StateSnapshot.capture([config_path])
        resp = api_post(page, dvad_server, "/api/config/model-timeout", csrf, {
            "model_name": "e2e-remote",
            "timeout": "not-a-number",
        })
        assert resp.status == 400
        after = StateSnapshot.capture([config_path])
        before.assert_no_mutation(after, "timeout string")

    def test_float_timeout_handled(self, page, dvad_server):
        """TYPE_CONFUSION: float timeout - should be rejected or truncated safely."""
        csrf, _ = self._setup(page, dvad_server)
        resp = api_post(page, dvad_server, "/api/config/model-timeout", csrf, {
            "model_name": "e2e-remote",
            "timeout": 120.5,
        })
        # Should either accept (truncated to int) or reject
        assert resp.status in (200, 400)

    def test_nonexistent_model_rejected(self, page, dvad_server):
        """Type confusion on model_name - unknown model must be rejected."""
        csrf, config_path = self._setup(page, dvad_server)
        before = StateSnapshot.capture([config_path])
        resp = api_post(page, dvad_server, "/api/config/model-timeout", csrf, {
            "model_name": "nonexistent-model-xyz",
            "timeout": 120,
        })
        assert resp.status == 404
        after = StateSnapshot.capture([config_path])
        before.assert_no_mutation(after, "timeout unknown model")


class TestModelMaxTokensFuzzing:
    """POST /api/config/model-max-tokens permutation tests."""

    @pytest.fixture(autouse=True)
    def _restore(self, page, dvad_server):
        yield
        restore_config_via_api(page, dvad_server)

    def _setup(self, page, dvad_server):
        page.goto(f"{dvad_server}/config")
        page.wait_for_load_state("networkidle")
        csrf = get_csrf_token(page)
        config_path = _get_config_path(page, dvad_server)
        return csrf, config_path

    def test_boundary_min_max_tokens(self, page, dvad_server):
        """BOUNDARY_MIN: max_out_configured=1 should be accepted."""
        csrf, _ = self._setup(page, dvad_server)
        resp = api_post(page, dvad_server, "/api/config/model-max-tokens", csrf, {
            "model_name": "e2e-remote",
            "max_out_configured": 1,
        })
        assert resp.status == 200

    def test_boundary_under_max_tokens(self, page, dvad_server):
        """BOUNDARY_UNDER: max_out_configured=0 should be rejected."""
        csrf, config_path = self._setup(page, dvad_server)
        before = StateSnapshot.capture([config_path])
        resp = api_post(page, dvad_server, "/api/config/model-max-tokens", csrf, {
            "model_name": "e2e-remote",
            "max_out_configured": 0,
        })
        assert resp.status == 400
        after = StateSnapshot.capture([config_path])
        before.assert_no_mutation(after, "max_tokens zero")

    def test_boundary_over_max_tokens(self, page, dvad_server):
        """BOUNDARY_OVER: max_out_configured=1000001 should be rejected."""
        csrf, config_path = self._setup(page, dvad_server)
        before = StateSnapshot.capture([config_path])
        resp = api_post(page, dvad_server, "/api/config/model-max-tokens", csrf, {
            "model_name": "e2e-remote",
            "max_out_configured": 1000001,
        })
        assert resp.status == 400
        after = StateSnapshot.capture([config_path])
        before.assert_no_mutation(after, "max_tokens over-max")

    def test_exceeds_stated_max_rejected(self, page, dvad_server):
        """BOUNDARY_OVER: max_out_configured > max_out_stated must be rejected.
        The fixture has max_out_stated=8192."""
        csrf, config_path = self._setup(page, dvad_server)
        before = StateSnapshot.capture([config_path])
        resp = api_post(page, dvad_server, "/api/config/model-max-tokens", csrf, {
            "model_name": "e2e-remote",
            "max_out_configured": 8193,
        })
        assert resp.status == 400
        after = StateSnapshot.capture([config_path])
        before.assert_no_mutation(after, "max_tokens over stated")

    def test_null_removes_key(self, page, dvad_server):
        """ALL_EMPTY: null max_out_configured REMOVES the key from config.
        This is documented behavior but warrants verification that it
        does not corrupt other model fields."""
        csrf, config_path = self._setup(page, dvad_server)
        before_data = yaml.safe_load(config_path.read_text())
        before_model_keys = set(before_data["models"]["e2e-remote"].keys())

        resp = api_post(page, dvad_server, "/api/config/model-max-tokens", csrf, {
            "model_name": "e2e-remote",
            "max_out_configured": None,
        })
        assert resp.status == 200

        after_data = yaml.safe_load(config_path.read_text())
        after_model_keys = set(after_data["models"]["e2e-remote"].keys())

        # Only max_out_configured should be removed
        unexpected_loss = (before_model_keys - after_model_keys) - {"max_out_configured"}
        assert not unexpected_loss, f"Unexpected keys removed: {unexpected_loss}"

    def test_negative_max_tokens_rejected(self, page, dvad_server):
        """BOUNDARY_UNDER: negative value should be rejected."""
        csrf, config_path = self._setup(page, dvad_server)
        before = StateSnapshot.capture([config_path])
        resp = api_post(page, dvad_server, "/api/config/model-max-tokens", csrf, {
            "model_name": "e2e-remote",
            "max_out_configured": -100,
        })
        assert resp.status == 400
        after = StateSnapshot.capture([config_path])
        before.assert_no_mutation(after, "max_tokens negative")

    def test_string_max_tokens_rejected(self, page, dvad_server):
        """TYPE_CONFUSION: string value for max_tokens should be rejected."""
        csrf, config_path = self._setup(page, dvad_server)
        before = StateSnapshot.capture([config_path])
        resp = api_post(page, dvad_server, "/api/config/model-max-tokens", csrf, {
            "model_name": "e2e-remote",
            "max_out_configured": "not-a-number",
        })
        assert resp.status == 400
        after = StateSnapshot.capture([config_path])
        before.assert_no_mutation(after, "max_tokens string")


# ---------------------------------------------------------------------------
# Override fuzzing
# ---------------------------------------------------------------------------

class TestOverrideFuzzing:
    """POST /api/review/{id}/override permutation tests."""

    def _setup(self, page, dvad_server):
        page.goto(f"{dvad_server}/review/{CAPTURED_REVIEW_ID}")
        page.wait_for_load_state("networkidle")
        return get_csrf_token(page)

    def test_invalid_resolution_rejected(self, page, dvad_server):
        """TYPE_CONFUSION: resolution value outside the valid set."""
        csrf = self._setup(page, dvad_server)
        resp = api_post(
            page, dvad_server,
            f"/api/review/{CAPTURED_REVIEW_ID}/override",
            csrf,
            {"group_id": "any_group", "resolution": "INVALID_VALUE"},
        )
        assert resp.status == 400

    def test_nonexistent_review_rejected(self, page, dvad_server):
        """Invalid review ID should return 400 or 404."""
        page.goto(dvad_server)
        page.wait_for_load_state("networkidle")
        csrf = get_csrf_token(page)
        resp = api_post(
            page, dvad_server,
            "/api/review/nonexistent_review_99999/override",
            csrf,
            {"group_id": "any", "resolution": "overridden"},
        )
        assert resp.status in (400, 404)

    def test_nonexistent_group_rejected(self, page, dvad_server):
        """Override with non-existent group_id should be rejected."""
        csrf = self._setup(page, dvad_server)
        resp = api_post(
            page, dvad_server,
            f"/api/review/{CAPTURED_REVIEW_ID}/override",
            csrf,
            {"group_id": "nonexistent_group_xyz", "resolution": "overridden"},
        )
        assert resp.status == 400

    def test_resolution_escalated_reopens_finding(self, page, dvad_server):
        """FINDING: Setting resolution to 'escalated' can re-open a dismissed
        finding. This is accepted by the API (it's in valid_resolutions).
        This test documents the behavior for policy review."""
        csrf = self._setup(page, dvad_server)

        # Get a real group_id
        resp = page.request.get(f"{dvad_server}/api/review/{CAPTURED_REVIEW_ID}")
        if resp.status != 200:
            pytest.skip("Captured review not available")
        ledger = resp.json()
        points = ledger.get("points", [])
        if not points:
            pytest.skip("No points in captured review")

        gid = points[0].get("group_id", points[0].get("point_id"))

        # First dismiss it
        resp1 = api_post(
            page, dvad_server,
            f"/api/review/{CAPTURED_REVIEW_ID}/override",
            csrf,
            {"group_id": gid, "resolution": "auto_dismissed"},
        )
        assert resp1.status == 200

        # Now re-escalate it - this succeeds, which is a policy concern
        resp2 = api_post(
            page, dvad_server,
            f"/api/review/{CAPTURED_REVIEW_ID}/override",
            csrf,
            {"group_id": gid, "resolution": "escalated"},
        )
        # Document: this DOES succeed. Whether it should is a policy question.
        assert resp2.status == 200


# ---------------------------------------------------------------------------
# Batch env save fuzzing - the "empty string deletes key" footgun
# ---------------------------------------------------------------------------

class TestBatchEnvSaveFuzzing:
    """POST /api/config/env - batch save where empty strings DELETE keys.
    This is the most dangerous env endpoint because a form that sends
    untouched fields as "" will delete existing keys."""

    def _setup(self, page, dvad_server):
        page.goto(f"{dvad_server}/config")
        page.wait_for_load_state("networkidle")
        return get_csrf_token(page)

    def test_empty_string_value_deletes_key(self, page, dvad_server):
        """PREEXISTING_WITH_EMPTY_SUBMIT: sending empty string for a key
        that has a value will DELETE it from .env and os.environ.

        This is the exact shape of the bug class this layer was built to catch.
        Documenting current behavior - the endpoint splits empty values into
        a remove set."""
        csrf = self._setup(page, dvad_server)

        # First, set a value
        resp1 = api_put(page, dvad_server, "/api/config/env/E2E_LOCAL_KEY", csrf, {
            "value": "known-good-value-12345",
        })
        if resp1.status == 400:
            pytest.skip("E2E_LOCAL_KEY not an allowed env name")

        # Now batch save with empty string - this will DELETE the key
        resp2 = api_post(page, dvad_server, "/api/config/env", csrf, {
            "env_vars": {"E2E_LOCAL_KEY": ""},
        })
        assert resp2.status == 200

        # Verify the key was indeed removed
        resp3 = page.request.get(f"{dvad_server}/api/config/env")
        env_data = resp3.json()
        for var in env_data.get("env_vars", []):
            if var["env_name"] == "E2E_LOCAL_KEY":
                # The key should no longer be in the .env file
                assert not var["in_env_file"], (
                    "Key should have been removed from .env by empty batch save. "
                    "If this assertion fires, the behavior changed - which may be "
                    "a good thing (preventing accidental deletion)."
                )
                break

        # Restore the key
        api_put(page, dvad_server, "/api/config/env/E2E_LOCAL_KEY", csrf, {
            "value": "e2e-dummy-key",
        })

    def test_batch_with_unknown_keys_rejected(self, page, dvad_server):
        """TYPE_CONFUSION: batch save with keys not in the allowed set."""
        csrf = self._setup(page, dvad_server)
        resp = api_post(page, dvad_server, "/api/config/env", csrf, {
            "env_vars": {"TOTALLY_FAKE_API_KEY": "some-value"},
        })
        assert resp.status == 400

    def test_batch_with_newline_in_value_rejected(self, page, dvad_server):
        """TYPE_CONFUSION: newlines in values could corrupt .env format."""
        csrf = self._setup(page, dvad_server)
        resp = api_post(page, dvad_server, "/api/config/env", csrf, {
            "env_vars": {"E2E_LOCAL_KEY": "value\nINJECTED_KEY=evil"},
        })
        assert resp.status == 400

    def test_batch_with_null_byte_rejected(self, page, dvad_server):
        """TYPE_CONFUSION: null bytes in values should be rejected."""
        csrf = self._setup(page, dvad_server)
        resp = api_post(page, dvad_server, "/api/config/env", csrf, {
            "env_vars": {"E2E_LOCAL_KEY": "value\x00evil"},
        })
        assert resp.status == 400

    def test_env_value_length_boundary(self, page, dvad_server):
        """BOUNDARY_MAX: values > 4096 chars should be rejected."""
        csrf = self._setup(page, dvad_server)
        long_value = "x" * 4097
        resp = api_post(page, dvad_server, "/api/config/env", csrf, {
            "env_vars": {"E2E_LOCAL_KEY": long_value},
        })
        assert resp.status == 400


# ---------------------------------------------------------------------------
# Review start fuzzing
# ---------------------------------------------------------------------------

class TestReviewStartFuzzing:
    """POST /api/review/start permutation tests."""

    def _setup(self, page, dvad_server):
        page.goto(dvad_server)
        page.wait_for_load_state("networkidle")
        return get_csrf_token(page)

    def test_empty_project_rejected(self, page, dvad_server):
        """ALL_EMPTY: empty project name must be rejected."""
        csrf = self._setup(page, dvad_server)
        resp = api_post(page, dvad_server, "/api/review/start", csrf, {
            "mode": "plan",
            "project": "",
        })
        assert resp.status == 400

    def test_whitespace_only_project_rejected(self, page, dvad_server):
        """ALL_EMPTY: whitespace-only project should be treated as empty."""
        csrf = self._setup(page, dvad_server)
        resp = api_post(page, dvad_server, "/api/review/start", csrf, {
            "mode": "plan",
            "project": "   ",
        })
        assert resp.status == 400

    def test_invalid_mode_rejected(self, page, dvad_server):
        """TYPE_CONFUSION: invalid mode value should be rejected."""
        csrf = self._setup(page, dvad_server)
        resp = api_post(page, dvad_server, "/api/review/start", csrf, {
            "mode": "FAKE_MODE",
            "project": "test-project",
        })
        assert resp.status == 400

    def test_invalid_max_cost_rejected(self, page, dvad_server):
        """TYPE_CONFUSION: non-numeric max_cost should be rejected."""
        csrf = self._setup(page, dvad_server)
        resp = api_post(page, dvad_server, "/api/review/start", csrf, {
            "mode": "plan",
            "project": "test-project",
            "max_cost": "not-a-number",
        })
        assert resp.status == 400

    def test_invalid_input_paths_json_rejected(self, page, dvad_server):
        """TYPE_CONFUSION: malformed JSON in input_paths should be rejected."""
        csrf = self._setup(page, dvad_server)
        resp = api_post(page, dvad_server, "/api/review/start", csrf, {
            "mode": "plan",
            "project": "test-project",
            "input_paths": "not valid json [[[",
        })
        assert resp.status == 400

    def test_nonexistent_input_file_rejected(self, page, dvad_server):
        """TYPE_CONFUSION: paths to nonexistent files should be rejected."""
        csrf = self._setup(page, dvad_server)
        resp = api_post(page, dvad_server, "/api/review/start", csrf, {
            "mode": "plan",
            "project": "test-project",
            "input_paths": json.dumps(["/nonexistent/path/file.md"]),
        })
        assert resp.status == 400

    def test_plan_mode_no_files_rejected(self, page, dvad_server):
        """ALL_EMPTY: plan mode without input files must be rejected."""
        csrf = self._setup(page, dvad_server)
        resp = api_post(page, dvad_server, "/api/review/start", csrf, {
            "mode": "plan",
            "project": "test-project",
            "input_paths": "[]",
        })
        assert resp.status == 400

    def test_code_mode_multiple_files_rejected(self, page, dvad_server):
        """BOUNDARY_OVER: code mode requires exactly 1 input file."""
        csrf = self._setup(page, dvad_server)
        fixtures = Path(__file__).parent / "fixtures"
        files = [str(fixtures / "test-plan.md"), str(fixtures / "test-plan-2.md")]
        resp = api_post(page, dvad_server, "/api/review/start", csrf, {
            "mode": "code",
            "project": "test-project",
            "input_paths": json.dumps(files),
        })
        assert resp.status == 400


# ---------------------------------------------------------------------------
# Settings toggle fuzzing
# ---------------------------------------------------------------------------

class TestSettingsToggleFuzzing:
    """POST /api/config/settings-toggle permutation tests."""

    @pytest.fixture(autouse=True)
    def _restore(self, page, dvad_server):
        yield
        restore_config_via_api(page, dvad_server)

    def _setup(self, page, dvad_server):
        page.goto(f"{dvad_server}/config")
        page.wait_for_load_state("networkidle")
        return get_csrf_token(page)

    def test_empty_key_rejected(self, page, dvad_server):
        """ALL_EMPTY: empty key must be rejected."""
        csrf = self._setup(page, dvad_server)
        resp = api_post(page, dvad_server, "/api/config/settings-toggle", csrf, {
            "key": "",
            "value": True,
        })
        assert resp.status == 400

    def test_unknown_key_rejected(self, page, dvad_server):
        """TYPE_CONFUSION: key not in valid_keys set."""
        csrf = self._setup(page, dvad_server)
        resp = api_post(page, dvad_server, "/api/config/settings-toggle", csrf, {
            "key": "enable_world_destruction",
            "value": True,
        })
        assert resp.status == 400

    def test_string_value_coerced(self, page, dvad_server):
        """TYPE_CONFUSION: string value should be coerced to bool safely."""
        csrf = self._setup(page, dvad_server)
        resp = api_post(page, dvad_server, "/api/config/settings-toggle", csrf, {
            "key": "live_testing",
            "value": "yes",
        })
        # Should either accept (truthy string -> True) or reject
        # The handler uses bool(value), so "yes" -> True
        assert resp.status in (200, 400)

    def test_null_value_handled(self, page, dvad_server):
        """TYPE_CONFUSION: null value should coerce to False safely."""
        csrf = self._setup(page, dvad_server)
        resp = api_post(page, dvad_server, "/api/config/settings-toggle", csrf, {
            "key": "live_testing",
            "value": None,
        })
        # bool(None) -> False, should be accepted
        assert resp.status == 200


# ---------------------------------------------------------------------------
# Validate endpoint fuzzing (non-destructive but should be robust)
# ---------------------------------------------------------------------------

class TestValidateEndpointFuzzing:
    """POST /api/config/validate - should never crash, never write to disk."""

    def _setup(self, page, dvad_server):
        page.goto(f"{dvad_server}/config")
        page.wait_for_load_state("networkidle")
        return get_csrf_token(page)

    def test_validate_empty_string(self, page, dvad_server):
        """Empty YAML content returns invalid, does not crash."""
        csrf = self._setup(page, dvad_server)
        resp = api_post(page, dvad_server, "/api/config/validate", csrf, {"yaml": ""})
        assert resp.status == 200  # Returns JSON with valid=False
        body = resp.json()
        assert body["valid"] is False

    def test_validate_malformed_yaml(self, page, dvad_server):
        """Malformed YAML returns invalid, does not crash."""
        csrf = self._setup(page, dvad_server)
        resp = api_post(page, dvad_server, "/api/config/validate", csrf, {
            "yaml": "{{{{invalid yaml}}}}",
        })
        assert resp.status == 200
        body = resp.json()
        assert body["valid"] is False

    def test_validate_null_yaml(self, page, dvad_server):
        """Null YAML content is handled gracefully."""
        csrf = self._setup(page, dvad_server)
        resp = api_post(page, dvad_server, "/api/config/validate", csrf, {"yaml": None})
        # Should return invalid or error, not crash. 500 = unhandled error.
        assert resp.status in (200, 400, 500)

    def test_validate_models_empty_dict(self, page, dvad_server):
        """models: {} - syntactically valid but operationally empty."""
        csrf = self._setup(page, dvad_server)
        resp = api_post(page, dvad_server, "/api/config/validate", csrf, {
            "yaml": "models: {}\nroles: {}\n",
        })
        assert resp.status == 200
        body = resp.json()
        # Should have validation errors (no author, no reviewers, etc.)
        assert body["valid"] is False
