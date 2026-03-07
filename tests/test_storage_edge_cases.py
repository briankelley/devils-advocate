"""Comprehensive edge case tests for StorageManager.

Covers atomic write failures, lock lifecycle edge cases, stale lock
detection, review listing, intermediate data saves, log file lazy open,
update_point_override, and manifest loading.
"""

from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path
from unittest.mock import patch

import pytest


def _isolated_storage(tmp_path):
    from devils_advocate.storage import StorageManager
    return StorageManager(tmp_path, data_dir=tmp_path)


# ═══════════════════════════════════════════════════════════════════════════
# 1. Atomic Write — Success Cases
# ═══════════════════════════════════════════════════════════════════════════


class TestAtomicWriteSuccess:
    def test_creates_file(self, tmp_path):
        from devils_advocate.storage import StorageManager

        target = tmp_path / "test.txt"
        StorageManager._atomic_write(target, "hello world")
        assert target.read_text() == "hello world"

    def test_overwrites_existing(self, tmp_path):
        from devils_advocate.storage import StorageManager

        target = tmp_path / "overwrite.txt"
        target.write_text("old content")
        StorageManager._atomic_write(target, "new content")
        assert target.read_text() == "new content"

    def test_preserves_content_on_unicode(self, tmp_path):
        from devils_advocate.storage import StorageManager

        target = tmp_path / "unicode.txt"
        content = "Hello 🌍 — ñ, ü, 中文"
        StorageManager._atomic_write(target, content)
        assert target.read_text() == content

    def test_empty_content(self, tmp_path):
        from devils_advocate.storage import StorageManager

        target = tmp_path / "empty.txt"
        StorageManager._atomic_write(target, "")
        assert target.read_text() == ""

    def test_large_content(self, tmp_path):
        from devils_advocate.storage import StorageManager

        target = tmp_path / "large.txt"
        content = "x" * 1_000_000
        StorageManager._atomic_write(target, content)
        assert target.read_text() == content

    def test_no_temp_files_left_after_success(self, tmp_path):
        from devils_advocate.storage import StorageManager

        target = tmp_path / "clean.txt"
        StorageManager._atomic_write(target, "clean")
        temps = list(tmp_path.glob(".tmp-*"))
        assert len(temps) == 0


# ═══════════════════════════════════════════════════════════════════════════
# 2. Atomic Write — Failure Cases
# ═══════════════════════════════════════════════════════════════════════════


class TestAtomicWriteFailure:
    def test_bad_directory_raises(self, tmp_path):
        from devils_advocate.storage import StorageManager

        target = tmp_path / "nonexistent" / "sub" / "file.txt"
        with pytest.raises(FileNotFoundError):
            StorageManager._atomic_write(target, "content")

    def test_temp_cleaned_on_write_error(self, tmp_path):
        from devils_advocate.storage import StorageManager

        target = tmp_path / "fail.txt"
        # Patch os.replace to simulate failure after temp file is created
        with patch("os.replace", side_effect=OSError("disk full")):
            with pytest.raises(OSError, match="disk full"):
                StorageManager._atomic_write(target, "content")

        # Temp file should have been cleaned up
        temps = list(tmp_path.glob(".tmp-*"))
        assert len(temps) == 0


# ═══════════════════════════════════════════════════════════════════════════
# 3. Lock — Basic Lifecycle
# ═══════════════════════════════════════════════════════════════════════════


class TestLockBasicLifecycle:
    def test_acquire_then_release(self, tmp_path):
        storage = _isolated_storage(tmp_path)
        assert storage.acquire_lock() is True
        storage.release_lock()

    def test_double_acquire_fails(self, tmp_path):
        storage = _isolated_storage(tmp_path)
        assert storage.acquire_lock() is True
        assert storage.acquire_lock() is False
        storage.release_lock()

    def test_acquire_after_release(self, tmp_path):
        storage = _isolated_storage(tmp_path)
        storage.acquire_lock()
        storage.release_lock()
        assert storage.acquire_lock() is True
        storage.release_lock()

    def test_release_nonexistent_is_safe(self, tmp_path):
        storage = _isolated_storage(tmp_path)
        storage.release_lock()  # should not raise

    def test_double_release_is_safe(self, tmp_path):
        storage = _isolated_storage(tmp_path)
        storage.acquire_lock()
        storage.release_lock()
        storage.release_lock()  # should not raise

    def test_lock_file_contains_pid_and_hostname(self, tmp_path):
        storage = _isolated_storage(tmp_path)
        storage.acquire_lock()

        lock_file = storage.lock_dir / ".lock"
        data = json.loads(lock_file.read_text())
        assert data["pid"] == os.getpid()
        assert data["hostname"] == socket.gethostname()
        assert "timestamp" in data

        storage.release_lock()


