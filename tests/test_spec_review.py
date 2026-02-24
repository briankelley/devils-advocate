"""Tests for devils_advocate.orchestrator.spec — spec review workflows.

Spec mode is non-adversarial: no author response, no governance.
Pipeline: Reviewers (parallel) -> Dedup (consensus) -> Revision (themed report).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import respx

from devils_advocate.orchestrator import run_spec_review
from devils_advocate.types import ModelConfig


# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

SPEC_REVIEWER_OUTPUT = """\
SUGGESTION 1:
THEME: architecture
TITLE: Modular decomposition
DESCRIPTION: Consider splitting the monolithic service config into per-domain modules
CONTEXT: Section 3 - Service Architecture

SUGGESTION 2:
THEME: security
TITLE: Token rotation policy
DESCRIPTION: Add explicit token rotation policy with configurable TTL values
CONTEXT: Section 5 - Authentication
"""

DEDUP_OUTPUT = """\
GROUP 1:
CONCERN: Modular decomposition of service configuration
POINTS: POINT 1
COMBINED_SEVERITY: info
COMBINED_CATEGORY: architecture

GROUP 2:
CONCERN: Token rotation policy for authentication
POINTS: POINT 2
COMBINED_SEVERITY: info
COMBINED_CATEGORY: security
"""

REVISION_OUTPUT = """\
=== SPEC SUGGESTIONS ===
# Suggestion Report

## Architecture
- Consider modular decomposition of service configuration.

## Security
- Add token rotation policy with configurable TTL.
=== END SPEC SUGGESTIONS ===
"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_config(monkeypatch, tmp_path):
    """Build a valid config dict for run_spec_review."""
    monkeypatch.setenv("TEST_KEY", "fake-key")
    monkeypatch.setenv("DVAD_HOME", str(tmp_path / "dvad-data"))

    # Spec mode has no author, but get_models_by_role still resolves one
    # (revision defaults to author when no explicit revision role).
    author = ModelConfig(
        name="author",
        provider="anthropic",
        model_id="claude-test",
        api_key_env="TEST_KEY",
        cost_per_1k_input=0.003,
        cost_per_1k_output=0.015,
        context_window=200000,
    )
    author.roles = {"author"}

    reviewer1 = ModelConfig(
        name="reviewer1",
        provider="openai",
        model_id="gpt-test",
        api_key_env="TEST_KEY",
        api_base="https://api.test.com/v1",
        cost_per_1k_input=0.005,
        cost_per_1k_output=0.015,
        context_window=128000,
    )
    reviewer1.roles = {"reviewer"}

    reviewer2 = ModelConfig(
        name="reviewer2",
        provider="openai",
        model_id="gemini-test",
        api_key_env="TEST_KEY",
        api_base="https://api.test2.com/v1",
        cost_per_1k_input=0.001,
        cost_per_1k_output=0.004,
        context_window=1000000,
    )
    reviewer2.roles = {"reviewer"}

    dedup = ModelConfig(
        name="dedup",
        provider="anthropic",
        model_id="haiku-test",
        api_key_env="TEST_KEY",
        cost_per_1k_input=0.001,
        cost_per_1k_output=0.004,
        context_window=200000,
    )
    dedup.deduplication = True

    config = {
        "models": {
            "author": author,
            "reviewer1": reviewer1,
            "reviewer2": reviewer2,
            "dedup": dedup,
        },
        "config_path": "/tmp/test-models.yaml",
    }
    return config


@pytest.fixture
def spec_config(monkeypatch, tmp_path):
    """Return a fully-configured config dict with env vars set."""
    return _make_config(monkeypatch, tmp_path)


@pytest.fixture
def spec_file(tmp_path):
    """Write a primary spec file and return its Path."""
    p = tmp_path / "spec.md"
    p.write_text(
        "# System Specification\n\n"
        "## Service Architecture\n"
        "Single monolithic service handling all domains.\n\n"
        "## Authentication\n"
        "Bearer tokens with no rotation policy.\n"
    )
    return p


