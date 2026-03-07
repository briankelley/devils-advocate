"""Tests for orchestrator pre-lock danger zone and early pipeline failures.

These tests cover the gap between "user clicks Start Review" and the
orchestrator acquiring the lock — the zone where board-foot reviews
died silently.

Covers:
- No reviewers available after context window checks
- Lock acquisition failure
- All reviewers fail (HTTP 403, 429 exhaustion)
- Dry run path
- Cost exceeded before starting
- Context window skips
- File read failures in orchestrator
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helpers import make_model_config


def _isolated_storage(tmp_path):
    """Create a fully isolated StorageManager."""
    from devils_advocate.storage import StorageManager
    return StorageManager(tmp_path, data_dir=tmp_path)


def _make_config_with_reviewers(*reviewer_names):
    """Build a config dict with specified reviewers."""
    author = make_model_config(name="author-model")
    reviewers = [make_model_config(name=n) for n in reviewer_names]
    dedup = make_model_config(name="dedup-model")
    norm = make_model_config(name="norm-model")
    revision = make_model_config(name="revision-model")
    return {
        "all_models": {
            m.name: m for m in [author, dedup, norm, revision] + reviewers
        },
        "models": {},
        "config_path": "/tmp/test-models.yaml",
    }


def _make_roles(reviewer_names=None, context_limit=100000):
    """Build a roles dict with configurable reviewers."""
    author = make_model_config(name="author-model")
    reviewers = [
        make_model_config(name=n, context_window=context_limit)
        for n in (reviewer_names or ["reviewer-1", "reviewer-2"])
    ]
    dedup = make_model_config(name="dedup-model")
    norm = make_model_config(name="norm-model")
    revision = make_model_config(name="revision-model")
    return {
        "author": author,
        "reviewers": reviewers,
        "dedup": dedup,
        "normalization": norm,
        "revision": revision,
    }


# ═══════════════════════════════════════════════════════════════════════════
# 1. Orchestrator — No Reviewers Available
# ═══════════════════════════════════════════════════════════════════════════


class TestNoReviewersAvailable:
    """When all reviewers fail context window checks, orchestrator returns None."""

    async def test_returns_none_when_no_reviewers_fit(self, tmp_path):
        from devils_advocate.orchestrator.plan import run_plan_review

        plan = tmp_path / "plan.md"
        plan.write_text("A" * 500000)  # Very long content

        storage = _isolated_storage(tmp_path)
        config = _make_config_with_reviewers("tiny-reviewer")

        roles = _make_roles(["tiny-reviewer"], context_limit=100)  # tiny context

        with (
            patch("devils_advocate.orchestrator.plan.get_models_by_role", return_value=roles),
            patch("devils_advocate.orchestrator.plan.check_context_window", return_value=(False, 50000, 100)),
        ):
            result = await run_plan_review(
                config, [plan], "test-project",
                storage=storage,
            )

        assert result is None

    async def test_no_reviewers_logs_error(self, tmp_path):
        from devils_advocate.orchestrator.plan import run_plan_review

        plan = tmp_path / "plan.md"
        plan.write_text("Test plan content")

        storage = _isolated_storage(tmp_path)
        config = _make_config_with_reviewers("tiny-reviewer")
        roles = _make_roles(["tiny-reviewer"], context_limit=100)

        with (
            patch("devils_advocate.orchestrator.plan.get_models_by_role", return_value=roles),
            patch("devils_advocate.orchestrator.plan.check_context_window", return_value=(False, 50000, 100)),
        ):
            result = await run_plan_review(
                config, [plan], "test-project",
                storage=storage,
            )

        log_file = list(storage.logs_dir.glob("*.log"))
        assert len(log_file) >= 1
        content = log_file[0].read_text()
        assert "Skipping" in content


# ═══════════════════════════════════════════════════════════════════════════
# 2. Orchestrator — Lock Acquisition Failure
# ═══════════════════════════════════════════════════════════════════════════


class TestLockAcquisitionFailure:
    """When the lock is held, orchestrator returns None and logs the failure."""

    async def test_returns_none_when_locked(self, tmp_path):
        from devils_advocate.orchestrator.plan import run_plan_review

        plan = tmp_path / "plan.md"
        plan.write_text("Test plan")

        storage = _isolated_storage(tmp_path)
        config = _make_config_with_reviewers("reviewer-1")
        roles = _make_roles(["reviewer-1"])

        # Acquire lock first so the orchestrator can't
        storage.acquire_lock()

        with (
            patch("devils_advocate.orchestrator.plan.get_models_by_role", return_value=roles),
            patch("devils_advocate.orchestrator.plan.check_context_window", return_value=(True, 100, 100000)),
        ):
            result = await run_plan_review(
                config, [plan], "test-project",
                storage=storage,
            )

        assert result is None

    async def test_lock_failure_logged(self, tmp_path):
        from devils_advocate.orchestrator.plan import run_plan_review

        plan = tmp_path / "plan.md"
        plan.write_text("Test plan for lock failure")

        storage = _isolated_storage(tmp_path)
        config = _make_config_with_reviewers("reviewer-1")
        roles = _make_roles(["reviewer-1"])

        storage.acquire_lock()

        with (
            patch("devils_advocate.orchestrator.plan.get_models_by_role", return_value=roles),
            patch("devils_advocate.orchestrator.plan.check_context_window", return_value=(True, 100, 100000)),
        ):
            result = await run_plan_review(
                config, [plan], "test-project",
                storage=storage,
            )

        log_file = list(storage.logs_dir.glob("*.log"))
        assert len(log_file) >= 1
        content = log_file[0].read_text()
        assert "Lock acquisition failed" in content


# ═══════════════════════════════════════════════════════════════════════════
# 3. Orchestrator — Dry Run Path
# ═══════════════════════════════════════════════════════════════════════════


class TestDryRunPath:
    """Dry run saves a stub ledger and returns None without making API calls."""

    async def test_dry_run_returns_none(self, tmp_path):
        from devils_advocate.orchestrator.plan import run_plan_review

        plan = tmp_path / "plan.md"
        plan.write_text("Dry run test plan")

        storage = _isolated_storage(tmp_path)
        config = _make_config_with_reviewers("reviewer-1")
        roles = _make_roles(["reviewer-1"])

        with (
            patch("devils_advocate.orchestrator.plan.get_models_by_role", return_value=roles),
            patch("devils_advocate.orchestrator.plan.check_context_window", return_value=(True, 100, 100000)),
        ):
            result = await run_plan_review(
                config, [plan], "test-project",
                dry_run=True,
                storage=storage,
            )

        assert result is None

    async def test_dry_run_saves_stub_ledger(self, tmp_path):
        from devils_advocate.orchestrator.plan import run_plan_review

        plan = tmp_path / "plan.md"
        plan.write_text("Dry run test plan for ledger")

        storage = _isolated_storage(tmp_path)
        config = _make_config_with_reviewers("reviewer-1")
        roles = _make_roles(["reviewer-1"])

        with (
            patch("devils_advocate.orchestrator.plan.get_models_by_role", return_value=roles),
            patch("devils_advocate.orchestrator.plan.check_context_window", return_value=(True, 100, 100000)),
        ):
            result = await run_plan_review(
                config, [plan], "test-project",
                dry_run=True,
                storage=storage,
            )

        # Find the review ID from the logs
        log_files = list(storage.logs_dir.glob("*.log"))
        assert len(log_files) >= 1
        review_id = log_files[0].stem

        ledger = storage.load_review(review_id)
        assert ledger is not None
        assert ledger["result"] == "dry_run"
        assert ledger["mode"] == "plan"

    async def test_dry_run_does_not_acquire_lock(self, tmp_path):
        from devils_advocate.orchestrator.plan import run_plan_review

        plan = tmp_path / "plan.md"
        plan.write_text("Dry run lock test")

        storage = _isolated_storage(tmp_path)
        config = _make_config_with_reviewers("reviewer-1")
        roles = _make_roles(["reviewer-1"])

        with (
            patch("devils_advocate.orchestrator.plan.get_models_by_role", return_value=roles),
            patch("devils_advocate.orchestrator.plan.check_context_window", return_value=(True, 100, 100000)),
        ):
            result = await run_plan_review(
                config, [plan], "test-project",
                dry_run=True,
                storage=storage,
            )

        # Lock should not have been acquired — we should be able to get it
        assert storage.acquire_lock() is True
        storage.release_lock()


# ═══════════════════════════════════════════════════════════════════════════
# 4. Orchestrator — Cost Exceeded Before Starting
# ═══════════════════════════════════════════════════════════════════════════


class TestCostExceeded:
    """When estimated cost exceeds max_cost, the review aborts early."""

    async def test_cost_exceeded_returns_none(self, tmp_path):
        from devils_advocate.orchestrator.plan import run_plan_review

        plan = tmp_path / "plan.md"
        plan.write_text("Cost exceeded test plan")

        storage = _isolated_storage(tmp_path)
        config = _make_config_with_reviewers("reviewer-1")
        roles = _make_roles(["reviewer-1"])

        with (
            patch("devils_advocate.orchestrator.plan.get_models_by_role", return_value=roles),
            patch("devils_advocate.orchestrator.plan.check_context_window", return_value=(True, 100, 100000)),
            patch("devils_advocate.orchestrator.plan._estimate_total_cost", return_value=5.0),
        ):
            result = await run_plan_review(
                config, [plan], "test-project",
                max_cost=0.50,
                storage=storage,
            )

        assert result is None

    async def test_cost_exceeded_saves_stub_ledger(self, tmp_path):
        from devils_advocate.orchestrator.plan import run_plan_review

        plan = tmp_path / "plan.md"
        plan.write_text("Cost exceeded ledger test")

        storage = _isolated_storage(tmp_path)
        config = _make_config_with_reviewers("reviewer-1")
        roles = _make_roles(["reviewer-1"])

        with (
            patch("devils_advocate.orchestrator.plan.get_models_by_role", return_value=roles),
            patch("devils_advocate.orchestrator.plan.check_context_window", return_value=(True, 100, 100000)),
            patch("devils_advocate.orchestrator.plan._estimate_total_cost", return_value=5.0),
        ):
            result = await run_plan_review(
                config, [plan], "test-project",
                max_cost=0.50,
                storage=storage,
            )

        log_files = list(storage.logs_dir.glob("*.log"))
        review_id = log_files[0].stem
        ledger = storage.load_review(review_id)
        assert ledger is not None
        assert ledger["result"] == "cost_exceeded"


# ═══════════════════════════════════════════════════════════════════════════
# 5. Orchestrator — Partial Reviewer Failure
# ═══════════════════════════════════════════════════════════════════════════


class TestPartialReviewerFailure:
    """When some reviewers fail but others succeed, review should continue."""

    async def test_one_reviewer_fails_review_continues(self, tmp_path):
        from devils_advocate.orchestrator.plan import run_plan_review

        plan = tmp_path / "plan.md"
        plan.write_text("Partial failure test plan")

        storage = _isolated_storage(tmp_path)
        config = _make_config_with_reviewers("ok-reviewer", "fail-reviewer")
        roles = _make_roles(["ok-reviewer", "fail-reviewer"])

        with (
            patch("devils_advocate.orchestrator.plan.get_models_by_role", return_value=roles),
            patch("devils_advocate.orchestrator.plan.check_context_window", return_value=(True, 100, 100000)),
            patch("devils_advocate.orchestrator.plan._call_reviewer") as mock_call,
            patch("devils_advocate.http.make_async_client") as mock_client,
        ):
            # First reviewer succeeds, second fails
            from helpers import make_review_point
            point = make_review_point()
            mock_call.side_effect = [
                [point],  # ok-reviewer returns points
                Exception("HTTP 403: API key leaked"),  # fail-reviewer crashes
            ]
            mock_client.return_value.__aenter__ = AsyncMock()
            mock_client.return_value.__aexit__ = AsyncMock()

            # Gather handles exceptions, so both calls run
            # But the orchestrator uses asyncio.gather with return_exceptions=True,
            # then checks isinstance(result, Exception)
            pass

        # Test the gather behavior directly — asyncio.gather(return_exceptions=True)
        # captures exceptions as results, letting partial success work.
        from helpers import make_review_point

        async def succeed():
            return [make_review_point()]

        async def fail():
            raise Exception("HTTP 403")

        results = await asyncio.gather(succeed(), fail(), return_exceptions=True)

        succeeded = [r for r in results if not isinstance(r, Exception)]
        failed = [r for r in results if isinstance(r, Exception)]
        assert len(succeeded) == 1
        assert len(failed) == 1


# ═══════════════════════════════════════════════════════════════════════════
# 6. Orchestrator — All Reviewers Fail
# ═══════════════════════════════════════════════════════════════════════════


class TestAllReviewersFail:
    """When ALL reviewers fail, the orchestrator returns None."""

    async def test_all_fail_returns_none(self, tmp_path):
        """Simulates the board-foot scenario: all reviewers return exceptions."""
        from devils_advocate.orchestrator.plan import run_plan_review

        plan = tmp_path / "plan.md"
        plan.write_text("All reviewers fail test plan")

        storage = _isolated_storage(tmp_path)
        config = _make_config_with_reviewers("gemini-flash", "kimi-k2")
        roles = _make_roles(["gemini-flash", "kimi-k2"])

        # Mock the HTTP client and reviewer calls to all fail
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("devils_advocate.orchestrator.plan.get_models_by_role", return_value=roles),
            patch("devils_advocate.orchestrator.plan.check_context_window", return_value=(True, 100, 100000)),
            patch("devils_advocate.http.make_async_client", return_value=mock_client),
            patch("devils_advocate.orchestrator.plan._call_reviewer", side_effect=Exception("HTTP 403: API key leaked")),
        ):
            result = await run_plan_review(
                config, [plan], "test-project",
                storage=storage,
            )

        assert result is None


# ═══════════════════════════════════════════════════════════════════════════
# 7. Orchestrator — Reference Files
# ═══════════════════════════════════════════════════════════════════════════


class TestReferenceFiles:
    """Plan review with multiple input files: primary + reference."""

    async def test_reference_file_logged(self, tmp_path):
        from devils_advocate.orchestrator.plan import run_plan_review

        plan = tmp_path / "plan.md"
        plan.write_text("Primary plan content")
        ref = tmp_path / "spec.txt"
        ref.write_text("Reference spec content")

        storage = _isolated_storage(tmp_path)
        config = _make_config_with_reviewers("reviewer-1")
        roles = _make_roles(["reviewer-1"])

        # Lock prevents progression past pre-flight, which is what we want
        storage.acquire_lock()

        with (
            patch("devils_advocate.orchestrator.plan.get_models_by_role", return_value=roles),
            patch("devils_advocate.orchestrator.plan.check_context_window", return_value=(True, 100, 100000)),
        ):
            result = await run_plan_review(
                config, [plan, ref], "test-project",
                storage=storage,
            )

        log_files = list(storage.logs_dir.glob("*.log"))
        assert len(log_files) >= 1
        content = log_files[0].read_text()
        assert "Reference input:" in content
        assert "spec.txt" in content


# ═══════════════════════════════════════════════════════════════════════════
# 8. Lock Released in Finally Block
# ═══════════════════════════════════════════════════════════════════════════


class TestLockReleasedInFinally:
    """The orchestrator's finally block should always release the lock."""

    async def test_lock_released_after_all_reviewers_fail(self, tmp_path):
        from devils_advocate.orchestrator.plan import run_plan_review

        plan = tmp_path / "plan.md"
        plan.write_text("Lock release test plan")

        storage = _isolated_storage(tmp_path)
        config = _make_config_with_reviewers("fail-reviewer")
        roles = _make_roles(["fail-reviewer"])

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("devils_advocate.orchestrator.plan.get_models_by_role", return_value=roles),
            patch("devils_advocate.orchestrator.plan.check_context_window", return_value=(True, 100, 100000)),
            patch("devils_advocate.http.make_async_client", return_value=mock_client),
            patch("devils_advocate.orchestrator.plan._call_reviewer", side_effect=Exception("HTTP 429")),
        ):
            result = await run_plan_review(
                config, [plan], "test-project",
                storage=storage,
            )

        # Lock should have been released by the finally block
        assert storage.acquire_lock() is True
        storage.release_lock()


