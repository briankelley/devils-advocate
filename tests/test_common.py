"""Tests for devils_advocate.orchestrator._common — shared pipeline helpers."""

from __future__ import annotations

import os
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx

from devils_advocate.orchestrator._common import (
    PipelineInputs,
    _apply_governance_or_escalate,
    _call_reviewer,
    _check_cost_guardrail,
    _promote_points_to_groups,
    _run_adversarial_pipeline,
    _run_round2_exchange,
)
from devils_advocate.types import (
    APIError,
    AuthorFinalResponse,
    AuthorResponse,
    CostTracker,
    GovernanceDecision,
    ModelConfig,
    RebuttalResponse,
    Resolution,
    ReviewContext,
    ReviewGroup,
    ReviewPoint,
)
from devils_advocate.storage import StorageManager

from conftest import (
    make_author_final,
    make_author_response,
    make_model_config,
    make_rebuttal,
    make_review_group,
    make_review_point,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_context() -> ReviewContext:
    """Return a ReviewContext with a fixed suffix for deterministic IDs."""
    return ReviewContext(
        project="test-project",
        review_id="test_review",
        review_start_time=datetime(2026, 2, 14, 18, 26, 0, tzinfo=timezone.utc),
        id_suffix="abcd",
    )


def _make_storage(tmp_path: Path) -> StorageManager:
    """Create a StorageManager rooted at tmp_path."""
    return StorageManager(
        project_dir=tmp_path,
        data_dir=tmp_path / "dvad-data",
    )


def _anthropic_json(text: str) -> dict:
    """Build an Anthropic Messages API response body."""
    return {
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": 100, "output_tokens": 50},
    }


def _openai_json(text: str) -> dict:
    """Build an OpenAI chat/completions response body."""
    return {
        "choices": [{"message": {"content": text}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }


def _make_anthropic_model(monkeypatch, name="author", context_window=200000) -> ModelConfig:
    """Return an Anthropic ModelConfig with fake credentials."""
    monkeypatch.setenv("TEST_KEY", "fake-key")
    return ModelConfig(
        name=name,
        provider="anthropic",
        model_id="claude-test",
        api_key_env="TEST_KEY",
        cost_per_1k_input=0.003,
        cost_per_1k_output=0.015,
        context_window=context_window,
    )


def _make_openai_model(
    monkeypatch,
    name="reviewer1",
    api_base="https://api.test.com/v1",
    context_window=128000,
) -> ModelConfig:
    """Return an OpenAI-compatible ModelConfig with fake credentials."""
    monkeypatch.setenv("TEST_KEY", "fake-key")
    return ModelConfig(
        name=name,
        provider="openai",
        model_id="gpt-test",
        api_key_env="TEST_KEY",
        api_base=api_base,
        cost_per_1k_input=0.005,
        cost_per_1k_output=0.015,
        context_window=context_window,
    )


# Structured review output that parse_review_response can extract.
REVIEWER_OUTPUT = """\
REVIEW POINT 1:
SEVERITY: high
CATEGORY: security
DESCRIPTION: SQL injection vulnerability in user input handling
RECOMMENDATION: Use parameterized queries
LOCATION: app.py line 42

REVIEW POINT 2:
SEVERITY: medium
CATEGORY: performance
DESCRIPTION: N+1 query pattern in the dashboard endpoint
RECOMMENDATION: Use eager loading or batch queries
LOCATION: views.py line 88
"""

AUTHOR_OUTPUT_TEMPLATE = """\
RESPONSE TO GROUP 1:
RESOLUTION: ACCEPTED
RATIONALE: The reviewer correctly identified that the user input is not sanitized before being passed to the SQL query. This is a legitimate security concern because unparameterized queries allow injection attacks via the login form handler.

RESPONSE TO GROUP 2:
RESOLUTION: ACCEPTED
RATIONALE: The N+1 query pattern is confirmed in the dashboard view. Using select_related or prefetch_related in Django ORM would resolve the performance issue because the current implementation makes separate DB calls for each row.
"""

REBUTTAL_OUTPUT = """\
REBUTTAL TO GROUP [{guid}]:
VERDICT: CHALLENGE
RATIONALE: The author's acceptance is insufficient because the fix should also cover the API endpoint.
"""

AUTHOR_FINAL_OUTPUT = """\
FINAL RESPONSE TO GROUP [{guid}]:
RESOLUTION: ACCEPTED
RATIONALE: After considering the reviewer's challenge, I agree the fix must also cover the API endpoint. The parameterized query approach should be applied consistently across both the login form and the REST API handlers.
"""


# ---------------------------------------------------------------------------
# _promote_points_to_groups
# ---------------------------------------------------------------------------


class TestPromotePointsToGroups:
    """Tests for _promote_points_to_groups — dedup fallback."""

    def test_each_point_becomes_own_group(self):
        """Each point should be promoted to its own group."""
        ctx = _make_context()
        points = [
            make_review_point(
                reviewer="r1",
                severity="high",
                category="security",
                description="SQL injection risk",
            ),
            make_review_point(
                reviewer="r2",
                severity="medium",
                category="performance",
                description="N+1 query",
            ),
        ]

        groups = _promote_points_to_groups(points, ctx)

        assert len(groups) == 2

        # First group
        assert groups[0].concern == "SQL injection risk"
        assert groups[0].combined_severity == "high"
        assert groups[0].combined_category == "security"
        assert groups[0].source_reviewers == ["r1"]
        assert len(groups[0].points) == 1
        assert groups[0].points[0] is points[0]

        # Second group
        assert groups[1].concern == "N+1 query"
        assert groups[1].combined_severity == "medium"
        assert groups[1].combined_category == "performance"
        assert groups[1].source_reviewers == ["r2"]

    def test_group_ids_are_assigned(self):
        """Groups should have proper group_id values from the context."""
        ctx = _make_context()
        points = [make_review_point(), make_review_point()]

        groups = _promote_points_to_groups(points, ctx)

        # Group IDs should be distinct
        assert groups[0].group_id != groups[1].group_id
        # Each should have a group_id
        assert groups[0].group_id
        assert groups[1].group_id

    def test_point_ids_are_assigned(self):
        """Points should receive assigned point_id values."""
        ctx = _make_context()
        points = [make_review_point(point_id="temp_001")]

        groups = _promote_points_to_groups(points, ctx)

        # point_id should be updated from the temporary value
        assert groups[0].points[0].point_id != "temp_001"

    def test_empty_points_returns_empty_list(self):
        """Empty input should return an empty list."""
        ctx = _make_context()
        groups = _promote_points_to_groups([], ctx)
        assert groups == []


# ---------------------------------------------------------------------------
# _call_reviewer
# ---------------------------------------------------------------------------


class TestCallReviewer:
    """Tests for _call_reviewer — async single reviewer call."""

    async def test_successful_call_with_point_parsing(self, monkeypatch, tmp_path):
        """A successful reviewer call should parse structured points."""
        reviewer = _make_openai_model(monkeypatch, name="test-reviewer")
        norm_model = _make_anthropic_model(monkeypatch, name="norm-model")
        storage = _make_storage(tmp_path)
        storage.set_review_id("test-review-id")
        cost_tracker = CostTracker()

        with respx.mock:
            respx.post("https://api.test.com/v1/chat/completions").mock(
                return_value=httpx.Response(200, json=_openai_json(REVIEWER_OUTPUT))
            )

            async with httpx.AsyncClient() as client:
                points = await _call_reviewer(
                    client=client,
                    reviewer=reviewer,
                    normalization_model=norm_model,
                    prompt="Review this plan.",
                    review_id="test-review-id",
                    cost_tracker=cost_tracker,
                    storage=storage,
                )

        assert len(points) == 2
        assert points[0].severity == "high"
        assert points[0].category == "security"
        assert "SQL injection" in points[0].description
        assert points[0].reviewer == "test-reviewer"
        assert points[1].severity == "medium"
        assert points[1].category == "performance"

        # Cost tracker should have recorded the call
        assert len(cost_tracker.entries) == 1
        assert cost_tracker.entries[0]["model"] == "test-reviewer"

    async def test_custom_system_prompt(self, monkeypatch, tmp_path):
        """A custom system_prompt should be passed instead of the default."""
        reviewer = _make_openai_model(monkeypatch, name="spec-reviewer")
        norm_model = _make_anthropic_model(monkeypatch, name="norm-model")
        storage = _make_storage(tmp_path)
        storage.set_review_id("test-review-id")
        cost_tracker = CostTracker()

        with respx.mock:
            route = respx.post("https://api.test.com/v1/chat/completions").mock(
                return_value=httpx.Response(200, json=_openai_json(REVIEWER_OUTPUT))
            )

            async with httpx.AsyncClient() as client:
                points = await _call_reviewer(
                    client=client,
                    reviewer=reviewer,
                    normalization_model=norm_model,
                    prompt="Review this spec.",
                    review_id="test-review-id",
                    cost_tracker=cost_tracker,
                    storage=storage,
                    system_prompt="You are a spec reviewer.",
                )

        assert len(points) == 2
        # Verify the custom system prompt was used (it is in the request body)
        request = route.calls[0].request
        import json
        body = json.loads(request.content)
        sys_msgs = [m for m in body["messages"] if m["role"] == "system"]
        assert len(sys_msgs) == 1
        assert sys_msgs[0]["content"] == "You are a spec reviewer."

    async def test_normalization_fallback_on_empty_parse(self, monkeypatch, tmp_path):
        """When parse_review_response returns empty, normalization is attempted."""
        reviewer = _make_openai_model(monkeypatch, name="bad-reviewer")
        norm_model = _make_anthropic_model(monkeypatch, name="norm-model")
        storage = _make_storage(tmp_path)
        storage.set_review_id("test-review-id")
        cost_tracker = CostTracker()

        # Reviewer returns unstructured text (no REVIEW POINT markers)
        unstructured_response = "I found some issues with the code. It looks problematic."

        with respx.mock:
            # Reviewer call - returns unstructured text
            respx.post("https://api.test.com/v1/chat/completions").mock(
                return_value=httpx.Response(200, json=_openai_json(unstructured_response))
            )
            # Normalization LLM call - returns structured text
            respx.post("https://api.anthropic.com/v1/messages").mock(
                return_value=httpx.Response(200, json=_anthropic_json(REVIEWER_OUTPUT))
            )

            async with httpx.AsyncClient() as client:
                points = await _call_reviewer(
                    client=client,
                    reviewer=reviewer,
                    normalization_model=norm_model,
                    prompt="Review this plan.",
                    review_id="test-review-id",
                    cost_tracker=cost_tracker,
                    storage=storage,
                )

        # Should get points from normalization fallback
        assert len(points) == 2
        # Both reviewer + normalization calls should be tracked
        assert len(cost_tracker.entries) == 2

    async def test_custom_point_parser(self, monkeypatch, tmp_path):
        """A custom point_parser function should be used instead of the default."""
        reviewer = _make_openai_model(monkeypatch, name="custom-reviewer")
        norm_model = _make_anthropic_model(monkeypatch, name="norm-model")
        storage = _make_storage(tmp_path)
        storage.set_review_id("test-review-id")
        cost_tracker = CostTracker()

        custom_point = make_review_point(
            reviewer="custom-reviewer",
            description="Custom parsed point",
        )

        def custom_parser(text, reviewer_name):
            return [custom_point]

        with respx.mock:
            respx.post("https://api.test.com/v1/chat/completions").mock(
                return_value=httpx.Response(200, json=_openai_json("Some text"))
            )

            async with httpx.AsyncClient() as client:
                points = await _call_reviewer(
                    client=client,
                    reviewer=reviewer,
                    normalization_model=norm_model,
                    prompt="Review this.",
                    review_id="test-review-id",
                    cost_tracker=cost_tracker,
                    storage=storage,
                    point_parser=custom_parser,
                )

        assert len(points) == 1
        assert points[0].description == "Custom parsed point"


# ---------------------------------------------------------------------------
# _run_round2_exchange
# ---------------------------------------------------------------------------


class TestRunRound2Exchange:
    """Tests for _run_round2_exchange — Round 2 reviewer rebuttals + author final."""

    async def test_all_accepted_skips_rebuttals(self, monkeypatch, tmp_path):
        """When the author accepted all groups, rebuttals should be skipped entirely."""
        author = _make_anthropic_model(monkeypatch, name="author")
        reviewer = _make_openai_model(monkeypatch, name="reviewer1")
        storage = _make_storage(tmp_path)
        storage.set_review_id("test-review-id")
        cost_tracker = CostTracker()

        groups = [
            make_review_group(group_id="grp_001", source_reviewers=["reviewer1"]),
            make_review_group(group_id="grp_002", source_reviewers=["reviewer1"]),
        ]
        author_responses = [
            make_author_response(group_id="grp_001", resolution="ACCEPTED"),
            make_author_response(group_id="grp_002", resolution="ACCEPTED"),
        ]

        # No HTTP mocks needed -- rebuttals are skipped
        async with httpx.AsyncClient() as client:
            rebuttals, finals, revised = await _run_round2_exchange(
                client=client,
                mode="plan",
                content="test content",
                groups=groups,
                author_responses=author_responses,
                grouped_text="test grouped text",
                author=author,
                reviewers=[reviewer],
                cost_tracker=cost_tracker,
                storage=storage,
                review_id="test-review-id",
            )

        assert rebuttals == []
        assert finals == []
        assert revised is None
        # No cost should be added for Round 2
        assert len(cost_tracker.entries) == 0

    async def test_per_reviewer_contested_group_filtering(self, monkeypatch, tmp_path):
        """Only reviewers with contested groups should receive rebuttal prompts."""
        author = _make_anthropic_model(monkeypatch, name="author")
        reviewer1 = _make_openai_model(monkeypatch, name="reviewer1", api_base="https://api.r1.com/v1")
        reviewer2 = _make_openai_model(monkeypatch, name="reviewer2", api_base="https://api.r2.com/v1")
        storage = _make_storage(tmp_path)
        storage.set_review_id("test-review-id")
        cost_tracker = CostTracker()

        # grp_001: reviewer1 is source, author REJECTED -> contested for reviewer1
        # grp_002: reviewer2 is source, author ACCEPTED -> NOT contested for reviewer2
        groups = [
            make_review_group(group_id="grp_001", source_reviewers=["reviewer1"], guid="guid-001"),
            make_review_group(group_id="grp_002", source_reviewers=["reviewer2"], guid="guid-002"),
        ]
        author_responses = [
            make_author_response(group_id="grp_001", resolution="REJECTED"),
            make_author_response(group_id="grp_002", resolution="ACCEPTED"),
        ]

        rebuttal_text = """\
REBUTTAL TO GROUP [guid-001]:
VERDICT: CHALLENGE
RATIONALE: The rejection is invalid because the vulnerability was demonstrated with a proof of concept exploit.
"""

        with respx.mock:
            # Only reviewer1 should get a rebuttal call
            r1_route = respx.post("https://api.r1.com/v1/chat/completions").mock(
                return_value=httpx.Response(200, json=_openai_json(rebuttal_text))
            )
            r2_route = respx.post("https://api.r2.com/v1/chat/completions").mock(
                return_value=httpx.Response(200, json=_openai_json(""))
            )

            # Author final response for the challenged group
            author_final_text = """\
FINAL RESPONSE TO GROUP [guid-001]:
RESOLUTION: ACCEPTED
RATIONALE: After reviewing the proof of concept the reviewer is correct that the vulnerability exists. The parameterized query fix should be applied to both the login and API endpoints to prevent SQL injection attacks.
"""
            respx.post("https://api.anthropic.com/v1/messages").mock(
                return_value=httpx.Response(200, json=_anthropic_json(author_final_text))
            )

            async with httpx.AsyncClient() as client:
                rebuttals, finals, revised = await _run_round2_exchange(
                    client=client,
                    mode="plan",
                    content="test content",
                    groups=groups,
                    author_responses=author_responses,
                    grouped_text="test",
                    author=author,
                    reviewers=[reviewer1, reviewer2],
                    cost_tracker=cost_tracker,
                    storage=storage,
                    review_id="test-review-id",
                )

        # reviewer1 should have been called, reviewer2 should NOT
        assert r1_route.called
        assert not r2_route.called

        # Should have at least one rebuttal from reviewer1
        assert len(rebuttals) >= 1
        assert rebuttals[0].reviewer == "reviewer1"

    async def test_context_window_exceeded_skips_reviewer(self, monkeypatch, tmp_path):
        """If rebuttal prompt exceeds a reviewer's context window, skip that reviewer."""
        author = _make_anthropic_model(monkeypatch, name="author")
        # Reviewer with tiny context window
        reviewer = _make_openai_model(monkeypatch, name="reviewer1", context_window=10)
        storage = _make_storage(tmp_path)
        storage.set_review_id("test-review-id")
        cost_tracker = CostTracker()

        groups = [
            make_review_group(group_id="grp_001", source_reviewers=["reviewer1"], guid="guid-001"),
        ]
        author_responses = [
            make_author_response(group_id="grp_001", resolution="REJECTED"),
        ]

        # No HTTP calls should be made since context is exceeded
        async with httpx.AsyncClient() as client:
            rebuttals, finals, revised = await _run_round2_exchange(
                client=client,
                mode="plan",
                content="test content with enough text to exceed the tiny context",
                groups=groups,
                author_responses=author_responses,
                grouped_text="test",
                author=author,
                reviewers=[reviewer],
                cost_tracker=cost_tracker,
                storage=storage,
                review_id="test-review-id",
            )

        # No rebuttals since the only reviewer was skipped
        assert rebuttals == []
        # No challenges means no author final
        assert finals == []

    async def test_author_final_context_window_exceeded(self, monkeypatch, tmp_path):
        """If author final prompt exceeds context window, fall through gracefully."""
        # Author with tiny context window
        author = _make_anthropic_model(monkeypatch, name="author", context_window=10)
        reviewer = _make_openai_model(monkeypatch, name="reviewer1", api_base="https://api.r1.com/v1")
        storage = _make_storage(tmp_path)
        storage.set_review_id("test-review-id")
        cost_tracker = CostTracker()

        groups = [
            make_review_group(group_id="grp_001", source_reviewers=["reviewer1"], guid="guid-001"),
        ]
        author_responses = [
            make_author_response(group_id="grp_001", resolution="REJECTED"),
        ]

        rebuttal_text = """\
REBUTTAL TO GROUP [guid-001]:
VERDICT: CHALLENGE
RATIONALE: The rejection is wrong because the code path is clearly vulnerable.
"""

        with respx.mock:
            respx.post("https://api.r1.com/v1/chat/completions").mock(
                return_value=httpx.Response(200, json=_openai_json(rebuttal_text))
            )
            # Anthropic should NOT be called (context exceeded for author final)
            anthropic_route = respx.post("https://api.anthropic.com/v1/messages").mock(
                return_value=httpx.Response(200, json=_anthropic_json("should not happen"))
            )

            async with httpx.AsyncClient() as client:
                rebuttals, finals, revised = await _run_round2_exchange(
                    client=client,
                    mode="plan",
                    content="test content",
                    groups=groups,
                    author_responses=author_responses,
                    grouped_text="test",
                    author=author,
                    reviewers=[reviewer],
                    cost_tracker=cost_tracker,
                    storage=storage,
                    review_id="test-review-id",
                )

        # Rebuttals should come through
        assert len(rebuttals) >= 1
        # Author final should be empty (context exceeded, fell through)
        assert finals == []
        # Anthropic should not have been called for author final
        assert not anthropic_route.called

    async def test_author_final_api_error_graceful_degradation(self, monkeypatch, tmp_path):
        """If author final call raises APIError, proceed with Round 1 positions."""
        author = _make_anthropic_model(monkeypatch, name="author")
        reviewer = _make_openai_model(monkeypatch, name="reviewer1", api_base="https://api.r1.com/v1")
        storage = _make_storage(tmp_path)
        storage.set_review_id("test-review-id")
        cost_tracker = CostTracker()

        groups = [
            make_review_group(group_id="grp_001", source_reviewers=["reviewer1"], guid="guid-001"),
        ]
        author_responses = [
            make_author_response(group_id="grp_001", resolution="REJECTED"),
        ]

        rebuttal_text = """\
REBUTTAL TO GROUP [guid-001]:
VERDICT: CHALLENGE
RATIONALE: I disagree because the code is clearly vulnerable to attacks.
"""

        with respx.mock:
            respx.post("https://api.r1.com/v1/chat/completions").mock(
                return_value=httpx.Response(200, json=_openai_json(rebuttal_text))
            )
            # Author final fails with 400 (non-retryable)
            respx.post("https://api.anthropic.com/v1/messages").mock(
                return_value=httpx.Response(400, text="Bad Request")
            )

            async with httpx.AsyncClient() as client:
                rebuttals, finals, revised = await _run_round2_exchange(
                    client=client,
                    mode="plan",
                    content="test content",
                    groups=groups,
                    author_responses=author_responses,
                    grouped_text="test",
                    author=author,
                    reviewers=[reviewer],
                    cost_tracker=cost_tracker,
                    storage=storage,
                    review_id="test-review-id",
                )

        # Rebuttals should come through
        assert len(rebuttals) >= 1
        # Author final should be empty (APIError caught)
        assert finals == []

    async def test_no_challenges_skip_author_final(self, monkeypatch, tmp_path):
        """If all reviewers CONCUR, author final response should be skipped."""
        author = _make_anthropic_model(monkeypatch, name="author")
        reviewer = _make_openai_model(monkeypatch, name="reviewer1", api_base="https://api.r1.com/v1")
        storage = _make_storage(tmp_path)
        storage.set_review_id("test-review-id")
        cost_tracker = CostTracker()

        groups = [
            make_review_group(group_id="grp_001", source_reviewers=["reviewer1"], guid="guid-001"),
        ]
        author_responses = [
            make_author_response(group_id="grp_001", resolution="REJECTED"),
        ]

        # Reviewer concurs with author's rejection
        rebuttal_text = """\
REBUTTAL TO GROUP [guid-001]:
VERDICT: CONCUR
RATIONALE: The author's rejection is well-reasoned, the finding was not critical.
"""

        with respx.mock:
            respx.post("https://api.r1.com/v1/chat/completions").mock(
                return_value=httpx.Response(200, json=_openai_json(rebuttal_text))
            )
            # Anthropic should not be called (no challenges)
            anthropic_route = respx.post("https://api.anthropic.com/v1/messages").mock(
                return_value=httpx.Response(200, json=_anthropic_json("should not happen"))
            )

            async with httpx.AsyncClient() as client:
                rebuttals, finals, revised = await _run_round2_exchange(
                    client=client,
                    mode="plan",
                    content="test content",
                    groups=groups,
                    author_responses=author_responses,
                    grouped_text="test",
                    author=author,
                    reviewers=[reviewer],
                    cost_tracker=cost_tracker,
                    storage=storage,
                    review_id="test-review-id",
                )

        # Should have concur rebuttal
        assert len(rebuttals) == 1
        assert rebuttals[0].verdict == "CONCUR"
        # Author final should be empty (no challenges)
        assert finals == []
        # Anthropic should NOT have been called for author final
        assert not anthropic_route.called


# ---------------------------------------------------------------------------
# _apply_governance_or_escalate
# ---------------------------------------------------------------------------


class TestApplyGovernanceOrEscalate:
    """Tests for _apply_governance_or_escalate — governance with catastrophic fallback."""

    def test_normal_governance_application(self, tmp_path):
        """Normal case: governance runs normally when parse coverage is adequate."""
        storage = _make_storage(tmp_path)
        storage.set_review_id("test-review-id")

        groups = [
            make_review_group(group_id="grp_001"),
            make_review_group(group_id="grp_002"),
        ]
        author_responses = [
            make_author_response(group_id="grp_001", resolution="ACCEPTED"),
            make_author_response(group_id="grp_002", resolution="ACCEPTED"),
        ]

        decisions = _apply_governance_or_escalate(
            groups=groups,
            author_responses=author_responses,
            all_rebuttals=[],
            author_final_responses=[],
            mode="plan",
            parsed_count=2,
            total_count=2,
            storage=storage,
        )

        assert len(decisions) == 2
        # Should be real governance decisions, not escalation fallback
        for d in decisions:
            assert d.group_id in ("grp_001", "grp_002")
            # Both accepted with substantive rationale should be auto_accepted
            assert d.governance_resolution == Resolution.AUTO_ACCEPTED.value

    def test_catastrophic_parse_failure_escalates_all(self, tmp_path):
        """When parse coverage < 25%, all groups should be escalated."""
        storage = _make_storage(tmp_path)
        storage.set_review_id("test-review-id")

        groups = [
            make_review_group(group_id="grp_001"),
            make_review_group(group_id="grp_002"),
            make_review_group(group_id="grp_003"),
            make_review_group(group_id="grp_004"),
        ]
        # Only 1 out of 4 parsed = 25% -> triggers < 25% check (need strictly less)
        # 0 out of 4 parsed = 0% -> definitely triggers
        author_responses = []

        decisions = _apply_governance_or_escalate(
            groups=groups,
            author_responses=author_responses,
            all_rebuttals=[],
            author_final_responses=[],
            mode="plan",
            parsed_count=0,
            total_count=4,
            storage=storage,
        )

        assert len(decisions) == 4
        for d in decisions:
            assert d.governance_resolution == Resolution.ESCALATED.value
            assert d.author_resolution == "parse_failure"
            assert "Catastrophic" in d.reason

    def test_boundary_at_25_percent_uses_normal_governance(self, tmp_path):
        """At exactly 25% coverage, normal governance should apply (not catastrophic)."""
        storage = _make_storage(tmp_path)
        storage.set_review_id("test-review-id")

        groups = [
            make_review_group(group_id="grp_001"),
            make_review_group(group_id="grp_002"),
            make_review_group(group_id="grp_003"),
            make_review_group(group_id="grp_004"),
        ]
        # 1 out of 4 = 25% exactly -> NOT < 25%, so normal governance
        author_responses = [
            make_author_response(group_id="grp_001", resolution="ACCEPTED"),
        ]

        decisions = _apply_governance_or_escalate(
            groups=groups,
            author_responses=author_responses,
            all_rebuttals=[],
            author_final_responses=[],
            mode="plan",
            parsed_count=1,
            total_count=4,
            storage=storage,
        )

        assert len(decisions) == 4
        # At least one should NOT be parse_failure escalation
        has_non_escalation = any(
            d.author_resolution != "parse_failure" for d in decisions
        )
        assert has_non_escalation

    def test_zero_total_count_uses_normal_governance(self, tmp_path):
        """When total_count is 0, should not divide by zero."""
        storage = _make_storage(tmp_path)
        storage.set_review_id("test-review-id")

        decisions = _apply_governance_or_escalate(
            groups=[],
            author_responses=[],
            all_rebuttals=[],
            author_final_responses=[],
            mode="plan",
            parsed_count=0,
            total_count=0,
            storage=storage,
        )

        assert decisions == []


# ---------------------------------------------------------------------------
# _check_cost_guardrail
# ---------------------------------------------------------------------------


class TestCheckCostGuardrail:
    """Tests for _check_cost_guardrail — cost limit checking."""

    def test_below_80_percent_returns_false(self, tmp_path):
        """Below 80% of budget, should return False (no abort)."""
        storage = _make_storage(tmp_path)
        storage.set_review_id("test-review-id")

        cost_tracker = CostTracker(max_cost=10.0)
        # Add some cost, but stay below 80%
        cost_tracker.add("model", 100, 50, 0.003, 0.015, role="reviewer")
        # total_usd should be small, well below 80% of 10.0

        result = _check_cost_guardrail(cost_tracker, storage)
        assert result is False

    def test_at_80_percent_emits_warning_returns_false(self, tmp_path):
        """At 80% of budget, should emit warning but NOT abort."""
        storage = _make_storage(tmp_path)
        storage.set_review_id("test-review-id")

        cost_tracker = CostTracker(max_cost=1.0)
        # Force the warned_80 flag to True (simulating what CostTracker.add does)
        cost_tracker.total_usd = 0.85
        cost_tracker.warned_80 = True

        result = _check_cost_guardrail(cost_tracker, storage)
        assert result is False
        # warned_80 should be reset to prevent repeated warnings
        assert cost_tracker.warned_80 is False

    def test_exceeded_returns_true(self, tmp_path):
        """When cost exceeds budget, should return True (abort)."""
        storage = _make_storage(tmp_path)
        storage.set_review_id("test-review-id")

        cost_tracker = CostTracker(max_cost=1.0)
        cost_tracker.total_usd = 1.05
        cost_tracker.exceeded = True

        result = _check_cost_guardrail(cost_tracker, storage)
        assert result is True

    def test_no_budget_returns_false(self, tmp_path):
        """Without a max_cost budget, should never trigger."""
        storage = _make_storage(tmp_path)
        storage.set_review_id("test-review-id")

        cost_tracker = CostTracker()
        cost_tracker.total_usd = 100.0  # Huge cost but no limit

        result = _check_cost_guardrail(cost_tracker, storage)
        assert result is False


# ---------------------------------------------------------------------------
# _run_adversarial_pipeline — end-to-end pipeline
# ---------------------------------------------------------------------------

# Shared constants for pipeline tests
DEDUP_OUTPUT = """\
GROUP 1:
CONCERN: SQL injection vulnerability in user input handling
POINTS: POINT 1
COMBINED_SEVERITY: high
COMBINED_CATEGORY: security

GROUP 2:
CONCERN: N+1 query pattern in the dashboard endpoint
POINTS: POINT 2
COMBINED_SEVERITY: medium
COMBINED_CATEGORY: performance
"""

REVISION_OUTPUT = """\
=== REVISED PLAN ===
Updated plan content with security fixes and performance improvements applied.
=== END REVISED PLAN ===
"""


class TestRunAdversarialPipeline:
    """Tests for _run_adversarial_pipeline — end-to-end pipeline."""

    def _make_pipeline_inputs(
        self,
        monkeypatch,
        tmp_path,
        groups,
        all_points,
        max_cost=10.0,
    ) -> PipelineInputs:
        """Build PipelineInputs for testing.

        all_points must be a list of ReviewPoint dataclass instances (not dicts),
        because _run_adversarial_pipeline calls asdict() on each element.
        """
        monkeypatch.setenv("TEST_KEY", "fake-key")
        monkeypatch.setenv("DVAD_HOME", str(tmp_path / "dvad-data"))

        author = ModelConfig(
            name="author",
            provider="anthropic",
            model_id="claude-test",
            api_key_env="TEST_KEY",
            cost_per_1k_input=0.003,
            cost_per_1k_output=0.015,
            context_window=200000,
        )

        reviewer = ModelConfig(
            name="reviewer1",
            provider="openai",
            model_id="gpt-test",
            api_key_env="TEST_KEY",
            api_base="https://api.test.com/v1",
            cost_per_1k_input=0.005,
            cost_per_1k_output=0.015,
            context_window=128000,
        )

        revision_model = ModelConfig(
            name="revision",
            provider="anthropic",
            model_id="claude-test",
            api_key_env="TEST_KEY",
            cost_per_1k_input=0.003,
            cost_per_1k_output=0.015,
            context_window=200000,
        )

        dedup_model = ModelConfig(
            name="dedup",
            provider="anthropic",
            model_id="haiku-test",
            api_key_env="TEST_KEY",
            cost_per_1k_input=0.001,
            cost_per_1k_output=0.004,
            context_window=200000,
        )

        cost_tracker = CostTracker(max_cost=max_cost)
        storage = StorageManager(
            project_dir=tmp_path,
            data_dir=tmp_path / "dvad-data",
        )
        storage.set_review_id("test_review")

        return PipelineInputs(
            mode="plan",
            content="# My Plan\n\nThis is a test plan.\n",
            input_file_label="plan.md",
            project="test-project",
            review_id="test_review",
            timestamp="2026-02-14T18:26:00Z",
            all_points=all_points,
            groups=groups,
            author=author,
            active_reviewers=[reviewer],
            dedup_model=dedup_model,
            revision_model=revision_model,
            cost_tracker=cost_tracker,
            storage=storage,
            revision_filename="revised-plan.md",
            reviewer_roles={"reviewer1": "reviewer"},
        )

    async def test_happy_path_full_pipeline(self, monkeypatch, tmp_path):
        """Full pipeline: author response, rebuttals, governance, revision."""
        ctx = _make_context()
        points = [
            make_review_point(
                reviewer="reviewer1",
                severity="high",
                category="security",
                description="SQL injection vulnerability in user input handling",
            ),
            make_review_point(
                reviewer="reviewer1",
                severity="medium",
                category="performance",
                description="N+1 query pattern in the dashboard endpoint",
            ),
        ]
        groups = _promote_points_to_groups(points, ctx)

        # Assign GUIDs for prompt correlation
        for i, g in enumerate(groups, 1):
            g.guid = f"guid-{i:03d}"

        inputs = self._make_pipeline_inputs(
            monkeypatch, tmp_path, groups, points,
        )

        with respx.mock:
            # Author round 1 + Author final (if challenged) + Revision = Anthropic calls
            respx.post("https://api.anthropic.com/v1/messages").mock(
                side_effect=[
                    # Author round 1 response
                    httpx.Response(200, json=_anthropic_json(AUTHOR_OUTPUT_TEMPLATE)),
                    # Revision
                    httpx.Response(200, json=_anthropic_json(REVISION_OUTPUT)),
                ]
            )
            # No reviewer rebuttal (all accepted -> skip round 2)

            async with httpx.AsyncClient() as client:
                result = await _run_adversarial_pipeline(client, inputs)

        assert result is not None
        assert result.mode == "plan"
        assert result.project == "test-project"
        assert result.review_id == "test_review"
        assert len(result.groups) == 2
        assert len(result.author_responses) == 2
        assert len(result.governance_decisions) == 2
        assert result.cost.total_usd > 0

        # Storage artifacts should exist
        review_dir = tmp_path / "dvad-data" / "reviews" / "test_review"
        assert (review_dir / "dvad-report.md").exists()
        assert (review_dir / "review-ledger.json").exists()
        assert (review_dir / "original_content.txt").exists()

    async def test_no_actionable_findings_skips_revision(self, monkeypatch, tmp_path):
        """When no findings are actionable, revision should be skipped."""
        ctx = _make_context()
        points = [
            make_review_point(
                reviewer="reviewer1",
                severity="low",
                category="other",
                description="Minor style issue",
            ),
        ]
        groups = _promote_points_to_groups(points, ctx)
        for i, g in enumerate(groups, 1):
            g.guid = f"guid-{i:03d}"

        inputs = self._make_pipeline_inputs(
            monkeypatch, tmp_path, groups, points,
        )

        # Author rejects the finding with a rationale that fails validation
        # Single reviewer + unchallenged rejection -> auto_dismissed (not actionable)
        author_response = """\
RESPONSE TO GROUP 1:
RESOLUTION: REJECTED
RATIONALE: This style concern is subjective and the current approach follows our team conventions.
"""

        with respx.mock:
            respx.post("https://api.anthropic.com/v1/messages").mock(
                side_effect=[
                    # Author round 1
                    httpx.Response(200, json=_anthropic_json(author_response)),
                ]
            )
            # Reviewer rebuttal: concur with rejection
            respx.post("https://api.test.com/v1/chat/completions").mock(
                return_value=httpx.Response(200, json=_openai_json("""\
REBUTTAL TO GROUP [guid-001]:
VERDICT: CONCUR
RATIONALE: The author is right that this is a subjective style choice.
"""))
            )

            async with httpx.AsyncClient() as client:
                result = await _run_adversarial_pipeline(client, inputs)

        assert result is not None
        # Revision should not have been called
        review_dir = tmp_path / "dvad-data" / "reviews" / "test_review"
        assert not (review_dir / "revised-plan.md").exists()

    async def test_revision_failure_non_fatal(self, monkeypatch, tmp_path):
        """If revision raises an exception, pipeline should still complete."""
        ctx = _make_context()
        points = [
            make_review_point(
                reviewer="reviewer1",
                severity="high",
                category="security",
                description="SQL injection vulnerability in user input handling",
            ),
        ]
        groups = _promote_points_to_groups(points, ctx)
        for i, g in enumerate(groups, 1):
            g.guid = f"guid-{i:03d}"

        inputs = self._make_pipeline_inputs(
            monkeypatch, tmp_path, groups, points,
        )

        with respx.mock:
            respx.post("https://api.anthropic.com/v1/messages").mock(
                side_effect=[
                    # Author round 1 response
                    httpx.Response(200, json=_anthropic_json(AUTHOR_OUTPUT_TEMPLATE)),
                    # Revision call fails
                    httpx.Response(500, text="Internal Server Error"),
                ]
            )

            async with httpx.AsyncClient() as client:
                result = await _run_adversarial_pipeline(client, inputs)

        # Pipeline should still return a result even if revision failed
        assert result is not None
        assert result.mode == "plan"
        assert len(result.governance_decisions) >= 1

    async def test_cost_guardrail_aborts_pipeline(self, monkeypatch, tmp_path):
        """If cost guardrail is exceeded after Round 1 author, pipeline returns None."""
        ctx = _make_context()
        points = [
            make_review_point(
                reviewer="reviewer1",
                severity="high",
                category="security",
                description="SQL injection vulnerability",
            ),
        ]
        groups = _promote_points_to_groups(points, ctx)
        for i, g in enumerate(groups, 1):
            g.guid = f"guid-{i:03d}"

        inputs = self._make_pipeline_inputs(
            monkeypatch, tmp_path, groups, points, max_cost=0.0001,
        )

        with respx.mock:
            respx.post("https://api.anthropic.com/v1/messages").mock(
                return_value=httpx.Response(200, json=_anthropic_json(AUTHOR_OUTPUT_TEMPLATE))
            )

            async with httpx.AsyncClient() as client:
                result = await _run_adversarial_pipeline(client, inputs)

        # Should abort due to cost guardrail
        assert result is None
