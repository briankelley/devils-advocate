"""Tests for revision module (build_revision_context, build_spec_revision_context,
_extract_revision_strict, build_revision_prompt) and cost module
(estimate_tokens, estimate_cost, check_context_window).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helpers import make_model_config, make_review_group, make_review_point


# ═══════════════════════════════════════════════════════════════════════════
# 1. build_revision_context
# ═══════════════════════════════════════════════════════════════════════════


class TestBuildRevisionContext:
    def test_empty_points(self):
        from devils_advocate.revision import build_revision_context
        result = build_revision_context({"points": []})
        assert result == ""

    def test_no_points_key(self):
        from devils_advocate.revision import build_revision_context
        result = build_revision_context({})
        assert result == ""

    def test_accepted_findings_section(self):
        from devils_advocate.revision import build_revision_context
        ledger = {"points": [
            {
                "point_id": "p1", "group_id": "g1", "reviewer": "r1",
                "severity": "high", "description": "SQL injection risk",
                "recommendation": "Use parameterized queries",
                "location": "db.py:42",
                "final_resolution": "auto_accepted",
            }
        ]}
        result = build_revision_context(ledger)
        assert "=== ACCEPTED FINDINGS" in result
        assert "SQL injection risk" in result
        assert "Use parameterized queries" in result
        assert "db.py:42" in result

    def test_dismissed_findings_section(self):
        from devils_advocate.revision import build_revision_context
        ledger = {"points": [
            {
                "point_id": "p1", "group_id": "g1", "reviewer": "r1",
                "severity": "low", "description": "Style issue",
                "recommendation": "Follow PEP8",
                "final_resolution": "auto_dismissed",
            }
        ]}
        result = build_revision_context(ledger)
        assert "=== DISMISSED FINDINGS" in result
        assert "Style issue" in result

    def test_unresolved_findings_section(self):
        from devils_advocate.revision import build_revision_context
        ledger = {"points": [
            {
                "point_id": "p1", "group_id": "g1", "reviewer": "r1",
                "severity": "medium", "description": "Needs review",
                "final_resolution": "escalated",
            }
        ]}
        result = build_revision_context(ledger)
        assert "=== UNRESOLVED FINDINGS" in result

    def test_pending_is_unresolved(self):
        from devils_advocate.revision import build_revision_context
        ledger = {"points": [
            {
                "point_id": "p1", "group_id": "g1", "reviewer": "r1",
                "severity": "medium", "description": "Still pending",
                "final_resolution": "pending",
            }
        ]}
        result = build_revision_context(ledger)
        assert "=== UNRESOLVED FINDINGS" in result

    def test_overridden_is_actionable(self):
        from devils_advocate.revision import build_revision_context
        ledger = {"points": [
            {
                "point_id": "p1", "group_id": "g1", "reviewer": "r1",
                "severity": "high", "description": "Override case",
                "final_resolution": "overridden",
            }
        ]}
        result = build_revision_context(ledger)
        assert "=== ACCEPTED FINDINGS" in result

    def test_accepted_is_actionable(self):
        from devils_advocate.revision import build_revision_context
        ledger = {"points": [
            {
                "point_id": "p1", "group_id": "g1", "reviewer": "r1",
                "severity": "high", "description": "Accepted",
                "final_resolution": "accepted",
            }
        ]}
        result = build_revision_context(ledger)
        assert "=== ACCEPTED FINDINGS" in result

    def test_multiple_groups(self):
        from devils_advocate.revision import build_revision_context
        ledger = {"points": [
            {
                "point_id": "p1", "group_id": "g1", "reviewer": "r1",
                "severity": "high", "description": "Issue A",
                "final_resolution": "auto_accepted",
            },
            {
                "point_id": "p2", "group_id": "g2", "reviewer": "r2",
                "severity": "low", "description": "Issue B",
                "final_resolution": "auto_dismissed",
            },
        ]}
        result = build_revision_context(ledger)
        assert "=== ACCEPTED FINDINGS" in result
        assert "=== DISMISSED FINDINGS" in result
        assert "Issue A" in result
        assert "Issue B" in result

    def test_inconsistent_resolutions_unresolved(self):
        from devils_advocate.revision import build_revision_context
        ledger = {"points": [
            {
                "point_id": "p1", "group_id": "g1", "reviewer": "r1",
                "severity": "high", "description": "Issue",
                "final_resolution": "auto_accepted",
            },
            {
                "point_id": "p2", "group_id": "g1", "reviewer": "r2",
                "severity": "high", "description": "Same issue",
                "final_resolution": "auto_dismissed",
            },
        ]}
        result = build_revision_context(ledger)
        # Inconsistent within g1 → treated as unresolved
        assert "=== UNRESOLVED FINDINGS" in result

    def test_author_rationale_included(self):
        from devils_advocate.revision import build_revision_context
        ledger = {"points": [
            {
                "point_id": "p1", "group_id": "g1", "reviewer": "r1",
                "severity": "high", "description": "Issue",
                "final_resolution": "auto_accepted",
                "author_rationale": "Valid concern that needs addressing",
            }
        ]}
        result = build_revision_context(ledger)
        assert "Valid concern" in result

    def test_deduplication_within_group(self):
        from devils_advocate.revision import build_revision_context
        ledger = {"points": [
            {
                "point_id": "p1", "group_id": "g1", "reviewer": "r1",
                "description": "Same issue", "recommendation": "Same fix",
                "location": "file.py:1",
                "severity": "high",
                "final_resolution": "auto_accepted",
            },
            {
                "point_id": "p2", "group_id": "g1", "reviewer": "r1",
                "description": "Same issue", "recommendation": "Same fix",
                "location": "file.py:1",
                "severity": "high",
                "final_resolution": "auto_accepted",
            },
        ]}
        result = build_revision_context(ledger)
        # Should deduplicate — only one mention of "Same issue"
        assert result.count("[r1] Same issue") == 1

    def test_no_recommendation_omits_line(self):
        from devils_advocate.revision import build_revision_context
        ledger = {"points": [
            {
                "point_id": "p1", "group_id": "g1", "reviewer": "r1",
                "severity": "high", "description": "Issue",
                "recommendation": "",
                "location": "",
                "final_resolution": "auto_accepted",
            }
        ]}
        result = build_revision_context(ledger)
        assert "Recommendation:" not in result
        assert "Location:" not in result

    def test_missing_group_id_uses_unknown(self):
        from devils_advocate.revision import build_revision_context
        ledger = {"points": [
            {
                "point_id": "p1", "reviewer": "r1",
                "severity": "high", "description": "Orphan",
                "final_resolution": "auto_accepted",
            }
        ]}
        result = build_revision_context(ledger)
        assert "unknown" in result


# ═══════════════════════════════════════════════════════════════════════════
# 2. build_spec_revision_context
# ═══════════════════════════════════════════════════════════════════════════


class TestBuildSpecRevisionContext:
    def test_empty_groups(self):
        from devils_advocate.revision import build_spec_revision_context
        result = build_spec_revision_context([])
        assert result == ""

    def test_single_group(self):
        from devils_advocate.revision import build_spec_revision_context
        g = make_review_group(
            group_id="sg1",
            concern="Add error handling",
            source_reviewers=["r1", "r2"],
        )
        g.combined_category = "error_handling"
        result = build_spec_revision_context([g], total_reviewers=2)
        assert "THEME: Error Handling" in result
        assert "sg1" in result
        assert "2 of 2" in result

    def test_multiple_themes(self):
        from devils_advocate.revision import build_spec_revision_context
        g1 = make_review_group(group_id="sg1", concern="Security fix")
        g1.combined_category = "security"
        g2 = make_review_group(group_id="sg2", concern="Performance fix")
        g2.combined_category = "performance"
        result = build_spec_revision_context([g1, g2], total_reviewers=2)
        assert "THEME: Security" in result
        assert "THEME: Performance" in result

    def test_default_category_is_other(self):
        from devils_advocate.revision import build_spec_revision_context
        g = make_review_group(group_id="sg1", concern="Something")
        g.combined_category = ""
        result = build_spec_revision_context([g])
        assert "THEME: Other" in result

    def test_point_locations_included(self):
        from devils_advocate.revision import build_spec_revision_context
        p = make_review_point(location="src/api.py:100")
        g = make_review_group(group_id="sg1", points=[p])
        g.combined_category = "correctness"
        result = build_spec_revision_context([g])
        assert "src/api.py:100" in result


# ═══════════════════════════════════════════════════════════════════════════
# 3. _extract_revision_strict
# ═══════════════════════════════════════════════════════════════════════════


class TestExtractRevisionStrict:
    def test_plan_mode(self):
        from devils_advocate.revision import _extract_revision_strict
        raw = "preamble\n=== REVISED PLAN ===\nRevised content here\n=== END REVISED PLAN ===\npostamble"
        result = _extract_revision_strict(raw, "plan")
        assert result == "Revised content here"

    def test_code_mode(self):
        from devils_advocate.revision import _extract_revision_strict
        raw = "=== UNIFIED DIFF ===\n--- a/file\n+++ b/file\n=== END UNIFIED DIFF ==="
        result = _extract_revision_strict(raw, "code")
        assert "--- a/file" in result

    def test_integration_mode(self):
        from devils_advocate.revision import _extract_revision_strict
        raw = "=== REMEDIATION PLAN ===\nFix things\n=== END REMEDIATION PLAN ==="
        result = _extract_revision_strict(raw, "integration")
        assert result == "Fix things"

    def test_spec_mode(self):
        from devils_advocate.revision import _extract_revision_strict
        raw = "=== SPEC SUGGESTIONS ===\nSuggestions here\n=== END SPEC SUGGESTIONS ==="
        result = _extract_revision_strict(raw, "spec")
        assert result == "Suggestions here"

    def test_missing_delimiters_returns_empty(self):
        from devils_advocate.revision import _extract_revision_strict
        raw = "Just some text without any delimiters"
        result = _extract_revision_strict(raw, "plan")
        assert result == ""

    def test_wrong_delimiters_returns_empty(self):
        from devils_advocate.revision import _extract_revision_strict
        raw = "=== UNIFIED DIFF ===\nContent\n=== END UNIFIED DIFF ==="
        result = _extract_revision_strict(raw, "plan")  # mode mismatch
        assert result == ""

    def test_unknown_mode_defaults_to_plan(self):
        from devils_advocate.revision import _extract_revision_strict
        raw = "=== REVISED PLAN ===\nContent\n=== END REVISED PLAN ==="
        result = _extract_revision_strict(raw, "unknown_mode")
        assert result == "Content"

    def test_multiline_content(self):
        from devils_advocate.revision import _extract_revision_strict
        raw = (
            "=== REVISED PLAN ===\n"
            "Line 1\n"
            "Line 2\n"
            "Line 3\n"
            "=== END REVISED PLAN ==="
        )
        result = _extract_revision_strict(raw, "plan")
        assert "Line 1" in result
        assert "Line 3" in result

    def test_content_stripped(self):
        from devils_advocate.revision import _extract_revision_strict
        raw = "=== REVISED PLAN ===\n\n  Content  \n\n=== END REVISED PLAN ==="
        result = _extract_revision_strict(raw, "plan")
        assert result == "Content"


# ═══════════════════════════════════════════════════════════════════════════
# 4. build_revision_prompt
# ═══════════════════════════════════════════════════════════════════════════


class TestBuildRevisionPrompt:
    def test_plan_mode_uses_template(self):
        from devils_advocate.revision import build_revision_prompt
        result = build_revision_prompt("plan", "original plan text", "context text")
        # Should contain the original content and context
        assert "original plan text" in result
        assert "context text" in result

    def test_code_mode_uses_template(self):
        from devils_advocate.revision import build_revision_prompt
        result = build_revision_prompt("code", "def foo(): pass", "findings")
        assert "def foo(): pass" in result

    def test_integration_mode_uses_template(self):
        from devils_advocate.revision import build_revision_prompt
        result = build_revision_prompt("integration", "manifest content", "findings")
        assert "manifest content" in result

    def test_spec_mode_uses_template(self):
        from devils_advocate.revision import build_revision_prompt
        result = build_revision_prompt("spec", "spec content", "suggestions")
        assert "spec content" in result

    def test_unknown_mode_falls_back_to_plan(self):
        from devils_advocate.revision import build_revision_prompt
        result = build_revision_prompt("weird", "content", "context")
        assert "content" in result


# ═══════════════════════════════════════════════════════════════════════════
# 5. run_revision
# ═══════════════════════════════════════════════════════════════════════════


class TestRunRevision:
    @pytest.mark.asyncio
    async def test_no_actionable_findings_skips(self, tmp_path):
        from devils_advocate.revision import run_revision
        from devils_advocate.types import CostTracker
        from devils_advocate.storage import StorageManager

        storage = StorageManager(tmp_path, data_dir=tmp_path)
        storage.set_review_id("rev-001")
        cost = CostTracker()
        model = make_model_config(name="rev-model")

        ledger = {"points": [
            {"point_id": "p1", "group_id": "g1", "final_resolution": "auto_dismissed",
             "reviewer": "r1", "severity": "low", "description": "nothing"},
        ]}

        result = await run_revision(
            None, model, "original", ledger, "plan", cost, storage, "rev-001",
        )
        assert result == ""

    @pytest.mark.asyncio
    async def test_actionable_findings_calls_llm(self, tmp_path):
        from devils_advocate.revision import run_revision
        from devils_advocate.types import CostTracker
        from devils_advocate.storage import StorageManager

        storage = StorageManager(tmp_path, data_dir=tmp_path)
        storage.set_review_id("rev-001")
        cost = CostTracker()
        model = make_model_config(name="rev-model")

        ledger = {"points": [
            {"point_id": "p1", "group_id": "g1", "final_resolution": "auto_accepted",
             "reviewer": "r1", "severity": "high", "description": "Fix this",
             "recommendation": "Do it right"},
        ]}

        raw_response = "=== REVISED PLAN ===\nRevised plan content\n=== END REVISED PLAN ==="
        usage = {"input_tokens": 100, "output_tokens": 50}

        with patch("devils_advocate.revision.call_with_retry", new_callable=AsyncMock,
                    return_value=(raw_response, usage)):
            result = await run_revision(
                MagicMock(), model, "original plan", ledger, "plan",
                cost, storage, "rev-001",
            )

        assert result == "Revised plan content"
        assert cost.total_usd > 0


# ═══════════════════════════════════════════════════════════════════════════
# 6. run_spec_revision
# ═══════════════════════════════════════════════════════════════════════════


class TestRunSpecRevision:
    @pytest.mark.asyncio
    async def test_empty_groups_skips(self, tmp_path):
        from devils_advocate.revision import run_spec_revision
        from devils_advocate.types import CostTracker
        from devils_advocate.storage import StorageManager

        storage = StorageManager(tmp_path, data_dir=tmp_path)
        storage.set_review_id("rev-001")
        cost = CostTracker()
        model = make_model_config(name="rev-model")

        result = await run_spec_revision(
            None, model, "original", [], 2, cost, storage, "rev-001",
        )
        assert result == ""

    @pytest.mark.asyncio
    async def test_with_groups_calls_llm(self, tmp_path):
        from devils_advocate.revision import run_spec_revision
        from devils_advocate.types import CostTracker
        from devils_advocate.storage import StorageManager

        storage = StorageManager(tmp_path, data_dir=tmp_path)
        storage.set_review_id("rev-001")
        cost = CostTracker()
        model = make_model_config(name="rev-model")

        g = make_review_group(group_id="sg1", concern="Add tests")
        g.combined_category = "testing"

        raw_response = "=== SPEC SUGGESTIONS ===\nSpec suggestions\n=== END SPEC SUGGESTIONS ==="
        usage = {"input_tokens": 100, "output_tokens": 50}

        with patch("devils_advocate.revision.call_with_retry", new_callable=AsyncMock,
                    return_value=(raw_response, usage)):
            result = await run_spec_revision(
                MagicMock(), model, "original spec", [g], 2,
                cost, storage, "rev-001",
            )

        assert result == "Spec suggestions"


# ═══════════════════════════════════════════════════════════════════════════
# 7. estimate_tokens
# ═══════════════════════════════════════════════════════════════════════════


class TestEstimateTokens:
    def test_basic_estimation(self):
        from devils_advocate.cost import estimate_tokens
        # 20 chars / 4 = 5 tokens
        assert estimate_tokens("a" * 20) == 5

    def test_empty_string_returns_1(self):
        from devils_advocate.cost import estimate_tokens
        assert estimate_tokens("") == 1

    def test_single_char(self):
        from devils_advocate.cost import estimate_tokens
        assert estimate_tokens("x") == 1

    def test_exact_multiple(self):
        from devils_advocate.cost import estimate_tokens
        assert estimate_tokens("a" * 400) == 100


# ═══════════════════════════════════════════════════════════════════════════
# 8. estimate_cost
# ═══════════════════════════════════════════════════════════════════════════


class TestEstimateCost:
    def test_basic_cost(self):
        from devils_advocate.cost import estimate_cost
        model = make_model_config(cost_per_1k_input=0.03, cost_per_1k_output=0.06)
        cost = estimate_cost(model, input_tokens=1000, output_tokens=500)
        expected = (1000 / 1000 * 0.03) + (500 / 1000 * 0.06)
        assert abs(cost - expected) < 1e-10

    def test_zero_tokens(self):
        from devils_advocate.cost import estimate_cost
        model = make_model_config(cost_per_1k_input=0.03, cost_per_1k_output=0.06)
        cost = estimate_cost(model, input_tokens=0, output_tokens=0)
        assert cost == 0.0

    def test_no_cost_configured(self):
        from devils_advocate.cost import estimate_cost
        model = make_model_config(cost_per_1k_input=None, cost_per_1k_output=None)
        cost = estimate_cost(model, input_tokens=1000, output_tokens=500)
        assert cost == 0.0

    def test_partial_cost_configured(self):
        from devils_advocate.cost import estimate_cost
        model = make_model_config(cost_per_1k_input=0.03, cost_per_1k_output=None)
        cost = estimate_cost(model, input_tokens=1000, output_tokens=500)
        assert cost == 0.0  # Returns 0 if either is None


# ═══════════════════════════════════════════════════════════════════════════
# 9. check_context_window
# ═══════════════════════════════════════════════════════════════════════════


class TestCheckContextWindow:
    def test_fits_within_window(self):
        from devils_advocate.cost import check_context_window
        model = make_model_config(context_window=128000)
        text = "a" * 400  # 100 tokens, well within 80% of 128000
        fits, est, limit = check_context_window(model, text)
        assert fits is True
        assert est == 100
        assert limit == int(128000 * 0.8)

    def test_exceeds_window(self):
        from devils_advocate.cost import check_context_window
        model = make_model_config(context_window=100)
        text = "a" * 400  # 100 tokens, 80% of 100 = 80
        fits, est, limit = check_context_window(model, text)
        assert fits is False
        assert est == 100
        assert limit == 80

    def test_no_context_window_always_fits(self):
        from devils_advocate.cost import check_context_window
        model = make_model_config(context_window=None)
        text = "a" * 1000000  # Huge text
        fits, est, limit = check_context_window(model, text)
        assert fits is True
        assert limit == 0

    def test_exactly_at_threshold(self):
        from devils_advocate.cost import check_context_window
        model = make_model_config(context_window=500)
        # 80% of 500 = 400, need 400 tokens = 1600 chars
        text = "a" * 1600
        fits, est, limit = check_context_window(model, text)
        assert fits is True
        assert est == 400
        assert limit == 400

    def test_one_over_threshold(self):
        from devils_advocate.cost import check_context_window
        model = make_model_config(context_window=500)
        # 401 tokens = 1604 chars
        text = "a" * 1604
        fits, est, limit = check_context_window(model, text)
        assert fits is False
        assert est == 401

    def test_empty_text(self):
        from devils_advocate.cost import check_context_window
        model = make_model_config(context_window=128000)
        fits, est, limit = check_context_window(model, "")
        assert fits is True
        assert est == 1  # min 1 from estimate_tokens


# ═══════════════════════════════════════════════════════════════════════════
# 10. _DELIMITERS constant
# ═══════════════════════════════════════════════════════════════════════════


class TestDelimitersConstant:
    def test_all_modes_covered(self):
        from devils_advocate.revision import _DELIMITERS
        assert set(_DELIMITERS.keys()) == {"plan", "code", "integration", "spec"}

    def test_delimiters_are_pairs(self):
        from devils_advocate.revision import _DELIMITERS
        for mode, (start, end) in _DELIMITERS.items():
            assert isinstance(start, str)
            assert isinstance(end, str)
            assert "===" in start
            assert "===" in end


# ═══════════════════════════════════════════════════════════════════════════
# 11. _ACTIONABLE_RESOLUTIONS constant
# ═══════════════════════════════════════════════════════════════════════════


class TestActionableResolutions:
    def test_expected_resolutions(self):
        from devils_advocate.revision import _ACTIONABLE_RESOLUTIONS
        assert "auto_accepted" in _ACTIONABLE_RESOLUTIONS
        assert "accepted" in _ACTIONABLE_RESOLUTIONS
        assert "overridden" in _ACTIONABLE_RESOLUTIONS

    def test_dismissed_not_actionable(self):
        from devils_advocate.revision import _ACTIONABLE_RESOLUTIONS
        assert "auto_dismissed" not in _ACTIONABLE_RESOLUTIONS
        assert "escalated" not in _ACTIONABLE_RESOLUTIONS


# ═══════════════════════════════════════════════════════════════════════════
# 12. REVISION_MAX_OUTPUT_TOKENS constant
# ═══════════════════════════════════════════════════════════════════════════


class TestRevisionConstants:
    def test_max_output_tokens(self):
        from devils_advocate.revision import REVISION_MAX_OUTPUT_TOKENS
        assert REVISION_MAX_OUTPUT_TOKENS == 64000

    def test_chars_per_token(self):
        from devils_advocate.cost import CHARS_PER_TOKEN
        assert CHARS_PER_TOKEN == 4

    def test_context_window_threshold(self):
        from devils_advocate.cost import CONTEXT_WINDOW_THRESHOLD
        assert CONTEXT_WINDOW_THRESHOLD == 0.8
