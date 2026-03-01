"""Storage manager with XDG data paths, atomic writes, and locking.

Key changes from the monolith:
- XDG data path resolution (``$DVAD_HOME`` or ``~/.local/share/devils-advocate/``)
- Lock directory: ``.dvad/`` (not ``.consensus/``)
- Atomic writes using ``mkstemp`` + ``os.replace`` (Bug 5 fix)
- Atomic lock creation using ``os.O_CREAT | os.O_EXCL`` (Bug 6 fix)
- Incremental logging with lazy file handle (Bug 8 fix)
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from .types import StorageError


LOCK_STALE_SECONDS = 3600


class StorageManager:
    """Manages review storage, locking, and incremental logging.

    Parameters
    ----------
    project_dir:
        Project directory used for ``.dvad/.lock`` and ``.dvad/manifest.json``.
    data_dir:
        Data directory for reviews and logs. Resolved via ``$DVAD_HOME`` if set,
        otherwise ``~/.local/share/devils-advocate/``.
    """

    def __init__(self, project_dir: Path, data_dir: Path | None = None) -> None:
        self.project_dir = project_dir
        self.data_dir = self._resolve_data_dir(data_dir)
        self.reviews_dir = self.data_dir / "reviews"
        self.logs_dir = self.data_dir / "logs"
        self.lock_dir = project_dir / ".dvad"

        # Ensure directories exist
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.reviews_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.lock_dir.mkdir(parents=True, exist_ok=True)

        # Incremental log state (lazy-opened)
        self._log_fh = None
        self._review_id: str | None = None

    @staticmethod
    def _resolve_data_dir(explicit: Path | None) -> Path:
        """Resolve the data directory using XDG conventions."""
        if explicit is not None:
            return explicit
        dvad_home = os.environ.get("DVAD_HOME")
        if dvad_home:
            return Path(dvad_home)
        return Path.home() / ".local" / "share" / "devils-advocate"

    # ─── Locking ─────────────────────────────────────────────────────────────

    def acquire_lock(self) -> bool:
        """Acquire an exclusive lock via atomic file creation.

        Uses ``os.O_CREAT | os.O_EXCL`` for race-free creation.
        Stale locks (age > LOCK_STALE_SECONDS or dead PID on same host)
        are removed with bounded retry.
        """
        lock_file = self.lock_dir / ".lock"
        max_attempts = 3

        for _attempt in range(max_attempts):
            try:
                fd = os.open(
                    str(lock_file),
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o644,
                )
                try:
                    lock_data = json.dumps({
                        "pid": os.getpid(),
                        "hostname": socket.gethostname(),
                        "timestamp": time.time(),
                    })
                    os.write(fd, lock_data.encode())
                    os.fsync(fd)
                finally:
                    os.close(fd)
                return True
            except FileExistsError:
                # Lock file already exists -- check if stale
                if self._try_remove_stale_lock(lock_file):
                    continue  # Removed stale lock, retry
                return False  # Lock held by another live process
            except OSError:
                return False

        return False

    def _try_remove_stale_lock(self, lock_file: Path) -> bool:
        """Check if the existing lock is stale and remove it if so.

        Returns True if the lock was removed (caller should retry).
        """
        try:
            lock_data = json.loads(lock_file.read_text())
        except (json.JSONDecodeError, OSError):
            # Corrupted lock file -- safe to remove
            try:
                lock_file.unlink()
            except OSError:
                pass
            return True

        pid = lock_data.get("pid", 0)
        ts = lock_data.get("timestamp", 0)
        hostname = lock_data.get("hostname", "")

        # Stale by age
        if time.time() - ts > LOCK_STALE_SECONDS:
            try:
                lock_file.unlink()
            except OSError:
                return False
            return True

        # Dead PID on same host
        if hostname == socket.gethostname() and not self._process_exists(pid):
            try:
                lock_file.unlink()
            except OSError:
                return False
            return True

        return False

    def release_lock(self) -> None:
        """Release the lock by removing the lock file."""
        lock_file = self.lock_dir / ".lock"
        try:
            lock_file.unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def _process_exists(pid: int) -> bool:
        """Check if a process with the given PID exists."""
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # Process exists but we lack permission to signal it

    # ─── Atomic Write ────────────────────────────────────────────────────────

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        """Write content atomically using mkstemp + os.replace.

        Creates a temporary file in the same directory as the target,
        writes content, flushes and fsyncs, then atomically replaces
        the target path. Cleans up the temp file on failure.
        """
        fd, tmp_path = tempfile.mkstemp(
            dir=path.parent, prefix=".tmp-", suffix=".atomic",
        )
        try:
            with os.fdopen(fd, "w") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # ─── Incremental Logging ─────────────────────────────────────────────────

    @property
    def current_review_id(self) -> str | None:
        """The current review ID, or None if not yet set."""
        return self._review_id

    def set_review_id(self, review_id: str) -> None:
        """Set the review ID for log file naming. Call before first log()."""
        self._review_id = review_id

    def log(self, msg: str) -> None:
        """Write a timestamped log line, flushing immediately.

        The log file is opened lazily on the first call. Path:
        ``{data_dir}/logs/{review_id}.log``
        """
        if self._log_fh is None:
            if self._review_id is None:
                # Fallback: use a generic name if review_id not yet set
                log_name = "session.log"
            else:
                log_name = f"{self._review_id}.log"
            log_path = self.logs_dir / log_name
            self._log_fh = open(log_path, "a", encoding="utf-8")  # noqa: SIM115

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        line = f"[{ts}] {msg}\n"
        self._log_fh.write(line)
        self._log_fh.flush()

    def close(self) -> None:
        """Close the log file handle if open."""
        if self._log_fh is not None:
            try:
                self._log_fh.close()
            except OSError:
                pass
            self._log_fh = None

    # ─── Review Directory ────────────────────────────────────────────────────

    def review_dir(self, review_id: str) -> Path:
        """Return the review directory, creating it with round subdirectories."""
        d = self.reviews_dir / review_id
        d.mkdir(parents=True, exist_ok=True)
        (d / "round1").mkdir(exist_ok=True)
        (d / "round2").mkdir(exist_ok=True)
        (d / "revision").mkdir(exist_ok=True)
        return d

    # ─── Save / Load ─────────────────────────────────────────────────────────

    def save_intermediate(
        self,
        review_id: str,
        stage: str,
        filename: str,
        data,
    ) -> None:
        """Save intermediate review data (raw text or JSON-serializable)."""
        rd = self.review_dir(review_id)
        path = rd / stage / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        content = (
            json.dumps(data, indent=2, default=str)
            if not isinstance(data, str)
            else data
        )
        self._atomic_write(path, content)

    def save_review_artifacts(
        self,
        review_id: str,
        report_str: str,
        ledger_dict: dict,
        round1_data: dict | None = None,
        round2_data: dict | None = None,
    ) -> None:
        """Save final review artifacts (decoupled from output generation).

        Accepts already-generated report string and ledger dict from
        ``output.py``. Does NOT call ``generate_report`` internally.
        """
        rd = self.review_dir(review_id)
        self._atomic_write(rd / "dvad-report.md", report_str)
        self._atomic_write(
            rd / "review-ledger.json",
            json.dumps(ledger_dict, indent=2, default=str),
        )
        if round1_data is not None:
            self._atomic_write(
                rd / "round1" / "round1-data.json",
                json.dumps(round1_data, indent=2, default=str),
            )
        if round2_data is not None:
            self._atomic_write(
                rd / "round2" / "round2-data.json",
                json.dumps(round2_data, indent=2, default=str),
            )

    def load_review(self, review_id: str) -> dict | None:
        """Load a review's ledger by ID. Returns None if not found."""
        path = self.reviews_dir / review_id / "review-ledger.json"
        if not path.exists():
            return None
        return json.loads(path.read_text())

    def list_reviews(self) -> list[dict]:
        """List all stored reviews with summary metadata."""
        reviews: list[dict] = []
        if not self.reviews_dir.exists():
            return reviews
        for d in sorted(self.reviews_dir.iterdir()):
            ledger_path = d / "review-ledger.json"
            if ledger_path.exists():
                try:
                    data = json.loads(ledger_path.read_text())
                    summary = data.get("summary", {})
                    reviews.append({
                        "review_id": data.get("review_id", d.name),
                        "result": data.get("result", "complete"),
                        "project": data.get("project", ""),
                        "mode": data.get("mode", "?"),
                        "input_file": data.get("input_file", "?"),
                        "timestamp": data.get("timestamp", "?"),
                        "total_points": summary.get("total_points", 0),
                        "total_groups": summary.get("total_groups", 0),
                        "escalated": summary.get("escalated", 0),
                        "total_cost": data.get("cost", {}).get("total_usd", 0),
                    })
                except json.JSONDecodeError:
                    pass
        return reviews

    def update_point_override(
        self, review_id: str, point_id: str, resolution: str,
    ) -> None:
        """Override a point's resolution in the ledger."""
        path = self.reviews_dir / review_id / "review-ledger.json"
        if not path.exists():
            raise StorageError(f"Review {review_id} not found")
        ledger = json.loads(path.read_text())
        found = False
        for point in ledger.get("points", []):
            if point.get("point_id") == point_id or point.get("group_id") == point_id:
                if "overrides" not in point:
                    point["overrides"] = []
                point["overrides"].append({
                    "previous_resolution": point.get("final_resolution", ""),
                    "new_resolution": resolution,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                })
                point["final_resolution"] = resolution
                found = True
        if not found:
            raise StorageError(
                f"Point/group {point_id} not found in review {review_id}"
            )
        self._atomic_write(path, json.dumps(ledger, indent=2, default=str))

    def load_manifest(self) -> dict | None:
        """Load the project manifest from ``.dvad/manifest.json``."""
        path = self.lock_dir / "manifest.json"
        if path.exists():
            return json.loads(path.read_text())
        return None
