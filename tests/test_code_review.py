"""Tests for devils_advocate.orchestrator.code — code review workflows."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import respx

from devils_advocate.orchestrator import run_code_review
from devils_advocate.types import ModelConfig


# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

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

AUTHOR_OUTPUT_TEMPLATE = """\
RESPONSE TO GROUP 1:
RESOLUTION: ACCEPTED
RATIONALE: The reviewer correctly identified that the user input is not sanitized before being passed to the SQL query. This is a legitimate security concern because unparameterized queries allow injection attacks via the login form handler.

RESPONSE TO GROUP 2:
RESOLUTION: ACCEPTED
RATIONALE: The N+1 query pattern is confirmed in the dashboard view. Using select_related or prefetch_related in Django ORM would resolve the performance issue because the current implementation makes separate DB calls for each row.
"""

REVISION_OUTPUT = """\
=== UNIFIED DIFF ===
Updated code with security fixes and performance improvements applied.
=== END UNIFIED DIFF ===
"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_config(monkeypatch, tmp_path):
    """Build a valid config dict for run_code_review with all required roles."""
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
def code_config(monkeypatch, tmp_path):
    """Return a fully-configured config dict with env vars set."""
    return _make_config(monkeypatch, tmp_path)


@pytest.fixture
def code_file(tmp_path):
    """Write a small code file and return its Path."""
    p = tmp_path / "app.py"
    p.write_text(
        "def login(user, password):\n"
        "    query = f'SELECT * FROM users WHERE user={user}'\n"
        "    return db.execute(query)\n"
    )
    return p


@pytest.fixture
def spec_file(tmp_path):
    """Write a small spec file and return its Path."""
    p = tmp_path / "spec.md"
    p.write_text("# Spec\n\nAll inputs must be sanitized.\n")
    return p


# ---------------------------------------------------------------------------
# Helper: API response builders
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


async def test_dry_run_no_api_calls(code_config, code_file, tmp_path):
    """Calling run_code_review with dry_run=True should make zero HTTP requests."""
    with respx.mock:
        old_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            result = await run_code_review(
                config=code_config,
                input_file=code_file,
                project="test-project",
                max_cost=10.0,
                dry_run=True,
            )
        finally:
            os.chdir(old_cwd)

    assert result is None


# ---------------------------------------------------------------------------
# Test 2: Successful code review end-to-end
# ---------------------------------------------------------------------------


async def test_successful_code_review_e2e(code_config, code_file, tmp_path):
    """Full successful code review with 2 reviewers, dedup, author response, and revision."""
    with respx.mock:
        respx.post("https://api.test.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=_openai_json(REVIEWER_OUTPUT))
        )
        respx.post("https://api.test2.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=_openai_json(REVIEWER_OUTPUT))
        )
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
            result = await run_code_review(
                config=code_config,
                input_file=code_file,
                project="test-project",
                max_cost=10.0,
                dry_run=False,
            )
        finally:
            os.chdir(old_cwd)

    assert result is not None
    assert result.mode == "code"
    assert result.project == "test-project"
    assert len(result.groups) > 0
    assert len(result.author_responses) > 0
    assert len(result.governance_decisions) > 0
    assert result.cost.total_usd > 0
    # Revised output stays empty -- canonical artifact is the separate file
    assert result.revised_output == ""

    # Verify storage artifacts were written
    reviews_dir = tmp_path / "dvad-data" / "reviews"
    review_dirs = list(reviews_dir.glob("*"))
    assert len(review_dirs) == 1
    review_dir = review_dirs[0]
    assert (review_dir / "dvad-report.md").exists()
    assert (review_dir / "review-ledger.json").exists()
    assert (review_dir / "original_content.txt").exists()


# ---------------------------------------------------------------------------
# Test 3: Spec file reading and prompt inclusion
# ---------------------------------------------------------------------------


async def test_spec_file_included_in_review(code_config, code_file, spec_file, tmp_path):
    """When a spec_file is provided, its content should be read and used."""
    with respx.mock:
        respx.post("https://api.test.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=_openai_json(REVIEWER_OUTPUT))
        )
        respx.post("https://api.test2.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=_openai_json(REVIEWER_OUTPUT))
        )
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
            result = await run_code_review(
                config=code_config,
                input_file=code_file,
                spec_file=spec_file,
                project="test-project",
                max_cost=10.0,
                dry_run=False,
            )
        finally:
            os.chdir(old_cwd)

    assert result is not None
    assert result.mode == "code"
    assert len(result.groups) > 0