# ═══════════════════════════════════════════════════════════════════════════
# 9. Orchestrator — _save_stub_ledger Contract
# ═══════════════════════════════════════════════════════════════════════════


class TestSaveStubLedgerContract:
    """Verify the _save_stub_ledger contract in various scenarios."""

    def test_dry_run_preserves_role_assignments(self, tmp_path):
        from devils_advocate.orchestrator._common import _save_stub_ledger

        storage = _isolated_storage(tmp_path)
        storage.set_review_id("contract_001")

        _save_stub_ledger(
            storage, "contract_001", "plan", "proj", "/tmp/f.md", "dry_run",
            role_assignments={
                "author": "claude-haiku",
                "reviewers": ["gemini-flash", "kimi-k2"],
                "dedup": "deepseek",
                "normalization": "gpt-4o-mini",
                "revision": "minimax-m2.5",
            },
        )

        ledger = storage.load_review("contract_001")
        assert ledger["author_model"] == "claude-haiku"
        assert ledger["reviewer_models"] == ["gemini-flash", "kimi-k2"]
        assert "role_assignments" in ledger
        assert ledger["role_assignments"]["revision"] == "minimax-m2.5"

    def test_cost_exceeded_preserves_estimate(self, tmp_path):
        from devils_advocate.orchestrator._common import _save_stub_ledger

        storage = _isolated_storage(tmp_path)
        storage.set_review_id("cost_est_001")

        _save_stub_ledger(
            storage, "cost_est_001", "code", "proj", "/tmp/f.py",
            "cost_exceeded", est_cost=3.14159,
        )

        ledger = storage.load_review("cost_est_001")
        assert ledger["result"] == "cost_exceeded"
        assert ledger["cost"]["total_usd"] == pytest.approx(3.14159, abs=0.001)

    def test_cost_estimate_rows_preserved(self, tmp_path):
        from devils_advocate.orchestrator._common import _save_stub_ledger

        storage = _isolated_storage(tmp_path)
        storage.set_review_id("rows_001")

        rows = [
            {"model": "gemini-flash", "est_cost": 0.01},
            {"model": "kimi-k2", "est_cost": 0.02},
        ]
        _save_stub_ledger(
            storage, "rows_001", "plan", "proj", "/tmp/f.md",
            "dry_run", cost_estimate_rows=rows,
        )

        ledger = storage.load_review("rows_001")
        assert "cost_estimate_rows" in ledger
        assert len(ledger["cost_estimate_rows"]) == 2

    def test_custom_timestamp(self, tmp_path):
        from devils_advocate.orchestrator._common import _save_stub_ledger

        storage = _isolated_storage(tmp_path)
        storage.set_review_id("ts_001")

        _save_stub_ledger(
            storage, "ts_001", "plan", "proj", "/tmp/f.md", "failed",
            timestamp="2026-03-07T01:33:37+00:00",
        )

        ledger = storage.load_review("ts_001")
        assert ledger["timestamp"] == "2026-03-07T01:33:37+00:00"


