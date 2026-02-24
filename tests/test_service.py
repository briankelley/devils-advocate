"""Tests for devils_advocate.service module."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from devils_advocate.service import (
    DEFAULT_PORT,
    SERVICE_NAME,
    SERVICE_TEMPLATE,
    check_gui_deps,
    check_platform,
    detect_dvad_binary,
    read_existing_service,
    remove_service_file,
    render_service_unit,
    service_exists,
    service_file_path,
    systemctl_daemon_reload,
    systemctl_disable,
    systemctl_enable,
    systemctl_is_active,
    systemctl_is_enabled,
    systemctl_start,
    systemctl_stop,
    write_service_file,
    _run_systemctl,
)


# ─── TestCheckPlatform ─────────────────────────────────────────────────────


class TestCheckPlatform:
    """Tests for check_platform()."""

    def test_linux_returns_none(self):
        with patch.object(sys, "platform", "linux"):
            assert check_platform() is None

    def test_linux_variant_returns_none(self):
        with patch.object(sys, "platform", "linux2"):
            assert check_platform() is None

    def test_darwin_returns_error(self):
        with patch.object(sys, "platform", "darwin"):
            result = check_platform()
            assert result is not None
            assert "darwin" in result

    def test_win32_returns_error(self):
        with patch.object(sys, "platform", "win32"):
            result = check_platform()
            assert result is not None
            assert "win32" in result


# ─── TestDetectDvadBinary ──────────────────────────────────────────────────


class TestDetectDvadBinary:
    """Tests for detect_dvad_binary()."""

    def test_sibling_strategy(self, tmp_path):
        """If dvad exists next to sys.executable, it's returned."""
        fake_python = tmp_path / "python"
        fake_python.touch()
        fake_dvad = tmp_path / "dvad"
        fake_dvad.touch()

        with patch.object(sys, "executable", str(fake_python)):
            result = detect_dvad_binary()
        assert result == fake_dvad

    def test_which_fallback(self, tmp_path):
        """If sibling not found, shutil.which is tried."""
        fake_python = tmp_path / "python"
        fake_python.touch()
        # No dvad sibling

        with patch.object(sys, "executable", str(fake_python)), \
             patch("devils_advocate.service.shutil.which", return_value="/usr/bin/dvad"):
            result = detect_dvad_binary()
        assert result == Path("/usr/bin/dvad")

    def test_not_found_raises(self, tmp_path):
        """If neither strategy works, FileNotFoundError is raised."""
        fake_python = tmp_path / "python"
        fake_python.touch()

        with patch.object(sys, "executable", str(fake_python)), \
             patch("devils_advocate.service.shutil.which", return_value=None):
            with pytest.raises(FileNotFoundError, match="Could not locate"):
                detect_dvad_binary()


# ─── TestCheckGuiDeps ──────────────────────────────────────────────────────


class TestCheckGuiDeps:
    """Tests for check_gui_deps()."""

    def test_all_available_returns_none(self):
        """When both fastapi and uvicorn are importable, returns None."""
        with patch.dict("sys.modules", {"fastapi": MagicMock(), "uvicorn": MagicMock()}):
            assert check_gui_deps() is None

    def test_fastapi_missing(self):
        """Missing fastapi is reported."""
        import builtins
        original_import = builtins.__import__

        def selective_import(name, *args, **kwargs):
            if name == "fastapi":
                raise ImportError("No module named 'fastapi'")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=selective_import):
            result = check_gui_deps()
        assert result is not None
        assert "fastapi" in result

    def test_uvicorn_missing(self):
        """Missing uvicorn is reported."""
        import builtins
        original_import = builtins.__import__

        def selective_import(name, *args, **kwargs):
            if name == "uvicorn":
                raise ImportError("No module named 'uvicorn'")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=selective_import):
            result = check_gui_deps()
        assert result is not None
        assert "uvicorn" in result


# ─── TestRenderServiceUnit ─────────────────────────────────────────────────


class TestRenderServiceUnit:
    """Tests for render_service_unit()."""

    def test_default_port(self):
        content = render_service_unit("/usr/bin/dvad")
        assert "ExecStart=/usr/bin/dvad gui --port 8411" in content

    def test_custom_port(self):
        content = render_service_unit("/usr/bin/dvad", port=9000)
        assert "ExecStart=/usr/bin/dvad gui --port 9000" in content

    def test_expected_directives(self):
        content = render_service_unit("/usr/bin/dvad")
        assert "[Unit]" in content
        assert "[Service]" in content
        assert "[Install]" in content
        assert "Restart=on-failure" in content
        assert "KillSignal=SIGINT" in content
        assert "WantedBy=default.target" in content

    def test_path_object_accepted(self):
        content = render_service_unit(Path("/usr/bin/dvad"))
        assert "ExecStart=/usr/bin/dvad gui --port 8411" in content


# ─── TestServiceFileOps ───────────────────────────────────────────────────


