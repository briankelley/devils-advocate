"""Tests for devils_advocate.parser module."""

import pytest
from datetime import datetime, timezone

from devils_advocate.ids import resolve_guid
from devils_advocate.parser import (
    _normalize_severity,
    _normalize_category,
    _normalize_theme,
    _extract_multiline_field,
    extract_revised_output,
    parse_author_final_response,
    parse_author_response,
    parse_dedup_response,
    parse_rebuttal_response,
    parse_review_response,
    parse_spec_response,
    parse_spec_dedup_response,
)
from devils_advocate.types import (
    ReviewContext,
    ReviewGroup,
    ReviewPoint,
)

from helpers import make_review_group, make_review_point


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


# ─── TestNormalizeSeverity ────────────────────────────────────────────────


class TestNormalizeSeverity:
    """Parameterized tests for _normalize_severity covering all aliases."""

    @pytest.mark.parametrize("raw,expected", [
        ("critical", "critical"),
        ("crit", "critical"),
        ("CRITICAL", "critical"),
        ("  Crit  ", "critical"),
        ("high", "high"),
        ("hi", "high"),
        ("HIGH", "high"),
        ("medium", "medium"),
        ("med", "medium"),
        ("moderate", "medium"),
        ("low", "low"),
        ("lo", "low"),
        ("minor", "low"),
        ("info", "info"),
        ("informational", "info"),
        ("note", "info"),
        ("INFO", "info"),
    ])
    def test_known_aliases(self, raw, expected):
        """All documented aliases resolve to their canonical severity."""
        assert _normalize_severity(raw) == expected

    def test_unknown_defaults_to_medium(self):
        """Unknown severity strings default to 'medium'."""
        assert _normalize_severity("banana") == "medium"

    def test_empty_string_defaults_to_medium(self):
        """Empty string defaults to 'medium'."""
        assert _normalize_severity("") == "medium"


# ─── TestNormalizeCategory ────────────────────────────────────────────────


class TestNormalizeCategory:
    """Parameterized tests for _normalize_category covering all aliases."""

    @pytest.mark.parametrize("raw,expected", [
        ("architecture", "architecture"),
        ("arch", "architecture"),
        ("design", "architecture"),
        ("security", "security"),
        ("sec", "security"),
        ("performance", "performance"),
        ("perf", "performance"),
        ("correctness", "correctness"),
        ("correct", "correctness"),
        ("bug", "correctness"),
        ("maintainability", "maintainability"),
        ("maintain", "maintainability"),
        ("readability", "maintainability"),
        ("error_handling", "error_handling"),
        ("error handling", "error_handling"),
        ("errors", "error_handling"),
        ("testing", "testing"),
        ("test", "testing"),
        ("tests", "testing"),
        ("documentation", "documentation"),
        ("docs", "documentation"),
        ("doc", "documentation"),
        ("other", "other"),
    ])
    def test_known_aliases(self, raw, expected):
        """All documented aliases resolve to their canonical category."""
        assert _normalize_category(raw) == expected

    def test_case_insensitive(self):
        """Category normalization is case-insensitive."""
        assert _normalize_category("ARCHITECTURE") == "architecture"
        assert _normalize_category("Security") == "security"

    def test_unknown_defaults_to_other(self):
        """Unknown category strings default to 'other'."""
        assert _normalize_category("banana") == "other"

    def test_hyphenated_alias(self):
        """Hyphens in category names are normalized to underscores."""
        assert _normalize_category("error-handling") == "error_handling"


# ─── TestNormalizeTheme ───────────────────────────────────────────────────


class TestNormalizeTheme:
    """Parameterized tests for _normalize_theme covering all aliases."""

    @pytest.mark.parametrize("raw,expected", [
        ("ux", "ux"),
        ("user_experience", "ux"),
        ("usability", "ux"),
        ("features", "features"),
        ("feature", "features"),
        ("functionality", "features"),
        ("integrations", "integrations"),
        ("integration", "integrations"),
        ("data_model", "data_model"),
        ("data model", "data_model"),
        ("data", "data_model"),
        ("monetization", "monetization"),
        ("revenue", "monetization"),
        ("pricing", "monetization"),
        ("accessibility", "accessibility"),
        ("a11y", "accessibility"),
        ("performance_ux", "performance_ux"),
        ("performance ux", "performance_ux"),
        ("content", "content"),
        ("social", "social"),
        ("community", "social"),
        ("platform", "platform"),
        ("security_privacy", "security_privacy"),
        ("security privacy", "security_privacy"),
        ("security", "security_privacy"),
        ("privacy", "security_privacy"),
        ("onboarding", "onboarding"),
        ("other", "other"),
    ])
    def test_known_aliases(self, raw, expected):
        """All documented aliases resolve to their canonical theme."""
        assert _normalize_theme(raw) == expected

    def test_case_insensitive(self):
        """Theme normalization is case-insensitive."""
        assert _normalize_theme("UX") == "ux"
        assert _normalize_theme("Features") == "features"

    def test_unknown_defaults_to_other(self):
        """Unknown theme strings default to 'other'."""
        assert _normalize_theme("banana") == "other"