@pytest.fixture
def reference_file(tmp_path):
    """Write a reference context file and return its Path."""
    p = tmp_path / "context.md"
    p.write_text(
        "# Existing Architecture\n\n"
        "Current system uses a single deployment model.\n"
    )
    return p


# ---------------------------------------------------------------------------
# Helper: API response builders
# ---------------------------------------------------------------------------


def _anthropic_json(text):
    return {
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": 100, "output_tokens": 50},
    }


def _openai_json(text):
    return {
        "choices": [{"message": {"content": text}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }


# ---------------------------------------------------------------------------
# Test 1: Dry run produces no API calls
# ---------------------------------------------------------------------------


async def test_dry_run_no_api_calls(spec_config, spec_file, tmp_path):
    """Calling run_spec_review with dry_run=True should make zero HTTP requests."""
    with respx.mock:
        old_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            result = await run_spec_review(
                config=spec_config,
                input_files=[spec_file],
                project="test-project",
                max_cost=10.0,
                dry_run=True,
            )
        finally:
            os.chdir(old_cwd)

    assert result is None


# ---------------------------------------------------------------------------
# Test 2: Successful spec review end-to-end
# ---------------------------------------------------------------------------


async def test_successful_spec_review_e2e(spec_config, spec_file, tmp_path):
    """Full successful spec review: reviewers -> dedup -> revision."""
    with respx.mock:
        respx.post("https://api.test.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=_openai_json(SPEC_REVIEWER_OUTPUT))
        )
        respx.post("https://api.test2.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=_openai_json(SPEC_REVIEWER_OUTPUT))
        )
        # Dedup + Revision (Anthropic)
        respx.post("https://api.anthropic.com/v1/messages").mock(
            side_effect=[
                httpx.Response(200, json=_anthropic_json(DEDUP_OUTPUT)),
                httpx.Response(200, json=_anthropic_json(REVISION_OUTPUT)),
            ]
        )

        old_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            result = await run_spec_review(
                config=spec_config,
                input_files=[spec_file],
                project="test-project",
                max_cost=10.0,
                dry_run=False,
            )
        finally:
            os.chdir(old_cwd)

    assert result is not None
    assert result.mode == "spec"
    assert result.project == "test-project"
    assert len(result.groups) > 0
    # Spec mode has no adversarial pipeline
    assert result.author_responses == []
    assert result.governance_decisions == []
    assert result.rebuttals == []
    assert result.author_final_responses == []
    # Author model is empty in spec mode
    assert result.author_model == ""
    assert result.cost.total_usd > 0

    # Verify storage artifacts
    reviews_dir = tmp_path / "dvad-data" / "reviews"
    review_dirs = list(reviews_dir.glob("*"))
    assert len(review_dirs) == 1
    review_dir = review_dirs[0]
    assert (review_dir / "dvad-report.md").exists()
    assert (review_dir / "review-ledger.json").exists()
    assert (review_dir / "original_content.txt").exists()
    assert (review_dir / "revised-spec-suggestions.md").exists()


# ---------------------------------------------------------------------------
# Test 3: Multi-file reference context construction
# ---------------------------------------------------------------------------


async def test_multi_file_reference_context(spec_config, spec_file, reference_file, tmp_path):
    """When multiple input files are given, the primary is reviewed
    and additional files are included as reference context."""
    with respx.mock:
        respx.post("https://api.test.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=_openai_json(SPEC_REVIEWER_OUTPUT))
        )
        respx.post("https://api.test2.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=_openai_json(SPEC_REVIEWER_OUTPUT))
        )
        respx.post("https://api.anthropic.com/v1/messages").mock(
            side_effect=[
                httpx.Response(200, json=_anthropic_json(DEDUP_OUTPUT)),
                httpx.Response(200, json=_anthropic_json(REVISION_OUTPUT)),
            ]
        )

        old_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            result = await run_spec_review(
                config=spec_config,
                input_files=[spec_file, reference_file],
                project="test-project",
                max_cost=10.0,
                dry_run=False,
            )
        finally:
            os.chdir(old_cwd)

    assert result is not None
    # input_file should be the primary file only
    assert str(spec_file) == result.input_file

    # Original content stored should include REFERENCE FILE markers
    reviews_dir = tmp_path / "dvad-data" / "reviews"
    review_dirs = list(reviews_dir.glob("*"))
    review_dir = review_dirs[0]
    original_content = (review_dir / "original_content.txt").read_text()
    assert "PRIMARY SPECIFICATION" in original_content
    assert "REFERENCE FILE" in original_content
    assert reference_file.name in original_content


# ---------------------------------------------------------------------------
# Test 4: Custom parser (parse_spec_response) and system prompt delegation
# ---------------------------------------------------------------------------


async def test_custom_parser_and_system_prompt(spec_config, spec_file, tmp_path):
    """Spec mode should use parse_spec_response (SUGGESTION format) and
    get_spec_reviewer_system_prompt, not the default reviewer parser/prompt."""
    called_with_system_prompts = []

    original_call_with_retry = None

    async def tracking_call_with_retry(client, model, sys_prompt, prompt, max_tokens, **kwargs):
        called_with_system_prompts.append(sys_prompt)
        return await original_call_with_retry(client, model, sys_prompt, prompt, max_tokens, **kwargs)

    from devils_advocate.providers import call_with_retry as _orig_cwr
    original_call_with_retry = _orig_cwr

    with respx.mock:
        respx.post("https://api.test.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=_openai_json(SPEC_REVIEWER_OUTPUT))
        )
        respx.post("https://api.test2.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=_openai_json(SPEC_REVIEWER_OUTPUT))
        )
        respx.post("https://api.anthropic.com/v1/messages").mock(
            side_effect=[
                httpx.Response(200, json=_anthropic_json(DEDUP_OUTPUT)),
                httpx.Response(200, json=_anthropic_json(REVISION_OUTPUT)),
            ]
        )

        old_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            with patch("devils_advocate.orchestrator._common.call_with_retry", side_effect=tracking_call_with_retry):
                result = await run_spec_review(
                    config=spec_config,
                    input_files=[spec_file],
                    project="test-project",
                    max_cost=10.0,
                    dry_run=False,
                )
        finally:
            os.chdir(old_cwd)

    assert result is not None
    # The reviewer calls should use the spec-specific system prompt
    from devils_advocate.prompts import get_spec_reviewer_system_prompt
    spec_sys = get_spec_reviewer_system_prompt()
    # At least the reviewer calls should have used the spec system prompt
    assert any(sp == spec_sys for sp in called_with_system_prompts)


# ---------------------------------------------------------------------------
# Test 5: No suggestions from any reviewer
# ---------------------------------------------------------------------------


async def test_no_suggestions_from_reviewers(spec_config, spec_file, tmp_path):
    """When reviewers return no parseable suggestions, should return None."""
    empty_output = "I found no issues with this specification. It looks great!"

    with respx.mock:
        respx.post("https://api.test.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=_openai_json(empty_output))
        )
        respx.post("https://api.test2.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=_openai_json(empty_output))
        )
        # Normalization fallback (Anthropic) -- also returns nothing parseable
        respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=httpx.Response(200, json=_anthropic_json(empty_output))
        )

        old_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            result = await run_spec_review(
                config=spec_config,
                input_files=[spec_file],
                project="test-project",
                max_cost=10.0,
                dry_run=False,
            )
        finally:
            os.chdir(old_cwd)

    assert result is None


# ---------------------------------------------------------------------------
# Test 6: Dedup skip on partial reviewer failure
# ---------------------------------------------------------------------------


async def test_dedup_skip_on_partial_failure(spec_config, spec_file, tmp_path):
    """When one reviewer fails, dedup should be skipped (each point promoted
    to its own group) and the review should still complete."""
    with respx.mock:
        respx.post("https://api.test.com/v1/chat/completions").mock(
            return_value=httpx.Response(500, text="Internal Server Error")
        )
        respx.post("https://api.test2.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=_openai_json(SPEC_REVIEWER_OUTPUT))
        )
        # No dedup -- straight to revision (Anthropic)
        respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=httpx.Response(200, json=_anthropic_json(REVISION_OUTPUT))
        )

        old_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            result = await run_spec_review(
                config=spec_config,
                input_files=[spec_file],
                project="test-project",
                max_cost=10.0,
                dry_run=False,
            )
        finally:
            os.chdir(old_cwd)

    assert result is not None
    # Each point should have been promoted to its own group
    assert len(result.groups) == 2
    for g in result.groups:
        assert len(g.points) == 1


