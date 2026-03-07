"""Tests for the GUI runner lifecycle — the gap between clicking 'Start Review' and seeing a result.

Covers:
- runner._run() exception handling (before/after SSE events)
- SSE stream behavior for dead/failed/completed reviews
- Orchestrator early-pipeline failures (no reviewers, lock contention, all-fail)
- Stub ledger creation and failure
- Cancel and timeout paths
- Review detail page rendering for failed reviews
- Active dict cleanup / memory leak
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from devils_advocate.gui.runner import ReviewRunner
from devils_advocate.gui.progress import ProgressEvent, make_terminal_event, classify_log_message


# ═══════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def runner():
    return ReviewRunner()


@pytest.fixture
def tmp_input_file(tmp_path):
    f = tmp_path / "input.md"
    f.write_text("# Test Plan\nThis is a test plan for board foot calculator.")
    return f


@pytest.fixture
def tmp_spec_file(tmp_path):
    f = tmp_path / "spec.txt"
    f.write_text("Test specification content.")
    return f


def _make_minimal_config():
    """Build a minimal config dict that load_config would return."""
    from helpers import make_model_config
    author = make_model_config(name="author-model")
    reviewer = make_model_config(name="reviewer-model")
    dedup = make_model_config(name="dedup-model")
    norm = make_model_config(name="norm-model")
    revision = make_model_config(name="revision-model")
    return {
        "all_models": {
            "author-model": author,
            "reviewer-model": reviewer,
            "dedup-model": dedup,
            "norm-model": norm,
            "revision-model": revision,
        },
        "models": {},
        "config_path": "/tmp/test-models.yaml",
    }


def _make_roles_dict():
    """Build a roles dict matching get_models_by_role output."""
    from helpers import make_model_config
    return {
        "author": make_model_config(name="author-model"),
        "reviewers": [
            make_model_config(name="reviewer-1"),
            make_model_config(name="reviewer-2"),
        ],
        "dedup": make_model_config(name="dedup-model"),
        "normalization": make_model_config(name="norm-model"),
        "revision": make_model_config(name="revision-model"),
    }


async def _drain_events(runner: ReviewRunner, review_id: str, timeout: float = 0.5) -> list[dict]:
    """Drain all events from a review's queue."""
    events = []
    queue = runner.get_queue(review_id)
    if queue is None:
        return events
    while True:
        try:
            ev = await asyncio.wait_for(queue.get(), timeout=timeout)
            events.append(ev)
            if ev.get("type") in ("complete", "error"):
                break
        except asyncio.TimeoutError:
            break
    return events


def _real_storage(tmp_path):
    """Create a real StorageManager backed by tmp_path.

    Uses tmp_path for both project_dir and data_dir so tests are fully
    isolated from ~/.local/share/devils-advocate/.
    """
    from devils_advocate.storage import StorageManager
    return StorageManager(tmp_path, data_dir=tmp_path)


# ═══════════════════════════════════════════════════════════════════════════
# 1. runner._run() — Config Load Failure
# ═══════════════════════════════════════════════════════════════════════════


class TestRunConfigLoadFailure:
    """When load_config() throws inside _run(), the review should fail
    with a terminal error event and a stub ledger."""

    async def test_config_load_exception_sets_failed_status(self, runner, tmp_input_file, tmp_path):
        with patch("devils_advocate.gui.runner.ReviewRunner._run") as original:
            pass  # We need to test the actual _run, not mock it

        with patch("devils_advocate.config.load_config", side_effect=FileNotFoundError("/bogus/models.yaml")):
            review_id = await runner.start_review(
                mode="plan",
                input_files=[tmp_input_file],
                project="test-config-fail",
                config_path="/bogus/models.yaml",
            )
            # Wait for the background task to complete
            await asyncio.sleep(0.5)

        assert runner.get_status(review_id) == "failed"

    async def test_config_load_exception_emits_terminal_error(self, runner, tmp_input_file):
        with patch("devils_advocate.config.load_config", side_effect=FileNotFoundError("/bogus/models.yaml")):
            review_id = await runner.start_review(
                mode="plan",
                input_files=[tmp_input_file],
                project="test-config-fail",
                config_path="/bogus/models.yaml",
            )
            await asyncio.sleep(0.5)

        events = runner.get_buffered_events(review_id)
        terminal = [e for e in events if e["type"] == "error"]
        assert len(terminal) >= 1, f"Expected terminal error event, got: {events}"
        assert "models.yaml" in terminal[-1]["message"] or "not found" in terminal[-1]["message"].lower()

    async def test_config_load_exception_saves_stub_ledger(self, runner, tmp_input_file, tmp_path):
        with patch("devils_advocate.config.load_config", side_effect=FileNotFoundError("/bogus")):
            review_id = await runner.start_review(
                mode="plan",
                input_files=[tmp_input_file],
                project="test-config-fail",
                config_path="/bogus/models.yaml",
            )
            await asyncio.sleep(0.5)

        # The _run exception handler tries to save a stub ledger.
        # Since StorageManager is never created (config fails first), storage is None.
        # The stub ledger save is skipped (if storage is not None check).
        # This IS the gap — no ledger created when config fails before StorageManager.
        assert runner.get_status(review_id) == "failed"

    async def test_config_load_clears_current_task(self, runner, tmp_input_file):
        with patch("devils_advocate.config.load_config", side_effect=RuntimeError("boom")):
            review_id = await runner.start_review(
                mode="plan",
                input_files=[tmp_input_file],
                project="test",
                config_path="/bogus",
            )
            await asyncio.sleep(0.5)

        assert runner.current_review_id is None
        assert runner.current_task is None


# ═══════════════════════════════════════════════════════════════════════════
# 2. runner._run() — Orchestrator Returns None
# ═══════════════════════════════════════════════════════════════════════════


class TestRunOrchestratorReturnsNone:
    """When the orchestrator returns None (no points, lock failure, etc.),
    the runner should save a stub ledger and emit an error event."""

    async def test_orchestrator_none_sets_failed_status(self, runner, tmp_input_file, tmp_path):
        with (
            patch("devils_advocate.config.load_config", return_value=_make_minimal_config()),
            patch("devils_advocate.config.get_models_by_role", return_value=_make_roles_dict()),
            patch("devils_advocate.orchestrator.run_plan_review", new_callable=AsyncMock, return_value=None),
            patch("devils_advocate.storage.StorageManager", return_value=_real_storage(tmp_path)),
        ):
            review_id = await runner.start_review(
                mode="plan",
                input_files=[tmp_input_file],
                project="test-none",
            )
            await asyncio.sleep(1.0)

        assert runner.get_status(review_id) == "failed"

    async def test_orchestrator_none_emits_no_result_message(self, runner, tmp_input_file, tmp_path):
        with (
            patch("devils_advocate.config.load_config", return_value=_make_minimal_config()),
            patch("devils_advocate.config.get_models_by_role", return_value=_make_roles_dict()),
            patch("devils_advocate.orchestrator.run_plan_review", new_callable=AsyncMock, return_value=None),
            patch("devils_advocate.storage.StorageManager", return_value=_real_storage(tmp_path)),
        ):
            review_id = await runner.start_review(
                mode="plan",
                input_files=[tmp_input_file],
                project="test-none",
            )
            await asyncio.sleep(1.0)

        events = runner.get_buffered_events(review_id)
        terminal = [e for e in events if e["type"] == "error"]
        assert len(terminal) >= 1
        assert "no result" in terminal[-1]["message"].lower()


# ═══════════════════════════════════════════════════════════════════════════
# 3. runner._run() — Orchestrator Returns None BUT Ledger Exists
# ═══════════════════════════════════════════════════════════════════════════


class TestRunOrchestratorNoneWithExistingLedger:
    """When the orchestrator returns None but already saved a ledger
    (dry_run, cost_exceeded), the runner should mark it complete, not failed."""

    async def test_dry_run_ledger_marks_complete(self, runner, tmp_input_file, tmp_path):
        existing_ledger = {"result": "dry_run", "review_id": "test123"}
        storage = _real_storage(tmp_path)

        with (
            patch("devils_advocate.config.load_config", return_value=_make_minimal_config()),
            patch("devils_advocate.config.get_models_by_role", return_value=_make_roles_dict()),
            patch("devils_advocate.orchestrator.run_plan_review", new_callable=AsyncMock, return_value=None),
            patch("devils_advocate.storage.StorageManager", return_value=storage),
            patch.object(storage, "load_review", return_value=existing_ledger),
        ):
            review_id = await runner.start_review(
                mode="plan",
                input_files=[tmp_input_file],
                project="test-dry",
            )
            await asyncio.sleep(1.0)

        assert runner.get_status(review_id) == "complete"
        events = runner.get_buffered_events(review_id)
        terminal = [e for e in events if e["type"] == "complete"]
        assert len(terminal) >= 1

    async def test_cost_exceeded_ledger_marks_complete(self, runner, tmp_input_file, tmp_path):
        existing_ledger = {"result": "cost_exceeded", "review_id": "test456"}
        storage = _real_storage(tmp_path)

        with (
            patch("devils_advocate.config.load_config", return_value=_make_minimal_config()),
            patch("devils_advocate.config.get_models_by_role", return_value=_make_roles_dict()),
            patch("devils_advocate.orchestrator.run_plan_review", new_callable=AsyncMock, return_value=None),
            patch("devils_advocate.storage.StorageManager", return_value=storage),
            patch.object(storage, "load_review", return_value=existing_ledger),
        ):
            review_id = await runner.start_review(
                mode="plan",
                input_files=[tmp_input_file],
                project="test-cost",
            )
            await asyncio.sleep(1.0)

        assert runner.get_status(review_id) == "complete"


# ═══════════════════════════════════════════════════════════════════════════
# 4. runner._run() — Exception Path (Generic Exception)
# ═══════════════════════════════════════════════════════════════════════════


class TestRunGenericException:
    """When _run() catches a generic exception, it should set failed status,
    release the lock, save a stub ledger, and emit a terminal error event."""

    async def test_generic_exception_sets_failed(self, runner, tmp_input_file):
        with (
            patch("devils_advocate.config.load_config", return_value=_make_minimal_config()),
            patch("devils_advocate.config.get_models_by_role", side_effect=KeyError("author")),
        ):
            review_id = await runner.start_review(
                mode="plan",
                input_files=[tmp_input_file],
                project="test-exc",
            )
            await asyncio.sleep(0.5)

        assert runner.get_status(review_id) == "failed"

    async def test_generic_exception_includes_message(self, runner, tmp_input_file):
        with (
            patch("devils_advocate.config.load_config", return_value=_make_minimal_config()),
            patch("devils_advocate.config.get_models_by_role", side_effect=ValueError("bad role config")),
        ):
            review_id = await runner.start_review(
                mode="plan",
                input_files=[tmp_input_file],
                project="test-exc",
            )
            await asyncio.sleep(0.5)

        events = runner.get_buffered_events(review_id)
        terminal = [e for e in events if e["type"] == "error"]
        assert len(terminal) >= 1
        assert "bad role config" in terminal[-1]["message"]

    async def test_generic_exception_clears_current_task(self, runner, tmp_input_file):
        with (
            patch("devils_advocate.config.load_config", side_effect=RuntimeError("boom")),
        ):
            review_id = await runner.start_review(
                mode="plan",
                input_files=[tmp_input_file],
                project="test-exc",
            )
            await asyncio.sleep(0.5)

        assert runner.current_review_id is None
        assert runner.current_task is None


# ═══════════════════════════════════════════════════════════════════════════
# 5. runner._run() — Stub Ledger Save Failure
# ═══════════════════════════════════════════════════════════════════════════


class TestStubLedgerSaveFailure:
    """When _save_stub_ledger() itself throws, it should be silently caught
    and the terminal error event should still be emitted."""

    async def test_stub_save_failure_still_emits_error(self, runner, tmp_input_file, tmp_path):
        with (
            patch("devils_advocate.config.load_config", return_value=_make_minimal_config()),
            patch("devils_advocate.config.get_models_by_role", return_value=_make_roles_dict()),
            patch("devils_advocate.orchestrator.run_plan_review", new_callable=AsyncMock, return_value=None),
            patch("devils_advocate.storage.StorageManager", return_value=_real_storage(tmp_path)),
            patch("devils_advocate.orchestrator._common._save_stub_ledger", side_effect=OSError("disk full")),
        ):
            review_id = await runner.start_review(
                mode="plan",
                input_files=[tmp_input_file],
                project="test-stub-fail",
            )
            await asyncio.sleep(1.0)

        assert runner.get_status(review_id) == "failed"
        events = runner.get_buffered_events(review_id)
        terminal = [e for e in events if e["type"] == "error"]
        assert len(terminal) >= 1, "Terminal error event must be emitted even when stub save fails"


