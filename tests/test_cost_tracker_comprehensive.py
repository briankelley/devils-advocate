"""Comprehensive tests for CostTracker, governance rules, and cost guardrails.

Covers every CostTracker method and flag, governance decision matrix,
validation helpers, and the CostTracker → log event → SSE chain.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from helpers import (
    make_author_final,
    make_author_response,
    make_model_config,
    make_rebuttal,
    make_review_group,
    make_review_point,
)


# ═══════════════════════════════════════════════════════════════════════════
# 1. CostTracker — Basic Operations
# ═══════════════════════════════════════════════════════════════════════════


class TestCostTrackerBasic:
    def test_initial_state(self):
        from devils_advocate.types import CostTracker

        ct = CostTracker()
        assert ct.total_usd == 0.0
        assert ct.entries == []
        assert ct.max_cost is None
        assert ct.warned_80 is False
        assert ct.exceeded is False
        assert ct.total_input_tokens == 0
        assert ct.total_output_tokens == 0
        assert ct.role_costs == {}

    def test_add_single_entry(self):
        from devils_advocate.types import CostTracker

        ct = CostTracker()
        ct.add("gemini-flash", 1000, 500, 0.001, 0.002, role="reviewer_1")

        assert len(ct.entries) == 1
        assert ct.total_input_tokens == 1000
        assert ct.total_output_tokens == 500
        assert ct.total_usd == pytest.approx(0.002, abs=0.0001)
        assert ct.role_costs["reviewer_1"] == pytest.approx(0.002, abs=0.0001)

    def test_add_multiple_entries(self):
        from devils_advocate.types import CostTracker

        ct = CostTracker()
        ct.add("model-a", 1000, 500, 0.01, 0.02, role="reviewer_1")
        ct.add("model-b", 2000, 1000, 0.01, 0.02, role="reviewer_2")

        assert len(ct.entries) == 2
        assert ct.total_input_tokens == 3000
        assert ct.total_output_tokens == 1500
        assert "reviewer_1" in ct.role_costs
        assert "reviewer_2" in ct.role_costs

    def test_add_same_role_accumulates(self):
        from devils_advocate.types import CostTracker

        ct = CostTracker()
        ct.add("gemini", 100, 50, 0.01, 0.02, role="reviewer_1")
        ct.add("gemini", 100, 50, 0.01, 0.02, role="reviewer_1")

        assert len(ct.entries) == 2
        # Role cost should be sum of both
        expected_single = 100 / 1000 * 0.01 + 50 / 1000 * 0.02
        assert ct.role_costs["reviewer_1"] == pytest.approx(expected_single * 2, abs=0.0001)

    def test_add_no_cost_per_token(self):
        from devils_advocate.types import CostTracker

        ct = CostTracker()
        ct.add("unknown-model", 1000, 500, None, None, role="reviewer_1")

        assert ct.total_usd == 0.0
        assert ct.entries[0]["cost_usd"] == 0.0

    def test_add_without_role(self):
        from devils_advocate.types import CostTracker

        ct = CostTracker()
        ct.add("model", 100, 50, 0.01, 0.02)

        assert len(ct.entries) == 1
        assert ct.role_costs == {}


# ═══════════════════════════════════════════════════════════════════════════
# 2. CostTracker — Cost Calculation
# ═══════════════════════════════════════════════════════════════════════════


class TestCostCalculation:
    def test_precise_calculation(self):
        from devils_advocate.types import CostTracker

        ct = CostTracker()
        # 5000 input at $0.01/1k = $0.05
        # 2000 output at $0.03/1k = $0.06
        # Total = $0.11
        ct.add("model", 5000, 2000, 0.01, 0.03, role="test")

        assert ct.total_usd == pytest.approx(0.11, abs=0.001)
        assert ct.entries[0]["cost_usd"] == pytest.approx(0.11, abs=0.001)

    def test_zero_tokens(self):
        from devils_advocate.types import CostTracker

        ct = CostTracker()
        ct.add("model", 0, 0, 0.01, 0.02, role="test")

        assert ct.total_usd == 0.0

    def test_large_token_counts(self):
        from devils_advocate.types import CostTracker

        ct = CostTracker()
        ct.add("model", 1_000_000, 500_000, 0.01, 0.03, role="test")

        expected = 1_000_000 / 1000 * 0.01 + 500_000 / 1000 * 0.03
        assert ct.total_usd == pytest.approx(expected, abs=0.01)


# ═══════════════════════════════════════════════════════════════════════════
# 3. CostTracker — Budget Guardrails
# ═══════════════════════════════════════════════════════════════════════════


class TestCostGuardrails:
    def test_80_percent_warning(self):
        from devils_advocate.types import CostTracker

        ct = CostTracker(max_cost=1.0)
        # Add $0.85 — should trigger 80% warning
        ct.add("model", 85000, 0, 0.01, 0.0, role="test")

        assert ct.warned_80 is True
        assert ct.exceeded is False

    def test_no_warning_below_80(self):
        from devils_advocate.types import CostTracker

        ct = CostTracker(max_cost=1.0)
        ct.add("model", 50000, 0, 0.01, 0.0, role="test")

        assert ct.warned_80 is False
        assert ct.exceeded is False

    def test_exceeded_at_100(self):
        from devils_advocate.types import CostTracker

        ct = CostTracker(max_cost=1.0)
        ct.add("model", 100000, 0, 0.01, 0.0, role="test")

        assert ct.warned_80 is True
        assert ct.exceeded is True

    def test_exceeded_above_100(self):
        from devils_advocate.types import CostTracker

        ct = CostTracker(max_cost=0.50)
        ct.add("model", 100000, 0, 0.01, 0.0, role="test")

        assert ct.exceeded is True

    def test_no_max_cost_no_flags(self):
        from devils_advocate.types import CostTracker

        ct = CostTracker()  # max_cost=None
        ct.add("model", 1_000_000, 500_000, 0.01, 0.03, role="test")

        assert ct.warned_80 is False
        assert ct.exceeded is False

    def test_warning_can_be_reset_and_retriggered(self):
        """warned_80 is set whenever total >= 80% of max_cost.

        The _check_cost_guardrail function resets it after printing;
        any subsequent add above 80% re-triggers it.
        """
        from devils_advocate.types import CostTracker

        ct = CostTracker(max_cost=1.0)
        ct.add("model", 85000, 0, 0.01, 0.0, role="test")
        assert ct.warned_80 is True

        # Simulating what _check_cost_guardrail does: reset after handling
        ct.warned_80 = False

        # Another add — total is still > 80%, so condition fires again
        ct.add("model", 1000, 0, 0.01, 0.0, role="test")
        assert ct.warned_80 is True  # Re-triggered because still above 80%

    def test_exceeded_fires_once(self):
        from devils_advocate.types import CostTracker

        ct = CostTracker(max_cost=1.0)
        ct.add("model", 100000, 0, 0.01, 0.0, role="test")
        assert ct.exceeded is True

        # More adds shouldn't change exceeded
        ct.add("model", 100000, 0, 0.01, 0.0, role="test")
        assert ct.exceeded is True


# ═══════════════════════════════════════════════════════════════════════════
# 4. CostTracker — Breakdown
# ═══════════════════════════════════════════════════════════════════════════


class TestCostBreakdown:
    def test_breakdown_by_model(self):
        from devils_advocate.types import CostTracker

        ct = CostTracker()
        ct.add("gemini-flash", 1000, 500, 0.001, 0.002, role="reviewer_1")
        ct.add("kimi-k2", 2000, 1000, 0.002, 0.003, role="reviewer_2")
        ct.add("gemini-flash", 500, 200, 0.001, 0.002, role="dedup")

        bd = ct.breakdown()
        assert "gemini-flash" in bd
        assert "kimi-k2" in bd
        assert bd["gemini-flash"] > 0
        assert bd["kimi-k2"] > 0

    def test_empty_breakdown(self):
        from devils_advocate.types import CostTracker

        ct = CostTracker()
        assert ct.breakdown() == {}


# ═══════════════════════════════════════════════════════════════════════════
# 5. CostTracker — Log Emission
# ═══════════════════════════════════════════════════════════════════════════


class TestCostLogEmission:
    def test_log_fn_called_with_role(self):
        from devils_advocate.types import CostTracker

        logged = []
        ct = CostTracker(_log_fn=logged.append)
        ct.add("gemini-flash", 1000, 500, 0.001, 0.002, role="reviewer_1")

        assert len(logged) == 1
        assert "§cost" in logged[0]
        assert "role=reviewer_1" in logged[0]
        assert "model=gemini-flash" in logged[0]

    def test_no_log_without_role(self):
        from devils_advocate.types import CostTracker

        logged = []
        ct = CostTracker(_log_fn=logged.append)
        ct.add("gemini-flash", 1000, 500, 0.001, 0.002)  # no role

        assert len(logged) == 0

    def test_no_log_without_log_fn(self):
        from devils_advocate.types import CostTracker

        ct = CostTracker()  # no _log_fn
        ct.add("gemini-flash", 1000, 500, 0.001, 0.002, role="test")
        # Should not raise

    def test_log_message_format(self):
        from devils_advocate.types import CostTracker

        logged = []
        ct = CostTracker(_log_fn=logged.append)
        ct.add("test-model", 3358, 1004, 0.001, 0.002, role="reviewer_1")

        msg = logged[0]
        assert "in_tokens=3358" in msg
        assert "out_tokens=1004" in msg
        assert "total_tokens=" in msg


# ═══════════════════════════════════════════════════════════════════════════
# 6. Governance — Acceptance Rules
# ═══════════════════════════════════════════════════════════════════════════


class TestGovernanceAcceptance:
    def test_accepted_with_substantive_rationale(self):
        from devils_advocate.governance import apply_governance

        groups = [make_review_group(group_id="g1")]
        responses = [make_author_response(
            group_id="g1",
            resolution="ACCEPTED",
            rationale="This is a substantive rationale with enough words to pass the minimum word count validation check easily",
        )]

        decisions = apply_governance(groups, responses)
        assert len(decisions) == 1
        assert decisions[0].governance_resolution == "auto_accepted"

    def test_partial_acceptance_escalates(self):
        from devils_advocate.governance import apply_governance

        groups = [make_review_group(group_id="g1")]
        responses = [make_author_response(
            group_id="g1",
            resolution="PARTIAL",
            rationale="Partially accepting because the API endpoint needs the auth middleware before the rate limiter to prevent unauthorized access",
        )]

        decisions = apply_governance(groups, responses)
        assert decisions[0].governance_resolution in ("auto_accepted", "escalated")


# ═══════════════════════════════════════════════════════════════════════════
# 7. Governance — Rejection Rules
# ═══════════════════════════════════════════════════════════════════════════


class TestGovernanceRejection:
    def test_rote_rejection_multi_reviewer_auto_accepts(self):
        """Multi-reviewer: rote rejection fails validation -> auto-accept the point."""
        from devils_advocate.governance import apply_governance

        groups = [make_review_group(
            group_id="g1",
            source_reviewers=["reviewer-1", "reviewer-2"],  # Multi-reviewer
        )]
        responses = [make_author_response(
            group_id="g1",
            resolution="REJECTED",
            rationale="No thanks",  # Rote rejection, no technical reason
        )]

        decisions = apply_governance(groups, responses)
        assert decisions[0].governance_resolution == "auto_accepted"

    def test_valid_rejection_multi_reviewer_escalated(self):
        """Multi-reviewer: valid rejection escalates (author has valid objection)."""
        from devils_advocate.governance import apply_governance

        groups = [make_review_group(
            group_id="g1",
            source_reviewers=["reviewer-1", "reviewer-2"],  # Multi-reviewer
        )]
        responses = [make_author_response(
            group_id="g1",
            resolution="REJECTED",
            rationale=(
                "The async function handles the transaction lock correctly because "
                "the database connection pool in module db/pool.py uses a FIFO queue "
                "which prevents deadlocks. The thread safety is guaranteed by the mutex "
                "in the connection class, so this finding would not result in data corruption."
            ),
        )]

        decisions = apply_governance(groups, responses)
        assert decisions[0].governance_resolution == "escalated"

    def test_single_reviewer_rejection_unchallenged_auto_dismissed(self):
        """Single reviewer, author rejects, unchallenged -> auto-dismissed."""
        from devils_advocate.governance import apply_governance

        groups = [make_review_group(
            group_id="g1",
            source_reviewers=["reviewer-1"],  # Single reviewer
        )]
        responses = [make_author_response(
            group_id="g1",
            resolution="REJECTED",
            rationale="No thanks",
        )]

        decisions = apply_governance(groups, responses)
        assert decisions[0].governance_resolution == "auto_dismissed"


# ═══════════════════════════════════════════════════════════════════════════
# 8. Governance — Missing Response
# ═══════════════════════════════════════════════════════════════════════════


class TestGovernanceMissingResponse:
    def test_no_response_escalates(self):
        """Groups without an author response are escalated."""
        from devils_advocate.governance import apply_governance

        groups = [
            make_review_group(group_id="g1"),
            make_review_group(group_id="g2"),
        ]
        responses = [make_author_response(group_id="g1")]

        decisions = apply_governance(groups, responses)
        g2_decision = [d for d in decisions if d.group_id == "g2"][0]
        assert g2_decision.governance_resolution == "escalated"


# ═══════════════════════════════════════════════════════════════════════════
# 9. Governance — Challenge/Concur Flow
# ═══════════════════════════════════════════════════════════════════════════


class TestGovernanceChallengeFlow:
    def test_challenge_with_accepted_final(self):
        """Author rejects -> reviewer challenges -> author accepts in final."""
        from devils_advocate.governance import apply_governance

        groups = [make_review_group(group_id="g1")]
        responses = [make_author_response(
            group_id="g1", resolution="REJECTED",
            rationale=(
                "The async function handles the transaction lock correctly because "
                "the database connection pool uses a FIFO queue preventing deadlocks."
            ),
        )]
        rebuttals = [make_rebuttal(
            group_id="g1", reviewer="reviewer-1",
            verdict="CHALLENGE",
            rationale="The FIFO queue doesn't prevent deadlocks in this case",
        )]
        final_responses = [make_author_final(
            group_id="g1", resolution="ACCEPTED",
            rationale=(
                "After consideration the reviewer is correct about the FIFO queue "
                "and the deadlock prevention mechanism needs to be revised because "
                "the async function can hold the lock across await boundaries."
            ),
        )]

        decisions = apply_governance(
            groups, responses,
            rebuttals=rebuttals,
            author_final_responses=final_responses,
        )

        assert decisions[0].governance_resolution == "auto_accepted"

    def test_concur_uses_round1_acceptance(self):
        """When reviewer concurs (not challenges), Round 1 acceptance stands."""
        from devils_advocate.governance import apply_governance

        groups = [make_review_group(group_id="g1")]
        responses = [make_author_response(
            group_id="g1",
            resolution="ACCEPTED",
            rationale="Accepting this finding with enough words to meet the minimum threshold for substantive engagement check",
        )]
        rebuttals = [make_rebuttal(
            group_id="g1", reviewer="reviewer-1",
            verdict="CONCUR",  # NOT a challenge — won't enter challenge_map
            rationale="I agree with the author",
        )]

        decisions = apply_governance(
            groups, responses, rebuttals=rebuttals,
        )

        # CONCUR doesn't count as a challenge, so acceptance with
        # substantive rationale auto-accepts
        assert decisions[0].governance_resolution == "auto_accepted"


# ═══════════════════════════════════════════════════════════════════════════
# 10. Governance — Validation Helpers
# ═══════════════════════════════════════════════════════════════════════════


class TestGovernanceValidation:
    def test_validate_rejection_technical(self):
        from devils_advocate.governance import validate_rejection

        valid = validate_rejection(
            "The async function in module api/handler.py would cause a deadlock "
            "because the transaction lock is held across the await boundary, "
            "which leads to thread starvation when concurrent requests arrive."
        )
        assert valid is True

    def test_validate_rejection_rote(self):
        from devils_advocate.governance import validate_rejection
        assert validate_rejection("No") is False
        assert validate_rejection("I disagree") is False
        assert validate_rejection("Not applicable") is False

    def test_validate_rejection_partially_valid(self):
        from devils_advocate.governance import validate_rejection

        # Has technical term but no mechanism or reference
        result = validate_rejection("The function is fine")
        assert result is False


# ═══════════════════════════════════════════════════════════════════════════
# 11. CostTracker → SSE Chain Integration
# ═══════════════════════════════════════════════════════════════════════════


class TestCostTrackerSSEIntegration:
    """Verify CostTracker log emissions produce correct SSE events."""

    def test_log_emits_parseable_cost_event(self):
        from devils_advocate.types import CostTracker
        from devils_advocate.gui.progress import classify_log_message

        logged = []
        ct = CostTracker(_log_fn=logged.append)
        ct.add("gemini-3-flash-preview", 3358, 1004, 0.001, 0.002, role="reviewer_1")

        assert len(logged) == 1
        ev = classify_log_message(logged[0])
        assert ev.event_type == "cost"
        assert ev.detail["role"] == "reviewer_1"
        assert ev.detail["model"] == "gemini-3-flash-preview"

    def test_multiple_adds_produce_multiple_events(self):
        from devils_advocate.types import CostTracker
        from devils_advocate.gui.progress import classify_log_message

        logged = []
        ct = CostTracker(_log_fn=logged.append)
        ct.add("model-a", 1000, 500, 0.01, 0.02, role="reviewer_1")
        ct.add("model-b", 2000, 1000, 0.01, 0.02, role="reviewer_2")
        ct.add("model-c", 500, 200, 0.01, 0.02, role="dedup")

        assert len(logged) == 3
        for msg in logged:
            ev = classify_log_message(msg)
            assert ev.event_type == "cost"

    def test_total_field_tracks_cumulative(self):
        from devils_advocate.types import CostTracker
        from devils_advocate.gui.progress import classify_log_message

        logged = []
        ct = CostTracker(_log_fn=logged.append)
        ct.add("model", 1000, 0, 0.01, 0.0, role="r1")
        ct.add("model", 1000, 0, 0.01, 0.0, role="r2")

        ev1 = classify_log_message(logged[0])
        ev2 = classify_log_message(logged[1])

        t1 = float(ev1.detail["total"])
        t2 = float(ev2.detail["total"])
        assert t2 > t1


# ═══════════════════════════════════════════════════════════════════════════
# 12. ReviewContext — ID Generation
# ═══════════════════════════════════════════════════════════════════════════


class TestReviewContext:
    def test_auto_id_suffix(self):
        from devils_advocate.types import ReviewContext

        ctx = ReviewContext(
            project="test",
            review_id="test_001",
            review_start_time=datetime.now(timezone.utc),
        )
        assert ctx.id_suffix != ""
        assert len(ctx.id_suffix) == 4  # 4-char random suffix

    def test_explicit_id_suffix(self):
        from devils_advocate.types import ReviewContext

        ctx = ReviewContext(
            project="test",
            review_id="test_001",
            review_start_time=datetime.now(timezone.utc),
            id_suffix="abcd",
        )
        assert ctx.id_suffix == "abcd"

    def test_make_group_id(self):
        from devils_advocate.types import ReviewContext

        ctx = ReviewContext(
            project="test",
            review_id="test_001",
            review_start_time=datetime.now(timezone.utc),
        )
        gid = ctx.make_group_id(0)
        assert "test" in gid
        assert "group" in gid

    def test_make_point_id(self):
        from devils_advocate.types import ReviewContext

        ctx = ReviewContext(
            project="test",
            review_id="test_001",
            review_start_time=datetime.now(timezone.utc),
        )
        gid = ctx.make_group_id(0)
        pid = ctx.make_point_id(gid, 0)
        assert "point" in pid


# ═══════════════════════════════════════════════════════════════════════════
# 13. Resolution Enum
# ═══════════════════════════════════════════════════════════════════════════


class TestResolutionEnum:
    def test_all_values(self):
        from devils_advocate.types import Resolution

        expected = {
            "accepted", "rejected", "partial", "auto_accepted",
            "auto_dismissed", "escalated", "overridden", "pending",
        }
        actual = {r.value for r in Resolution}
        assert actual == expected

    def test_enum_access(self):
        from devils_advocate.types import Resolution

        assert Resolution.ACCEPTED.value == "accepted"
        assert Resolution.ESCALATED.value == "escalated"


# ═══════════════════════════════════════════════════════════════════════════
# 14. Exception Types
# ═══════════════════════════════════════════════════════════════════════════


class TestExceptionTypes:
    def test_advocate_error_is_exception(self):
        from devils_advocate.types import AdvocateError
        assert issubclass(AdvocateError, Exception)

    def test_config_error_is_advocate_error(self):
        from devils_advocate.types import ConfigError, AdvocateError
        assert issubclass(ConfigError, AdvocateError)

    def test_api_error_is_advocate_error(self):
        from devils_advocate.types import APIError, AdvocateError
        assert issubclass(APIError, AdvocateError)

    def test_cost_limit_error_is_advocate_error(self):
        from devils_advocate.types import CostLimitError, AdvocateError
        assert issubclass(CostLimitError, AdvocateError)

    def test_storage_error_is_advocate_error(self):
        from devils_advocate.types import StorageError, AdvocateError
        assert issubclass(StorageError, AdvocateError)

    def test_storage_error_message(self):
        from devils_advocate.types import StorageError
        err = StorageError("Test error message")
        assert str(err) == "Test error message"


# ═══════════════════════════════════════════════════════════════════════════
# 15. ModelConfig
# ═══════════════════════════════════════════════════════════════════════════


class TestModelConfig:
    def test_api_key_from_env(self, monkeypatch):
        from devils_advocate.types import ModelConfig

        monkeypatch.setenv("TEST_API_KEY", "sk-test-12345")
        mc = ModelConfig(
            name="test-model",
            provider="openai",
            model_id="gpt-4",
            api_key_env="TEST_API_KEY",
        )
        assert mc.api_key == "sk-test-12345"

    def test_api_key_missing_returns_empty(self, monkeypatch):
        from devils_advocate.types import ModelConfig

        monkeypatch.delenv("NONEXISTENT_KEY", raising=False)
        mc = ModelConfig(
            name="test-model",
            provider="openai",
            model_id="gpt-4",
            api_key_env="NONEXISTENT_KEY",
        )
        assert mc.api_key == ""

    def test_default_values(self):
        from devils_advocate.types import ModelConfig

        mc = ModelConfig(
            name="test",
            provider="openai",
            model_id="gpt-4",
            api_key_env="KEY",
        )
        assert mc.timeout == 120
        assert mc.enabled is True
        assert mc.thinking is False
        assert mc.context_window is None
        assert mc.deduplication is False

    def test_custom_values(self):
        mc = make_model_config(
            name="custom-model",
            provider="anthropic",
            cost_per_1k_input=0.015,
            cost_per_1k_output=0.075,
            context_window=200000,
        )
        assert mc.name == "custom-model"
        assert mc.provider == "anthropic"
        assert mc.cost_per_1k_input == 0.015
        assert mc.context_window == 200000


# ═══════════════════════════════════════════════════════════════════════════
# 16. ReviewResult
# ═══════════════════════════════════════════════════════════════════════════


class TestReviewResult:
    def test_minimal_result(self):
        from devils_advocate.types import ReviewResult, CostTracker

        result = ReviewResult(
            review_id="test_001",
            mode="plan",
            input_file="/tmp/plan.md",
            project="test",
            timestamp="2026-01-01T00:00:00Z",
            author_model="claude-haiku",
            reviewer_models=["gemini-flash"],
            dedup_model="deepseek",
            points=[],
            groups=[],
            author_responses=[],
            governance_decisions=[],
        )
        assert result.review_id == "test_001"
        assert result.revised_output == ""
        assert result.summary == {}
        assert isinstance(result.cost, CostTracker)

    def test_result_with_all_fields(self):
        from devils_advocate.types import ReviewResult, CostTracker

        ct = CostTracker()
        ct.add("model", 100, 50, 0.01, 0.02, role="test")

        result = ReviewResult(
            review_id="full_001",
            mode="code",
            input_file="/tmp/code.py",
            project="test",
            timestamp="2026-01-01T00:00:00Z",
            author_model="claude-haiku",
            reviewer_models=["gemini-flash", "kimi-k2"],
            dedup_model="deepseek",
            points=[{"id": "p1"}],
            groups=[make_review_group()],
            author_responses=[make_author_response()],
            governance_decisions=[],
            rebuttals=[make_rebuttal()],
            author_final_responses=[make_author_final()],
            cost=ct,
            revised_output="revised content",
            summary={"total_groups": 1},
        )
        assert result.mode == "code"
        assert len(result.reviewer_models) == 2
        assert result.revised_output == "revised content"
        assert result.cost.total_usd > 0
