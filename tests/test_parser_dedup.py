"""Tests for parser module (parse_review_response, parse_author_response,
parse_rebuttal_response, parse_author_final_response, extract_revised_output,
severity/category/theme normalization) and dedup module (promote_points_to_groups,
format_points_for_dedup, format_suggestions_for_dedup, deduplicate_points).
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helpers import make_model_config, make_review_group, make_review_point


def _make_context(project="test", review_id="test-001"):
    from devils_advocate.types import ReviewContext
    return ReviewContext(
        project=project,
        review_id=review_id,
        review_start_time=datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc),
        id_suffix="abc1",
    )


# ═══════════════════════════════════════════════════════════════════════════
# 1. Severity normalization
# ═══════════════════════════════════════════════════════════════════════════


class TestNormalizeSeverity:
    def test_exact_match(self):
        from devils_advocate.parser import _normalize_severity
        assert _normalize_severity("high") == "high"
        assert _normalize_severity("critical") == "critical"
        assert _normalize_severity("low") == "low"

    def test_aliases(self):
        from devils_advocate.parser import _normalize_severity
        assert _normalize_severity("crit") == "critical"
        assert _normalize_severity("hi") == "high"
        assert _normalize_severity("med") == "medium"
        assert _normalize_severity("moderate") == "medium"
        assert _normalize_severity("lo") == "low"
        assert _normalize_severity("minor") == "low"
        assert _normalize_severity("informational") == "info"

    def test_case_insensitive(self):
        from devils_advocate.parser import _normalize_severity
        assert _normalize_severity("HIGH") == "high"
        assert _normalize_severity("Critical") == "critical"

    def test_unknown_defaults_medium(self):
        from devils_advocate.parser import _normalize_severity
        assert _normalize_severity("extreme") == "medium"
        assert _normalize_severity("") == "medium"


# ═══════════════════════════════════════════════════════════════════════════
# 2. Category normalization
# ═══════════════════════════════════════════════════════════════════════════


class TestNormalizeCategory:
    def test_exact_match(self):
        from devils_advocate.parser import _normalize_category
        assert _normalize_category("security") == "security"
        assert _normalize_category("performance") == "performance"

    def test_aliases(self):
        from devils_advocate.parser import _normalize_category
        assert _normalize_category("arch") == "architecture"
        assert _normalize_category("design") == "architecture"
        assert _normalize_category("sec") == "security"
        assert _normalize_category("perf") == "performance"
        assert _normalize_category("bug") == "correctness"
        assert _normalize_category("maintain") == "maintainability"
        assert _normalize_category("readability") == "maintainability"
        assert _normalize_category("error handling") == "error_handling"
        assert _normalize_category("test") == "testing"
        assert _normalize_category("docs") == "documentation"

    def test_unknown_defaults_other(self):
        from devils_advocate.parser import _normalize_category
        assert _normalize_category("weird") == "other"


# ═══════════════════════════════════════════════════════════════════════════
# 3. Theme normalization
# ═══════════════════════════════════════════════════════════════════════════


class TestNormalizeTheme:
    def test_exact_match(self):
        from devils_advocate.parser import _normalize_theme
        assert _normalize_theme("ux") == "ux"
        assert _normalize_theme("features") == "features"

    def test_aliases(self):
        from devils_advocate.parser import _normalize_theme
        assert _normalize_theme("user_experience") == "ux"
        assert _normalize_theme("usability") == "ux"
        assert _normalize_theme("functionality") == "features"
        assert _normalize_theme("a11y") == "accessibility"
        assert _normalize_theme("revenue") == "monetization"
        assert _normalize_theme("privacy") == "security_privacy"
        assert _normalize_theme("community") == "social"

    def test_unknown_defaults_other(self):
        from devils_advocate.parser import _normalize_theme
        assert _normalize_theme("xyz") == "other"


# ═══════════════════════════════════════════════════════════════════════════
# 4. parse_review_response
# ═══════════════════════════════════════════════════════════════════════════


class TestParseReviewResponse:
    def test_single_point(self):
        from devils_advocate.parser import parse_review_response
        raw = (
            "REVIEW POINT 1:\n"
            "SEVERITY: high\n"
            "CATEGORY: security\n"
            "DESCRIPTION: SQL injection vulnerability\n"
            "RECOMMENDATION: Use parameterized queries\n"
            "LOCATION: db.py:42\n"
        )
        points = parse_review_response(raw, "reviewer-1")
        assert len(points) == 1
        assert points[0].severity == "high"
        assert points[0].category == "security"
        assert "SQL injection" in points[0].description
        assert points[0].location == "db.py:42"
        assert points[0].reviewer == "reviewer-1"

    def test_multiple_points(self):
        from devils_advocate.parser import parse_review_response
        raw = (
            "REVIEW POINT 1:\n"
            "SEVERITY: high\n"
            "CATEGORY: security\n"
            "DESCRIPTION: Issue A\n"
            "RECOMMENDATION: Fix A\n\n"
            "REVIEW POINT 2:\n"
            "SEVERITY: low\n"
            "CATEGORY: documentation\n"
            "DESCRIPTION: Issue B\n"
            "RECOMMENDATION: Fix B\n"
        )
        points = parse_review_response(raw, "r1")
        assert len(points) == 2

    def test_strips_thinking_tags(self):
        from devils_advocate.parser import parse_review_response
        raw = (
            "<thinking>Let me analyze this...</thinking>\n"
            "REVIEW POINT 1:\n"
            "SEVERITY: medium\n"
            "CATEGORY: correctness\n"
            "DESCRIPTION: Off-by-one error\n"
            "RECOMMENDATION: Fix the loop\n"
        )
        points = parse_review_response(raw, "r1")
        assert len(points) == 1
        assert "Let me analyze" not in points[0].description

    def test_no_description_skips_block(self):
        from devils_advocate.parser import parse_review_response
        raw = (
            "REVIEW POINT 1:\n"
            "SEVERITY: high\n"
            "CATEGORY: security\n"
            "RECOMMENDATION: Fix it\n"
        )
        points = parse_review_response(raw, "r1")
        assert len(points) == 0

    def test_start_index(self):
        from devils_advocate.parser import parse_review_response
        raw = (
            "REVIEW POINT 1:\n"
            "SEVERITY: high\n"
            "DESCRIPTION: Issue\n"
        )
        points = parse_review_response(raw, "r1", start_index=10)
        assert points[0].point_id == "temp_011"

    def test_missing_recommendation_uses_default(self):
        from devils_advocate.parser import parse_review_response
        raw = (
            "REVIEW POINT 1:\n"
            "SEVERITY: high\n"
            "CATEGORY: security\n"
            "DESCRIPTION: Missing auth\n"
        )
        points = parse_review_response(raw, "r1")
        assert "No specific recommendation" in points[0].recommendation

    def test_point_header_variations(self):
        from devils_advocate.parser import parse_review_response
        for header in ["REVIEW POINT 1:", "POINT 1:", "ISSUE 1:", "POINT #1:"]:
            raw = f"{header}\nDESCRIPTION: Something\nSEVERITY: high\n"
            points = parse_review_response(raw, "r1")
            assert len(points) == 1, f"Failed for header: {header}"


# ═══════════════════════════════════════════════════════════════════════════
# 5. parse_author_response
# ═══════════════════════════════════════════════════════════════════════════


class TestParseAuthorResponse:
    def test_single_response(self):
        from devils_advocate.parser import parse_author_response
        g = make_review_group(group_id="g1")
        g.guid = "test-guid-1"
        raw = (
            "RESPONSE TO GROUP [test-guid-1]:\n"
            "RESOLUTION: ACCEPTED\n"
            "RATIONALE: Valid concern that needs addressing with proper fix\n"
        )
        responses = parse_author_response(raw, [g])
        assert len(responses) == 1
        assert responses[0].group_id == "g1"
        assert responses[0].resolution == "ACCEPTED"

    def test_rejected(self):
        from devils_advocate.parser import parse_author_response
        g = make_review_group(group_id="g1")
        g.guid = "test-guid-1"
        raw = (
            "RESPONSE TO GROUP [test-guid-1]:\n"
            "RESOLUTION: REJECTED\n"
            "RATIONALE: Not applicable\n"
        )
        responses = parse_author_response(raw, [g])
        assert responses[0].resolution == "REJECTED"

    def test_partial(self):
        from devils_advocate.parser import parse_author_response
        g = make_review_group(group_id="g1")
        g.guid = "test-guid-1"
        raw = (
            "RESPONSE TO GROUP [test-guid-1]:\n"
            "RESOLUTION: PARTIALLY ACCEPTED\n"
            "RATIONALE: Only some apply\n"
        )
        responses = parse_author_response(raw, [g])
        assert responses[0].resolution == "PARTIAL"

    def test_unknown_resolution(self):
        from devils_advocate.parser import parse_author_response
        g = make_review_group(group_id="g1")
        g.guid = "test-guid-1"
        raw = (
            "RESPONSE TO GROUP [test-guid-1]:\n"
            "RESOLUTION: WONTFIX\n"
            "RATIONALE: Not doing it\n"
        )
        responses = parse_author_response(raw, [g])
        assert responses[0].resolution == "UNKNOWN"

    def test_positional_fallback(self):
        from devils_advocate.parser import parse_author_response
        g = make_review_group(group_id="real-id")
        g.guid = ""  # No GUID match possible
        raw = (
            "RESPONSE TO GROUP 1:\n"
            "RESOLUTION: ACCEPTED\n"
            "RATIONALE: OK\n"
        )
        responses = parse_author_response(raw, [g])
        assert len(responses) == 1
        assert responses[0].group_id == "real-id"


# ═══════════════════════════════════════════════════════════════════════════
# 6. parse_rebuttal_response
# ═══════════════════════════════════════════════════════════════════════════


class TestParseRebuttalResponse:
    def test_challenge(self):
        from devils_advocate.parser import parse_rebuttal_response
        g = make_review_group(group_id="g1")
        g.guid = "uuid-1"
        raw = (
            "REBUTTAL TO GROUP [uuid-1]:\n"
            "VERDICT: CHALLENGE\n"
            "RATIONALE: The author's fix is incomplete\n"
        )
        responses = parse_rebuttal_response(raw, "reviewer-1", [g])
        assert len(responses) == 1
        assert responses[0].verdict == "CHALLENGE"
        assert responses[0].reviewer == "reviewer-1"

    def test_concur(self):
        from devils_advocate.parser import parse_rebuttal_response
        g = make_review_group(group_id="g1")
        g.guid = "uuid-1"
        raw = (
            "REBUTTAL TO GROUP [uuid-1]:\n"
            "VERDICT: CONCUR\n"
            "RATIONALE: The author's approach is sound\n"
        )
        responses = parse_rebuttal_response(raw, "reviewer-1", [g])
        assert responses[0].verdict == "CONCUR"

    def test_unknown_verdict_defaults_concur(self):
        from devils_advocate.parser import parse_rebuttal_response
        g = make_review_group(group_id="g1")
        g.guid = "uuid-1"
        raw = (
            "REBUTTAL TO GROUP [uuid-1]:\n"
            "VERDICT: MAYBE\n"
            "RATIONALE: Unsure\n"
        )
        responses = parse_rebuttal_response(raw, "reviewer-1", [g])
        assert responses[0].verdict == "CONCUR"

    def test_unmatched_group_skipped(self):
        from devils_advocate.parser import parse_rebuttal_response
        g = make_review_group(group_id="g1")
        g.guid = "uuid-1"
        raw = (
            "REBUTTAL TO GROUP [unknown-uuid]:\n"
            "VERDICT: CHALLENGE\n"
        )
        responses = parse_rebuttal_response(raw, "reviewer-1", [g])
        assert len(responses) == 0


# ═══════════════════════════════════════════════════════════════════════════
# 7. parse_author_final_response
# ═══════════════════════════════════════════════════════════════════════════


class TestParseAuthorFinalResponse:
    def test_maintained(self):
        from devils_advocate.parser import parse_author_final_response
        g = make_review_group(group_id="g1")
        g.guid = "uuid-1"
        raw = (
            "FINAL RESPONSE TO GROUP [uuid-1]:\n"
            "RESOLUTION: MAINTAINED\n"
            "RATIONALE: I stand by my original assessment\n"
        )
        responses = parse_author_final_response(raw, [g])
        assert len(responses) == 1
        assert responses[0].resolution == "MAINTAINED"

    def test_accepted_after_challenge(self):
        from devils_advocate.parser import parse_author_final_response
        g = make_review_group(group_id="g1")
        g.guid = "uuid-1"
        raw = (
            "FINAL RESPONSE TO GROUP [uuid-1]:\n"
            "RESOLUTION: ACCEPTED\n"
            "RATIONALE: On reflection the reviewer is correct\n"
        )
        responses = parse_author_final_response(raw, [g])
        assert responses[0].resolution == "ACCEPTED"

    def test_unknown_resolution_defaults_maintained(self):
        from devils_advocate.parser import parse_author_final_response
        g = make_review_group(group_id="g1")
        g.guid = "uuid-1"
        raw = (
            "FINAL RESPONSE TO GROUP [uuid-1]:\n"
            "RESOLUTION: WHATEVER\n"
            "RATIONALE: Something\n"
        )
        responses = parse_author_final_response(raw, [g])
        assert responses[0].resolution == "MAINTAINED"


# ═══════════════════════════════════════════════════════════════════════════
# 8. extract_revised_output
# ═══════════════════════════════════════════════════════════════════════════


class TestExtractRevisedOutput:
    def test_plan_mode(self):
        from devils_advocate.parser import extract_revised_output
        raw = "preamble\n=== REVISED PLAN ===\nContent\n=== END REVISED PLAN ===\npostamble"
        assert extract_revised_output(raw, "plan") == "Content"

    def test_code_mode(self):
        from devils_advocate.parser import extract_revised_output
        raw = "=== UNIFIED DIFF ===\n--- a/file\n=== END UNIFIED DIFF ==="
        assert "--- a/file" in extract_revised_output(raw, "code")

    def test_integration_mode(self):
        from devils_advocate.parser import extract_revised_output
        raw = "=== REMEDIATION PLAN ===\nFix\n=== END REMEDIATION PLAN ==="
        assert extract_revised_output(raw, "integration") == "Fix"

    def test_spec_mode(self):
        from devils_advocate.parser import extract_revised_output
        raw = "=== SPEC SUGGESTIONS ===\nSuggestions\n=== END SPEC SUGGESTIONS ==="
        assert extract_revised_output(raw, "spec") == "Suggestions"

    def test_no_delimiters_returns_empty(self):
        from devils_advocate.parser import extract_revised_output
        assert extract_revised_output("just text", "plan") == ""


# ═══════════════════════════════════════════════════════════════════════════
# 9. promote_points_to_groups
# ═══════════════════════════════════════════════════════════════════════════


class TestPromotePointsToGroups:
    def test_each_point_becomes_group(self):
        from devils_advocate.dedup import promote_points_to_groups
        ctx = _make_context()
        p1 = make_review_point(reviewer="r1", description="Issue A")
        p2 = make_review_point(reviewer="r2", description="Issue B")
        groups = promote_points_to_groups([p1, p2], ctx)
        assert len(groups) == 2
        assert groups[0].concern == "Issue A"
        assert groups[1].concern == "Issue B"
        assert groups[0].source_reviewers == ["r1"]

    def test_empty_list(self):
        from devils_advocate.dedup import promote_points_to_groups
        ctx = _make_context()
        assert promote_points_to_groups([], ctx) == []


# ═══════════════════════════════════════════════════════════════════════════
# 10. format_points_for_dedup
# ═══════════════════════════════════════════════════════════════════════════


class TestFormatPointsForDedup:
    def test_basic_formatting(self):
        from devils_advocate.dedup import format_points_for_dedup
        p = make_review_point(
            reviewer="r1", severity="high", category="security",
            description="SQL injection", recommendation="Use params",
            location="db.py:42",
        )
        result = format_points_for_dedup([p])
        assert "POINT 1:" in result
        assert "REVIEWER: r1" in result
        assert "SEVERITY: high" in result
        assert "DESCRIPTION: SQL injection" in result
        assert "LOCATION: db.py:42" in result

    def test_no_location_omits_field(self):
        from devils_advocate.dedup import format_points_for_dedup
        p = make_review_point(location="")
        result = format_points_for_dedup([p])
        assert "LOCATION:" not in result

    def test_multiple_points_numbered(self):
        from devils_advocate.dedup import format_points_for_dedup
        p1 = make_review_point(description="A")
        p2 = make_review_point(description="B")
        result = format_points_for_dedup([p1, p2])
        assert "POINT 1:" in result
        assert "POINT 2:" in result


# ═══════════════════════════════════════════════════════════════════════════
# 11. format_suggestions_for_dedup
# ═══════════════════════════════════════════════════════════════════════════


class TestFormatSuggestionsForDedup:
    def test_basic_formatting(self):
        from devils_advocate.dedup import format_suggestions_for_dedup
        p = make_review_point(
            reviewer="r1", category="ux",
            description="Add search feature", location="UI context",
        )
        result = format_suggestions_for_dedup([p])
        assert "SUGGESTION 1:" in result
        assert "REVIEWER: r1" in result
        assert "THEME: ux" in result
        assert "DESCRIPTION: Add search feature" in result
        assert "CONTEXT: UI context" in result


# ═══════════════════════════════════════════════════════════════════════════
# 12. deduplicate_points
# ═══════════════════════════════════════════════════════════════════════════


class TestDeduplicatePoints:
    @pytest.mark.asyncio
    async def test_empty_points_returns_empty(self):
        from devils_advocate.dedup import deduplicate_points
        ctx = _make_context()
        model = make_model_config(name="dedup-model")
        result = await deduplicate_points(MagicMock(), [], model, ctx)
        assert result == []

    @pytest.mark.asyncio
    async def test_context_overflow_promotes(self):
        from devils_advocate.dedup import deduplicate_points
        ctx = _make_context()
        model = make_model_config(name="dedup-model", context_window=10)
        p = make_review_point(description="Issue with a long description")
        log = MagicMock()
        result = await deduplicate_points(
            MagicMock(), [p], model, ctx, log_fn=log,
        )
        assert len(result) == 1
        assert result[0].concern == "Issue with a long description"

    @pytest.mark.asyncio
    async def test_successful_dedup(self):
        from devils_advocate.dedup import deduplicate_points
        from devils_advocate.types import CostTracker
        ctx = _make_context()
        model = make_model_config(name="dedup-model")
        cost = CostTracker()

        p1 = make_review_point(reviewer="r1", description="Issue A")
        p2 = make_review_point(reviewer="r2", description="Issue B")

        dedup_response = (
            "GROUP 1:\n"
            "CONCERN: Both issues related\n"
            "POINTS: Point 1, Point 2\n"
            "COMBINED_SEVERITY: high\n"
            "COMBINED_CATEGORY: security\n"
        )
        usage = {"input_tokens": 200, "output_tokens": 100}

        with patch("devils_advocate.dedup.call_with_retry",
                    new_callable=AsyncMock, return_value=(dedup_response, usage)):
            groups = await deduplicate_points(
                MagicMock(), [p1, p2], model, ctx,
                cost_tracker=cost,
            )

        assert len(groups) >= 1
        assert cost.total_usd > 0


# ═══════════════════════════════════════════════════════════════════════════
# 13. parse_spec_response
# ═══════════════════════════════════════════════════════════════════════════


class TestParseSpecResponse:
    def test_single_suggestion(self):
        from devils_advocate.parser import parse_spec_response
        raw = (
            "SUGGESTION 1:\n"
            "THEME: ux\n"
            "TITLE: Add dark mode\n"
            "DESCRIPTION: Users prefer dark mode\n"
            "CONTEXT: Settings page\n"
        )
        points = parse_spec_response(raw, "reviewer-1")
        assert len(points) == 1
        assert points[0].severity == "info"
        assert "dark mode" in points[0].description.lower()
        assert points[0].category == "ux"

    def test_no_title_uses_description(self):
        from devils_advocate.parser import parse_spec_response
        raw = (
            "SUGGESTION 1:\n"
            "THEME: features\n"
            "DESCRIPTION: Add sorting feature\n"
        )
        points = parse_spec_response(raw, "r1")
        assert len(points) == 1
        assert "sorting" in points[0].description

    def test_strips_thinking_tags(self):
        from devils_advocate.parser import parse_spec_response
        raw = (
            "<think>Analyzing...</think>\n"
            "SUGGESTION 1:\n"
            "THEME: performance\n"
            "TITLE: Cache results\n"
            "DESCRIPTION: Add caching layer\n"
        )
        points = parse_spec_response(raw, "r1")
        assert len(points) == 1
        assert "Analyzing" not in points[0].description


# ═══════════════════════════════════════════════════════════════════════════
# 14. _extract_multiline_field
# ═══════════════════════════════════════════════════════════════════════════


class TestExtractMultilineField:
    def test_single_line(self):
        from devils_advocate.parser import _extract_multiline_field
        text = "DESCRIPTION: Simple value\nNEXT: Other"
        result = _extract_multiline_field(text, "DESCRIPTION", ["NEXT"])
        assert result == "Simple value"

    def test_multiline(self):
        from devils_advocate.parser import _extract_multiline_field
        text = "DESCRIPTION: Line 1\nLine 2\nLine 3\nNEXT: stop"
        result = _extract_multiline_field(text, "DESCRIPTION", ["NEXT"])
        assert "Line 1" in result
        assert "Line 3" in result

    def test_missing_field(self):
        from devils_advocate.parser import _extract_multiline_field
        text = "OTHER: value"
        result = _extract_multiline_field(text, "DESCRIPTION", ["OTHER"])
        assert result == ""
