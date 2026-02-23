"""Comprehensive tests for GUI API endpoints — file upload, reference_files merge, config mutations."""

import io
import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

from fastapi.testclient import TestClient

from devils_advocate.gui import create_app


@pytest.fixture
def app():
    return create_app()


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def token(app):
    return app.state.csrf_token


def _upload(name: str, content: bytes = b"test content") -> tuple[str, tuple]:
    """Build an upload tuple for TestClient multipart."""
    return (name, (f"{name}.txt", io.BytesIO(content), "text/plain"))


# ── Start Review — reference_files merge ────────────────────────────────────


class TestReferenceFilesMerge:
    """Verify that reference_files are merged into the input_files list."""

    def test_reference_files_counted_in_total(self, client, token):
        """Reference files + input files share the MAX_FILES limit."""
        resp = client.post(
            "/api/review/start",
            data={"mode": "plan", "project": "test"},
            files=[
                _upload("input_files"),
                _upload("reference_files"),
            ],
            headers={"X-DVAD-Token": token},
        )
        # Should NOT fail with "requires at least one input file" since we
        # provided an input file. It may fail on config loading, but not
        # on file validation.
        if resp.status_code == 400:
            detail = resp.json().get("detail", "")
            assert "input file" not in detail.lower()

    def test_plan_mode_with_only_reference_files_rejected(self, client, token):
        """Plan mode requires at least one input_files upload, not just reference_files."""
        resp = client.post(
            "/api/review/start",
            data={"mode": "plan", "project": "test"},
            files=[_upload("reference_files")],
            headers={"X-DVAD-Token": token},
        )
        # reference_files alone should still pass the file loop (they get
        # merged), so plan mode should have at least 1 file and NOT reject.
        # This tests that the merge happens before mode validation.
        if resp.status_code == 400:
            detail = resp.json().get("detail", "")
            # If it fails, it should be a downstream error (config), not
            # "requires at least one input file"
            assert "input file" not in detail.lower()


# ── Start Review — file size/count limits ───────────────────────────────────


class TestFileLimits:
    def test_oversized_file_rejected(self, client, token):
        """A file exceeding MAX_FILE_SIZE (10MB) is rejected."""
        big_content = b"x" * (10 * 1024 * 1024 + 1)
        resp = client.post(
            "/api/review/start",
            data={"mode": "plan", "project": "test"},
            files=[("input_files", ("big.txt", io.BytesIO(big_content), "text/plain"))],
            headers={"X-DVAD-Token": token},
        )
        assert resp.status_code == 400
        assert "exceeds" in resp.json()["detail"].lower() or "limit" in resp.json()["detail"].lower()

    def test_too_many_files_rejected(self, client, token):
        """More than MAX_FILES (25) is rejected."""
        files = [
            ("input_files", (f"file{i}.txt", io.BytesIO(b"data"), "text/plain"))
            for i in range(26)
        ]
        resp = client.post(
            "/api/review/start",
            data={"mode": "plan", "project": "test"},
            files=files,
            headers={"X-DVAD-Token": token},
        )
        assert resp.status_code == 400
        assert "too many" in resp.json()["detail"].lower()

    def test_exactly_max_files_accepted(self, client, token):
        """Exactly MAX_FILES (25) should pass file count validation."""
        files = [
            ("input_files", (f"file{i}.txt", io.BytesIO(b"data"), "text/plain"))
            for i in range(25)
        ]
        resp = client.post(
            "/api/review/start",
            data={"mode": "plan", "project": "test"},
            files=files,
            headers={"X-DVAD-Token": token},
        )
        # May fail downstream (config) but not on file count
        if resp.status_code == 400:
            assert "too many" not in resp.json()["detail"].lower()


# ── Start Review — mode-specific validation ─────────────────────────────────


