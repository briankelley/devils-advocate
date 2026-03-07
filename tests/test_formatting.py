"""Tests for devils_advocate.orchestrator._formatting module."""

from __future__ import annotations

import pytest
from dataclasses import asdict

from devils_advocate.orchestrator._formatting import (
    _compute_summary,
    _format_author_responses_for_rebuttal,
    _format_challenged_groups,
    _format_groups_for_author,
    _get_contested_groups_for_reviewer,
    _group_to_dict,
)
from devils_advocate.types import (
    AuthorResponse,
    GovernanceDecision,
    RebuttalResponse,
    Resolution,
    ReviewGroup,
    ReviewPoint,
)

from helpers import (
    make_author_response,
    make_rebuttal,
    make_review_group,
    make_review_point,
)


# ─── TestFormatGroupsForAuthor ────────────────────────────────────────────────


class TestFormatGroupsForAuthor:
    """Tests for _format_groups_for_author: GUID embedding, reviewer grammar, feedback nesting."""

    def test_guid_embedded_in_header(self):
        """Group header includes the GUID in square brackets."""
        group = make_review_group(guid="abc-123")
        text = _format_groups_for_author([group])
        assert "[abc-123]" in text

    def test_group_numbering_sequential(self):
        """Groups are numbered starting from 1."""
        g1 = make_review_group(group_id="grp_1", guid="guid-1")
        g2 = make_review_group(group_id="grp_2", guid="guid-2")
        text = _format_groups_for_author([g1, g2])
        assert "GROUP 1 [guid-1]:" in text
        assert "GROUP 2 [guid-2]:" in text

    def test_single_reviewer_grammar(self):
        """Single reviewer shows '1 reviewer' (singular)."""
        group = make_review_group(source_reviewers=["alice"], guid="g1")
        text = _format_groups_for_author([group])
        assert "(1 reviewer)" in text
        assert "(1 reviewers)" not in text

    def test_multiple_reviewers_grammar(self):
        """Multiple reviewers show 'N reviewers' (plural)."""
        group = make_review_group(source_reviewers=["alice", "bob"], guid="g2")
        text = _format_groups_for_author([group])
        assert "(2 reviewers)" in text

    def test_reviewer_names_listed(self):
        """Reviewer names appear in the REVIEWERS line."""
        group = make_review_group(source_reviewers=["alice", "bob", "charlie"], guid="g3")
        text = _format_groups_for_author([group])
        assert "REVIEWERS: alice, bob, charlie" in text

    def test_concern_shown(self):
        """The concern text is included."""
        group = make_review_group(concern="Missing error handling", guid="g4")
        text = _format_groups_for_author([group])
        assert "CONCERN: Missing error handling" in text

    def test_severity_and_category(self):
        """Severity and category are shown."""
        group = make_review_group(
            combined_severity="high",
            combined_category="security",
            guid="g5",
        )
        text = _format_groups_for_author([group])
        assert "SEVERITY: high" in text
        assert "CATEGORY: security" in text

    def test_feedback_nesting_with_recommendation(self):
        """Feedback includes reviewer attribution and recommendation indented."""
        p = make_review_point(
            reviewer="alice",
            description="SQL injection risk",
            recommendation="Use parameterized queries",
            location="app.py line 10",
        )
        group = make_review_group(points=[p], guid="g6")
        text = _format_groups_for_author([group])
        assert "[alice] SQL injection risk" in text
        assert "Recommendation: Use parameterized queries" in text
        assert "Location: app.py line 10" in text

    def test_feedback_without_recommendation(self):
        """Points without recommendations skip that line."""
        p = make_review_point(
            reviewer="bob",
            description="Minor issue",
            recommendation="",
            location="",
        )
        group = make_review_group(points=[p], guid="g7")
        text = _format_groups_for_author([group])
        assert "[bob] Minor issue" in text
        assert "Recommendation:" not in text

    def test_feedback_without_location(self):
        """Points without location skip that line."""
        p = make_review_point(
            reviewer="carol",
            description="Some issue",
            recommendation="Fix it",
            location="",
        )
        group = make_review_group(points=[p], guid="g8")
        text = _format_groups_for_author([group])
        assert "Location:" not in text

    def test_multiple_points_in_group(self):
        """Multiple points in a group are all rendered under FEEDBACK."""
        p1 = make_review_point(reviewer="alice", description="Issue A")
        p2 = make_review_point(reviewer="bob", description="Issue B")
        group = make_review_group(points=[p1, p2], source_reviewers=["alice", "bob"], guid="g9")
        text = _format_groups_for_author([group])
        assert "[alice] Issue A" in text
        assert "[bob] Issue B" in text
        assert "FEEDBACK:" in text

    def test_empty_groups_list(self):
        """Empty group list produces empty string."""
        text = _format_groups_for_author([])
        assert text == ""


