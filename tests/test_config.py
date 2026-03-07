"""Tests for devils_advocate.config module."""

import textwrap
from pathlib import Path

import pytest
import yaml

from devils_advocate.config import (
    find_config,
    get_models_by_role,
    load_config,
    validate_config_structure,
    validate_review_readiness,
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

    def test_missing_roles_block_loads_empty(self, tmp_path):
        no_roles_yaml = textwrap.dedent("""\
            models:
              test-model:
                provider: openai
                model_id: gpt-test
                api_key_env: TEST_KEY
        """)
        cfg_path = _write_yaml(tmp_path / "noroles.yaml", no_roles_yaml)
        config = load_config(path=cfg_path)
        assert config["models"] == {}

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
    """Tests for validate_config_structure() constraint checking."""

    def _load_valid(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TEST_KEY", "fake-key-123")
        cfg_path = _write_yaml(tmp_path / "models.yaml")
        return load_config(path=cfg_path)

    def test_fewer_than_2_reviewers_no_structural_error(self, tmp_path, monkeypatch):
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
        # Structural validation no longer checks reviewer count
        issues = validate_config_structure(config)
        errors = [msg for level, msg in issues if level == "error"]
        assert not any("reviewer" in e.lower() for e in errors)
        # Review readiness warns about 1 reviewer
        readiness = validate_review_readiness(config, "plan")
        warnings = [msg for level, msg in readiness if level == "warn"]
        assert any("1 reviewer" in w for w in warnings)

    def test_missing_api_keys_error(self, tmp_path, monkeypatch):
        # Do NOT set TEST_KEY so api_key returns ""
        monkeypatch.delenv("TEST_KEY", raising=False)
        cfg_path = _write_yaml(tmp_path / "models.yaml")
        config = load_config(path=cfg_path)
        issues = validate_config_structure(config)
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
        issues = validate_config_structure(config)
        errors = [msg for level, msg in issues if level == "error"]
        assert errors == []

    def test_missing_author_no_structural_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TEST_KEY", "fake-key-123")
        no_author_yaml = textwrap.dedent("""\
            models:
              reviewer1:
                provider: openai
                model_id: gpt-test
                api_key_env: TEST_KEY
                context_window: 128000
                cost_per_1k_input: 0.005
                cost_per_1k_output: 0.015
              reviewer2:
                provider: openai
                model_id: gemini-test
                api_key_env: TEST_KEY
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
              reviewers:
                - reviewer1
                - reviewer2
              deduplication: dedup-model
              integration_reviewer: reviewer1
        """)
        cfg_path = _write_yaml(tmp_path / "no_author.yaml", no_author_yaml)
        config = load_config(path=cfg_path)
        # Structural validation no longer checks author count
        issues = validate_config_structure(config)
        errors = [msg for level, msg in issues if level == "error"]
        assert not any("author" in e.lower() for e in errors)
        # Review readiness catches missing author
        readiness = validate_review_readiness(config, "plan")
        r_errors = [msg for level, msg in readiness if level == "error"]
        assert any("Author" in e for e in r_errors)

    def test_missing_dedup_no_structural_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TEST_KEY", "fake-key-123")
        no_dedup_yaml = textwrap.dedent("""\
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
              reviewer2:
                provider: openai
                model_id: gemini-test
                api_key_env: TEST_KEY
                context_window: 1000000
                cost_per_1k_input: 0.001
                cost_per_1k_output: 0.004
            roles:
              author: author-model
              reviewers:
                - reviewer1
                - reviewer2
              integration_reviewer: reviewer1
        """)
        cfg_path = _write_yaml(tmp_path / "no_dedup.yaml", no_dedup_yaml)
        config = load_config(path=cfg_path)
        # Structural validation no longer checks dedup presence
        issues = validate_config_structure(config)
        errors = [msg for level, msg in issues if level == "error"]
        assert not any("dedup" in e.lower() for e in errors)
        # Review readiness catches missing dedup (config has 2 reviewers)
        readiness = validate_review_readiness(config, "plan")
        r_errors = [msg for level, msg in readiness if level == "error"]
        assert any("Dedup" in e for e in r_errors)

    def test_missing_integration_reviewer_no_structural_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TEST_KEY", "fake-key-123")
        no_integ_yaml = textwrap.dedent("""\
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
              reviewer2:
                provider: openai
                model_id: gemini-test
                api_key_env: TEST_KEY
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
        """)
        cfg_path = _write_yaml(tmp_path / "no_integ.yaml", no_integ_yaml)
        config = load_config(path=cfg_path)
        # Structural validation no longer checks integration_reviewer count
        issues = validate_config_structure(config)
        errors = [msg for level, msg in issues if level == "error"]
        assert not any("integration_reviewer" in e.lower() for e in errors)
        # Review readiness catches missing integration_reviewer
        readiness = validate_review_readiness(config, "integration")
        r_errors = [msg for level, msg in readiness if level == "error"]
        assert any("Integration" in e for e in r_errors)

    def test_dedup_same_as_author_warns_structurally(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TEST_KEY", "fake-key-123")
        dedup_is_author_yaml = textwrap.dedent("""\
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
              reviewer2:
                provider: openai
                model_id: gemini-test
                api_key_env: TEST_KEY
                context_window: 1000000
                cost_per_1k_input: 0.001
                cost_per_1k_output: 0.004
            roles:
              author: author-model
              reviewers:
                - reviewer1
                - reviewer2
              deduplication: author-model
              integration_reviewer: reviewer1
        """)
        cfg_path = _write_yaml(tmp_path / "dedup_author.yaml", dedup_is_author_yaml)
        config = load_config(path=cfg_path)
        # Structural validation now warns (not errors) about author-dedup collision
        issues = validate_config_structure(config)
        errors = [msg for level, msg in issues if level == "error"]
        warnings = [msg for level, msg in issues if level == "warn"]
        assert not any("author" in e.lower() and "dedup" in e.lower() for e in errors)
        assert any("NOT be the author" in w for w in warnings)
        # Review readiness catches it as an error
        readiness = validate_review_readiness(config, "plan")
        r_errors = [msg for level, msg in readiness if level == "error"]
        assert any("must NOT be the author" in e for e in r_errors)


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

    def test_revision_defaults_to_author(self, tmp_path, monkeypatch):
        config = self._load_valid(tmp_path, monkeypatch)
        roles = get_models_by_role(config)
        assert roles["revision"] is roles["author"]

    def test_revision_uses_explicit_model(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TEST_KEY", "fake-key-123")
        yaml_with_revision = textwrap.dedent("""\
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
              revision-model:
                provider: anthropic
                model_id: revision-test
                api_key_env: TEST_KEY
                context_window: 200000
                cost_per_1k_input: 0.003
                cost_per_1k_output: 0.015
            roles:
              author: author-model
              reviewers:
                - reviewer1
                - reviewer2
              deduplication: dedup-model
              integration_reviewer: reviewer1
              revision: revision-model
        """)
        cfg_path = _write_yaml(tmp_path / "rev.yaml", yaml_with_revision)
        config = load_config(path=cfg_path)
        roles = get_models_by_role(config)
        assert roles["revision"].name == "revision-model"
        assert roles["revision"] is not roles["author"]

    def test_bare_minimum_3_model_config(self, tmp_path, monkeypatch):
        """3 models, no normalization/revision roles, model reuse across roles.

        model-a = author
        model-b, model-c = reviewers
        model-b doubles as dedup + integration_reviewer
        Normalization and revision are absent (default to dedup and author).
        Should pass validation with zero errors.
        """
        monkeypatch.setenv("TEST_KEY", "fake-key-123")
        minimal_yaml = textwrap.dedent("""\
            models:
              model-a:
                provider: anthropic
                model_id: claude-test
                api_key_env: TEST_KEY
                context_window: 200000
                cost_per_1k_input: 0.003
                cost_per_1k_output: 0.015
              model-b:
                provider: openai
                model_id: gpt-test
                api_key_env: TEST_KEY
                context_window: 128000
                cost_per_1k_input: 0.005
                cost_per_1k_output: 0.015
              model-c:
                provider: openai
                model_id: gemini-test
                api_key_env: TEST_KEY
                context_window: 1000000
                cost_per_1k_input: 0.001
                cost_per_1k_output: 0.004
            roles:
              author: model-a
              reviewers:
                - model-b
                - model-c
              deduplication: model-b
              integration_reviewer: model-b
        """)
        cfg_path = _write_yaml(tmp_path / "minimal.yaml", minimal_yaml)
        config = load_config(path=cfg_path)

        # Validation should produce zero errors
        issues = validate_config_structure(config)
        errors = [msg for level, msg in issues if level == "error"]
        assert errors == [], f"Expected no errors, got: {errors}"

        # Verify role assignments via get_models_by_role
        roles = get_models_by_role(config)
        assert roles["author"].name == "model-a"
        assert len(roles["reviewers"]) == 2
        assert {r.name for r in roles["reviewers"]} == {"model-b", "model-c"}
        assert roles["dedup"].name == "model-b"
        assert roles["integration"].name == "model-b"
        # Normalization defaults to dedup (model-b)
        assert roles["normalization"] is roles["dedup"]
        # Revision defaults to author (model-a)
        assert roles["revision"] is roles["author"]


# ─── TestEnabledField ───────────────────────────────────────────────────────


class TestEnabledField:
    """Tests for the model enabled/disabled feature."""

    def test_disabled_model_in_reviewers_raises(self, tmp_path, monkeypatch):
        """Referencing a disabled model in roles should raise ConfigError."""
        monkeypatch.setenv("FAKE_KEY", "sk-fake")
        cfg_path = _write_yaml(tmp_path / "dis.yaml", textwrap.dedent("""\
            models:
              active-model:
                provider: anthropic
                model_id: claude-test
                api_key_env: FAKE_KEY
              disabled-model:
                provider: anthropic
                model_id: claude-test-2
                api_key_env: FAKE_KEY
                enabled: false
            roles:
              author: active-model
              reviewers:
                - disabled-model
              deduplication: active-model
        """))
        with pytest.raises(ConfigError, match="disabled model"):
            load_config(cfg_path)

    def test_disabled_model_in_author_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("FAKE_KEY", "sk-fake")
        cfg_path = _write_yaml(tmp_path / "dis2.yaml", textwrap.dedent("""\
            models:
              disabled-model:
                provider: anthropic
                model_id: claude-test
                api_key_env: FAKE_KEY
                enabled: false
              other-model:
                provider: anthropic
                model_id: claude-test-2
                api_key_env: FAKE_KEY
            roles:
              author: disabled-model
              reviewers:
                - other-model
              deduplication: other-model
        """))
        with pytest.raises(ConfigError, match="disabled model"):
            load_config(cfg_path)

    def test_enabled_true_model_works(self, tmp_path, monkeypatch):
        """Explicitly enabled model should load without error."""
        monkeypatch.setenv("FAKE_KEY", "sk-fake")
        cfg_path = _write_yaml(tmp_path / "en.yaml", textwrap.dedent("""\
            models:
              my-model:
                provider: anthropic
                model_id: claude-test
                api_key_env: FAKE_KEY
                enabled: true
            roles:
              author: my-model
              reviewers:
                - my-model
              deduplication: my-model
        """))
        config = load_config(cfg_path)
        assert "my-model" in config["all_models"]

    def test_enabled_defaults_true(self, tmp_path, monkeypatch):
        """Omitting enabled should default to True (model is active)."""
        monkeypatch.setenv("FAKE_KEY", "sk-fake")
        cfg_path = _write_yaml(tmp_path / "def.yaml", textwrap.dedent("""\
            models:
              my-model:
                provider: anthropic
                model_id: claude-test
                api_key_env: FAKE_KEY
            roles:
              author: my-model
              reviewers:
                - my-model
              deduplication: my-model
        """))
        config = load_config(cfg_path)
        assert config["all_models"]["my-model"].enabled is True


# ─── TestValidateReviewReadiness ─────────────────────────────────────────────


class TestValidateReviewReadiness:
    """Tests for validate_review_readiness() mode-specific role checks."""

    def _load(self, tmp_path, yaml_text, monkeypatch):
        monkeypatch.setenv("TEST_KEY", "fake-key")
        cfg_path = tmp_path / "models.yaml"
        cfg_path.write_text(textwrap.dedent(yaml_text))
        return load_config(cfg_path)

    def test_plan_full_config_no_errors(self, tmp_path, monkeypatch):
        config = self._load(tmp_path, VALID_YAML, monkeypatch)
        issues = validate_review_readiness(config, "plan")
        errors = [msg for lvl, msg in issues if lvl == "error"]
        assert errors == []

    def test_plan_no_author(self, tmp_path, monkeypatch):
        yaml_text = """\
            models:
              rev1:
                provider: openai
                model_id: test
                api_key_env: TEST_KEY
              rev2:
                provider: openai
                model_id: test
                api_key_env: TEST_KEY
              dedup:
                provider: openai
                model_id: test
                api_key_env: TEST_KEY
            roles:
              reviewers:
                - rev1
                - rev2
              deduplication: dedup
        """
        config = self._load(tmp_path, yaml_text, monkeypatch)
        issues = validate_review_readiness(config, "plan")
        errors = [msg for lvl, msg in issues if lvl == "error"]
        assert any("Author" in e for e in errors)

    def test_plan_one_reviewer_warns(self, tmp_path, monkeypatch):
        yaml_text = """\
            models:
              author:
                provider: openai
                model_id: test
                api_key_env: TEST_KEY
              rev1:
                provider: openai
                model_id: test
                api_key_env: TEST_KEY
              dedup:
                provider: openai
                model_id: test
                api_key_env: TEST_KEY
            roles:
              author: author
              reviewers:
                - rev1
              deduplication: dedup
        """
        config = self._load(tmp_path, yaml_text, monkeypatch)
        issues = validate_review_readiness(config, "plan")
        errors = [msg for lvl, msg in issues if lvl == "error"]
        warnings = [msg for lvl, msg in issues if lvl == "warn"]
        assert errors == []
        assert any("adversarial" in w.lower() or "1 reviewer" in w for w in warnings)

    def test_plan_two_reviewers_no_dedup_errors(self, tmp_path, monkeypatch):
        yaml_text = """\
            models:
              author:
                provider: openai
                model_id: test
                api_key_env: TEST_KEY
              rev1:
                provider: openai
                model_id: test
                api_key_env: TEST_KEY
              rev2:
                provider: openai
                model_id: test
                api_key_env: TEST_KEY
            roles:
              author: author
              reviewers:
                - rev1
                - rev2
        """
        config = self._load(tmp_path, yaml_text, monkeypatch)
        issues = validate_review_readiness(config, "plan")
        errors = [msg for lvl, msg in issues if lvl == "error"]
        assert any("Dedup" in e for e in errors)

    def test_plan_one_reviewer_no_dedup_ok(self, tmp_path, monkeypatch):
        """With 1 reviewer and normalization covered, no dedup error is raised."""
        yaml_text = """\
            models:
              author:
                provider: openai
                model_id: test
                api_key_env: TEST_KEY
              rev1:
                provider: openai
                model_id: test
                api_key_env: TEST_KEY
              norm:
                provider: openai
                model_id: test
                api_key_env: TEST_KEY
            roles:
              author: author
              reviewers:
                - rev1
              normalization: norm
        """
        config = self._load(tmp_path, yaml_text, monkeypatch)
        issues = validate_review_readiness(config, "plan")
        errors = [msg for lvl, msg in issues if lvl == "error"]
        assert not any("Dedup" in e for e in errors)

    def test_code_same_as_plan(self, tmp_path, monkeypatch):
        yaml_text = """\
            models:
              rev1:
                provider: openai
                model_id: test
                api_key_env: TEST_KEY
            roles:
              reviewers:
                - rev1
        """
        config = self._load(tmp_path, yaml_text, monkeypatch)
        issues = validate_review_readiness(config, "code")
        errors = [msg for lvl, msg in issues if lvl == "error"]
        assert any("Author" in e for e in errors)

    def test_spec_no_reviewers_errors(self, tmp_path, monkeypatch):
        yaml_text = """\
            models:
              author:
                provider: openai
                model_id: test
                api_key_env: TEST_KEY
            roles:
              author: author
        """
        config = self._load(tmp_path, yaml_text, monkeypatch)
        issues = validate_review_readiness(config, "spec")
        errors = [msg for lvl, msg in issues if lvl == "error"]
        assert any("Reviewer" in e for e in errors)

    def test_spec_no_author_no_error(self, tmp_path, monkeypatch):
        yaml_text = """\
            models:
              rev1:
                provider: openai
                model_id: test
                api_key_env: TEST_KEY
              rev2:
                provider: openai
                model_id: test
                api_key_env: TEST_KEY
              dedup:
                provider: openai
                model_id: test
                api_key_env: TEST_KEY
            roles:
              reviewers:
                - rev1
                - rev2
              deduplication: dedup
              revision: rev1
        """
        config = self._load(tmp_path, yaml_text, monkeypatch)
        issues = validate_review_readiness(config, "spec")
        errors = [msg for lvl, msg in issues if lvl == "error"]
        assert not any("requires an Author" in e for e in errors)

    def test_integration_no_integration_reviewer_errors(self, tmp_path, monkeypatch):
        yaml_text = """\
            models:
              author:
                provider: openai
                model_id: test
                api_key_env: TEST_KEY
            roles:
              author: author
        """
        config = self._load(tmp_path, yaml_text, monkeypatch)
        issues = validate_review_readiness(config, "integration")
        errors = [msg for lvl, msg in issues if lvl == "error"]
        assert any("Integration" in e for e in errors)

    def test_integration_no_reviewers_no_error(self, tmp_path, monkeypatch):
        yaml_text = """\
            models:
              author:
                provider: openai
                model_id: test
                api_key_env: TEST_KEY
              integ:
                provider: openai
                model_id: test
                api_key_env: TEST_KEY
            roles:
              author: author
              integration_reviewer: integ
        """
        config = self._load(tmp_path, yaml_text, monkeypatch)
        issues = validate_review_readiness(config, "integration")
        errors = [msg for lvl, msg in issues if lvl == "error"]
        assert not any("Reviewer" in e for e in errors)

    def test_revision_fallback_to_author(self, tmp_path, monkeypatch):
        """No explicit revision role but author assigned - no revision error."""
        yaml_text = """\
            models:
              author:
                provider: openai
                model_id: test
                api_key_env: TEST_KEY
              rev1:
                provider: openai
                model_id: test
                api_key_env: TEST_KEY
            roles:
              author: author
              reviewers:
                - rev1
        """
        config = self._load(tmp_path, yaml_text, monkeypatch)
        issues = validate_review_readiness(config, "plan")
        errors = [msg for lvl, msg in issues if lvl == "error"]
        assert not any("Revision" in e for e in errors)

    def test_no_revision_no_author_warns(self, tmp_path, monkeypatch):
        """Missing revision (with no author fallback) produces a warning, not error."""
        yaml_text = """\
            models:
              rev1:
                provider: openai
                model_id: test
                api_key_env: TEST_KEY
            roles:
              reviewers:
                - rev1
        """
        config = self._load(tmp_path, yaml_text, monkeypatch)
        issues = validate_review_readiness(config, "integration")
        warnings = [msg for lvl, msg in issues if lvl == "warn"]
        assert any("Revision" in w for w in warnings)
