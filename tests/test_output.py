"""Tests for devils_advocate.output module — report and ledger generation."""

from __future__ import annotations

import pytest

from devils_advocate.output import (
    _build_lookup_maps,
    _format_group_section,
    _generate_spec_report,
    generate_ledger,
    generate_report,
)
from devils_advocate.types import (
    AuthorFinalResponse,
    AuthorResponse,
    CostTracker,
    GovernanceDecision,
    RebuttalResponse,
    Resolution,
    ReviewGroup,
    ReviewPoint,
    ReviewResult,
)

from conftest import (
    make_author_final,
    make_author_response,
    make_rebuttal,
    make_review_group,
    make_review_point,
)


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _make_cost_tracker(**kwargs) -> CostTracker:
    """Build a CostTracker with some default cost entries."""
    ct = CostTracker()
    ct.add("model-alpha", 1000, 500, 0.01, 0.02, role="reviewer")
    ct.add("model-beta", 800, 400, 0.005, 0.01, role="author")
    return ct


def _make_review_result(
    mode: str = "plan",
    groups: list | None = None,
    author_responses: list | None = None,
    governance_decisions: list | None = None,
    rebuttals: list | None = None,
    author_final_responses: list | None = None,
    cost: CostTracker | None = None,
    revised_output: str = "",
    summary: dict | None = None,
    reviewer_models: list | None = None,
) -> ReviewResult:
    """Build a ReviewResult with sensible defaults for testing."""
    if groups is None:
        groups = [make_review_group()]
    if author_responses is None:
        author_responses = [make_author_response(group_id=g.group_id) for g in groups]
    if governance_decisions is None:
        governance_decisions = [
            GovernanceDecision(
                group_id=g.group_id,
                author_resolution="ACCEPTED",
                governance_resolution=Resolution.AUTO_ACCEPTED.value,
                reason="Substantive acceptance rationale",
            )
            for g in groups
        ]
    if cost is None:
        cost = _make_cost_tracker()
    if summary is None:
        summary = {"accepted": 1, "total_groups": len(groups)}

    return ReviewResult(
        review_id="test_review_001",
        mode=mode,
        input_file="plan.md",
        project="test-project",
        timestamp="2026-02-14T18:26:00Z",
        author_model="model-alpha",
        reviewer_models=reviewer_models or ["model-beta"],
        dedup_model="model-gamma",
        points=[],
        groups=groups,
        author_responses=author_responses,
        governance_decisions=governance_decisions,
        rebuttals=rebuttals or [],
        author_final_responses=author_final_responses or [],
        cost=cost,
        revised_output=revised_output,
        summary=summary,
    )


# ─── TestBuildLookupMaps ──────────────────────────────────────────────────────


class TestBuildLookupMaps:
    """Tests for _build_lookup_maps: creates decision/response/rebuttal/final lookup dicts."""

    def test_all_maps_populated(self):
        """All four maps contain entries keyed by group_id."""
        g1 = make_review_group(group_id="grp_a")
        g2 = make_review_group(group_id="grp_b")
        ar1 = make_author_response(group_id="grp_a")
        ar2 = make_author_response(group_id="grp_b")
        rb1 = make_rebuttal(group_id="grp_a", reviewer="r1")
        rb2 = make_rebuttal(group_id="grp_a", reviewer="r2")
        rb3 = make_rebuttal(group_id="grp_b", reviewer="r1")
        af1 = make_author_final(group_id="grp_a")
        dec1 = GovernanceDecision("grp_a", "ACCEPTED", Resolution.AUTO_ACCEPTED.value, "ok")
        dec2 = GovernanceDecision("grp_b", "REJECTED", Resolution.ESCALATED.value, "needs review")

        result = _make_review_result(
            groups=[g1, g2],
            author_responses=[ar1, ar2],
            governance_decisions=[dec1, dec2],
            rebuttals=[rb1, rb2, rb3],
            author_final_responses=[af1],
        )

        decision_map, response_map, rebuttal_map, final_map = _build_lookup_maps(result)

        assert set(decision_map.keys()) == {"grp_a", "grp_b"}
        assert set(response_map.keys()) == {"grp_a", "grp_b"}
        assert set(rebuttal_map.keys()) == {"grp_a", "grp_b"}
        assert len(rebuttal_map["grp_a"]) == 2
        assert len(rebuttal_map["grp_b"]) == 1
        assert "grp_a" in final_map
        assert "grp_b" not in final_map

    def test_empty_result(self):
        """Empty lists produce empty maps."""
        result = _make_review_result(
            groups=[],
            author_responses=[],
            governance_decisions=[],
            rebuttals=[],
            author_final_responses=[],
        )
        decision_map, response_map, rebuttal_map, final_map = _build_lookup_maps(result)

        assert decision_map == {}
        assert response_map == {}
        assert rebuttal_map == {}
        assert final_map == {}

    def test_rebuttal_map_groups_by_group_id(self):
        """Multiple rebuttals for the same group are grouped in a list."""
        rb1 = make_rebuttal(group_id="grp_x", reviewer="r1", verdict="CONCUR")
        rb2 = make_rebuttal(group_id="grp_x", reviewer="r2", verdict="CHALLENGE")
        result = _make_review_result(
            rebuttals=[rb1, rb2],
        )
        _, _, rebuttal_map, _ = _build_lookup_maps(result)
        assert len(rebuttal_map["grp_x"]) == 2
        verdicts = {rb.verdict for rb in rebuttal_map["grp_x"]}
        assert verdicts == {"CONCUR", "CHALLENGE"}

    def test_decision_map_last_write_wins(self):
        """If duplicate group_ids exist in decisions, the last one wins (dict behavior)."""
        dec1 = GovernanceDecision("grp_a", "ACCEPTED", Resolution.AUTO_ACCEPTED.value, "first")
        dec2 = GovernanceDecision("grp_a", "REJECTED", Resolution.ESCALATED.value, "second")
        result = _make_review_result(governance_decisions=[dec1, dec2])
        decision_map, _, _, _ = _build_lookup_maps(result)
        assert decision_map["grp_a"].reason == "second"


