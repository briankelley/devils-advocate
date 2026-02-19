"""Tests for GUI runner queue mechanics."""

import asyncio
import pytest

from devils_advocate.gui.runner import ReviewRunner
from devils_advocate.gui.progress import ProgressEvent, make_terminal_event


class TestReviewRunner:
    def test_initial_state(self):
        runner = ReviewRunner()
        assert runner.current_review_id is None
        assert runner.current_task is None
        assert runner.statuses == {}
        assert runner.active == {}

    def test_get_status_unknown(self):
        runner = ReviewRunner()
        assert runner.get_status("nonexistent") == "unknown"

    def test_emit_event_to_queue(self):
        runner = ReviewRunner()
        review_id = "test_review"
        queue = asyncio.Queue(maxsize=100)
        runner.active[review_id] = {
            "queue": queue,
            "buffered": [],
            "state": "running",
        }

        event = ProgressEvent(event_type="log", message="test")
        runner.emit_event(review_id, event)

        assert not queue.empty()
        data = queue.get_nowait()
        assert data["type"] == "log"
        assert data["message"] == "test"

    def test_emit_event_buffers(self):
        runner = ReviewRunner()
        review_id = "test_review"
        buffered = []
        runner.active[review_id] = {
            "queue": asyncio.Queue(maxsize=100),
            "buffered": buffered,
            "state": "running",
        }

        event = ProgressEvent(event_type="phase", message="hello", phase="round1")
        runner.emit_event(review_id, event)

        assert len(buffered) == 1
        assert buffered[0]["message"] == "hello"

    def test_emit_event_drops_oldest_on_full_queue(self):
        runner = ReviewRunner()
        review_id = "test_review"
        queue = asyncio.Queue(maxsize=2)
        runner.active[review_id] = {
            "queue": queue,
            "buffered": [],
            "state": "running",
        }

        # Fill queue
        runner.emit_event(review_id, ProgressEvent(event_type="log", message="msg1"))
        runner.emit_event(review_id, ProgressEvent(event_type="log", message="msg2"))
        # This should drop oldest
        runner.emit_event(review_id, ProgressEvent(event_type="log", message="msg3"))

        # Queue should have msg2 and msg3
        items = []
        while not queue.empty():
            items.append(queue.get_nowait())
        assert len(items) == 2
        assert items[0]["message"] == "msg2"
        assert items[1]["message"] == "msg3"

    def test_emit_event_nonexistent_review(self):
        """Emitting to a nonexistent review should not raise."""
        runner = ReviewRunner()
        event = ProgressEvent(event_type="log", message="test")
        runner.emit_event("nonexistent", event)  # Should not raise

    def test_get_buffered_events(self):
        runner = ReviewRunner()
        review_id = "test_review"
        runner.active[review_id] = {
            "queue": asyncio.Queue(),
            "buffered": [{"type": "log", "message": "buffered msg"}],
            "state": "running",
        }
        events = runner.get_buffered_events(review_id)
        assert len(events) == 1
        assert events[0]["message"] == "buffered msg"

    def test_get_buffered_events_nonexistent(self):
        runner = ReviewRunner()
        assert runner.get_buffered_events("nonexistent") == []

    def test_get_queue_nonexistent(self):
        runner = ReviewRunner()
        assert runner.get_queue("nonexistent") is None

    def test_get_queue_existing(self):
        runner = ReviewRunner()
        queue = asyncio.Queue()
        runner.active["test"] = {
            "queue": queue,
            "buffered": [],
            "state": "running",
        }
        assert runner.get_queue("test") is queue