# ─── TestFormatAuthorResponsesForRebuttal ─────────────────────────────────────


class TestFormatAuthorResponsesForRebuttal:
    """Tests for _format_author_responses_for_rebuttal: response map lookup and fallback."""

    def test_response_found(self):
        """When author response exists, resolution and rationale are shown."""
        group = make_review_group(group_id="grp_1", guid="guid-1", concern="Some concern")
        ar = make_author_response(group_id="grp_1", resolution="ACCEPTED", rationale="Good point")
        text = _format_author_responses_for_rebuttal([group], [ar])
        assert "GROUP [guid-1]:" in text
        assert "RESOLUTION: ACCEPTED" in text
        assert "RATIONALE: Good point" in text

    def test_no_author_response_fallback(self):
        """Missing author response shows '[NO AUTHOR RESPONSE]' fallback."""
        group = make_review_group(group_id="grp_orphan", guid="guid-orphan")
        text = _format_author_responses_for_rebuttal([group], [])
        assert "[NO AUTHOR RESPONSE]" in text

    def test_multiple_groups(self):
        """Each group is formatted with its respective response or fallback."""
        g1 = make_review_group(group_id="grp_a", guid="guid-a", concern="Concern A")
        g2 = make_review_group(group_id="grp_b", guid="guid-b", concern="Concern B")
        ar1 = make_author_response(group_id="grp_a", resolution="REJECTED", rationale="Not valid")
        # No response for grp_b
        text = _format_author_responses_for_rebuttal([g1, g2], [ar1])
        assert "GROUP [guid-a]:" in text
        assert "RESOLUTION: REJECTED" in text
        assert "RATIONALE: Not valid" in text
        assert "GROUP [guid-b]:" in text
        assert "[NO AUTHOR RESPONSE]" in text

    def test_concern_truncated_to_120(self):
        """Group concern is truncated to 120 characters in the header."""
        long_concern = "A" * 200
        group = make_review_group(group_id="grp_long", guid="guid-long", concern=long_concern)
        ar = make_author_response(group_id="grp_long")
        text = _format_author_responses_for_rebuttal([group], [ar])
        # The header line should contain at most 120 chars of the concern
        header_line = [l for l in text.split("\n") if "GROUP [guid-long]:" in l][0]
        # Extract the concern part after "GROUP [guid-long]: "
        concern_part = header_line.split(": ", 1)[1]
        assert len(concern_part) == 120


# ─── TestGetContestedGroupsForReviewer ────────────────────────────────────────