# ---------------------------------------------------------------------------
# Test 7: Cost guardrail checkpoints
# ---------------------------------------------------------------------------


async def test_cost_estimate_exceeds_max_cost(spec_config, spec_file, tmp_path):
    """When estimated cost exceeds max_cost, should return None without API calls."""
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        result = await run_spec_review(
            config=spec_config,
            input_files=[spec_file],
            project="test-project",
            max_cost=0.0000001,  # Impossibly low
            dry_run=False,
        )
    finally:
        os.chdir(old_cwd)

    assert result is None


# ---------------------------------------------------------------------------
# Test 8: Report save before revision, then re-save after revision
# ---------------------------------------------------------------------------


async def test_report_saved_before_and_after_revision(spec_config, spec_file, tmp_path):
    """The report should be saved before revision, then re-saved after
    revision completes with the revised output included."""
    save_calls = []
    from devils_advocate.storage import StorageManager
    original_atomic_write = StorageManager._atomic_write

    @staticmethod
    def tracking_atomic_write(path, content):
        if "dvad-report.md" in str(path):
            save_calls.append(str(path))
        return original_atomic_write(path, content)

    with respx.mock:
        respx.post("https://api.test.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=_openai_json(SPEC_REVIEWER_OUTPUT))
        )
        respx.post("https://api.test2.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=_openai_json(SPEC_REVIEWER_OUTPUT))
        )
        respx.post("https://api.anthropic.com/v1/messages").mock(
            side_effect=[
                httpx.Response(200, json=_anthropic_json(DEDUP_OUTPUT)),
                httpx.Response(200, json=_anthropic_json(REVISION_OUTPUT)),
            ]
        )

        old_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            with patch.object(StorageManager, "_atomic_write", tracking_atomic_write):
                result = await run_spec_review(
                    config=spec_config,
                    input_files=[spec_file],
                    project="test-project",
                    max_cost=10.0,
                    dry_run=False,
                )
        finally:
            os.chdir(old_cwd)

    assert result is not None
    # dvad-report.md should have been written at least twice:
    # once in save_review_artifacts and once after revision
    report_saves = [c for c in save_calls if "dvad-report.md" in c]
    assert len(report_saves) >= 2, (
        f"Expected dvad-report.md to be saved at least twice, got {len(report_saves)}"
    )