# ─── TestGenerateReport ───────────────────────────────────────────────────────


class TestGenerateReport:
    """Tests for generate_report: full report generation."""

    def test_mode_dispatch_spec(self):
        """Spec mode dispatches to _generate_spec_report."""
        result = _make_review_result(mode="spec")
        report = generate_report(result)
        assert "Specification Enrichment Report" in report
        assert "Devil's Advocate Review Report" not in report

    def test_mode_dispatch_plan(self):
        """Plan mode generates the standard report."""
        result = _make_review_result(mode="plan")
        report = generate_report(result)
        assert "Devil's Advocate Review Report" in report
        assert "**Mode:** Plan Review" in report

    def test_header_metadata(self):
        """Report header includes all metadata fields."""
        result = _make_review_result()
        report = generate_report(result)
        assert "**Input:** `plan.md`" in report
        assert "**Project:** test-project" in report
        assert "**Date:** 2026-02-14T18:26:00Z" in report
        assert "**Review ID:** `test_review_001`" in report
        assert "**Author Model:** model-alpha" in report
        assert "model-beta" in report
        assert "**Dedup Model:** model-gamma" in report

    def test_summary_table_nonzero_only(self):
        """Summary table only includes rows with count > 0."""
        result = _make_review_result(
            summary={"accepted": 3, "rejected": 0, "escalated": 1, "total_groups": 4},
        )
        report = generate_report(result)
        assert "| Accepted | 3 |" in report
        assert "| Escalated | 1 |" in report
        assert "Rejected" not in report
        assert "| **Total** | **4** |" in report

    def test_summary_table_auto_accepted(self):
        """Summary table formats auto_accepted as 'Auto Accepted'."""
        result = _make_review_result(
            summary={"auto_accepted": 5, "total_groups": 5},
        )
        report = generate_report(result)
        assert "| Auto Accepted | 5 |" in report

    def test_escalated_section_present(self):
        """Escalated items get their own section header."""
        group = make_review_group(group_id="grp_esc")
        dec = GovernanceDecision(
            "grp_esc", "PARTIAL", Resolution.ESCALATED.value, "Needs human review"
        )
        ar = make_author_response(group_id="grp_esc", resolution="PARTIAL")
        result = _make_review_result(
            groups=[group],
            author_responses=[ar],
            governance_decisions=[dec],
        )
        report = generate_report(result)
        assert "## Escalated Items (Require Human Decision)" in report

    def test_non_escalated_section(self):
        """Non-escalated items appear under 'Review Points'."""
        group = make_review_group(group_id="grp_ok")
        dec = GovernanceDecision(
            "grp_ok", "ACCEPTED", Resolution.AUTO_ACCEPTED.value, "Accepted"
        )
        ar = make_author_response(group_id="grp_ok")
        result = _make_review_result(
            groups=[group],
            author_responses=[ar],
            governance_decisions=[dec],
        )
        report = generate_report(result)
        assert "## Review Points" in report
        assert "Escalated Items" not in report

    def test_mixed_escalated_and_non_escalated(self):
        """Report includes both sections when both types exist."""
        g_esc = make_review_group(group_id="grp_esc")
        g_ok = make_review_group(group_id="grp_ok")
        dec_esc = GovernanceDecision(
            "grp_esc", "PARTIAL", Resolution.ESCALATED.value, "Escalated reason"
        )
        dec_ok = GovernanceDecision(
            "grp_ok", "ACCEPTED", Resolution.AUTO_ACCEPTED.value, "Ok reason"
        )
        ar_esc = make_author_response(group_id="grp_esc", resolution="PARTIAL")
        ar_ok = make_author_response(group_id="grp_ok")
        result = _make_review_result(
            groups=[g_esc, g_ok],
            author_responses=[ar_esc, ar_ok],
            governance_decisions=[dec_esc, dec_ok],
        )
        report = generate_report(result)
        assert "## Escalated Items (Require Human Decision)" in report
        assert "## Review Points" in report

    def test_revised_output_plan_label(self):
        """Plan mode uses 'Revised Plan' label for revised output."""
        result = _make_review_result(mode="plan", revised_output="new plan content")
        report = generate_report(result)
        assert "## Revised Plan" in report
        assert "new plan content" in report

    def test_revised_output_integration_label(self):
        """Integration mode uses 'Remediation Plan' label."""
        result = _make_review_result(mode="integration", revised_output="remediation steps")
        report = generate_report(result)
        assert "## Remediation Plan" in report
        assert "remediation steps" in report

    def test_revised_output_code_label(self):
        """Code mode (or any other mode) uses 'Unified Diff' label."""
        result = _make_review_result(mode="code", revised_output="diff content")
        report = generate_report(result)
        assert "## Unified Diff" in report
        assert "diff content" in report

    def test_revised_output_absent(self):
        """No revised output section when revised_output is empty."""
        result = _make_review_result(revised_output="")
        report = generate_report(result)
        assert "## Revised Plan" not in report
        assert "## Remediation Plan" not in report
        assert "## Unified Diff" not in report

    def test_cost_breakdown_section(self):
        """Cost breakdown table appears with model costs."""
        result = _make_review_result()
        report = generate_report(result)
        assert "## Cost Breakdown" in report
        assert "| Model | Cost (USD) |" in report
        assert "model-alpha" in report
        assert "model-beta" in report
        assert "**Total**" in report