# ─── TestExtractMultilineField ────────────────────────────────────────────


class TestExtractMultilineField:
    """Tests for _extract_multiline_field edge cases."""

    def test_field_present_single_line(self):
        """Extracts a single-line field value correctly."""
        text = "SEVERITY: high\nCATEGORY: security"
        result = _extract_multiline_field(text, "SEVERITY", ["CATEGORY"])
        assert result == "high"

    def test_field_present_multi_line(self):
        """Extracts a multi-line field value up to the next field boundary."""
        text = (
            "DESCRIPTION: This is a long description\n"
            "that spans multiple lines and has detail.\n"
            "RECOMMENDATION: Fix it"
        )
        result = _extract_multiline_field(text, "DESCRIPTION", ["RECOMMENDATION"])
        assert "long description" in result
        assert "spans multiple lines" in result

    def test_field_absent_returns_empty(self):
        """Returns empty string when the field is not present."""
        text = "CATEGORY: security\nDESCRIPTION: something"
        result = _extract_multiline_field(text, "SEVERITY", ["CATEGORY", "DESCRIPTION"])
        assert result == ""

    def test_field_at_end_of_text(self):
        """Extracts a field at the end of text with no following fields."""
        text = "SEVERITY: high\nLOCATION: src/main.py line 42"
        result = _extract_multiline_field(text, "LOCATION", ["REVIEW POINT"])
        assert "src/main.py" in result

    def test_field_with_empty_value(self):
        """A field present but with an empty value returns empty string."""
        text = "SEVERITY: \nCATEGORY: security"
        result = _extract_multiline_field(text, "SEVERITY", ["CATEGORY"])
        assert result == ""

    def test_case_insensitive_field_name(self):
        """Field extraction is case-insensitive."""
        text = "severity: high\ncategory: security"
        result = _extract_multiline_field(text, "SEVERITY", ["CATEGORY"])
        assert result == "high"


# ─── TestParseSpecResponse ────────────────────────────────────────────────


class TestParseSpecResponse:
    """Tests for parse_spec_response (spec-mode suggestion parsing)."""

    def test_standard_suggestion_format(self):
        """Parses SUGGESTION N format with THEME, TITLE, DESCRIPTION, CONTEXT."""
        raw = """
SUGGESTION 1:
THEME: ux
TITLE: Add dark mode support
DESCRIPTION: The app currently only supports light mode which causes eye strain
in low-light environments.
CONTEXT: Settings page and theme configuration

SUGGESTION 2:
THEME: features
TITLE: Export to PDF
DESCRIPTION: Users need the ability to export reports to PDF format for sharing.
CONTEXT: Report generation module
"""
        points = parse_spec_response(raw, "spec_reviewer_a")
        assert len(points) == 2
        assert points[0].category == "ux"
        assert "dark mode" in points[0].description
        assert points[0].location == "Settings page and theme configuration"
        assert points[0].severity == "info"
        assert points[1].category == "features"
        assert "Export to PDF" in points[1].description

    def test_title_only_no_description(self):
        """A suggestion with only TITLE but no DESCRIPTION is still parsed."""
        raw = """
SUGGESTION 1:
THEME: accessibility
TITLE: Screen reader compatibility improvements
CONTEXT: Navigation components
"""
        points = parse_spec_response(raw, "spec_reviewer_b")
        assert len(points) == 1
        assert "Screen reader" in points[0].description

    def test_description_only_no_title(self):
        """A suggestion with only DESCRIPTION but no TITLE is still parsed."""
        raw = """
SUGGESTION 1:
THEME: platform
DESCRIPTION: The API should support webhook callbacks for async operations.
"""
        points = parse_spec_response(raw, "spec_reviewer_c")
        assert len(points) == 1
        assert "webhook" in points[0].description

    def test_thinking_tags_stripped(self):
        """Reasoning/thinking tags are stripped before parsing."""
        raw = """
<thinking>Let me analyze this specification carefully.</thinking>

SUGGESTION 1:
THEME: monetization
TITLE: Tiered pricing model
DESCRIPTION: Implement a freemium tier to drive adoption before conversion.
"""
        points = parse_spec_response(raw, "spec_reviewer_d")
        assert len(points) == 1
        assert "Tiered pricing" in points[0].description

    def test_no_suggestions_returns_empty(self):
        """Input with no SUGGESTION blocks returns an empty list."""
        raw = "Just some text about the spec without any suggestions."
        points = parse_spec_response(raw, "spec_reviewer_e")
        assert len(points) == 0

    def test_start_index_applied(self):
        """Start index offsets the point IDs."""
        raw = """
SUGGESTION 1:
THEME: ux
TITLE: Better onboarding
DESCRIPTION: First-time user experience needs improvement.
"""
        points = parse_spec_response(raw, "spec_reviewer_f", start_index=5)
        assert len(points) == 1
        assert points[0].point_id == "temp_006"


