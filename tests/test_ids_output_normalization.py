"""Tests for IDs module (generate_review_id, generate_new_group_id,
generate_new_point_id, assign_guids, resolve_guid), output module
(generate_report, generate_ledger), and normalization module
(normalize_review_response).
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helpers import (
    make_author_response,
    make_model_config,
    make_review_group,
    make_review_point,
    make_rebuttal,
    make_author_final,
)


# ═══════════════════════════════════════════════════════════════════════════
# 1. IDs module
# ═══════════════════════════════════════════════════════════════════════════


class TestRandomSuffix:
    def test_default_length(self):
        from devils_advocate.ids import _random_suffix
        s = _random_suffix()
        assert len(s) == 4
        assert all(c in "abcdefghijklmnopqrstuvwxyz0123456789" for c in s)

    def test_custom_length(self):
        from devils_advocate.ids import _random_suffix
        assert len(_random_suffix(8)) == 8

    def test_different_each_call(self):
        from devils_advocate.ids import _random_suffix
        results = {_random_suffix() for _ in range(100)}
        assert len(results) > 1  # Extremely unlikely all same


class TestFormatIdTimestamp:
    def test_specific_datetime(self):
        from devils_advocate.ids import _format_id_timestamp
        dt = datetime(2026, 2, 14, 18, 26, 0, tzinfo=timezone.utc)
        result = _format_id_timestamp(dt)
        assert result == "14FEB2026.1826"

    def test_uppercase(self):
        from devils_advocate.ids import _format_id_timestamp
        dt = datetime(2025, 12, 1, 9, 5, 0, tzinfo=timezone.utc)
        result = _format_id_timestamp(dt)
        assert result == "01DEC2025.0905"


class TestGenerateReviewId:
    def test_format(self):
        from devils_advocate.ids import generate_review_id
        rid = generate_review_id("test content")
        # Format: YYYYMMDDThhmmss_<sha256-6>
        assert re.match(r"\d{8}T\d{6}_[0-9a-f]{6}", rid)

    def test_same_content_same_hash_suffix(self):
        from devils_advocate.ids import generate_review_id
        r1 = generate_review_id("same content")
        r2 = generate_review_id("same content")
        # Same content → same hash suffix (but timestamp may differ)
        assert r1.split("_")[1] == r2.split("_")[1]

    def test_different_content_different_hash(self):
        from devils_advocate.ids import generate_review_id
        r1 = generate_review_id("content A")
        r2 = generate_review_id("content B")
        assert r1.split("_")[1] != r2.split("_")[1]


class TestGenerateNewGroupId:
    def test_format(self):
        from devils_advocate.ids import generate_new_group_id
        dt = datetime(2026, 2, 14, 18, 26, 0, tzinfo=timezone.utc)
        gid = generate_new_group_id("atlas-voice", 1, dt, "4g9a")
        assert gid == "atlas-voice.group_001.14FEB2026.1826.4g9a"

    def test_zero_padded_index(self):
        from devils_advocate.ids import generate_new_group_id
        dt = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        gid = generate_new_group_id("proj", 42, dt, "abc1")
        assert "group_042" in gid


class TestGenerateNewPointId:
    def test_derived_from_group(self):
        from devils_advocate.ids import generate_new_point_id
        pid = generate_new_point_id("proj.group_001.14FEB2026.1826.4g9a", 3)
        assert pid == "proj.group_001.14FEB2026.1826.4g9a.point_003"


class TestAssignGuids:
    def test_assigns_uuid4(self):
        from devils_advocate.ids import assign_guids
        g1 = make_review_group(group_id="g1")
        g2 = make_review_group(group_id="g2")
        assign_guids([g1, g2])
        assert g1.guid != ""
        assert g2.guid != ""
        # Valid UUID
        uuid.UUID(g1.guid)
        uuid.UUID(g2.guid)

    def test_unique_guids(self):
        from devils_advocate.ids import assign_guids
        groups = [make_review_group(group_id=f"g{i}") for i in range(10)]
        assign_guids(groups)
        guids = [g.guid for g in groups]
        assert len(set(guids)) == 10


class TestResolveGuid:
    def _group_with_guid(self, gid, guid):
        g = make_review_group(group_id=gid)
        g.guid = guid
        return g

    def test_exact_match(self):
        from devils_advocate.ids import resolve_guid
        g = self._group_with_guid("real-group", "abc-123-def")
        result = resolve_guid("abc-123-def", [g])
        assert result == "real-group"

    def test_extract_uuid_from_noise(self):
        from devils_advocate.ids import resolve_guid
        uid = "12345678-1234-1234-1234-123456789abc"
        g = self._group_with_guid("real-group", uid)
        result = resolve_guid(f"GROUP 1 [{uid}]", [g])
        assert result == "real-group"

    def test_fuzzy_match_one_char(self):
        from devils_advocate.ids import resolve_guid
        uid = "12345678-1234-1234-1234-123456789abc"
        g = self._group_with_guid("real-group", uid)
        # Change one character
        mangled = "12345678-1234-1234-1234-123456789abd"
        result = resolve_guid(mangled, [g])
        assert result == "real-group"

    def test_fuzzy_match_two_chars(self):
        from devils_advocate.ids import resolve_guid
        uid = "12345678-1234-1234-1234-123456789abc"
        g = self._group_with_guid("real-group", uid)
        # Change two characters
        mangled = "12345678-1234-1234-1234-123456789abe"
        result = resolve_guid(mangled, [g])
        assert result == "real-group"

    def test_three_chars_too_far(self):
        from devils_advocate.ids import resolve_guid
        uid = "12345678-1234-1234-1234-123456789abc"
        g = self._group_with_guid("real-group", uid)
        # Change three characters — exceeds threshold
        mangled = "12345678-1234-1234-1234-123456789xyz"
        result = resolve_guid(mangled, [g])
        assert result is None

    def test_no_match_returns_none(self):
        from devils_advocate.ids import resolve_guid
        g = self._group_with_guid("real-group", "abc-123")
        result = resolve_guid("totally-different", [g])
        assert result is None

    def test_whitespace_stripped(self):
        from devils_advocate.ids import resolve_guid
        g = self._group_with_guid("real-group", "abc-123")
        result = resolve_guid("  abc-123  ", [g])
        assert result == "real-group"

    def test_log_fn_called_on_exact(self):
        from devils_advocate.ids import resolve_guid
        g = self._group_with_guid("real-group", "abc-123")
        log = MagicMock()
        resolve_guid("abc-123", [g], log_fn=log)
        log.assert_called_once()
        assert "exact" in log.call_args[0][0]

    def test_log_fn_called_on_failure(self):
        from devils_advocate.ids import resolve_guid
        g = self._group_with_guid("real-group", "abc-123")
        log = MagicMock()
        resolve_guid("xyz-999", [g], log_fn=log)
        log.assert_called_once()
        assert "FAILED" in log.call_args[0][0]

    def test_silent_suppresses_log(self):
        from devils_advocate.ids import resolve_guid
        g = self._group_with_guid("real-group", "abc-123")
        log = MagicMock()
        resolve_guid("abc-123", [g], log_fn=log, silent=True)
        log.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
# 2. Output module — generate_report
# ═══════════════════════════════════════════════════════════════════════════


def _make_review_result(**kwargs):
    """Build a ReviewResult for test purposes."""
    from devils_advocate.types import ReviewResult, CostTracker, GovernanceDecision

    defaults = {
        "review_id": "test-review-001",
        "mode": "plan",
        "input_file": "plan.md",
        "project": "test-project",
        "timestamp": "2026-01-01T00:00:00Z",
        "author_model": "gpt-4",
        "reviewer_models": ["gemini-flash", "kimi-k2"],
        "dedup_model": "gpt-4o-mini",
        "points": [],  # required — list of dicts for ledger
        "groups": [],
        "author_responses": [],
        "governance_decisions": [],
        "rebuttals": [],
        "author_final_responses": [],
        "revised_output": "",
        "cost": CostTracker(),
        "summary": {"total_groups": 0, "total_points": 0},
    }
    defaults.update(kwargs)
    return ReviewResult(**defaults)


class TestGenerateReport:
    def test_basic_report_structure(self):
        from devils_advocate.output import generate_report
        result = _make_review_result()
        report = generate_report(result)
        assert "# Devil's Advocate Review Report" in report
        assert "Plan Review" in report
        assert "test-project" in report

    def test_report_includes_cost(self):
        from devils_advocate.output import generate_report
        from devils_advocate.types import CostTracker
        cost = CostTracker()
        cost.add("model-a", 1000, 500, 0.03, 0.06)
        result = _make_review_result(cost=cost)
        report = generate_report(result)
        assert "Cost Breakdown" in report
        assert "model-a" in report

    def test_report_with_groups(self):
        from devils_advocate.output import generate_report
        from devils_advocate.types import GovernanceDecision
        g = make_review_group(group_id="g1", concern="Security issue")
        dec = GovernanceDecision(
            group_id="g1",
            author_resolution="ACCEPTED",
            governance_resolution="auto_accepted",
            reason="Author accepted with substantive rationale",
        )
        resp = make_author_response(group_id="g1")
        result = _make_review_result(
            groups=[g],
            governance_decisions=[dec],
            author_responses=[resp],
            summary={"total_groups": 1, "total_points": 1, "auto_accepted": 1},
        )
        report = generate_report(result)
        assert "Security issue" in report
        assert "Auto Accepted" in report

    def test_escalated_section(self):
        from devils_advocate.output import generate_report
        from devils_advocate.types import GovernanceDecision
        g = make_review_group(group_id="g1", concern="Needs review")
        dec = GovernanceDecision(
            group_id="g1",
            author_resolution="REJECTED",
            governance_resolution="escalated",
            reason="Rejected without substantive rationale",
        )
        result = _make_review_result(
            groups=[g],
            governance_decisions=[dec],
            summary={"total_groups": 1, "escalated": 1},
        )
        report = generate_report(result)
        assert "Escalated" in report

    def test_revised_output_included(self):
        from devils_advocate.output import generate_report
        result = _make_review_result(revised_output="# Revised Plan\nDo things better")
        report = generate_report(result)
        assert "Revised Plan" in report
        assert "Do things better" in report

    def test_code_mode_revised_label(self):
        from devils_advocate.output import generate_report
        result = _make_review_result(mode="code", revised_output="--- a/file")
        report = generate_report(result)
        assert "Revised Code Diff" in report

    def test_integration_mode_revised_label(self):
        from devils_advocate.output import generate_report
        result = _make_review_result(mode="integration", revised_output="Fix it")
        report = generate_report(result)
        assert "Remediation Plan" in report

    def test_round2_rebuttals_in_report(self):
        from devils_advocate.output import generate_report
        from devils_advocate.types import GovernanceDecision
        g = make_review_group(group_id="g1", concern="Issue")
        dec = GovernanceDecision("g1", "REJECTED", "escalated", "Challenged")
        resp = make_author_response(group_id="g1", resolution="REJECTED", rationale="Not a bug")
        rebuttal = make_rebuttal(group_id="g1", reviewer="reviewer_a", verdict="CHALLENGE")
        result = _make_review_result(
            groups=[g],
            governance_decisions=[dec],
            author_responses=[resp],
            rebuttals=[rebuttal],
            summary={"total_groups": 1, "escalated": 1},
        )
        report = generate_report(result)
        assert "Rebuttals" in report
        assert "CHALLENGE" in report

    def test_missing_author_response_shown(self):
        from devils_advocate.output import generate_report
        from devils_advocate.types import GovernanceDecision
        g = make_review_group(group_id="g1", concern="Issue")
        dec = GovernanceDecision("g1", "no_response", "escalated", "No response")
        result = _make_review_result(
            groups=[g],
            governance_decisions=[dec],
            summary={"total_groups": 1, "escalated": 1},
        )
        report = generate_report(result)
        assert "Author did not respond" in report


# ═══════════════════════════════════════════════════════════════════════════
# 3. Output module — generate_ledger
# ═══════════════════════════════════════════════════════════════════════════


class TestGenerateLedger:
    def test_basic_ledger(self):
        from devils_advocate.output import generate_ledger
        result = _make_review_result()
        ledger = generate_ledger(result)
        assert ledger["review_id"] == "test-review-001"
        assert ledger["result"] == "success"
        assert ledger["mode"] == "plan"
        assert "cost" in ledger

    def test_ledger_points(self):
        from devils_advocate.output import generate_ledger
        from devils_advocate.types import GovernanceDecision
        g = make_review_group(group_id="g1")
        dec = GovernanceDecision("g1", "ACCEPTED", "auto_accepted", "Reason")
        resp = make_author_response(group_id="g1")
        result = _make_review_result(
            groups=[g],
            governance_decisions=[dec],
            author_responses=[resp],
        )
        ledger = generate_ledger(result)
        assert len(ledger["points"]) == 1
        assert ledger["points"][0]["group_id"] == "g1"
        assert ledger["points"][0]["governance_resolution"] == "auto_accepted"
        assert ledger["points"][0]["final_resolution"] == "auto_accepted"

    def test_ledger_summary(self):
        from devils_advocate.output import generate_ledger
        from devils_advocate.types import GovernanceDecision
        g = make_review_group(group_id="g1")
        dec = GovernanceDecision("g1", "ACCEPTED", "auto_accepted", "Reason")
        result = _make_review_result(
            groups=[g],
            governance_decisions=[dec],
            summary={"total_groups": 1, "total_points": 1},
        )
        ledger = generate_ledger(result)
        assert ledger["summary"]["total_groups"] == 1
        assert ledger["summary"]["auto_accepted"] == 1

    def test_ledger_cost_breakdown(self):
        from devils_advocate.output import generate_ledger
        from devils_advocate.types import CostTracker
        cost = CostTracker()
        cost.add("model-a", 1000, 500, 0.03, 0.06)
        result = _make_review_result(cost=cost)
        ledger = generate_ledger(result)
        assert ledger["cost"]["total_usd"] > 0
        assert "model-a" in ledger["cost"]["breakdown"]

    def test_ledger_no_author_response(self):
        from devils_advocate.output import generate_ledger
        from devils_advocate.types import GovernanceDecision
        g = make_review_group(group_id="g1")
        dec = GovernanceDecision("g1", "no_response", "escalated", "Missing")
        result = _make_review_result(
            groups=[g],
            governance_decisions=[dec],
        )
        ledger = generate_ledger(result)
        assert ledger["points"][0]["author_resolution"] == "no_response"
        assert ledger["points"][0]["author_rationale"] == ""

    def test_ledger_with_rebuttals(self):
        from devils_advocate.output import generate_ledger
        from devils_advocate.types import GovernanceDecision
        g = make_review_group(group_id="g1")
        dec = GovernanceDecision("g1", "REJECTED", "escalated", "Challenged")
        resp = make_author_response(group_id="g1", resolution="REJECTED")
        rebuttal = make_rebuttal(group_id="g1", reviewer="r1", verdict="CHALLENGE")
        result = _make_review_result(
            groups=[g],
            governance_decisions=[dec],
            author_responses=[resp],
            rebuttals=[rebuttal],
        )
        ledger = generate_ledger(result)
        assert len(ledger["points"][0]["rebuttals"]) == 1
        assert ledger["points"][0]["rebuttals"][0]["verdict"] == "CHALLENGE"

    def test_ledger_spec_mode_preserves_extra_summary(self):
        from devils_advocate.output import generate_ledger
        result = _make_review_result(
            mode="spec",
            summary={"total_groups": 5, "total_points": 10,
                      "multi_consensus": 3, "single_source": 2},
        )
        ledger = generate_ledger(result)
        assert ledger["summary"]["multi_consensus"] == 3
        assert ledger["summary"]["single_source"] == 2

    def test_ledger_overrides_field_present(self):
        from devils_advocate.output import generate_ledger
        from devils_advocate.types import GovernanceDecision
        g = make_review_group(group_id="g1")
        dec = GovernanceDecision("g1", "ACCEPTED", "auto_accepted", "Reason")
        resp = make_author_response(group_id="g1")
        result = _make_review_result(
            groups=[g],
            governance_decisions=[dec],
            author_responses=[resp],
        )
        ledger = generate_ledger(result)
        assert "overrides" in ledger["points"][0]
        assert ledger["points"][0]["overrides"] == []


# ═══════════════════════════════════════════════════════════════════════════
# 4. Spec report
# ═══════════════════════════════════════════════════════════════════════════


class TestSpecReport:
    def test_spec_report_structure(self):
        from devils_advocate.output import generate_report
        result = _make_review_result(
            mode="spec",
            summary={"total_groups": 2, "total_points": 3,
                      "multi_consensus": 1, "single_source": 1},
        )
        report = generate_report(result)
        assert "Specification Enrichment Report" in report
        assert "Collaborative Ideation" in report

    def test_spec_high_consensus_section(self):
        from devils_advocate.output import generate_report
        g1 = make_review_group(group_id="g1", concern="Important thing",
                                source_reviewers=["r1", "r2"])
        g1.combined_category = "security"
        result = _make_review_result(
            mode="spec",
            groups=[g1],
            summary={"total_groups": 1, "total_points": 1,
                      "multi_consensus": 1, "single_source": 0},
        )
        report = generate_report(result)
        assert "High-Consensus" in report
        assert "Important thing" in report

    def test_spec_themed_sections(self):
        from devils_advocate.output import generate_report
        g1 = make_review_group(group_id="g1", concern="Fix auth")
        g1.combined_category = "security"
        g2 = make_review_group(group_id="g2", concern="Optimize query")
        g2.combined_category = "performance"
        result = _make_review_result(
            mode="spec",
            groups=[g1, g2],
            summary={"total_groups": 2, "total_points": 2},
        )
        report = generate_report(result)
        assert "Security" in report
        assert "Performance" in report


# ═══════════════════════════════════════════════════════════════════════════
# 5. Normalization module
# ═══════════════════════════════════════════════════════════════════════════


class TestNormalizeReviewResponse:
    @pytest.mark.asyncio
    async def test_successful_normalization(self):
        from devils_advocate.normalization import normalize_review_response
        from devils_advocate.types import CostTracker

        model = make_model_config(name="norm-model")
        cost = CostTracker()

        # Mock the LLM returning structured output
        structured_output = (
            "### FINDING 1\n"
            "**Severity:** high\n"
            "**Category:** security\n"
            "**Description:** SQL injection in auth module\n"
            "**Recommendation:** Use parameterized queries\n"
            "**Location:** auth.py:42\n"
        )
        usage = {"input_tokens": 200, "output_tokens": 100}

        with patch("devils_advocate.normalization.call_with_retry",
                    new_callable=AsyncMock, return_value=(structured_output, usage)):
            points = await normalize_review_response(
                MagicMock(), "raw text", model, "reviewer-1",
                cost_tracker=cost,
            )

        # Should have parsed the structured output
        assert isinstance(points, list)

    @pytest.mark.asyncio
    async def test_normalization_failure_returns_empty(self):
        from devils_advocate.normalization import normalize_review_response

        model = make_model_config(name="norm-model")
        log = MagicMock()

        with patch("devils_advocate.normalization.call_with_retry",
                    new_callable=AsyncMock, side_effect=Exception("LLM error")):
            points = await normalize_review_response(
                MagicMock(), "raw text", model, "reviewer-1",
                log_fn=log,
            )

        assert points == []
        log.assert_called()  # Should log the failure

    @pytest.mark.asyncio
    async def test_log_fn_called_with_call_info(self):
        from devils_advocate.normalization import normalize_review_response

        model = make_model_config(name="norm-model")
        log = MagicMock()
        usage = {"input_tokens": 100, "output_tokens": 50}

        with patch("devils_advocate.normalization.call_with_retry",
                    new_callable=AsyncMock, return_value=("no findings", usage)):
            await normalize_review_response(
                MagicMock(), "raw text", model, "reviewer-1",
                log_fn=log,
            )

        # First call should be the call info log
        assert any("Normalization" in str(c) for c in log.call_args_list)

    @pytest.mark.asyncio
    async def test_cost_tracker_updated(self):
        from devils_advocate.normalization import normalize_review_response
        from devils_advocate.types import CostTracker

        model = make_model_config(name="norm-model")
        cost = CostTracker()
        usage = {"input_tokens": 200, "output_tokens": 100}

        with patch("devils_advocate.normalization.call_with_retry",
                    new_callable=AsyncMock, return_value=("no findings", usage)):
            await normalize_review_response(
                MagicMock(), "raw text", model, "reviewer-1",
                cost_tracker=cost,
            )

        assert cost.total_usd > 0

    @pytest.mark.asyncio
    async def test_start_index_passed_through(self):
        from devils_advocate.normalization import normalize_review_response

        model = make_model_config(name="norm-model")
        usage = {"input_tokens": 100, "output_tokens": 50}

        with patch("devils_advocate.normalization.call_with_retry",
                    new_callable=AsyncMock, return_value=("no findings", usage)):
            points = await normalize_review_response(
                MagicMock(), "raw text", model, "reviewer-1",
                start_index=10,
            )

        # No findings parsed, but function should not error
        assert isinstance(points, list)
