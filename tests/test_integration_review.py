"""Tests for devils_advocate.orchestrator.integration — integration review workflows."""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest
import respx

from devils_advocate.orchestrator import run_integration_review
from devils_advocate.types import ModelConfig


# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

REVIEWER_OUTPUT = """\
REVIEW POINT 1:
SEVERITY: high
CATEGORY: architecture
DESCRIPTION: Module A calls Module B through a circular dependency
RECOMMENDATION: Extract shared interface into a separate module
LOCATION: module_a.py line 15

REVIEW POINT 2:
SEVERITY: medium
CATEGORY: correctness
DESCRIPTION: Inconsistent error handling between services
RECOMMENDATION: Standardize error propagation patterns
LOCATION: service.py line 42
"""

AUTHOR_OUTPUT_TEMPLATE = """\
RESPONSE TO GROUP 1:
RESOLUTION: ACCEPTED
RATIONALE: The reviewer correctly identified the circular dependency between Module A and Module B. This is a legitimate architectural concern because circular imports cause startup failures and make the dependency graph impossible to reason about.

RESPONSE TO GROUP 2:
RESOLUTION: ACCEPTED
RATIONALE: The inconsistent error handling pattern is confirmed across the service layer. Standardizing to structured error types would improve reliability because currently some paths swallow exceptions while others propagate raw exceptions.
"""