class TestServiceFileOps:
    """Tests for service file read/write/remove operations."""

    def test_write_read_roundtrip(self, tmp_path):
        """Write and read back produces identical content."""
        fake_path = tmp_path / ".config" / "systemd" / "user" / SERVICE_NAME
        content = render_service_unit("/usr/bin/dvad")

        with patch("devils_advocate.service.service_file_path", return_value=fake_path):
            written = write_service_file(content)
            assert written == fake_path
            assert written.exists()

            result = read_existing_service()
            assert result == content

    def test_write_creates_parents(self, tmp_path):
        """write_service_file creates parent directories."""
        deep_path = tmp_path / "a" / "b" / "c" / SERVICE_NAME
        with patch("devils_advocate.service.service_file_path", return_value=deep_path):
            write_service_file("test content")
        assert deep_path.exists()

    def test_service_exists_true(self, tmp_path):
        """service_exists returns True when file is present."""
        fake_path = tmp_path / SERVICE_NAME
        fake_path.write_text("content")
        with patch("devils_advocate.service.service_file_path", return_value=fake_path):
            assert service_exists() is True

    def test_service_exists_false(self, tmp_path):
        """service_exists returns False when file is absent."""
        fake_path = tmp_path / SERVICE_NAME
        with patch("devils_advocate.service.service_file_path", return_value=fake_path):
            assert service_exists() is False

    def test_read_nonexistent_returns_none(self, tmp_path):
        """read_existing_service returns None when no file."""
        fake_path = tmp_path / SERVICE_NAME
        with patch("devils_advocate.service.service_file_path", return_value=fake_path):
            assert read_existing_service() is None

    def test_remove_existing(self, tmp_path):
        """remove_service_file removes and returns True."""
        fake_path = tmp_path / SERVICE_NAME
        fake_path.write_text("content")
        with patch("devils_advocate.service.service_file_path", return_value=fake_path):
            assert remove_service_file() is True
            assert not fake_path.exists()

    def test_remove_nonexistent(self, tmp_path):
        """remove_service_file returns False when no file."""
        fake_path = tmp_path / SERVICE_NAME
        with patch("devils_advocate.service.service_file_path", return_value=fake_path):
            assert remove_service_file() is False


# ─── TestSystemctlWrappers ────────────────────────────────────────────────


class TestSystemctlWrappers:
    """Tests for systemctl wrapper functions."""

    def test_run_systemctl_success(self):
        """Successful systemctl call returns CompletedProcess."""
        mock_result = subprocess.CompletedProcess(
            args=["systemctl", "--user", "daemon-reload"],
            returncode=0, stdout="", stderr="",
        )
        with patch("devils_advocate.service.subprocess.run", return_value=mock_result):
            result = _run_systemctl("daemon-reload")
            assert result.returncode == 0

    def test_run_systemctl_failure_raises(self):
        """Failed systemctl call raises RuntimeError."""
        mock_result = subprocess.CompletedProcess(
            args=["systemctl", "--user", "start", SERVICE_NAME],
            returncode=1, stdout="", stderr="Unit not found.",
        )
        with patch("devils_advocate.service.subprocess.run", return_value=mock_result):
            with pytest.raises(RuntimeError, match="Unit not found"):
                _run_systemctl("start", SERVICE_NAME)

    def test_daemon_reload_calls_systemctl(self):
        """systemctl_daemon_reload calls the right command."""
        with patch("devils_advocate.service._run_systemctl") as mock:
            systemctl_daemon_reload()
            mock.assert_called_once_with("daemon-reload")

    def test_enable_calls_systemctl(self):
        with patch("devils_advocate.service._run_systemctl") as mock:
            systemctl_enable()
            mock.assert_called_once_with("enable", SERVICE_NAME)

    def test_start_calls_systemctl(self):
        with patch("devils_advocate.service._run_systemctl") as mock:
            systemctl_start()
            mock.assert_called_once_with("start", SERVICE_NAME)

    def test_stop_calls_systemctl(self):
        with patch("devils_advocate.service._run_systemctl") as mock:
            systemctl_stop()
            mock.assert_called_once_with("stop", SERVICE_NAME)

    def test_disable_calls_systemctl(self):
        with patch("devils_advocate.service._run_systemctl") as mock:
            systemctl_disable()
            mock.assert_called_once_with("disable", SERVICE_NAME)

    def test_is_active_true(self):
        """is_active returns True on rc=0."""
        mock_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="active\n", stderr="",
        )
        with patch("devils_advocate.service.subprocess.run", return_value=mock_result):
            assert systemctl_is_active() is True

    def test_is_active_false(self):
        """is_active returns False on rc!=0."""
        mock_result = subprocess.CompletedProcess(
            args=[], returncode=3, stdout="inactive\n", stderr="",
        )
        with patch("devils_advocate.service.subprocess.run", return_value=mock_result):
            assert systemctl_is_active() is False

    def test_is_enabled_true(self):
        """is_enabled returns True on rc=0."""
        mock_result = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="enabled\n", stderr="",
        )
        with patch("devils_advocate.service.subprocess.run", return_value=mock_result):
            assert systemctl_is_enabled() is True

    def test_is_enabled_false(self):
        """is_enabled returns False on rc!=0."""
        mock_result = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="disabled\n", stderr="",
        )
        with patch("devils_advocate.service.subprocess.run", return_value=mock_result):
            assert systemctl_is_enabled() is False

    def test_is_active_exception_returns_false(self):
        """is_active returns False if subprocess.run raises."""
        with patch("devils_advocate.service.subprocess.run", side_effect=OSError("no such command")):
            assert systemctl_is_active() is False

    def test_is_enabled_exception_returns_false(self):
        """is_enabled returns False if subprocess.run raises."""
        with patch("devils_advocate.service.subprocess.run", side_effect=OSError("no such command")):
            assert systemctl_is_enabled() is False