class TestGetContestedGroupsForReviewer:
    """Tests for _get_contested_groups_for_reviewer: filtering logic."""

    def test_accepted_excluded(self):
        """Groups where the author ACCEPTED are excluded."""
        group = make_review_group(
            group_id="grp_ok", source_reviewers=["alice"]
        )
        ar = make_author_response(group_id="grp_ok", resolution="ACCEPTED")
        result = _get_contested_groups_for_reviewer("alice", [group], [ar])
        assert len(result) == 0

    def test_rejected_included(self):
        """Groups where the author REJECTED are included."""
        group = make_review_group(
            group_id="grp_rej", source_reviewers=["alice"]
        )
        ar = make_author_response(group_id="grp_rej", resolution="REJECTED")
        result = _get_contested_groups_for_reviewer("alice", [group], [ar])
        assert len(result) == 1
        assert result[0].group_id == "grp_rej"

    def test_partial_included(self):
        """Groups where the author gave PARTIAL are included."""
        group = make_review_group(
            group_id="grp_part", source_reviewers=["alice"]
        )
        ar = make_author_response(group_id="grp_part", resolution="PARTIAL")
        result = _get_contested_groups_for_reviewer("alice", [group], [ar])
        assert len(result) == 1

    def test_no_response_included(self):
        """Groups with no author response are included."""
        group = make_review_group(
            group_id="grp_nr", source_reviewers=["alice"]
        )
        result = _get_contested_groups_for_reviewer("alice", [group], [])
        assert len(result) == 1

    def test_reviewer_not_source_excluded(self):
        """Groups where the reviewer was NOT a source reviewer are excluded."""
        group = make_review_group(
            group_id="grp_other", source_reviewers=["bob"]
        )
        ar = make_author_response(group_id="grp_other", resolution="REJECTED")
        result = _get_contested_groups_for_reviewer("alice", [group], [ar])
        assert len(result) == 0

    def test_mixed_scenario(self):
        """Complex scenario with multiple groups and different resolutions."""
        g1 = make_review_group(group_id="grp_1", source_reviewers=["alice", "bob"])
        g2 = make_review_group(group_id="grp_2", source_reviewers=["alice"])
        g3 = make_review_group(group_id="grp_3", source_reviewers=["bob"])
        g4 = make_review_group(group_id="grp_4", source_reviewers=["alice"])

        ar1 = make_author_response(group_id="grp_1", resolution="REJECTED")
        ar2 = make_author_response(group_id="grp_2", resolution="ACCEPTED")
        ar3 = make_author_response(group_id="grp_3", resolution="PARTIAL")
        # No response for grp_4

        result = _get_contested_groups_for_reviewer(
            "alice", [g1, g2, g3, g4], [ar1, ar2, ar3]
        )
        # g1: alice is source, REJECTED -> included
        # g2: alice is source, ACCEPTED -> excluded
        # g3: alice is NOT source -> excluded
        # g4: alice is source, no response -> included
        assert len(result) == 2
        group_ids = {g.group_id for g in result}
        assert group_ids == {"grp_1", "grp_4"}

    def test_unrecognized_resolution_included(self):
        """Unrecognized resolution strings (not ACCEPTED) are treated as contested."""
        group = make_review_group(
            group_id="grp_weird", source_reviewers=["alice"]
        )
        ar = make_author_response(group_id="grp_weird", resolution="MAYBE")
        result = _get_contested_groups_for_reviewer("alice", [group], [ar])
        assert len(result) == 1

    def test_empty_groups(self):
        """Empty groups list returns empty result."""
        result = _get_contested_groups_for_reviewer("alice", [], [])
        assert result == []


# ─── TestFormatChallengedGroups ───────────────────────────────────────────────