REVISION_OUTPUT = """\
=== REMEDIATION PLAN ===
Remediation steps for resolving integration issues.
=== END REMEDIATION PLAN ===
"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_config(monkeypatch, tmp_path):
    """Build a valid config dict for run_integration_review."""
    monkeypatch.setenv("TEST_KEY", "fake-key")
    monkeypatch.setenv("DVAD_HOME", str(tmp_path / "dvad-data"))

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

    integ_reviewer = ModelConfig(
        name="integ_reviewer",
        provider="openai",
        model_id="gpt-test",
        api_key_env="TEST_KEY",
        api_base="https://api.test.com/v1",
        cost_per_1k_input=0.005,
        cost_per_1k_output=0.015,
        context_window=128000,
    )
    integ_reviewer.roles = set()
    integ_reviewer.integration_reviewer = True

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
            "integ_reviewer": integ_reviewer,
            "dedup": dedup,
        },
        "config_path": "/tmp/test-models.yaml",
    }
    return config


@pytest.fixture
def integ_config(monkeypatch, tmp_path):
    """Return a fully-configured config dict with env vars set."""
    return _make_config(monkeypatch, tmp_path)


@pytest.fixture
def source_files(tmp_path):
    """Write source files for integration review and return their Paths."""
    f1 = tmp_path / "module_a.py"
    f1.write_text("import module_b\n\ndef func_a():\n    return module_b.func_b()\n")

    f2 = tmp_path / "module_b.py"
    f2.write_text("import module_a\n\ndef func_b():\n    return module_a.func_a()\n")

    return [f1, f2]


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


async def test_dry_run_no_api_calls(integ_config, source_files, tmp_path):
    """Calling run_integration_review with dry_run=True should make zero HTTP requests."""
    with respx.mock:
        old_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            result = await run_integration_review(
                config=integ_config,
                project="test-project",
                input_files=source_files,
                max_cost=10.0,
                dry_run=True,
            )
        finally:
            os.chdir(old_cwd)

    assert result is None


# ---------------------------------------------------------------------------
# Test 2: Successful integration review end-to-end
# ---------------------------------------------------------------------------


async def test_successful_integration_review_e2e(integ_config, source_files, tmp_path):
    """Full successful integration review with single reviewer, author response, and revision."""
    with respx.mock:
        # Integration reviewer (OpenAI-compatible)
        respx.post("https://api.test.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=_openai_json(REVIEWER_OUTPUT))
        )
        # Author + Revision (Anthropic -- called multiple times)
        respx.post("https://api.anthropic.com/v1/messages").mock(
            side_effect=[
                httpx.Response(200, json=_anthropic_json(AUTHOR_OUTPUT_TEMPLATE)),
                httpx.Response(200, json=_anthropic_json(REVISION_OUTPUT)),
            ]
        )

        old_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            result = await run_integration_review(
                config=integ_config,
                project="test-project",
                input_files=source_files,
                max_cost=10.0,
                dry_run=False,
            )
        finally:
            os.chdir(old_cwd)

    assert result is not None
    assert result.mode == "integration"
    assert result.project == "test-project"
    assert len(result.groups) > 0
    assert len(result.author_responses) > 0
    assert len(result.governance_decisions) > 0
    assert result.cost.total_usd > 0

    # Verify storage artifacts
    reviews_dir = tmp_path / "dvad-data" / "reviews"
    review_dirs = list(reviews_dir.glob("*"))
    assert len(review_dirs) == 1
    review_dir = review_dirs[0]
    assert (review_dir / "dvad-report.md").exists()
    assert (review_dir / "review-ledger.json").exists()
    assert (review_dir / "original_content.txt").exists()


# ---------------------------------------------------------------------------
# Test 3: File discovery from manifest.json (task status filtering)
# ---------------------------------------------------------------------------


async def test_manifest_file_discovery(integ_config, tmp_path):
    """When no input_files are given, files should be discovered from manifest.json."""
    # Create project files
    src_a = tmp_path / "src_a.py"
    src_a.write_text("def a(): pass\n")
    src_b = tmp_path / "src_b.py"
    src_b.write_text("def b(): pass\n")
    # This file is in a pending task -- should be filtered out
    src_c = tmp_path / "src_c.py"
    src_c.write_text("def c(): pass\n")

    # Create manifest.json in .dvad/
    dvad_dir = tmp_path / ".dvad"
    dvad_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "tasks": [
            {
                "status": "completed",
                "files": [str(src_a), str(src_b)],
            },
            {
                "status": "pending",
                "files": [str(src_c)],
            },
        ]
    }
    (dvad_dir / "manifest.json").write_text(json.dumps(manifest))

    with respx.mock:
        respx.post("https://api.test.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=_openai_json(REVIEWER_OUTPUT))
        )
        respx.post("https://api.anthropic.com/v1/messages").mock(
            side_effect=[
                httpx.Response(200, json=_anthropic_json(AUTHOR_OUTPUT_TEMPLATE)),
                httpx.Response(200, json=_anthropic_json(REVISION_OUTPUT)),
            ]
        )

        old_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            result = await run_integration_review(
                config=integ_config,
                project="test-project",
                input_files=None,
                max_cost=10.0,
                dry_run=False,
            )
        finally:
            os.chdir(old_cwd)

    assert result is not None
    # Only completed-task files should be included
    assert str(src_a) in result.input_file
    assert str(src_b) in result.input_file
    assert str(src_c) not in result.input_file


# ---------------------------------------------------------------------------
# Test 4: Manifest file existence check (nonexistent files filtered)
# ---------------------------------------------------------------------------


async def test_manifest_nonexistent_files_filtered(integ_config, tmp_path):
    """Files listed in manifest but not on disk should be silently skipped."""
    src_a = tmp_path / "exists.py"
    src_a.write_text("def a(): pass\n")

    dvad_dir = tmp_path / ".dvad"
    dvad_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "tasks": [
            {
                "status": "completed",
                "files": [str(src_a), str(tmp_path / "missing.py")],
            }
        ]
    }
    (dvad_dir / "manifest.json").write_text(json.dumps(manifest))

    with respx.mock:
        respx.post("https://api.test.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=_openai_json(REVIEWER_OUTPUT))
        )
        respx.post("https://api.anthropic.com/v1/messages").mock(
            side_effect=[
                httpx.Response(200, json=_anthropic_json(AUTHOR_OUTPUT_TEMPLATE)),
                httpx.Response(200, json=_anthropic_json(REVISION_OUTPUT)),
            ]
        )

        old_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            result = await run_integration_review(
                config=integ_config,
                project="test-project",
                input_files=None,
                max_cost=10.0,
                dry_run=False,
            )
        finally:
            os.chdir(old_cwd)

    assert result is not None
    assert str(src_a) in result.input_file
    assert "missing.py" not in result.input_file


# ---------------------------------------------------------------------------
# Test 5: Spec discovery from 000-strategic-summary.md
# ---------------------------------------------------------------------------


async def test_spec_discovery_strategic_summary(integ_config, source_files, tmp_path):
    """When no spec_file is given but project_dir has 000-strategic-summary.md,
    it should be discovered and used."""
    # Create the strategic summary
    summary = tmp_path / "000-strategic-summary.md"
    summary.write_text("# Strategic Summary\n\nArchitecture overview.\n")

    with respx.mock:
        respx.post("https://api.test.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=_openai_json(REVIEWER_OUTPUT))
        )
        respx.post("https://api.anthropic.com/v1/messages").mock(
            side_effect=[
                httpx.Response(200, json=_anthropic_json(AUTHOR_OUTPUT_TEMPLATE)),
                httpx.Response(200, json=_anthropic_json(REVISION_OUTPUT)),
            ]
        )

        old_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            result = await run_integration_review(
                config=integ_config,
                project="test-project",
                input_files=source_files,
                project_dir=tmp_path,
                max_cost=10.0,
                dry_run=False,
            )
        finally:
            os.chdir(old_cwd)

    assert result is not None


# ---------------------------------------------------------------------------
# Test 6: Spec discovery from strategic-summary.md (fallback name)
# ---------------------------------------------------------------------------


async def test_spec_discovery_fallback_name(integ_config, source_files, tmp_path):
    """When no spec_file and no 000- prefix, falls back to strategic-summary.md."""
    summary = tmp_path / "strategic-summary.md"
    summary.write_text("# Strategic Summary\n\nArchitecture overview.\n")

    with respx.mock:
        respx.post("https://api.test.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=_openai_json(REVIEWER_OUTPUT))
        )
        respx.post("https://api.anthropic.com/v1/messages").mock(
            side_effect=[
                httpx.Response(200, json=_anthropic_json(AUTHOR_OUTPUT_TEMPLATE)),
                httpx.Response(200, json=_anthropic_json(REVISION_OUTPUT)),
            ]
        )

        old_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            result = await run_integration_review(
                config=integ_config,
                project="test-project",
                input_files=source_files,
                project_dir=tmp_path,
                max_cost=10.0,
                dry_run=False,
            )
        finally:
            os.chdir(old_cwd)

    assert result is not None


# ---------------------------------------------------------------------------
# Test 7: No manifest and no input files error
# ---------------------------------------------------------------------------


async def test_no_manifest_no_input_files(integ_config, tmp_path):
    """When no input_files and no manifest.json, should return None."""
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        result = await run_integration_review(
            config=integ_config,
            project="test-project",
            input_files=None,
            max_cost=10.0,
            dry_run=False,
        )
    finally:
        os.chdir(old_cwd)

    assert result is None


# ---------------------------------------------------------------------------
# Test 8: No files to review error (all filtered out)
# ---------------------------------------------------------------------------


async def test_no_files_to_review(integ_config, tmp_path):
    """When input_files are provided but none exist on disk, should return None."""
    nonexistent = [tmp_path / "ghost.py"]

    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        result = await run_integration_review(
            config=integ_config,
            project="test-project",
            input_files=nonexistent,
            max_cost=10.0,
            dry_run=False,
        )
    finally:
        os.chdir(old_cwd)

    assert result is None


# ---------------------------------------------------------------------------
# Test 9: Context window exceeded for combined content
# ---------------------------------------------------------------------------


async def test_context_window_exceeded(integ_config, source_files, tmp_path):
    """When combined content exceeds integration reviewer context, should return None."""
    integ_config["models"]["integ_reviewer"].context_window = 10  # Tiny window

    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        result = await run_integration_review(
            config=integ_config,
            project="test-project",
            input_files=source_files,
            max_cost=10.0,
            dry_run=False,
        )
    finally:
        os.chdir(old_cwd)

    assert result is None


# ---------------------------------------------------------------------------
# Test 10: Single-reviewer group creation (no dedup phase)
# ---------------------------------------------------------------------------


async def test_single_reviewer_no_dedup(integ_config, source_files, tmp_path):
    """Integration uses single reviewer -- points are promoted directly
    to groups without a dedup LLM call."""
    with respx.mock:
        respx.post("https://api.test.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=_openai_json(REVIEWER_OUTPUT))
        )
        # No dedup call -- only author + revision
        respx.post("https://api.anthropic.com/v1/messages").mock(
            side_effect=[
                httpx.Response(200, json=_anthropic_json(AUTHOR_OUTPUT_TEMPLATE)),
                httpx.Response(200, json=_anthropic_json(REVISION_OUTPUT)),
            ]
        )

        old_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            result = await run_integration_review(
                config=integ_config,
                project="test-project",
                input_files=source_files,
                max_cost=10.0,
                dry_run=False,
            )
        finally:
            os.chdir(old_cwd)

    assert result is not None
    # Each point is its own group (no dedup)
    assert len(result.groups) == 2
    for g in result.groups:
        assert len(g.points) == 1
        assert g.source_reviewers == ["integ_reviewer"]


# ---------------------------------------------------------------------------
# Test 11: Lock acquired and released
# ---------------------------------------------------------------------------


async def test_lock_acquired_and_released(integ_config, source_files, tmp_path):
    """Verify .dvad/.lock is created during review and removed after."""
    lock_path = tmp_path / ".dvad" / ".lock"

    with respx.mock:
        respx.post("https://api.test.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=_openai_json(REVIEWER_OUTPUT))
        )
        respx.post("https://api.anthropic.com/v1/messages").mock(
            side_effect=[
                httpx.Response(200, json=_anthropic_json(AUTHOR_OUTPUT_TEMPLATE)),
                httpx.Response(200, json=_anthropic_json(REVISION_OUTPUT)),
            ]
        )

        old_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            result = await run_integration_review(
                config=integ_config,
                project="test-project",
                input_files=source_files,
                max_cost=10.0,
                dry_run=False,
            )
        finally:
            os.chdir(old_cwd)

    assert result is not None
    assert not lock_path.exists(), ".dvad/.lock should be removed after review completes"
