"""Tests for code, spec, and integration orchestrator pre-flight paths.

These complement test_orchestrator_preflight.py (which focuses on plan mode)
by covering the same danger zones in the other three orchestrator modules.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helpers import make_model_config, make_review_point


def _isolated_storage(tmp_path):
    from devils_advocate.storage import StorageManager
    return StorageManager(tmp_path, data_dir=tmp_path)


def _make_roles(reviewer_names=None, context_limit=100000, with_integration=False):
    author = make_model_config(name="author-model")
    reviewers = [
        make_model_config(name=n, context_window=context_limit)
        for n in (reviewer_names or ["reviewer-1", "reviewer-2"])
    ]
    dedup = make_model_config(name="dedup-model")
    norm = make_model_config(name="norm-model")
    revision = make_model_config(name="revision-model")
    roles = {
        "author": author,
        "reviewers": reviewers,
        "dedup": dedup,
        "normalization": norm,
        "revision": revision,
        "integration": make_model_config(name="integ-model", context_window=context_limit) if with_integration else None,
    }
    return roles


def _make_config_with_reviewers(*reviewer_names):
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


# ═══════════════════════════════════════════════════════════════════════════
# 1. Code Review — No Reviewers Available
# ═══════════════════════════════════════════════════════════════════════════


class TestCodeReviewNoReviewers:
    async def test_returns_none_when_no_reviewers_fit(self, tmp_path):
        from devils_advocate.orchestrator.code import run_code_review

        code_file = tmp_path / "app.py"
        code_file.write_text("print('hello world')")

        storage = _isolated_storage(tmp_path)
        config = _make_config_with_reviewers("tiny-reviewer")
        roles = _make_roles(["tiny-reviewer"], context_limit=100)

        with (
            patch("devils_advocate.orchestrator.code.get_models_by_role", return_value=roles),
            patch("devils_advocate.orchestrator.code.check_context_window", return_value=(False, 50000, 100)),
        ):
            result = await run_code_review(config, code_file, "test-proj", storage=storage)

        assert result is None

    async def test_code_no_reviewers_logs_skipping(self, tmp_path):
        from devils_advocate.orchestrator.code import run_code_review

        code_file = tmp_path / "app.py"
        code_file.write_text("print('skipping test')")

        storage = _isolated_storage(tmp_path)
        config = _make_config_with_reviewers("tiny-reviewer")
        roles = _make_roles(["tiny-reviewer"], context_limit=100)

        with (
            patch("devils_advocate.orchestrator.code.get_models_by_role", return_value=roles),
            patch("devils_advocate.orchestrator.code.check_context_window", return_value=(False, 50000, 100)),
        ):
            await run_code_review(config, code_file, "test-proj", storage=storage)

        log_files = list(storage.logs_dir.glob("*.log"))
        assert len(log_files) >= 1
        content = log_files[0].read_text()
        assert "Skipping" in content


# ═══════════════════════════════════════════════════════════════════════════
# 2. Code Review — Lock Failure
# ═══════════════════════════════════════════════════════════════════════════


class TestCodeReviewLockFailure:
    async def test_returns_none_when_locked(self, tmp_path):
        from devils_advocate.orchestrator.code import run_code_review

        code_file = tmp_path / "locked.py"
        code_file.write_text("locked = True")

        storage = _isolated_storage(tmp_path)
        config = _make_config_with_reviewers("reviewer-1")
        roles = _make_roles(["reviewer-1"])
        storage.acquire_lock()

        with (
            patch("devils_advocate.orchestrator.code.get_models_by_role", return_value=roles),
            patch("devils_advocate.orchestrator.code.check_context_window", return_value=(True, 100, 100000)),
        ):
            result = await run_code_review(config, code_file, "test-proj", storage=storage)

        assert result is None

    async def test_lock_failure_logged(self, tmp_path):
        from devils_advocate.orchestrator.code import run_code_review

        code_file = tmp_path / "lock_log.py"
        code_file.write_text("x = 1")

        storage = _isolated_storage(tmp_path)
        config = _make_config_with_reviewers("reviewer-1")
        roles = _make_roles(["reviewer-1"])
        storage.acquire_lock()

        with (
            patch("devils_advocate.orchestrator.code.get_models_by_role", return_value=roles),
            patch("devils_advocate.orchestrator.code.check_context_window", return_value=(True, 100, 100000)),
        ):
            await run_code_review(config, code_file, "test-proj", storage=storage)

        log_files = list(storage.logs_dir.glob("*.log"))
        content = log_files[0].read_text()
        assert "Lock acquisition failed" in content


# ═══════════════════════════════════════════════════════════════════════════
# 3. Code Review — Dry Run
# ═══════════════════════════════════════════════════════════════════════════


class TestCodeReviewDryRun:
    async def test_dry_run_returns_none(self, tmp_path):
        from devils_advocate.orchestrator.code import run_code_review

        code_file = tmp_path / "dry.py"
        code_file.write_text("dry_run = True")

        storage = _isolated_storage(tmp_path)
        config = _make_config_with_reviewers("reviewer-1")
        roles = _make_roles(["reviewer-1"])

        with (
            patch("devils_advocate.orchestrator.code.get_models_by_role", return_value=roles),
            patch("devils_advocate.orchestrator.code.check_context_window", return_value=(True, 100, 100000)),
        ):
            result = await run_code_review(
                config, code_file, "test-proj", dry_run=True, storage=storage,
            )

        assert result is None

    async def test_dry_run_saves_stub(self, tmp_path):
        from devils_advocate.orchestrator.code import run_code_review

        code_file = tmp_path / "dry_stub.py"
        code_file.write_text("stub_test = True")

        storage = _isolated_storage(tmp_path)
        config = _make_config_with_reviewers("reviewer-1")
        roles = _make_roles(["reviewer-1"])

        with (
            patch("devils_advocate.orchestrator.code.get_models_by_role", return_value=roles),
            patch("devils_advocate.orchestrator.code.check_context_window", return_value=(True, 100, 100000)),
        ):
            await run_code_review(
                config, code_file, "test-proj", dry_run=True, storage=storage,
            )

        log_files = list(storage.logs_dir.glob("*.log"))
        review_id = log_files[0].stem
        ledger = storage.load_review(review_id)
        assert ledger is not None
        assert ledger["result"] == "dry_run"
        assert ledger["mode"] == "code"


# ═══════════════════════════════════════════════════════════════════════════
# 4. Code Review — Cost Exceeded
# ═══════════════════════════════════════════════════════════════════════════


class TestCodeReviewCostExceeded:
    async def test_cost_exceeded_returns_none(self, tmp_path):
        from devils_advocate.orchestrator.code import run_code_review

        code_file = tmp_path / "costly.py"
        code_file.write_text("cost = 999")

        storage = _isolated_storage(tmp_path)
        config = _make_config_with_reviewers("reviewer-1")
        roles = _make_roles(["reviewer-1"])

        with (
            patch("devils_advocate.orchestrator.code.get_models_by_role", return_value=roles),
            patch("devils_advocate.orchestrator.code.check_context_window", return_value=(True, 100, 100000)),
            patch("devils_advocate.orchestrator.code._estimate_total_cost", return_value=5.0),
        ):
            result = await run_code_review(
                config, code_file, "test-proj", max_cost=0.50, storage=storage,
            )

        assert result is None

    async def test_cost_exceeded_saves_stub(self, tmp_path):
        from devils_advocate.orchestrator.code import run_code_review

        code_file = tmp_path / "cost_stub.py"
        code_file.write_text("cost_stub = True")

        storage = _isolated_storage(tmp_path)
        config = _make_config_with_reviewers("reviewer-1")
        roles = _make_roles(["reviewer-1"])

        with (
            patch("devils_advocate.orchestrator.code.get_models_by_role", return_value=roles),
            patch("devils_advocate.orchestrator.code.check_context_window", return_value=(True, 100, 100000)),
            patch("devils_advocate.orchestrator.code._estimate_total_cost", return_value=5.0),
        ):
            await run_code_review(
                config, code_file, "test-proj", max_cost=0.50, storage=storage,
            )

        log_files = list(storage.logs_dir.glob("*.log"))
        review_id = log_files[0].stem
        ledger = storage.load_review(review_id)
        assert ledger["result"] == "cost_exceeded"


# ═══════════════════════════════════════════════════════════════════════════
# 5. Code Review — All Reviewers Fail
# ═══════════════════════════════════════════════════════════════════════════


class TestCodeReviewAllReviewersFail:
    async def test_all_fail_returns_none(self, tmp_path):
        from devils_advocate.orchestrator.code import run_code_review

        code_file = tmp_path / "all_fail.py"
        code_file.write_text("fail = True")

        storage = _isolated_storage(tmp_path)
        config = _make_config_with_reviewers("reviewer-1")
        roles = _make_roles(["reviewer-1"])

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("devils_advocate.orchestrator.code.get_models_by_role", return_value=roles),
            patch("devils_advocate.orchestrator.code.check_context_window", return_value=(True, 100, 100000)),
            patch("devils_advocate.http.make_async_client", return_value=mock_client),
            patch("devils_advocate.orchestrator.code._call_reviewer", side_effect=Exception("HTTP 429")),
        ):
            result = await run_code_review(config, code_file, "test-proj", storage=storage)

        assert result is None


# ═══════════════════════════════════════════════════════════════════════════
# 6. Code Review — Lock Released in Finally
# ═══════════════════════════════════════════════════════════════════════════


class TestCodeReviewLockRelease:
    async def test_lock_released_after_failure(self, tmp_path):
        from devils_advocate.orchestrator.code import run_code_review

        code_file = tmp_path / "lock_release.py"
        code_file.write_text("release = True")

        storage = _isolated_storage(tmp_path)
        config = _make_config_with_reviewers("fail-reviewer")
        roles = _make_roles(["fail-reviewer"])

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("devils_advocate.orchestrator.code.get_models_by_role", return_value=roles),
            patch("devils_advocate.orchestrator.code.check_context_window", return_value=(True, 100, 100000)),
            patch("devils_advocate.http.make_async_client", return_value=mock_client),
            patch("devils_advocate.orchestrator.code._call_reviewer", side_effect=Exception("boom")),
        ):
            await run_code_review(config, code_file, "test-proj", storage=storage)

        # Lock released in finally block
        assert storage.acquire_lock() is True
        storage.release_lock()


# ═══════════════════════════════════════════════════════════════════════════
# 7. Code Review — with Spec File
# ═══════════════════════════════════════════════════════════════════════════


class TestCodeReviewWithSpec:
    async def test_spec_file_logged(self, tmp_path):
        from devils_advocate.orchestrator.code import run_code_review

        code_file = tmp_path / "app.py"
        code_file.write_text("def main(): pass")
        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Spec\nDo thing X")

        storage = _isolated_storage(tmp_path)
        config = _make_config_with_reviewers("reviewer-1")
        roles = _make_roles(["reviewer-1"])
        storage.acquire_lock()  # block before API calls

        with (
            patch("devils_advocate.orchestrator.code.get_models_by_role", return_value=roles),
            patch("devils_advocate.orchestrator.code.check_context_window", return_value=(True, 100, 100000)),
        ):
            await run_code_review(
                config, code_file, "test-proj",
                spec_file=spec_file, storage=storage,
            )

        log_files = list(storage.logs_dir.glob("*.log"))
        content = log_files[0].read_text()
        assert "Spec:" in content


# ═══════════════════════════════════════════════════════════════════════════
# 8. Spec Review — No Reviewers Available
# ═══════════════════════════════════════════════════════════════════════════


class TestSpecReviewNoReviewers:
    async def test_returns_none_when_no_reviewers_fit(self, tmp_path):
        from devils_advocate.orchestrator.spec import run_spec_review

        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Spec content")

        storage = _isolated_storage(tmp_path)
        config = _make_config_with_reviewers("tiny-reviewer")
        roles = _make_roles(["tiny-reviewer"], context_limit=100)

        with (
            patch("devils_advocate.orchestrator.spec.get_models_by_role", return_value=roles),
            patch("devils_advocate.orchestrator.spec.check_context_window", return_value=(False, 50000, 100)),
        ):
            result = await run_spec_review(config, [spec_file], "test-proj", storage=storage)

        assert result is None


# ═══════════════════════════════════════════════════════════════════════════
# 9. Spec Review — Dry Run
# ═══════════════════════════════════════════════════════════════════════════


class TestSpecReviewDryRun:
    async def test_dry_run_returns_none(self, tmp_path):
        from devils_advocate.orchestrator.spec import run_spec_review

        spec_file = tmp_path / "spec.md"
        spec_file.write_text("# Spec dry run")

        storage = _isolated_storage(tmp_path)
        config = _make_config_with_reviewers("reviewer-1")
        roles = _make_roles(["reviewer-1"])

        with (
            patch("devils_advocate.orchestrator.spec.get_models_by_role", return_value=roles),
            patch("devils_advocate.orchestrator.spec.check_context_window", return_value=(True, 100, 100000)),
        ):
            result = await run_spec_review(
                config, [spec_file], "test-proj",
                dry_run=True, storage=storage,
            )

        assert result is None

    async def test_dry_run_saves_stub_ledger(self, tmp_path):
        from devils_advocate.orchestrator.spec import run_spec_review

        spec_file = tmp_path / "spec_stub.md"
        spec_file.write_text("# Spec stub")

        storage = _isolated_storage(tmp_path)
        config = _make_config_with_reviewers("reviewer-1")
        roles = _make_roles(["reviewer-1"])

        with (
            patch("devils_advocate.orchestrator.spec.get_models_by_role", return_value=roles),
            patch("devils_advocate.orchestrator.spec.check_context_window", return_value=(True, 100, 100000)),
        ):
            await run_spec_review(
                config, [spec_file], "test-proj",
                dry_run=True, storage=storage,
            )

        log_files = list(storage.logs_dir.glob("*.log"))
        review_id = log_files[0].stem
        ledger = storage.load_review(review_id)
        assert ledger is not None
        assert ledger["result"] == "dry_run"
        assert ledger["mode"] == "spec"


# ═══════════════════════════════════════════════════════════════════════════
# 10. Spec Review — Cost Exceeded
# ═══════════════════════════════════════════════════════════════════════════


class TestSpecReviewCostExceeded:
    async def test_cost_exceeded_returns_none(self, tmp_path):
        from devils_advocate.orchestrator.spec import run_spec_review

        spec_file = tmp_path / "spec_cost.md"
        spec_file.write_text("# Cost exceeded spec")

        storage = _isolated_storage(tmp_path)
        config = _make_config_with_reviewers("reviewer-1")
        roles = _make_roles(["reviewer-1"])

        with (
            patch("devils_advocate.orchestrator.spec.get_models_by_role", return_value=roles),
            patch("devils_advocate.orchestrator.spec.check_context_window", return_value=(True, 100, 100000)),
            patch("devils_advocate.orchestrator.spec._estimate_spec_cost", return_value=5.0),
        ):
            result = await run_spec_review(
                config, [spec_file], "test-proj",
                max_cost=0.50, storage=storage,
            )

        assert result is None


# ═══════════════════════════════════════════════════════════════════════════
# 11. Spec Review — Lock Failure
# ═══════════════════════════════════════════════════════════════════════════


class TestSpecReviewLockFailure:
    async def test_lock_failure_returns_none(self, tmp_path):
        from devils_advocate.orchestrator.spec import run_spec_review

        spec_file = tmp_path / "spec_lock.md"
        spec_file.write_text("# Lock failure spec")

        storage = _isolated_storage(tmp_path)
        config = _make_config_with_reviewers("reviewer-1")
        roles = _make_roles(["reviewer-1"])
        storage.acquire_lock()

        with (
            patch("devils_advocate.orchestrator.spec.get_models_by_role", return_value=roles),
            patch("devils_advocate.orchestrator.spec.check_context_window", return_value=(True, 100, 100000)),
        ):
            result = await run_spec_review(config, [spec_file], "test-proj", storage=storage)

        assert result is None


# ═══════════════════════════════════════════════════════════════════════════
# 12. Spec Review — All Reviewers Fail
# ═══════════════════════════════════════════════════════════════════════════


class TestSpecReviewAllReviewersFail:
    async def test_all_fail_returns_none(self, tmp_path):
        from devils_advocate.orchestrator.spec import run_spec_review

        spec_file = tmp_path / "spec_fail.md"
        spec_file.write_text("# All fail spec")

        storage = _isolated_storage(tmp_path)
        config = _make_config_with_reviewers("reviewer-1")
        roles = _make_roles(["reviewer-1"])

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("devils_advocate.orchestrator.spec.get_models_by_role", return_value=roles),
            patch("devils_advocate.orchestrator.spec.check_context_window", return_value=(True, 100, 100000)),
            patch("devils_advocate.http.make_async_client", return_value=mock_client),
            patch("devils_advocate.orchestrator.spec._call_reviewer", side_effect=Exception("HTTP 403")),
        ):
            result = await run_spec_review(config, [spec_file], "test-proj", storage=storage)

        assert result is None


# ═══════════════════════════════════════════════════════════════════════════
# 13. Spec Review — Lock Released in Finally
# ═══════════════════════════════════════════════════════════════════════════


class TestSpecReviewLockRelease:
    async def test_lock_released_after_failure(self, tmp_path):
        from devils_advocate.orchestrator.spec import run_spec_review

        spec_file = tmp_path / "spec_release.md"
        spec_file.write_text("# Release spec")

        storage = _isolated_storage(tmp_path)
        config = _make_config_with_reviewers("fail-reviewer")
        roles = _make_roles(["fail-reviewer"])

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("devils_advocate.orchestrator.spec.get_models_by_role", return_value=roles),
            patch("devils_advocate.orchestrator.spec.check_context_window", return_value=(True, 100, 100000)),
            patch("devils_advocate.http.make_async_client", return_value=mock_client),
            patch("devils_advocate.orchestrator.spec._call_reviewer", side_effect=Exception("boom")),
        ):
            await run_spec_review(config, [spec_file], "test-proj", storage=storage)

        assert storage.acquire_lock() is True
        storage.release_lock()


# ═══════════════════════════════════════════════════════════════════════════
# 14. Spec Review — Reference Files
# ═══════════════════════════════════════════════════════════════════════════


class TestSpecReviewReferenceFiles:
    async def test_reference_files_logged(self, tmp_path):
        from devils_advocate.orchestrator.spec import run_spec_review

        primary = tmp_path / "spec.md"
        primary.write_text("# Primary spec")
        ref = tmp_path / "context.md"
        ref.write_text("# Reference context")

        storage = _isolated_storage(tmp_path)
        config = _make_config_with_reviewers("reviewer-1")
        roles = _make_roles(["reviewer-1"])
        storage.acquire_lock()

        with (
            patch("devils_advocate.orchestrator.spec.get_models_by_role", return_value=roles),
            patch("devils_advocate.orchestrator.spec.check_context_window", return_value=(True, 100, 100000)),
        ):
            await run_spec_review(config, [primary, ref], "test-proj", storage=storage)

        log_files = list(storage.logs_dir.glob("*.log"))
        content = log_files[0].read_text()
        assert "Reference input:" in content
        assert "context.md" in content


# ═══════════════════════════════════════════════════════════════════════════
# 15. Integration Review — No Integration Reviewer
# ═══════════════════════════════════════════════════════════════════════════


class TestIntegrationReviewNoIntegReviewer:
    async def test_returns_none_when_no_integ_reviewer(self, tmp_path):
        from devils_advocate.orchestrator.integration import run_integration_review

        f1 = tmp_path / "auth.py"
        f1.write_text("def auth(): pass")

        storage = _isolated_storage(tmp_path)
        config = _make_config_with_reviewers("reviewer-1")
        roles = _make_roles(["reviewer-1"], with_integration=False)

        with patch("devils_advocate.orchestrator.integration.get_models_by_role", return_value=roles):
            result = await run_integration_review(
                config, "test-proj", input_files=[f1], storage=storage,
            )

        assert result is None


# ═══════════════════════════════════════════════════════════════════════════
# 16. Integration Review — No Files to Review
# ═══════════════════════════════════════════════════════════════════════════


class TestIntegrationReviewNoFiles:
    async def test_returns_none_with_empty_input(self, tmp_path):
        from devils_advocate.orchestrator.integration import run_integration_review

        storage = _isolated_storage(tmp_path)
        config = _make_config_with_reviewers("reviewer-1")
        roles = _make_roles(["reviewer-1"], with_integration=True)

        with patch("devils_advocate.orchestrator.integration.get_models_by_role", return_value=roles):
            result = await run_integration_review(
                config, "test-proj", input_files=[], storage=storage,
            )

        assert result is None


# ═══════════════════════════════════════════════════════════════════════════
# 17. Integration Review — Context Window Exceeded
# ═══════════════════════════════════════════════════════════════════════════


class TestIntegrationReviewContextExceeded:
    async def test_context_exceeded_returns_none(self, tmp_path):
        from devils_advocate.orchestrator.integration import run_integration_review

        f1 = tmp_path / "huge.py"
        f1.write_text("x = 1\n" * 100000)

        storage = _isolated_storage(tmp_path)
        config = _make_config_with_reviewers("reviewer-1")
        roles = _make_roles(["reviewer-1"], with_integration=True)

        with (
            patch("devils_advocate.orchestrator.integration.get_models_by_role", return_value=roles),
            patch("devils_advocate.orchestrator.integration.check_context_window", return_value=(False, 500000, 128000)),
        ):
            result = await run_integration_review(
                config, "test-proj", input_files=[f1], storage=storage,
            )

        assert result is None


# ═══════════════════════════════════════════════════════════════════════════
# 18. Integration Review — Dry Run
# ═══════════════════════════════════════════════════════════════════════════


class TestIntegrationReviewDryRun:
    async def test_dry_run_returns_none(self, tmp_path):
        from devils_advocate.orchestrator.integration import run_integration_review

        f1 = tmp_path / "dry.py"
        f1.write_text("dry = True")

        storage = _isolated_storage(tmp_path)
        config = _make_config_with_reviewers("reviewer-1")
        roles = _make_roles(["reviewer-1"], with_integration=True)

        with (
            patch("devils_advocate.orchestrator.integration.get_models_by_role", return_value=roles),
            patch("devils_advocate.orchestrator.integration.check_context_window", return_value=(True, 100, 100000)),
        ):
            result = await run_integration_review(
                config, "test-proj", input_files=[f1],
                dry_run=True, storage=storage,
            )

        assert result is None

    async def test_dry_run_saves_stub(self, tmp_path):
        from devils_advocate.orchestrator.integration import run_integration_review

        f1 = tmp_path / "dry_stub.py"
        f1.write_text("stub = True")

        storage = _isolated_storage(tmp_path)
        config = _make_config_with_reviewers("reviewer-1")
        roles = _make_roles(["reviewer-1"], with_integration=True)

        with (
            patch("devils_advocate.orchestrator.integration.get_models_by_role", return_value=roles),
            patch("devils_advocate.orchestrator.integration.check_context_window", return_value=(True, 100, 100000)),
        ):
            await run_integration_review(
                config, "test-proj", input_files=[f1],
                dry_run=True, storage=storage,
            )

        log_files = list(storage.logs_dir.glob("*.log"))
        review_id = log_files[0].stem
        ledger = storage.load_review(review_id)
        assert ledger is not None
        assert ledger["result"] == "dry_run"
        assert ledger["mode"] == "integration"


# ═══════════════════════════════════════════════════════════════════════════
# 19. Integration Review — Lock Failure
# ═══════════════════════════════════════════════════════════════════════════


class TestIntegrationReviewLockFailure:
    async def test_lock_failure_returns_none(self, tmp_path):
        from devils_advocate.orchestrator.integration import run_integration_review

        f1 = tmp_path / "locked.py"
        f1.write_text("locked = True")

        storage = _isolated_storage(tmp_path)
        config = _make_config_with_reviewers("reviewer-1")
        roles = _make_roles(["reviewer-1"], with_integration=True)
        storage.acquire_lock()

        with (
            patch("devils_advocate.orchestrator.integration.get_models_by_role", return_value=roles),
            patch("devils_advocate.orchestrator.integration.check_context_window", return_value=(True, 100, 100000)),
        ):
            result = await run_integration_review(
                config, "test-proj", input_files=[f1], storage=storage,
            )

        assert result is None


# ═══════════════════════════════════════════════════════════════════════════
# 20. Integration Review — Cost Exceeded
# ═══════════════════════════════════════════════════════════════════════════


class TestIntegrationReviewCostExceeded:
    async def test_cost_exceeded_returns_none(self, tmp_path):
        from devils_advocate.orchestrator.integration import run_integration_review

        f1 = tmp_path / "costly.py"
        f1.write_text("cost = 999")

        storage = _isolated_storage(tmp_path)
        config = _make_config_with_reviewers("reviewer-1")
        roles = _make_roles(["reviewer-1"], with_integration=True)

        with (
            patch("devils_advocate.orchestrator.integration.get_models_by_role", return_value=roles),
            patch("devils_advocate.orchestrator.integration.check_context_window", return_value=(True, 100, 100000)),
            patch("devils_advocate.orchestrator.integration._estimate_total_cost", return_value=5.0),
        ):
            result = await run_integration_review(
                config, "test-proj", input_files=[f1],
                max_cost=0.50, storage=storage,
            )

        assert result is None


# ═══════════════════════════════════════════════════════════════════════════
# 21. Integration Review — Lock Released in Finally
# ═══════════════════════════════════════════════════════════════════════════


class TestIntegrationReviewLockRelease:
    async def test_lock_released_after_reviewer_failure(self, tmp_path):
        from devils_advocate.orchestrator.integration import run_integration_review

        f1 = tmp_path / "release.py"
        f1.write_text("release = True")

        storage = _isolated_storage(tmp_path)
        config = _make_config_with_reviewers("reviewer-1")
        roles = _make_roles(["reviewer-1"], with_integration=True)

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("devils_advocate.orchestrator.integration.get_models_by_role", return_value=roles),
            patch("devils_advocate.orchestrator.integration.check_context_window", return_value=(True, 100, 100000)),
            patch("devils_advocate.http.make_async_client", return_value=mock_client),
            patch("devils_advocate.orchestrator.integration.call_with_retry", side_effect=Exception("HTTP 500")),
        ):
            # Integration review doesn't catch reviewer exceptions — they propagate
            # through the try block, but the finally block still runs.
            with pytest.raises(Exception, match="HTTP 500"):
                await run_integration_review(
                    config, "test-proj", input_files=[f1], storage=storage,
                )

        # Lock released in finally block even though exception propagated
        assert storage.acquire_lock() is True
        storage.release_lock()


# ═══════════════════════════════════════════════════════════════════════════
# 22. Integration Review — Manifest Discovery
# ═══════════════════════════════════════════════════════════════════════════


class TestIntegrationReviewManifest:
    async def test_no_manifest_returns_none(self, tmp_path):
        """Without input_files or manifest, returns None."""
        from devils_advocate.orchestrator.integration import run_integration_review

        storage = _isolated_storage(tmp_path)
        config = _make_config_with_reviewers("reviewer-1")
        roles = _make_roles(["reviewer-1"], with_integration=True)

        with patch("devils_advocate.orchestrator.integration.get_models_by_role", return_value=roles):
            result = await run_integration_review(
                config, "test-proj", storage=storage,
            )

        assert result is None

    async def test_manifest_with_completed_tasks(self, tmp_path):
        """Manifest with completed task files gets discovered."""
        from devils_advocate.orchestrator.integration import run_integration_review

        # Create a file the manifest references
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        src = project_dir / "auth.py"
        src.write_text("def auth(): pass")

        storage = _isolated_storage(tmp_path)
        # Write manifest
        manifest = {
            "tasks": [
                {"status": "completed", "files": [str(src)]},
            ]
        }
        storage.lock_dir.mkdir(parents=True, exist_ok=True)
        (storage.lock_dir / "manifest.json").write_text(json.dumps(manifest))

        config = _make_config_with_reviewers("reviewer-1")
        roles = _make_roles(["reviewer-1"], with_integration=True)
        storage.acquire_lock()  # block before API calls

        with (
            patch("devils_advocate.orchestrator.integration.get_models_by_role", return_value=roles),
            patch("devils_advocate.orchestrator.integration.check_context_window", return_value=(True, 100, 100000)),
        ):
            result = await run_integration_review(
                config, "test-proj",
                project_dir=project_dir,
                storage=storage,
            )

        # Returns None because lock was held, but files were discovered and logged
        log_files = list(storage.logs_dir.glob("*.log"))
        content = log_files[0].read_text()
        assert "auth.py" in content


# ═══════════════════════════════════════════════════════════════════════════
# 23. Code Review — Dedup Skip on Partial Failure
# ═══════════════════════════════════════════════════════════════════════════


class TestCodeReviewDedupSkip:
    """When some reviewers fail, dedup is skipped and points are promoted."""

    async def test_partial_failure_skips_dedup(self, tmp_path):
        """Verify the gather + dedup skip path when 1 of 2 reviewers fails."""

        async def succeed():
            return [make_review_point(point_id="p1", reviewer="ok-reviewer")]

        async def fail():
            raise Exception("HTTP 403")

        results = await asyncio.gather(succeed(), fail(), return_exceptions=True)

        succeeded = [r for r in results if not isinstance(r, Exception)]
        failed = [r for r in results if isinstance(r, Exception)]

        assert len(succeeded) == 1
        assert len(failed) == 1

        # The code path: failed_reviewers > 0 and len(active_reviewers) > 1
        # triggers _promote_points_to_groups instead of deduplicate_points
        from devils_advocate.dedup import promote_points_to_groups
        from devils_advocate.types import ReviewContext
        from datetime import datetime, timezone

        ctx = ReviewContext(
            project="test", review_id="test_001",
            review_start_time=datetime.now(timezone.utc),
        )
        all_points = []
        for r in succeeded:
            all_points.extend(r)

        groups = promote_points_to_groups(all_points, ctx)
        assert len(groups) == 1
        assert groups[0].points[0].reviewer == "ok-reviewer"


# ═══════════════════════════════════════════════════════════════════════════
# 24. _call_reviewer — Normalization Fallback
# ═══════════════════════════════════════════════════════════════════════════


class TestCallReviewerNormalization:
    """When parse_review_response returns no points, normalization is used."""

    async def test_normalization_called_on_no_points(self, tmp_path):
        from devils_advocate.orchestrator._common import _call_reviewer
        from devils_advocate.types import CostTracker

        storage = _isolated_storage(tmp_path)
        storage.set_review_id("norm_test")
        reviewer = make_model_config(name="reviewer-1")
        norm_model = make_model_config(name="norm-model")
        ct = CostTracker()

        mock_client = AsyncMock()

        with (
            patch(
                "devils_advocate.orchestrator._common.call_with_retry",
                return_value=("raw unparseable text", {"input_tokens": 100, "output_tokens": 50}),
            ),
            patch(
                "devils_advocate.orchestrator._common.parse_review_response",
                return_value=[],  # No points parsed
            ),
            patch(
                "devils_advocate.orchestrator._common.normalize_review_response",
                return_value=[make_review_point()],
            ) as mock_norm,
        ):
            points = await _call_reviewer(
                mock_client, reviewer, norm_model,
                "review prompt", "norm_test", ct, storage,
                role_label="reviewer_1", mode="plan",
            )

        assert len(points) == 1
        mock_norm.assert_called_once()

    async def test_no_normalization_when_points_parsed(self, tmp_path):
        from devils_advocate.orchestrator._common import _call_reviewer
        from devils_advocate.types import CostTracker

        storage = _isolated_storage(tmp_path)
        storage.set_review_id("no_norm_test")
        reviewer = make_model_config(name="reviewer-1")
        norm_model = make_model_config(name="norm-model")
        ct = CostTracker()

        mock_client = AsyncMock()

        with (
            patch(
                "devils_advocate.orchestrator._common.call_with_retry",
                return_value=("parsed text", {"input_tokens": 100, "output_tokens": 50}),
            ),
            patch(
                "devils_advocate.orchestrator._common.parse_review_response",
                return_value=[make_review_point()],  # Points parsed successfully
            ),
            patch(
                "devils_advocate.orchestrator._common.normalize_review_response",
            ) as mock_norm,
        ):
            points = await _call_reviewer(
                mock_client, reviewer, norm_model,
                "review prompt", "no_norm_test", ct, storage,
                role_label="reviewer_1", mode="plan",
            )

        assert len(points) == 1
        mock_norm.assert_not_called()

    async def test_custom_parser_used_when_provided(self, tmp_path):
        from devils_advocate.orchestrator._common import _call_reviewer
        from devils_advocate.types import CostTracker

        storage = _isolated_storage(tmp_path)
        storage.set_review_id("custom_parser")
        reviewer = make_model_config(name="reviewer-1")
        norm_model = make_model_config(name="norm-model")
        ct = CostTracker()

        mock_client = AsyncMock()
        custom_parser = MagicMock(return_value=[make_review_point(point_id="custom")])

        with patch(
            "devils_advocate.orchestrator._common.call_with_retry",
            return_value=("custom text", {"input_tokens": 100, "output_tokens": 50}),
        ):
            points = await _call_reviewer(
                mock_client, reviewer, norm_model,
                "prompt", "custom_parser", ct, storage,
                point_parser=custom_parser,
                role_label="reviewer_1", mode="spec",
            )

        assert len(points) == 1
        assert points[0].point_id == "custom"
        custom_parser.assert_called_once_with("custom text", "reviewer-1")


# ═══════════════════════════════════════════════════════════════════════════
# 25. _call_info — Log Line Formatting
# ═══════════════════════════════════════════════════════════════════════════


class TestCallInfo:
    def test_basic_format(self):
        from devils_advocate.orchestrator._common import _call_info

        model = make_model_config(name="test-model")
        result = _call_info(model, "short prompt", 8192)

        assert "sent:" in result
        assert "timeout:" in result
        assert "max_out:" in result
        assert "thinking:" in result

    def test_thinking_off(self):
        from devils_advocate.orchestrator._common import _call_info

        model = make_model_config(name="test-model")
        model.thinking = False
        result = _call_info(model, "prompt", 8192)
        assert "thinking: off" in result

    def test_thinking_on(self):
        from devils_advocate.orchestrator._common import _call_info

        model = make_model_config(name="test-model")
        model.thinking = True
        result = _call_info(model, "prompt", 8192)
        assert "thinking: on" in result


# ═══════════════════════════════════════════════════════════════════════════
# 26. PipelineInputs — Dataclass Contract
# ═══════════════════════════════════════════════════════════════════════════


class TestPipelineInputs:
    def test_all_fields_required(self, tmp_path):
        from devils_advocate.orchestrator._common import PipelineInputs
        from devils_advocate.types import CostTracker

        storage = _isolated_storage(tmp_path)
        ct = CostTracker()

        pi = PipelineInputs(
            mode="plan",
            content="test content",
            input_file_label="/tmp/plan.md",
            project="test-proj",
            review_id="test_001",
            timestamp="2026-01-01T00:00:00Z",
            all_points=[],
            groups=[],
            author=make_model_config(name="author"),
            active_reviewers=[make_model_config(name="reviewer")],
            dedup_model=make_model_config(name="dedup"),
            revision_model=make_model_config(name="revision"),
            cost_tracker=ct,
            storage=storage,
            revision_filename="revised-plan.md",
            reviewer_roles={"reviewer": "reviewer_1"},
        )

        assert pi.mode == "plan"
        assert pi.project == "test-proj"
        assert pi.revision_filename == "revised-plan.md"


# ═══════════════════════════════════════════════════════════════════════════
# 27. Round 2 Exchange — All Accepted Path
# ═══════════════════════════════════════════════════════════════════════════


class TestRound2AllAccepted:
    """When the author accepts every group, rebuttals are skipped."""

    async def test_all_accepted_skips_rebuttals(self, tmp_path):
        from devils_advocate.orchestrator._common import _run_round2_exchange
        from helpers import make_review_group, make_author_response

        storage = _isolated_storage(tmp_path)
        storage.set_review_id("all_accept")

        groups = [
            make_review_group(group_id="g1"),
            make_review_group(group_id="g2"),
        ]
        author_responses = [
            make_author_response(group_id="g1", resolution="ACCEPTED"),
            make_author_response(group_id="g2", resolution="ACCEPTED"),
        ]

        mock_client = AsyncMock()

        rebuttals, final_responses, _ = await _run_round2_exchange(
            mock_client,
            mode="plan",
            content="test content",
            groups=groups,
            author_responses=author_responses,
            grouped_text="formatted groups",
            author=make_model_config(name="author"),
            reviewers=[make_model_config(name="reviewer-1")],
            cost_tracker=MagicMock(total_usd=0, warned_80=False, exceeded=False),
            storage=storage,
            review_id="all_accept",
        )

        assert rebuttals == []
        assert final_responses == []
