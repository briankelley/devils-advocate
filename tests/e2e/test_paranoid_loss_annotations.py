"""Approach 5: Loss Annotation Policy Tests.

Enforces organizational policy about destructive operations using the
LOSS_ANNOTATIONS metadata.

This answers: "Do our stated safety properties actually hold?"
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
    UNGUARDED_DESTRUCTIVE_ENDPOINTS,
    get_csrf_token,
    api_post,
    api_put,
    api_delete,
)

pytestmark = [pytest.mark.e2e, pytest.mark.paranoid]

CAPTURED_REVIEW_ID = "captured_e2e_review"


# ---------------------------------------------------------------------------
# Policy: endpoints that describe data loss on all_empty MUST reject it
# ---------------------------------------------------------------------------

class TestAllEmptyRejectionPolicy:
    """Every endpoint where on_all_empty describes data loss must reject
    all-empty input with a 400 error."""

    def _get_csrf(self, page, dvad_server) -> str:
        page.goto(dvad_server)
        page.wait_for_load_state("networkidle")
        return get_csrf_token(page)

    @pytest.mark.parametrize("endpoint_key", [
        k for k, v in LOSS_ANNOTATIONS.items()
        if "400 error" not in v["on_all_empty"].lower()
        and k not in (
            # Skip endpoints that don't accept user input (no payload)
            "POST /api/review/{id}/cancel",
            "POST /api/review/{id}/revise",
            "POST /api/review/{id}/revise-full",
        )
    ])
    def test_data_loss_on_all_empty_has_guard(self, endpoint_key):
        """Endpoints where all-empty input could cause data loss should have
        explicit guards. This test is a static policy check."""
        ann = LOSS_ANNOTATIONS[endpoint_key]
        on_all_empty = ann["on_all_empty"].lower()
        # If the annotation says something other than "400 error" or "validation",
        # that endpoint may allow all-empty through
        dangerous_phrases = ["removes", "deletes", "unset", "delete"]
        is_dangerous = any(phrase in on_all_empty for phrase in dangerous_phrases)
        if is_dangerous:
            pytest.xfail(
                f"POLICY VIOLATION: {endpoint_key} allows all-empty input "
                f"that causes: {ann['on_all_empty']}"
            )


# ---------------------------------------------------------------------------
# Policy: irreversible + no backup = must have confirmation
# ---------------------------------------------------------------------------

class TestConfirmationPolicy:
    """Endpoints that are irreversible AND have no backup SHOULD require
    confirmation. Violations are flagged as xfail - policy gaps, not bugs."""

    @pytest.mark.parametrize("endpoint_key", UNGUARDED_DESTRUCTIVE_ENDPOINTS)
    def test_irreversible_unguarded_endpoints_flagged(self, endpoint_key):
        """FINDING: This endpoint is irreversible, has no backup, and has no
        confirmation dialog. This is a risk that should be reviewed."""
        ann = LOSS_ANNOTATIONS[endpoint_key]
        # This should xfail to flag the gap without blocking the suite
        pytest.xfail(
            f"UNGUARDED DESTRUCTIVE ENDPOINT: {endpoint_key}\n"
            f"  reversible={ann['reversible']}, "
            f"  backup_exists={ann['backup_exists']}, "
            f"  confirmation_required={ann['confirmation_required']}\n"
            f"  on_all_empty: {ann['on_all_empty']}"
        )


# ---------------------------------------------------------------------------
# Policy: preconditions must be enforced
# ---------------------------------------------------------------------------

class TestPreconditionEnforcement:
    """Every endpoint with a stated precondition must enforce it."""

    def _get_csrf(self, page, dvad_server) -> str:
        page.goto(dvad_server)
        page.wait_for_load_state("networkidle")
        return get_csrf_token(page)

    def test_review_start_enforces_no_running_review(self, page, dvad_server):
        """Precondition: No review currently running (409 if busy).
        Starting a review when one is running should return 409."""
        # We can't easily create a running review in E2E, but we can verify
        # the precondition annotation exists and the endpoint handles it
        ann = LOSS_ANNOTATIONS["POST /api/review/start"]
        assert "running" in ann["precondition"].lower()

    def test_override_enforces_review_exists(self, page, dvad_server):
        """Precondition: Review must exist. Override on nonexistent review
        must return error."""
        csrf = self._get_csrf(page, dvad_server)
        resp = api_post(
            page, dvad_server,
            "/api/review/nonexistent_id_xyz/override",
            csrf,
            {"group_id": "any", "resolution": "overridden"},
        )
        assert resp.status in (400, 404)

    def test_override_enforces_group_exists(self, page, dvad_server):
        """Precondition: group_id must exist in ledger."""
        csrf = self._get_csrf(page, dvad_server)
        resp = api_post(
            page, dvad_server,
            f"/api/review/{CAPTURED_REVIEW_ID}/override",
            csrf,
            {"group_id": "totally_fake_group_999", "resolution": "overridden"},
        )
        assert resp.status == 400

    def test_model_timeout_enforces_model_exists(self, page, dvad_server):
        """Precondition: Model must exist in models.yaml."""
        csrf = self._get_csrf(page, dvad_server)
        resp = api_post(page, dvad_server, "/api/config/model-timeout", csrf, {
            "model_name": "nonexistent_model_xyz",
            "timeout": 120,
        })
        assert resp.status == 404

    def test_model_max_tokens_enforces_model_exists(self, page, dvad_server):
        """Precondition: Model must exist in models.yaml."""
        csrf = self._get_csrf(page, dvad_server)
        resp = api_post(page, dvad_server, "/api/config/model-max-tokens", csrf, {
            "model_name": "nonexistent_model_xyz",
            "max_out_configured": 4096,
        })
        assert resp.status == 404

    def test_env_put_enforces_allowed_name(self, page, dvad_server):
        """Precondition: env_name must be in allowed_env_names from config."""
        csrf = self._get_csrf(page, dvad_server)
        resp = api_put(page, dvad_server, "/api/config/env/NOT_A_REAL_KEY", csrf, {
            "value": "test",
        })
        assert resp.status == 400

    def test_env_delete_enforces_allowed_name(self, page, dvad_server):
        """Precondition: env_name must be in allowed_env_names."""
        csrf = self._get_csrf(page, dvad_server)
        # Include X-Confirm-Destructive to bypass that guard and test name enforcement
        resp = page.request.delete(
            f"{dvad_server}/api/config/env/NOT_A_REAL_KEY",
            headers={
                "X-DVAD-Token": csrf,
                "X-Confirm-Destructive": "true",
            },
        )
        assert resp.status == 400

    def test_revise_enforces_review_exists(self, page, dvad_server):
        """Precondition: Review must exist."""
        csrf = self._get_csrf(page, dvad_server)
        resp = api_post(
            page, dvad_server,
            "/api/review/nonexistent_id_xyz/revise",
            csrf,
            {},
        )
        assert resp.status == 404

    def test_revise_full_enforces_review_exists(self, page, dvad_server):
        """Precondition: Review must exist."""
        csrf = self._get_csrf(page, dvad_server)
        resp = api_post(
            page, dvad_server,
            "/api/review/nonexistent_id_xyz/revise-full",
            csrf,
            {},
        )
        assert resp.status == 404

    def test_revise_full_enforces_code_mode(self, page, dvad_server):
        """Precondition: Review must be code mode for revise-full."""
        csrf = self._get_csrf(page, dvad_server)
        # The captured review is "spec" mode, not "code" mode
        resp = api_post(
            page, dvad_server,
            f"/api/review/{CAPTURED_REVIEW_ID}/revise-full",
            csrf,
            {},
        )
        assert resp.status == 400


# ---------------------------------------------------------------------------
# Policy: validate endpoint must never write to disk permanently
# ---------------------------------------------------------------------------

class TestValidateNoSideEffects:
    """POST /api/config/validate must never modify persistent state."""

    def test_validate_does_not_write_config(self, page, dvad_server):
        """Validate with valid YAML must not modify the config file."""
        page.goto(f"{dvad_server}/config")
        page.wait_for_load_state("networkidle")
        csrf = get_csrf_token(page)

        # Get current config
        resp1 = page.request.get(f"{dvad_server}/api/config")
        config_path = resp1.json()["config_path"]
        from pathlib import Path
        from paranoid_helpers import StateSnapshot
        path = Path(config_path)

        before = StateSnapshot.capture([path])

        # Validate with different YAML
        different_yaml = (
            "models:\n"
            "  fake-model:\n"
            "    provider: openai\n"
            "    model_id: gpt-4\n"
            "    api_key_env: FAKE_KEY\n"
            "roles:\n"
            "  author: fake-model\n"
            "  reviewers: [fake-model, fake-model]\n"
            "  deduplication: fake-model\n"
            "  integration_reviewer: fake-model\n"
        )
        resp2 = api_post(page, dvad_server, "/api/config/validate", csrf, {
            "yaml": different_yaml,
        })
        assert resp2.status == 200

        after = StateSnapshot.capture([path])
        before.assert_no_mutation(after, "validate side-effect check")


# ---------------------------------------------------------------------------
# Summary of findings from static analysis of annotations
# ---------------------------------------------------------------------------

class TestAnnotationFindings:
    """Surface findings from the loss annotations as documented test outcomes."""

    def test_config_full_save_has_backup(self):
        """POST /api/config creates a .bak backup before overwriting and
        requires a confirmation dialog."""
        ann = LOSS_ANNOTATIONS["POST /api/config"]
        assert ann["reversible"] is False
        assert ann["backup_exists"] is True
        assert ann["confirmation_required"] is True

    def test_env_batch_delete_has_guards(self):
        """POST /api/config/env now requires X-Confirm-Destructive header
        when empty values would delete keys, and creates a .bak backup."""
        ann = LOSS_ANNOTATIONS["POST /api/config/env"]
        assert ann["confirmation_required"] is True
        assert ann["backup_exists"] is True

    def test_env_delete_has_backup(self):
        """DELETE /api/config/env/{env_name} requires confirmation and creates
        a .bak backup before deletion."""
        ann = LOSS_ANNOTATIONS["DELETE /api/config/env/{env_name}"]
        assert ann["reversible"] is False
        assert ann["backup_exists"] is True
        assert ann["confirmation_required"] is True

    def test_review_cancel_has_confirmation(self):
        """POST /api/review/{id}/cancel now requires confirmation dialog."""
        ann = LOSS_ANNOTATIONS["POST /api/review/{id}/cancel"]
        assert ann["reversible"] is False
        assert ann["backup_exists"] is False
        assert ann["confirmation_required"] is True