class TestFormatChallengedGroups:
    """Tests for _format_challenged_groups: challenge-only filtering, multi-section formatting."""

    def test_only_challenged_groups_included(self):
        """Groups with only CONCUR rebuttals are excluded from output."""
        g1 = make_review_group(group_id="grp_challenged", guid="guid-c", concern="Challenged concern")
        g2 = make_review_group(group_id="grp_concurred", guid="guid-ok", concern="Concurred concern")

        ar1 = make_author_response(group_id="grp_challenged")
        ar2 = make_author_response(group_id="grp_concurred")

        rb_challenge = make_rebuttal(group_id="grp_challenged", verdict="CHALLENGE", rationale="I disagree")
        rb_concur = make_rebuttal(group_id="grp_concurred", verdict="CONCUR", rationale="I agree")

        text = _format_challenged_groups(
            [g1, g2], [ar1, ar2], [rb_challenge, rb_concur]
        )
        assert "guid-c" in text
        assert "guid-ok" not in text

    def test_challenge_section_structure(self):
        """Challenged group section has the expected subsections."""
        p = make_review_point(reviewer="alice", description="Finding desc", recommendation="Fix it")
        group = make_review_group(
            group_id="grp_1", guid="guid-1", concern="Test concern",
            points=[p], source_reviewers=["alice"],
        )
        ar = make_author_response(
            group_id="grp_1", resolution="REJECTED", rationale="Not a real issue"
        )
        rb = make_rebuttal(
            group_id="grp_1", reviewer="alice", verdict="CHALLENGE",
            rationale="Yes it is a real issue"
        )
        text = _format_challenged_groups([group], [ar], [rb])

        assert "GROUP [guid-1]: Test concern" in text
        assert "ORIGINAL REVIEWER FINDINGS:" in text
        assert "alice: Finding desc" in text
        assert "Recommendation: Fix it" in text
        assert "YOUR ROUND 1 RESPONSE: REJECTED" in text
        assert "Not a real issue" in text
        assert "REVIEWER CHALLENGES:" in text
        assert "alice: Yes it is a real issue" in text
        assert "---" in text

    def test_no_author_response_fallback(self):
        """Missing author response shows '[none]' in the challenge section."""
        group = make_review_group(group_id="grp_no_ar", guid="guid-no-ar")
        rb = make_rebuttal(group_id="grp_no_ar", verdict="CHALLENGE")
        text = _format_challenged_groups([group], [], [rb])
        assert "YOUR ROUND 1 RESPONSE: [none]" in text

    def test_multiple_challenges_same_group(self):
        """Multiple CHALLENGEs for the same group are all listed."""
        group = make_review_group(group_id="grp_multi", guid="guid-multi")
        ar = make_author_response(group_id="grp_multi")
        rb1 = make_rebuttal(
            group_id="grp_multi", reviewer="r1", verdict="CHALLENGE",
            rationale="Challenge from r1"
        )
        rb2 = make_rebuttal(
            group_id="grp_multi", reviewer="r2", verdict="CHALLENGE",
            rationale="Challenge from r2"
        )
        text = _format_challenged_groups([group], [ar], [rb1, rb2])
        assert "r1: Challenge from r1" in text
        assert "r2: Challenge from r2" in text

    def test_mixed_verdicts_only_challenges_shown(self):
        """When a group has both CONCUR and CHALLENGE, only challenges appear in REVIEWER CHALLENGES."""
        group = make_review_group(group_id="grp_mix", guid="guid-mix")
        ar = make_author_response(group_id="grp_mix")
        rb_concur = make_rebuttal(
            group_id="grp_mix", reviewer="r1", verdict="CONCUR",
            rationale="I concur"
        )
        rb_challenge = make_rebuttal(
            group_id="grp_mix", reviewer="r2", verdict="CHALLENGE",
            rationale="I challenge"
        )
        text = _format_challenged_groups([group], [ar], [rb_concur, rb_challenge])
        # The group IS included because there is at least one CHALLENGE
        assert "guid-mix" in text
        # Under REVIEWER CHALLENGES, only the challenge rationale should appear
        challenges_section = text.split("REVIEWER CHALLENGES:")[1]
        assert "r2: I challenge" in challenges_section
        assert "r1: I concur" not in challenges_section

    def test_no_challenges_produces_empty(self):
        """When no groups have CHALLENGEs, output is empty."""
        group = make_review_group(group_id="grp_ok", guid="guid-ok")
        ar = make_author_response(group_id="grp_ok")
        rb = make_rebuttal(group_id="grp_ok", verdict="CONCUR")
        text = _format_challenged_groups([group], [ar], [rb])
        assert text.strip() == ""

    def test_no_rebuttals_produces_empty(self):
        """When there are no rebuttals at all, output is empty."""
        group = make_review_group(group_id="grp_no_rb", guid="guid-no-rb")
        ar = make_author_response(group_id="grp_no_rb")
        text = _format_challenged_groups([group], [ar], [])
        assert text.strip() == ""

    def test_recommendation_shown_in_findings(self):
        """Points with recommendations show them in the original findings section."""
        p = make_review_point(reviewer="r1", description="Issue", recommendation="Fix this way")
        group = make_review_group(group_id="grp_rec", guid="guid-rec", points=[p])
        ar = make_author_response(group_id="grp_rec")
        rb = make_rebuttal(group_id="grp_rec", verdict="CHALLENGE")
        text = _format_challenged_groups([group], [ar], [rb])
        assert "Recommendation: Fix this way" in text

    def test_point_without_recommendation(self):
        """Points without recommendations skip the recommendation line."""
        p = make_review_point(reviewer="r1", description="Issue", recommendation="")
        group = make_review_group(group_id="grp_norec", guid="guid-norec", points=[p])
        ar = make_author_response(group_id="grp_norec")
        rb = make_rebuttal(group_id="grp_norec", verdict="CHALLENGE")
        text = _format_challenged_groups([group], [ar], [rb])
        assert "r1: Issue" in text
        assert "Recommendation:" not in text