# ═══════════════════════════════════════════════════════════════════════════
# 4. Lock — Stale Detection
# ═══════════════════════════════════════════════════════════════════════════


class TestStaleLockDetection:
    def test_stale_by_age(self, tmp_path):
        """Lock older than LOCK_STALE_SECONDS gets removed on retry."""
        storage = _isolated_storage(tmp_path)
        lock_file = storage.lock_dir / ".lock"

        # Create a lock with old timestamp
        lock_data = {
            "pid": 99999,
            "hostname": socket.gethostname(),
            "timestamp": time.time() - 7200,  # 2 hours ago
        }
        lock_file.write_text(json.dumps(lock_data))

        # Should succeed by removing stale lock
        assert storage.acquire_lock() is True
        storage.release_lock()

    def test_stale_by_dead_pid(self, tmp_path):
        """Lock held by dead process on same host gets removed."""
        storage = _isolated_storage(tmp_path)
        lock_file = storage.lock_dir / ".lock"

        lock_data = {
            "pid": 999999999,  # Almost certainly dead
            "hostname": socket.gethostname(),
            "timestamp": time.time(),  # Recent
        }
        lock_file.write_text(json.dumps(lock_data))

        with patch.object(type(storage), '_process_exists', staticmethod(lambda pid: False)):
            assert storage.acquire_lock() is True
        storage.release_lock()

    def test_live_pid_not_stale(self, tmp_path):
        """Lock held by live process on same host is NOT stale."""
        storage = _isolated_storage(tmp_path)
        lock_file = storage.lock_dir / ".lock"

        lock_data = {
            "pid": os.getpid(),  # Current process — definitely alive
            "hostname": socket.gethostname(),
            "timestamp": time.time(),
        }
        lock_file.write_text(json.dumps(lock_data))

        assert storage.acquire_lock() is False
        storage.release_lock()

    def test_different_hostname_not_stale_by_pid(self, tmp_path):
        """Lock from different host — can't check PID, rely on age only."""
        storage = _isolated_storage(tmp_path)
        lock_file = storage.lock_dir / ".lock"

        lock_data = {
            "pid": 99999,
            "hostname": "other-host-not-ours",
            "timestamp": time.time(),  # Recent
        }
        lock_file.write_text(json.dumps(lock_data))

        assert storage.acquire_lock() is False

    def test_corrupted_lock_file_removed(self, tmp_path):
        """Corrupted/unparseable lock file gets removed."""
        storage = _isolated_storage(tmp_path)
        lock_file = storage.lock_dir / ".lock"
        lock_file.write_text("not valid json }{}{")

        assert storage.acquire_lock() is True
        storage.release_lock()

    def test_empty_lock_file_removed(self, tmp_path):
        """Empty lock file gets removed."""
        storage = _isolated_storage(tmp_path)
        lock_file = storage.lock_dir / ".lock"
        lock_file.write_text("")

        assert storage.acquire_lock() is True
        storage.release_lock()


# ═══════════════════════════════════════════════════════════════════════════
# 5. Lock — _process_exists
# ═══════════════════════════════════════════════════════════════════════════


class TestProcessExists:
    def test_current_process_exists(self):
        from devils_advocate.storage import StorageManager
        assert StorageManager._process_exists(os.getpid()) is True

    def test_dead_pid_returns_false(self):
        from devils_advocate.storage import StorageManager
        # PID 4000000 should not exist on any realistic system
        assert StorageManager._process_exists(4000000) is False

    def test_permission_error_returns_true(self):
        """Process exists but we can't signal it (different user)."""
        from devils_advocate.storage import StorageManager
        with patch("os.kill", side_effect=PermissionError):
            assert StorageManager._process_exists(1) is True