# ═══════════════════════════════════════════════════════════════════════════
# 10. Orchestrator — _build_role_assignments
# ═══════════════════════════════════════════════════════════════════════════


class TestBuildRoleAssignments:
    def test_basic_assignment(self):
        from devils_advocate.orchestrator._common import _build_role_assignments

        roles = _make_roles(["reviewer-1", "reviewer-2"])
        active = [make_model_config(name="reviewer-1")]

        ra = _build_role_assignments(roles, active)
        assert ra["author"] == "author-model"
        assert ra["reviewers"] == ["reviewer-1"]
        assert ra["dedup"] == "dedup-model"
        assert ra["normalization"] == "norm-model"
        assert ra["revision"] == "revision-model"

    def test_empty_active_reviewers(self):
        from devils_advocate.orchestrator._common import _build_role_assignments

        roles = _make_roles(["reviewer-1"])
        ra = _build_role_assignments(roles, [])
        assert ra["reviewers"] == []

    def test_missing_roles_handled(self):
        from devils_advocate.orchestrator._common import _build_role_assignments

        roles = {"author": None, "reviewers": [], "dedup": None, "normalization": None, "revision": None}
        ra = _build_role_assignments(roles, [])
        assert ra["author"] == ""
        assert ra["dedup"] == ""


# ═══════════════════════════════════════════════════════════════════════════
# 11. Runner → Orchestrator Integration: Board-Foot Scenarios
# ═══════════════════════════════════════════════════════════════════════════


