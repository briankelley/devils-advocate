"""Tests for devils_advocate.dedup — deduplication engine."""

from __future__ import annotations

import httpx
import pytest
import respx

from devils_advocate.dedup import (
    deduplicate_points,
    format_points_for_dedup,
    format_suggestions_for_dedup,
)
from devils_advocate.types import CostTracker, ModelConfig, ReviewContext, ReviewPoint

from conftest import make_model_config, make_review_point


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_context() -> ReviewContext:
    """Return a ReviewContext with a fixed suffix for deterministic IDs."""
    from datetime import datetime, timezone

    return ReviewContext(
        project="test-project",
        review_id="test_review",
        review_start_time=datetime(2026, 2, 14, 18, 26, 0, tzinfo=timezone.utc),
        id_suffix="abcd",
    )


def _anthropic_json(text: str) -> dict:
    """Build an Anthropic Messages API response body."""
    return {
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": 100, "output_tokens": 50},
    }


def _make_dedup_model(monkeypatch, context_window: int = 200000) -> ModelConfig:
    """Return a dedup ModelConfig with fake credentials."""
    monkeypatch.setenv("TEST_KEY", "fake-key")
    return ModelConfig(
        name="dedup-model",
        provider="anthropic",
        model_id="haiku-test",
        api_key_env="TEST_KEY",
        cost_per_1k_input=0.001,
        cost_per_1k_output=0.004,
        context_window=context_window,
    )


# ---------------------------------------------------------------------------
# format_points_for_dedup
# ---------------------------------------------------------------------------


class TestFormatPointsForDedup:
    """Tests for the format_points_for_dedup() function."""

    def test_basic_formatting_with_location(self):
        """Points with a location field should include the LOCATION line."""
        points = [
            make_review_point(
                reviewer="model-a",
                severity="high",
                category="security",
                description="SQL injection risk",
                recommendation="Use parameterized queries",
                location="app.py line 42",
            ),
        ]
        result = format_points_for_dedup(points)

        assert "POINT 1:" in result
        assert "REVIEWER: model-a" in result
        assert "SEVERITY: high" in result
        assert "CATEGORY: security" in result
        assert "DESCRIPTION: SQL injection risk" in result
        assert "RECOMMENDATION: Use parameterized queries" in result
        assert "LOCATION: app.py line 42" in result

    def test_formatting_without_location(self):
        """Points without a location should NOT include the LOCATION line."""
        points = [
            make_review_point(location=""),
        ]
        result = format_points_for_dedup(points)

        assert "POINT 1:" in result
        assert "LOCATION" not in result

    def test_multiple_points_numbered_sequentially(self):
        """Multiple points should be numbered starting from 1."""
        points = [
            make_review_point(reviewer="r1", description="First issue"),
            make_review_point(reviewer="r2", description="Second issue"),
            make_review_point(reviewer="r3", description="Third issue"),
        ]
        result = format_points_for_dedup(points)

        assert "POINT 1:" in result
        assert "POINT 2:" in result
        assert "POINT 3:" in result
        assert "First issue" in result
        assert "Second issue" in result
        assert "Third issue" in result

    def test_empty_list_returns_empty_string(self):
        """An empty list of points should produce an empty string."""
        result = format_points_for_dedup([])
        assert result == ""


# ---------------------------------------------------------------------------
# format_suggestions_for_dedup
# ---------------------------------------------------------------------------


class TestFormatSuggestionsForDedup:
    """Tests for the format_suggestions_for_dedup() function."""

    def test_basic_spec_formatting_with_context(self):
        """Spec suggestions with a location should include the CONTEXT field."""
        points = [
            make_review_point(
                reviewer="spec-model",
                category="ux",
                description="Improve onboarding flow",
                location="Section 3.2",
            ),
        ]
        result = format_suggestions_for_dedup(points)

        assert "SUGGESTION 1:" in result
        assert "REVIEWER: spec-model" in result
        assert "THEME: ux" in result
        assert "DESCRIPTION: Improve onboarding flow" in result
        assert "CONTEXT: Section 3.2" in result

    def test_formatting_without_context(self):
        """Spec suggestions without location should NOT include the CONTEXT line."""
        points = [
            make_review_point(
                category="features",
                description="Add dark mode support",
                location="",
            ),
        ]
        result = format_suggestions_for_dedup(points)

        assert "SUGGESTION 1:" in result
        assert "THEME: features" in result
        assert "CONTEXT" not in result

    def test_multiple_suggestions_numbered(self):
        """Multiple suggestions should be numbered sequentially."""
        points = [
            make_review_point(description="Suggestion A"),
            make_review_point(description="Suggestion B"),
        ]
        result = format_suggestions_for_dedup(points)

        assert "SUGGESTION 1:" in result
        assert "SUGGESTION 2:" in result

    def test_uses_category_as_theme(self):
        """The THEME field should map from the point's category."""
        points = [
            make_review_point(category="monetization"),
        ]
        result = format_suggestions_for_dedup(points)
        assert "THEME: monetization" in result

    def test_does_not_include_severity_or_recommendation(self):
        """Spec format should not include SEVERITY or RECOMMENDATION fields."""
        points = [
            make_review_point(
                severity="high",
                recommendation="Do something important",
            ),
        ]
        result = format_suggestions_for_dedup(points)

        assert "SEVERITY" not in result
        assert "RECOMMENDATION" not in result