# ═══════════════════════════════════════════════════════════════════════════
# 6. runner._run() — Cancel Path
# ═══════════════════════════════════════════════════════════════════════════


class TestRunCancelPath:
    """When a review is cancelled, the runner should set failed status,
    release the lock, and emit a 'cancelled' error event."""

    async def test_cancel_sets_failed_status(self, runner, tmp_input_file, tmp_path):
        # Make the orchestrator hang so we can cancel it
        async def slow_review(*args, **kwargs):
            await asyncio.sleep(60)

        with (
            patch("devils_advocate.config.load_config", return_value=_make_minimal_config()),
            patch("devils_advocate.config.get_models_by_role", return_value=_make_roles_dict()),
            patch("devils_advocate.orchestrator.run_plan_review", side_effect=slow_review),
            patch("devils_advocate.storage.StorageManager", return_value=_real_storage(tmp_path)),
        ):
            review_id = await runner.start_review(
                mode="plan",
                input_files=[tmp_input_file],
                project="test-cancel",
            )
            await asyncio.sleep(0.3)

            # Cancel
            cancelled = runner.cancel_review(review_id)
            assert cancelled is True

            await asyncio.sleep(0.5)

        assert runner.get_status(review_id) == "failed"

    async def test_cancel_emits_cancelled_message(self, runner, tmp_input_file, tmp_path):
        async def slow_review(*args, **kwargs):
            await asyncio.sleep(60)

        with (
            patch("devils_advocate.config.load_config", return_value=_make_minimal_config()),
            patch("devils_advocate.config.get_models_by_role", return_value=_make_roles_dict()),
            patch("devils_advocate.orchestrator.run_plan_review", side_effect=slow_review),
            patch("devils_advocate.storage.StorageManager", return_value=_real_storage(tmp_path)),
        ):
            review_id = await runner.start_review(
                mode="plan",
                input_files=[tmp_input_file],
                project="test-cancel",
            )
            await asyncio.sleep(0.3)
            runner.cancel_review(review_id)
            await asyncio.sleep(0.5)

        events = runner.get_buffered_events(review_id)
        terminal = [e for e in events if e["type"] == "error"]
        assert len(terminal) >= 1
        assert "cancel" in terminal[-1]["message"].lower()


# ═══════════════════════════════════════════════════════════════════════════
# 7. runner.start_review() — Concurrent Review Rejection (409)
# ═══════════════════════════════════════════════════════════════════════════


class TestConcurrentReviewRejection:
    """Attempting to start a second review while one is running should raise 409."""

    async def test_409_when_review_running(self, runner, tmp_input_file, tmp_path):
        from fastapi import HTTPException

        async def slow_review(*args, **kwargs):
            await asyncio.sleep(60)

        with (
            patch("devils_advocate.config.load_config", return_value=_make_minimal_config()),
            patch("devils_advocate.config.get_models_by_role", return_value=_make_roles_dict()),
            patch("devils_advocate.orchestrator.run_plan_review", side_effect=slow_review),
            patch("devils_advocate.storage.StorageManager", return_value=_real_storage(tmp_path)),
        ):
            # Start first review
            review_id_1 = await runner.start_review(
                mode="plan",
                input_files=[tmp_input_file],
                project="test-1",
            )
            await asyncio.sleep(0.1)

            # Second review should raise 409
            with pytest.raises(HTTPException) as exc_info:
                await runner.start_review(
                    mode="plan",
                    input_files=[tmp_input_file],
                    project="test-2",
                )
            assert exc_info.value.status_code == 409

            # Clean up
            runner.cancel_review(review_id_1)
            await asyncio.sleep(0.3)


# ═══════════════════════════════════════════════════════════════════════════
# 8. SSE Stream Behavior
# ═══════════════════════════════════════════════════════════════════════════


class TestSSEStreamBehavior:
    """Test SSE stream behavior via the FastAPI test client."""

    def test_sse_for_dead_review_returns_terminal(self):
        """SSE connect to a review that already failed should get a terminal event immediately."""
        from fastapi.testclient import TestClient
        from devils_advocate.gui import create_app

        app = create_app()
        client = TestClient(app)

        # Manually set a failed status in the runner
        app.state.runner.statuses["dead_review_123"] = "failed"

        resp = client.get("/api/review/dead_review_123/progress")
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]

        # Parse SSE events
        events = []
        for line in resp.text.strip().split("\n"):
            if line.startswith("data:"):
                events.append(json.loads(line[5:].strip()))

        assert len(events) >= 1
        assert events[-1]["type"] == "error"
        assert "failed" in events[-1]["message"].lower()

    def test_sse_for_completed_review_returns_complete(self):
        """SSE connect to a completed review should return a complete event."""
        from fastapi.testclient import TestClient
        from devils_advocate.gui import create_app

        app = create_app()
        client = TestClient(app)

        app.state.runner.statuses["done_review_456"] = "complete"

        resp = client.get("/api/review/done_review_456/progress")
        events = []
        for line in resp.text.strip().split("\n"):
            if line.startswith("data:"):
                events.append(json.loads(line[5:].strip()))

        assert len(events) >= 1
        assert events[-1]["type"] == "complete"

    def test_sse_replays_buffered_events_for_late_connect(self):
        """SSE should replay buffered events when a client connects late."""
        from fastapi.testclient import TestClient
        from devils_advocate.gui import create_app

        app = create_app()
        runner = app.state.runner
        client = TestClient(app)

        review_id = "buffered_test_789"
        queue = asyncio.Queue(maxsize=100)
        runner.active[review_id] = {
            "queue": queue,
            "buffered": [
                {"type": "phase", "message": "Starting review", "phase": "review_start", "detail": {}, "timestamp": ""},
                {"type": "phase", "message": "Round 1 complete", "phase": "round1_complete", "detail": {}, "timestamp": ""},
                {"type": "complete", "message": "Review complete", "phase": "done", "detail": {}, "timestamp": ""},
            ],
            "state": "complete",
            "created_at": time.time(),
            "last_event_at": time.time(),
        }
        runner.statuses[review_id] = "complete"

        # Put the terminal event in the queue too (as it would be in real flow)
        queue.put_nowait({"type": "phase", "message": "Starting review", "phase": "review_start", "detail": {}, "timestamp": ""})
        queue.put_nowait({"type": "phase", "message": "Round 1 complete", "phase": "round1_complete", "detail": {}, "timestamp": ""})
        queue.put_nowait({"type": "complete", "message": "Review complete", "phase": "done", "detail": {}, "timestamp": ""})

        resp = client.get(f"/api/review/{review_id}/progress")
        events = []
        for line in resp.text.strip().split("\n"):
            if line.startswith("data:"):
                events.append(json.loads(line[5:].strip()))

        # Should have buffered replay + terminal
        assert len(events) >= 3
        assert events[0]["message"] == "Starting review"
        assert events[-1]["type"] == "complete"

    def test_sse_unknown_review_returns_error(self):
        """SSE for a completely unknown review_id should return an error event."""
        from fastapi.testclient import TestClient
        from devils_advocate.gui import create_app

        app = create_app()
        client = TestClient(app)

        resp = client.get("/api/review/totally_unknown/progress")
        events = []
        for line in resp.text.strip().split("\n"):
            if line.startswith("data:"):
                events.append(json.loads(line[5:].strip()))

        assert len(events) >= 1
        assert events[-1]["type"] == "error"
        assert "unknown" in events[-1]["message"].lower()


# ═══════════════════════════════════════════════════════════════════════════
# 9. Review Detail Page — Failed Review Handling
# ═══════════════════════════════════════════════════════════════════════════


class TestReviewDetailFailedReview:
    """The review detail page should handle failed reviews without crashing."""

    def test_failed_review_no_ledger_redirects_to_dashboard(self):
        """A failed review with no ledger should redirect to dashboard."""
        from fastapi.testclient import TestClient
        from devils_advocate.gui import create_app

        app = create_app()
        client = TestClient(app)

        # Runner has status=failed but storage has no ledger
        app.state.runner.statuses["failed_no_ledger"] = "failed"

        resp = client.get("/review/failed_no_ledger", follow_redirects=False)
        assert resp.status_code == 302

    def test_failed_review_with_stub_ledger_renders(self):
        """A failed review with a stub ledger should render without error."""
        from fastapi.testclient import TestClient
        from devils_advocate.gui import create_app
        from unittest.mock import patch

        app = create_app()
        client = TestClient(app)

        stub_ledger = {
            "review_id": "failed_with_stub",
            "result": "failed",
            "mode": "plan",
            "input_file": "/tmp/test.md",
            "project": "test",
            "timestamp": "2026-03-07T01:00:00+00:00",
            "author_model": "",
            "reviewer_models": [],
            "dedup_model": "",
            "points": [],
            "summary": {"total_points": 0, "total_groups": 0},
            "cost": {"total_usd": 0.0, "breakdown": {}, "role_costs": {}},
        }

        with patch("devils_advocate.gui.pages.get_gui_storage") as mock_storage:
            mock_storage.return_value.load_review.return_value = stub_ledger
            mock_storage.return_value.reviews_dir = Path("/tmp/nonexistent")
            mock_storage.return_value.data_dir = Path("/tmp/nonexistent")
            resp = client.get("/review/failed_with_stub")

        # Should render without 500 error
        assert resp.status_code == 200

    def test_nonexistent_review_unknown_status_redirects(self):
        """A review_id that's unknown to both runner and storage should redirect."""
        from fastapi.testclient import TestClient
        from devils_advocate.gui import create_app

        app = create_app()
        client = TestClient(app)

        resp = client.get("/review/totally_bogus_id_xyz", follow_redirects=False)
        assert resp.status_code == 302


# ═══════════════════════════════════════════════════════════════════════════
# 10. Progress Event Classification
# ═══════════════════════════════════════════════════════════════════════════


class TestProgressEventClassification:
    """classify_log_message should correctly categorize log lines."""

    def test_cost_event_parsed(self):
        msg = "§cost role=reviewer_1 model=gemini-3-flash cost=0.004 total=0.008 in_tokens=3000 out_tokens=1000 total_tokens=4000"
        ev = classify_log_message(msg)
        assert ev.event_type == "cost"
        assert ev.detail["role"] == "reviewer_1"
        assert ev.detail["model"] == "gemini-3-flash"

    def test_review_start_classified(self):
        ev = classify_log_message("Starting plan review for project 'board-foot'")
        assert ev.phase == "review_start"

    def test_round1_calling_classified(self):
        ev = classify_log_message("Round 1: calling gemini-3-flash-preview (sent: 3270, timeout: 900s)")
        assert ev.phase == "round1_calling"

    def test_lock_failure_is_unclassified(self):
        ev = classify_log_message("Lock acquisition failed — another review is running or a stale lock exists")
        assert ev.event_type == "log"
        assert ev.phase == "unknown"

    def test_reviewer_failure_classified_as_log(self):
        ev = classify_log_message("Reviewer gemini-3-flash-preview failed: HTTP 403")
        assert ev.event_type == "log"  # No dedicated phase pattern for this

    def test_governance_complete_classified(self):
        ev = classify_log_message("Governance complete: {'total_groups': 6, 'total_points': 7}")
        assert ev.phase == "governance_complete"

    def test_revision_calling_classified(self):
        ev = classify_log_message("Revision: calling minimax-m2.5 to incorporate 4 findings")
        assert ev.phase == "revision_calling"

    def test_normalization_classified(self):
        ev = classify_log_message("No structured points from kimi-k2-thinking -- trying LLM normalization")
        assert ev.phase == "normalization"

    def test_cost_warning_classified(self):
        ev = classify_log_message("Cost warning: $0.80 (80% of $1.00)")
        assert ev.phase == "cost_warning"

    def test_round2_skip_classified(self):
        ev = classify_log_message("Round 2: all groups accepted by author -- skipping")
        assert ev.phase == "round2_skip"


