"""Tests for devils_advocate.orchestrator — plan review workflows."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import respx

from devils_advocate.orchestrator import run_plan_review
from devils_advocate.types import ModelConfig


# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

# Structured review output that parse_review_response can extract points from.
REVIEWER_OUTPUT = """\
REVIEW POINT 1:
SEVERITY: high
CATEGORY: security
DESCRIPTION: SQL injection vulnerability in user input handling
RECOMMENDATION: Use parameterized queries
LOCATION: app.py line 42

REVIEW POINT 2:
SEVERITY: medium
CATEGORY: performance
DESCRIPTION: N+1 query pattern in the dashboard endpoint
RECOMMENDATION: Use eager loading or batch queries
LOCATION: views.py line 88
"""

# Dedup output that parse_dedup_response can extract groups from.
DEDUP_OUTPUT = """\
GROUP 1:
CONCERN: SQL injection vulnerability in user input handling
POINTS: POINT 1
COMBINED_SEVERITY: high
COMBINED_CATEGORY: security

GROUP 2:
CONCERN: N+1 query pattern in the dashboard endpoint
POINTS: POINT 2
COMBINED_SEVERITY: medium
COMBINED_CATEGORY: performance
"""

# Author response that parse_author_response can extract. The GUIDs will be
# matched positionally (GROUP N fallback) since we cannot predict UUID4 values.
AUTHOR_OUTPUT_TEMPLATE = """\
RESPONSE TO GROUP 1:
RESOLUTION: ACCEPTED
RATIONALE: The reviewer correctly identified that the user input is not sanitized before being passed to the SQL query. This is a legitimate security concern because unparameterized queries allow injection attacks via the login form handler.

RESPONSE TO GROUP 2:
RESOLUTION: ACCEPTED
RATIONALE: The N+1 query pattern is confirmed in the dashboard view. Using select_related or prefetch_related in Django ORM would resolve the performance issue because the current implementation makes separate DB calls for each row.
"""

REVISION_OUTPUT = """\
=== REVISED PLAN ===
Updated plan content with security fixes and performance improvements applied.
=== END REVISED PLAN ===
"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_config(monkeypatch, tmp_path):
    """Build a valid config dict for run_plan_review with all required roles."""
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
def plan_config(monkeypatch, tmp_path):
    """Return a fully-configured config dict with env vars set."""
    return _make_config(monkeypatch, tmp_path)


@pytest.fixture
def plan_file(tmp_path):
    """Write a small plan file and return its Path."""
    p = tmp_path / "plan.md"
    p.write_text("# My Plan\n\nThis is a test plan for review.\n")
    return p


# ---------------------------------------------------------------------------
# Helper: set up API mocks for the full end-to-end flow
# ---------------------------------------------------------------------------


def _anthropic_json(text):
    """Build an Anthropic Messages API response body."""
    return {
        "content": [{"type": "text", "text": text}],
        "usage": {"input_tokens": 100, "output_tokens": 50},
    }


