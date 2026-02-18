"""Tests for devils_advocate.config module."""

import textwrap
from pathlib import Path

import pytest
import yaml

from devils_advocate.config import (
    find_config,
    get_models_by_role,
    load_config,
    validate_config,
)
from devils_advocate.types import ConfigError


# ─── Shared YAML helpers ─────────────────────────────────────────────────────

VALID_YAML = textwrap.dedent("""\
    models:
      author-model:
        provider: anthropic
        model_id: claude-test
        api_key_env: TEST_KEY
        context_window: 200000
        cost_per_1k_input: 0.003
        cost_per_1k_output: 0.015
      reviewer1:
        provider: openai
        model_id: gpt-test
        api_key_env: TEST_KEY
        api_base: https://api.example.com/v1
        context_window: 128000
        cost_per_1k_input: 0.005
        cost_per_1k_output: 0.015
      reviewer2:
        provider: openai
        model_id: gemini-test
        api_key_env: TEST_KEY
        api_base: https://api.example.com/v1
        context_window: 1000000
        cost_per_1k_input: 0.001
        cost_per_1k_output: 0.004
      dedup-model:
        provider: anthropic
        model_id: haiku-test
        api_key_env: TEST_KEY
        context_window: 200000
        cost_per_1k_input: 0.001
        cost_per_1k_output: 0.004
    roles:
      author: author-model
      reviewers:
        - reviewer1
        - reviewer2
      deduplication: dedup-model
      integration_reviewer: reviewer1
""")


def _write_yaml(path: Path, content: str = VALID_YAML) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


# ─── TestFindConfig ──────────────────────────────────────────────────────────


class TestFindConfig:
    """Tests for find_config() path resolution."""

    def test_explicit_path_found(self, tmp_path):
        cfg = _write_yaml(tmp_path / "models.yaml")
        result = find_config(explicit=cfg)
        assert result == cfg

    def test_explicit_path_not_found(self, tmp_path):
        missing = tmp_path / "nonexistent.yaml"
        with pytest.raises(ConfigError, match="Explicit config not found"):
            find_config(explicit=missing)

    def test_project_local_found(self, tmp_path, monkeypatch):
        _write_yaml(tmp_path / "models.yaml")
        monkeypatch.chdir(tmp_path)
        # Clear env vars that could interfere
        monkeypatch.delenv("DVAD_HOME", raising=False)
        result = find_config()
        assert result == (tmp_path / "models.yaml").resolve()

    def test_dvad_home_found(self, tmp_path, monkeypatch):
        dvad_dir = tmp_path / "dvad_home"
        _write_yaml(dvad_dir / "models.yaml")
        monkeypatch.setenv("DVAD_HOME", str(dvad_dir))
        # Ensure project-local doesn't exist by changing to empty dir
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.chdir(empty)
        result = find_config()
        assert result == dvad_dir / "models.yaml"

    def test_xdg_default_found(self, tmp_path, monkeypatch):
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        xdg_path = fake_home / ".config" / "devils-advocate" / "models.yaml"
        _write_yaml(xdg_path)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
        monkeypatch.delenv("DVAD_HOME", raising=False)
        # Ensure project-local doesn't exist
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.chdir(empty)
        result = find_config()
        assert result == xdg_path

    def test_no_config_found_raises(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DVAD_HOME", raising=False)
        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.chdir(empty)
        # Point home to an empty dir so XDG path doesn't exist
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "nohome"))
        with pytest.raises(ConfigError, match="No models.yaml found.*Searched"):
            find_config()


# ─── TestLoadConfig ──────────────────────────────────────────────────────────


