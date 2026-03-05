"""Systemd user-service management for the dvad GUI.

Pure logic module — no Click or Rich imports. All user-facing output
stays in cli.py.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

# ─── Constants ─────────────────────────────────────────────────────────────

SERVICE_NAME = "dvad-gui.service"
DEFAULT_PORT = 8411

SERVICE_TEMPLATE = """\
[Unit]
Description=Devil's Advocate Web GUI
Documentation=https://github.com/briankelley/devils-advocate
After=default.target

[Service]
Type=simple
ExecStart={dvad_bin} gui --port {port}
Restart=on-failure
RestartSec=5
KillSignal=SIGINT
TimeoutStopSec=10
SuccessExitStatus=SIGTERM

[Install]
WantedBy=default.target
"""


# ─── Platform / Dependency Checks ─────────────────────────────────────────


def check_platform() -> str | None:
    """Return an error message if not Linux, None if OK."""
    if not sys.platform.startswith("linux"):
        return (
            f"systemd user services require Linux (detected: {sys.platform}). "
            "This command is not supported on your platform."
        )
    return None


def detect_dvad_binary() -> Path:
    """Locate the dvad binary.

    Strategy 1: sibling of sys.executable (same venv).
    Strategy 2: shutil.which("dvad") on PATH.
    Raises FileNotFoundError if neither works.
    """
    # Strategy 1: venv sibling
    candidate = Path(sys.executable).parent / "dvad"
    if candidate.is_file():
        return candidate

    # Strategy 2: PATH lookup
    found = shutil.which("dvad")
    if found:
        return Path(found)

    raise FileNotFoundError(
        "Could not locate the dvad binary. "
        "Ensure it is installed in your active environment or on your PATH."
    )



# ─── Service File Operations ──────────────────────────────────────────────


def service_file_path() -> Path:
    """Return the path to the systemd user service file."""
    return Path.home() / ".config" / "systemd" / "user" / SERVICE_NAME


def render_service_unit(dvad_bin: Path | str, port: int = DEFAULT_PORT) -> str:
    """Render the systemd unit file from the template."""
    return SERVICE_TEMPLATE.format(dvad_bin=dvad_bin, port=port)


def service_exists() -> bool:
    """Check whether the service file already exists."""
    return service_file_path().exists()


def read_existing_service() -> str | None:
    """Read and return the existing service file content, or None."""
    path = service_file_path()
    if path.exists():
        return path.read_text()
    return None


def write_service_file(content: str) -> Path:
    """Write the service file, creating parent directories as needed."""
    path = service_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def remove_service_file() -> bool:
    """Remove the service file. Returns True if removed, False if not found."""
    path = service_file_path()
    if path.exists():
        path.unlink()
        return True
    return False


# ─── systemctl Wrappers ──────────────────────────────────────────────────


def _run_systemctl(*args: str) -> subprocess.CompletedProcess:
    """Run ``systemctl --user <args>``. Raises RuntimeError on failure."""
    cmd = ["systemctl", "--user", *args]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(
            f"systemctl --user {' '.join(args)} failed (rc={result.returncode}): {stderr}"
        )
    return result


def systemctl_daemon_reload() -> None:
    """Run ``systemctl --user daemon-reload``."""
    _run_systemctl("daemon-reload")


def systemctl_enable() -> None:
    """Run ``systemctl --user enable dvad-gui.service``."""
    _run_systemctl("enable", SERVICE_NAME)


def systemctl_start() -> None:
    """Run ``systemctl --user start dvad-gui.service``."""
    _run_systemctl("start", SERVICE_NAME)


def systemctl_restart() -> None:
    """Run ``systemctl --user restart dvad-gui.service``."""
    _run_systemctl("restart", SERVICE_NAME)


def systemctl_stop() -> None:
    """Run ``systemctl --user stop dvad-gui.service``."""
    _run_systemctl("stop", SERVICE_NAME)


def systemctl_disable() -> None:
    """Run ``systemctl --user disable dvad-gui.service``."""
    _run_systemctl("disable", SERVICE_NAME)


def systemctl_is_active() -> bool:
    """Return True if the service is currently active (running)."""
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", SERVICE_NAME],
            capture_output=True, text=True,
        )
        return result.returncode == 0
    except Exception:
        return False


def systemctl_is_enabled() -> bool:
    """Return True if the service is enabled (starts on login)."""
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-enabled", SERVICE_NAME],
            capture_output=True, text=True,
        )
        return result.returncode == 0
    except Exception:
        return False
