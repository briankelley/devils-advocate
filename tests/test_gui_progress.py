"""Tests for GUI progress event model and log parsing."""

from devils_advocate.gui.progress import (
    ProgressEvent,
    classify_log_message,
    make_terminal_event,
)


class TestClassifyLogMessage:
    """Test keyword-based phase inference from storage.log() strings."""

    def test_round1_calling(self):
        ev = classify_log_message("Round 1: calling gpt-4o")
        assert ev.event_type == "phase"
        assert ev.phase == "round1_calling"
        assert "gpt-4o" in ev.detail["groups"]

    def test_round1_responded(self):
        ev = classify_log_message(
            "Round 1: gpt-4o responded (in: 1200, out: 4821 tokens, running total: 6021)"
        )
        assert ev.phase == "round1_responded"
        assert "gpt-4o" in ev.detail["groups"]

    def test_normalization_fallback(self):
        ev = classify_log_message(
            "No structured points from gemini-pro -- trying LLM normalization"
        )
        assert ev.phase == "normalization"

    def test_round1_author(self):
        ev = classify_log_message(
            "Round 1: author responding to grouped feedback "
            "(timeout: 120s, max_out: 32000)"
        )
        assert ev.phase == "round1_author"

    def test_round2_skip_all(self):
        ev = classify_log_message(
            "Round 2: all groups accepted by author -- skipping rebuttals"
        )
        assert ev.phase == "round2_skip"

    def test_round2_skip_reviewer(self):
        ev = classify_log_message(
            "Round 2: gemini-pro has no contested groups -- skipping"
        )
        assert ev.phase == "round2_skip_reviewer"

    def test_context_exceeded(self):
        ev = classify_log_message("Skipping gpt-4o rebuttal: context exceeded")
        assert ev.phase == "round2_skip_context"

    def test_rebuttal_failed(self):
        ev = classify_log_message("Rebuttal gpt-4o failed: timeout")
        assert ev.phase == "round2_rebuttal_failed"

    def test_author_final_failed(self):
        ev = classify_log_message("Author final response failed: API error")
        assert ev.phase == "round2_author_failed"

    def test_catastrophic_parse(self):
        ev = classify_log_message(
            "Catastrophic parse failure (<25% coverage) -- escalating all groups"
        )
        assert ev.phase == "governance_catastrophic"

    def test_governance_complete(self):
        ev = classify_log_message("Governance complete: 5 accepted, 2 escalated")
        assert ev.phase == "governance_complete"

    def test_cost_warning(self):
        ev = classify_log_message("Cost warning: $0.80 (80% of $1.00)")
        assert ev.phase == "cost_warning"

    def test_cost_exceeded(self):
        ev = classify_log_message("Cost limit exceeded: $1.05 >= $1.00")
        assert ev.phase == "cost_exceeded"

    def test_revision_calling(self):
        ev = classify_log_message("Revision: calling claude-sonnet")
        assert ev.phase == "revision_calling"

    def test_revision_responded(self):
        ev = classify_log_message(
            "Revision: claude-sonnet responded (8000 output tokens)"
        )
        assert ev.phase == "revision_responded"

    def test_cost_update_with_tokens(self):
        ev = classify_log_message(
            "§cost role=reviewer model=gpt-4o cost=0.032000 total=0.145000 "
            "in_tokens=1200 out_tokens=4821 total_tokens=6021"
        )
        assert ev.event_type == "cost"
        assert ev.detail["in_tokens"] == "1200"
        assert ev.detail["out_tokens"] == "4821"
        assert ev.detail["total_tokens"] == "6021"

    def test_revision_skip(self):
        ev = classify_log_message("Revision: no actionable findings — skipping")
        assert ev.phase == "revision_skip"

    def test_revision_extraction_failed(self):
        ev = classify_log_message(
            "Revision: extraction failed — canonical delimiters not found in response"
        )
        assert ev.phase == "revision_extraction_failed"

    def test_review_start(self):
        ev = classify_log_message(
            "Starting plan review for project 'atlas-voice'"
        )
        assert ev.phase == "review_start"

    def test_unknown_message(self):
        ev = classify_log_message("Some random log message")
        assert ev.event_type == "log"
        assert ev.phase == "unknown"

    def test_best_effort_degradation(self):
        """Unknown messages should still produce valid events."""
        ev = classify_log_message("Completely unknown log entry xyz")
        assert ev.event_type == "log"
        assert ev.message == "Completely unknown log entry xyz"


class TestMakeTerminalEvent:
    def test_success_event(self):
        ev = make_terminal_event(True)
        assert ev.event_type == "complete"
        assert ev.phase == "done"
        assert "complete" in ev.message.lower()

    def test_error_event(self):
        ev = make_terminal_event(False, "Something broke")
        assert ev.event_type == "error"
        assert ev.phase == "error"
        assert ev.message == "Something broke"


class TestProgressEventSSE:
    def test_to_sse_format(self):
        ev = ProgressEvent(event_type="log", message="test msg", phase="unknown")
        sse = ev.to_sse()
        assert sse.startswith("data: ")
        assert sse.endswith("\n\n")
        import json
        data = json.loads(sse[6:].strip())
        assert data["type"] == "log"
        assert data["message"] == "test msg"


class TestCostEventClassification:
    """Tests for the internal cost event (section-cost prefix)."""

    def test_cost_update_event(self):
        ev = classify_log_message("§cost role=reviewer model=gpt-4o cost=0.032 total=0.145")
        assert ev.event_type == "cost"
        assert ev.phase == "cost_update"
        assert ev.detail["role"] == "reviewer"
        assert ev.detail["model"] == "gpt-4o"
        assert ev.detail["cost"] == "0.032"
        assert ev.detail["total"] == "0.145"
        assert ev.message == ""

    def test_cost_update_suppresses_message(self):
        """Cost events should have an empty message (suppressed from console)."""
        ev = classify_log_message("§cost role=author model=claude-sonnet cost=0.010 total=0.010")
        assert ev.message == ""
        assert ev.event_type == "cost"
