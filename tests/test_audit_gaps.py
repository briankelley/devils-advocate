"""Tests covering gaps identified in the 2026-02-25 test audit.

Addresses coverage for:
- gui/api.py: path-based file picker, cancel_review, _get_git_info
- gui/pages.py: _list_reviews_cached TTL cache
- gui/progress.py: revision_skip_context, revision_failed patterns
- normalization.py: cost_tracker integration
- config.py: use_responses_api / thinking field parsing
- cli.py: GUI first-run detection branches
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

import httpx
import pytest
import respx
from click.testing import CliRunner
from fastapi.testclient import TestClient

from devils_advocate.gui import create_app
from devils_advocate.gui.progress import classify_log_message
from devils_advocate.types import (
    ConfigError,
    CostTracker,
    ModelConfig,
)


# ═══════════════════════════════════════════════════════════════════════════
# gui/progress.py — missing patterns
# ═══════════════════════════════════════════════════════════════════════════


class TestProgressGaps:
    """Cover revision_skip_context and revision_failed patterns."""

    def test_revision_skip_context(self):
        ev = classify_log_message(
            "Revision: prompt (85000 tokens) exceeds context window"
        )
        assert ev.phase == "revision_skip_context"
        assert ev.event_type == "phase"

    def test_revision_failed_nonfatal(self):
        ev = classify_log_message(
            "Revision failed (non-fatal): Connection reset by peer"
        )
        assert ev.phase == "revision_failed"
        assert ev.event_type == "phase"

    def test_dedup_calling(self):
        ev = classify_log_message(
            "Deduplication: calling haiku-3 (12 points)"
        )
        assert ev.phase == "dedup_calling"

    def test_dedup_responded(self):
        ev = classify_log_message(
            "Deduplication: haiku-3 responded (2500 output tokens)"
        )
        assert ev.phase == "dedup_responded"


# ═══════════════════════════════════════════════════════════════════════════
# config.py — use_responses_api and thinking fields
# ═══════════════════════════════════════════════════════════════════════════


class TestConfigFieldParsing:
    """Verify that new ModelConfig fields are parsed from YAML."""

    def _load(self, tmp_path, monkeypatch, yaml_text):
        config_file = tmp_path / "models.yaml"
        config_file.write_text(yaml_text)
        monkeypatch.setenv("FAKE_KEY", "sk-test")
        from devils_advocate.config import load_config
        return load_config(config_file)

    def test_use_responses_api_parsed(self, tmp_path, monkeypatch):
        """use_responses_api: true in YAML should set the flag on ModelConfig."""
        yaml = """\
models:
  codex-model:
    provider: openai
    model_id: gpt-5.3-codex
    api_key_env: FAKE_KEY
    api_base: https://api.openai.com/v1
    use_responses_api: true
    context_window: 128000
    cost_per_1k_input: 0.01
    cost_per_1k_output: 0.03
  author-model:
    provider: openai
    model_id: gpt-4
    api_key_env: FAKE_KEY
    api_base: https://api.openai.com/v1
    context_window: 128000
    cost_per_1k_input: 0.01
    cost_per_1k_output: 0.03
  reviewer-b:
    provider: openai
    model_id: gpt-4o
    api_key_env: FAKE_KEY
    api_base: https://api.openai.com/v1
    context_window: 128000
    cost_per_1k_input: 0.01
    cost_per_1k_output: 0.03
  integ-model:
    provider: openai
    model_id: gpt-4
    api_key_env: FAKE_KEY
    api_base: https://api.openai.com/v1
    context_window: 128000
    cost_per_1k_input: 0.01
    cost_per_1k_output: 0.03
roles:
  author: author-model
  reviewers:
    - codex-model
    - reviewer-b
  deduplication: reviewer-b
  integration_reviewer: integ-model
"""
        config = self._load(tmp_path, monkeypatch, yaml)
        assert config["all_models"]["codex-model"].use_responses_api is True
        assert config["all_models"]["author-model"].use_responses_api is False

    def test_thinking_parsed(self, tmp_path, monkeypatch):
        """thinking: true in YAML should set the flag on ModelConfig."""
        yaml = """\