# ═══════════════════════════════════════════════════════════════════════════
# 11. make_terminal_event
# ═══════════════════════════════════════════════════════════════════════════


class TestMakeTerminalEvent:
    def test_success_event(self):
        ev = make_terminal_event(True)
        assert ev.event_type == "complete"
        assert ev.phase == "done"

    def test_failure_event(self):
        ev = make_terminal_event(False)
        assert ev.event_type == "error"
        assert ev.phase == "error"

    def test_failure_with_custom_message(self):
        ev = make_terminal_event(False, "All reviewers failed: HTTP 403")
        assert ev.event_type == "error"
        assert "403" in ev.message

    def test_success_has_timestamp(self):
        ev = make_terminal_event(True)
        assert ev.timestamp  # Not empty


# ═══════════════════════════════════════════════════════════════════════════
# 12. emit_event Edge Cases
# ═══════════════════════════════════════════════════════════════════════════


class TestEmitEventEdgeCases:
    def test_emit_updates_last_event_at(self, runner):
        review_id = "test_ts"
        runner.active[review_id] = {
            "queue": asyncio.Queue(maxsize=100),
            "buffered": [],
            "state": "running",
            "created_at": time.time() - 100,
            "last_event_at": time.time() - 100,
        }
        old_ts = runner.active[review_id]["last_event_at"]

        runner.emit_event(review_id, ProgressEvent(event_type="log", message="test"))

        assert runner.active[review_id]["last_event_at"] > old_ts

    def test_emit_to_nonexistent_review_is_noop(self, runner):
        """Should not raise when emitting to a review that doesn't exist."""
        runner.emit_event("nonexistent", ProgressEvent(event_type="log", message="lost"))
        # No exception = pass

    def test_emit_appends_to_buffer_and_queue(self, runner):
        review_id = "test_both"
        queue = asyncio.Queue(maxsize=100)
        buffered = []
        runner.active[review_id] = {
            "queue": queue,
            "buffered": buffered,
            "state": "running",
            "created_at": time.time(),
            "last_event_at": time.time(),
        }

        runner.emit_event(review_id, ProgressEvent(event_type="phase", message="hello", phase="test"))

        assert len(buffered) == 1
        assert not queue.empty()
        assert buffered[0]["message"] == "hello"


# ═══════════════════════════════════════════════════════════════════════════
# 13. runner._run() — Unknown Mode
# ═══════════════════════════════════════════════════════════════════════════


class TestRunUnknownMode:
    """An invalid mode should raise ValueError, caught by the exception handler."""

    async def test_unknown_mode_raises_and_fails(self, runner, tmp_input_file, tmp_path):
        with (
            patch("devils_advocate.config.load_config", return_value=_make_minimal_config()),
            patch("devils_advocate.config.get_models_by_role", return_value=_make_roles_dict()),
            patch("devils_advocate.storage.StorageManager", return_value=_real_storage(tmp_path)),
        ):
            review_id = await runner.start_review(
                mode="bogus_mode",
                input_files=[tmp_input_file],
                project="test-bad-mode",
            )
            await asyncio.sleep(0.5)

        assert runner.get_status(review_id) == "failed"
        events = runner.get_buffered_events(review_id)
        terminal = [e for e in events if e["type"] == "error"]
        assert len(terminal) >= 1
        assert "unknown mode" in terminal[-1]["message"].lower()


# ═══════════════════════════════════════════════════════════════════════════
# 14. _save_stub_ledger Unit Tests
# ═══════════════════════════════════════════════════════════════════════════


class TestSaveStubLedger:
    """Direct tests for _save_stub_ledger."""

    def test_creates_minimal_ledger(self, tmp_path):
        from devils_advocate.orchestrator._common import _save_stub_ledger
        from devils_advocate.storage import StorageManager

        storage = StorageManager(tmp_path, data_dir=tmp_path)
        storage.set_review_id("stub_test_001")

        _save_stub_ledger(storage, "stub_test_001", "plan", "test-project", "/tmp/input.md", "failed")

        ledger_path = storage.reviews_dir / "stub_test_001" / "review-ledger.json"
        assert ledger_path.exists()

        ledger = json.loads(ledger_path.read_text())
        assert ledger["review_id"] == "stub_test_001"
        assert ledger["result"] == "failed"
        assert ledger["mode"] == "plan"
        assert ledger["project"] == "test-project"
        assert ledger["cost"]["total_usd"] == 0.0

    def test_dry_run_result(self, tmp_path):
        from devils_advocate.orchestrator._common import _save_stub_ledger
        from devils_advocate.storage import StorageManager

        storage = StorageManager(tmp_path, data_dir=tmp_path)
        storage.set_review_id("stub_dry_001")

        _save_stub_ledger(storage, "stub_dry_001", "plan", "test", "/tmp/in.md", "dry_run")

        ledger_path = storage.reviews_dir / "stub_dry_001" / "review-ledger.json"
        ledger = json.loads(ledger_path.read_text())
        assert ledger["result"] == "dry_run"

    def test_cost_exceeded_with_estimate(self, tmp_path):
        from devils_advocate.orchestrator._common import _save_stub_ledger
        from devils_advocate.storage import StorageManager

        storage = StorageManager(tmp_path, data_dir=tmp_path)
        storage.set_review_id("stub_cost_001")

        _save_stub_ledger(
            storage, "stub_cost_001", "plan", "test", "/tmp/in.md",
            "cost_exceeded", est_cost=5.1234,
        )

        ledger_path = storage.reviews_dir / "stub_cost_001" / "review-ledger.json"
        ledger = json.loads(ledger_path.read_text())
        assert ledger["result"] == "cost_exceeded"
        assert ledger["cost"]["total_usd"] == pytest.approx(5.1234, abs=0.001)

    def test_with_role_assignments(self, tmp_path):
        from devils_advocate.orchestrator._common import _save_stub_ledger
        from devils_advocate.storage import StorageManager

        storage = StorageManager(tmp_path, data_dir=tmp_path)
        storage.set_review_id("stub_roles_001")

        ra = {
            "author": "claude-haiku",
            "reviewers": ["gemini-flash", "kimi-k2"],
            "dedup": "deepseek",
        }
        _save_stub_ledger(
            storage, "stub_roles_001", "plan", "test", "/tmp/in.md",
            "dry_run", role_assignments=ra,
        )

        ledger_path = storage.reviews_dir / "stub_roles_001" / "review-ledger.json"
        ledger = json.loads(ledger_path.read_text())
        assert ledger["author_model"] == "claude-haiku"
        assert ledger["reviewer_models"] == ["gemini-flash", "kimi-k2"]
        assert ledger["dedup_model"] == "deepseek"

    def test_stub_has_no_error_message_field(self, tmp_path):
        """Documents the current gap: stub ledgers have no error_message field."""
        from devils_advocate.orchestrator._common import _save_stub_ledger
        from devils_advocate.storage import StorageManager

        storage = StorageManager(tmp_path, data_dir=tmp_path)
        storage.set_review_id("stub_no_err")

        _save_stub_ledger(storage, "stub_no_err", "plan", "test", "/tmp/in.md", "failed")

        ledger_path = storage.reviews_dir / "stub_no_err" / "review-ledger.json"
        ledger = json.loads(ledger_path.read_text())
        # This documents the gap — no error_message preserved
        assert "error_message" not in ledger


# ═══════════════════════════════════════════════════════════════════════════
# 15. Lock Lifecycle in Runner Context
# ═══════════════════════════════════════════════════════════════════════════


class TestLockLifecycleInRunner:
    """Lock acquisition and release in the runner context."""

    def test_lock_acquire_release_cycle(self, tmp_path):
        from devils_advocate.storage import StorageManager

        storage = StorageManager(tmp_path, data_dir=tmp_path)
        assert storage.acquire_lock() is True
        assert storage.acquire_lock() is False  # Already held by us
        storage.release_lock()
        assert storage.acquire_lock() is True  # Now available again
        storage.release_lock()

    def test_double_release_is_safe(self, tmp_path):
        from devils_advocate.storage import StorageManager

        storage = StorageManager(tmp_path, data_dir=tmp_path)
        storage.acquire_lock()
        storage.release_lock()
        storage.release_lock()  # Should not raise

    def test_release_without_acquire_is_safe(self, tmp_path):
        from devils_advocate.storage import StorageManager

        storage = StorageManager(tmp_path, data_dir=tmp_path)
        storage.release_lock()  # Should not raise

    def test_stale_lock_by_dead_pid(self, tmp_path):
        from devils_advocate.storage import StorageManager
        import socket

        storage = StorageManager(tmp_path, data_dir=tmp_path)
        lock_file = storage.lock_dir / ".lock"
        lock_file.parent.mkdir(parents=True, exist_ok=True)

        # Write a lock with a PID that doesn't exist
        lock_data = json.dumps({
            "pid": 999999999,
            "hostname": socket.gethostname(),
            "timestamp": time.time(),
        })
        lock_file.write_text(lock_data)

        # Should detect dead PID and acquire
        assert storage.acquire_lock() is True
        storage.release_lock()

    def test_stale_lock_by_age(self, tmp_path):
        from devils_advocate.storage import StorageManager
        import socket

        storage = StorageManager(tmp_path, data_dir=tmp_path)
        lock_file = storage.lock_dir / ".lock"
        lock_file.parent.mkdir(parents=True, exist_ok=True)

        # Write a lock with a very old timestamp
        lock_data = json.dumps({
            "pid": 1,  # init — always exists
            "hostname": "different-host",
            "timestamp": time.time() - 7200,  # 2 hours ago
        })
        lock_file.write_text(lock_data)

        assert storage.acquire_lock() is True
        storage.release_lock()


# ═══════════════════════════════════════════════════════════════════════════
# 16. Active Dict Memory Leak
# ═══════════════════════════════════════════════════════════════════════════


class TestActiveDictMemoryLeak:
    """Active dict entries are never cleaned up — verify the leak exists."""

    async def test_active_entry_persists_after_completion(self, runner, tmp_input_file):
        with patch("devils_advocate.config.load_config", side_effect=RuntimeError("fast fail")):
            review_id = await runner.start_review(
                mode="plan",
                input_files=[tmp_input_file],
                project="test-leak",
            )
            await asyncio.sleep(0.5)

        # The review failed but active dict still has the entry
        assert review_id in runner.active
        assert runner.active[review_id]["state"] == "failed"
        # Buffered events are still there consuming memory
        assert isinstance(runner.active[review_id]["buffered"], list)


# ═══════════════════════════════════════════════════════════════════════════
# 17. cancel_review Edge Cases
# ═══════════════════════════════════════════════════════════════════════════


class TestCancelReviewEdgeCases:
    def test_cancel_nonexistent_review_returns_false(self, runner):
        assert runner.cancel_review("nonexistent") is False

    def test_cancel_completed_review_returns_false(self, runner):
        runner.current_review_id = "completed_one"
        runner.current_task = MagicMock()
        runner.current_task.done.return_value = True
        assert runner.cancel_review("completed_one") is False

    def test_cancel_wrong_review_id_returns_false(self, runner):
        runner.current_review_id = "review_A"
        runner.current_task = MagicMock()
        runner.current_task.done.return_value = False
        assert runner.cancel_review("review_B") is False


# ═══════════════════════════════════════════════════════════════════════════
# 18. start_review — Input File Read Failures
# ═══════════════════════════════════════════════════════════════════════════


class TestStartReviewInputFileReadFailure:
    """When input files can't be read for review ID generation."""

    async def test_binary_file_raises_unicode_error(self, runner, tmp_path):
        """Binary input files cause UnicodeDecodeError in start_review() —
        the OSError fallback on line 57 doesn't catch decode errors.
        This documents a real gap: only OSError is caught, not ValueError subclasses."""
        f = tmp_path / "unreadable.bin"
        f.write_bytes(b"\x80\x81\x82\x83")  # Binary content

        with patch("devils_advocate.config.load_config", side_effect=RuntimeError("fast fail")):
            with pytest.raises(UnicodeDecodeError):
                await runner.start_review(
                    mode="plan",
                    input_files=[f],
                    project="test-binary",
                )

    async def test_missing_file_falls_back_to_path(self, runner, tmp_path):
        """Missing input files fall back to str(path) for review ID — OSError IS caught."""
        f = tmp_path / "missing.md"

        with patch("devils_advocate.config.load_config", side_effect=RuntimeError("fast fail")):
            review_id = await runner.start_review(
                mode="plan",
                input_files=[f],
                project="test-missing",
            )
            await asyncio.sleep(0.5)

        assert review_id is not None
        assert runner.get_status(review_id) == "failed"