# ═══════════════════════════════════════════════════════════════════════════
# 6. Logging — Lazy File Open
# ═══════════════════════════════════════════════════════════════════════════


class TestLogging:
    def test_log_creates_file_lazily(self, tmp_path):
        storage = _isolated_storage(tmp_path)
        storage.set_review_id("lazy_001")

        # No log file yet
        assert not (storage.logs_dir / "lazy_001.log").exists()

        storage.log("First message")

        assert (storage.logs_dir / "lazy_001.log").exists()
        content = (storage.logs_dir / "lazy_001.log").read_text()
        assert "First message" in content

    def test_log_without_review_id_uses_session(self, tmp_path):
        storage = _isolated_storage(tmp_path)
        # Don't set review_id
        storage.log("Session message")

        assert (storage.logs_dir / "session.log").exists()
        content = (storage.logs_dir / "session.log").read_text()
        assert "Session message" in content

    def test_multiple_log_lines_appended(self, tmp_path):
        storage = _isolated_storage(tmp_path)
        storage.set_review_id("multi_001")

        storage.log("Line 1")
        storage.log("Line 2")
        storage.log("Line 3")

        content = (storage.logs_dir / "multi_001.log").read_text()
        assert "Line 1" in content
        assert "Line 2" in content
        assert "Line 3" in content

    def test_log_timestamp_format(self, tmp_path):
        import re

        storage = _isolated_storage(tmp_path)
        storage.set_review_id("ts_001")
        storage.log("Timestamp test")

        content = (storage.logs_dir / "ts_001.log").read_text()
        # Expect ISO-like: [2026-03-07T01:33:37Z]
        assert re.search(r"\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\]", content)

    def test_close_allows_reopen(self, tmp_path):
        storage = _isolated_storage(tmp_path)
        storage.set_review_id("close_001")

        storage.log("Before close")
        storage.close()
        storage.log("After close")

        content = (storage.logs_dir / "close_001.log").read_text()
        assert "Before close" in content
        assert "After close" in content


# ═══════════════════════════════════════════════════════════════════════════
# 7. Review Directory Structure
# ═══════════════════════════════════════════════════════════════════════════


class TestReviewDirectory:
    def test_review_dir_creates_structure(self, tmp_path):
        storage = _isolated_storage(tmp_path)
        rd = storage.review_dir("test_001")

        assert rd.exists()
        assert (rd / "round1").exists()
        assert (rd / "round2").exists()
        assert (rd / "revision").exists()

    def test_review_dir_idempotent(self, tmp_path):
        storage = _isolated_storage(tmp_path)
        rd1 = storage.review_dir("idem_001")
        rd2 = storage.review_dir("idem_001")
        assert rd1 == rd2


# ═══════════════════════════════════════════════════════════════════════════
# 8. Save / Load Intermediate Data
# ═══════════════════════════════════════════════════════════════════════════


class TestSaveIntermediate:
    def test_save_json_data(self, tmp_path):
        storage = _isolated_storage(tmp_path)
        data = [{"point_id": "p1", "severity": "high"}]
        storage.save_intermediate("test_001", "round1", "points.json", data)

        path = storage.reviews_dir / "test_001" / "round1" / "points.json"
        assert path.exists()
        loaded = json.loads(path.read_text())
        assert loaded == data

    def test_save_string_data(self, tmp_path):
        storage = _isolated_storage(tmp_path)
        storage.save_intermediate("test_001", "round1", "raw.txt", "raw review text")

        path = storage.reviews_dir / "test_001" / "round1" / "raw.txt"
        assert path.read_text() == "raw review text"

    def test_save_nested_dict(self, tmp_path):
        storage = _isolated_storage(tmp_path)
        data = {"groups": [{"group_id": "g1", "points": [{"id": "p1"}]}]}
        storage.save_intermediate("test_001", "round2", "groups.json", data)

        path = storage.reviews_dir / "test_001" / "round2" / "groups.json"
        loaded = json.loads(path.read_text())
        assert loaded["groups"][0]["group_id"] == "g1"


# ═══════════════════════════════════════════════════════════════════════════
# 9. Save / Load Review Artifacts
# ═══════════════════════════════════════════════════════════════════════════