models:
  thinker:
    provider: anthropic
    model_id: claude-opus-4-6
    api_key_env: FAKE_KEY
    thinking: true
    context_window: 200000
    cost_per_1k_input: 0.01
    cost_per_1k_output: 0.03
  basic:
    provider: openai
    model_id: gpt-4
    api_key_env: FAKE_KEY
    api_base: https://api.openai.com/v1
    context_window: 128000
    cost_per_1k_input: 0.01
    cost_per_1k_output: 0.03
  reviewer-b:
    provider: openai
    model_id: gpt-4o
    api_key_env: FAKE_KEY
    api_base: https://api.openai.com/v1
    context_window: 128000
    cost_per_1k_input: 0.01
    cost_per_1k_output: 0.03
  integ-model:
    provider: openai
    model_id: gpt-4
    api_key_env: FAKE_KEY
    api_base: https://api.openai.com/v1
    context_window: 128000
    cost_per_1k_input: 0.01
    cost_per_1k_output: 0.03
roles:
  author: thinker
  reviewers:
    - basic
    - reviewer-b
  deduplication: basic
  integration_reviewer: integ-model
"""
        config = self._load(tmp_path, monkeypatch, yaml)
        assert config["all_models"]["thinker"].thinking is True
        assert config["all_models"]["basic"].thinking is False

    def test_use_responses_api_defaults_false(self, tmp_path, monkeypatch):
        """Omitting use_responses_api should default to False."""
        yaml = """\
models:
  my-model:
    provider: openai
    model_id: gpt-4
    api_key_env: FAKE_KEY
    api_base: https://api.openai.com/v1
    context_window: 128000
    cost_per_1k_input: 0.01
    cost_per_1k_output: 0.03
  reviewer-b:
    provider: openai
    model_id: gpt-4o
    api_key_env: FAKE_KEY
    api_base: https://api.openai.com/v1
    context_window: 128000
    cost_per_1k_input: 0.01
    cost_per_1k_output: 0.03
  integ-model:
    provider: openai
    model_id: gpt-4
    api_key_env: FAKE_KEY
    api_base: https://api.openai.com/v1
    context_window: 128000
    cost_per_1k_input: 0.01
    cost_per_1k_output: 0.03
roles:
  author: my-model
  reviewers:
    - reviewer-b
    - my-model
  deduplication: reviewer-b
  integration_reviewer: integ-model
"""
        config = self._load(tmp_path, monkeypatch, yaml)
        assert config["all_models"]["my-model"].use_responses_api is False


# ═══════════════════════════════════════════════════════════════════════════
# normalization.py — cost_tracker integration
# ═══════════════════════════════════════════════════════════════════════════


class TestNormalizationCostTracker:
    """Verify cost_tracker.add() is called during normalization."""

    async def test_cost_tracker_records_normalization(self, monkeypatch):
        monkeypatch.setenv("NORM_KEY", "fake-key")
        model = ModelConfig(
            name="norm-model",
            provider="anthropic",
            model_id="haiku-test",
            api_key_env="NORM_KEY",
            cost_per_1k_input=0.001,
            cost_per_1k_output=0.004,
        )
        cost_tracker = CostTracker()

        structured = """\