# ─── TestFormatGroupSection ───────────────────────────────────────────────────


class TestFormatGroupSection:
    """Tests for _format_group_section: individual group formatting."""

    def _build_maps(
        self,
        group_id="grp_001",
        resolution="auto_accepted",
        reason="Good rationale",
        author_resolution="ACCEPTED",
        author_rationale="I agree with the detailed technical analysis",
        rebuttals=None,
        author_final=None,
    ):
        """Build the lookup maps needed by _format_group_section."""
        decision_map = {
            group_id: GovernanceDecision(
                group_id, author_resolution, resolution, reason
            )
        }
        response_map = {}
        if author_resolution is not None:
            response_map[group_id] = AuthorResponse(
                group_id=group_id,
                resolution=author_resolution,
                rationale=author_rationale,
            )

        rebuttal_map = {}
        if rebuttals:
            for rb in rebuttals:
                rebuttal_map.setdefault(rb.group_id, []).append(rb)

        final_map = {}
        if author_final:
            final_map[author_final.group_id] = author_final

        return decision_map, response_map, rebuttal_map, final_map

    def test_header_includes_group_id_and_concern(self):
        """Section header shows group_id and concern."""
        group = make_review_group(group_id="grp_test", concern="Missing null check")
        decision_map, response_map, rebuttal_map, final_map = self._build_maps(
            group_id="grp_test"
        )
        lines = _format_group_section(
            group, decision_map, response_map,
            rebuttal_map=rebuttal_map, final_response_map=final_map,
        )
        text = "\n".join(lines)
        assert "### grp_test: Missing null check" in text

    def test_reviewer_count_singular(self):
        """Single reviewer shows '1 reviewer' without plural."""
        group = make_review_group(source_reviewers=["r1"])
        decision_map, response_map, rebuttal_map, final_map = self._build_maps()
        lines = _format_group_section(
            group, decision_map, response_map,
            rebuttal_map=rebuttal_map, final_response_map=final_map,
        )
        text = "\n".join(lines)
        assert "(1 reviewer)" in text
        assert "(1 reviewers)" not in text

    def test_reviewer_count_plural(self):
        """Multiple reviewers show 'N reviewers' with plural."""
        group = make_review_group(source_reviewers=["r1", "r2", "r3"])
        decision_map, response_map, rebuttal_map, final_map = self._build_maps()
        lines = _format_group_section(
            group, decision_map, response_map,
            rebuttal_map=rebuttal_map, final_response_map=final_map,
        )
        text = "\n".join(lines)
        assert "(3 reviewers)" in text

    def test_author_response_present(self):
        """Author response section shows resolution and rationale."""
        group = make_review_group()
        decision_map, response_map, rebuttal_map, final_map = self._build_maps(
            author_resolution="ACCEPTED",
            author_rationale="Detailed technical reasoning here",
        )
        lines = _format_group_section(
            group, decision_map, response_map,
            rebuttal_map=rebuttal_map, final_response_map=final_map,
        )
        text = "\n".join(lines)
        assert "**Author Response (Round 1):**" in text
        assert "**Resolution:** ACCEPTED" in text
        assert "> Detailed technical reasoning here" in text

    def test_author_response_no_rationale(self):
        """Author response with empty rationale shows fallback message."""
        group = make_review_group()
        decision_map, response_map, rebuttal_map, final_map = self._build_maps(
            author_rationale="",
        )
        lines = _format_group_section(
            group, decision_map, response_map,
            rebuttal_map=rebuttal_map, final_response_map=final_map,
        )
        text = "\n".join(lines)
        assert "*(No rationale provided)*" in text

    def test_author_response_missing(self):
        """Missing author response shows '[No author response]' style fallback."""
        group = make_review_group(group_id="grp_missing")
        decision_map = {
            "grp_missing": GovernanceDecision(
                "grp_missing", "", Resolution.ESCALATED.value, "No response"
            )
        }
        response_map = {}  # No author response for this group
        lines = _format_group_section(
            group, decision_map, response_map,
            rebuttal_map={}, final_response_map={},
        )
        text = "\n".join(lines)
        assert "**Author did not respond to this group.**" in text

    def test_rebuttal_concur_icon(self):
        """CONCUR verdict shows '+' icon."""
        group = make_review_group()
        rb = make_rebuttal(verdict="CONCUR", reviewer="reviewer_x", rationale="Looks good")
        decision_map, response_map, rebuttal_map, final_map = self._build_maps(
            rebuttals=[rb],
        )
        lines = _format_group_section(
            group, decision_map, response_map,
            rebuttal_map=rebuttal_map, final_response_map=final_map,
        )
        text = "\n".join(lines)
        assert "- + **reviewer_x:** CONCUR" in text
        assert "**Reviewer Rebuttals (Round 2):**" in text

    def test_rebuttal_challenge_icon(self):
        """CHALLENGE verdict shows 'x' icon."""
        group = make_review_group()
        rb = make_rebuttal(verdict="CHALLENGE", reviewer="reviewer_y", rationale="Disagree")
        decision_map, response_map, rebuttal_map, final_map = self._build_maps(
            rebuttals=[rb],
        )
        lines = _format_group_section(
            group, decision_map, response_map,
            rebuttal_map=rebuttal_map, final_response_map=final_map,
        )
        text = "\n".join(lines)
        assert "- x **reviewer_y:** CHALLENGE" in text

    def test_rebuttal_rationale_shown(self):
        """Rebuttal rationale is shown as blockquote."""
        group = make_review_group()
        rb = make_rebuttal(verdict="CHALLENGE", rationale="Strong disagreement here")
        decision_map, response_map, rebuttal_map, final_map = self._build_maps(
            rebuttals=[rb],
        )
        lines = _format_group_section(
            group, decision_map, response_map,
            rebuttal_map=rebuttal_map, final_response_map=final_map,
        )
        text = "\n".join(lines)
        assert "  > Strong disagreement here" in text

    def test_no_rebuttals_section_absent(self):
        """No rebuttals means no Round 2 rebuttal section."""
        group = make_review_group()
        decision_map, response_map, rebuttal_map, final_map = self._build_maps()
        lines = _format_group_section(
            group, decision_map, response_map,
            rebuttal_map=rebuttal_map, final_response_map=final_map,
        )
        text = "\n".join(lines)
        assert "**Reviewer Rebuttals (Round 2):**" not in text

    def test_author_final_shown_when_challenges_exist(self):
        """Author final response shown when at least one CHALLENGE exists."""
        group = make_review_group()
        rb = make_rebuttal(verdict="CHALLENGE")
        af = make_author_final(rationale="Reconsidered and accepting this")
        decision_map, response_map, rebuttal_map, final_map = self._build_maps(
            rebuttals=[rb],
            author_final=af,
        )
        lines = _format_group_section(
            group, decision_map, response_map,
            rebuttal_map=rebuttal_map, final_response_map=final_map,
        )
        text = "\n".join(lines)
        assert "**Author Final Response (Round 2):**" in text
        assert "**Resolution:** ACCEPTED" in text
        assert "> Reconsidered and accepting this" in text

    def test_author_final_not_shown_without_challenges(self):
        """Author final response NOT shown when only CONCUR rebuttals exist."""
        group = make_review_group()
        rb = make_rebuttal(verdict="CONCUR")
        af = make_author_final()
        decision_map, response_map, rebuttal_map, final_map = self._build_maps(
            rebuttals=[rb],
            author_final=af,
        )
        lines = _format_group_section(
            group, decision_map, response_map,
            rebuttal_map=rebuttal_map, final_response_map=final_map,
        )
        text = "\n".join(lines)
        assert "**Author Final Response (Round 2):**" not in text

    def test_author_final_missing_with_challenge(self):
        """Missing author final response with challenge shows fallback."""
        group = make_review_group()
        rb = make_rebuttal(verdict="CHALLENGE")
        decision_map, response_map, rebuttal_map, final_map = self._build_maps(
            rebuttals=[rb],
            author_final=None,
        )
        lines = _format_group_section(
            group, decision_map, response_map,
            rebuttal_map=rebuttal_map, final_response_map=final_map,
        )
        text = "\n".join(lines)
        assert "**Author Final Response (Round 2):**" in text
        assert "**Author did not provide a final response to this challenge.**" in text

    def test_author_final_no_rationale(self):
        """Author final response with empty rationale shows fallback."""
        group = make_review_group()
        rb = make_rebuttal(verdict="CHALLENGE")
        af = make_author_final(rationale="")
        decision_map, response_map, rebuttal_map, final_map = self._build_maps(
            rebuttals=[rb],
            author_final=af,
        )
        lines = _format_group_section(
            group, decision_map, response_map,
            rebuttal_map=rebuttal_map, final_response_map=final_map,
        )
        text = "\n".join(lines)
        assert "*(No rationale provided)*" in text

    def test_governance_reason_displayed(self):
        """Governance reason is shown at the end of the section."""
        group = make_review_group()
        decision_map, response_map, rebuttal_map, final_map = self._build_maps(
            reason="Auto-accepted: substantive rationale",
        )
        lines = _format_group_section(
            group, decision_map, response_map,
            rebuttal_map=rebuttal_map, final_response_map=final_map,
        )
        text = "\n".join(lines)
        assert "**Governance:** Auto-accepted: substantive rationale" in text

    def test_section_ends_with_divider(self):
        """Each group section ends with a horizontal rule."""
        group = make_review_group()
        decision_map, response_map, rebuttal_map, final_map = self._build_maps()
        lines = _format_group_section(
            group, decision_map, response_map,
            rebuttal_map=rebuttal_map, final_response_map=final_map,
        )
        assert "---" in lines

    def test_severity_and_category_line(self):
        """Severity and category are shown on the same line."""
        group = make_review_group(
            combined_severity="high", combined_category="security"
        )
        decision_map, response_map, rebuttal_map, final_map = self._build_maps()
        lines = _format_group_section(
            group, decision_map, response_map,
            rebuttal_map=rebuttal_map, final_response_map=final_map,
        )
        text = "\n".join(lines)
        assert "**Severity:** High" in text
        assert "**Category:** Security" in text

    def test_reviewer_feedback_points(self):
        """Individual review points are listed with reviewer attribution."""
        p1 = make_review_point(reviewer="alice", description="Issue A", recommendation="Fix A")
        p2 = make_review_point(reviewer="bob", description="Issue B", recommendation="")
        group = make_review_group(points=[p1, p2], source_reviewers=["alice", "bob"])
        decision_map, response_map, rebuttal_map, final_map = self._build_maps()
        lines = _format_group_section(
            group, decision_map, response_map,
            rebuttal_map=rebuttal_map, final_response_map=final_map,
        )
        text = "\n".join(lines)
        assert "- **alice:** Issue A" in text
        assert "  - *Recommendation:* Fix A" in text
        assert "- **bob:** Issue B" in text


