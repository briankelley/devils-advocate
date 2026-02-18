"""Tests for devils_advocate.storage module."""

import json
import os
import socket
import time

import pytest

from devils_advocate.storage import LOCK_STALE_SECONDS, StorageManager
from devils_advocate.types import StorageError


def _make_storage(tmp_path):
    """Create a StorageManager rooted in tmp_path."""
    return StorageManager(project_dir=tmp_path, data_dir=tmp_path / "data")


# ─── TestLocking ─────────────────────────────────────────────────────────────


class TestLocking:
    """Tests for acquire_lock / release_lock."""

    def test_acquire_succeeds_on_empty_dir(self, tmp_path):
        storage = _make_storage(tmp_path)
        assert storage.acquire_lock() is True

    def test_acquire_fails_when_already_held(self, tmp_path):
        storage = _make_storage(tmp_path)
        assert storage.acquire_lock() is True
        assert storage.acquire_lock() is False

    def test_release_then_reacquire(self, tmp_path):
        storage = _make_storage(tmp_path)
        assert storage.acquire_lock() is True
        storage.release_lock()
        assert storage.acquire_lock() is True

    def test_stale_lock_by_age_removed(self, tmp_path, monkeypatch):
        storage = _make_storage(tmp_path)
        # Acquire a lock
        assert storage.acquire_lock() is True

        # Patch time.time so the lock appears older than LOCK_STALE_SECONDS
        real_time = time.time
        call_count = 0

        def fake_time():
            nonlocal call_count
            call_count += 1
            # Return a time far in the future so the existing lock looks stale
            return real_time() + LOCK_STALE_SECONDS + 100

        monkeypatch.setattr(time, "time", fake_time)

        # Second acquire should succeed because the stale lock gets removed
        storage2 = _make_storage(tmp_path)
        assert storage2.acquire_lock() is True

    def test_dead_pid_lock_removed(self, tmp_path):
        storage = _make_storage(tmp_path)
        # Manually create a lock file with a PID that doesn't exist
        lock_file = storage.lock_dir / ".lock"
        lock_data = json.dumps({
            "pid": 999999999,
            "hostname": socket.gethostname(),
            "timestamp": time.time(),
        })
        lock_file.write_text(lock_data)

        # Acquire should succeed because the PID doesn't exist on this host
        assert storage.acquire_lock() is True


# ─── TestAtomicWrite ─────────────────────────────────────────────────────────


class TestAtomicWrite:
    """Tests for _atomic_write static method."""

    def test_writes_content_atomically(self, tmp_path):
        target = tmp_path / "output.txt"
        content = "Hello, atomic world!"
        StorageManager._atomic_write(target, content)
        assert target.read_text() == content

    def test_no_tmp_files_remain(self, tmp_path):
        target = tmp_path / "output.txt"
        StorageManager._atomic_write(target, "test content")
        remaining = list(tmp_path.glob(".tmp-*"))
        assert remaining == []


# ─── TestIncrementalLogging ──────────────────────────────────────────────────


class TestIncrementalLogging:
    """Tests for set_review_id, log, and close."""

    def test_log_writes_to_correct_path(self, tmp_path):
        storage = _make_storage(tmp_path)
        storage.set_review_id("test-review-001")
        storage.log("first message")
        expected_path = tmp_path / "data" / "logs" / "test-review-001.log"
        assert expected_path.exists()
        content = expected_path.read_text()
        assert "first message" in content

    def test_close_closes_handle(self, tmp_path):
        storage = _make_storage(tmp_path)
        storage.set_review_id("close-test")
        storage.log("before close")
        storage.close()
        # After close, the internal handle should be None
        assert storage._log_fh is None
        # Data should still be readable
        log_path = tmp_path / "data" / "logs" / "close-test.log"
        content = log_path.read_text()
        assert "before close" in content

    def test_lazy_open_first_log_creates_file(self, tmp_path):
        storage = _make_storage(tmp_path)
        storage.set_review_id("lazy-test")
        log_path = tmp_path / "data" / "logs" / "lazy-test.log"
        # File should not exist before first log call
        assert not log_path.exists()
        storage.log("trigger creation")
        assert log_path.exists()
        storage.close()