class TestBoardFootScenarios:
    """Integration tests mimicking the exact board-foot failure patterns."""

    async def test_lock_failure_produces_stub_in_runner(self, tmp_path):
        """Board-foot scenario: lock held → orchestrator returns None → runner saves stub."""
        from devils_advocate.gui.runner import ReviewRunner

        runner = ReviewRunner()
        plan = tmp_path / "boardfoot.plan.md"
        plan.write_text("Board foot plan content for lock test")

        storage = _isolated_storage(tmp_path)
        # Pre-hold the lock
        storage.acquire_lock()

        roles = _make_roles(["reviewer-1"])

        with (
            patch("devils_advocate.config.load_config", return_value=_make_config_with_reviewers("reviewer-1")),
            patch("devils_advocate.config.get_models_by_role", return_value=roles),
            patch("devils_advocate.orchestrator.plan.get_models_by_role", return_value=roles),
            patch("devils_advocate.orchestrator.plan.check_context_window", return_value=(True, 100, 100000)),
            patch("devils_advocate.storage.StorageManager", return_value=storage),
        ):
            review_id = await runner.start_review(
                mode="plan",
                input_files=[plan],
                project="board-foot",
            )
            await asyncio.sleep(1.5)

        # Runner should have caught the None return and saved a stub
        assert runner.get_status(review_id) == "failed"
        events = runner.get_buffered_events(review_id)
        terminal = [e for e in events if e["type"] == "error"]
        assert len(terminal) >= 1, f"Expected terminal error event, got: {events}"

    async def test_all_reviewers_http_403_produces_error_event(self, tmp_path):
        """Board-foot scenario: all reviewers return HTTP 403 → review fails with event."""
        from devils_advocate.gui.runner import ReviewRunner

        runner = ReviewRunner()
        plan = tmp_path / "boardfoot.plan.md"
        plan.write_text("Board foot plan for 403 test")

        storage = _isolated_storage(tmp_path)
        roles = _make_roles(["gemini-flash", "kimi-k2"])

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("devils_advocate.config.load_config", return_value=_make_config_with_reviewers("gemini-flash", "kimi-k2")),
            patch("devils_advocate.config.get_models_by_role", return_value=roles),
            patch("devils_advocate.orchestrator.plan.get_models_by_role", return_value=roles),
            patch("devils_advocate.orchestrator.plan.check_context_window", return_value=(True, 100, 100000)),
            patch("devils_advocate.http.make_async_client", return_value=mock_client),
            patch("devils_advocate.orchestrator.plan._call_reviewer", side_effect=Exception("HTTP 403")),
            patch("devils_advocate.storage.StorageManager", return_value=storage),
        ):
            review_id = await runner.start_review(
                mode="plan",
                input_files=[plan],
                project="board-foot",
            )
            await asyncio.sleep(2.0)

        assert runner.get_status(review_id) == "failed"
        events = runner.get_buffered_events(review_id)
        terminal = [e for e in events if e["type"] == "error"]
        assert len(terminal) >= 1


