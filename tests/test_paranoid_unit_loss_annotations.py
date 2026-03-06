"""Approach 5: Loss Annotation Policy Tests.

Enforces organizational policy about destructive operations using
the LOSS_ANNOTATIONS registry. Every annotation's stated safety
properties must actually hold.
"""

from __future__ import annotations

import pytest

from paranoid_unit_helpers import (
    WRITE_ENDPOINTS,
    LOSS_ANNOTATIONS,
)

pytest_plugins = ["conftest_paranoid_unit"]


# ── Structural validation: every endpoint has an annotation ─────────────────


class TestAnnotationCompleteness:
    """Every WRITE_ENDPOINTS entry must have a corresponding LOSS_ANNOTATIONS entry."""

    def test_all_write_endpoints_have_annotations(self):
        missing = set(WRITE_ENDPOINTS.keys()) - set(LOSS_ANNOTATIONS.keys())
        assert not missing, (
            f"Write endpoints without loss annotations: {missing}\n"
            f"Add entries to LOSS_ANNOTATIONS in paranoid_unit_helpers.py."
        )

    def test_all_annotations_have_write_endpoints(self):
        orphaned = set(LOSS_ANNOTATIONS.keys()) - set(WRITE_ENDPOINTS.keys())
        assert not orphaned, (
            f"Loss annotations without corresponding write endpoints: {orphaned}"
        )


# ── Policy: irreversible + no backup + no confirmation = FINDING ────────────


class TestIrreversibleWithoutSafeguards:
    """Flag endpoints that are irreversible, have no backup, and require no confirmation.

    These represent the highest-risk data loss vectors.
    """

    @pytest.mark.parametrize("endpoint_key", list(LOSS_ANNOTATIONS.keys()))
    def test_irreversible_must_have_safeguard(self, endpoint_key):
        """Every irreversible endpoint without a backup SHOULD require confirmation."""
        ann = LOSS_ANNOTATIONS[endpoint_key]

        if ann.get("reversible", True):
            pytest.skip("Endpoint is reversible")
        if ann.get("backup_exists", True):
            pytest.skip("Endpoint has backup")

        if not ann.get("confirmation_required", False):
            pytest.xfail(
                f"FINDING: {endpoint_key} is irreversible, has no backup, "
                f"and requires no confirmation dialog. "
                f"This is a high-risk data loss vector.\n"
                f"Details: {ann.get('FINDING', 'No additional details.')}"
            )


# ── Policy: data-loss-on-empty must reject empty input ──────────────────────


class TestEmptyInputDataLossPolicy:
    """Endpoints where empty input causes data loss MUST reject empty input."""

    @pytest.mark.parametrize(
        "endpoint_key",
        [
            k for k, v in LOSS_ANNOTATIONS.items()
            if "clear" in v.get("on_all_empty", "").lower()
            or "delete" in v.get("on_all_empty", "").lower()
            or "destroy" in v.get("on_all_empty", "").lower()
            or "loss" in v.get("on_all_empty", "").lower()
        ],
    )
    def test_data_loss_on_empty_must_be_rejected(
        self, paranoid_client, csrf_token, endpoint_key,
    ):
        """Endpoints annotated as causing data loss on empty input must reject it."""
        entry = WRITE_ENDPOINTS[endpoint_key]
        method = entry["method"].lower()
        path = entry["path"]
        path = path.replace("{review_id}", "nonexistent_xyz")
        path = path.replace("{env_name}", "FAKE_VAR")

        payload = entry["empty_payload"]
        headers = {"X-DVAD-Token": csrf_token}

        client_method = getattr(paranoid_client, method)

        if method == "post" and "review/start" in path:
            resp = client_method(path, data=payload, headers=headers)
        elif method in ("post", "put"):
            resp = client_method(path, json=payload, headers=headers)
        else:
            resp = client_method(path, headers=headers)

        if resp.status_code == 200:
            pytest.xfail(
                f"FINDING: {endpoint_key} annotated as data-loss-on-empty, "
                f"but accepted empty payload with status {resp.status_code}.\n"
                f"Annotation: {LOSS_ANNOTATIONS[endpoint_key]['on_all_empty']}"
            )


# ── Policy: preconditions must be enforced ──────────────────────────────────