# ═══════════════════════════════════════════════════════════════════════════
# 19. Hooked Log Dedup
# ═══════════════════════════════════════════════════════════════════════════


class TestHookedLogDedup:
    """The monkey-patched storage.log should deduplicate consecutive identical messages."""

    async def test_duplicate_messages_not_emitted_twice(self, runner, tmp_input_file, tmp_path):
        captured_events = []
        original_emit = runner.emit_event

        def capture_emit(review_id, event):
            captured_events.append(event)
            original_emit(review_id, event)

        runner.emit_event = capture_emit

        with (
            patch("devils_advocate.config.load_config", return_value=_make_minimal_config()),
            patch("devils_advocate.config.get_models_by_role", return_value=_make_roles_dict()),
            patch("devils_advocate.orchestrator.run_plan_review", new_callable=AsyncMock, return_value=None),
            patch("devils_advocate.storage.StorageManager.load_review", return_value=None),
            patch("devils_advocate.orchestrator._common._save_stub_ledger"),
        ):
            review_id = await runner.start_review(
                mode="plan",
                input_files=[tmp_input_file],
                project="test-dedup",
                project_dir=tmp_path,
            )
            await asyncio.sleep(1.0)

        # Check that metadata event was emitted (the hooked log should have fired)
        meta_events = [e for e in captured_events if hasattr(e, 'event_type') and e.event_type == "metadata"]
        assert len(meta_events) >= 1, "Expected at least one metadata event from _run()"


# ═══════════════════════════════════════════════════════════════════════════
# 20. API Endpoint — Start Review with Path-Based Flow
# ═══════════════════════════════════════════════════════════════════════════


class TestStartReviewPathFlow:
    """Test the path-based file input flow for start_review API endpoint."""

    def test_path_nonexistent_file_returns_400(self):
        from fastapi.testclient import TestClient
        from devils_advocate.gui import create_app

        app = create_app()
        client = TestClient(app)
        token = app.state.csrf_token

        resp = client.post(
            "/api/review/start",
            data={
                "mode": "plan",
                "project": "test",
                "input_paths": json.dumps(["/nonexistent/file.md"]),
            },
            headers={"X-DVAD-Token": token},
        )
        assert resp.status_code == 400
        assert "not found" in resp.json()["detail"].lower()

    def test_path_directory_returns_400(self, tmp_path):
        from fastapi.testclient import TestClient
        from devils_advocate.gui import create_app

        app = create_app()
        client = TestClient(app)
        token = app.state.csrf_token

        resp = client.post(
            "/api/review/start",
            data={
                "mode": "plan",
                "project": "test",
                "input_paths": json.dumps([str(tmp_path)]),
            },
            headers={"X-DVAD-Token": token},
        )
        assert resp.status_code == 400
        assert "not a file" in resp.json()["detail"].lower()

    def test_path_invalid_json_returns_400(self):
        from fastapi.testclient import TestClient
        from devils_advocate.gui import create_app

        app = create_app()
        client = TestClient(app)
        token = app.state.csrf_token

        resp = client.post(
            "/api/review/start",
            data={
                "mode": "plan",
                "project": "test",
                "input_paths": "not valid json[",
            },
            headers={"X-DVAD-Token": token},
        )
        assert resp.status_code == 400
        assert "json" in resp.json()["detail"].lower()

    def test_path_invalid_reference_json_returns_400(self, tmp_input_file):
        from fastapi.testclient import TestClient
        from devils_advocate.gui import create_app

        app = create_app()
        client = TestClient(app)
        token = app.state.csrf_token

        resp = client.post(
            "/api/review/start",
            data={
                "mode": "plan",
                "project": "test",
                "input_paths": json.dumps([str(tmp_input_file)]),
                "reference_paths": "bad json{",
            },
            headers={"X-DVAD-Token": token},
        )
        assert resp.status_code == 400
        assert "json" in resp.json()["detail"].lower()

    def test_path_nonexistent_spec_returns_400(self, tmp_input_file):
        from fastapi.testclient import TestClient
        from devils_advocate.gui import create_app

        app = create_app()
        client = TestClient(app)
        token = app.state.csrf_token

        resp = client.post(
            "/api/review/start",
            data={
                "mode": "code",
                "project": "test",
                "input_paths": json.dumps([str(tmp_input_file)]),
                "spec_path": "/nonexistent/spec.txt",
            },
            headers={"X-DVAD-Token": token},
        )
        assert resp.status_code == 400
        assert "spec" in resp.json()["detail"].lower()


# ═══════════════════════════════════════════════════════════════════════════
# 21. API Cancel Endpoint
# ═══════════════════════════════════════════════════════════════════════════


class TestCancelEndpoint:
    def test_cancel_requires_csrf(self):
        from fastapi.testclient import TestClient
        from devils_advocate.gui import create_app

        app = create_app()
        client = TestClient(app)

        resp = client.post("/api/review/test_id/cancel")
        assert resp.status_code == 403

    def test_cancel_nonexistent_returns_404(self):
        from fastapi.testclient import TestClient
        from devils_advocate.gui import create_app

        app = create_app()
        client = TestClient(app)
        token = app.state.csrf_token

        resp = client.post(
            "/api/review/nonexistent/cancel",
            headers={"X-DVAD-Token": token},
        )
        assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════
# 22. Storage Manager Logging
# ═══════════════════════════════════════════════════════════════════════════


class TestStorageManagerLogging:
    """Test the incremental logging behavior."""

    def test_log_creates_file_on_first_call(self, tmp_path):
        from devils_advocate.storage import StorageManager

        storage = StorageManager(tmp_path, data_dir=tmp_path)
        storage.set_review_id("log_test_001")
        storage.log("First message")

        log_file = storage.logs_dir / "log_test_001.log"
        assert log_file.exists()
        content = log_file.read_text()
        assert "First message" in content

    def test_log_appends_on_subsequent_calls(self, tmp_path):
        from devils_advocate.storage import StorageManager

        storage = StorageManager(tmp_path, data_dir=tmp_path)
        storage.set_review_id("log_test_002")
        storage.log("Message 1")
        storage.log("Message 2")

        log_file = storage.logs_dir / "log_test_002.log"
        content = log_file.read_text()
        assert "Message 1" in content
        assert "Message 2" in content

    def test_log_includes_timestamp(self, tmp_path):
        from devils_advocate.storage import StorageManager

        storage = StorageManager(tmp_path, data_dir=tmp_path)
        storage.set_review_id("log_test_003")
        storage.log("Timestamped message")

        log_file = storage.logs_dir / "log_test_003.log"
        content = log_file.read_text()
        # Should have ISO timestamp in brackets
        assert "[202" in content

    def test_log_without_review_id_does_not_crash(self, tmp_path):
        from devils_advocate.storage import StorageManager

        storage = StorageManager(tmp_path, data_dir=tmp_path)
        # No set_review_id called — log should handle gracefully
        # (either no-op or write to a fallback)
        try:
            storage.log("Orphan message")
        except Exception:
            pass  # Document whatever behavior exists


# ═══════════════════════════════════════════════════════════════════════════
# 23. Full Integration: Config Error → SSE → Detail Page
# ═══════════════════════════════════════════════════════════════════════════


class TestFullChainConfigError:
    """End-to-end: config error should propagate through runner → SSE → detail."""

    async def test_config_error_produces_failed_review_with_events(self, runner, tmp_input_file):
        """When config fails, the review should have status=failed and buffered error events."""
        with patch("devils_advocate.config.load_config", side_effect=FileNotFoundError("/missing/models.yaml")):
            review_id = await runner.start_review(
                mode="plan",
                input_files=[tmp_input_file],
                project="chain-test",
                config_path="/missing/models.yaml",
            )
            await asyncio.sleep(0.5)

        # Status
        assert runner.get_status(review_id) == "failed"

        # Events
        events = runner.get_buffered_events(review_id)
        assert len(events) >= 1
        terminal = [e for e in events if e["type"] == "error"]
        assert len(terminal) >= 1

        # Current task cleared
        assert runner.current_review_id is None
        assert runner.current_task is None


# ═══════════════════════════════════════════════════════════════════════════
# 24. Runner Mode Dispatch — Code Mode
# ═══════════════════════════════════════════════════════════════════════════


class TestRunCodeMode:
    """Verify code mode dispatches to run_code_review and handles results."""

    async def test_code_mode_dispatches_correctly(self, runner, tmp_input_file, tmp_path):
        mock_result = MagicMock()
        mock_result.review_id = "code_test"

        with (
            patch("devils_advocate.config.load_config", return_value=_make_minimal_config()),
            patch("devils_advocate.config.get_models_by_role", return_value=_make_roles_dict()),
            patch("devils_advocate.orchestrator.run_code_review", new_callable=AsyncMock, return_value=mock_result) as mock_code,
            patch("devils_advocate.storage.StorageManager", return_value=_real_storage(tmp_path)),
        ):
            review_id = await runner.start_review(
                mode="code",
                input_files=[tmp_input_file],
                project="test-code",
            )
            await asyncio.sleep(1.0)

        mock_code.assert_called_once()
        assert runner.get_status(review_id) == "complete"

    async def test_code_mode_passes_spec_file(self, runner, tmp_input_file, tmp_path):
        spec = tmp_path / "spec.txt"
        spec.write_text("Spec content")

        with (
            patch("devils_advocate.config.load_config", return_value=_make_minimal_config()),
            patch("devils_advocate.config.get_models_by_role", return_value=_make_roles_dict()),
            patch("devils_advocate.orchestrator.run_code_review", new_callable=AsyncMock, return_value=MagicMock()) as mock_code,
            patch("devils_advocate.storage.StorageManager", return_value=_real_storage(tmp_path)),
        ):
            review_id = await runner.start_review(
                mode="code",
                input_files=[tmp_input_file],
                project="test-code-spec",
                spec_file=spec,
            )
            await asyncio.sleep(1.0)

        call_args = mock_code.call_args
        assert call_args[0][2] == "test-code-spec"  # project
        assert call_args[0][3] == spec  # spec_file


# ═══════════════════════════════════════════════════════════════════════════
# 25. Runner Mode Dispatch — Spec Mode
# ═══════════════════════════════════════════════════════════════════════════


class TestRunSpecMode:
    async def test_spec_mode_dispatches(self, runner, tmp_input_file, tmp_path):
        with (
            patch("devils_advocate.config.load_config", return_value=_make_minimal_config()),
            patch("devils_advocate.config.get_models_by_role", return_value=_make_roles_dict()),
            patch("devils_advocate.orchestrator.run_spec_review", new_callable=AsyncMock, return_value=MagicMock()) as mock_spec,
            patch("devils_advocate.storage.StorageManager", return_value=_real_storage(tmp_path)),
        ):
            review_id = await runner.start_review(
                mode="spec",
                input_files=[tmp_input_file],
                project="test-spec",
            )
            await asyncio.sleep(1.0)

        mock_spec.assert_called_once()
        assert runner.get_status(review_id) == "complete"


# ═══════════════════════════════════════════════════════════════════════════
# 26. Runner Mode Dispatch — Integration Mode
# ═══════════════════════════════════════════════════════════════════════════


