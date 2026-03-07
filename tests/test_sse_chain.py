"""End-to-end SSE event chain tests.

Covers the full path from storage.log() -> hooked_log() -> classify_log_message()
-> emit_event() -> queue -> get_buffered_events() -> SSE formatting.

Tests verify that real log messages from actual review runs produce the
correct sequence of events that the GUI dashboard expects.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helpers import make_model_config


def _isolated_storage(tmp_path):
    from devils_advocate.storage import StorageManager
    return StorageManager(tmp_path, data_dir=tmp_path)


def _make_roles(reviewer_names=None, context_limit=100000):
    author = make_model_config(name="author-model")
    reviewers = [
        make_model_config(name=n, context_window=context_limit)
        for n in (reviewer_names or ["reviewer-1"])
    ]
    dedup = make_model_config(name="dedup-model")
    norm = make_model_config(name="norm-model")
    revision = make_model_config(name="revision-model")
    return {
        "author": author,
        "reviewers": reviewers,
        "dedup": dedup,
        "normalization": norm,
        "revision": revision,
    }


def _make_config_with_reviewers(*reviewer_names):
    author = make_model_config(name="author-model")
    reviewers = [make_model_config(name=n) for n in reviewer_names]
    dedup = make_model_config(name="dedup-model")
    norm = make_model_config(name="norm-model")
    revision = make_model_config(name="revision-model")
    return {
        "all_models": {
            m.name: m for m in [author, dedup, norm, revision] + reviewers
        },
        "models": {},
        "config_path": "/tmp/test-models.yaml",
    }


# ═══════════════════════════════════════════════════════════════════════════
# 1. classify_log_message — Phase Detection
# ═══════════════════════════════════════════════════════════════════════════


class TestClassifyLogMessagePhases:
    """Test every phase pattern in _PHASE_PATTERNS."""

    def test_review_start(self):
        from devils_advocate.gui.progress import classify_log_message
        ev = classify_log_message("Starting plan review for project 'board-foot'")
        assert ev.phase == "review_start"
        assert ev.event_type == "phase"

    def test_round1_calling_reviewer(self):
        from devils_advocate.gui.progress import classify_log_message
        ev = classify_log_message("Round 1: calling gemini-3-flash-preview (sent: 3270, timeout: 900s)")
        assert ev.phase == "round1_calling"

    def test_round1_responded(self):
        from devils_advocate.gui.progress import classify_log_message
        ev = classify_log_message("Round 1: gemini-3-flash-preview responded (recv: 1004)")
        assert ev.phase == "round1_responded"

    def test_round1_author(self):
        from devils_advocate.gui.progress import classify_log_message
        ev = classify_log_message("Round 1: calling author to respond to grouped feedback (sent: 5111)")
        assert ev.phase == "round1_author"

    def test_round1_author_responded(self):
        from devils_advocate.gui.progress import classify_log_message
        ev = classify_log_message("Round 1: author responded (recv: 2818)")
        assert ev.phase == "round1_author_responded"

    def test_normalization_no_points(self):
        from devils_advocate.gui.progress import classify_log_message
        ev = classify_log_message("No structured points from kimi-k2-thinking -- trying LLM normalization")
        assert ev.phase == "normalization"

    def test_normalization_calling(self):
        from devils_advocate.gui.progress import classify_log_message
        ev = classify_log_message("Normalization: calling gemini-3-pro-preview (fallback for kimi-k2-thinking, sent: 160)")
        assert ev.phase == "normalization"

    def test_dedup_calling(self):
        from devils_advocate.gui.progress import classify_log_message
        ev = classify_log_message("Deduplication: calling gemini-3-pro-preview (7 points, sent: 1424)")
        assert ev.phase == "dedup_calling"

    def test_dedup_responded(self):
        from devils_advocate.gui.progress import classify_log_message
        ev = classify_log_message("Deduplication: gemini-3-pro-preview responded (recv: 344)")
        assert ev.phase == "dedup_responded"

    def test_round2_sending(self):
        from devils_advocate.gui.progress import classify_log_message
        ev = classify_log_message("Round 2: sending author responses to reviewers for rebuttal")
        assert ev.phase == "round2_sending"

    def test_round2_calling_reviewer(self):
        from devils_advocate.gui.progress import classify_log_message
        ev = classify_log_message("Round 2: calling gemini-3-flash-preview (sent: 4422)")
        assert ev.phase == "round2_calling"

    def test_round2_responded(self):
        from devils_advocate.gui.progress import classify_log_message
        ev = classify_log_message("Round 2: gemini-3-flash-preview responded (recv: 213)")
        assert ev.phase == "round2_responded"

    def test_round2_complete(self):
        from devils_advocate.gui.progress import classify_log_message
        ev = classify_log_message("Round 2: rebuttals complete -- 1 challenge(s)")
        assert ev.phase == "round2_complete"

    def test_round2_author_last_word(self):
        from devils_advocate.gui.progress import classify_log_message
        ev = classify_log_message("Round 2: giving author last word on 1 challenge(s)")
        assert ev.phase == "round2_author"

    def test_round2_author_calling(self):
        from devils_advocate.gui.progress import classify_log_message
        ev = classify_log_message("Round 2: calling author to respond to rebuttals (sent: 3875)")
        assert ev.phase == "round2_author_calling"

    def test_round2_author_responded(self):
        from devils_advocate.gui.progress import classify_log_message
        ev = classify_log_message("Round 2: author responded (recv: 2083)")
        assert ev.phase == "round2_author_responded"

    def test_governance_applying(self):
        from devils_advocate.gui.progress import classify_log_message
        ev = classify_log_message("Governance: applying deterministic rules")
        assert ev.phase == "governance_applying"

    def test_governance_complete(self):
        from devils_advocate.gui.progress import classify_log_message
        ev = classify_log_message("Governance complete: {'total_groups': 6, 'total_points': 7}")
        assert ev.phase == "governance_complete"

    def test_governance_catastrophic(self):
        from devils_advocate.gui.progress import classify_log_message
        ev = classify_log_message("Catastrophic parse failure (<25% coverage) -- escalating all groups")
        assert ev.phase == "governance_catastrophic"

    def test_revision_generating(self):
        from devils_advocate.gui.progress import classify_log_message
        ev = classify_log_message("Revision: generating revised artifact with authors final input")
        assert ev.phase == "revision_generating"

    def test_revision_duration_estimate(self):
        from devils_advocate.gui.progress import classify_log_message
        ev = classify_log_message("Revision: large context (~6,390 tokens) — expect ~3 min")
        assert ev.phase == "revision_duration_estimate"

    def test_revision_calling(self):
        from devils_advocate.gui.progress import classify_log_message
        ev = classify_log_message("Revision: calling minimax-m2.5 to incorporate 4 findings")
        assert ev.phase == "revision_calling"

    def test_revision_responded(self):
        from devils_advocate.gui.progress import classify_log_message
        ev = classify_log_message("Revision: minimax-m2.5 responded (recv: 7626)")
        assert ev.phase == "revision_responded"

    def test_revision_skip(self):
        from devils_advocate.gui.progress import classify_log_message
        ev = classify_log_message("Revision: no actionable findings")
        assert ev.phase == "revision_skip"

    def test_revision_skip_context(self):
        from devils_advocate.gui.progress import classify_log_message
        ev = classify_log_message("Revision: prompt (50000 tokens) exceeds context limit")
        assert ev.phase == "revision_skip_context"

    def test_revision_extraction_failed(self):
        from devils_advocate.gui.progress import classify_log_message
        ev = classify_log_message("Revision: extraction failed — could not find delimiters")
        assert ev.phase == "revision_extraction_failed"

    def test_revision_failed(self):
        from devils_advocate.gui.progress import classify_log_message
        ev = classify_log_message("Revision failed (non-fatal): HTTP 429")
        assert ev.phase == "revision_failed"

    def test_round2_skip(self):
        from devils_advocate.gui.progress import classify_log_message
        ev = classify_log_message("Round 2: all groups accepted by author -- skipping rebuttals")
        assert ev.phase == "round2_skip"

    def test_round2_skip_reviewer(self):
        from devils_advocate.gui.progress import classify_log_message
        ev = classify_log_message("Round 2: gemini-flash has no contested groups -- skipping")
        assert ev.phase == "round2_skip_reviewer"

    def test_round2_rebuttal_failed(self):
        from devils_advocate.gui.progress import classify_log_message
        ev = classify_log_message("Rebuttal kimi-k2 failed: HTTP 429")
        assert ev.phase == "round2_rebuttal_failed"

    def test_round2_author_failed(self):
        from devils_advocate.gui.progress import classify_log_message
        ev = classify_log_message("Author final response failed: HTTP 502")
        assert ev.phase == "round2_author_failed"

    def test_cost_warning(self):
        from devils_advocate.gui.progress import classify_log_message
        ev = classify_log_message("Cost warning: $0.8000 (80% of $1.00)")
        assert ev.phase == "cost_warning"

    def test_cost_exceeded(self):
        from devils_advocate.gui.progress import classify_log_message
        ev = classify_log_message("Cost limit exceeded: $1.0500 >= $1.00")
        assert ev.phase == "cost_exceeded"

    def test_skipping_rebuttal_context(self):
        from devils_advocate.gui.progress import classify_log_message
        ev = classify_log_message("Skipping kimi-k2 rebuttal: context exceeded")
        assert ev.phase == "round2_skip_context"


# ═══════════════════════════════════════════════════════════════════════════
# 2. classify_log_message — Cost Events
# ═══════════════════════════════════════════════════════════════════════════


class TestClassifyCostEvents:
    """Cost events have structured data and suppress console output."""

    def test_cost_event_structure(self):
        from devils_advocate.gui.progress import classify_log_message

        msg = "§cost role=reviewer_1 model=gemini-3-flash-preview cost=0.004691 total=0.004691 in_tokens=3358 out_tokens=1004 total_tokens=4362"
        ev = classify_log_message(msg)

        assert ev.event_type == "cost"
        assert ev.phase == "cost_update"
        assert ev.message == ""  # Cost events suppress message
        assert ev.detail["role"] == "reviewer_1"
        assert ev.detail["model"] == "gemini-3-flash-preview"
        assert ev.detail["cost"] == "0.004691"
        assert ev.detail["total"] == "0.004691"
        assert ev.detail["in_tokens"] == "3358"
        assert ev.detail["out_tokens"] == "1004"
        assert ev.detail["total_tokens"] == "4362"

    def test_cost_event_without_token_breakdown(self):
        from devils_advocate.gui.progress import classify_log_message

        msg = "§cost role=dedup model=deepseek cost=0.001 total=0.005"
        ev = classify_log_message(msg)

        assert ev.event_type == "cost"
        assert ev.detail["role"] == "dedup"
        assert "in_tokens" not in ev.detail

    def test_normalization_cost_event(self):
        from devils_advocate.gui.progress import classify_log_message

        msg = "§cost role=normalization model=gemini-3-pro-preview cost=0.001720 total=0.013380 in_tokens=140 out_tokens=120 total_tokens=9755"
        ev = classify_log_message(msg)

        assert ev.event_type == "cost"
        assert ev.detail["role"] == "normalization"


# ═══════════════════════════════════════════════════════════════════════════
# 3. classify_log_message — Unclassified Lines
# ═══════════════════════════════════════════════════════════════════════════


class TestClassifyUnclassified:
    def test_unknown_message(self):
        from devils_advocate.gui.progress import classify_log_message
        ev = classify_log_message("Some random unrecognized message")
        assert ev.event_type == "log"
        assert ev.phase == "unknown"
        assert ev.message == "Some random unrecognized message"

    def test_http_retry_not_classified(self):
        from devils_advocate.gui.progress import classify_log_message
        ev = classify_log_message("  kimi-k2-thinking: HTTP 429, retry 1/3 in 1.4s")
        assert ev.event_type == "log"  # Not a known phase


# ═══════════════════════════════════════════════════════════════════════════
# 4. make_terminal_event
# ═══════════════════════════════════════════════════════════════════════════


class TestMakeTerminalEvent:
    def test_success_event(self):
        from devils_advocate.gui.progress import make_terminal_event
        ev = make_terminal_event(True)
        assert ev.event_type == "complete"
        assert ev.phase == "done"
        assert ev.message == "Review complete"

    def test_failure_event(self):
        from devils_advocate.gui.progress import make_terminal_event
        ev = make_terminal_event(False)
        assert ev.event_type == "error"
        assert ev.phase == "error"
        assert ev.message == "Review failed"

    def test_custom_success_message(self):
        from devils_advocate.gui.progress import make_terminal_event
        ev = make_terminal_event(True, "All done!")
        assert ev.message == "All done!"

    def test_custom_failure_message(self):
        from devils_advocate.gui.progress import make_terminal_event
        ev = make_terminal_event(False, "Timed out after 30 minutes")
        assert ev.message == "Timed out after 30 minutes"


# ═══════════════════════════════════════════════════════════════════════════
# 5. ProgressEvent — to_sse Format
# ═══════════════════════════════════════════════════════════════════════════


class TestProgressEventSSE:
    def test_basic_sse_format(self):
        from devils_advocate.gui.progress import ProgressEvent
        ev = ProgressEvent(event_type="phase", message="Test message", phase="review_start")
        sse = ev.to_sse()

        assert sse.startswith("data: ")
        assert sse.endswith("\n\n")
        data = json.loads(sse[6:].strip())
        assert data["type"] == "phase"
        assert data["message"] == "Test message"
        assert data["phase"] == "review_start"

    def test_cost_event_sse(self):
        from devils_advocate.gui.progress import ProgressEvent
        ev = ProgressEvent(
            event_type="cost",
            phase="cost_update",
            detail={"role": "reviewer_1", "cost": "0.01"},
        )
        sse = ev.to_sse()
        data = json.loads(sse[6:].strip())
        assert data["type"] == "cost"
        assert data["detail"]["role"] == "reviewer_1"

    def test_auto_timestamp(self):
        from devils_advocate.gui.progress import ProgressEvent
        ev = ProgressEvent(event_type="log", message="test")
        assert ev.timestamp != ""
        # Should look like HH:MM:SS
        parts = ev.timestamp.split(":")
        assert len(parts) == 3

    def test_explicit_timestamp(self):
        from devils_advocate.gui.progress import ProgressEvent
        ev = ProgressEvent(event_type="log", message="test", timestamp="12:34:56")
        assert ev.timestamp == "12:34:56"


# ═══════════════════════════════════════════════════════════════════════════
# 6. ReviewRunner — emit_event and Queue Behavior
# ═══════════════════════════════════════════════════════════════════════════


class TestRunnerEmitEvent:
    def test_emit_adds_to_buffer(self):
        from devils_advocate.gui.runner import ReviewRunner
        from devils_advocate.gui.progress import ProgressEvent

        runner = ReviewRunner()
        runner.active["test_001"] = {
            "queue": asyncio.Queue(maxsize=500),
            "buffered": [],
            "state": "running",
            "created_at": time.time(),
            "last_event_at": time.time(),
        }

        ev = ProgressEvent(event_type="phase", message="Test", phase="review_start")
        runner.emit_event("test_001", ev)

        assert len(runner.active["test_001"]["buffered"]) == 1
        assert runner.active["test_001"]["buffered"][0]["type"] == "phase"

    def test_emit_adds_to_queue(self):
        from devils_advocate.gui.runner import ReviewRunner
        from devils_advocate.gui.progress import ProgressEvent

        runner = ReviewRunner()
        queue = asyncio.Queue(maxsize=500)
        runner.active["test_001"] = {
            "queue": queue,
            "buffered": [],
            "state": "running",
            "created_at": time.time(),
            "last_event_at": time.time(),
        }

        ev = ProgressEvent(event_type="phase", message="Test", phase="review_start")
        runner.emit_event("test_001", ev)

        assert queue.qsize() == 1

    def test_emit_to_unknown_review_is_safe(self):
        from devils_advocate.gui.runner import ReviewRunner
        from devils_advocate.gui.progress import ProgressEvent

        runner = ReviewRunner()
        ev = ProgressEvent(event_type="phase", message="Test", phase="review_start")
        runner.emit_event("nonexistent", ev)  # should not raise

    def test_emit_updates_last_event_at(self):
        from devils_advocate.gui.runner import ReviewRunner
        from devils_advocate.gui.progress import ProgressEvent

        runner = ReviewRunner()
        old_time = time.time() - 100
        runner.active["test_001"] = {
            "queue": asyncio.Queue(maxsize=500),
            "buffered": [],
            "state": "running",
            "created_at": old_time,
            "last_event_at": old_time,
        }

        ev = ProgressEvent(event_type="phase", message="Test", phase="review_start")
        runner.emit_event("test_001", ev)

        assert runner.active["test_001"]["last_event_at"] > old_time

    def test_queue_full_drops_oldest(self):
        from devils_advocate.gui.runner import ReviewRunner
        from devils_advocate.gui.progress import ProgressEvent

        runner = ReviewRunner()
        queue = asyncio.Queue(maxsize=2)
        runner.active["test_001"] = {
            "queue": queue,
            "buffered": [],
            "state": "running",
            "created_at": time.time(),
            "last_event_at": time.time(),
        }

        # Fill the queue
        for i in range(3):
            ev = ProgressEvent(event_type="phase", message=f"Msg {i}", phase="test")
            runner.emit_event("test_001", ev)

        # Queue maxsize=2, so oldest should have been dropped
        assert queue.qsize() == 2
        # Buffer has all 3
        assert len(runner.active["test_001"]["buffered"]) == 3


# ═══════════════════════════════════════════════════════════════════════════
# 7. ReviewRunner — get_buffered_events
# ═══════════════════════════════════════════════════════════════════════════


class TestRunnerGetBufferedEvents:
    def test_returns_copy(self):
        from devils_advocate.gui.runner import ReviewRunner
        from devils_advocate.gui.progress import ProgressEvent

        runner = ReviewRunner()
        runner.active["test_001"] = {
            "queue": asyncio.Queue(maxsize=500),
            "buffered": [],
            "state": "running",
            "created_at": time.time(),
            "last_event_at": time.time(),
        }

        ev = ProgressEvent(event_type="phase", message="Test", phase="start")
        runner.emit_event("test_001", ev)

        events = runner.get_buffered_events("test_001")
        assert len(events) == 1

        # Modifying returned list shouldn't affect internal buffer
        events.clear()
        assert len(runner.get_buffered_events("test_001")) == 1

    def test_unknown_review_returns_empty(self):
        from devils_advocate.gui.runner import ReviewRunner

        runner = ReviewRunner()
        assert runner.get_buffered_events("nonexistent") == []


# ═══════════════════════════════════════════════════════════════════════════
# 8. ReviewRunner — get_queue
# ═══════════════════════════════════════════════════════════════════════════


class TestRunnerGetQueue:
    def test_returns_queue_for_active(self):
        from devils_advocate.gui.runner import ReviewRunner

        runner = ReviewRunner()
        queue = asyncio.Queue(maxsize=500)
        runner.active["test_001"] = {
            "queue": queue,
            "buffered": [],
            "state": "running",
            "created_at": time.time(),
            "last_event_at": time.time(),
        }

        assert runner.get_queue("test_001") is queue

    def test_returns_none_for_unknown(self):
        from devils_advocate.gui.runner import ReviewRunner

        runner = ReviewRunner()
        assert runner.get_queue("nonexistent") is None


# ═══════════════════════════════════════════════════════════════════════════
# 9. ReviewRunner — cancel_review
# ═══════════════════════════════════════════════════════════════════════════


class TestRunnerCancelReview:
    def test_cancel_nonexistent_returns_false(self):
        from devils_advocate.gui.runner import ReviewRunner

        runner = ReviewRunner()
        assert runner.cancel_review("nonexistent") is False

    def test_cancel_completed_returns_false(self):
        from devils_advocate.gui.runner import ReviewRunner

        runner = ReviewRunner()
        runner.current_review_id = "test_001"
        runner.current_task = MagicMock()
        runner.current_task.done.return_value = True

        assert runner.cancel_review("test_001") is False

    def test_cancel_wrong_review_returns_false(self):
        from devils_advocate.gui.runner import ReviewRunner

        runner = ReviewRunner()
        runner.current_review_id = "test_001"
        runner.current_task = MagicMock()
        runner.current_task.done.return_value = False

        assert runner.cancel_review("different_review") is False

    def test_cancel_running_returns_true(self):
        from devils_advocate.gui.runner import ReviewRunner

        runner = ReviewRunner()
        runner.current_review_id = "test_001"
        runner.current_task = MagicMock()
        runner.current_task.done.return_value = False

        assert runner.cancel_review("test_001") is True
        runner.current_task.cancel.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════
# 10. ReviewRunner — Hooked Log Integration
# ═══════════════════════════════════════════════════════════════════════════


class TestHookedLogIntegration:
    """Verify the hooked_log function in _run() emits proper events."""

    async def test_hooked_log_emits_phase_events(self, tmp_path):
        from devils_advocate.gui.runner import ReviewRunner
        from devils_advocate.gui.progress import classify_log_message

        runner = ReviewRunner()

        storage = _isolated_storage(tmp_path)
        storage.set_review_id("hook_test")

        # Simulate the hooked_log setup from _run()
        queue = asyncio.Queue(maxsize=500)
        buffered = []
        review_id = "hook_test"

        runner.active[review_id] = {
            "queue": queue,
            "buffered": buffered,
            "state": "running",
            "created_at": time.time(),
            "last_event_at": time.time(),
        }

        original_log = storage.log
        _last_emitted_msg = [None]

        def hooked_log(msg: str) -> None:
            original_log(msg)
            event = classify_log_message(msg)
            if event.message and event.message == _last_emitted_msg[0]:
                return
            _last_emitted_msg[0] = event.message
            runner.emit_event(review_id, event)

        storage.log = hooked_log

        # Send log messages that match known phases
        storage.log("Starting plan review for project 'test'")
        storage.log("Round 1: calling gemini-flash (sent: 100, timeout: 900s)")
        storage.log("§cost role=reviewer_1 model=gemini-flash cost=0.01 total=0.01")
        storage.log("Round 1: gemini-flash responded (recv: 500)")

        events = runner.get_buffered_events(review_id)
        phases = [e["phase"] for e in events]

        assert "review_start" in phases
        assert "round1_calling" in phases
        assert "cost_update" in phases
        assert "round1_responded" in phases

    async def test_hooked_log_dedup_consecutive_identical(self, tmp_path):
        from devils_advocate.gui.runner import ReviewRunner
        from devils_advocate.gui.progress import classify_log_message

        runner = ReviewRunner()

        storage = _isolated_storage(tmp_path)
        storage.set_review_id("dedup_test")

        review_id = "dedup_test"
        runner.active[review_id] = {
            "queue": asyncio.Queue(maxsize=500),
            "buffered": [],
            "state": "running",
            "created_at": time.time(),
            "last_event_at": time.time(),
        }

        original_log = storage.log
        _last_emitted_msg = [None]

        def hooked_log(msg: str) -> None:
            original_log(msg)
            event = classify_log_message(msg)
            if event.message and event.message == _last_emitted_msg[0]:
                return
            _last_emitted_msg[0] = event.message
            runner.emit_event(review_id, event)

        storage.log = hooked_log

        # Send same message twice — second should be deduped
        storage.log("Starting plan review for project 'test'")
        storage.log("Starting plan review for project 'test'")

        events = runner.get_buffered_events(review_id)
        start_events = [e for e in events if e["phase"] == "review_start"]
        assert len(start_events) == 1

    async def test_cost_events_not_deduped(self, tmp_path):
        """Cost events have empty message, so dedup check skips them."""
        from devils_advocate.gui.runner import ReviewRunner
        from devils_advocate.gui.progress import classify_log_message

        runner = ReviewRunner()

        storage = _isolated_storage(tmp_path)
        storage.set_review_id("cost_dedup")

        review_id = "cost_dedup"
        runner.active[review_id] = {
            "queue": asyncio.Queue(maxsize=500),
            "buffered": [],
            "state": "running",
            "created_at": time.time(),
            "last_event_at": time.time(),
        }

        original_log = storage.log
        _last_emitted_msg = [None]

        def hooked_log(msg: str) -> None:
            original_log(msg)
            event = classify_log_message(msg)
            if event.message and event.message == _last_emitted_msg[0]:
                return
            _last_emitted_msg[0] = event.message
            runner.emit_event(review_id, event)

        storage.log = hooked_log

        # Two cost events with same model — both should emit (message is "")
        storage.log("§cost role=reviewer_1 model=gemini cost=0.01 total=0.01")
        storage.log("§cost role=reviewer_1 model=gemini cost=0.02 total=0.03")

        events = runner.get_buffered_events(review_id)
        cost_events = [e for e in events if e["type"] == "cost"]
        assert len(cost_events) == 2


# ═══════════════════════════════════════════════════════════════════════════
# 11. Full SSE Sequence from Real Log
# ═══════════════════════════════════════════════════════════════════════════


class TestRealLogSequence:
    """Replay a real log sequence and verify correct event ordering."""

    def test_board_foot_log_sequence(self):
        """Replay log messages from the actual board-foot review."""
        from devils_advocate.gui.progress import classify_log_message

        log_messages = [
            "Starting plan review for project 'board-foot'",
            "Primary input: /home/kelleyb/Desktop/Board Foot Android App/boardfoot.sample.plan.md (9783 chars, ~2445 tokens)",
            "Reference input: /home/kelleyb/Desktop/Board Foot Android App/boardfoot.sample.spec.txt (2288 chars)",
            "Total prompt content: 12463 chars, ~3115 tokens",
            "Review ID: 20260307T013337_5ba2af",
            "Author: claude-haiku-4-5, Reviewers: gemini-3-flash-preview, kimi-k2-thinking",
            "Dedup: gemini-3-pro-preview",
            "Round 1: calling gemini-3-flash-preview (sent: 3270, timeout: 900s, max_out: 19660/19660, thinking: on)",
            "Round 1: calling kimi-k2-thinking (sent: 3270, timeout: 900s, max_out: 19660/19660, thinking: on)",
            "  kimi-k2-thinking: HTTP 429, retry 1/3 in 1.4s",
            "§cost role=reviewer_1 model=gemini-3-flash-preview cost=0.004691 total=0.004691 in_tokens=3358 out_tokens=1004 total_tokens=4362",
            "Round 1: gemini-3-flash-preview responded (recv: 1004)",
            "§cost role=reviewer_2 model=kimi-k2-thinking cost=0.006969 total=0.011660 in_tokens=3086 out_tokens=2047 total_tokens=9495",
            "Round 1: kimi-k2-thinking responded (recv: 2047)",
            "  No structured points from kimi-k2-thinking -- trying LLM normalization",
            "  Normalization: calling gemini-3-pro-preview (fallback for kimi-k2-thinking, sent: 160, timeout: 900s, max_out: 19660/16384, thinking: off)",
            "§cost role=normalization model=gemini-3-pro-preview cost=0.001720 total=0.013380 in_tokens=140 out_tokens=120 total_tokens=9755",
            "  Deduplication: calling gemini-3-pro-preview (7 points, sent: 1424, timeout: 900s, max_out: 19660/16384, thinking: off)",
            "§cost role=dedup model=gemini-3-pro-preview cost=0.006820 total=0.020200 in_tokens=1346 out_tokens=344 total_tokens=11445",
            "  Deduplication: gemini-3-pro-preview responded (recv: 344)",
            "  Deduplication: combined 7 points into 6 groups",
            "Round 1: calling author to respond to grouped feedback (sent: 5111, timeout: 120s, max_out: 10000/32000, thinking: on)",
            "§cost role=author model=claude-haiku-4-5 cost=0.015757 total=0.035957 in_tokens=5606 out_tokens=2818 total_tokens=19869",
            "Round 1: author responded (recv: 2818)",
            "Round 2: sending author responses to reviewers for rebuttal",
            "Round 2: calling gemini-3-flash-preview (sent: 4422, timeout: 900s)",
            "Round 2: calling kimi-k2-thinking (sent: 4035, timeout: 900s)",
            "§cost role=reviewer_1 model=gemini-3-flash-preview cost=0.002887 total=0.038843 in_tokens=4495 out_tokens=213 total_tokens=24577",
            "Round 2: gemini-3-flash-preview responded (recv: 213)",
            "§cost role=reviewer_2 model=kimi-k2-thinking cost=0.007416 total=0.046259 in_tokens=3830 out_tokens=2047 total_tokens=30454",
            "Round 2: kimi-k2-thinking responded (recv: 2047)",
            "Round 2: rebuttals complete -- 1 challenge(s)",
            "Round 2: giving author last word on 1 challenge(s)",
            "Round 2: calling author to respond to rebuttals (sent: 3875, timeout: 120s)",
            "§cost role=author model=claude-haiku-4-5 cost=0.011687 total=0.057946 in_tokens=4194 out_tokens=2083 total_tokens=36731",
            "Round 2: author responded (recv: 2083)",
            "Governance: applying deterministic rules",
            "Governance complete: {'total_groups': 6, 'total_points': 7, 'auto_accepted': 3, 'escalated': 2, 'auto_dismissed': 1}",
            "Revision: generating revised artifact with authors final input",
            "Revision: large context (~6,390 tokens) — expect ~3 min",
            "Revision: calling minimax-m2.5 to incorporate 4 findings (sent: 6390, timeout: 900s, max_out: 19660/19660, thinking: off)",
            "§cost role=revision model=minimax-m2.5 cost=0.010890 total=0.068836 in_tokens=5797 out_tokens=7626 total_tokens=50154",
            "Revision: minimax-m2.5 responded (recv: 7626)",
        ]

        events = [classify_log_message(msg) for msg in log_messages]
        classified_phases = [e.phase for e in events if e.phase != "unknown"]

        # Verify the essential phase sequence is present
        expected_sequence = [
            "review_start",
            "round1_calling",
            "round1_calling",
            "cost_update",
            "round1_responded",
            "cost_update",
            "round1_responded",
            "normalization",
            "normalization",
            "cost_update",
            "dedup_calling",
            "cost_update",
            "dedup_responded",
            "round1_author",
            "cost_update",
            "round1_author_responded",
            "round2_sending",
            "round2_calling",
            "round2_calling",
            "cost_update",
            "round2_responded",
            "cost_update",
            "round2_responded",
            "round2_complete",
            "round2_author",
            "round2_author_calling",
            "cost_update",
            "round2_author_responded",
            "governance_applying",
            "governance_complete",
            "revision_generating",
            "revision_duration_estimate",
            "revision_calling",
            "cost_update",
            "revision_responded",
        ]

        assert classified_phases == expected_sequence

    def test_cost_events_count(self):
        """Board-foot log has 9 cost events (3 reviewer_1, 3 reviewer_2, normalization, dedup, revision)."""
        from devils_advocate.gui.progress import classify_log_message

        cost_lines = [
            "§cost role=reviewer_1 model=gemini-3-flash-preview cost=0.004691 total=0.004691 in_tokens=3358 out_tokens=1004 total_tokens=4362",
            "§cost role=reviewer_2 model=kimi-k2-thinking cost=0.006969 total=0.011660 in_tokens=3086 out_tokens=2047 total_tokens=9495",
            "§cost role=normalization model=gemini-3-pro-preview cost=0.001720 total=0.013380 in_tokens=140 out_tokens=120 total_tokens=9755",
            "§cost role=dedup model=gemini-3-pro-preview cost=0.006820 total=0.020200 in_tokens=1346 out_tokens=344 total_tokens=11445",
            "§cost role=author model=claude-haiku-4-5 cost=0.015757 total=0.035957 in_tokens=5606 out_tokens=2818 total_tokens=19869",
            "§cost role=reviewer_1 model=gemini-3-flash-preview cost=0.002887 total=0.038843 in_tokens=4495 out_tokens=213 total_tokens=24577",
            "§cost role=reviewer_2 model=kimi-k2-thinking cost=0.007416 total=0.046259 in_tokens=3830 out_tokens=2047 total_tokens=30454",
            "§cost role=author model=claude-haiku-4-5 cost=0.011687 total=0.057946 in_tokens=4194 out_tokens=2083 total_tokens=36731",
            "§cost role=revision model=minimax-m2.5 cost=0.010890 total=0.068836 in_tokens=5797 out_tokens=7626 total_tokens=50154",
        ]

        for line in cost_lines:
            ev = classify_log_message(line)
            assert ev.event_type == "cost"


# ═══════════════════════════════════════════════════════════════════════════
# 12. Runner _run — Event Ordering with Metadata
# ═══════════════════════════════════════════════════════════════════════════


class TestRunnerEventOrdering:
    """Verify the event sequence emitted during _run() startup."""

    async def test_metadata_event_before_start(self, tmp_path):
        """Metadata event should be emitted before the review_start event."""
        from devils_advocate.gui.runner import ReviewRunner

        runner = ReviewRunner()
        plan = tmp_path / "plan.md"
        plan.write_text("Test plan content")

        storage = _isolated_storage(tmp_path)
        roles = _make_roles(["reviewer-1"])

        # Lock prevents API calls
        storage.acquire_lock()

        with (
            patch("devils_advocate.config.load_config", return_value=_make_config_with_reviewers("reviewer-1")),
            patch("devils_advocate.config.get_models_by_role", return_value=roles),
            patch("devils_advocate.orchestrator.plan.get_models_by_role", return_value=roles),
            patch("devils_advocate.orchestrator.plan.check_context_window", return_value=(True, 100, 100000)),
            patch("devils_advocate.storage.StorageManager", return_value=storage),
        ):
            review_id = await runner.start_review(
                mode="plan", input_files=[plan], project="test",
            )
            await asyncio.sleep(0.5)

        events = runner.get_buffered_events(review_id)
        types = [e["type"] for e in events]

        # Metadata should come before phase events
        assert "metadata" in types
        metadata_idx = types.index("metadata")
        phase_indices = [i for i, t in enumerate(types) if t == "phase"]
        if phase_indices:
            assert metadata_idx < phase_indices[0]

    async def test_metadata_contains_role_mapping(self, tmp_path):
        from devils_advocate.gui.runner import ReviewRunner

        runner = ReviewRunner()
        plan = tmp_path / "plan.md"
        plan.write_text("Test plan for metadata")

        storage = _isolated_storage(tmp_path)
        roles = _make_roles(["reviewer-1"])
        storage.acquire_lock()

        with (
            patch("devils_advocate.config.load_config", return_value=_make_config_with_reviewers("reviewer-1")),
            patch("devils_advocate.config.get_models_by_role", return_value=roles),
            patch("devils_advocate.orchestrator.plan.get_models_by_role", return_value=roles),
            patch("devils_advocate.orchestrator.plan.check_context_window", return_value=(True, 100, 100000)),
            patch("devils_advocate.storage.StorageManager", return_value=storage),
        ):
            review_id = await runner.start_review(
                mode="plan", input_files=[plan], project="test",
            )
            await asyncio.sleep(0.5)

        events = runner.get_buffered_events(review_id)
        meta_events = [e for e in events if e["type"] == "metadata"]
        assert len(meta_events) >= 1

        detail = meta_events[0]["detail"]
        assert detail["mode"] == "plan"
        assert detail["project"] == "test"
        assert "author" in detail["roles"]
        assert "reviewer_1" in detail["roles"]