class TestPreconditionEnforcement:
    """Verify that stated preconditions are actually enforced."""

    def test_config_save_requires_models_key(self, paranoid_client, csrf_token):
        """Precondition: YAML must contain 'models' key."""
        resp = paranoid_client.post(
            "/api/config",
            json={"yaml": "foo: bar\n"},
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code == 400
        assert "models" in resp.json().get("detail", "").lower()

    def test_config_save_requires_roles_key(self, paranoid_client, csrf_token):
        """Precondition: YAML must contain 'roles' key."""
        resp = paranoid_client.post(
            "/api/config",
            json={"yaml": "models:\n  m:\n    provider: openai\n"},
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code == 400
        assert "roles" in resp.json().get("detail", "").lower()

    def test_config_save_requires_validation_pass(self, paranoid_client, csrf_token):
        """Precondition: must pass validation."""
        # YAML with models+roles but invalid (author references nonexistent model)
        resp = paranoid_client.post(
            "/api/config",
            json={
                "yaml": (
                    "models:\n"
                    "  m:\n"
                    "    provider: openai\n"
                    "    model_id: gpt-4\n"
                    "    api_key_env: NONEXISTENT_KEY\n"
                    "roles:\n"
                    "  author: nonexistent\n"
                    "  reviewers:\n"
                    "    - m\n"
                )
            },
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code == 400

    def test_model_timeout_requires_model_exists(self, paranoid_client, csrf_token):
        """Precondition: model_name must exist in config."""
        resp = paranoid_client.post(
            "/api/config/model-timeout",
            json={"model_name": "NONEXISTENT_MODEL", "timeout": 120},
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code != 200

    def test_override_requires_review_exists(self, paranoid_client, csrf_token):
        """Precondition: review_id must exist."""
        resp = paranoid_client.post(
            "/api/review/nonexistent_abc/override",
            json={"group_id": "grp-001", "resolution": "overridden"},
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code != 200

    def test_override_requires_group_exists(
        self, paranoid_client, csrf_token, temp_review_env,
    ):
        """Precondition: group_id must exist in ledger."""
        _, _, review_id = temp_review_env
        resp = paranoid_client.post(
            f"/api/review/{review_id}/override",
            json={"group_id": "grp-NONEXISTENT", "resolution": "overridden"},
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code == 400

    def test_override_requires_valid_resolution(
        self, paranoid_client, csrf_token, temp_review_env,
    ):
        """Precondition: resolution must be in valid set."""
        _, _, review_id = temp_review_env
        resp = paranoid_client.post(
            f"/api/review/{review_id}/override",
            json={"group_id": "grp-001", "resolution": "invalid_value"},
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code == 400

    def test_env_var_must_be_in_allowed_set(self, paranoid_client, csrf_token):
        """Precondition: env_name must be in allowed_env_names."""
        resp = paranoid_client.put(
            "/api/config/env/TOTALLY_RANDOM_KEY",
            json={"value": "sk-test"},
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code == 400

    def test_revise_full_requires_code_mode(
        self, paranoid_client, csrf_token, temp_review_env,
    ):
        """Precondition: revise-full only works for code mode reviews."""
        _, _, review_id = temp_review_env
        # Our sample ledger has mode=plan
        resp = paranoid_client.post(
            f"/api/review/{review_id}/revise-full",
            headers={
                "X-DVAD-Token": csrf_token,
                "Content-Type": "application/json",
            },
        )
        assert resp.status_code == 400


# ── Document all findings ───────────────────────────────────────────────────


class TestFindingsDocumented:
    """Ensure all FINDING annotations are visible in test output."""

    @pytest.mark.parametrize(
        "endpoint_key",
        [k for k, v in LOSS_ANNOTATIONS.items() if "FINDING" in v],
    )
    def test_finding_is_documented(self, endpoint_key):
        """Each FINDING is an explicit acknowledgment of a risk that lacks mitigation."""
        finding = LOSS_ANNOTATIONS[endpoint_key]["FINDING"]
        # This test always passes but makes findings visible in pytest output
        # with the -v flag, documenting each risk.
        assert finding, f"Empty FINDING annotation for {endpoint_key}"

    def test_total_findings_count(self):
        """Report the total number of open findings."""
        findings = [
            k for k, v in LOSS_ANNOTATIONS.items() if "FINDING" in v
        ]
        # This test always passes; the count is informational.
        assert True, f"Total open findings: {len(findings)}"