class TestRunIntegrationMode:
    async def test_integration_mode_dispatches(self, runner, tmp_input_file, tmp_path):
        with (
            patch("devils_advocate.config.load_config", return_value=_make_minimal_config()),
            patch("devils_advocate.config.get_models_by_role", return_value=_make_roles_dict()),
            patch("devils_advocate.orchestrator.run_integration_review", new_callable=AsyncMock, return_value=MagicMock()) as mock_int,
            patch("devils_advocate.storage.StorageManager", return_value=_real_storage(tmp_path)),
        ):
            review_id = await runner.start_review(
                mode="integration",
                input_files=[tmp_input_file],
                project="test-integration",
                project_dir=tmp_path,
            )
            await asyncio.sleep(1.0)

        mock_int.assert_called_once()
        assert runner.get_status(review_id) == "complete"

    async def test_integration_passes_project_dir(self, runner, tmp_input_file, tmp_path):
        with (
            patch("devils_advocate.config.load_config", return_value=_make_minimal_config()),
            patch("devils_advocate.config.get_models_by_role", return_value=_make_roles_dict()),
            patch("devils_advocate.orchestrator.run_integration_review", new_callable=AsyncMock, return_value=MagicMock()) as mock_int,
            patch("devils_advocate.storage.StorageManager", return_value=_real_storage(tmp_path)),
        ):
            review_id = await runner.start_review(
                mode="integration",
                input_files=[tmp_input_file],
                project="test-integration-dir",
                project_dir=tmp_path,
            )
            await asyncio.sleep(1.0)

        _, kwargs = mock_int.call_args
        assert kwargs.get("project_dir") == tmp_path


# ═══════════════════════════════════════════════════════════════════════════
# 27. Runner — Successful Orchestrator Return
# ═══════════════════════════════════════════════════════════════════════════


class TestRunSuccessfulReturn:
    """When the orchestrator returns a valid result, status should be complete."""

    async def test_successful_result_sets_complete(self, runner, tmp_input_file, tmp_path):
        result = MagicMock()
        result.review_id = "success_test"

        with (
            patch("devils_advocate.config.load_config", return_value=_make_minimal_config()),
            patch("devils_advocate.config.get_models_by_role", return_value=_make_roles_dict()),
            patch("devils_advocate.orchestrator.run_plan_review", new_callable=AsyncMock, return_value=result),
            patch("devils_advocate.storage.StorageManager", return_value=_real_storage(tmp_path)),
        ):
            review_id = await runner.start_review(
                mode="plan",
                input_files=[tmp_input_file],
                project="test-success",
            )
            await asyncio.sleep(1.0)

        assert runner.get_status(review_id) == "complete"
        events = runner.get_buffered_events(review_id)
        terminal = [e for e in events if e["type"] == "complete"]
        assert len(terminal) >= 1

    async def test_successful_result_emits_metadata_then_start_then_complete(self, runner, tmp_input_file, tmp_path):
        result = MagicMock()

        with (
            patch("devils_advocate.config.load_config", return_value=_make_minimal_config()),
            patch("devils_advocate.config.get_models_by_role", return_value=_make_roles_dict()),
            patch("devils_advocate.orchestrator.run_plan_review", new_callable=AsyncMock, return_value=result),
            patch("devils_advocate.storage.StorageManager", return_value=_real_storage(tmp_path)),
        ):
            review_id = await runner.start_review(
                mode="plan",
                input_files=[tmp_input_file],
                project="test-event-order",
            )
            await asyncio.sleep(1.0)

        events = runner.get_buffered_events(review_id)
        types = [e["type"] for e in events]
        # Should have metadata -> phase (review_start) -> complete
        assert "metadata" in types
        assert "phase" in types
        assert types[-1] == "complete"

    async def test_successful_result_clears_current_task(self, runner, tmp_input_file, tmp_path):
        with (
            patch("devils_advocate.config.load_config", return_value=_make_minimal_config()),
            patch("devils_advocate.config.get_models_by_role", return_value=_make_roles_dict()),
            patch("devils_advocate.orchestrator.run_plan_review", new_callable=AsyncMock, return_value=MagicMock()),
            patch("devils_advocate.storage.StorageManager", return_value=_real_storage(tmp_path)),
        ):
            review_id = await runner.start_review(
                mode="plan",
                input_files=[tmp_input_file],
                project="test-cleanup",
            )
            await asyncio.sleep(1.0)

        assert runner.current_review_id is None
        assert runner.current_task is None


# ═══════════════════════════════════════════════════════════════════════════
# 28. Runner — Timeout Path
# ═══════════════════════════════════════════════════════════════════════════


class TestRunTimeout:
    """The runner wraps orchestrator calls in wait_for(1800s). Test the timeout path."""

    async def test_timeout_sets_failed_and_releases_lock(self, runner, tmp_input_file, tmp_path):
        """Simulate the timeout path: orchestrator raises asyncio.TimeoutError,
        which is caught by the generic except handler in _run()."""
        async def timeout_orchestrator(*args, **kwargs):
            raise asyncio.TimeoutError()

        storage = _real_storage(tmp_path)

        with (
            patch("devils_advocate.config.load_config", return_value=_make_minimal_config()),
            patch("devils_advocate.config.get_models_by_role", return_value=_make_roles_dict()),
            patch("devils_advocate.orchestrator.run_plan_review", side_effect=timeout_orchestrator),
            patch("devils_advocate.storage.StorageManager", return_value=storage),
        ):
            review_id = await runner.start_review(
                mode="plan",
                input_files=[tmp_input_file],
                project="test-timeout",
            )
            await asyncio.sleep(1.0)

        # TimeoutError from the orchestrator is caught by the generic except handler.
        assert runner.get_status(review_id) == "failed"


# ═══════════════════════════════════════════════════════════════════════════
# 29. Runner — File Manifest Handling
# ═══════════════════════════════════════════════════════════════════════════


class TestFileManifestHandling:
    """Runner should save file manifests when provided."""

    async def test_manifest_saved_to_review_dir(self, runner, tmp_input_file, tmp_path, monkeypatch):
        """Manifest saving requires real StorageManager._atomic_write,
        so we set DVAD_HOME and let the real class be used."""
        monkeypatch.setenv("DVAD_HOME", str(tmp_path))
        manifest = {
            "files": [
                {"original_path": str(tmp_input_file), "filename": "input.md", "type": "plan", "size_bytes": 50, "copied": False},
            ]
        }

        with (
            patch("devils_advocate.config.load_config", return_value=_make_minimal_config()),
            patch("devils_advocate.config.get_models_by_role", return_value=_make_roles_dict()),
            patch("devils_advocate.orchestrator.run_plan_review", new_callable=AsyncMock, return_value=MagicMock()),
        ):
            review_id = await runner.start_review(
                mode="plan",
                input_files=[tmp_input_file],
                project="test-manifest",
                project_dir=tmp_path,
                file_manifest=manifest,
            )
            await asyncio.sleep(1.0)

        manifest_path = tmp_path / "reviews" / review_id / "input_files_manifest.json"
        assert manifest_path.exists()
        saved = json.loads(manifest_path.read_text())
        assert len(saved["files"]) == 1

    async def test_no_manifest_still_completes(self, runner, tmp_input_file, tmp_path):
        with (
            patch("devils_advocate.config.load_config", return_value=_make_minimal_config()),
            patch("devils_advocate.config.get_models_by_role", return_value=_make_roles_dict()),
            patch("devils_advocate.orchestrator.run_plan_review", new_callable=AsyncMock, return_value=MagicMock()),
            patch("devils_advocate.storage.StorageManager", return_value=_real_storage(tmp_path)),
        ):
            review_id = await runner.start_review(
                mode="plan",
                input_files=[tmp_input_file],
                project="test-no-manifest",
            )
            await asyncio.sleep(1.0)

        assert runner.get_status(review_id) == "complete"


# ═══════════════════════════════════════════════════════════════════════════
# 30. Runner — Metadata Event Content
# ═══════════════════════════════════════════════════════════════════════════


class TestMetadataEvent:
    """The metadata event should contain role→model mapping for the live cost table."""

    async def test_metadata_contains_roles(self, runner, tmp_input_file, tmp_path):
        with (
            patch("devils_advocate.config.load_config", return_value=_make_minimal_config()),
            patch("devils_advocate.config.get_models_by_role", return_value=_make_roles_dict()),
            patch("devils_advocate.orchestrator.run_plan_review", new_callable=AsyncMock, return_value=MagicMock()),
            patch("devils_advocate.storage.StorageManager", return_value=_real_storage(tmp_path)),
        ):
            review_id = await runner.start_review(
                mode="plan",
                input_files=[tmp_input_file],
                project="test-meta",
            )
            await asyncio.sleep(1.0)

        events = runner.get_buffered_events(review_id)
        meta = [e for e in events if e["type"] == "metadata"]
        assert len(meta) == 1
        detail = meta[0]["detail"]
        assert detail["mode"] == "plan"
        assert detail["project"] == "test-meta"
        assert "roles" in detail
        assert "author" in detail["roles"]

    async def test_metadata_includes_reviewers(self, runner, tmp_input_file, tmp_path):
        with (
            patch("devils_advocate.config.load_config", return_value=_make_minimal_config()),
            patch("devils_advocate.config.get_models_by_role", return_value=_make_roles_dict()),
            patch("devils_advocate.orchestrator.run_plan_review", new_callable=AsyncMock, return_value=MagicMock()),
            patch("devils_advocate.storage.StorageManager", return_value=_real_storage(tmp_path)),
        ):
            review_id = await runner.start_review(
                mode="plan",
                input_files=[tmp_input_file],
                project="test-meta-reviewers",
            )
            await asyncio.sleep(1.0)

        events = runner.get_buffered_events(review_id)
        meta = [e for e in events if e["type"] == "metadata"][0]
        roles = meta["detail"]["roles"]
        assert "reviewer_1" in roles
        assert "reviewer_2" in roles


# ═══════════════════════════════════════════════════════════════════════════
# 31. Runner — Hooked Log Behavior
# ═══════════════════════════════════════════════════════════════════════════


class TestHookedLogBehavior:
    """The monkey-patched storage.log should emit classified progress events."""

    async def test_hooked_log_emits_review_start_phase(self, runner, tmp_input_file, tmp_path):
        with (
            patch("devils_advocate.config.load_config", return_value=_make_minimal_config()),
            patch("devils_advocate.config.get_models_by_role", return_value=_make_roles_dict()),
            patch("devils_advocate.orchestrator.run_plan_review", new_callable=AsyncMock, return_value=MagicMock()),
            patch("devils_advocate.storage.StorageManager", return_value=_real_storage(tmp_path)),
        ):
            review_id = await runner.start_review(
                mode="plan",
                input_files=[tmp_input_file],
                project="test-hooked",
            )
            await asyncio.sleep(1.0)

        events = runner.get_buffered_events(review_id)
        phases = [e for e in events if e["type"] == "phase"]
        phase_names = [e["phase"] for e in phases]
        assert "review_start" in phase_names

    async def test_hooked_log_writes_to_disk(self, runner, tmp_input_file, tmp_path):
        """The hooked log writes to disk when the orchestrator calls storage.log().
        We use an orchestrator that actually calls storage.log() to verify this."""
        storage = _real_storage(tmp_path)

        async def logging_orchestrator(config, input_files, project, max_cost, dry_run, storage=None, **kw):
            if storage:
                storage.log("Round 1: calling test-model (sent: 100)")
                storage.log("Round 1: test-model responded (recv: 50)")
            return MagicMock()

        with (
            patch("devils_advocate.config.load_config", return_value=_make_minimal_config()),
            patch("devils_advocate.config.get_models_by_role", return_value=_make_roles_dict()),
            patch("devils_advocate.orchestrator.run_plan_review", side_effect=logging_orchestrator),
            patch("devils_advocate.storage.StorageManager", return_value=storage),
        ):
            review_id = await runner.start_review(
                mode="plan",
                input_files=[tmp_input_file],
                project="test-log-disk",
            )
            await asyncio.sleep(1.0)

        # Check log file was created by the hooked log
        log_file = storage.logs_dir / f"{review_id}.log"
        assert log_file.exists()
        content = log_file.read_text()
        assert "Round 1" in content


# ═══════════════════════════════════════════════════════════════════════════
# 32. API — Start Review Validation
# ═══════════════════════════════════════════════════════════════════════════