# ─── TestGenerateLedger ───────────────────────────────────────────────────────


class TestGenerateLedger:
    """Tests for generate_ledger: JSON ledger structure."""

    def test_required_top_level_keys(self):
        """Ledger has all required top-level keys."""
        result = _make_review_result()
        ledger = generate_ledger(result)
        required_keys = {
            "review_id", "mode", "input_file", "project", "timestamp",
            "author_model", "reviewer_models", "dedup_model",
            "points", "summary", "cost",
        }
        assert required_keys.issubset(set(ledger.keys()))

    def test_point_structure(self):
        """Each point in the ledger has the expected fields."""
        result = _make_review_result()
        ledger = generate_ledger(result)
        assert len(ledger["points"]) > 0
        point = ledger["points"][0]
        expected_fields = {
            "point_id", "group_id", "severity", "category", "description",
            "recommendation", "location", "reviewer", "source_reviewers",
            "author_resolution", "author_rationale", "rebuttals",
            "author_final_resolution", "author_final_rationale",
            "governance_resolution", "governance_reason", "final_resolution",
            "overrides",
        }
        assert expected_fields.issubset(set(point.keys()))

    def test_point_aggregation_across_groups(self):
        """Points from multiple groups are all included in the ledger."""
        p1 = make_review_point(point_id="pt_1", reviewer="r1")
        p2 = make_review_point(point_id="pt_2", reviewer="r2")
        p3 = make_review_point(point_id="pt_3", reviewer="r3")
        g1 = make_review_group(group_id="grp_1", points=[p1], source_reviewers=["r1"])
        g2 = make_review_group(group_id="grp_2", points=[p2, p3], source_reviewers=["r2", "r3"])
        result = _make_review_result(
            groups=[g1, g2],
            author_responses=[
                make_author_response(group_id="grp_1"),
                make_author_response(group_id="grp_2"),
            ],
            governance_decisions=[
                GovernanceDecision("grp_1", "ACCEPTED", Resolution.AUTO_ACCEPTED.value, "ok"),
                GovernanceDecision("grp_2", "ACCEPTED", Resolution.AUTO_ACCEPTED.value, "ok"),
            ],
        )
        ledger = generate_ledger(result)
        assert len(ledger["points"]) == 3

    def test_author_response_no_response_fallback(self):
        """Points without author response show 'no_response' and empty rationale."""
        group = make_review_group(group_id="grp_orphan")
        result = _make_review_result(
            groups=[group],
            author_responses=[],  # No author response
            governance_decisions=[
                GovernanceDecision("grp_orphan", "", "pending", "")
            ],
        )
        ledger = generate_ledger(result)
        point = ledger["points"][0]
        assert point["author_resolution"] == "no_response"
        assert point["author_rationale"] == ""

    def test_author_final_none_when_absent(self):
        """Author final fields are None when no final response exists."""
        result = _make_review_result(author_final_responses=[])
        ledger = generate_ledger(result)
        point = ledger["points"][0]
        assert point["author_final_resolution"] is None
        assert point["author_final_rationale"] is None

    def test_author_final_present(self):
        """Author final fields populated when a final response exists."""
        group = make_review_group(group_id="grp_final")
        af = make_author_final(
            group_id="grp_final",
            resolution="ACCEPTED",
            rationale="Reconsidered the position",
        )
        result = _make_review_result(
            groups=[group],
            author_responses=[make_author_response(group_id="grp_final")],
            governance_decisions=[
                GovernanceDecision("grp_final", "ACCEPTED", Resolution.AUTO_ACCEPTED.value, "ok")
            ],
            author_final_responses=[af],
        )
        ledger = generate_ledger(result)
        point = ledger["points"][0]
        assert point["author_final_resolution"] == "ACCEPTED"
        assert point["author_final_rationale"] == "Reconsidered the position"

    def test_rebuttals_serialized(self):
        """Rebuttals are serialized as list of dicts."""
        group = make_review_group(group_id="grp_rb")
        rb = make_rebuttal(group_id="grp_rb", reviewer="r1", verdict="CHALLENGE")
        result = _make_review_result(
            groups=[group],
            author_responses=[make_author_response(group_id="grp_rb")],
            governance_decisions=[
                GovernanceDecision("grp_rb", "ACCEPTED", Resolution.ESCALATED.value, "challenged")
            ],
            rebuttals=[rb],
        )
        ledger = generate_ledger(result)
        point = ledger["points"][0]
        assert len(point["rebuttals"]) == 1
        assert point["rebuttals"][0]["verdict"] == "CHALLENGE"
        assert point["rebuttals"][0]["reviewer"] == "r1"

    def test_governance_aggregation_in_summary(self):
        """Summary includes governance decision counts."""
        g1 = make_review_group(group_id="grp_1")
        g2 = make_review_group(group_id="grp_2")
        g3 = make_review_group(group_id="grp_3")
        dec1 = GovernanceDecision("grp_1", "ACCEPTED", Resolution.AUTO_ACCEPTED.value, "ok")
        dec2 = GovernanceDecision("grp_2", "ACCEPTED", Resolution.AUTO_ACCEPTED.value, "ok")
        dec3 = GovernanceDecision("grp_3", "REJECTED", Resolution.ESCALATED.value, "needs review")
        result = _make_review_result(
            groups=[g1, g2, g3],
            author_responses=[
                make_author_response(group_id="grp_1"),
                make_author_response(group_id="grp_2"),
                make_author_response(group_id="grp_3", resolution="REJECTED"),
            ],
            governance_decisions=[dec1, dec2, dec3],
        )
        ledger = generate_ledger(result)
        summary = ledger["summary"]
        assert summary["total_groups"] == 3
        assert summary["total_points"] == 3
        assert summary[Resolution.AUTO_ACCEPTED.value] == 2
        assert summary[Resolution.ESCALATED.value] == 1

    def test_cost_breakdown_with_role_costs(self):
        """Cost section includes total, breakdown, and role_costs."""
        cost = _make_cost_tracker()
        result = _make_review_result(cost=cost)
        ledger = generate_ledger(result)
        cost_section = ledger["cost"]
        assert "total_usd" in cost_section
        assert "breakdown" in cost_section
        assert "role_costs" in cost_section
        assert cost_section["total_usd"] > 0
        assert "model-alpha" in cost_section["breakdown"]
        assert "reviewer" in cost_section["role_costs"]
        assert "author" in cost_section["role_costs"]

    def test_cost_values_rounded(self):
        """Cost values are rounded to 6 decimal places."""
        result = _make_review_result()
        ledger = generate_ledger(result)
        total_str = str(ledger["cost"]["total_usd"])
        # Ensure rounding to at most 6 decimal places
        if "." in total_str:
            decimal_places = len(total_str.split(".")[1])
            assert decimal_places <= 6

    def test_overrides_field_empty(self):
        """The overrides field is always an empty list."""
        result = _make_review_result()
        ledger = generate_ledger(result)
        for point in ledger["points"]:
            assert point["overrides"] == []

    def test_metadata_matches_result(self):
        """Ledger metadata fields match the ReviewResult input."""
        result = _make_review_result()
        ledger = generate_ledger(result)
        assert ledger["review_id"] == "test_review_001"
        assert ledger["mode"] == "plan"
        assert ledger["input_file"] == "plan.md"
        assert ledger["project"] == "test-project"
        assert ledger["author_model"] == "model-alpha"
        assert ledger["reviewer_models"] == ["model-beta"]
        assert ledger["dedup_model"] == "model-gamma"


