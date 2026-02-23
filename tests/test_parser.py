"""Tests for devils_advocate.parser module."""

import pytest
from datetime import datetime, timezone

from devils_advocate.ids import resolve_guid
from devils_advocate.parser import (
    extract_revised_output,
    parse_author_final_response,
    parse_author_response,
    parse_dedup_response,
    parse_rebuttal_response,
    parse_review_response,
)
from devils_advocate.types import (
    ReviewContext,
    ReviewGroup,
    ReviewPoint,
)

from conftest import make_review_group, make_review_point


# ─── TestParseReviewResponse ────────────────────────────────────────────────


class TestParseReviewResponse:
    """Tests for parse_review_response."""

    def test_standard_format(self):
        """Parses a standard REVIEW POINT format."""
        raw = """
REVIEW POINT #1:
SEVERITY: high
CATEGORY: security
DESCRIPTION: SQL injection vulnerability in user input handling
RECOMMENDATION: Use parameterized queries
LOCATION: src/db.py

REVIEW POINT #2:
SEVERITY: low
CATEGORY: documentation
DESCRIPTION: Missing docstring on public API method
RECOMMENDATION: Add docstring following project conventions
LOCATION: src/api.py
"""
        points = parse_review_response(raw, "reviewer_a")
        assert len(points) == 2
        assert points[0].severity == "high"
        assert points[0].category == "security"
        assert "SQL injection" in points[0].description
        assert points[1].severity == "low"
        assert points[1].category == "documentation"

    def test_missing_severity_defaults_to_medium(self):
        """Missing SEVERITY field defaults to 'medium'."""
        raw = """
REVIEW POINT #1:
CATEGORY: correctness
DESCRIPTION: Off-by-one error in loop boundary
RECOMMENDATION: Fix the loop condition
LOCATION: src/main.py
"""
        points = parse_review_response(raw, "reviewer_b")
        assert len(points) == 1
        assert points[0].severity == "medium"

    def test_thinking_tags_stripped(self):
        """<thinking> tags are stripped before parsing."""
        raw = """
<thinking>I need to review this code carefully for issues.</thinking>

REVIEW POINT #1:
SEVERITY: critical
CATEGORY: security
DESCRIPTION: Hardcoded credentials in configuration file
RECOMMENDATION: Use environment variables
LOCATION: config.py
"""
        points = parse_review_response(raw, "reviewer_c")
        assert len(points) == 1
        assert points[0].severity == "critical"
        assert "Hardcoded credentials" in points[0].description

    def test_multiline_description(self):
        """Multiline DESCRIPTION fields are captured fully."""
        raw = """
REVIEW POINT #1:
SEVERITY: high
CATEGORY: architecture
DESCRIPTION: The service layer directly accesses the database
without going through the repository pattern. This creates tight
coupling and makes testing difficult.
RECOMMENDATION: Introduce a repository interface
LOCATION: src/services/user_service.py
"""
        points = parse_review_response(raw, "reviewer_d")
        assert len(points) == 1
        assert "tight" in points[0].description
        assert "coupling" in points[0].description


# ─── TestParseAuthorResponse ────────────────────────────────────────────────


class TestParseAuthorResponse:
    """Tests for parse_author_response."""

    def test_standard_guid_format(self):
        """Parses RESPONSE TO GROUP with GUID resolution."""
        guid = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        group = make_review_group(group_id="grp_001", guid=guid)

        raw = f"""
RESPONSE TO GROUP [{guid}]:
RESOLUTION: ACCEPTED
RATIONALE: The reviewer is right, the error handling needs improvement because
the current approach silently swallows exceptions in the handler module.
"""
        responses = parse_author_response(raw, [group])
        assert len(responses) == 1
        assert responses[0].group_id == "grp_001"
        assert responses[0].resolution == "ACCEPTED"

    def test_numeric_group_refs_positional_fallback(self):
        """Numeric group references fall back to positional matching."""
        group1 = make_review_group(group_id="grp_001", guid="uuid-1111-1111-1111-111111111111")
        group2 = make_review_group(group_id="grp_002", guid="uuid-2222-2222-2222-222222222222")

        raw = """
RESPONSE TO GROUP 1:
RESOLUTION: ACCEPTED
RATIONALE: Agreed with the finding about error handling improvements across the codebase.

RESPONSE TO GROUP 2:
RESOLUTION: REJECTED
RATIONALE: The performance concern is not applicable to our use case here.
"""
        responses = parse_author_response(raw, [group1, group2])
        assert len(responses) == 2
        assert responses[0].group_id == "grp_001"
        assert responses[1].group_id == "grp_002"

    def test_unparseable_resolution_becomes_unknown(self):
        """Unparseable resolution string becomes 'UNKNOWN'."""
        guid = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        group = make_review_group(group_id="grp_001", guid=guid)

        raw = f"""
RESPONSE TO GROUP [{guid}]:
RESOLUTION: MAYBE_LATER
RATIONALE: I will think about it more.
"""
        responses = parse_author_response(raw, [group])
        assert len(responses) == 1
        assert responses[0].resolution == "UNKNOWN"