REVIEW POINT 1:
SEVERITY: medium
CATEGORY: correctness
DESCRIPTION: Some issue found
RECOMMENDATION: Fix it
"""
        with respx.mock:
            respx.post("https://api.anthropic.com/v1/messages").mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "content": [{"type": "text", "text": structured}],
                        "usage": {"input_tokens": 200, "output_tokens": 80},
                    },
                )
            )

            from devils_advocate.normalization import normalize_review_response

            async with httpx.AsyncClient() as client:
                points = await normalize_review_response(
                    client,
                    raw="unstructured text",
                    model=model,
                    reviewer_name="test-reviewer",
                    cost_tracker=cost_tracker,
                    mode="normalization",
                )

        assert len(points) == 1
        assert len(cost_tracker.entries) == 1
        assert cost_tracker.entries[0]["model"] == "norm-model"
        assert cost_tracker.entries[0]["input_tokens"] == 200
        assert cost_tracker.entries[0]["output_tokens"] == 80
        assert cost_tracker.total_usd > 0
        assert cost_tracker.role_costs.get("normalization", 0) > 0


# ═══════════════════════════════════════════════════════════════════════════
# gui/api.py — path-based file picker flow
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def gui_app():
    return create_app()


@pytest.fixture
def gui_client(gui_app):
    return TestClient(gui_app)


@pytest.fixture
def csrf_token(gui_app):
    return gui_app.state.csrf_token


class TestPathBasedFilePickerFlow:
    """Tests for the server-side file picker path-based review start."""

    def test_path_mode_with_valid_files(self, gui_client, csrf_token, tmp_path):
        """input_paths JSON with valid file paths should pass file validation."""
        f1 = tmp_path / "plan.md"
        f1.write_text("# Plan")
        paths = json.dumps([str(f1)])
        resp = gui_client.post(
            "/api/review/start",
            data={
                "mode": "plan",
                "project": "test",
                "input_paths": paths,
            },
            headers={"X-DVAD-Token": csrf_token},
        )
        # May fail downstream on config, but should not fail on file validation
        if resp.status_code == 400:
            detail = resp.json().get("detail", "")
            assert "not found" not in detail.lower()
            assert "not a file" not in detail.lower()

    def test_path_mode_nonexistent_file_rejected(self, gui_client, csrf_token, tmp_path):
        """input_paths with a non-existent file should return 400."""
        paths = json.dumps([str(tmp_path / "nonexistent.md")])
        resp = gui_client.post(
            "/api/review/start",
            data={
                "mode": "plan",
                "project": "test",
                "input_paths": paths,
            },
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code == 400
        assert "not found" in resp.json()["detail"].lower()

    def test_path_mode_directory_rejected(self, gui_client, csrf_token, tmp_path):
        """input_paths pointing to a directory should return 400."""
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        paths = json.dumps([str(subdir)])
        resp = gui_client.post(
            "/api/review/start",
            data={
                "mode": "plan",
                "project": "test",
                "input_paths": paths,
            },
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code == 400
        assert "not a file" in resp.json()["detail"].lower()

    def test_path_mode_invalid_json_rejected(self, gui_client, csrf_token):
        """Invalid JSON in input_paths should return 400."""
        resp = gui_client.post(
            "/api/review/start",
            data={
                "mode": "plan",
                "project": "test",
                "input_paths": "{not valid json",
            },
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code == 400
        assert "json" in resp.json()["detail"].lower()

    def test_path_mode_with_reference_paths(self, gui_client, csrf_token, tmp_path):
        """reference_paths should be merged with input_paths."""
        f1 = tmp_path / "main.md"
        f1.write_text("main content")
        f2 = tmp_path / "ref.md"
        f2.write_text("reference content")
        resp = gui_client.post(
            "/api/review/start",
            data={
                "mode": "plan",
                "project": "test",
                "input_paths": json.dumps([str(f1)]),
                "reference_paths": json.dumps([str(f2)]),
            },
            headers={"X-DVAD-Token": csrf_token},
        )
        # Should not fail on file validation
        if resp.status_code == 400:
            detail = resp.json().get("detail", "")
            assert "not found" not in detail.lower()

    def test_path_mode_with_spec_path(self, gui_client, csrf_token, tmp_path):
        """spec_path in path mode should be validated."""
        code_file = tmp_path / "main.py"
        code_file.write_text("print('hello')")
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Spec")
        resp = gui_client.post(
            "/api/review/start",
            data={
                "mode": "code",
                "project": "test",
                "input_paths": json.dumps([str(code_file)]),
                "spec_path": str(spec_file),
            },
            headers={"X-DVAD-Token": csrf_token},
        )
        if resp.status_code == 400:
            detail = resp.json().get("detail", "")
            assert "spec file not found" not in detail.lower()

    def test_path_mode_missing_spec_rejected(self, gui_client, csrf_token, tmp_path):
        """Non-existent spec_path should return 400."""
        code_file = tmp_path / "main.py"
        code_file.write_text("print('hello')")
        resp = gui_client.post(
            "/api/review/start",
            data={
                "mode": "code",
                "project": "test",
                "input_paths": json.dumps([str(code_file)]),
                "spec_path": str(tmp_path / "missing-spec.md"),
            },
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code == 400
        assert "spec file not found" in resp.json()["detail"].lower()


# ═══════════════════════════════════════════════════════════════════════════
# gui/api.py — cancel_review endpoint
# ═══════════════════════════════════════════════════════════════════════════


class TestCancelReview:
    """Tests for the /api/review/{id}/cancel endpoint."""

    def test_cancel_requires_csrf(self, gui_client):
        resp = gui_client.post("/api/review/test_id/cancel")
        assert resp.status_code == 403

    def test_cancel_nonexistent_review_returns_404(self, gui_client, csrf_token):
        resp = gui_client.post(
            "/api/review/nonexistent_xyz/cancel",
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code == 404
        assert "no running review" in resp.json()["detail"].lower()


# ═══════════════════════════════════════════════════════════════════════════
# gui/pages.py — _list_reviews_cached TTL
# ═══════════════════════════════════════════════════════════════════════════


class TestListReviewsCached:
    """Tests for the TTL cache in _list_reviews_cached."""

    def test_cache_returns_same_data_within_ttl(self):
        from devils_advocate.gui.pages import _list_reviews_cached, _review_cache

        mock_storage = MagicMock()
        mock_storage.list_reviews.return_value = [{"review_id": "cached-1"}]

        # Reset cache
        _review_cache["data"] = None
        _review_cache["expires"] = 0

        with patch("devils_advocate.gui.pages.get_gui_storage", return_value=mock_storage):
            first = _list_reviews_cached()
            second = _list_reviews_cached()

        # Should only call list_reviews once (second call uses cache)
        assert mock_storage.list_reviews.call_count == 1
        assert first == second
        assert first == [{"review_id": "cached-1"}]

        # Clean up
        _review_cache["data"] = None
        _review_cache["expires"] = 0

    def test_cache_expires_after_ttl(self):
        from devils_advocate.gui.pages import _list_reviews_cached, _review_cache, _CACHE_TTL

        mock_storage = MagicMock()
        mock_storage.list_reviews.side_effect = [
            [{"review_id": "first"}],
            [{"review_id": "second"}],
        ]

        # Reset cache
        _review_cache["data"] = None
        _review_cache["expires"] = 0

        with patch("devils_advocate.gui.pages.get_gui_storage", return_value=mock_storage):
            first = _list_reviews_cached()
            # Force cache expiry
            _review_cache["expires"] = time.time() - 1
            second = _list_reviews_cached()

        assert mock_storage.list_reviews.call_count == 2
        assert first == [{"review_id": "first"}]
        assert second == [{"review_id": "second"}]

        # Clean up
        _review_cache["data"] = None
        _review_cache["expires"] = 0

    def test_cache_invalidation_via_none(self):
        """Setting _review_cache['data'] = None forces refresh."""
        from devils_advocate.gui.pages import _list_reviews_cached, _review_cache

        mock_storage = MagicMock()
        mock_storage.list_reviews.return_value = [{"review_id": "fresh"}]

        _review_cache["data"] = None
        _review_cache["expires"] = time.time() + 9999  # far future

        with patch("devils_advocate.gui.pages.get_gui_storage", return_value=mock_storage):
            result = _list_reviews_cached()

        # data=None overrides TTL check
        assert mock_storage.list_reviews.call_count == 1
        assert result == [{"review_id": "fresh"}]

        # Clean up
        _review_cache["data"] = None
        _review_cache["expires"] = 0


# ═══════════════════════════════════════════════════════════════════════════
# cli.py — GUI first-run detection branches
# ═══════════════════════════════════════════════════════════════════════════


def _mock_gui_context_for_firstrun(mock_sock_instance, mock_uvicorn=None, mock_create_app=None):
    """Build patches for GUI command tests with first-run detection."""
    gui_mock = MagicMock()
    if mock_create_app is not None:
        gui_mock.create_app = mock_create_app
    else:
        gui_mock.create_app = MagicMock(return_value=MagicMock())

    modules_patch = {"devils_advocate.gui": gui_mock}
    if mock_uvicorn is not None:
        modules_patch["uvicorn"] = mock_uvicorn

    return patch.dict("sys.modules", modules_patch), \
        patch("socket.socket", return_value=mock_sock_instance)


class TestGuiFirstRunDetection:
    """Tests for the first-run / config health detection in gui_cmd."""

    def test_first_run_file_not_found(self, tmp_path):
        """FileNotFoundError during config load prints first-run message."""
        from devils_advocate.cli import cli
        runner = CliRunner()

        mock_sock = MagicMock()
        mock_uvicorn = MagicMock()

        modules_patch, socket_patch = _mock_gui_context_for_firstrun(
            mock_sock, mock_uvicorn=mock_uvicorn,
            mock_create_app=MagicMock(return_value=MagicMock()),
        )

        with modules_patch, socket_patch, \
             patch("devils_advocate.config.load_config", side_effect=FileNotFoundError("no config")), \
             patch("devils_advocate.config.get_config_health"):
            result = runner.invoke(cli, ["gui", "--port", "18413"])

        assert result.exit_code == 0
        assert "First run" in result.output or "first run" in result.output.lower()

    def test_config_has_errors_prints_setup_incomplete(self, tmp_path):
        """Config with validation errors prints setup-incomplete message."""
        from devils_advocate.cli import cli
        runner = CliRunner()

        mock_sock = MagicMock()
        mock_uvicorn = MagicMock()

        modules_patch, socket_patch = _mock_gui_context_for_firstrun(
            mock_sock, mock_uvicorn=mock_uvicorn,
            mock_create_app=MagicMock(return_value=MagicMock()),
        )

        mock_config = {"models": {}, "config_path": "/fake"}

        with modules_patch, socket_patch, \
             patch("devils_advocate.config.load_config", return_value=mock_config), \
             patch("devils_advocate.config.get_config_health", return_value=(True, "Missing API keys")):
            result = runner.invoke(cli, ["gui", "--port", "18414"])

        assert result.exit_code == 0
        assert "Setup incomplete" in result.output or "setup incomplete" in result.output.lower()

    def test_config_generic_error_prints_config_error(self, tmp_path):
        """Generic exception during config load prints configuration error."""
        from devils_advocate.cli import cli
        runner = CliRunner()

        mock_sock = MagicMock()
        mock_uvicorn = MagicMock()

        modules_patch, socket_patch = _mock_gui_context_for_firstrun(
            mock_sock, mock_uvicorn=mock_uvicorn,
            mock_create_app=MagicMock(return_value=MagicMock()),
        )

        with modules_patch, socket_patch, \
             patch("devils_advocate.config.load_config", side_effect=ValueError("corrupt yaml")):
            result = runner.invoke(cli, ["gui", "--port", "18415"])

        assert result.exit_code == 0
        assert "Configuration error" in result.output or "configuration error" in result.output.lower()


# ═══════════════════════════════════════════════════════════════════════════
# gui/api.py — start_review missing project name
# ═══════════════════════════════════════════════════════════════════════════


class TestStartReviewValidation:
    """Additional validation edge cases for /api/review/start."""

    def test_missing_project_name_rejected(self, gui_client, csrf_token):
        resp = gui_client.post(
            "/api/review/start",
            data={"mode": "plan", "project": ""},
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code == 400
        assert "project" in resp.json()["detail"].lower()

    def test_whitespace_only_project_rejected(self, gui_client, csrf_token):
        resp = gui_client.post(
            "/api/review/start",
            data={"mode": "plan", "project": "   "},
            headers={"X-DVAD-Token": csrf_token},
        )
        assert resp.status_code == 400
        assert "project" in resp.json()["detail"].lower()


# ═══════════════════════════════════════════════════════════════════════════
# CostTracker — structured log event emission
# ═══════════════════════════════════════════════════════════════════════════


class TestCostTrackerLogEvent:
    """Test that CostTracker emits §cost structured events via _log_fn."""

    def test_log_fn_emits_structured_cost_event(self):
        log_messages = []
        tracker = CostTracker(_log_fn=log_messages.append)
        tracker.add(
            model_name="gpt-4o",
            input_tokens=1000,
            output_tokens=500,
            cost_input=0.01,
            cost_output=0.03,
            role="reviewer",
        )
        assert len(log_messages) == 1
        msg = log_messages[0]
        assert msg.startswith("§cost")
        assert "role=reviewer" in msg
        assert "model=gpt-4o" in msg

    def test_log_fn_not_called_without_role(self):
        log_messages = []
        tracker = CostTracker(_log_fn=log_messages.append)
        tracker.add(
            model_name="gpt-4o",
            input_tokens=1000,
            output_tokens=500,
            cost_input=0.01,
            cost_output=0.03,
            role="",  # empty role
        )
        assert len(log_messages) == 0

    def test_role_costs_accumulated(self):
        tracker = CostTracker()
        tracker.add("m1", 1000, 500, 0.01, 0.03, role="reviewer")
        tracker.add("m1", 1000, 500, 0.01, 0.03, role="reviewer")
        tracker.add("m2", 1000, 500, 0.01, 0.03, role="author")
        assert "reviewer" in tracker.role_costs
        assert "author" in tracker.role_costs
        assert tracker.role_costs["reviewer"] > tracker.role_costs["author"]


# ═══════════════════════════════════════════════════════════════════════════
# Validate Keys SSRF Allowlist
# ═══════════════════════════════════════════════════════════════════════════


class TestValidateKeysAllowlist:
    """Tests for the dynamic SSRF allowlist built from config."""

    def test_allowlist_built_from_config(self):
        from devils_advocate.gui.api import _build_api_domain_allowlist
        from helpers import make_model_config

        m1 = make_model_config(name="m1")
        m1.api_base = "https://api.openai.com/v1"
        m2 = make_model_config(name="m2")
        m2.api_base = "https://api.custom-llm.example.com/v1"
        config = {"all_models": {"m1": m1, "m2": m2}}

        allowlist = _build_api_domain_allowlist(config)
        assert "api.openai.com" in allowlist
        assert "api.custom-llm.example.com" in allowlist
        # Anthropic always included even without a model entry
        assert "api.anthropic.com" in allowlist

    def test_unlisted_domain_not_in_allowlist(self):
        from devils_advocate.gui.api import _build_api_domain_allowlist
        from helpers import make_model_config

        m1 = make_model_config(name="m1")
        m1.api_base = "https://api.openai.com/v1"
        config = {"all_models": {"m1": m1}}

        allowlist = _build_api_domain_allowlist(config)
        assert "evil.example.com" not in allowlist

    def test_local_model_base_in_allowlist(self):
        from devils_advocate.gui.api import _build_api_domain_allowlist
        from helpers import make_model_config

        m1 = make_model_config(name="local")
        m1.api_base = "http://192.168.1.100:8080/v1"
        config = {"all_models": {"local": m1}}

        allowlist = _build_api_domain_allowlist(config)
        assert "192.168.1.100" in allowlist


class TestRoleConflictValidationData:
    """Test that author==dedup produces a warning in config validation."""

    def test_author_dedup_collision_flagged(self):
        from devils_advocate.config import validate_config_structure
        from helpers import make_model_config

        m = make_model_config(name="dual-role", api_key_env="AUTH_KEY")
        m.roles = {"author"}
        m.deduplication = True
        cfg = {"models": {"dual-role": m}, "all_models": {"dual-role": m}}
        issues = validate_config_structure(cfg)
        assert any("Deduplication model should NOT" in msg for _, msg in issues)
