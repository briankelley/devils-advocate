"""Tests for devils_advocate.revision module."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from devils_advocate.revision import (
    _extract_revision_strict,
    build_revision_context,
    build_revision_prompt,
    run_revision,
)
from devils_advocate.types import CostTracker, ModelConfig
from devils_advocate.storage import StorageManager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_model():
    return ModelConfig(
        name="test-revision",
        provider="anthropic",
        model_id="test-model",
        api_key_env="TEST_KEY",
        cost_per_1k_input=0.003,
        cost_per_1k_output=0.015,
        context_window=200000,
    )


def _make_ledger(resolutions):
    """Build a minimal ledger dict with points having the given final_resolutions."""
    points = []
    for i, res in enumerate(resolutions, 1):
        points.append({
            "group_id": f"grp_{i:03d}",
            "point_id": f"pt_{i:03d}",
            "description": f"Finding {i} description",
            "recommendation": f"Fix finding {i}",
            "location": f"file.py line {i * 10}",
            "reviewer": f"reviewer_{i}",
            "severity": "high",
            "concern": f"Concern about finding {i}",
            "final_resolution": res,
        })
    return {"points": points, "review_id": "test_review", "mode": "plan"}


# ---------------------------------------------------------------------------
# build_revision_context tests
# ---------------------------------------------------------------------------


class TestBuildRevisionContext:

    def test_actionable_findings_present(self):
        ledger = _make_ledger(["auto_accepted", "auto_dismissed"])
        ctx = build_revision_context(ledger)
        assert "=== ACCEPTED FINDINGS" in ctx
        assert "=== DISMISSED FINDINGS" in ctx
        assert "grp_001" in ctx
        assert "grp_002" in ctx

    def test_no_actionable_findings(self):
        ledger = _make_ledger(["auto_dismissed", "auto_dismissed"])
        ctx = build_revision_context(ledger)
        assert "=== ACCEPTED FINDINGS" not in ctx
        assert "=== DISMISSED FINDINGS" in ctx

    def test_accepted_resolution_is_actionable(self):
        ledger = _make_ledger(["accepted"])
        ctx = build_revision_context(ledger)
        assert "=== ACCEPTED FINDINGS" in ctx

    def test_overridden_resolution_is_actionable(self):
        ledger = _make_ledger(["overridden"])
        ctx = build_revision_context(ledger)
        assert "=== ACCEPTED FINDINGS" in ctx

    def test_escalated_resolution_is_unresolved(self):
        ledger = _make_ledger(["escalated"])
        ctx = build_revision_context(ledger)
        assert "=== ACCEPTED FINDINGS" not in ctx
        assert "=== UNRESOLVED FINDINGS" in ctx

    def test_mixed_resolutions(self):
        ledger = _make_ledger(["auto_accepted", "auto_dismissed", "escalated", "overridden"])
        ctx = build_revision_context(ledger)
        assert "=== ACCEPTED FINDINGS" in ctx
        assert "=== DISMISSED FINDINGS" in ctx
        assert "=== UNRESOLVED FINDINGS" in ctx

    def test_inconsistent_group_resolutions(self):
        """Points in the same group with different resolutions => unresolved."""
        ledger = {
            "points": [
                {
                    "group_id": "grp_001",
                    "point_id": "pt_001",
                    "description": "Finding A",
                    "recommendation": "Fix A",
                    "reviewer": "r1",
                    "severity": "high",
                    "concern": "Concern A",
                    "final_resolution": "auto_accepted",
                },
                {
                    "group_id": "grp_001",
                    "point_id": "pt_002",
                    "description": "Finding B",
                    "recommendation": "Fix B",
                    "reviewer": "r2",
                    "severity": "high",
                    "concern": "Concern A",
                    "final_resolution": "auto_dismissed",
                },
            ]
        }
        ctx = build_revision_context(ledger)
        # Inconsistent resolutions -> treated as unresolved
        assert "=== UNRESOLVED FINDINGS" in ctx
        assert "=== ACCEPTED FINDINGS" not in ctx

    def test_empty_ledger(self):
        ctx = build_revision_context({"points": []})
        assert ctx == ""


# ---------------------------------------------------------------------------
# _extract_revision_strict tests
# ---------------------------------------------------------------------------


class TestExtractRevisionStrict:

    def test_plan_mode_canonical_delimiters(self):
        raw = "Some preamble\n=== REVISED PLAN ===\nRevised content here\n=== END REVISED PLAN ===\nTrailing"
        result = _extract_revision_strict(raw, "plan")
        assert result == "Revised content here"

    def test_code_mode_canonical_delimiters(self):
        raw = "=== UNIFIED DIFF ===\n--- a/file.py\n+++ b/file.py\n=== END UNIFIED DIFF ==="
        result = _extract_revision_strict(raw, "code")
        assert result == "--- a/file.py\n+++ b/file.py"

    def test_integration_mode_canonical_delimiters(self):
        raw = "=== REMEDIATION PLAN ===\nStep 1: Fix X\n=== END REMEDIATION PLAN ==="
        result = _extract_revision_strict(raw, "integration")
        assert result == "Step 1: Fix X"

    def test_no_delimiters_returns_empty(self):
        raw = "This has no delimiters at all, just raw text."
        assert _extract_revision_strict(raw, "plan") == ""

    def test_wrong_mode_delimiters_returns_empty(self):
        raw = "=== UNIFIED DIFF ===\nContent\n=== END UNIFIED DIFF ==="
        assert _extract_revision_strict(raw, "plan") == ""

    def test_no_fallback_to_markdown_headings(self):
        """Strict extractor does NOT fall back to markdown patterns."""
        raw = "## REVISED PLAN\nSome content\n## END"
        assert _extract_revision_strict(raw, "plan") == ""


# ---------------------------------------------------------------------------
# run_revision tests
# ---------------------------------------------------------------------------


class TestRunRevision:

    @pytest.fixture
    def storage(self, tmp_path):
        os.chdir(tmp_path)
        return StorageManager(tmp_path)

    @pytest.fixture
    def model(self):
        return _make_model()

    @pytest.mark.asyncio
    async def test_no_actionable_findings_skips(self, storage, model):
        """No actionable findings => returns empty string without API call."""
        client = AsyncMock()
        ledger = _make_ledger(["auto_dismissed", "auto_dismissed"])
        cost = CostTracker()
        storage.set_review_id("test_review")

        result = await run_revision(
            client, model, "original content", ledger, "plan",
            cost, storage, "test_review",
        )
        assert result == ""
        # No API call should have been made
        client.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_actionable_findings_calls_api(self, storage, model, monkeypatch):
        """Actionable findings => calls API, saves raw, returns extracted."""
        monkeypatch.setenv("TEST_KEY", "fake-key")
        ledger = _make_ledger(["auto_accepted", "auto_dismissed"])
        cost = CostTracker()
        storage.set_review_id("test_review")

        revision_response = (
            "=== REVISED PLAN ===\nRevised plan content\n=== END REVISED PLAN ==="
        )

        with patch("devils_advocate.revision.call_with_retry", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = (revision_response, {"input_tokens": 100, "output_tokens": 50})

            result = await run_revision(
                MagicMock(), model, "original content", ledger, "plan",
                cost, storage, "test_review",
            )

        assert result == "Revised plan content"
        mock_call.assert_called_once()
        # Verify raw was saved
        raw_path = storage.reviews_dir / "test_review" / "revision" / "revision_raw.txt"
        assert raw_path.exists()

    @pytest.mark.asyncio
    async def test_extraction_failure_returns_empty(self, storage, model, monkeypatch):
        """If canonical delimiters are missing, returns empty and does NOT save artifact."""
        monkeypatch.setenv("TEST_KEY", "fake-key")
        ledger = _make_ledger(["auto_accepted"])
        cost = CostTracker()
        storage.set_review_id("test_review")

        bad_response = "This response has no delimiters at all."

        with patch("devils_advocate.revision.call_with_retry", new_callable=AsyncMock) as mock_call:
            mock_call.return_value = (bad_response, {"input_tokens": 100, "output_tokens": 50})

            result = await run_revision(
                MagicMock(), model, "original content", ledger, "plan",
                cost, storage, "test_review",
            )

        assert result == ""
        # Raw should still be saved
        raw_path = storage.reviews_dir / "test_review" / "revision" / "revision_raw.txt"
        assert raw_path.exists()

    @pytest.mark.asyncio
    async def test_api_failure_propagates(self, storage, model, monkeypatch):
        """API failure should propagate (callers wrap in try/except)."""
        monkeypatch.setenv("TEST_KEY", "fake-key")
        ledger = _make_ledger(["auto_accepted"])
        cost = CostTracker()
        storage.set_review_id("test_review")

        with patch("devils_advocate.revision.call_with_retry", new_callable=AsyncMock) as mock_call:
            mock_call.side_effect = Exception("API failure")

            with pytest.raises(Exception, match="API failure"):
                await run_revision(
                    MagicMock(), model, "original content", ledger, "plan",
                    cost, storage, "test_review",
                )