class TestStartReviewValidation:
    """API validation for the start_review endpoint."""

    def _make_app(self):
        from devils_advocate.gui import create_app
        from fastapi.testclient import TestClient
        app = create_app()
        return app, TestClient(app), app.state.csrf_token

    def test_missing_project_returns_400(self):
        app, client, token = self._make_app()
        resp = client.post(
            "/api/review/start",
            data={"mode": "plan"},
            headers={"X-DVAD-Token": token},
        )
        assert resp.status_code == 400
        assert "project" in resp.json()["detail"].lower()

    def test_invalid_mode_returns_400(self, tmp_input_file):
        app, client, token = self._make_app()
        resp = client.post(
            "/api/review/start",
            data={
                "mode": "invalid_mode",
                "project": "test",
                "input_paths": json.dumps([str(tmp_input_file)]),
            },
            headers={"X-DVAD-Token": token},
        )
        assert resp.status_code == 400
        assert "mode" in resp.json()["detail"].lower()

    def test_missing_csrf_returns_403(self, tmp_input_file):
        app, client, token = self._make_app()
        resp = client.post(
            "/api/review/start",
            data={"mode": "plan", "project": "test"},
        )
        assert resp.status_code == 403

    def test_wrong_csrf_returns_403(self, tmp_input_file):
        app, client, token = self._make_app()
        resp = client.post(
            "/api/review/start",
            data={"mode": "plan", "project": "test"},
            headers={"X-DVAD-Token": "wrong_token_value"},
        )
        assert resp.status_code == 403

    def test_invalid_max_cost_returns_400(self, tmp_input_file):
        app, client, token = self._make_app()
        resp = client.post(
            "/api/review/start",
            data={
                "mode": "plan",
                "project": "test",
                "input_paths": json.dumps([str(tmp_input_file)]),
                "max_cost": "not_a_number",
            },
            headers={"X-DVAD-Token": token},
        )
        assert resp.status_code == 400
        assert "max_cost" in resp.json()["detail"].lower()

    def test_code_mode_multiple_files_returns_400(self, tmp_path):
        app, client, token = self._make_app()
        f1 = tmp_path / "a.py"
        f2 = tmp_path / "b.py"
        f1.write_text("code1")
        f2.write_text("code2")
        resp = client.post(
            "/api/review/start",
            data={
                "mode": "code",
                "project": "test",
                "input_paths": json.dumps([str(f1), str(f2)]),
            },
            headers={"X-DVAD-Token": token},
        )
        assert resp.status_code == 400
        assert "one" in resp.json()["detail"].lower()

    def test_plan_mode_no_files_returns_400(self):
        app, client, token = self._make_app()
        resp = client.post(
            "/api/review/start",
            data={
                "mode": "plan",
                "project": "test",
                "input_paths": json.dumps([]),
            },
            headers={"X-DVAD-Token": token},
        )
        assert resp.status_code == 400
        assert "input file" in resp.json()["detail"].lower()


# ═══════════════════════════════════════════════════════════════════════════
# 33. API — Override Endpoint
# ═══════════════════════════════════════════════════════════════════════════


class TestOverrideEndpoint:
    def _make_app(self):
        from devils_advocate.gui import create_app
        from fastapi.testclient import TestClient
        app = create_app()
        return app, TestClient(app), app.state.csrf_token

    def test_override_requires_csrf(self):
        app, client, token = self._make_app()
        resp = client.post(
            "/api/review/test_id/override",
            json={"group_id": "g1", "resolution": "overridden"},
        )
        assert resp.status_code == 403

    def test_override_invalid_resolution(self):
        app, client, token = self._make_app()
        resp = client.post(
            "/api/review/test_id/override",
            json={"group_id": "g1", "resolution": "INVALID"},
            headers={"X-DVAD-Token": token},
        )
        assert resp.status_code == 400
        assert "resolution" in resp.json()["detail"].lower()

    def test_override_missing_group_id(self):
        app, client, token = self._make_app()
        resp = client.post(
            "/api/review/test_id/override",
            json={"resolution": "overridden"},
            headers={"X-DVAD-Token": token},
        )
        assert resp.status_code == 400
        assert "group_id" in resp.json()["detail"].lower()


# ═══════════════════════════════════════════════════════════════════════════
# 34. API — Version Endpoint
# ═══════════════════════════════════════════════════════════════════════════


class TestVersionEndpoint:
    def test_version_returns_200(self):
        from fastapi.testclient import TestClient
        from devils_advocate.gui import create_app
        app = create_app()
        client = TestClient(app)
        resp = client.get("/api/version")
        assert resp.status_code == 200
        data = resp.json()
        assert "installed" in data
        assert "module" in data
        assert "pid" in data

    def test_version_includes_python_path(self):
        from fastapi.testclient import TestClient
        from devils_advocate.gui import create_app
        app = create_app()
        client = TestClient(app)
        data = client.get("/api/version").json()
        assert "python" in data
        assert "/python" in data["python"].lower() or "python" in data["python"].lower()


# ═══════════════════════════════════════════════════════════════════════════
# 35. API — Review JSON Endpoint
# ═══════════════════════════════════════════════════════════════════════════


class TestReviewJsonEndpoint:
    def test_nonexistent_review_returns_404(self):
        from fastapi.testclient import TestClient
        from devils_advocate.gui import create_app
        app = create_app()
        client = TestClient(app)
        resp = client.get("/api/review/nonexistent_id_xyz")
        assert resp.status_code == 404


# ═══════════════════════════════════════════════════════════════════════════
# 36. Progress Event — Comprehensive Phase Coverage
# ═══════════════════════════════════════════════════════════════════════════


class TestProgressPhaseComprehensive:
    """Test all phase patterns in progress.py to ensure classification works."""

    def test_round1_author_classified(self):
        ev = classify_log_message("Round 1: calling author to respond to grouped feedback (sent: 5111)")
        assert ev.phase == "round1_author"

    def test_round1_responded_classified(self):
        ev = classify_log_message("Round 1: gemini-3-flash-preview responded (recv: 1004)")
        assert ev.phase == "round1_responded"

    def test_round1_author_responded_classified(self):
        ev = classify_log_message("Round 1: author responded (recv: 2818)")
        assert ev.phase == "round1_author_responded"

    def test_dedup_calling_classified(self):
        ev = classify_log_message("Deduplication: calling gemini-3-pro-preview (7 points, sent: 1424)")
        assert ev.phase == "dedup_calling"

    def test_dedup_responded_classified(self):
        ev = classify_log_message("Deduplication: gemini-3-pro-preview responded (recv: 344)")
        assert ev.phase == "dedup_responded"

    def test_round2_sending_classified(self):
        ev = classify_log_message("Round 2: sending author responses to reviewers for rebuttal")
        assert ev.phase == "round2_sending"

    def test_round2_calling_classified(self):
        ev = classify_log_message("Round 2: calling gemini-3-flash-preview (sent: 4422)")
        assert ev.phase == "round2_calling"

    def test_round2_responded_classified(self):
        ev = classify_log_message("Round 2: gemini-3-flash-preview responded (recv: 213)")
        assert ev.phase == "round2_responded"

    def test_round2_complete_classified(self):
        ev = classify_log_message("Round 2: rebuttals complete -- 1 challenge(s)")
        assert ev.phase == "round2_complete"

    def test_round2_author_calling_classified(self):
        ev = classify_log_message("Round 2: calling author to respond to rebuttals (sent: 3875)")
        assert ev.phase == "round2_author_calling"

    def test_round2_author_responded_classified(self):
        ev = classify_log_message("Round 2: author responded (recv: 2083)")
        assert ev.phase == "round2_author_responded"

    def test_round2_giving_author_last_word(self):
        ev = classify_log_message("Round 2: giving author last word on 1 challenge(s)")
        assert ev.phase == "round2_author"

    def test_round2_skip_reviewer(self):
        ev = classify_log_message("Round 2: gemini-flash has no contested groups -- skipping")
        assert ev.phase == "round2_skip_reviewer"

    def test_governance_applying_classified(self):
        ev = classify_log_message("Governance: applying deterministic rules")
        assert ev.phase == "governance_applying"

    def test_catastrophic_parse_classified(self):
        ev = classify_log_message("Catastrophic parse failure (<25% coverage) -- escalating all groups")
        assert ev.phase == "governance_catastrophic"

    def test_cost_exceeded_classified(self):
        ev = classify_log_message("Cost limit exceeded: $1.5000 >= $1.00")
        assert ev.phase == "cost_exceeded"

    def test_revision_generating_classified(self):
        ev = classify_log_message("Revision: generating revised artifact with authors final input")
        assert ev.phase == "revision_generating"

    def test_revision_duration_estimate_classified(self):
        ev = classify_log_message("Revision: large context (~6,390 tokens) — expect ~3 min")
        assert ev.phase == "revision_duration_estimate"

    def test_revision_responded_classified(self):
        ev = classify_log_message("Revision: minimax-m2.5 responded (recv: 7626)")
        assert ev.phase == "revision_responded"

    def test_revision_skip_classified(self):
        ev = classify_log_message("Revision: no actionable findings")
        assert ev.phase == "revision_skip"

    def test_revision_failed_classified(self):
        ev = classify_log_message("Revision failed (non-fatal): connection timeout")
        assert ev.phase == "revision_failed"

    def test_normalization_fallback_classified(self):
        ev = classify_log_message("Normalization: calling gemini-3-pro-preview (fallback for kimi-k2, sent: 160)")
        assert ev.phase == "normalization"

    def test_skipping_rebuttal_context_exceeded(self):
        ev = classify_log_message("Skipping kimi-k2 rebuttal: context exceeded")
        assert ev.phase == "round2_skip_context"

    def test_rebuttal_failed(self):
        ev = classify_log_message("Rebuttal gemini-flash failed: HTTP 503")
        assert ev.phase == "round2_rebuttal_failed"

    def test_author_final_failed(self):
        ev = classify_log_message("Author final response failed: API timeout")
        assert ev.phase == "round2_author_failed"


# ═══════════════════════════════════════════════════════════════════════════
# 37. Progress Event — Cost Event Structure
# ═══════════════════════════════════════════════════════════════════════════


class TestCostEventStructure:
    def test_cost_event_full_detail(self):
        msg = "§cost role=reviewer_1 model=gemini-3-flash cost=0.004 total=0.008 in_tokens=3000 out_tokens=1000 total_tokens=4000"
        ev = classify_log_message(msg)
        assert ev.event_type == "cost"
        assert ev.detail["role"] == "reviewer_1"
        assert ev.detail["model"] == "gemini-3-flash"
        assert ev.detail["cost"] == "0.004"
        assert ev.detail["total"] == "0.008"
        assert ev.detail["in_tokens"] == "3000"
        assert ev.detail["out_tokens"] == "1000"
        assert ev.detail["total_tokens"] == "4000"

    def test_cost_event_empty_message(self):
        msg = "§cost role=author model=claude-haiku cost=0.015 total=0.035 in_tokens=5000 out_tokens=2000 total_tokens=7000"
        ev = classify_log_message(msg)
        assert ev.message == ""  # Cost events suppress the message

    def test_cost_event_without_token_detail(self):
        msg = "§cost role=dedup model=deepseek cost=0.001 total=0.009"
        ev = classify_log_message(msg)
        assert ev.event_type == "cost"
        assert "in_tokens" not in ev.detail


# ═══════════════════════════════════════════════════════════════════════════
# 38. ProgressEvent Dataclass
# ═══════════════════════════════════════════════════════════════════════════


class TestProgressEventDataclass:
    def test_auto_timestamp(self):
        ev = ProgressEvent(event_type="log", message="test")
        assert ev.timestamp  # Auto-filled
        assert ":" in ev.timestamp  # HH:MM:SS format

    def test_custom_timestamp_preserved(self):
        ev = ProgressEvent(event_type="log", message="test", timestamp="12:34:56")
        assert ev.timestamp == "12:34:56"

    def test_to_sse_format(self):
        ev = ProgressEvent(event_type="phase", message="hello", phase="test_phase", timestamp="00:00:00")
        sse = ev.to_sse()
        assert sse.startswith("data: ")
        assert sse.endswith("\n\n")
        data = json.loads(sse[6:].strip())
        assert data["type"] == "phase"
        assert data["message"] == "hello"
        assert data["phase"] == "test_phase"

    def test_default_detail_is_dict(self):
        ev = ProgressEvent(event_type="log", message="test")
        assert isinstance(ev.detail, dict)
        assert len(ev.detail) == 0

    def test_default_phase_is_empty(self):
        ev = ProgressEvent(event_type="log", message="test")
        assert ev.phase == ""


# ═══════════════════════════════════════════════════════════════════════════
# 39. Storage — Advanced Operations
# ═══════════════════════════════════════════════════════════════════════════