class TestSaveReviewArtifacts:
    def test_saves_report_and_ledger(self, tmp_path):
        storage = _isolated_storage(tmp_path)
        ledger = {"review_id": "art_001", "result": "complete", "mode": "plan"}
        storage.save_review_artifacts("art_001", "# Report", ledger)

        rd = storage.reviews_dir / "art_001"
        assert (rd / "dvad-report.md").read_text() == "# Report"
        loaded = json.loads((rd / "review-ledger.json").read_text())
        assert loaded["review_id"] == "art_001"

    def test_saves_round_data(self, tmp_path):
        storage = _isolated_storage(tmp_path)
        ledger = {"review_id": "rd_001", "result": "complete"}
        r1 = {"points": []}
        r2 = {"rebuttals": []}
        storage.save_review_artifacts("rd_001", "# Report", ledger, r1, r2)

        rd = storage.reviews_dir / "rd_001"
        assert (rd / "round1" / "round1-data.json").exists()
        assert (rd / "round2" / "round2-data.json").exists()

    def test_no_round_data_when_none(self, tmp_path):
        storage = _isolated_storage(tmp_path)
        ledger = {"review_id": "nr_001", "result": "failed"}
        storage.save_review_artifacts("nr_001", "# Report", ledger)

        rd = storage.reviews_dir / "nr_001"
        assert not (rd / "round1" / "round1-data.json").exists()
        assert not (rd / "round2" / "round2-data.json").exists()


# ═══════════════════════════════════════════════════════════════════════════
# 10. load_review
# ═══════════════════════════════════════════════════════════════════════════


class TestLoadReview:
    def test_load_existing(self, tmp_path):
        storage = _isolated_storage(tmp_path)
        ledger = {"review_id": "load_001", "result": "complete", "mode": "plan"}
        storage.save_review_artifacts("load_001", "", ledger)

        loaded = storage.load_review("load_001")
        assert loaded is not None
        assert loaded["review_id"] == "load_001"

    def test_load_nonexistent_returns_none(self, tmp_path):
        storage = _isolated_storage(tmp_path)
        assert storage.load_review("nonexistent_999") is None

    def test_load_after_overwrite(self, tmp_path):
        storage = _isolated_storage(tmp_path)
        storage.save_review_artifacts("ow_001", "", {"review_id": "ow_001", "result": "running"})
        storage.save_review_artifacts("ow_001", "", {"review_id": "ow_001", "result": "complete"})

        loaded = storage.load_review("ow_001")
        assert loaded["result"] == "complete"


# ═══════════════════════════════════════════════════════════════════════════
# 11. list_reviews
# ═══════════════════════════════════════════════════════════════════════════


class TestListReviews:
    def test_empty_initially(self, tmp_path):
        storage = _isolated_storage(tmp_path)
        assert storage.list_reviews() == []

    def test_lists_saved_reviews(self, tmp_path):
        storage = _isolated_storage(tmp_path)
        for i in range(3):
            ledger = {
                "review_id": f"list_{i:03d}",
                "result": "complete",
                "mode": "plan",
                "project": "test-proj",
                "input_file": "/tmp/f.md",
                "timestamp": "2026-01-01T00:00:00Z",
                "summary": {"total_points": i, "total_groups": 1},
                "cost": {"total_usd": 0.01},
            }
            storage.save_review_artifacts(f"list_{i:03d}", "", ledger)

        reviews = storage.list_reviews()
        assert len(reviews) == 3
        ids = [r["review_id"] for r in reviews]
        assert "list_000" in ids
        assert "list_001" in ids
        assert "list_002" in ids

    def test_handles_corrupted_ledger(self, tmp_path):
        storage = _isolated_storage(tmp_path)
        # Good review
        storage.save_review_artifacts("good_001", "", {"review_id": "good_001", "result": "complete"})

        # Corrupt review
        bad_dir = storage.reviews_dir / "bad_001"
        bad_dir.mkdir(parents=True)
        (bad_dir / "review-ledger.json").write_text("not json{{{")

        reviews = storage.list_reviews()
        # Should still list the good one
        assert len(reviews) == 1
        assert reviews[0]["review_id"] == "good_001"

    def test_sorted_order(self, tmp_path):
        storage = _isolated_storage(tmp_path)
        for name in ["zzz_001", "aaa_001", "mmm_001"]:
            storage.save_review_artifacts(name, "", {"review_id": name, "result": "complete"})

        reviews = storage.list_reviews()
        ids = [r["review_id"] for r in reviews]
        assert ids == sorted(ids)

    def test_missing_summary_fields(self, tmp_path):
        storage = _isolated_storage(tmp_path)
        # Minimal ledger with no summary
        ledger = {"review_id": "min_001", "result": "failed"}
        storage.save_review_artifacts("min_001", "", ledger)

        reviews = storage.list_reviews()
        assert len(reviews) == 1
        assert reviews[0]["total_points"] == 0
        assert reviews[0]["total_groups"] == 0
        assert reviews[0]["escalated"] == 0