class TestLoadConfig:
    """Tests for load_config() parsing and validation."""

    def test_valid_config_parses(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TEST_KEY", "fake-key-123")
        cfg_path = _write_yaml(tmp_path / "models.yaml")
        config = load_config(path=cfg_path)
        assert "models" in config
        assert "all_models" in config
        assert "config_path" in config
        # Active models should include those referenced in roles
        models = config["models"]
        assert "author-model" in models
        assert "reviewer1" in models
        assert "reviewer2" in models
        assert "dedup-model" in models
        # Check role assignment
        assert "author" in models["author-model"].roles
        assert "reviewer" in models["reviewer1"].roles
        assert "reviewer" in models["reviewer2"].roles
        assert models["dedup-model"].deduplication is True
        assert models["reviewer1"].integration_reviewer is True

    def test_missing_models_key_raises(self, tmp_path):
        bad_yaml = "roles:\n  author: foo\n"
        cfg_path = _write_yaml(tmp_path / "bad.yaml", bad_yaml)
        with pytest.raises(ConfigError, match="'models' key missing"):
            load_config(path=cfg_path)

    def test_missing_roles_block_raises(self, tmp_path):
        no_roles_yaml = textwrap.dedent("""\
            models:
              test-model:
                provider: openai
                model_id: gpt-test
                api_key_env: TEST_KEY
        """)
        cfg_path = _write_yaml(tmp_path / "noroles.yaml", no_roles_yaml)
        with pytest.raises(ConfigError, match="missing 'roles' block"):
            load_config(path=cfg_path)

    def test_roles_referencing_unknown_model_raises(self, tmp_path):
        bad_ref_yaml = textwrap.dedent("""\
            models:
              real-model:
                provider: openai
                model_id: gpt-test
                api_key_env: TEST_KEY
            roles:
              author: nonexistent-model
        """)
        cfg_path = _write_yaml(tmp_path / "badref.yaml", bad_ref_yaml)
        with pytest.raises(ConfigError, match="references unknown model"):
            load_config(path=cfg_path)


# ─── TestValidateConfig ──────────────────────────────────────────────────────


class TestValidateConfig:
    """Tests for validate_config() constraint checking."""

    def _load_valid(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TEST_KEY", "fake-key-123")
        cfg_path = _write_yaml(tmp_path / "models.yaml")
        return load_config(path=cfg_path)

    def test_fewer_than_2_reviewers_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TEST_KEY", "fake-key-123")
        one_reviewer_yaml = textwrap.dedent("""\
            models:
              author-model:
                provider: anthropic
                model_id: claude-test
                api_key_env: TEST_KEY
                context_window: 200000
                cost_per_1k_input: 0.003
                cost_per_1k_output: 0.015
              reviewer1:
                provider: openai
                model_id: gpt-test
                api_key_env: TEST_KEY
                context_window: 128000
                cost_per_1k_input: 0.005
                cost_per_1k_output: 0.015
              dedup-model:
                provider: anthropic
                model_id: haiku-test
                api_key_env: TEST_KEY
                context_window: 200000
                cost_per_1k_input: 0.001
                cost_per_1k_output: 0.004
            roles:
              author: author-model
              reviewers:
                - reviewer1
              deduplication: dedup-model
              integration_reviewer: reviewer1
        """)
        cfg_path = _write_yaml(tmp_path / "one_rev.yaml", one_reviewer_yaml)
        config = load_config(path=cfg_path)
        issues = validate_config(config)
        errors = [msg for level, msg in issues if level == "error"]
        assert any("at least 2 reviewers" in e for e in errors)

    def test_missing_api_keys_error(self, tmp_path, monkeypatch):
        # Do NOT set TEST_KEY so api_key returns ""
        monkeypatch.delenv("TEST_KEY", raising=False)
        cfg_path = _write_yaml(tmp_path / "models.yaml")
        config = load_config(path=cfg_path)
        issues = validate_config(config)
        errors = [msg for level, msg in issues if level == "error"]
        assert any("empty or unset" in e for e in errors)

    def test_normalization_role_absent_no_error(self, tmp_path, monkeypatch):
        """When normalization is not set, get_models_by_role defaults gracefully."""
        config = self._load_valid(tmp_path, monkeypatch)
        roles = get_models_by_role(config)
        # normalization should fall back to dedup model, no error
        assert roles["normalization"] is not None
        assert roles["normalization"].deduplication is True

    def test_valid_config_no_errors(self, tmp_path, monkeypatch):
        config = self._load_valid(tmp_path, monkeypatch)
        issues = validate_config(config)
        errors = [msg for level, msg in issues if level == "error"]
        assert errors == []


# ─── TestGetModelsByRole ─────────────────────────────────────────────────────


class TestGetModelsByRole:
    """Tests for get_models_by_role() role extraction."""

    def _load_valid(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TEST_KEY", "fake-key-123")
        cfg_path = _write_yaml(tmp_path / "models.yaml")
        return load_config(path=cfg_path)

    def test_returns_correct_models_per_role(self, tmp_path, monkeypatch):
        config = self._load_valid(tmp_path, monkeypatch)
        roles = get_models_by_role(config)
        assert roles["author"].name == "author-model"
        assert len(roles["reviewers"]) == 2
        reviewer_names = {r.name for r in roles["reviewers"]}
        assert reviewer_names == {"reviewer1", "reviewer2"}
        assert roles["dedup"].name == "dedup-model"
        assert roles["integration"].name == "reviewer1"

    def test_normalization_defaults_to_dedup(self, tmp_path, monkeypatch):
        config = self._load_valid(tmp_path, monkeypatch)
        roles = get_models_by_role(config)
        assert roles["normalization"] is roles["dedup"]

    def test_normalization_uses_explicit_model(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TEST_KEY", "fake-key-123")
        yaml_with_norm = textwrap.dedent("""\
            models:
              author-model:
                provider: anthropic
                model_id: claude-test
                api_key_env: TEST_KEY
                context_window: 200000
                cost_per_1k_input: 0.003
                cost_per_1k_output: 0.015
              reviewer1:
                provider: openai
                model_id: gpt-test
                api_key_env: TEST_KEY
                api_base: https://api.example.com/v1
                context_window: 128000
                cost_per_1k_input: 0.005
                cost_per_1k_output: 0.015
              reviewer2:
                provider: openai
                model_id: gemini-test
                api_key_env: TEST_KEY
                api_base: https://api.example.com/v1
                context_window: 1000000
                cost_per_1k_input: 0.001
                cost_per_1k_output: 0.004
              dedup-model:
                provider: anthropic
                model_id: haiku-test
                api_key_env: TEST_KEY
                context_window: 200000
                cost_per_1k_input: 0.001
                cost_per_1k_output: 0.004
              norm-model:
                provider: anthropic
                model_id: norm-test
                api_key_env: TEST_KEY
                context_window: 200000
                cost_per_1k_input: 0.001
                cost_per_1k_output: 0.004
            roles:
              author: author-model
              reviewers:
                - reviewer1
                - reviewer2
              deduplication: dedup-model
              integration_reviewer: reviewer1
              normalization: norm-model
        """)
        cfg_path = _write_yaml(tmp_path / "norm.yaml", yaml_with_norm)
        config = load_config(path=cfg_path)
        roles = get_models_by_role(config)
        assert roles["normalization"].name == "norm-model"
        assert roles["normalization"] is not roles["dedup"]