# ---------------------------------------------------------------------------
# Test 4: Reviewer context window skip with active_reviewers accumulator
# ---------------------------------------------------------------------------


async def test_reviewer_context_window_skip(code_config, code_file, tmp_path):
    """Reviewer whose context window is too small should be skipped.

    reviewer1 has context_window=128000. We make the input large enough
    to exceed 80% of that (the CONTEXT_WINDOW_THRESHOLD).
    """
    # Override reviewer1 with a tiny context window
    code_config["models"]["reviewer1"].context_window = 10  # ~8 token limit

    with respx.mock:
        # Only reviewer2 should be called
        respx.post("https://api.test2.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=_openai_json(REVIEWER_OUTPUT))
        )
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
            result = await run_code_review(
                config=code_config,
                input_file=code_file,
                project="test-project",
                max_cost=10.0,
                dry_run=False,
            )
        finally:
            os.chdir(old_cwd)

    assert result is not None
    # reviewer1 should have been skipped, only reviewer2 active
    assert result.reviewer_models == ["reviewer2"]


# ---------------------------------------------------------------------------
# Test 5: No reviewers available exit (all exceed context window)
# ---------------------------------------------------------------------------


async def test_no_reviewers_available(code_config, code_file, tmp_path):
    """When all reviewers exceed context window, should return None."""
    code_config["models"]["reviewer1"].context_window = 10
    code_config["models"]["reviewer2"].context_window = 10

    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        result = await run_code_review(
            config=code_config,
            input_file=code_file,
            project="test-project",
            max_cost=10.0,
            dry_run=False,
        )
    finally:
        os.chdir(old_cwd)

    assert result is None


# ---------------------------------------------------------------------------
# Test 6: Parallel reviewer exception handling (gather with return_exceptions)
# ---------------------------------------------------------------------------


async def test_one_reviewer_failure_continues(code_config, code_file, tmp_path):
    """If one reviewer returns 500 errors (exhausting retries), the other
    reviewer's points still drive the review to completion."""
    with respx.mock:
        respx.post("https://api.test.com/v1/chat/completions").mock(
            return_value=httpx.Response(500, text="Internal Server Error")
        )
        respx.post("https://api.test2.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=_openai_json(REVIEWER_OUTPUT))
        )
        # When one reviewer fails, dedup is skipped, so pipeline goes
        # straight to author + revision (Anthropic calls only).
        respx.post("https://api.anthropic.com/v1/messages").mock(
            side_effect=[
                httpx.Response(200, json=_anthropic_json(AUTHOR_OUTPUT_TEMPLATE)),
                httpx.Response(200, json=_anthropic_json(REVISION_OUTPUT)),
            ]
        )

        old_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            result = await run_code_review(
                config=code_config,
                input_file=code_file,
                project="test-project",
                max_cost=10.0,
                dry_run=False,
            )
        finally:
            os.chdir(old_cwd)

    assert result is not None
    assert len(result.groups) > 0


# ---------------------------------------------------------------------------
# Test 7: Dedup skip on partial reviewer failure
# ---------------------------------------------------------------------------