# ─── TestGroupToDict ──────────────────────────────────────────────────────────


class TestGroupToDict:
    """Tests for _group_to_dict: serialization with optional guid field."""

    def test_required_fields_present(self):
        """Dict contains all required fields."""
        group = make_review_group()
        d = _group_to_dict(group)
        assert "group_id" in d
        assert "concern" in d
        assert "points" in d
        assert "combined_severity" in d
        assert "combined_category" in d
        assert "source_reviewers" in d

    def test_guid_included_when_set(self):
        """GUID is included in the dict when it has a value."""
        group = make_review_group(guid="my-guid-123")
        d = _group_to_dict(group)
        assert d["guid"] == "my-guid-123"

    def test_guid_excluded_when_empty(self):
        """GUID is NOT included in the dict when it is empty."""
        group = make_review_group(guid="")
        d = _group_to_dict(group)
        assert "guid" not in d

    def test_points_serialized_as_dicts(self):
        """Points are serialized as list of plain dicts."""
        p = make_review_point(
            point_id="pt_1", reviewer="alice",
            severity="high", category="security",
            description="Some desc", recommendation="Fix it",
            location="foo.py",
        )
        group = make_review_group(points=[p])
        d = _group_to_dict(group)
        assert len(d["points"]) == 1
        pt = d["points"][0]
        assert isinstance(pt, dict)
        assert pt["point_id"] == "pt_1"
        assert pt["reviewer"] == "alice"
        assert pt["severity"] == "high"
        assert pt["category"] == "security"
        assert pt["description"] == "Some desc"
        assert pt["recommendation"] == "Fix it"
        assert pt["location"] == "foo.py"

    def test_multiple_points(self):
        """Multiple points are all serialized."""
        p1 = make_review_point(point_id="pt_1")
        p2 = make_review_point(point_id="pt_2")
        group = make_review_group(points=[p1, p2])
        d = _group_to_dict(group)
        assert len(d["points"]) == 2

    def test_field_values_match_group(self):
        """Dict field values match the original ReviewGroup fields."""
        group = make_review_group(
            group_id="grp_abc",
            concern="Test concern text",
            combined_severity="critical",
            combined_category="architecture",
            source_reviewers=["r1", "r2"],
            guid="test-guid",
        )
        d = _group_to_dict(group)
        assert d["group_id"] == "grp_abc"
        assert d["concern"] == "Test concern text"
        assert d["combined_severity"] == "critical"
        assert d["combined_category"] == "architecture"
        assert d["source_reviewers"] == ["r1", "r2"]
        assert d["guid"] == "test-guid"


# ─── TestComputeSummary ───────────────────────────────────────────────────────