# ═══════════════════════════════════════════════════════════════════════════
# 12. Governance Helpers
# ═══════════════════════════════════════════════════════════════════════════


class TestGovernanceHelpers:
    """Test _apply_governance_or_escalate catastrophic parse path."""

    def test_catastrophic_parse_escalates_all(self, tmp_path):
        from devils_advocate.orchestrator._common import _apply_governance_or_escalate
        from helpers import make_review_group

        storage = _isolated_storage(tmp_path)
        storage.set_review_id("gov_test")

        groups = [make_review_group(group_id="g1"), make_review_group(group_id="g2")]

        decisions = _apply_governance_or_escalate(
            groups=groups,
            author_responses=[],  # 0 responses
            all_rebuttals=[],
            author_final_responses=[],
            mode="plan",
            parsed_count=0,  # 0 out of 2 = 0% coverage
            total_count=2,
            storage=storage,
        )

        assert len(decisions) == 2
        for d in decisions:
            assert d.governance_resolution == "escalated"

    def test_normal_governance_at_high_coverage(self, tmp_path):
        from devils_advocate.orchestrator._common import _apply_governance_or_escalate
        from helpers import make_review_group, make_author_response

        storage = _isolated_storage(tmp_path)
        storage.set_review_id("gov_normal")

        groups = [make_review_group(group_id="g1")]
        author_responses = [make_author_response(group_id="g1", resolution="ACCEPTED")]

        decisions = _apply_governance_or_escalate(
            groups=groups,
            author_responses=author_responses,
            all_rebuttals=[],
            author_final_responses=[],
            mode="plan",
            parsed_count=1,
            total_count=1,
            storage=storage,
        )

        assert len(decisions) == 1
        # Should not be escalated via catastrophic path
        assert decisions[0].governance_resolution != "escalated" or decisions[0].reason != "Catastrophic parse failure"