def _openai_json(text):
    """Build an OpenAI chat/completions response body."""
    return {
        "choices": [{"message": {"content": text}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }


# ---------------------------------------------------------------------------
# Test 1: Dry run produces no API calls
# ---------------------------------------------------------------------------


async def test_dry_run_no_api_calls(plan_config, plan_file, tmp_path):
    """Calling run_plan_review with dry_run=True should make zero HTTP requests."""
    with respx.mock:
        # No routes defined -- any request would raise
        old_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            result = await run_plan_review(
                config=plan_config,
                input_files=[plan_file],
                project="test-project",
                max_cost=10.0,
                dry_run=True,
            )
        finally:
            os.chdir(old_cwd)

    assert result is None
    # If any HTTP call was made, respx would have raised since we have no routes.


# ---------------------------------------------------------------------------
# Test 2: Successful plan review end-to-end
# ---------------------------------------------------------------------------


async def test_successful_plan_review_e2e(plan_config, plan_file, tmp_path):
    """Full successful plan review with 2 reviewers, dedup, author response, and revision."""
    with respx.mock:
        # Reviewer 1 (OpenAI-compatible)
        respx.post("https://api.test.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=_openai_json(REVIEWER_OUTPUT))
        )
        # Reviewer 2 (OpenAI-compatible)
        respx.post("https://api.test2.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=_openai_json(REVIEWER_OUTPUT))
        )
        # Dedup + Author + Revision (all Anthropic — same endpoint, called multiple times)
        respx.post("https://api.anthropic.com/v1/messages").mock(
            side_effect=[
                # Call 1: Dedup
                httpx.Response(200, json=_anthropic_json(DEDUP_OUTPUT)),
                # Call 2: Author round 1 response
                httpx.Response(200, json=_anthropic_json(AUTHOR_OUTPUT_TEMPLATE)),
                # Call 3: Revision (post-governance)
                httpx.Response(200, json=_anthropic_json(REVISION_OUTPUT)),
            ]
        )

        old_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            result = await run_plan_review(
                config=plan_config,
                input_files=[plan_file],
                project="test-project",
                max_cost=10.0,
                dry_run=False,
            )
        finally:
            os.chdir(old_cwd)

    assert result is not None
    assert result.mode == "plan"
    assert result.project == "test-project"
    assert len(result.groups) > 0
    assert len(result.author_responses) > 0
    assert len(result.governance_decisions) > 0
    assert result.cost.total_usd > 0
    # Revised output is now populated by the pipeline for report inclusion
    assert result.revised_output != ""

    # Verify storage artifacts were written (DVAD_HOME points to tmp_path/dvad-data)
    reviews_dir = tmp_path / "dvad-data" / "reviews"
    review_dirs = list(reviews_dir.glob("*_*"))
    assert len(review_dirs) == 1
    review_dir = review_dirs[0]
    assert (review_dir / "dvad-report.md").exists()
    assert (review_dir / "review-ledger.json").exists()
    assert (review_dir / "original_content.txt").exists()
    assert (review_dir / "revised-plan.md").exists()


# ---------------------------------------------------------------------------
# Test 3: One reviewer HTTP failure does not abort the review
# ---------------------------------------------------------------------------


async def test_one_reviewer_failure_continues(plan_config, plan_file, tmp_path):
    """If one reviewer returns 500 errors (exhausting retries), the other
    reviewer's points still drive the review to completion."""
    with respx.mock:
        # Reviewer 1 always fails with 500
        respx.post("https://api.test.com/v1/chat/completions").mock(
            return_value=httpx.Response(500, text="Internal Server Error")
        )
        # Reviewer 2 succeeds
        respx.post("https://api.test2.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=_openai_json(REVIEWER_OUTPUT))
        )
        # Dedup + Author + Revision (Anthropic)
        respx.post("https://api.anthropic.com/v1/messages").mock(
            side_effect=[
                httpx.Response(200, json=_anthropic_json(DEDUP_OUTPUT)),
                httpx.Response(200, json=_anthropic_json(AUTHOR_OUTPUT_TEMPLATE)),
                httpx.Response(200, json=_anthropic_json(REVISION_OUTPUT)),
            ]
        )

        old_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            result = await run_plan_review(
                config=plan_config,
                input_files=[plan_file],
                project="test-project",
                max_cost=10.0,
                dry_run=False,
            )
        finally:
            os.chdir(old_cwd)

    # Review should still complete with the surviving reviewer's points
    assert result is not None
    assert len(result.groups) > 0


# ---------------------------------------------------------------------------
# Test 4: Lock acquired and released
# ---------------------------------------------------------------------------


async def test_lock_acquired_and_released(plan_config, plan_file, tmp_path):
    """Verify .dvad/.lock is created during review and removed after."""
    lock_path = tmp_path / ".dvad" / ".lock"

    with respx.mock:
        # Reviewer 1
        respx.post("https://api.test.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=_openai_json(REVIEWER_OUTPUT))
        )
        # Reviewer 2
        respx.post("https://api.test2.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=_openai_json(REVIEWER_OUTPUT))
        )
        # Dedup + Author + Revision
        respx.post("https://api.anthropic.com/v1/messages").mock(
            side_effect=[
                httpx.Response(200, json=_anthropic_json(DEDUP_OUTPUT)),
                httpx.Response(200, json=_anthropic_json(AUTHOR_OUTPUT_TEMPLATE)),
                httpx.Response(200, json=_anthropic_json(REVISION_OUTPUT)),
            ]
        )

        old_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            result = await run_plan_review(
                config=plan_config,
                input_files=[plan_file],
                project="test-project",
                max_cost=10.0,
                dry_run=False,
            )
        finally:
            os.chdir(old_cwd)

    assert result is not None
    # After the review finishes, the lock must have been released
    assert not lock_path.exists(), ".dvad/.lock should be removed after review completes"