# ═══════════════════════════════════════════════════════════════════════════
# 12. update_point_override
# ═══════════════════════════════════════════════════════════════════════════


class TestUpdatePointOverride:
    def test_basic_override(self, tmp_path):
        from devils_advocate.types import StorageError

        storage = _isolated_storage(tmp_path)
        ledger = {
            "review_id": "ovr_001",
            "result": "complete",
            "points": [
                {"point_id": "p1", "final_resolution": "escalated"},
                {"point_id": "p2", "final_resolution": "auto_accepted"},
            ],
        }
        storage.save_review_artifacts("ovr_001", "", ledger)

        storage.update_point_override("ovr_001", "p1", "overridden")

        loaded = storage.load_review("ovr_001")
        p1 = [p for p in loaded["points"] if p["point_id"] == "p1"][0]
        assert p1["final_resolution"] == "overridden"
        assert len(p1["overrides"]) == 1
        assert p1["overrides"][0]["previous_resolution"] == "escalated"
        assert p1["overrides"][0]["new_resolution"] == "overridden"

    def test_override_by_group_id(self, tmp_path):
        storage = _isolated_storage(tmp_path)
        ledger = {
            "review_id": "grp_ovr",
            "result": "complete",
            "points": [
                {"group_id": "g1", "point_id": "", "final_resolution": "escalated"},
            ],
        }
        storage.save_review_artifacts("grp_ovr", "", ledger)

        storage.update_point_override("grp_ovr", "g1", "auto_dismissed")

        loaded = storage.load_review("grp_ovr")
        assert loaded["points"][0]["final_resolution"] == "auto_dismissed"

    def test_multiple_overrides_stack(self, tmp_path):
        storage = _isolated_storage(tmp_path)
        ledger = {
            "review_id": "stack_001",
            "result": "complete",
            "points": [
                {"point_id": "p1", "final_resolution": "escalated"},
            ],
        }
        storage.save_review_artifacts("stack_001", "", ledger)

        storage.update_point_override("stack_001", "p1", "overridden")
        storage.update_point_override("stack_001", "p1", "auto_dismissed")

        loaded = storage.load_review("stack_001")
        p1 = loaded["points"][0]
        assert p1["final_resolution"] == "auto_dismissed"
        assert len(p1["overrides"]) == 2

    def test_missing_review_raises(self, tmp_path):
        from devils_advocate.types import StorageError

        storage = _isolated_storage(tmp_path)
        with pytest.raises(StorageError, match="not found"):
            storage.update_point_override("nonexistent", "p1", "overridden")

    def test_missing_point_raises(self, tmp_path):
        from devils_advocate.types import StorageError

        storage = _isolated_storage(tmp_path)
        ledger = {"review_id": "no_point", "result": "complete", "points": []}
        storage.save_review_artifacts("no_point", "", ledger)

        with pytest.raises(StorageError, match="not found"):
            storage.update_point_override("no_point", "nonexistent_p", "overridden")

    def test_creates_backup(self, tmp_path):
        storage = _isolated_storage(tmp_path)
        ledger = {
            "review_id": "bak_001",
            "result": "complete",
            "points": [{"point_id": "p1", "final_resolution": "escalated"}],
        }
        storage.save_review_artifacts("bak_001", "", ledger)

        storage.update_point_override("bak_001", "p1", "overridden")

        backup = storage.reviews_dir / "bak_001" / "review-ledger.json.bak"
        assert backup.exists()
        # Backup should have old resolution
        old = json.loads(backup.read_text())
        assert old["points"][0]["final_resolution"] == "escalated"


# ═══════════════════════════════════════════════════════════════════════════
# 13. Manifest Loading
# ═══════════════════════════════════════════════════════════════════════════