# ─── TestGenerateSpecReport ───────────────────────────────────────────────────


class TestGenerateSpecReport:
    """Tests for _generate_spec_report: spec-specific report generation."""

    def _make_spec_result(
        self,
        groups=None,
        reviewer_models=None,
        summary=None,
        revised_output="",
    ):
        """Build a spec-mode ReviewResult."""
        if reviewer_models is None:
            reviewer_models = ["model-a", "model-b", "model-c"]
        if groups is None:
            groups = []
        if summary is None:
            summary = {
                "total_points": sum(len(g.points) for g in groups),
                "total_groups": len(groups),
                "multi_consensus": 0,
                "single_source": 0,
            }
        return _make_review_result(
            mode="spec",
            groups=groups,
            author_responses=[],
            governance_decisions=[],
            reviewer_models=reviewer_models,
            summary=summary,
            revised_output=revised_output,
        )

    def test_spec_report_header(self):
        """Spec report has the correct title and mode label."""
        result = self._make_spec_result()
        report = _generate_spec_report(result)
        assert "# Specification Enrichment Report" in report
        assert "**Mode:** Spec Review (Collaborative Ideation)" in report

    def test_spec_summary_section(self):
        """Spec report summary shows suggestion counts."""
        result = self._make_spec_result(
            summary={
                "total_points": 10,
                "total_groups": 5,
                "multi_consensus": 2,
                "single_source": 3,
            },
        )
        report = _generate_spec_report(result)
        assert "**Total Suggestions:** 10" in report
        assert "**Suggestion Groups:** 5" in report
        assert "**Multi-Reviewer Consensus:** 2" in report
        assert "**Single Source:** 3" in report

    def test_theme_grouping(self):
        """Groups are organized by theme (combined_category)."""
        g1 = make_review_group(
            group_id="grp_1", combined_category="security",
            concern="SQL injection risk",
            source_reviewers=["model-a"],
        )
        g2 = make_review_group(
            group_id="grp_2", combined_category="performance",
            concern="N+1 query",
            source_reviewers=["model-b"],
        )
        result = self._make_spec_result(groups=[g1, g2])
        report = _generate_spec_report(result)
        assert "## Security" in report
        assert "## Performance" in report

    def test_theme_alphabetical_sorting_other_last(self):
        """Themes are sorted alphabetically with 'Other' always last."""
        g_other = make_review_group(
            group_id="grp_other", combined_category="other",
            concern="Misc concern",
            source_reviewers=["model-a"],
        )
        g_arch = make_review_group(
            group_id="grp_arch", combined_category="architecture",
            concern="Arch concern",
            source_reviewers=["model-a"],
        )
        g_sec = make_review_group(
            group_id="grp_sec", combined_category="security",
            concern="Security concern",
            source_reviewers=["model-a"],
        )
        result = self._make_spec_result(groups=[g_other, g_arch, g_sec])
        report = _generate_spec_report(result)

        # Find positions of theme headers
        arch_pos = report.index("## Architecture")
        sec_pos = report.index("## Security")
        other_pos = report.index("## Other")

        assert arch_pos < sec_pos < other_pos

    def test_consensus_indicator_multi_source(self):
        """Multi-source groups show consensus indicator with reviewer count."""
        g = make_review_group(
            group_id="grp_multi", combined_category="security",
            concern="Shared finding",
            source_reviewers=["model-a", "model-b"],
        )
        result = self._make_spec_result(
            groups=[g],
            reviewer_models=["model-a", "model-b", "model-c"],
        )
        report = _generate_spec_report(result)
        assert "2/3 reviewers" in report

    def test_no_consensus_indicator_single_source(self):
        """Single-source groups do NOT show consensus indicator."""
        g = make_review_group(
            group_id="grp_single", combined_category="security",
            concern="Solo finding",
            source_reviewers=["model-a"],
        )
        result = self._make_spec_result(
            groups=[g],
            reviewer_models=["model-a", "model-b"],
        )
        report = _generate_spec_report(result)
        # The concern should appear but without the "N/M reviewers" indicator
        assert "Solo finding" in report
        assert "1/2 reviewers" not in report

    def test_high_consensus_section_present(self):
        """High-consensus section appears when multi-source groups exist."""
        g_multi = make_review_group(
            group_id="grp_m", combined_category="security",
            concern="Multi-reviewer finding",
            source_reviewers=["model-a", "model-b"],
        )
        g_single = make_review_group(
            group_id="grp_s", combined_category="security",
            concern="Single reviewer finding",
            source_reviewers=["model-a"],
        )
        result = self._make_spec_result(groups=[g_multi, g_single])
        report = _generate_spec_report(result)
        assert "## High-Consensus Ideas" in report
        assert "Multi-reviewer finding" in report
        assert "independently raised by multiple reviewers" in report

    def test_high_consensus_section_absent_when_all_single(self):
        """High-consensus section absent when no multi-source groups exist."""
        g = make_review_group(
            group_id="grp_s", combined_category="security",
            concern="Only one reviewer",
            source_reviewers=["model-a"],
        )
        result = self._make_spec_result(groups=[g])
        report = _generate_spec_report(result)
        assert "## High-Consensus Ideas" not in report

    def test_high_consensus_lists_reviewer_names(self):
        """High-consensus items list the specific reviewer names."""
        g = make_review_group(
            group_id="grp_hc", combined_category="testing",
            concern="Shared concern about testing",
            source_reviewers=["model-a", "model-c"],
        )
        result = self._make_spec_result(
            groups=[g],
            reviewer_models=["model-a", "model-b", "model-c"],
        )
        report = _generate_spec_report(result)
        assert "model-a, model-c" in report

    def test_point_location_shown_as_context(self):
        """Points with location show it as 'Context:' line."""
        p = make_review_point(reviewer="model-a", description="Test desc", location="spec section 3")
        g = make_review_group(
            group_id="grp_loc", combined_category="documentation",
            concern="Location concern",
            points=[p],
            source_reviewers=["model-a"],
        )
        result = self._make_spec_result(groups=[g])
        report = _generate_spec_report(result)
        assert "*Context:* spec section 3" in report

    def test_revised_output_compiled_report(self):
        """Revised output appears as 'Compiled Suggestion Report'."""
        result = self._make_spec_result(revised_output="Here are the compiled suggestions")
        report = _generate_spec_report(result)
        assert "## Compiled Suggestion Report" in report
        assert "Here are the compiled suggestions" in report

    def test_no_revised_output_no_section(self):
        """No compiled section when revised_output is empty."""
        result = self._make_spec_result(revised_output="")
        report = _generate_spec_report(result)
        assert "## Compiled Suggestion Report" not in report

    def test_spec_cost_breakdown(self):
        """Spec report includes cost breakdown table."""
        result = self._make_spec_result()
        report = _generate_spec_report(result)
        assert "## Cost Breakdown" in report
        assert "| Model | Cost (USD) |" in report

    def test_groups_sorted_by_consensus_within_theme(self):
        """Within a theme, groups are sorted by consensus count descending."""
        p1 = make_review_point(point_id="p1", reviewer="model-a")
        p2 = make_review_point(point_id="p2", reviewer="model-b")
        p3 = make_review_point(point_id="p3", reviewer="model-c")
        g_low = make_review_group(
            group_id="grp_low", combined_category="security",
            concern="Low consensus finding",
            points=[p1],
            source_reviewers=["model-a"],
        )
        g_high = make_review_group(
            group_id="grp_high", combined_category="security",
            concern="High consensus finding",
            points=[p2, p3],
            source_reviewers=["model-b", "model-c"],
        )
        result = self._make_spec_result(
            groups=[g_low, g_high],  # lower consensus first in input
            reviewer_models=["model-a", "model-b", "model-c"],
        )
        report = _generate_spec_report(result)

        # Higher consensus should appear first within the Security theme
        high_pos = report.index("High consensus finding")
        low_pos = report.index("Low consensus finding")
        assert high_pos < low_pos

    def test_underscore_category_formatted(self):
        """Categories with underscores are formatted to title case with spaces."""
        g = make_review_group(
            group_id="grp_eh", combined_category="error_handling",
            concern="Error handling issue",
            source_reviewers=["model-a"],
        )
        result = self._make_spec_result(groups=[g])
        report = _generate_spec_report(result)
        assert "## Error Handling" in report

    def test_no_author_model_in_spec_header(self):
        """Spec report does NOT include author model since there is no author phase."""
        result = self._make_spec_result()
        report = _generate_spec_report(result)
        assert "**Author Model:**" not in report