# ─── TestParseDedupResponse ─────────────────────────────────────────────────


class TestParseDedupResponse:
    """Tests for parse_dedup_response."""

    def test_group_headers_with_point_refs(self):
        """Parses GROUP N headers referencing POINT numbers."""
        p1 = make_review_point(point_id="temp_001", description="SQL injection risk")
        p2 = make_review_point(point_id="temp_002", description="XSS vulnerability")
        p3 = make_review_point(point_id="temp_003", description="Missing input validation")

        ctx = ReviewContext(
            project="test",
            review_id="test_review",
            review_start_time=datetime(2026, 2, 14, 18, 0, 0, tzinfo=timezone.utc),
            id_suffix="xxxx",
        )

        raw = """
GROUP 1:
CONCERN: Security vulnerabilities in input handling
POINTS: POINT 1, POINT 2
COMBINED_SEVERITY: high
COMBINED_CATEGORY: security

GROUP 2:
CONCERN: Input validation gaps
POINTS: POINT 3
COMBINED_SEVERITY: medium
COMBINED_CATEGORY: correctness
"""
        groups = parse_dedup_response(raw, [p1, p2, p3], ctx)
        assert len(groups) == 2
        assert len(groups[0].points) == 2
        assert len(groups[1].points) == 1

    def test_ungrouped_points_create_singletons(self):
        """Points not referenced by any group get singleton groups."""
        p1 = make_review_point(point_id="temp_001", description="Finding one about error paths")
        p2 = make_review_point(point_id="temp_002", description="Finding two about logging")

        ctx = ReviewContext(
            project="test",
            review_id="test_review",
            review_start_time=datetime(2026, 2, 14, 18, 0, 0, tzinfo=timezone.utc),
            id_suffix="xxxx",
        )

        # Only group point 1; point 2 is ungrouped
        raw = """
GROUP 1:
CONCERN: Error handling issues
POINTS: POINT 1
COMBINED_SEVERITY: high
COMBINED_CATEGORY: error_handling
"""
        groups = parse_dedup_response(raw, [p1, p2], ctx)
        # Should have 2 groups: 1 explicit + 1 singleton for ungrouped p2
        assert len(groups) == 2
        assert len(groups[1].points) == 1

    def test_fuzzy_matching_keyword_fallback(self):
        """Keyword matching fallback when no point refs found."""
        p1 = make_review_point(
            point_id="temp_001",
            description="Memory leak in connection pool handler",
        )

        ctx = ReviewContext(
            project="test",
            review_id="test_review",
            review_start_time=datetime(2026, 2, 14, 18, 0, 0, tzinfo=timezone.utc),
            id_suffix="xxxx",
        )

        # No POINTS field, but CONCERN overlaps with point description keywords
        raw = """
GROUP 1:
CONCERN: Memory leak in connection pool
COMBINED_SEVERITY: high
COMBINED_CATEGORY: performance
"""
        groups = parse_dedup_response(raw, [p1], ctx)
        assert len(groups) >= 1
        # The point should have been matched via keyword fallback
        found_points = [p for g in groups for p in g.points]
        assert len(found_points) == 1


# ─── TestExtractRevisedOutput ───────────────────────────────────────────────


class TestExtractRevisedOutput:
    """Tests for extract_revised_output."""

    def test_plan_mode(self):
        """Extracts content between REVISED PLAN markers."""
        raw = """
Some preamble text.

=== REVISED PLAN ===
Step 1: Do the thing
Step 2: Do the other thing
=== END REVISED PLAN ===

Some trailing text.
"""
        result = extract_revised_output(raw, "plan")
        assert "Step 1" in result
        assert "Step 2" in result

    def test_code_mode(self):
        """Extracts content between UNIFIED DIFF markers."""
        raw = """
=== UNIFIED DIFF ===
--- a/src/main.py
+++ b/src/main.py
@@ -10,3 +10,4 @@
 existing line
+new line
=== END UNIFIED DIFF ===
"""
        result = extract_revised_output(raw, "code")
        assert "+new line" in result

    def test_integration_mode(self):
        """Extracts content between REMEDIATION PLAN markers."""
        raw = """
=== REMEDIATION PLAN ===
1. Fix the integration issue
2. Update the API contract
=== END REMEDIATION PLAN ===
"""
        result = extract_revised_output(raw, "integration")
        assert "Fix the integration issue" in result

    def test_no_match_returns_empty(self):
        """Returns empty string when no markers found."""
        raw = "Just some text with no markers at all."
        assert extract_revised_output(raw, "plan") == ""
        assert extract_revised_output(raw, "code") == ""
        assert extract_revised_output(raw, "integration") == ""