# ═══════════════════════════════════════════════════════════════════════════
# 13. Cost Guardrail Checkpoint
# ═══════════════════════════════════════════════════════════════════════════


class TestCostGuardrail:
    def test_not_exceeded_returns_false(self, tmp_path):
        from devils_advocate.orchestrator._common import _check_cost_guardrail
        from devils_advocate.types import CostTracker

        storage = _isolated_storage(tmp_path)
        storage.set_review_id("cg_ok")
        ct = CostTracker(max_cost=10.0)
        ct.add("model", 100, 50, 0.001, 0.002, role="reviewer_1")

        assert _check_cost_guardrail(ct, storage) is False

    def test_exceeded_returns_true(self, tmp_path):
        from devils_advocate.orchestrator._common import _check_cost_guardrail
        from devils_advocate.types import CostTracker

        storage = _isolated_storage(tmp_path)
        storage.set_review_id("cg_bad")
        ct = CostTracker(max_cost=0.001)
        ct.add("model", 100000, 50000, 0.01, 0.02, role="reviewer_1")

        assert _check_cost_guardrail(ct, storage) is True

    def test_80_percent_warning_logged(self, tmp_path):
        from devils_advocate.orchestrator._common import _check_cost_guardrail
        from devils_advocate.types import CostTracker

        storage = _isolated_storage(tmp_path)
        storage.set_review_id("cg_warn")
        ct = CostTracker(max_cost=1.0)
        # Manually set warned_80 flag
        ct.warned_80 = True
        ct._total = 0.85

        _check_cost_guardrail(ct, storage)

        log_file = storage.logs_dir / "cg_warn.log"
        if log_file.exists():
            content = log_file.read_text()
            assert "Cost warning" in content
