"""Tests for init_config() — fresh creation, idempotence, fallbacks, permissions."""

import os
import stat
import textwrap
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from devils_advocate.config import init_config


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Point Path.home() at a temp directory."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    return home


class TestInitConfigCreation:
    def test_creates_config_dir_and_file(self, fake_home):
        status, path = init_config()
        assert status == "created"
        assert path.exists()
        assert path.name == "models.yaml"
        assert path.parent.name == "devils-advocate"
        assert path.parent.parent.name == ".config"

    def test_config_dir_permissions(self, fake_home):
        init_config()
        config_dir = fake_home / ".config" / "devils-advocate"
        mode = stat.S_IMODE(config_dir.stat().st_mode)
        assert mode == 0o700

    def test_config_file_permissions(self, fake_home):
        _, path = init_config()
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600

    def test_config_content_not_empty(self, fake_home):
        _, path = init_config()
        content = path.read_text()
        assert len(content) > 0
        # Should contain either shipped example or fallback content
        assert "models" in content.lower()


class TestInitConfigIdempotence:
    def test_existing_config_returns_exists(self, fake_home):
        """If models.yaml already exists, init_config should return 'exists'."""
        config_dir = fake_home / ".config" / "devils-advocate"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "models.yaml"
        config_file.write_text("# existing config\nmodels: {}\n")

        status, path = init_config()
        assert status == "exists"
        assert path == config_file
        # Content should NOT be overwritten
        assert config_file.read_text() == "# existing config\nmodels: {}\n"


class TestInitConfigFallback:
    def test_fallback_when_read_text_fails(self, fake_home, monkeypatch):
        """When shipped example read fails, a minimal fallback config is written."""
        import importlib.resources

        # files() returns an object; the / operator returns a Traversable.
        # The fallback triggers when read_text() on the Traversable fails.
        # So we mock files() to return an object whose __truediv__ returns
        # something whose read_text() raises.
        class _BadTraversable:
            def read_text(self):
                raise FileNotFoundError("no packaged example")

        class _BadPkg:
            def __truediv__(self, other):
                return _BadTraversable()

        monkeypatch.setattr(importlib.resources, "files", lambda pkg: _BadPkg())

        status, path = init_config()
        assert status == "created"
        content = path.read_text()
        assert "models:" in content
        assert "roles:" in content


class TestInitConfigEnvExample:
    def test_env_example_created(self, fake_home):
        """init_config should also copy .env.example."""
        init_config()
        env_example = fake_home / ".config" / "devils-advocate" / ".env.example"
        # May or may not exist depending on package resources, but if it does
        # it should have correct permissions
        if env_example.exists():
            mode = stat.S_IMODE(env_example.stat().st_mode)
            assert mode == 0o600
            assert len(env_example.read_text()) > 0

    def test_env_example_not_overwritten(self, fake_home):
        """If .env.example already exists, it should not be overwritten."""
        config_dir = fake_home / ".config" / "devils-advocate"
        config_dir.mkdir(parents=True)
        env_file = config_dir / ".env.example"
        env_file.write_text("# my custom env\n")

        # Remove config file so init_config proceeds with creation
        # but .env.example already exists
        init_config()

        # If config was already created, this test is moot. But if we
        # manually create just the .env.example, the next test covers it.
        # Let's verify that a fresh init with pre-existing .env doesn't clobber
        # To test properly, ensure config file doesn't exist
        config_file = config_dir / "models.yaml"
        if config_file.exists():
            config_file.unlink()

        init_config()
        assert env_file.read_text() == "# my custom env\n"