# ---------------------------------------------------------------------------
# Test 9: Revision failure (non-fatal exception)
# ---------------------------------------------------------------------------


async def test_revision_failure_non_fatal(spec_config, spec_file, tmp_path):
    """When revision fails, the review should still complete with the
    pre-revision report saved."""
    with respx.mock:
        respx.post("https://api.test.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=_openai_json(SPEC_REVIEWER_OUTPUT))
        )
        respx.post("https://api.test2.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=_openai_json(SPEC_REVIEWER_OUTPUT))
        )
        # Dedup succeeds, revision fails
        respx.post("https://api.anthropic.com/v1/messages").mock(
            side_effect=[
                httpx.Response(200, json=_anthropic_json(DEDUP_OUTPUT)),
                httpx.Response(500, text="Revision model unavailable"),
            ]
        )

        old_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            result = await run_spec_review(
                config=spec_config,
                input_files=[spec_file],
                project="test-project",
                max_cost=10.0,
                dry_run=False,
            )
        finally:
            os.chdir(old_cwd)

    assert result is not None
    # Review completed despite revision failure
    assert result.mode == "spec"
    assert len(result.groups) > 0
    # Revised output should be empty since revision failed
    assert result.revised_output == ""

    # Pre-revision report should still exist
    reviews_dir = tmp_path / "dvad-data" / "reviews"
    review_dirs = list(reviews_dir.glob("*"))
    assert len(review_dirs) == 1
    review_dir = review_dirs[0]
    assert (review_dir / "dvad-report.md").exists()
    assert (review_dir / "review-ledger.json").exists()
    # No revised suggestions file since revision failed
    assert not (review_dir / "revised-spec-suggestions.md").exists()