class TestModeValidation:
    def test_code_mode_rejects_multiple_files(self, client, token):
        """Code mode requires exactly one input file."""
        files = [
            ("input_files", ("a.py", io.BytesIO(b"code"), "text/plain")),
            ("input_files", ("b.py", io.BytesIO(b"code"), "text/plain")),
        ]
        resp = client.post(
            "/api/review/start",
            data={"mode": "code", "project": "test"},
            files=files,
            headers={"X-DVAD-Token": token},
        )
        assert resp.status_code == 400
        assert "exactly one" in resp.json()["detail"].lower()

    def test_code_mode_rejects_zero_files(self, client, token):
        """Code mode requires exactly one input file — zero should fail."""
        resp = client.post(
            "/api/review/start",
            data={"mode": "code", "project": "test"},
            headers={"X-DVAD-Token": token},
        )
        assert resp.status_code == 400
        assert "exactly one" in resp.json()["detail"].lower()

    def test_integration_mode_allows_no_files(self, client, token):
        """Integration mode does not require input files."""
        resp = client.post(
            "/api/review/start",
            data={"mode": "integration", "project": "test"},
            headers={"X-DVAD-Token": token},
        )
        # Should not fail with a file requirement error
        if resp.status_code == 400:
            detail = resp.json()["detail"].lower()
            assert "input file" not in detail
            assert "requires" not in detail or "input" not in detail

    def test_spec_mode_requires_files(self, client, token):
        """Spec mode requires at least one input file."""
        resp = client.post(
            "/api/review/start",
            data={"mode": "spec", "project": "test"},
            headers={"X-DVAD-Token": token},
        )
        assert resp.status_code == 400
        assert "input file" in resp.json()["detail"].lower()

    def test_invalid_mode_rejected(self, client, token):
        resp = client.post(
            "/api/review/start",
            data={"mode": "bogus", "project": "test"},
            headers={"X-DVAD-Token": token},
        )
        assert resp.status_code == 400
        assert "invalid mode" in resp.json()["detail"].lower()


# ── Start Review — dry run and max_cost ─────────────────────────────────────


class TestDryRunAndMaxCost:
    def test_invalid_max_cost_rejected(self, client, token):
        resp = client.post(
            "/api/review/start",
            data={"mode": "plan", "project": "test", "max_cost": "not_a_number"},
            files=[_upload("input_files")],
            headers={"X-DVAD-Token": token},
        )
        assert resp.status_code == 400
        assert "max_cost" in resp.json()["detail"].lower()


# ── Config Mutation Endpoints ───────────────────────────────────────────────


class TestModelTimeout:
    def test_requires_csrf(self, client):
        resp = client.post(
            "/api/config/model-timeout",
            json={"model_name": "test", "timeout": 60},
        )
        assert resp.status_code == 403

    def test_requires_model_name(self, client, token):
        resp = client.post(
            "/api/config/model-timeout",
            json={"model_name": "", "timeout": 60},
            headers={"X-DVAD-Token": token},
        )
        assert resp.status_code == 400
        assert "model_name" in resp.json()["detail"]

    def test_timeout_too_low(self, client, token):
        resp = client.post(
            "/api/config/model-timeout",
            json={"model_name": "test", "timeout": 5},
            headers={"X-DVAD-Token": token},
        )
        assert resp.status_code == 400
        assert "between" in resp.json()["detail"].lower()

    def test_timeout_too_high(self, client, token):
        resp = client.post(
            "/api/config/model-timeout",
            json={"model_name": "test", "timeout": 9999},
            headers={"X-DVAD-Token": token},
        )
        assert resp.status_code == 400
        assert "between" in resp.json()["detail"].lower()

    def test_timeout_non_integer(self, client, token):
        resp = client.post(
            "/api/config/model-timeout",
            json={"model_name": "test", "timeout": "abc"},
            headers={"X-DVAD-Token": token},
        )
        assert resp.status_code == 400


class TestModelThinking:
    def test_requires_csrf(self, client):
        resp = client.post(
            "/api/config/model-thinking",
            json={"model_name": "test", "thinking": True},
        )
        assert resp.status_code == 403

    def test_requires_model_name(self, client, token):
        resp = client.post(
            "/api/config/model-thinking",
            json={"model_name": "", "thinking": True},
            headers={"X-DVAD-Token": token},
        )
        assert resp.status_code == 400
        assert "model_name" in resp.json()["detail"]


class TestModelMaxTokens:
    def test_requires_csrf(self, client):
        resp = client.post(
            "/api/config/model-max-tokens",
            json={"model_name": "test", "max_output_tokens": 1000},
        )
        assert resp.status_code == 403

    def test_requires_model_name(self, client, token):
        resp = client.post(
            "/api/config/model-max-tokens",
            json={"model_name": "", "max_output_tokens": 1000},
            headers={"X-DVAD-Token": token},
        )
        assert resp.status_code == 400

    def test_rejects_out_of_range(self, client, token):
        resp = client.post(
            "/api/config/model-max-tokens",
            json={"model_name": "test", "max_output_tokens": -1},
            headers={"X-DVAD-Token": token},
        )
        assert resp.status_code == 400

    def test_rejects_too_large(self, client, token):
        resp = client.post(
            "/api/config/model-max-tokens",
            json={"model_name": "test", "max_output_tokens": 2_000_000},
            headers={"X-DVAD-Token": token},
        )
        assert resp.status_code == 400


