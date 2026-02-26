"""E2E live flow tests — initiate review via GUI, SSE progress, completion.

These tests use the local_llm fixture which auto-launches llama-server if needed.
Marked with both @e2e and @e2e_live so they can be filtered separately.
"""

import json
import time
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import expect

pytestmark = [pytest.mark.e2e, pytest.mark.e2e_live]

FIXTURES = Path(__file__).parent / "fixtures"


def _start_review(page, dvad_server: str, project: str, input_file) -> str:
    """Start a review, waiting for any prior review to finish first.

    Returns the review_id.
    """
    page.goto(dvad_server)
    page.wait_for_load_state("networkidle")
    csrf = page.locator('meta[name="csrf-token"]').get_attribute("content")

    deadline = time.monotonic() + 300
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
            return resp.json()["review_id"]
        if resp.status == 409:
            time.sleep(5)
            continue
        pytest.fail(f"Unexpected status {resp.status} starting review")
    pytest.fail("Timed out waiting for prior review to finish")


@pytest.fixture(autouse=True)
def _require_local_llm(local_llm):
    """Ensure local LLM is running before live flow tests."""


def _create_test_input(tmp_path: Path) -> Path:
    """Create a small test plan file for review submission."""
    test_file = tmp_path / "test-plan.md"
    test_file.write_text(
        "# Test Plan\n\n"
        "## Overview\n"
        "This is a small test plan for E2E testing.\n\n"
        "## Steps\n"
        "1. Initialize the module\n"
        "2. Validate inputs\n"
        "3. Run processing\n"
        "4. Return results\n\n"
        "## Considerations\n"
        "- Error handling for invalid inputs\n"
        "- Performance under load\n"
    )
    return test_file


def test_initiate_review(live_page, dvad_server, tmp_path):
    """Submit a new review via the dashboard form and verify redirect to progress page."""
    page = live_page
    test_file = _create_test_input(tmp_path)

    review_id = _start_review(page, dvad_server, "e2e-test-run", test_file)
    assert review_id

    # Navigate to the progress page
    page.goto(f"{dvad_server}/review/{review_id}")
    expect(page.locator(".running-header")).to_be_visible(timeout=5000)


def test_sse_populates_log(live_page, dvad_server, tmp_path):
    """SSE events appear in the log panel during a review."""
    page = live_page
    test_file = _create_test_input(tmp_path)

    review_id = _start_review(page, dvad_server, "e2e-sse-test", test_file)

    # Navigate to progress page
    page.goto(f"{dvad_server}/review/{review_id}")
    expect(page.locator(".log-output")).to_be_visible(timeout=10000)

    # Wait for SSE content to appear in the log
    page.wait_for_function(
        "document.querySelector('#log-output')?.innerText?.length > 10",
        timeout=120_000,
    )

    log_text = page.locator("#log-output").inner_text()
    assert len(log_text) > 10


def test_review_completes(live_page, dvad_server, tmp_path):
    """A review runs to completion and transitions to the results view."""
    page = live_page
    test_file = _create_test_input(tmp_path)

    review_id = _start_review(page, dvad_server, "e2e-complete-test", test_file)

    page.goto(f"{dvad_server}/review/{review_id}")

    # Wait for the review to complete (transition from running to detail view)
    # The page auto-reloads on completion via SSE
    page.wait_for_selector(".page-header", timeout=300_000)

    # Verify we're on the completed detail page
    body = page.locator("body").inner_text()
    assert "e2e-complete-test" in body

    # Cost table should be populated
    cost_rows = page.locator(".cost-row")
    assert cost_rows.count() >= 1