# ---------------------------------------------------------------------------
# Test 10: No reviewers available (all exceed context window)
# ---------------------------------------------------------------------------


async def test_no_reviewers_available(spec_config, spec_file, tmp_path):
    """When all reviewers exceed context window, should return None."""
    spec_config["models"]["reviewer1"].context_window = 10
    spec_config["models"]["reviewer2"].context_window = 10

    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        result = await run_spec_review(
            config=spec_config,
            input_files=[spec_file],
            project="test-project",
            max_cost=10.0,
            dry_run=False,
        )
    finally:
        os.chdir(old_cwd)

    assert result is None


# ---------------------------------------------------------------------------
# Test 11: Lock acquisition failure
# ---------------------------------------------------------------------------


async def test_lock_acquisition_failure(spec_config, spec_file, tmp_path):
    """When the lock is already held, should return None."""
    import socket
    import time

    lock_dir = tmp_path / ".dvad"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_file = lock_dir / ".lock"
    lock_data = json.dumps({
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "timestamp": time.time(),
    })
    lock_file.write_text(lock_data)

    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        result = await run_spec_review(
            config=spec_config,
            input_files=[spec_file],
            project="test-project",
            max_cost=10.0,
            dry_run=False,
        )
    finally:
        os.chdir(old_cwd)

    assert result is None


# ---------------------------------------------------------------------------
# Test 12: Lock acquired and released
# ---------------------------------------------------------------------------


async def test_lock_acquired_and_released(spec_config, spec_file, tmp_path):
    """Verify .dvad/.lock is created during review and removed after."""
    lock_path = tmp_path / ".dvad" / ".lock"

    with respx.mock:
        respx.post("https://api.test.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=_openai_json(SPEC_REVIEWER_OUTPUT))
        )
        respx.post("https://api.test2.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=_openai_json(SPEC_REVIEWER_OUTPUT))
        )
        respx.post("https://api.anthropic.com/v1/messages").mock(
            side_effect=[
                httpx.Response(200, json=_anthropic_json(DEDUP_OUTPUT)),
                httpx.Response(200, json=_anthropic_json(REVISION_OUTPUT)),
            ]
        )

        old_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            result = await run_spec_review(
                config=spec_config,
                input_files=[spec_file],
                project="test-project",
                max_cost=10.0,
                dry_run=False,
            )
        finally:
            os.chdir(old_cwd)

    assert result is not None
    assert not lock_path.exists(), ".dvad/.lock should be removed after review completes"


# ---------------------------------------------------------------------------
# Test 13: Reviewer context window skip with active_reviewers accumulator
# ---------------------------------------------------------------------------


async def test_reviewer_context_window_skip(spec_config, spec_file, tmp_path):
    """Reviewer whose context window is too small should be skipped, but
    review proceeds with remaining reviewers."""
    spec_config["models"]["reviewer1"].context_window = 10  # Tiny

    with respx.mock:
        # Only reviewer2 called
        respx.post("https://api.test2.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=_openai_json(SPEC_REVIEWER_OUTPUT))
        )
        respx.post("https://api.anthropic.com/v1/messages").mock(
            side_effect=[
                httpx.Response(200, json=_anthropic_json(DEDUP_OUTPUT)),
                httpx.Response(200, json=_anthropic_json(REVISION_OUTPUT)),
            ]
        )

        old_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            result = await run_spec_review(
                config=spec_config,
                input_files=[spec_file],
                project="test-project",
                max_cost=10.0,
                dry_run=False,
            )
        finally:
            os.chdir(old_cwd)

    assert result is not None
    assert result.reviewer_models == ["reviewer2"]