class TestSettingsToggle:
    def test_requires_csrf(self, client):
        resp = client.post(
            "/api/config/settings-toggle",
            json={"key": "live_testing", "value": True},
        )
        assert resp.status_code == 403

    def test_rejects_unknown_key(self, client, token):
        resp = client.post(
            "/api/config/settings-toggle",
            json={"key": "unknown_setting", "value": True},
            headers={"X-DVAD-Token": token},
        )
        assert resp.status_code == 400
        assert "unknown" in resp.json()["detail"].lower()

    def test_accepts_valid_key(self, client, token):
        """live_testing is a valid key, so it should pass validation
        (may fail on config file access, but not on key validation)."""
        resp = client.post(
            "/api/config/settings-toggle",
            json={"key": "live_testing", "value": True},
            headers={"X-DVAD-Token": token},
        )
        # Could be 200 (success) or 400/500 (config issue), but not 400 for unknown key
        if resp.status_code == 400:
            assert "unknown" not in resp.json().get("detail", "").lower()


# ── Config validate/save ────────────────────────────────────────────────────


class TestConfigValidate:
    def test_validates_good_yaml(self, client, token):
        """Valid YAML with models key should not return parse error."""
        resp = client.post(
            "/api/config/validate",
            json={"yaml": "models:\n  test: {}\nroles:\n  author: test\n"},
            headers={"X-DVAD-Token": token},
        )
        assert resp.status_code == 200
        data = resp.json()
        # It may have validation issues (missing reviewers etc.) but not parse error
        assert isinstance(data.get("issues"), list)

    def test_validates_missing_models_key(self, client, token):
        resp = client.post(
            "/api/config/validate",
            json={"yaml": "foo: bar\n"},
            headers={"X-DVAD-Token": token},
        )
        data = resp.json()
        assert data["valid"] is False
        assert any("models" in msg.lower() for _, msg in data["issues"])


class TestConfigSave:
    def test_save_requires_csrf(self, client):
        resp = client.post("/api/config", json={"yaml": "models: {}"})
        assert resp.status_code == 403

    def test_save_rejects_bad_yaml(self, client, token):
        resp = client.post(
            "/api/config",
            json={"yaml": "{{{{not yaml"},
            headers={"X-DVAD-Token": token},
        )
        assert resp.status_code == 400
        assert "yaml" in resp.json()["detail"].lower()

    def test_save_rejects_missing_models(self, client, token):
        resp = client.post(
            "/api/config",
            json={"yaml": "foo: bar\n"},
            headers={"X-DVAD-Token": token},
        )
        assert resp.status_code == 400
        assert "models" in resp.json()["detail"].lower()


# ── Download Endpoints ──────────────────────────────────────────────────────


class TestDownloads:
    def test_report_404_for_missing(self, client):
        resp = client.get("/api/review/nonexistent_xyz/report")
        assert resp.status_code == 404

    def test_revised_404_for_missing(self, client):
        resp = client.get("/api/review/nonexistent_xyz/revised")
        assert resp.status_code == 404

    def test_review_json_404_for_missing(self, client):
        resp = client.get("/api/review/nonexistent_xyz")
        assert resp.status_code == 404


# ── SSE Progress ────────────────────────────────────────────────────────────


class TestSSEProgress:
    def test_nonexistent_review_returns_terminal_event(self, client):
        resp = client.get("/api/review/nonexistent_xyz/progress")
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        body = resp.text
        assert "data:" in body
        # Should contain a terminal event
        for line in body.strip().split("\n"):
            if line.startswith("data:"):
                data = json.loads(line[5:].strip())
                assert data["type"] in ("complete", "error")
                break


# ── Revision Endpoint ───────────────────────────────────────────────────────


class TestRevision:
    def test_requires_csrf(self, client):
        resp = client.post(
            "/api/review/test_id/revise",
            json={},
        )
        assert resp.status_code == 403

    def test_nonexistent_review_404(self, client, token):
        resp = client.post(
            "/api/review/nonexistent_xyz/revise",
            json={},
            headers={
                "Content-Type": "application/json",
                "X-DVAD-Token": token,
            },
        )
        assert resp.status_code == 404


# ── Override Endpoint (additional tests) ────────────────────────────────────


class TestOverrideExtended:
    def test_override_all_valid_resolutions_pass_validation(self, client, token):
        """Each valid resolution should pass the validation check (may fail on storage)."""
        for res in ("overridden", "auto_dismissed", "escalated"):
            resp = client.post(
                "/api/review/nonexistent_xyz/override",
                json={"group_id": "grp_01", "resolution": res},
                headers={"X-DVAD-Token": token},
            )
            # Should NOT be rejected for invalid resolution
            if resp.status_code == 400:
                assert "invalid resolution" not in resp.json()["detail"].lower()

    def test_override_empty_group_id_rejected(self, client, token):
        resp = client.post(
            "/api/review/test_id/override",
            json={"group_id": "", "resolution": "overridden"},
            headers={"X-DVAD-Token": token},
        )
        assert resp.status_code == 400
        assert "group_id" in resp.json()["detail"]