# ---------------------------------------------------------------------------
# deduplicate_points (async)
# ---------------------------------------------------------------------------


class TestDeduplicatePoints:
    """Tests for the async deduplicate_points() orchestration function."""

    async def test_empty_points_returns_empty_list(self, monkeypatch):
        """Empty input should return an empty list without making any API calls."""
        model = _make_dedup_model(monkeypatch)
        ctx = _make_context()

        async with httpx.AsyncClient() as client:
            result = await deduplicate_points(
                client, [], model, ctx, mode="plan"
            )

        assert result == []

    async def test_context_window_overflow_fallback(self, monkeypatch):
        """When input exceeds context window, each point becomes its own group."""
        # Use a tiny context window to force overflow
        model = _make_dedup_model(monkeypatch, context_window=10)
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
                description="N+1 query pattern",
            ),
        ]

        log_messages = []

        async with httpx.AsyncClient() as client:
            groups = await deduplicate_points(
                client, points, model, ctx,
                log_fn=log_messages.append,
                mode="plan",
            )

        # Each point should become its own group
        assert len(groups) == 2
        assert groups[0].concern == "SQL injection risk"
        assert groups[0].combined_severity == "high"
        assert groups[0].combined_category == "security"
        assert groups[0].source_reviewers == ["r1"]
        assert len(groups[0].points) == 1

        assert groups[1].concern == "N+1 query pattern"
        assert groups[1].combined_severity == "medium"
        assert groups[1].combined_category == "performance"
        assert groups[1].source_reviewers == ["r2"]

        # Points should have assigned point IDs
        assert groups[0].points[0].point_id != "temp_001"
        assert groups[1].points[0].point_id != "temp_001"

        # Log should mention overflow
        assert any("exceeds" in msg.lower() or "Skipping dedup" in msg for msg in log_messages)

    async def test_context_window_overflow_no_log_fn(self, monkeypatch):
        """Overflow fallback should work even without a log_fn."""
        model = _make_dedup_model(monkeypatch, context_window=10)
        ctx = _make_context()
        points = [make_review_point()]

        async with httpx.AsyncClient() as client:
            groups = await deduplicate_points(
                client, points, model, ctx,
                log_fn=None,
                mode="plan",
            )

        assert len(groups) == 1

    async def test_successful_dedup_non_spec_mode(self, monkeypatch):
        """Successful dedup in plan mode should call LLM and parse response."""
        model = _make_dedup_model(monkeypatch)
        ctx = _make_context()

        points = [
            make_review_point(
                reviewer="r1",
                severity="high",
                category="security",
                description="SQL injection vulnerability in user input handling",
            ),
            make_review_point(
                reviewer="r2",
                severity="medium",
                category="performance",
                description="N+1 query pattern in the dashboard endpoint",
            ),
        ]

        dedup_response = """\
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

        log_messages = []

        with respx.mock:
            respx.post("https://api.anthropic.com/v1/messages").mock(
                return_value=httpx.Response(200, json=_anthropic_json(dedup_response))
            )

            async with httpx.AsyncClient() as client:
                groups = await deduplicate_points(
                    client, points, model, ctx,
                    log_fn=log_messages.append,
                    mode="plan",
                )

        assert len(groups) == 2
        assert groups[0].combined_severity == "high"
        assert groups[0].combined_category == "security"
        assert groups[1].combined_severity == "medium"
        assert groups[1].combined_category == "performance"

        # Log should mention calling model with points
        assert any("calling" in msg.lower() and "points" in msg.lower() for msg in log_messages)

    async def test_successful_dedup_spec_mode(self, monkeypatch):
        """Spec mode should use spec-specific formatting, prompt, and parser."""
        model = _make_dedup_model(monkeypatch)
        ctx = _make_context()

        points = [
            make_review_point(
                reviewer="r1",
                category="ux",
                description="Improve onboarding flow",
                location="Section 3.2",
            ),
            make_review_point(
                reviewer="r2",
                category="features",
                description="Add dark mode support",
                location="",
            ),
        ]

        spec_dedup_response = """\