# ─── TestParseSpecDedupResponse ───────────────────────────────────────────


class TestParseSpecDedupResponse:
    """Tests for parse_spec_dedup_response (spec-mode dedup parsing)."""

    def test_groups_suggestions_by_theme(self):
        """Groups suggestions and assigns group/point IDs."""
        p1 = make_review_point(
            point_id="temp_001",
            description="Dark mode support needed for accessibility",
            reviewer="reviewer_a",
        )
        p2 = make_review_point(
            point_id="temp_002",
            description="Night theme requested by users",
            reviewer="reviewer_b",
        )

        ctx = ReviewContext(
            project="test",
            review_id="test_review",
            review_start_time=datetime(2026, 2, 14, 18, 0, 0, tzinfo=timezone.utc),
            id_suffix="xxxx",
        )

        raw = """
GROUP 1:
THEME: ux
TITLE: Dark mode support
DESCRIPTION: Multiple reviewers identified the need for dark/night mode
CONSENSUS: 2 of 2 reviewers
SUGGESTIONS: SUGGESTION 1, SUGGESTION 2
"""
        groups = parse_spec_dedup_response(raw, [p1, p2], ctx)
        assert len(groups) >= 1
        assert groups[0].combined_severity == "info"
        assert groups[0].combined_category == "ux"

    def test_ungrouped_suggestions_create_singletons(self):
        """Suggestions not referenced by any group get singleton groups."""
        p1 = make_review_point(
            point_id="temp_001",
            description="Add export functionality for reports",
            reviewer="reviewer_a",
        )
        p2 = make_review_point(
            point_id="temp_002",
            description="Improve search performance with indexing",
            reviewer="reviewer_b",
        )

        ctx = ReviewContext(
            project="test",
            review_id="test_review",
            review_start_time=datetime(2026, 2, 14, 18, 0, 0, tzinfo=timezone.utc),
            id_suffix="xxxx",
        )

        # Only group suggestion 1; suggestion 2 is ungrouped
        raw = """
GROUP 1:
THEME: features
TITLE: Export functionality
DESCRIPTION: Add report export capability
SUGGESTIONS: SUGGESTION 1
"""
        groups = parse_spec_dedup_response(raw, [p1, p2], ctx)
        # Should have 2 groups: 1 explicit + 1 singleton for ungrouped p2
        assert len(groups) == 2
        singleton = groups[1]
        assert len(singleton.points) == 1
        assert singleton.combined_severity == "info"

    def test_consensus_indicator_parsed(self):
        """CONSENSUS field is present in parsed response."""
        p1 = make_review_point(
            point_id="temp_001",
            description="Add webhooks for integrations",
            reviewer="reviewer_a",
        )

        ctx = ReviewContext(
            project="test",
            review_id="test_review",
            review_start_time=datetime(2026, 2, 14, 18, 0, 0, tzinfo=timezone.utc),
            id_suffix="xxxx",
        )

        raw = """
GROUP 1:
THEME: integrations
TITLE: Webhook support
DESCRIPTION: Add webhook callbacks for async event notification
CONSENSUS: 1 of 2 reviewers
SUGGESTIONS: SUGGESTION 1
"""
        groups = parse_spec_dedup_response(raw, [p1], ctx)
        assert len(groups) == 1
        assert groups[0].combined_category == "integrations"


# ─── TestParseAuthorResponseExpanded ──────────────────────────────────────