class TestManifest:
    def test_load_missing_manifest(self, tmp_path):
        storage = _isolated_storage(tmp_path)
        assert storage.load_manifest() is None

    def test_load_existing_manifest(self, tmp_path):
        storage = _isolated_storage(tmp_path)
        manifest = {"tasks": [{"status": "completed", "files": ["foo.py"]}]}
        (storage.lock_dir / "manifest.json").write_text(json.dumps(manifest))

        loaded = storage.load_manifest()
        assert loaded is not None
        assert len(loaded["tasks"]) == 1


# ═══════════════════════════════════════════════════════════════════════════
# 14. XDG Data Dir Resolution
# ═══════════════════════════════════════════════════════════════════════════


class TestXDGResolution:
    def test_explicit_data_dir_wins(self, tmp_path):
        from devils_advocate.storage import StorageManager

        explicit = tmp_path / "explicit"
        explicit.mkdir()
        storage = StorageManager(tmp_path, data_dir=explicit)
        assert storage.data_dir == explicit

    def test_dvad_home_env_used(self, tmp_path, monkeypatch):
        from devils_advocate.storage import StorageManager

        custom = tmp_path / "custom_home"
        custom.mkdir()
        monkeypatch.setenv("DVAD_HOME", str(custom))

        storage = StorageManager(tmp_path)
        assert storage.data_dir == custom

    def test_default_xdg_path(self, tmp_path, monkeypatch):
        from devils_advocate.storage import StorageManager

        monkeypatch.delenv("DVAD_HOME", raising=False)
        storage = StorageManager(tmp_path)
        expected = Path.home() / ".local" / "share" / "devils-advocate"
        assert storage.data_dir == expected


# ═══════════════════════════════════════════════════════════════════════════
# 15. Review ID Management
# ═══════════════════════════════════════════════════════════════════════════


class TestReviewIdManagement:
    def test_initial_review_id_is_none(self, tmp_path):
        storage = _isolated_storage(tmp_path)
        assert storage.current_review_id is None

    def test_set_review_id(self, tmp_path):
        storage = _isolated_storage(tmp_path)
        storage.set_review_id("test_123")
        assert storage.current_review_id == "test_123"

    def test_set_review_id_changes_log_file(self, tmp_path):
        storage = _isolated_storage(tmp_path)
        storage.set_review_id("id_001")
        storage.log("Message for id_001")

        assert (storage.logs_dir / "id_001.log").exists()


# ═══════════════════════════════════════════════════════════════════════════
# 16. Close Behavior
# ═══════════════════════════════════════════════════════════════════════════


class TestCloseStorage:
    def test_close_without_log(self, tmp_path):
        """Close before any logging should not raise."""
        storage = _isolated_storage(tmp_path)
        storage.close()  # no log file opened

    def test_close_with_log(self, tmp_path):
        storage = _isolated_storage(tmp_path)
        storage.set_review_id("close_test")
        storage.log("Test message")
        storage.close()

        # File should be readable after close
        content = (storage.logs_dir / "close_test.log").read_text()
        assert "Test message" in content

    def test_double_close_safe(self, tmp_path):
        storage = _isolated_storage(tmp_path)
        storage.set_review_id("double_close")
        storage.log("msg")
        storage.close()
        storage.close()  # should not raise


# ═══════════════════════════════════════════════════════════════════════════
# 17. Directory Initialization
# ═══════════════════════════════════════════════════════════════════════════


class TestDirectoryInit:
    def test_directories_created_on_init(self, tmp_path):
        storage = _isolated_storage(tmp_path)
        assert storage.data_dir.exists()
        assert storage.reviews_dir.exists()
        assert storage.logs_dir.exists()
        assert storage.lock_dir.exists()

    def test_lock_dir_is_project_based(self, tmp_path):
        storage = _isolated_storage(tmp_path)
        assert storage.lock_dir == tmp_path / ".dvad"

    def test_reviews_dir_is_data_based(self, tmp_path):
        storage = _isolated_storage(tmp_path)
        assert storage.reviews_dir == tmp_path / "reviews"

    def test_logs_dir_is_data_based(self, tmp_path):
        storage = _isolated_storage(tmp_path)
        assert storage.logs_dir == tmp_path / "logs"