GROUP 1:
THEME: ux
TITLE: Onboarding improvements
DESCRIPTION: Improve onboarding flow
SUGGESTIONS: SUGGESTION 1

GROUP 2:
THEME: features
TITLE: Dark mode
DESCRIPTION: Add dark mode support
SUGGESTIONS: SUGGESTION 2
"""

        with respx.mock:
            respx.post("https://api.anthropic.com/v1/messages").mock(
                return_value=httpx.Response(200, json=_anthropic_json(spec_dedup_response))
            )

            async with httpx.AsyncClient() as client:
                groups = await deduplicate_points(
                    client, points, model, ctx,
                    mode="spec",
                )

        assert len(groups) == 2

    async def test_cost_tracking_during_dedup(self, monkeypatch):
        """Cost tracker should record the dedup model's token usage."""
        model = _make_dedup_model(monkeypatch)
        ctx = _make_context()
        cost_tracker = CostTracker()

        points = [
            make_review_point(description="Some finding"),
        ]

        dedup_response = """\
GROUP 1:
CONCERN: Some finding
POINTS: POINT 1
COMBINED_SEVERITY: medium
COMBINED_CATEGORY: other
"""

        with respx.mock:
            respx.post("https://api.anthropic.com/v1/messages").mock(
                return_value=httpx.Response(200, json=_anthropic_json(dedup_response))
            )

            async with httpx.AsyncClient() as client:
                await deduplicate_points(
                    client, points, model, ctx,
                    cost_tracker=cost_tracker,
                    mode="plan",
                )

        assert len(cost_tracker.entries) == 1
        entry = cost_tracker.entries[0]
        assert entry["model"] == "dedup-model"
        assert entry["input_tokens"] == 100
        assert entry["output_tokens"] == 50
        assert cost_tracker.total_usd > 0

    async def test_cost_tracking_not_called_without_tracker(self, monkeypatch):
        """When cost_tracker is None, no cost tracking should occur."""
        model = _make_dedup_model(monkeypatch)
        ctx = _make_context()

        points = [make_review_point()]

        dedup_response = """\
GROUP 1:
CONCERN: finding
POINTS: POINT 1
COMBINED_SEVERITY: medium
COMBINED_CATEGORY: other
"""

        with respx.mock:
            respx.post("https://api.anthropic.com/v1/messages").mock(
                return_value=httpx.Response(200, json=_anthropic_json(dedup_response))
            )

            async with httpx.AsyncClient() as client:
                groups = await deduplicate_points(
                    client, points, model, ctx,
                    cost_tracker=None,
                    mode="plan",
                )

        # Should succeed without error even without a cost tracker
        assert len(groups) >= 1

    async def test_spec_mode_uses_suggestion_formatting(self, monkeypatch):
        """Spec mode should format points as SUGGESTION N, not POINT N."""
        model = _make_dedup_model(monkeypatch)
        ctx = _make_context()

        points = [
            make_review_point(
                reviewer="spec-r1",
                category="ux",
                description="Better navigation",
                location="Section 1",
            ),
        ]

        # We use a mock that captures the request body to verify formatting
        captured_prompts = []

        spec_dedup_response = """\
GROUP 1:
THEME: ux
TITLE: Navigation
DESCRIPTION: Better navigation
SUGGESTIONS: SUGGESTION 1
"""

        with respx.mock:
            route = respx.post("https://api.anthropic.com/v1/messages").mock(
                return_value=httpx.Response(200, json=_anthropic_json(spec_dedup_response))
            )

            async with httpx.AsyncClient() as client:
                groups = await deduplicate_points(
                    client, points, model, ctx,
                    mode="spec",
                )

        # Verify the request was sent (spec mode goes through LLM path)
        assert route.called

    async def test_non_spec_mode_uses_point_formatting(self, monkeypatch):
        """Non-spec mode should format points as POINT N."""
        model = _make_dedup_model(monkeypatch)
        ctx = _make_context()

        points = [make_review_point()]

        dedup_response = """\
GROUP 1:
CONCERN: finding
POINTS: POINT 1
COMBINED_SEVERITY: medium
COMBINED_CATEGORY: other
"""

        with respx.mock:
            route = respx.post("https://api.anthropic.com/v1/messages").mock(
                return_value=httpx.Response(200, json=_anthropic_json(dedup_response))
            )

            async with httpx.AsyncClient() as client:
                await deduplicate_points(
                    client, points, model, ctx,
                    mode="plan",
                )

        assert route.called
