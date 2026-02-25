"""Tests for API key management endpoints and env file helpers."""

import os
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient

from devils_advocate.gui import create_app
from devils_advocate.gui.api import _read_env_file, _write_env_file


@pytest.fixture
def app():
    return create_app()


@pytest.fixture
def client(app):
    return TestClient(app)


@pytest.fixture
def token(app):
    return app.state.csrf_token


# ── _read_env_file helpers ───────────────────────────────────────────────────


class TestReadEnvFile:
    def test_read_empty_file(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("")
        lines, kv = _read_env_file(env_file)
        assert lines == [""]
        assert kv == {}

    def test_read_preserves_comments(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("# comment\nKEY=value\n")
        lines, kv = _read_env_file(env_file)
        assert len(lines) == 3  # comment, KEY=value, empty trailing
        assert kv == {"KEY": "value"}

    def test_read_nonexistent_file(self, tmp_path):
        env_file = tmp_path / ".env"
        lines, kv = _read_env_file(env_file)
        assert lines == []
        assert kv == {}

    def test_read_values_with_equals(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("KEY=value=with=equals\n")
        lines, kv = _read_env_file(env_file)
        assert kv == {"KEY": "value=with=equals"}


# ── _write_env_file helpers ──────────────────────────────────────────────────


class TestWriteEnvFile:
    def test_write_creates_file_with_0600(self, tmp_path):
        env_file = tmp_path / ".env"
        _write_env_file(env_file, [], {"KEY": "value"})
        assert env_file.exists()
        assert "KEY=value" in env_file.read_text()
        assert oct(env_file.stat().st_mode)[-3:] == "600"

    def test_write_updates_existing_key(self, tmp_path):
        env_file = tmp_path / ".env"
        lines = ["# header", "KEY=old_value", "OTHER=keep"]
        _write_env_file(env_file, lines, {"KEY": "new_value"})
        content = env_file.read_text()
        assert "KEY=new_value" in content
        assert "OTHER=keep" in content
        assert "# header" in content
        assert "old_value" not in content

    def test_write_preserves_comments_and_blanks(self, tmp_path):
        env_file = tmp_path / ".env"
        lines = ["# comment", "", "KEY=val"]
        _write_env_file(env_file, lines, {"KEY": "new"})
        written_lines = env_file.read_text().splitlines()
        assert written_lines[0] == "# comment"
        assert written_lines[1] == ""
        assert written_lines[2] == "KEY=new"

    def test_write_appends_new_keys(self, tmp_path):
        env_file = tmp_path / ".env"
        lines = ["EXISTING=val"]
        _write_env_file(env_file, lines, {"NEW_KEY": "new_val"})
        content = env_file.read_text()
        assert "EXISTING=val" in content
        assert "NEW_KEY=new_val" in content


# ── GET /api/config/env ──────────────────────────────────────────────────────


class TestGetEnvVars:
    def test_returns_200(self, client):
        resp = client.get("/api/config/env")
        assert resp.status_code == 200
        data = resp.json()
        assert "env_vars" in data
        assert "env_file_path" in data
        assert "env_file_exists" in data

    def test_env_vars_have_expected_fields(self, client):
        resp = client.get("/api/config/env")
        data = resp.json()
        for ev in data["env_vars"]:
            assert "env_name" in ev
            assert "is_set" in ev
            assert "in_env_file" in ev


# ── POST /api/config/env ─────────────────────────────────────────────────────


class TestSaveEnvVars:
    def test_requires_csrf(self, client):
        resp = client.post(
            "/api/config/env",
            json={"env_vars": {"SOME_KEY": "value"}},
        )
        assert resp.status_code == 403

    def test_rejects_empty_body(self, client, token):
        resp = client.post(
            "/api/config/env",
            json={"env_vars": {}},
            headers={"X-DVAD-Token": token},
        )
        assert resp.status_code == 400

    def test_rejects_unknown_env_var(self, client, token):
        resp = client.post(
            "/api/config/env",
            json={"env_vars": {"TOTALLY_FAKE_KEY_XYZ": "value"}},
            headers={"X-DVAD-Token": token},
        )
        assert resp.status_code == 400
        assert "Unknown" in resp.json()["detail"]


# ── get_config_health ────────────────────────────────────────────────────────


class TestGetConfigHealth:
    def test_no_errors_returns_false(self):
        from devils_advocate.config import get_config_health
        # Build a minimal valid config dict with mock models
        mock_model = MagicMock()
        mock_model.roles = {"author"}
        mock_model.deduplication = False
        mock_model.integration_reviewer = False
        mock_model.api_key = "sk-test"
        mock_model.api_key_env = "TEST_KEY"
        mock_model.context_window = 128000
        mock_model.cost_per_1k_input = 0.01
        mock_model.cost_per_1k_output = 0.03
        mock_model.name = "author-model"

        mock_reviewer1 = MagicMock()
        mock_reviewer1.roles = {"reviewer"}
        mock_reviewer1.deduplication = False
        mock_reviewer1.integration_reviewer = False
        mock_reviewer1.api_key = "sk-test"
        mock_reviewer1.api_key_env = "TEST_KEY"
        mock_reviewer1.context_window = 128000
        mock_reviewer1.cost_per_1k_input = 0.01
        mock_reviewer1.cost_per_1k_output = 0.03
        mock_reviewer1.name = "reviewer1"

        mock_reviewer2 = MagicMock()
        mock_reviewer2.roles = {"reviewer"}
        mock_reviewer2.deduplication = False
        mock_reviewer2.integration_reviewer = False
        mock_reviewer2.api_key = "sk-test"
        mock_reviewer2.api_key_env = "TEST_KEY"
        mock_reviewer2.context_window = 128000
        mock_reviewer2.cost_per_1k_input = 0.01
        mock_reviewer2.cost_per_1k_output = 0.03
        mock_reviewer2.name = "reviewer2"

        mock_dedup = MagicMock()
        mock_dedup.roles = set()
        mock_dedup.deduplication = True
        mock_dedup.integration_reviewer = False
        mock_dedup.api_key = "sk-test"
        mock_dedup.api_key_env = "TEST_KEY"
        mock_dedup.context_window = 128000
        mock_dedup.cost_per_1k_input = 0.01
        mock_dedup.cost_per_1k_output = 0.03
        mock_dedup.name = "dedup-model"

        mock_integ = MagicMock()
        mock_integ.roles = set()
        mock_integ.deduplication = False
        mock_integ.integration_reviewer = True
        mock_integ.api_key = "sk-test"
        mock_integ.api_key_env = "TEST_KEY"
        mock_integ.context_window = 128000
        mock_integ.cost_per_1k_input = 0.01
        mock_integ.cost_per_1k_output = 0.03
        mock_integ.name = "integ-model"

        config = {
            "models": {
                "author-model": mock_model,
                "reviewer1": mock_reviewer1,
                "reviewer2": mock_reviewer2,
                "dedup-model": mock_dedup,
                "integ-model": mock_integ,
            }
        }
        has_errors, summary = get_config_health(config)
        assert has_errors is False
        assert summary == ""

    def test_errors_returns_true(self):
        from devils_advocate.config import get_config_health
        config = {"models": {}}
        has_errors, summary = get_config_health(config)
        assert has_errors is True
        assert summary != ""