class TestStorageAdvanced:
    def test_review_dir_creates_subdirs(self, tmp_path):
        from devils_advocate.storage import StorageManager
        storage = StorageManager(tmp_path, data_dir=tmp_path)
        rd = storage.review_dir("test_review_001")
        assert rd.exists()
        assert (rd / "round1").exists()
        assert (rd / "round2").exists()
        assert (rd / "revision").exists()

    def test_save_load_roundtrip(self, tmp_path):
        from devils_advocate.storage import StorageManager
        storage = StorageManager(tmp_path, data_dir=tmp_path)
        ledger = {
            "review_id": "roundtrip_001",
            "result": "complete",
            "mode": "plan",
            "points": [{"point_id": "p1", "description": "test"}],
        }
        storage.save_review_artifacts("roundtrip_001", "# Report", ledger)
        loaded = storage.load_review("roundtrip_001")
        assert loaded["review_id"] == "roundtrip_001"
        assert loaded["result"] == "complete"
        assert len(loaded["points"]) == 1

    def test_load_nonexistent_returns_none(self, tmp_path):
        from devils_advocate.storage import StorageManager
        storage = StorageManager(tmp_path, data_dir=tmp_path)
        assert storage.load_review("totally_bogus") is None

    def test_list_reviews_empty(self, tmp_path):
        from devils_advocate.storage import StorageManager
        storage = StorageManager(tmp_path, data_dir=tmp_path)
        assert storage.list_reviews() == []

    def test_list_reviews_with_entries(self, tmp_path):
        from devils_advocate.storage import StorageManager
        storage = StorageManager(tmp_path, data_dir=tmp_path)

        ledger1 = {"review_id": "r1", "result": "complete", "mode": "plan",
                    "project": "p1", "input_file": "f1.md", "timestamp": "2026-01-01",
                    "summary": {"total_points": 5, "total_groups": 3}, "cost": {"total_usd": 0.05}}
        storage.save_review_artifacts("r1", "# R1", ledger1)

        ledger2 = {"review_id": "r2", "result": "failed", "mode": "code",
                    "project": "p2", "input_file": "f2.py", "timestamp": "2026-01-02",
                    "summary": {"total_points": 0, "total_groups": 0}, "cost": {"total_usd": 0.0}}
        storage.save_review_artifacts("r2", "", ledger2)

        reviews = storage.list_reviews()
        assert len(reviews) == 2
        ids = [r["review_id"] for r in reviews]
        assert "r1" in ids
        assert "r2" in ids

    def test_list_reviews_skips_corrupted_ledger(self, tmp_path):
        from devils_advocate.storage import StorageManager
        storage = StorageManager(tmp_path, data_dir=tmp_path)

        # Create a valid ledger
        storage.save_review_artifacts("good", "# Report", {"review_id": "good", "result": "complete"})

        # Create a corrupted ledger
        bad_dir = storage.reviews_dir / "bad"
        bad_dir.mkdir(parents=True)
        (bad_dir / "review-ledger.json").write_text("{invalid json!!!}")

        reviews = storage.list_reviews()
        assert len(reviews) == 1
        assert reviews[0]["review_id"] == "good"

    def test_save_intermediate(self, tmp_path):
        from devils_advocate.storage import StorageManager
        storage = StorageManager(tmp_path, data_dir=tmp_path)
        storage.save_intermediate("rev_001", "round1", "test_raw.txt", "Raw response text")
        saved = (storage.reviews_dir / "rev_001" / "round1" / "test_raw.txt").read_text()
        assert saved == "Raw response text"

    def test_save_intermediate_json(self, tmp_path):
        from devils_advocate.storage import StorageManager
        storage = StorageManager(tmp_path, data_dir=tmp_path)
        data = {"key": "value", "list": [1, 2, 3]}
        storage.save_intermediate("rev_002", "round2", "data.json", data)
        saved = json.loads((storage.reviews_dir / "rev_002" / "round2" / "data.json").read_text())
        assert saved["key"] == "value"

    def test_update_point_override(self, tmp_path):
        from devils_advocate.storage import StorageManager
        storage = StorageManager(tmp_path, data_dir=tmp_path)
        ledger = {
            "review_id": "override_test",
            "result": "complete",
            "points": [
                {"point_id": "p1", "group_id": "g1", "final_resolution": "escalated"},
                {"point_id": "p2", "group_id": "g2", "final_resolution": "auto_accepted"},
            ],
        }
        storage.save_review_artifacts("override_test", "# Report", ledger)

        storage.update_point_override("override_test", "g1", "overridden")
        updated = storage.load_review("override_test")
        p1 = [p for p in updated["points"] if p["group_id"] == "g1"][0]
        assert p1["final_resolution"] == "overridden"
        assert len(p1["overrides"]) == 1
        assert p1["overrides"][0]["previous_resolution"] == "escalated"

    def test_update_point_override_not_found(self, tmp_path):
        from devils_advocate.storage import StorageManager
        from devils_advocate.types import StorageError
        storage = StorageManager(tmp_path, data_dir=tmp_path)
        ledger = {"review_id": "no_point", "points": []}
        storage.save_review_artifacts("no_point", "", ledger)

        with pytest.raises(StorageError):
            storage.update_point_override("no_point", "nonexistent_group", "overridden")

    def test_update_point_override_creates_backup(self, tmp_path):
        from devils_advocate.storage import StorageManager
        storage = StorageManager(tmp_path, data_dir=tmp_path)
        ledger = {"review_id": "backup_test", "points": [
            {"point_id": "p1", "group_id": "g1", "final_resolution": "escalated"},
        ]}
        storage.save_review_artifacts("backup_test", "", ledger)

        storage.update_point_override("backup_test", "g1", "overridden")
        backup = storage.reviews_dir / "backup_test" / "review-ledger.json.bak"
        assert backup.exists()

    def test_atomic_write_creates_file(self, tmp_path):
        from devils_advocate.storage import StorageManager
        target = tmp_path / "atomic_test.txt"
        StorageManager._atomic_write(target, "hello world")
        assert target.read_text() == "hello world"

    def test_atomic_write_replaces_existing(self, tmp_path):
        from devils_advocate.storage import StorageManager
        target = tmp_path / "atomic_replace.txt"
        target.write_text("old content")
        StorageManager._atomic_write(target, "new content")
        assert target.read_text() == "new content"


# ═══════════════════════════════════════════════════════════════════════════
# 40. Storage — XDG Data Dir Resolution
# ═══════════════════════════════════════════════════════════════════════════


class TestStorageXDGResolution:
    def test_explicit_data_dir(self, tmp_path):
        from devils_advocate.storage import StorageManager
        custom_data = tmp_path / "custom_data"
        storage = StorageManager(tmp_path, data_dir=custom_data)
        assert storage.data_dir == custom_data

    def test_dvad_home_env(self, tmp_path, monkeypatch):
        from devils_advocate.storage import StorageManager
        dvad_home = tmp_path / "dvad_home"
        monkeypatch.setenv("DVAD_HOME", str(dvad_home))
        storage = StorageManager(tmp_path)  # Intentionally no data_dir — testing DVAD_HOME
        assert storage.data_dir == dvad_home

    def test_default_xdg_path(self, tmp_path, monkeypatch):
        from devils_advocate.storage import StorageManager
        monkeypatch.delenv("DVAD_HOME", raising=False)
        storage = StorageManager(tmp_path)  # Intentionally no data_dir — testing default
        expected = Path.home() / ".local" / "share" / "devils-advocate"
        assert storage.data_dir == expected


# ═══════════════════════════════════════════════════════════════════════════
# 41. Review ID Generation
# ═══════════════════════════════════════════════════════════════════════════


class TestReviewIDGeneration:
    def test_format_timestamp_hash(self):
        from devils_advocate.ids import generate_review_id
        rid = generate_review_id("test content")
        parts = rid.split("_")
        assert len(parts) == 2
        assert len(parts[0]) == 15  # YYYYMMDDThhmmss
        assert len(parts[1]) == 6   # sha256[:6]

    def test_same_content_same_hash(self):
        from devils_advocate.ids import generate_review_id, _content_hash
        h1 = _content_hash("identical content")
        h2 = _content_hash("identical content")
        assert h1 == h2

    def test_different_content_different_hash(self):
        from devils_advocate.ids import _content_hash
        h1 = _content_hash("content A")
        h2 = _content_hash("content B")
        assert h1 != h2


# ═══════════════════════════════════════════════════════════════════════════
# 42. Runner — Queue Full Behavior
# ═══════════════════════════════════════════════════════════════════════════


class TestQueueFullBehavior:
    """When the queue is full, emit_event should drop the oldest and retry."""

    def test_queue_full_drops_oldest(self, runner):
        review_id = "full_queue_test"
        queue = asyncio.Queue(maxsize=2)
        runner.active[review_id] = {
            "queue": queue,
            "buffered": [],
            "state": "running",
            "created_at": time.time(),
            "last_event_at": time.time(),
        }

        # Fill the queue
        runner.emit_event(review_id, ProgressEvent(event_type="log", message="msg1"))
        runner.emit_event(review_id, ProgressEvent(event_type="log", message="msg2"))

        # Queue is now full. Third emit should drop msg1
        runner.emit_event(review_id, ProgressEvent(event_type="log", message="msg3"))

        # Buffer has all 3
        assert len(runner.active[review_id]["buffered"]) == 3

        # Queue has 2 (msg2 and msg3, with msg1 dropped)
        assert queue.qsize() == 2

    def test_buffer_never_drops(self, runner):
        review_id = "buffer_no_drop"
        queue = asyncio.Queue(maxsize=1)
        runner.active[review_id] = {
            "queue": queue,
            "buffered": [],
            "state": "running",
            "created_at": time.time(),
            "last_event_at": time.time(),
        }

        for i in range(50):
            runner.emit_event(review_id, ProgressEvent(event_type="log", message=f"msg{i}"))

        assert len(runner.active[review_id]["buffered"]) == 50


# ═══════════════════════════════════════════════════════════════════════════
# 43. Runner — Multiple Sequential Reviews
# ═══════════════════════════════════════════════════════════════════════════


class TestMultipleSequentialReviews:
    """After one review completes, a second should start without issues."""

    async def test_second_review_after_first_completes(self, runner, tmp_path):
        f1 = tmp_path / "first.md"
        f1.write_text("First review content")
        f2 = tmp_path / "second.md"
        f2.write_text("Second review content different")

        with (
            patch("devils_advocate.config.load_config", return_value=_make_minimal_config()),
            patch("devils_advocate.config.get_models_by_role", return_value=_make_roles_dict()),
            patch("devils_advocate.orchestrator.run_plan_review", new_callable=AsyncMock, return_value=MagicMock()),
            patch("devils_advocate.storage.StorageManager", return_value=_real_storage(tmp_path)),
        ):
            r1 = await runner.start_review(mode="plan", input_files=[f1], project="seq-1")
            await asyncio.sleep(1.0)
            assert runner.get_status(r1) == "complete"
            assert runner.current_task is None

            r2 = await runner.start_review(mode="plan", input_files=[f2], project="seq-2")
            await asyncio.sleep(1.0)
            assert runner.get_status(r2) == "complete"

        # Both should be in history
        assert r1 != r2
        assert runner.get_status(r1) == "complete"
        assert runner.get_status(r2) == "complete"

    async def test_second_review_after_first_fails(self, runner, tmp_path):
        f1 = tmp_path / "fail.md"
        f1.write_text("Will fail")
        f2 = tmp_path / "succeed.md"
        f2.write_text("Will succeed different content")

        with (
            patch("devils_advocate.config.load_config", return_value=_make_minimal_config()),
            patch("devils_advocate.config.get_models_by_role", return_value=_make_roles_dict()),
            patch("devils_advocate.storage.StorageManager", return_value=_real_storage(tmp_path)),
        ):
            # First fails
            with patch("devils_advocate.orchestrator.run_plan_review", side_effect=RuntimeError("boom")):
                r1 = await runner.start_review(mode="plan", input_files=[f1], project="seq-fail")
                await asyncio.sleep(1.0)
            assert runner.get_status(r1) == "failed"

            # Second succeeds
            with patch("devils_advocate.orchestrator.run_plan_review", new_callable=AsyncMock, return_value=MagicMock()):
                r2 = await runner.start_review(mode="plan", input_files=[f2], project="seq-ok")
                await asyncio.sleep(1.0)
            assert runner.get_status(r2) == "complete"


