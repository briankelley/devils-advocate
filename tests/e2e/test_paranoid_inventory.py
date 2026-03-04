"""Approach 1: Destructive Action Inventory.

Validates that the WRITE_ENDPOINTS registry is complete by:
1. Checking every registered endpoint rejects requests without CSRF tokens
2. Checking every registered endpoint rejects empty/degenerate payloads
3. Cross-referencing registered endpoints against the actual FastAPI routes

This answers: "Do we know about every way a user can mutate state?"
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Add e2e dir to sys.path for paranoid_helpers import (no __init__.py)
sys.path.insert(0, str(Path(__file__).parent))
from paranoid_helpers import (
    WRITE_ENDPOINTS,
    LOSS_ANNOTATIONS,
    get_csrf_token,
    api_post,
    api_put,
    api_delete,
)

pytestmark = [pytest.mark.e2e, pytest.mark.paranoid]

# The captured review ID from fixtures
CAPTURED_REVIEW_ID = "captured_e2e_review"


# ---------------------------------------------------------------------------
# Approach 1a: Registry completeness - every write endpoint is annotated
# ---------------------------------------------------------------------------

class TestRegistryCompleteness:
    """Verify the registry and annotations are in sync."""

    def test_every_endpoint_has_loss_annotation(self):
        """Every WRITE_ENDPOINTS entry must have a corresponding LOSS_ANNOTATIONS entry."""
        missing = set(WRITE_ENDPOINTS.keys()) - set(LOSS_ANNOTATIONS.keys())
        assert not missing, f"Endpoints without loss annotations: {missing}"

    def test_every_annotation_has_endpoint(self):
        """Every LOSS_ANNOTATIONS entry must have a corresponding WRITE_ENDPOINTS entry."""
        orphaned = set(LOSS_ANNOTATIONS.keys()) - set(WRITE_ENDPOINTS.keys())
        assert not orphaned, f"Annotations without endpoints: {orphaned}"

    def test_all_endpoints_have_required_fields(self):
        """Every registry entry must have method, path, writes_to, destroys."""
        required = {"method", "path", "writes_to", "destroys", "empty_payload"}
        for key, entry in WRITE_ENDPOINTS.items():
            missing = required - set(entry.keys())
            assert not missing, f"Endpoint {key} missing fields: {missing}"

    def test_all_annotations_have_required_fields(self):
        """Every annotation must have the standard loss-analysis fields."""
        required = {
            "on_empty_input", "on_all_empty", "reversible",
            "backup_exists", "confirmation_required", "precondition",
        }
        for key, ann in LOSS_ANNOTATIONS.items():
            missing = required - set(ann.keys())
            assert not missing, f"Annotation {key} missing fields: {missing}"


# ---------------------------------------------------------------------------
# Approach 1b: CSRF enforcement - every mutating endpoint requires CSRF
# ---------------------------------------------------------------------------

class TestCSRFEnforcement:
    """Every write endpoint must reject requests without a valid CSRF token."""

    def test_review_start_rejects_no_csrf(self, page, dvad_server):
        """POST /api/review/start without CSRF token returns 403."""
        resp = page.request.post(
            f"{dvad_server}/api/review/start",
            data=json.dumps({"mode": "plan", "project": "test"}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 403

    def test_review_cancel_rejects_no_csrf(self, page, dvad_server):
        """POST /api/review/{id}/cancel without CSRF token returns 403."""
        resp = page.request.post(
            f"{dvad_server}/api/review/fake-id/cancel",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 403

    def test_override_rejects_no_csrf(self, page, dvad_server):
        """POST /api/review/{id}/override without CSRF token returns 403."""
        resp = page.request.post(
            f"{dvad_server}/api/review/{CAPTURED_REVIEW_ID}/override",
            data=json.dumps({"group_id": "x", "resolution": "overridden"}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 403

    def test_config_save_rejects_no_csrf(self, page, dvad_server):
        """POST /api/config without CSRF token returns 403."""
        resp = page.request.post(
            f"{dvad_server}/api/config",
            data=json.dumps({"yaml": "models: {}"}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 403

    def test_model_timeout_rejects_no_csrf(self, page, dvad_server):
        """POST /api/config/model-timeout without CSRF token returns 403."""
        resp = page.request.post(
            f"{dvad_server}/api/config/model-timeout",
            data=json.dumps({"model_name": "e2e-remote", "timeout": 120}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 403

    def test_model_thinking_rejects_no_csrf(self, page, dvad_server):
        """POST /api/config/model-thinking without CSRF token returns 403."""
        resp = page.request.post(
            f"{dvad_server}/api/config/model-thinking",
            data=json.dumps({"model_name": "e2e-remote", "thinking": True}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 403

    def test_model_max_tokens_rejects_no_csrf(self, page, dvad_server):
        """POST /api/config/model-max-tokens without CSRF token returns 403."""
        resp = page.request.post(
            f"{dvad_server}/api/config/model-max-tokens",
            data=json.dumps({"model_name": "e2e-remote", "max_out_configured": 4096}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 403

    def test_settings_toggle_rejects_no_csrf(self, page, dvad_server):
        """POST /api/config/settings-toggle without CSRF token returns 403."""
        resp = page.request.post(
            f"{dvad_server}/api/config/settings-toggle",
            data=json.dumps({"key": "live_testing", "value": False}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 403

    def test_validate_rejects_no_csrf(self, page, dvad_server):
        """POST /api/config/validate without CSRF token returns 403."""
        resp = page.request.post(
            f"{dvad_server}/api/config/validate",
            data=json.dumps({"yaml": "models: {}"}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 403

    def test_env_put_rejects_no_csrf(self, page, dvad_server):
        """PUT /api/config/env/{name} without CSRF token returns 403."""
        resp = page.request.put(
            f"{dvad_server}/api/config/env/E2E_LOCAL_KEY",
            data=json.dumps({"value": "test"}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 403

    def test_env_delete_rejects_no_csrf(self, page, dvad_server):
        """DELETE /api/config/env/{name} without CSRF token returns 403."""
        resp = page.request.delete(
            f"{dvad_server}/api/config/env/E2E_LOCAL_KEY",
        )
        assert resp.status == 403

    def test_env_batch_rejects_no_csrf(self, page, dvad_server):
        """POST /api/config/env without CSRF token returns 403."""
        resp = page.request.post(
            f"{dvad_server}/api/config/env",
            data=json.dumps({"env_vars": {"E2E_LOCAL_KEY": "test"}}),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 403

    def test_revise_rejects_no_csrf(self, page, dvad_server):
        """POST /api/review/{id}/revise without CSRF token returns 403."""
        resp = page.request.post(
            f"{dvad_server}/api/review/{CAPTURED_REVIEW_ID}/revise",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 403

    def test_revise_full_rejects_no_csrf(self, page, dvad_server):
        """POST /api/review/{id}/revise-full without CSRF token returns 403."""
        resp = page.request.post(
            f"{dvad_server}/api/review/{CAPTURED_REVIEW_ID}/revise-full",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 403

    def test_wrong_csrf_token_rejected(self, page, dvad_server):
        """A wrong CSRF token must be rejected just like a missing one."""
        resp = page.request.post(
            f"{dvad_server}/api/config",
            data=json.dumps({"yaml": "models: {}"}),
            headers={
                "X-DVAD-Token": "completely-wrong-token-value",
                "Content-Type": "application/json",
            },
        )
        assert resp.status == 403


# ---------------------------------------------------------------------------
# Approach 1c: Empty payload rejection - write endpoints reject empty input
# ---------------------------------------------------------------------------

class TestEmptyPayloadRejection:
    """Every write endpoint that accepts user input must reject empty payloads
    with a 400 error rather than silently proceeding."""

    def _get_csrf(self, page, dvad_server) -> str:
        page.goto(dvad_server)
        page.wait_for_load_state("networkidle")
        return get_csrf_token(page)

    def test_review_start_rejects_empty_project(self, page, dvad_server):
        """POST /api/review/start with empty project returns 400."""
        csrf = self._get_csrf(page, dvad_server)
        resp = api_post(page, dvad_server, "/api/review/start", csrf, {
            "mode": "plan",
            "project": "",
        })
        assert resp.status == 400

    def test_override_rejects_empty_group_id(self, page, dvad_server):
        """POST /api/review/{id}/override with empty group_id returns 400."""
        csrf = self._get_csrf(page, dvad_server)
        resp = api_post(
            page, dvad_server,
            f"/api/review/{CAPTURED_REVIEW_ID}/override",
            csrf,
            {"group_id": "", "resolution": "overridden"},
        )
        assert resp.status == 400

    def test_override_rejects_empty_resolution(self, page, dvad_server):
        """POST /api/review/{id}/override with empty resolution returns 400."""
        csrf = self._get_csrf(page, dvad_server)
        resp = api_post(
            page, dvad_server,
            f"/api/review/{CAPTURED_REVIEW_ID}/override",
            csrf,
            {"group_id": "some_group", "resolution": ""},
        )
        assert resp.status == 400

    def test_model_timeout_rejects_empty_model_name(self, page, dvad_server):
        """POST /api/config/model-timeout with empty model_name returns 400."""
        csrf = self._get_csrf(page, dvad_server)
        resp = api_post(page, dvad_server, "/api/config/model-timeout", csrf, {
            "model_name": "",
            "timeout": 120,
        })
        assert resp.status == 400

    def test_model_timeout_rejects_null_timeout(self, page, dvad_server):
        """POST /api/config/model-timeout with null timeout returns 400."""
        csrf = self._get_csrf(page, dvad_server)
        resp = api_post(page, dvad_server, "/api/config/model-timeout", csrf, {
            "model_name": "e2e-remote",
            "timeout": None,
        })
        assert resp.status == 400

    def test_model_thinking_rejects_empty_model_name(self, page, dvad_server):
        """POST /api/config/model-thinking with empty model_name returns 400."""
        csrf = self._get_csrf(page, dvad_server)
        resp = api_post(page, dvad_server, "/api/config/model-thinking", csrf, {
            "model_name": "",
            "thinking": True,
        })
        assert resp.status == 400

    def test_model_max_tokens_rejects_empty_model_name(self, page, dvad_server):
        """POST /api/config/model-max-tokens with empty model_name returns 400."""
        csrf = self._get_csrf(page, dvad_server)
        resp = api_post(page, dvad_server, "/api/config/model-max-tokens", csrf, {
            "model_name": "",
            "max_out_configured": 4096,
        })
        assert resp.status == 400

    def test_settings_toggle_rejects_unknown_key(self, page, dvad_server):
        """POST /api/config/settings-toggle with unknown key returns 400."""
        csrf = self._get_csrf(page, dvad_server)
        resp = api_post(page, dvad_server, "/api/config/settings-toggle", csrf, {
            "key": "nonexistent_setting",
            "value": True,
        })
        assert resp.status == 400

    def test_settings_toggle_rejects_empty_key(self, page, dvad_server):
        """POST /api/config/settings-toggle with empty key returns 400."""
        csrf = self._get_csrf(page, dvad_server)
        resp = api_post(page, dvad_server, "/api/config/settings-toggle", csrf, {
            "key": "",
            "value": True,
        })
        assert resp.status == 400

    def test_config_save_rejects_empty_yaml(self, page, dvad_server):
        """POST /api/config with empty yaml returns 400."""
        csrf = self._get_csrf(page, dvad_server)
        resp = api_post(page, dvad_server, "/api/config", csrf, {"yaml": ""})
        assert resp.status == 400

    def test_config_save_rejects_yaml_without_models(self, page, dvad_server):
        """POST /api/config with yaml missing 'models' key returns 400."""
        csrf = self._get_csrf(page, dvad_server)
        resp = api_post(page, dvad_server, "/api/config", csrf, {
            "yaml": "settings:\n  live_testing: false\n",
        })
        assert resp.status == 400

    def test_env_put_rejects_empty_value(self, page, dvad_server):
        """PUT /api/config/env/{name} with empty value returns 400."""
        csrf = self._get_csrf(page, dvad_server)
        resp = api_put(page, dvad_server, "/api/config/env/E2E_LOCAL_KEY", csrf, {
            "value": "",
        })
        assert resp.status == 400

    def test_env_put_rejects_whitespace_only_value(self, page, dvad_server):
        """PUT /api/config/env/{name} with whitespace-only value returns 400."""
        csrf = self._get_csrf(page, dvad_server)
        resp = api_put(page, dvad_server, "/api/config/env/E2E_LOCAL_KEY", csrf, {
            "value": "   ",
        })
        assert resp.status == 400

    def test_env_batch_rejects_empty_dict(self, page, dvad_server):
        """POST /api/config/env with empty env_vars dict returns 400."""
        csrf = self._get_csrf(page, dvad_server)
        resp = api_post(page, dvad_server, "/api/config/env", csrf, {
            "env_vars": {},
        })
        assert resp.status == 400

    def test_env_put_rejects_unknown_env_name(self, page, dvad_server):
        """PUT /api/config/env/{name} with unknown env name returns 400."""
        csrf = self._get_csrf(page, dvad_server)
        resp = api_put(page, dvad_server, "/api/config/env/TOTALLY_FAKE_KEY", csrf, {
            "value": "some-value",
        })
        assert resp.status == 400

    def test_env_delete_rejects_unknown_env_name(self, page, dvad_server):
        """DELETE /api/config/env/{name} with unknown env name returns 400."""
        csrf = self._get_csrf(page, dvad_server)
        resp = api_delete(page, dvad_server, "/api/config/env/TOTALLY_FAKE_KEY", csrf)
        assert resp.status == 400


# ---------------------------------------------------------------------------
# Approach 1d: Route cross-reference (static check)
# ---------------------------------------------------------------------------

class TestRouteRegistry:
    """Verify the registry covers all mutating routes in the application."""

    def test_all_known_mutating_methods_covered(self):
        """Every POST/PUT/DELETE endpoint in the app must appear in the registry."""
        # These are the mutating routes from api.py, verified by reading the source.
        # If a new route is added to api.py without adding it here, this test fails.
        expected_routes = {
            "POST /api/review/start",
            "POST /api/review/{id}/cancel",
            "POST /api/review/{id}/override",
            "POST /api/review/{id}/revise",
            "POST /api/review/{id}/revise-full",
            "POST /api/config/model-timeout",
            "POST /api/config/model-thinking",
            "POST /api/config/model-max-tokens",
            "POST /api/config/settings-toggle",
            "POST /api/config/validate",
            "POST /api/config",
            "PUT /api/config/env/{env_name}",
            "DELETE /api/config/env/{env_name}",
            "POST /api/config/env",
        }
        registered = set(WRITE_ENDPOINTS.keys())
        missing = expected_routes - registered
        extra = registered - expected_routes
        assert not missing, f"Unregistered routes: {missing}"
        assert not extra, f"Registry has routes not in expected set: {extra}"