async def test_dedup_skip_on_partial_failure(code_config, code_file, tmp_path):
    """When one reviewer fails, dedup should be skipped (each point promoted
    to its own group) and the review should still complete."""
    with respx.mock:
        respx.post("https://api.test.com/v1/chat/completions").mock(
            return_value=httpx.Response(500, text="Internal Server Error")
        )
        respx.post("https://api.test2.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=_openai_json(REVIEWER_OUTPUT))
        )
        # No dedup call -- straight to author + revision
        respx.post("https://api.anthropic.com/v1/messages").mock(
            side_effect=[
                httpx.Response(200, json=_anthropic_json(AUTHOR_OUTPUT_TEMPLATE)),
                httpx.Response(200, json=_anthropic_json(REVISION_OUTPUT)),
            ]
        )

        old_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            result = await run_code_review(
                config=code_config,
                input_file=code_file,
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
# Test 8: Cost estimate exceeding max_cost returns None
# ---------------------------------------------------------------------------


async def test_cost_estimate_exceeds_max_cost(code_config, code_file, tmp_path):
    """When the estimated cost exceeds max_cost, should return None immediately."""
    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        result = await run_code_review(
            config=code_config,
            input_file=code_file,
            project="test-project",
            max_cost=0.0000001,  # Impossibly low cost limit
            dry_run=False,
        )
    finally:
        os.chdir(old_cwd)

    assert result is None


# ---------------------------------------------------------------------------
# Test 9: Lock acquisition failure
# ---------------------------------------------------------------------------


async def test_lock_acquisition_failure(code_config, code_file, tmp_path):
    """When the lock is already held, should return None."""
    # Pre-create the lock file to simulate another process holding it
    lock_dir = tmp_path / ".dvad"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_file = lock_dir / ".lock"
    # Write valid lock data with current host and a live PID (current process)
    import socket
    import time
    lock_data = json.dumps({
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "timestamp": time.time(),
    })
    lock_file.write_text(lock_data)

    old_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        result = await run_code_review(
            config=code_config,
            input_file=code_file,
            project="test-project",
            max_cost=10.0,
            dry_run=False,
        )
    finally:
        os.chdir(old_cwd)

    assert result is None


# ---------------------------------------------------------------------------
# Test 10: Lock acquired and released
# ---------------------------------------------------------------------------


async def test_lock_acquired_and_released(code_config, code_file, tmp_path):
    """Verify .dvad/.lock is created during review and removed after."""
    lock_path = tmp_path / ".dvad" / ".lock"

    with respx.mock:
        respx.post("https://api.test.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=_openai_json(REVIEWER_OUTPUT))
        )
        respx.post("https://api.test2.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=_openai_json(REVIEWER_OUTPUT))
        )
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
            result = await run_code_review(
                config=code_config,
                input_file=code_file,
                project="test-project",
                max_cost=10.0,
                dry_run=False,
            )
        finally:
            os.chdir(old_cwd)

    assert result is not None
    assert not lock_path.exists(), ".dvad/.lock should be removed after review completes"


# ---------------------------------------------------------------------------
# Test 11: Pipeline difflib generation in code mode
# ---------------------------------------------------------------------------

# Revised code response that the extractor can successfully parse.
# Must use canonical code mode delimiters.
REVISION_CODE_OUTPUT = """\
=== REVISED CODE ===
def login(user, password):
    query = 'SELECT * FROM users WHERE user = ?'
    return db.execute(query, (user,))
=== END REVISED CODE ===
"""


async def test_code_review_generates_diff_patch(code_config, code_file, tmp_path):
    """When revision returns changed code, revised-diff.patch is written with valid unified diff."""
    with respx.mock:
        respx.post("https://api.test.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=_openai_json(REVIEWER_OUTPUT))
        )
        respx.post("https://api.test2.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=_openai_json(REVIEWER_OUTPUT))
        )
        respx.post("https://api.anthropic.com/v1/messages").mock(
            side_effect=[
                httpx.Response(200, json=_anthropic_json(DEDUP_OUTPUT)),
                httpx.Response(200, json=_anthropic_json(AUTHOR_OUTPUT_TEMPLATE)),
                httpx.Response(200, json=_anthropic_json(REVISION_CODE_OUTPUT)),
            ]
        )

        old_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            result = await run_code_review(
                config=code_config,
                input_file=code_file,
                project="test-project",
                max_cost=10.0,
                dry_run=False,
            )
        finally:
            os.chdir(old_cwd)

    assert result is not None

    # Locate the review directory
    reviews_dir = tmp_path / "dvad-data" / "reviews"
    review_dirs = list(reviews_dir.glob("*"))
    assert len(review_dirs) == 1
    review_dir = review_dirs[0]

    # revised-diff.patch should exist with valid unified diff format
    diff_path = review_dir / "revised-diff.patch"
    assert diff_path.exists(), "revised-diff.patch should be created for code mode"
    diff_content = diff_path.read_text()
    assert diff_content.startswith("---"), "Diff should start with --- header"
    assert "+++" in diff_content, "Diff should contain +++ header"
    # Headers should use a/ and b/ prefixes with the input file label
    assert "a/" in diff_content
    assert "b/" in diff_content

    # The revised code file should also exist
    revised_path = review_dir / f"revised-{code_file.name}"
    assert revised_path.exists(), "Revised code file should be written"

    # result.revised_output should contain the diff (not the full revised code)
    assert result.revised_output.startswith("---")


async def test_code_review_no_diff_when_unchanged(code_config, tmp_path, monkeypatch):
    """When revised code equals original, revised-diff.patch should NOT be written.

    Note: _extract_revision_strict strips the extracted content, so the original
    content must not have a trailing newline to get a true no-change scenario.
    """
    monkeypatch.setenv("TEST_KEY", "fake-key")
    monkeypatch.setenv("DVAD_HOME", str(tmp_path / "dvad-data"))

    # Write a code file with content that matches what the extractor will return
    # (extractor strips whitespace, so original must match that stripped form)
    code_file = tmp_path / "app.py"
    original_code = "def hello():\n    return 'world'"
    code_file.write_text(original_code)

    # Revision returns the EXACT same code as original (strip-stable)
    identical_revision = (
        f"=== REVISED CODE ===\n{original_code}\n=== END REVISED CODE ==="
    )

    with respx.mock:
        respx.post("https://api.test.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=_openai_json(REVIEWER_OUTPUT))
        )
        respx.post("https://api.test2.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=_openai_json(REVIEWER_OUTPUT))
        )
        respx.post("https://api.anthropic.com/v1/messages").mock(
            side_effect=[
                httpx.Response(200, json=_anthropic_json(DEDUP_OUTPUT)),
                httpx.Response(200, json=_anthropic_json(AUTHOR_OUTPUT_TEMPLATE)),
                httpx.Response(200, json=_anthropic_json(identical_revision)),
            ]
        )

        old_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            result = await run_code_review(
                config=code_config,
                input_file=code_file,
                project="test-project",
                max_cost=10.0,
                dry_run=False,
            )
        finally:
            os.chdir(old_cwd)

    assert result is not None

    reviews_dir = tmp_path / "dvad-data" / "reviews"
    review_dirs = list(reviews_dir.glob("*"))
    assert len(review_dirs) == 1
    review_dir = review_dirs[0]

    # revised-diff.patch should NOT exist since there are no changes
    diff_path = review_dir / "revised-diff.patch"
    assert not diff_path.exists(), "revised-diff.patch should not exist when code is unchanged"

    # When diff is empty, result.revised_output should fall back to the full revised code
    assert result.revised_output == original_code


# ---------------------------------------------------------------------------
# Test 12: Revised filename uses input file name
# ---------------------------------------------------------------------------


async def test_revised_filename_includes_input_name(code_config, tmp_path, monkeypatch):
    """The revised artifact should be named revised-{input_file.name}."""
    monkeypatch.setenv("TEST_KEY", "fake-key")
    monkeypatch.setenv("DVAD_HOME", str(tmp_path / "dvad-data"))

    # Input file with a specific name
    code_file = tmp_path / "orchestrator.py"
    code_file.write_text("def main(): pass\n")

    with respx.mock:
        respx.post("https://api.test.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=_openai_json(REVIEWER_OUTPUT))
        )
        respx.post("https://api.test2.com/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=_openai_json(REVIEWER_OUTPUT))
        )
        respx.post("https://api.anthropic.com/v1/messages").mock(
            side_effect=[
                httpx.Response(200, json=_anthropic_json(DEDUP_OUTPUT)),
                httpx.Response(200, json=_anthropic_json(AUTHOR_OUTPUT_TEMPLATE)),
                httpx.Response(200, json=_anthropic_json(REVISION_CODE_OUTPUT)),
            ]
        )

        old_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            result = await run_code_review(
                config=code_config,
                input_file=code_file,
                project="test-project",
                max_cost=10.0,
                dry_run=False,
            )
        finally:
            os.chdir(old_cwd)

    assert result is not None

    reviews_dir = tmp_path / "dvad-data" / "reviews"
    review_dirs = list(reviews_dir.glob("*"))
    assert len(review_dirs) == 1
    review_dir = review_dirs[0]

    # The revised file should follow the pattern revised-{input_file.name}
    expected_name = f"revised-{code_file.name}"
    revised_path = review_dir / expected_name
    assert revised_path.exists(), (
        f"Expected revised artifact at {expected_name}, "
        f"found: {[p.name for p in review_dir.iterdir()]}"
    )