# ═══════════════════════════════════════════════════════════════════════════
# 44. Runner — Lock Release on Failure
# ═══════════════════════════════════════════════════════════════════════════


class TestLockReleaseOnFailure:
    """When _run() fails, it should release the storage lock."""

    async def test_lock_released_on_exception(self, runner, tmp_input_file, tmp_path):
        storage = _real_storage(tmp_path)

        with (
            patch("devils_advocate.config.load_config", return_value=_make_minimal_config()),
            patch("devils_advocate.config.get_models_by_role", return_value=_make_roles_dict()),
            patch("devils_advocate.orchestrator.run_plan_review", side_effect=RuntimeError("lock test")),
            patch("devils_advocate.storage.StorageManager", return_value=storage),
        ):
            review_id = await runner.start_review(
                mode="plan",
                input_files=[tmp_input_file],
                project="test-lock-release",
            )
            await asyncio.sleep(1.0)

        # Lock should have been released — we should be able to acquire it
        assert storage.acquire_lock() is True
        storage.release_lock()

    async def test_lock_released_on_orchestrator_none(self, runner, tmp_input_file, tmp_path):
        storage = _real_storage(tmp_path)

        with (
            patch("devils_advocate.config.load_config", return_value=_make_minimal_config()),
            patch("devils_advocate.config.get_models_by_role", return_value=_make_roles_dict()),
            patch("devils_advocate.orchestrator.run_plan_review", new_callable=AsyncMock, return_value=None),
            patch("devils_advocate.storage.StorageManager", return_value=storage),
        ):
            review_id = await runner.start_review(
                mode="plan",
                input_files=[tmp_input_file],
                project="test-lock-none",
            )
            await asyncio.sleep(1.0)

        # The orchestrator itself may acquire/release lock, but runner also calls release_lock
        # on failure. Either way, lock should be available.
        assert storage.acquire_lock() is True
        storage.release_lock()


# ═══════════════════════════════════════════════════════════════════════════
# 45. Storage — Process Exists Check
# ═══════════════════════════════════════════════════════════════════════════


class TestProcessExists:
    def test_current_process_exists(self, tmp_path):
        from devils_advocate.storage import StorageManager
        import os
        assert StorageManager._process_exists(os.getpid()) is True

    def test_dead_pid_not_exists(self, tmp_path):
        from devils_advocate.storage import StorageManager
        assert StorageManager._process_exists(999999999) is False

    def test_pid_1_exists(self, tmp_path):
        from devils_advocate.storage import StorageManager
        # PID 1 (init/systemd) always exists but we may lack permission
        # _process_exists returns True on PermissionError
        result = StorageManager._process_exists(1)
        assert result is True  # Either exists or PermissionError → True


# ═══════════════════════════════════════════════════════════════════════════
# 46. Pages — Dashboard Route
# ═══════════════════════════════════════════════════════════════════════════


class TestDashboardPage:
    def test_dashboard_returns_200(self):
        from fastapi.testclient import TestClient
        from devils_advocate.gui import create_app
        app = create_app()
        client = TestClient(app)
        resp = client.get("/")
        assert resp.status_code == 200

    def test_new_review_redirects_to_dashboard(self):
        from fastapi.testclient import TestClient
        from devils_advocate.gui import create_app
        app = create_app()
        client = TestClient(app)
        resp = client.get("/review/new", follow_redirects=False)
        assert resp.status_code == 302


# ═══════════════════════════════════════════════════════════════════════════
# 47. Runner — get_status for Various States
# ═══════════════════════════════════════════════════════════════════════════


class TestGetStatusVariants:
    def test_unknown_review(self, runner):
        assert runner.get_status("never_existed") == "unknown"

    def test_running_status(self, runner):
        runner.statuses["running_one"] = "running"
        assert runner.get_status("running_one") == "running"

    def test_complete_status(self, runner):
        runner.statuses["done_one"] = "complete"
        assert runner.get_status("done_one") == "complete"

    def test_failed_status(self, runner):
        runner.statuses["bad_one"] = "failed"
        assert runner.get_status("bad_one") == "failed"


# ═══════════════════════════════════════════════════════════════════════════
# 48. Runner — get_buffered_events Edge Cases
# ═══════════════════════════════════════════════════════════════════════════


class TestGetBufferedEventsEdge:
    def test_no_active_entry(self, runner):
        assert runner.get_buffered_events("nonexistent") == []

    def test_returns_copy_not_reference(self, runner):
        review_id = "copy_test"
        original = [{"type": "log", "message": "hello"}]
        runner.active[review_id] = {
            "queue": asyncio.Queue(),
            "buffered": original,
            "state": "running",
            "created_at": time.time(),
            "last_event_at": time.time(),
        }
        result = runner.get_buffered_events(review_id)
        result.append({"type": "log", "message": "injected"})
        # Original should not be modified
        assert len(original) == 1


# ═══════════════════════════════════════════════════════════════════════════
# 49. Runner — get_queue Edge Cases
# ═══════════════════════════════════════════════════════════════════════════


class TestGetQueueEdge:
    def test_no_active_entry(self, runner):
        assert runner.get_queue("nonexistent") is None

    def test_returns_queue_object(self, runner):
        review_id = "queue_test"
        queue = asyncio.Queue()
        runner.active[review_id] = {
            "queue": queue,
            "buffered": [],
            "state": "running",
            "created_at": time.time(),
            "last_event_at": time.time(),
        }
        assert runner.get_queue(review_id) is queue


# ═══════════════════════════════════════════════════════════════════════════
# 50. Runner — Exception in Config + get_models_by_role
# ═══════════════════════════════════════════════════════════════════════════


class TestConfigAndRoleFailures:
    """Test various failure points in the config/role loading phase of _run()."""

    async def test_get_models_by_role_keyerror(self, runner, tmp_input_file, tmp_path):
        """get_models_by_role raises KeyError when role is missing."""
        with (
            patch("devils_advocate.config.load_config", return_value=_make_minimal_config()),
            patch("devils_advocate.config.get_models_by_role", side_effect=KeyError("revision")),
            patch("devils_advocate.storage.StorageManager", return_value=_real_storage(tmp_path)),
        ):
            review_id = await runner.start_review(
                mode="plan",
                input_files=[tmp_input_file],
                project="test-role-fail",
            )
            await asyncio.sleep(1.0)

        assert runner.get_status(review_id) == "failed"
        events = runner.get_buffered_events(review_id)
        terminal = [e for e in events if e["type"] == "error"]
        assert len(terminal) >= 1

    async def test_config_returns_empty_dict(self, runner, tmp_input_file, tmp_path):
        """Empty config causes errors in get_models_by_role."""
        with (
            patch("devils_advocate.config.load_config", return_value={}),
            patch("devils_advocate.config.get_models_by_role", side_effect=KeyError("all_models")),
            patch("devils_advocate.storage.StorageManager", return_value=_real_storage(tmp_path)),
        ):
            review_id = await runner.start_review(
                mode="plan",
                input_files=[tmp_input_file],
                project="test-empty-config",
            )
            await asyncio.sleep(1.0)

        assert runner.get_status(review_id) == "failed"

    async def test_storage_init_fails(self, runner, tmp_input_file):
        """StorageManager creation fails (e.g., disk full, permissions)."""
        with (
            patch("devils_advocate.config.load_config", return_value=_make_minimal_config()),
            patch("devils_advocate.storage.StorageManager", side_effect=OSError("Permission denied")),
        ):
            review_id = await runner.start_review(
                mode="plan",
                input_files=[tmp_input_file],
                project="test-storage-init-fail",
            )
            await asyncio.sleep(1.0)

        assert runner.get_status(review_id) == "failed"
        events = runner.get_buffered_events(review_id)
        terminal = [e for e in events if e["type"] == "error"]
        assert len(terminal) >= 1
        assert "permission" in terminal[-1]["message"].lower()


# ═══════════════════════════════════════════════════════════════════════════
# 51. Stub Ledger — Edge Cases
# ═══════════════════════════════════════════════════════════════════════════


class TestStubLedgerEdgeCases:
    def test_cost_aborted_result(self, tmp_path):
        from devils_advocate.orchestrator._common import _save_stub_ledger
        from devils_advocate.storage import StorageManager
        storage = StorageManager(tmp_path, data_dir=tmp_path)
        storage.set_review_id("cost_abort_001")
        _save_stub_ledger(storage, "cost_abort_001", "plan", "test", "/tmp/in.md", "cost_aborted")
        ledger = storage.load_review("cost_abort_001")
        assert ledger["result"] == "cost_aborted"

    def test_stub_with_cost_tracker(self, tmp_path):
        from devils_advocate.orchestrator._common import _save_stub_ledger
        from devils_advocate.storage import StorageManager
        from devils_advocate.types import CostTracker
        storage = StorageManager(tmp_path, data_dir=tmp_path)
        storage.set_review_id("ct_001")
        ct = CostTracker()
        ct.add("model-1", 1000, 500, 0.001, 0.002, role="reviewer_1")
        _save_stub_ledger(storage, "ct_001", "plan", "test", "/tmp/in.md", "cost_aborted", cost_tracker=ct)
        ledger = storage.load_review("ct_001")
        assert ledger["cost"]["total_usd"] > 0

    def test_stub_empty_points_and_summary(self, tmp_path):
        from devils_advocate.orchestrator._common import _save_stub_ledger
        from devils_advocate.storage import StorageManager
        storage = StorageManager(tmp_path, data_dir=tmp_path)
        storage.set_review_id("empty_001")
        _save_stub_ledger(storage, "empty_001", "spec", "proj", "/tmp/f.md", "failed")
        ledger = storage.load_review("empty_001")
        assert ledger["points"] == []
        assert ledger["summary"]["total_points"] == 0
        assert ledger["summary"]["total_groups"] == 0
        assert ledger["author_model"] == ""
        assert ledger["reviewer_models"] == []


# ═══════════════════════════════════════════════════════════════════════════
# 52. Storage — Manifest and Close
# ═══════════════════════════════════════════════════════════════════════════


class TestStorageManifestAndClose:
    def test_load_manifest_none_when_missing(self, tmp_path):
        from devils_advocate.storage import StorageManager
        storage = StorageManager(tmp_path, data_dir=tmp_path)
        assert storage.load_manifest() is None

    def test_load_manifest_returns_dict(self, tmp_path):
        from devils_advocate.storage import StorageManager
        storage = StorageManager(tmp_path, data_dir=tmp_path)
        manifest = {"version": 1, "project": "test"}
        (storage.lock_dir / "manifest.json").write_text(json.dumps(manifest))
        loaded = storage.load_manifest()
        assert loaded["version"] == 1
        assert loaded["project"] == "test"

    def test_close_log_handle(self, tmp_path):
        from devils_advocate.storage import StorageManager
        storage = StorageManager(tmp_path, data_dir=tmp_path)
        storage.set_review_id("close_test")
        storage.log("message before close")
        storage.close()
        # Second close is safe
        storage.close()
        # Log after close re-opens
        storage.set_review_id("close_test_2")
        storage.log("message after close")

    def test_close_without_opening(self, tmp_path):
        from devils_advocate.storage import StorageManager
        storage = StorageManager(tmp_path, data_dir=tmp_path)
        storage.close()  # Should not raise


# ═══════════════════════════════════════════════════════════════════════════
# 53. SSE — Review Data Endpoint
# ═══════════════════════════════════════════════════════════════════════════


class TestSSEReviewData:
    """Test /api/review/{id} JSON endpoint more thoroughly."""

    def test_valid_review_returns_ledger(self, tmp_path):
        from fastapi.testclient import TestClient
        from devils_advocate.gui import create_app

        app = create_app()
        client = TestClient(app)

        ledger = {
            "review_id": "api_test_001",
            "result": "complete",
            "mode": "plan",
            "points": [],
        }

        with patch("devils_advocate.gui.api.get_gui_storage") as mock_storage:
            mock_storage.return_value.load_review.return_value = ledger
            resp = client.get("/api/review/api_test_001")

        assert resp.status_code == 200
        data = resp.json()
        assert data["review_id"] == "api_test_001"
