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

# Daily roster scan. Separate unit from the GUI so a scan failure can never
# take the interface down with it.
SCAN_SERVICE_NAME = "dvad-roster-scan.service"
SCAN_TIMER_NAME = "dvad-roster-scan.timer"
SCAN_AUTOSTART_NAME = "dvad-roster-scan-timer.desktop"

SCAN_SERVICE_TEMPLATE = """\
[Unit]
Description=Devil's Advocate — daily model-roster scan
Documentation=https://github.com/briankelley/devils-advocate

[Service]
Type=oneshot
ExecStart={dvad_bin} roster scan
# The scan shells out to the claude and codex CLIs for judgement and
# availability probes; a systemd user unit does not inherit a login PATH.
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin
Nice=10
"""

# Deliberately relative (OnBootSec / OnUnitActiveSec) rather than OnCalendar
# with Persistent=true. Persistent= needs a timestamp file under
# ~/.local/share/systemd/, which on an encrypted home does not exist until the
# user logs in and the volume is mounted — the timer would either never fire or
# fire spuriously on every unlock. Relative timers need no on-disk state.
SCAN_TIMER_TEMPLATE = """\
[Unit]
Description=Devil's Advocate — daily model-roster scan cadence

[Timer]
OnBootSec=10min
OnUnitActiveSec=24h
RandomizedDelaySec=30min

[Install]
WantedBy=timers.target
"""

# Belt and braces for the encrypted home: `systemctl --user enable` covers the
# lingering case, and this covers the case where the user manager came up
# before the home volume was decrypted and so saw no unit files at all.
SCAN_AUTOSTART_TEMPLATE = """\
[Desktop Entry]
Type=Application
Name=Devil's Advocate Roster Scan Timer
Comment=Start dvad roster scan timer on login (encrypted-home safe)
Exec=/bin/bash -c 'systemctl --user start {timer}'
Terminal=false
X-GNOME-Autostart-enabled=true
StartupNotify=false
Categories=System;
"""

SERVICE_TEMPLATE = """\
[Unit]
Description=Devil's Advocate Web GUI
Documentation=https://github.com/briankelley/devils-advocate
After=default.target
StartLimitIntervalSec=60
StartLimitBurst=3

[Service]
Type=simple
ExecStart={dvad_bin} gui --port {port}
Restart=on-failure
RestartSec=10
KillSignal=SIGTERM
TimeoutStopSec=15

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


# ─── Roster scan timer ────────────────────────────────────────────────────


def user_unit_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def autostart_dir() -> Path:
    return Path.home() / ".config" / "autostart"


def render_scan_units(dvad_bin: Path | str) -> dict[Path, str]:
    """Render every file the scheduled scan needs, keyed by destination."""
    return {
        user_unit_dir() / SCAN_SERVICE_NAME: SCAN_SERVICE_TEMPLATE.format(dvad_bin=dvad_bin),
        user_unit_dir() / SCAN_TIMER_NAME: SCAN_TIMER_TEMPLATE,
        autostart_dir() / SCAN_AUTOSTART_NAME: SCAN_AUTOSTART_TEMPLATE.format(
            timer=SCAN_TIMER_NAME
        ),
    }


def install_scan_timer(dvad_bin: Path | str) -> list[Path]:
    """Write the unit, timer and autostart entry. Returns the paths written."""
    written = []
    for path, content in render_scan_units(dvad_bin).items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        written.append(path)
    systemctl_daemon_reload()
    _run_systemctl("enable", SCAN_TIMER_NAME)
    _run_systemctl("start", SCAN_TIMER_NAME)
    return written


def remove_scan_timer() -> list[Path]:
    """Stop, disable and delete the timer. Returns the paths removed."""
    for args in (("stop", SCAN_TIMER_NAME), ("disable", SCAN_TIMER_NAME)):
        try:
            _run_systemctl(*args)
        except RuntimeError:
            pass  # Already stopped or never enabled; removal proceeds.
    removed = []
    for path in render_scan_units("dvad"):
        if path.exists():
            path.unlink()
            removed.append(path)
    try:
        systemctl_daemon_reload()
    except RuntimeError:
        pass
    return removed


def scan_timer_status() -> dict:
    """Report whether the scheduled scan is installed, enabled and pending."""

    def probe(*args: str) -> str:
        try:
            result = subprocess.run(
                ["systemctl", "--user", *args], capture_output=True, text=True
            )
            return (result.stdout or result.stderr).strip()
        except Exception:
            return "unknown"

    units = render_scan_units("dvad")
    return {
        "installed": all(p.exists() for p in units),
        "enabled": probe("is-enabled", SCAN_TIMER_NAME) == "enabled",
        "active": probe("is-active", SCAN_TIMER_NAME) == "active",
        "next": probe(
            "show", SCAN_TIMER_NAME, "--property=NextElapseUSecRealtime", "--value"
        ),
        "last_result": probe(
            "show", SCAN_SERVICE_NAME, "--property=Result", "--value"
        ),
    }