# ─── TestSaveAndLoad ─────────────────────────────────────────────────────────


class TestSaveAndLoad:
    """Tests for save_review_artifacts, load_review, list_reviews."""

    def test_save_writes_correct_paths(self, tmp_path):
        storage = _make_storage(tmp_path)
        review_id = "save-test-001"
        report = "# Review Report\nAll good."
        ledger = {"review_id": review_id, "points": [], "summary": {}}
        round1 = {"reviewer1": "data"}
        round2 = {"reviewer2": "data"}
        storage.save_review_artifacts(review_id, report, ledger, round1, round2)

        rd = tmp_path / "data" / "reviews" / review_id
        assert (rd / "dvad-report.md").read_text() == report
        assert json.loads((rd / "review-ledger.json").read_text()) == ledger
        assert json.loads((rd / "round1" / "round1-data.json").read_text()) == round1
        assert json.loads((rd / "round2" / "round2-data.json").read_text()) == round2

    def test_load_review_returns_ledger(self, tmp_path):
        storage = _make_storage(tmp_path)
        review_id = "load-test-001"
        ledger = {"review_id": review_id, "mode": "plan", "points": []}
        storage.save_review_artifacts(review_id, "report", ledger)
        result = storage.load_review(review_id)
        assert result == ledger

    def test_load_review_nonexistent_returns_none(self, tmp_path):
        storage = _make_storage(tmp_path)
        assert storage.load_review("nonexistent") is None

    def test_list_reviews_returns_metadata(self, tmp_path):
        storage = _make_storage(tmp_path)
        for i in range(3):
            rid = f"list-test-{i:03d}"
            ledger = {
                "review_id": rid,
                "mode": "plan",
                "input_file": f"file{i}.py",
                "timestamp": f"2026-02-14T00:00:0{i}Z",
                "summary": {"total_points": i + 1},
                "cost": {"total_usd": 0.01 * (i + 1)},
            }
            storage.save_review_artifacts(rid, "report", ledger)
        reviews = storage.list_reviews()
        assert len(reviews) == 3
        assert reviews[0]["review_id"] == "list-test-000"
        assert reviews[2]["total_points"] == 3
        assert reviews[1]["total_cost"] == pytest.approx(0.02)


# ─── TestUpdatePointOverride ─────────────────────────────────────────────────


class TestUpdatePointOverride:
    """Tests for update_point_override."""

    def _save_with_points(self, storage, review_id, points):
        ledger = {"review_id": review_id, "points": points}
        storage.save_review_artifacts(review_id, "report", ledger)

    def test_successful_override(self, tmp_path):
        storage = _make_storage(tmp_path)
        review_id = "override-test"
        points = [
            {"point_id": "pt-001", "final_resolution": "accepted"},
            {"point_id": "pt-002", "final_resolution": "rejected"},
        ]
        self._save_with_points(storage, review_id, points)
        storage.update_point_override(review_id, "pt-001", "overridden")
        ledger = storage.load_review(review_id)
        pt = ledger["points"][0]
        assert pt["final_resolution"] == "overridden"
        assert len(pt["overrides"]) == 1
        assert pt["overrides"][0]["previous_resolution"] == "accepted"
        assert pt["overrides"][0]["new_resolution"] == "overridden"

    def test_missing_review_raises(self, tmp_path):
        storage = _make_storage(tmp_path)
        with pytest.raises(StorageError, match="not found"):
            storage.update_point_override("ghost-review", "pt-001", "overridden")

    def test_missing_point_raises(self, tmp_path):
        storage = _make_storage(tmp_path)
        review_id = "point-miss"
        points = [{"point_id": "pt-001", "final_resolution": "accepted"}]
        self._save_with_points(storage, review_id, points)
        with pytest.raises(StorageError, match="not found"):
            storage.update_point_override(review_id, "pt-nonexistent", "overridden")