# ─── TestResolveGuid ────────────────────────────────────────────────────────


class TestResolveGuid:
    """Tests for _resolve_guid."""

    def test_exact_match(self):
        """Exact GUID match returns the group_id."""
        guid = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        group = make_review_group(group_id="grp_001", guid=guid)
        assert resolve_guid(guid, [group]) == "grp_001"

    def test_extracted_uuid_from_noise(self):
        """UUID extracted from surrounding text."""
        guid = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        group = make_review_group(group_id="grp_001", guid=guid)
        noisy = f"GROUP 3 [{guid}]"
        assert resolve_guid(noisy, [group]) == "grp_001"

    def test_fuzzy_match_1_char_diff(self):
        """Fuzzy match with 1 character difference."""
        guid = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        group = make_review_group(group_id="grp_001", guid=guid)
        # Change one character: 'a' -> 'b' in the first octet
        fuzzy = "b1b2c3d4-e5f6-7890-abcd-ef1234567890"
        assert resolve_guid(fuzzy, [group]) == "grp_001"

    def test_fuzzy_match_2_char_diff(self):
        """Fuzzy match with 2 character differences."""
        guid = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        group = make_review_group(group_id="grp_001", guid=guid)
        # Change exactly two characters: pos 0 (a->b) and pos 1 (1->2)
        fuzzy = "b2b2c3d4-e5f6-7890-abcd-ef1234567890"
        assert resolve_guid(fuzzy, [group]) == "grp_001"

    def test_no_match_returns_none(self):
        """Completely different GUID returns None."""
        guid = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        group = make_review_group(group_id="grp_001", guid=guid)
        unrelated = "ffffffff-ffff-ffff-ffff-ffffffffffff"
        assert resolve_guid(unrelated, [group]) is None


# ─── TestParseRebuttalResponse ──────────────────────────────────────────────


class TestParseRebuttalResponse:
    """Tests for parse_rebuttal_response."""

    def test_standard_rebuttal_format(self):
        """Parses standard REBUTTAL TO GROUP format."""
        guid = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        group = make_review_group(group_id="grp_001", guid=guid)

        raw = f"""
REBUTTAL TO GROUP [{guid}]:
VERDICT: CHALLENGE
RATIONALE: The author's rejection ignores the thread safety concern. The mutex
is not held during the critical section.
"""
        responses = parse_rebuttal_response(raw, "reviewer_b", [group])
        assert len(responses) == 1
        assert responses[0].group_id == "grp_001"
        assert responses[0].verdict == "CHALLENGE"
        assert "thread safety" in responses[0].rationale

    def test_concur_verdict(self):
        """Parses CONCUR verdict correctly."""
        guid = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        group = make_review_group(group_id="grp_001", guid=guid)

        raw = f"""
REBUTTAL TO GROUP [{guid}]:
VERDICT: CONCUR
RATIONALE: The author's resolution is reasonable and addresses the concern.
"""
        responses = parse_rebuttal_response(raw, "reviewer_b", [group])
        assert len(responses) == 1
        assert responses[0].verdict == "CONCUR"


# ─── TestParseAuthorFinalResponse ───────────────────────────────────────────


class TestParseAuthorFinalResponse:
    """Tests for parse_author_final_response."""

    def test_standard_final_response(self):
        """Parses FINAL RESPONSE TO GROUP format."""
        guid = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        group = make_review_group(group_id="grp_001", guid=guid)

        raw = f"""
FINAL RESPONSE TO GROUP [{guid}]:
RESOLUTION: MAINTAINED
RATIONALE: After reviewing the challenge, I still believe the original approach
is correct because the mutex is held for the duration of the critical section.
"""
        responses = parse_author_final_response(raw, [group])
        assert len(responses) == 1
        assert responses[0].group_id == "grp_001"
        assert responses[0].resolution == "MAINTAINED"
        assert "mutex" in responses[0].rationale

    def test_accepted_final_resolution(self):
        """Parses ACCEPTED resolution in final response."""
        guid = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        group = make_review_group(group_id="grp_001", guid=guid)

        raw = f"""
FINAL RESPONSE TO GROUP [{guid}]:
RESOLUTION: ACCEPTED
RATIONALE: After reviewing the challenge more carefully I now agree with the
reviewer that the error handling path is insufficient for production use.
"""
        responses = parse_author_final_response(raw, [group])
        assert len(responses) == 1
        assert responses[0].resolution == "ACCEPTED"