class TestComputeSummary:
    """Tests for _compute_summary: governance decision counting."""

    def test_basic_counts(self):
        """Summary counts governance decisions by resolution type."""
        g1 = make_review_group(group_id="grp_1")
        g2 = make_review_group(group_id="grp_2")
        g3 = make_review_group(group_id="grp_3")

        decisions = [
            GovernanceDecision("grp_1", "ACCEPTED", Resolution.AUTO_ACCEPTED.value, "ok"),
            GovernanceDecision("grp_2", "ACCEPTED", Resolution.AUTO_ACCEPTED.value, "ok"),
            GovernanceDecision("grp_3", "REJECTED", Resolution.ESCALATED.value, "needs review"),
        ]
        summary = _compute_summary(decisions, [g1, g2, g3])
        assert summary["total_groups"] == 3
        assert summary["total_points"] == 3
        assert summary[Resolution.AUTO_ACCEPTED.value] == 2
        assert summary[Resolution.ESCALATED.value] == 1

    def test_total_points_counts_all_group_points(self):
        """Total points is the sum of points across all groups."""
        p1 = make_review_point(point_id="pt_1")
        p2 = make_review_point(point_id="pt_2")
        p3 = make_review_point(point_id="pt_3")
        g1 = make_review_group(group_id="grp_1", points=[p1, p2])
        g2 = make_review_group(group_id="grp_2", points=[p3])

        decisions = [
            GovernanceDecision("grp_1", "ACCEPTED", Resolution.AUTO_ACCEPTED.value, "ok"),
            GovernanceDecision("grp_2", "ACCEPTED", Resolution.AUTO_ACCEPTED.value, "ok"),
        ]
        summary = _compute_summary(decisions, [g1, g2])
        assert summary["total_points"] == 3
        assert summary["total_groups"] == 2

    def test_empty_decisions(self):
        """No decisions -> only total_groups and total_points in summary."""
        g = make_review_group()
        summary = _compute_summary([], [g])
        assert summary["total_groups"] == 1
        assert summary["total_points"] == 1
        # No resolution keys
        assert Resolution.AUTO_ACCEPTED.value not in summary
        assert Resolution.ESCALATED.value not in summary

    def test_empty_groups(self):
        """No groups -> totals are zero."""
        summary = _compute_summary([], [])
        assert summary["total_groups"] == 0
        assert summary["total_points"] == 0

    def test_all_resolution_types(self):
        """Each distinct resolution type gets its own count."""
        groups = [make_review_group(group_id=f"grp_{i}") for i in range(5)]
        decisions = [
            GovernanceDecision("grp_0", "ACCEPTED", Resolution.AUTO_ACCEPTED.value, "ok"),
            GovernanceDecision("grp_1", "REJECTED", Resolution.AUTO_DISMISSED.value, "dismissed"),
            GovernanceDecision("grp_2", "PARTIAL", Resolution.ESCALATED.value, "escalated"),
            GovernanceDecision("grp_3", "ACCEPTED", Resolution.ACCEPTED.value, "human accepted"),
            GovernanceDecision("grp_4", "REJECTED", Resolution.REJECTED.value, "human rejected"),
        ]
        summary = _compute_summary(decisions, groups)
        assert summary[Resolution.AUTO_ACCEPTED.value] == 1
        assert summary[Resolution.AUTO_DISMISSED.value] == 1
        assert summary[Resolution.ESCALATED.value] == 1
        assert summary[Resolution.ACCEPTED.value] == 1
        assert summary[Resolution.REJECTED.value] == 1
        assert summary["total_groups"] == 5

    def test_duplicate_resolution_types_accumulated(self):
        """Multiple decisions with the same resolution accumulate correctly."""
        groups = [make_review_group(group_id=f"grp_{i}") for i in range(4)]
        decisions = [
            GovernanceDecision("grp_0", "ACCEPTED", Resolution.AUTO_ACCEPTED.value, "ok"),
            GovernanceDecision("grp_1", "ACCEPTED", Resolution.AUTO_ACCEPTED.value, "ok"),
            GovernanceDecision("grp_2", "ACCEPTED", Resolution.AUTO_ACCEPTED.value, "ok"),
            GovernanceDecision("grp_3", "REJECTED", Resolution.ESCALATED.value, "needs review"),
        ]
        summary = _compute_summary(decisions, groups)
        assert summary[Resolution.AUTO_ACCEPTED.value] == 3
        assert summary[Resolution.ESCALATED.value] == 1
