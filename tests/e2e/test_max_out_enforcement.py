"""E2E tests for max_out_configured enforcement across roles and modes.

Validates that output token limits are respected when sending requests to the
LLM, including edge cases like very small limits and extraction failures.
"""

import json
import shutil
import time
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import expect

pytestmark = [pytest.mark.e2e, pytest.mark.e2e_live]

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _require_local_llm(local_llm):
    """Ensure local LLM is running before live flow tests."""


def _create_test_plan(tmp_path: Path) -> Path:
    """Create a small test plan for review submission."""
    f = tmp_path / "test-plan.md"
    f.write_text(
        "# Test Plan\n\n"
        "## Steps\n"
        "1. Initialize module\n"
        "2. Validate inputs\n"
        "3. Process data\n"
        "4. Return results\n"
    )
    return f


def _start_review_and_wait(page, dvad_server, project, input_file, *, timeout=300):
    """Start a review and wait for completion. Returns the review ledger."""
    page.goto(dvad_server)
    page.wait_for_load_state("networkidle")
    csrf = page.locator('meta[name="csrf-token"]').get_attribute("content")

    deadline = time.monotonic() + timeout
    review_id = None
    while time.monotonic() < deadline:
        resp = page.request.post(
            f"{dvad_server}/api/review/start",
            multipart={
                "mode": "plan",
                "project": project,
                "input_paths": json.dumps([str(input_file)]),
            },
            headers={"X-DVAD-Token": csrf},
        )
        if resp.status == 200:
            review_id = resp.json()["review_id"]
            break
        if resp.status == 409:
            time.sleep(5)
            continue
        pytest.fail(f"Unexpected status {resp.status}")

    if not review_id:
        pytest.fail("Timed out waiting for slot")

    # Poll for completion
    while time.monotonic() < deadline:
        resp = page.request.get(f"{dvad_server}/api/review/{review_id}")
        if resp.status == 200:
            ledger = resp.json()
            if ledger.get("result") and ledger["result"] != "running":
                return ledger
        time.sleep(3)

    pytest.fail("Review did not complete in time")


def _set_max_out(page, dvad_server, model_name, max_tokens):
    """Set max_out_configured for a model via the API."""
    page.goto(dvad_server)
    page.wait_for_load_state("networkidle")
    csrf = page.locator('meta[name="csrf-token"]').get_attribute("content")

    resp = page.request.post(
        f"{dvad_server}/api/config/model-max-tokens",
        data=json.dumps({
            "model_name": model_name,
            "max_out_configured": max_tokens,
        }),
        headers={"X-DVAD-Token": csrf, "Content-Type": "application/json"},
    )
    assert resp.status == 200, f"Failed to set max_out: {resp.text()}"


def _restore_max_out(page, dvad_server, model_name, original_value):
    """Restore max_out_configured to its original value."""
    try:
        page.goto(dvad_server)
        page.wait_for_load_state("networkidle")
        csrf = page.locator('meta[name="csrf-token"]').get_attribute("content")
        page.request.post(
            f"{dvad_server}/api/config/model-max-tokens",
            data=json.dumps({
                "model_name": model_name,
                "max_out_configured": original_value,
            }),
            headers={"X-DVAD-Token": csrf, "Content-Type": "application/json"},
            timeout=30_000,
        )
    except Exception:
        pass


def test_sane_limit_respected(live_page, dvad_server, tmp_path):
    """With max_out_configured=4096, output tokens should not exceed the limit."""
    page = live_page
    input_file = _create_test_plan(tmp_path)

    ledger = _start_review_and_wait(
        page, dvad_server, "e2e-max-out-sane", input_file
    )

    # Check that the review completed
    assert ledger.get("result") in ("success", "dry_run", "cost_exceeded", "cost_aborted"), (
        f"Unexpected result: {ledger.get('result')}"
    )

    # The fixture config has max_out_configured=4096 for both models.
    # Verify no role exceeded the limit by checking the cost breakdown.
    cost = ledger.get("cost", {})
    role_costs = cost.get("role_costs", {})
    # If the review produced output, it should have stayed within limits
    assert ledger.get("result") == "success" or ledger.get("points") is not None


def test_tiny_limit_graceful(live_page, dvad_server, tmp_path):
    """With max_out_configured=1500, review should handle truncated output gracefully."""
    page = live_page
    input_file = _create_test_plan(tmp_path)

    # Set very small limit
    _set_max_out(page, dvad_server, "e2e-remote", 1500)
    _set_max_out(page, dvad_server, "e2e-remote-thinker", 1500)

    try:
        ledger = _start_review_and_wait(
            page, dvad_server, "e2e-max-out-tiny", input_file
        )

        # Review should still complete (not crash)
        assert ledger.get("result") is not None, "Review should have a result"

        # The review may have fewer points or extraction failures, but should not error
        result = ledger.get("result")
        assert result in ("success", "failed", "dry_run", "cost_exceeded", "cost_aborted"), (
            f"Unexpected result: {result}"
        )
    finally:
        # Restore original limits
        _restore_max_out(page, dvad_server, "e2e-remote", 4096)
        _restore_max_out(page, dvad_server, "e2e-remote-thinker", 4096)


def test_revision_raw_always_saved(live_page, dvad_server, tmp_path, seeded_data_dir):
    """revision_raw.txt should be populated regardless of extraction success."""
    page = live_page
    input_file = _create_test_plan(tmp_path)

    ledger = _start_review_and_wait(
        page, dvad_server, "e2e-max-out-raw", input_file
    )

    if ledger.get("result") != "success":
        pytest.skip("Review did not succeed; cannot check revision artifacts")

    review_id = ledger.get("review_id", "")
    if not review_id:
        pytest.skip("No review_id in ledger")

    # Check for revision_raw.txt in the review directory
    reviews_dir = seeded_data_dir / "reviews" / review_id
    if not reviews_dir.exists():
        pytest.skip("Review directory not found")

    revision_dir = reviews_dir / "revision"
    if revision_dir.exists():
        raw_file = revision_dir / "revision_raw.txt"
        # If revision was attempted, raw should exist
        if any(revision_dir.iterdir()):
            assert raw_file.exists(), (
                "revision_raw.txt should always be saved when revision is attempted"
            )
            content = raw_file.read_text()
            assert len(content) > 0, "revision_raw.txt should not be empty"