class TestParseAuthorResponseExpanded:
    """Expanded tests for parse_author_response: partial GUID, multi-group mixed."""

    def test_partial_guid_match(self):
        """Author references a GUID with 1-2 character errors, resolved via fuzzy matching."""
        guid = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        group = make_review_group(group_id="grp_001", guid=guid)

        # Introduce 1 character error in the GUID
        bad_guid = "b1b2c3d4-e5f6-7890-abcd-ef1234567890"
        raw = f"""
RESPONSE TO GROUP [{bad_guid}]:
RESOLUTION: ACCEPTED
RATIONALE: The reviewer is correct about the vulnerability. Fixing immediately.
"""
        responses = parse_author_response(raw, [group])
        assert len(responses) == 1
        assert responses[0].group_id == "grp_001"
        assert responses[0].resolution == "ACCEPTED"

    def test_multi_group_with_mixed_guids(self):
        """Multiple groups with mixed GUID and positional references."""
        guid1 = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        guid2 = "11111111-2222-3333-4444-555555555555"
        guid3 = "ffffffff-9999-8888-7777-666666666666"
        group1 = make_review_group(group_id="grp_001", guid=guid1)
        group2 = make_review_group(group_id="grp_002", guid=guid2)
        group3 = make_review_group(group_id="grp_003", guid=guid3)

        raw = f"""
RESPONSE TO GROUP [{guid1}]:
RESOLUTION: ACCEPTED
RATIONALE: Agreed with the security finding about input validation.

RESPONSE TO GROUP [{guid2}]:
RESOLUTION: REJECTED
RATIONALE: The performance concern is not applicable to our batch processing use case.

RESPONSE TO GROUP [{guid3}]:
RESOLUTION: PARTIAL
RATIONALE: Will address the logging concern but the metrics collection part is out of scope.
"""
        responses = parse_author_response(raw, [group1, group2, group3])
        assert len(responses) == 3
        assert responses[0].group_id == "grp_001"
        assert responses[0].resolution == "ACCEPTED"
        assert responses[1].group_id == "grp_002"
        assert responses[1].resolution == "REJECTED"
        assert responses[2].group_id == "grp_003"
        assert responses[2].resolution == "PARTIAL"


# ─── TestParseRebuttalResponseExpanded ────────────────────────────────────


class TestParseRebuttalResponseExpanded:
    """Expanded tests for parse_rebuttal_response: unknown verdict, multiple reviewers."""

    def test_unknown_verdict_defaults_to_concur(self):
        """Unparseable verdict string defaults to 'CONCUR'."""
        guid = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        group = make_review_group(group_id="grp_001", guid=guid)

        raw = f"""
REBUTTAL TO GROUP [{guid}]:
VERDICT: UNDECIDED
RATIONALE: Not sure what to make of the author's response.
"""
        responses = parse_rebuttal_response(raw, "reviewer_x", [group])
        assert len(responses) == 1
        assert responses[0].verdict == "CONCUR"

    def test_multiple_reviewers_same_group(self):
        """Multiple rebuttals to the same group from different calls."""
        guid = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        group = make_review_group(group_id="grp_001", guid=guid)

        raw_r1 = f"""
REBUTTAL TO GROUP [{guid}]:
VERDICT: CHALLENGE
RATIONALE: The author has not addressed the thread safety issue at all.
"""
        raw_r2 = f"""
REBUTTAL TO GROUP [{guid}]:
VERDICT: CONCUR
RATIONALE: The author's explanation is satisfactory.
"""
        responses_r1 = parse_rebuttal_response(raw_r1, "reviewer_alpha", [group])
        responses_r2 = parse_rebuttal_response(raw_r2, "reviewer_beta", [group])

        assert len(responses_r1) == 1
        assert responses_r1[0].reviewer == "reviewer_alpha"
        assert responses_r1[0].verdict == "CHALLENGE"

        assert len(responses_r2) == 1
        assert responses_r2[0].reviewer == "reviewer_beta"
        assert responses_r2[0].verdict == "CONCUR"

    def test_multiple_groups_single_response(self):
        """A reviewer's rebuttal covers multiple groups in one response."""
        guid1 = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        guid2 = "11111111-2222-3333-4444-555555555555"
        group1 = make_review_group(group_id="grp_001", guid=guid1)
        group2 = make_review_group(group_id="grp_002", guid=guid2)

        raw = f"""
REBUTTAL TO GROUP [{guid1}]:
VERDICT: CONCUR
RATIONALE: The author's fix for the SQL injection is appropriate.

REBUTTAL TO GROUP [{guid2}]:
VERDICT: CHALLENGE
RATIONALE: The error handling approach still leaves a race condition.
"""
        responses = parse_rebuttal_response(raw, "reviewer_gamma", [group1, group2])
        assert len(responses) == 2
        assert responses[0].group_id == "grp_001"
        assert responses[0].verdict == "CONCUR"
        assert responses[1].group_id == "grp_002"
        assert responses[1].verdict == "CHALLENGE"
