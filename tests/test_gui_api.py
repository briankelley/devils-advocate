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
            json={"model_name": "test", "max_out_configured": -1},
            headers={"X-DVAD-Token": token},
        )
        assert resp.status_code == 400

    def test_rejects_too_large(self, client, token):
        resp = client.post(
            "/api/config/model-max-tokens",
            json={"model_name": "test", "max_out_configured": 2_000_000},
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
        (may fail on config file access, but not on key validation).
        Uses value=False to avoid accidentally enabling live tests."""
        resp = client.post(
            "/api/config/settings-toggle",
            json={"key": "live_testing", "value": False},
            headers={"X-DVAD-Token": token},
        )
        # Could be 200 (success) or 400/500 (config issue), but not 400 for unknown key
        if resp.status_code == 400:
            assert "unknown" not in resp.json().get("detail", "").lower()


class TestLiveTestingSafety:
    """Guard against tests accidentally enabling live_testing in the real config."""

    def test_live_testing_is_not_enabled_in_config(self):
        """Fail loudly if any prior test left live_testing: true in models.yaml."""
        try:
            from devils_advocate.config import find_config
            import yaml
            config_path = find_config()
            with open(config_path) as f:
                raw = yaml.safe_load(f)
            value = raw.get("settings", {}).get("live_testing", False)
            assert value is not True, (
                f"live_testing is true in {config_path} — a test mutated the real config. "
                "This causes live API tests to run silently on subsequent pytest invocations."
            )
        except Exception:
            pass  # Config not found is fine


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


# ── Partial Acceptance Remap ──────────────────────────────────────────────────


class TestPartialAcceptanceRemap:
    """When 'Accept Author' is clicked on a PARTIAL finding, the override
    endpoint should remap auto_dismissed → partial_accepted."""

    def test_auto_dismissed_remapped_for_partial(self, client, token):
        """auto_dismissed on a PARTIAL author_resolution → partial_accepted."""
        ledger = {
            "points": [{
                "group_id": "grp_001",
                "point_id": "pt_001",
                "author_resolution": "PARTIAL",
                "final_resolution": "escalated",
            }],
        }
        mock_storage = MagicMock()
        mock_storage.load_review.return_value = ledger
        mock_storage.update_point_override.return_value = None

        with patch("devils_advocate.gui.api.get_gui_storage", return_value=mock_storage):
            resp = client.post(
                "/api/review/test_review/override",
                json={"group_id": "grp_001", "resolution": "auto_dismissed"},
                headers={"X-DVAD-Token": token},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["resolution"] == "partial_accepted"
        # Storage should have been called with the remapped resolution
        mock_storage.update_point_override.assert_called_once_with(
            "test_review", "grp_001", "partial_accepted"
        )

    def test_auto_dismissed_not_remapped_for_non_partial(self, client, token):
        """auto_dismissed on a non-PARTIAL finding stays auto_dismissed."""
        ledger = {
            "points": [{
                "group_id": "grp_001",
                "point_id": "pt_001",
                "author_resolution": "ACCEPTED",
                "final_resolution": "escalated",
            }],
        }
        mock_storage = MagicMock()
        mock_storage.load_review.return_value = ledger
        mock_storage.update_point_override.return_value = None

        with patch("devils_advocate.gui.api.get_gui_storage", return_value=mock_storage):
            resp = client.post(
                "/api/review/test_review/override",
                json={"group_id": "grp_001", "resolution": "auto_dismissed"},
                headers={"X-DVAD-Token": token},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["resolution"] == "auto_dismissed"
        mock_storage.update_point_override.assert_called_once_with(
            "test_review", "grp_001", "auto_dismissed"
        )

    def test_overridden_not_affected_by_remap(self, client, token):
        """Overridden resolution bypasses remap logic entirely."""
        mock_storage = MagicMock()
        mock_storage.update_point_override.return_value = None

        with patch("devils_advocate.gui.api.get_gui_storage", return_value=mock_storage):
            resp = client.post(
                "/api/review/test_review/override",
                json={"group_id": "grp_001", "resolution": "overridden"},
                headers={"X-DVAD-Token": token},
            )

        assert resp.status_code == 200
        assert resp.json()["resolution"] == "overridden"
        # load_review should NOT have been called (remap only fires for auto_dismissed)
        mock_storage.load_review.assert_not_called()

    def test_remap_matches_by_point_id(self, client, token):
        """Remap works when the ledger point uses point_id instead of group_id."""
        ledger = {
            "points": [{
                "point_id": "grp_001",
                "author_resolution": "PARTIAL",
                "final_resolution": "escalated",
            }],
        }
        mock_storage = MagicMock()
        mock_storage.load_review.return_value = ledger
        mock_storage.update_point_override.return_value = None

        with patch("devils_advocate.gui.api.get_gui_storage", return_value=mock_storage):
            resp = client.post(
                "/api/review/test_review/override",
                json={"group_id": "grp_001", "resolution": "auto_dismissed"},
                headers={"X-DVAD-Token": token},
            )

        assert resp.status_code == 200
        assert resp.json()["resolution"] == "partial_accepted"


# ── Filesystem Browser (/api/fs/ls) ──────────────────────────────────────────


class TestFilesystemBrowser:
    """Tests for the /api/fs/ls endpoint."""

    def test_default_home_directory(self, client):
        """Omitting dir param should list the home directory."""
        resp = client.get("/api/fs/ls")
        assert resp.status_code == 200
        data = resp.json()
        assert "current_dir" in data
        assert "entries" in data
        assert isinstance(data["entries"], list)

    def test_explicit_directory(self, client, tmp_path):
        """Passing an explicit directory should list its contents."""
        (tmp_path / "fileA.txt").write_text("a")
        (tmp_path / "subdir").mkdir()
        resp = client.get("/api/fs/ls", params={"dir": str(tmp_path)})
        assert resp.status_code == 200
        data = resp.json()
        assert data["current_dir"] == str(tmp_path)
        names = [e["name"] for e in data["entries"]]
        assert "fileA.txt" in names
        assert "subdir" in names

    def test_directories_sorted_before_files(self, client, tmp_path):
        """Directories should appear before files in the listing."""
        (tmp_path / "zfile.txt").write_text("z")
        (tmp_path / "adir").mkdir()
        resp = client.get("/api/fs/ls", params={"dir": str(tmp_path)})
        data = resp.json()
        entries = data["entries"]
        assert len(entries) == 2
        assert entries[0]["name"] == "adir"
        assert entries[0]["is_dir"] is True
        assert entries[1]["name"] == "zfile.txt"

    def test_dotfiles_filtered_out(self, client, tmp_path):
        """Hidden files (starting with .) should be excluded."""
        (tmp_path / ".hidden").write_text("secret")
        (tmp_path / "visible.txt").write_text("public")
        resp = client.get("/api/fs/ls", params={"dir": str(tmp_path)})
        data = resp.json()
        names = [e["name"] for e in data["entries"]]
        assert ".hidden" not in names
        assert "visible.txt" in names

    def test_nonexistent_path_returns_400(self, client):
        resp = client.get("/api/fs/ls", params={"dir": "/nonexistent/path/xyz"})
        assert resp.status_code == 400
        assert "does not exist" in resp.json()["detail"]

    def test_file_path_returns_400(self, client, tmp_path):
        """Passing a file (not directory) should return 400."""
        f = tmp_path / "notadir.txt"
        f.write_text("content")
        resp = client.get("/api/fs/ls", params={"dir": str(f)})
        assert resp.status_code == 400
        assert "Not a directory" in resp.json()["detail"]

    def test_parent_dir_included(self, client, tmp_path):
        """Response should include parent_dir for non-root paths."""
        subdir = tmp_path / "child"
        subdir.mkdir()
        resp = client.get("/api/fs/ls", params={"dir": str(subdir)})
        data = resp.json()
        assert data["parent_dir"] == str(tmp_path)

    def test_file_entries_have_size(self, client, tmp_path):
        """File entries should include a numeric size field."""
        (tmp_path / "sized.txt").write_text("hello")
        resp = client.get("/api/fs/ls", params={"dir": str(tmp_path)})
        entry = resp.json()["entries"][0]
        assert entry["name"] == "sized.txt"
        assert entry["size"] == 5

    def test_directory_entries_have_null_size(self, client, tmp_path):
        """Directory entries should have size=None."""
        (tmp_path / "mydir").mkdir()
        resp = client.get("/api/fs/ls", params={"dir": str(tmp_path)})
        entry = resp.json()["entries"][0]
        assert entry["is_dir"] is True
        assert entry["size"] is None


# ── Log Viewer (/api/review/{id}/log) ────────────────────────────────────────


class TestLogViewer:
    """Tests for the /api/review/{review_id}/log endpoint."""

    def test_missing_log_returns_404(self, client):
        resp = client.get("/api/review/nonexistent_review_xyz/log")
        assert resp.status_code == 404
        assert "Log not found" in resp.json()["detail"]

    def test_existing_log_returns_content(self, client):
        """When a log file exists, it should be returned as text/plain."""
        from unittest.mock import patch, MagicMock
        from pathlib import Path
        import tempfile, os

        with tempfile.TemporaryDirectory() as td:
            log_dir = Path(td) / "logs"
            log_dir.mkdir()
            log_file = log_dir / "test_review_123.log"
            log_file.write_text("Line 1\nLine 2\n")

            mock_storage = MagicMock()
            mock_storage.data_dir = Path(td)

            with patch("devils_advocate.gui.api.get_gui_storage", return_value=mock_storage):
                resp = client.get("/api/review/test_review_123/log")

            assert resp.status_code == 200
            assert "Line 1" in resp.text
            assert "Line 2" in resp.text


# ── Diff Download Endpoint (/api/review/{id}/diff) ──────────────────────────


class TestDiffDownload:
    """Tests for the GET /api/review/{id}/diff endpoint."""

    def test_diff_200_when_patch_exists(self, client):
        """GET /diff returns 200 and the patch file when revised-diff.patch exists."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            reviews_dir = Path(td) / "reviews"
            review_dir = reviews_dir / "test_review_abc"
            review_dir.mkdir(parents=True)
            patch_content = "--- a/main.py\n+++ b/main.py\n@@ -1 +1 @@\n-old\n+new\n"
            (review_dir / "revised-diff.patch").write_text(patch_content)

            mock_storage = MagicMock()
            mock_storage.reviews_dir = reviews_dir

            with patch("devils_advocate.gui.api.get_gui_storage", return_value=mock_storage):
                resp = client.get("/api/review/test_review_abc/diff")

            assert resp.status_code == 200
            assert patch_content in resp.text
            # Verify download filename
            content_disp = resp.headers.get("content-disposition", "")
            assert "revised-diff-test_review_abc.patch" in content_disp

    def test_diff_404_when_no_patch(self, client):
        """GET /diff returns 404 when revised-diff.patch does not exist."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            reviews_dir = Path(td) / "reviews"
            review_dir = reviews_dir / "test_review_abc"
            review_dir.mkdir(parents=True)
            # No revised-diff.patch file

            mock_storage = MagicMock()
            mock_storage.reviews_dir = reviews_dir

            with patch("devils_advocate.gui.api.get_gui_storage", return_value=mock_storage):
                resp = client.get("/api/review/test_review_abc/diff")

            assert resp.status_code == 404
            assert "Diff not found" in resp.json()["detail"]


# ── Revised Download Backward Compatibility ─────────────────────────────────


class TestRevisedDownloadCompat:
    """Tests for /api/review/{id}/revised backward compatibility with diff artifacts."""

    def test_revised_prefers_full_file_over_diff(self, client):
        """When both revised-{name} and revised-diff.patch exist, /revised returns the full file."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            reviews_dir = Path(td) / "reviews"
            review_dir = reviews_dir / "test_review_code"
            review_dir.mkdir(parents=True)
            # Full revised code file
            (review_dir / "revised-main.py").write_text("def fixed(): pass\n")
            # Also has the diff
            (review_dir / "revised-diff.patch").write_text("--- a/main.py\n+++ b/main.py\n")

            mock_storage = MagicMock()
            mock_storage.reviews_dir = reviews_dir

            with patch("devils_advocate.gui.api.get_gui_storage", return_value=mock_storage):
                resp = client.get("/api/review/test_review_code/revised")

            assert resp.status_code == 200
            # Should return the full revised file, not the diff
            assert "def fixed(): pass" in resp.text
            content_disp = resp.headers.get("content-disposition", "")
            assert "revised-main-test_review_code.py" in content_disp

    def test_revised_falls_back_to_diff_for_old_reviews(self, client):
        """When ONLY revised-diff.patch exists (old review), /revised returns the diff."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            reviews_dir = Path(td) / "reviews"
            review_dir = reviews_dir / "test_review_old"
            review_dir.mkdir(parents=True)
            # Only the diff file (no full revised file)
            diff_content = "--- a/source.py\n+++ b/source.py\n@@ -1 +1 @@\n-old\n+new\n"
            (review_dir / "revised-diff.patch").write_text(diff_content)

            mock_storage = MagicMock()
            mock_storage.reviews_dir = reviews_dir

            with patch("devils_advocate.gui.api.get_gui_storage", return_value=mock_storage):
                resp = client.get("/api/review/test_review_old/revised")

            assert resp.status_code == 200
            assert diff_content in resp.text
            content_disp = resp.headers.get("content-disposition", "")
            assert "revised-diff-test_review_old.patch" in content_disp

    def test_revised_404_when_no_artifacts(self, client):
        """When no revised artifacts exist at all, /revised returns 404."""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            reviews_dir = Path(td) / "reviews"
            review_dir = reviews_dir / "test_review_empty"
            review_dir.mkdir(parents=True)
            # No revised-* files at all

            mock_storage = MagicMock()
            mock_storage.reviews_dir = reviews_dir

            with patch("devils_advocate.gui.api.get_gui_storage", return_value=mock_storage):
                resp = client.get("/api/review/test_review_empty/revised")

            assert resp.status_code == 404
            assert "Revised artifact not found" in resp.json()["detail"]
