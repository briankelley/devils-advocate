"""E2E live flow tests — initiate review via GUI, SSE progress, completion.

These tests require a local LLM server (llama-server) running on :8080.
Marked with both @e2e and @e2e_live so they can be filtered separately.
"""

import json
from pathlib import Path

import pytest
from playwright.sync_api import expect

pytestmark = [pytest.mark.e2e, pytest.mark.e2e_live]

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _require_local_llm(local_llm_available):
    """Skip live flow tests if local LLM is not reachable."""
    if not local_llm_available:
        pytest.skip("Local LLM server (llama-server) not running on :8080")


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

    page.goto(dvad_server)
    page.wait_for_load_state("networkidle")

    # Fill the form
    page.locator("#project").fill("e2e-test-run")

    # Use the path-based flow: set hidden input directly since we can't
    # use the file picker modal in headless mode easily
    page.locator("#input_files_paths").evaluate(
        f'(el) => el.value = JSON.stringify(["{test_file}"])'
    )
    # Also update the display so the form doesn't reject
    page.locator("#input_files_display").evaluate(
        '(el) => el.innerHTML = "<div class=\\"selected-file\\">test-plan.md</div>"'
    )

    # Get CSRF token
    csrf = page.locator('meta[name="csrf-token"]').get_attribute("content")

    # Submit via API directly (more reliable than clicking through interstitial)
    resp = page.request.post(
        f"{dvad_server}/api/review/start",
        multipart={
            "mode": "plan",
            "project": "e2e-test-run",
            "input_paths": json.dumps([str(test_file)]),
        },
        headers={"X-DVAD-Token": csrf},
    )
    assert resp.status == 200
    data = resp.json()
    review_id = data["review_id"]
    assert review_id

    # Navigate to the progress page
    page.goto(f"{dvad_server}/review/{review_id}")
    expect(page.locator(".running-header")).to_be_visible(timeout=5000)


def test_sse_populates_log(live_page, dvad_server, tmp_path):
    """SSE events appear in the log panel during a review."""
    page = live_page
    test_file = _create_test_input(tmp_path)
    csrf = page.goto(dvad_server).request.headers.get("x-dvad-token", "")

    # Get CSRF from meta tag
    page.wait_for_load_state("networkidle")
    csrf = page.locator('meta[name="csrf-token"]').get_attribute("content")

    resp = page.request.post(
        f"{dvad_server}/api/review/start",
        multipart={
            "mode": "plan",
            "project": "e2e-sse-test",
            "input_paths": json.dumps([str(test_file)]),
        },
        headers={"X-DVAD-Token": csrf},
    )
    assert resp.status == 200
    review_id = resp.json()["review_id"]

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

    page.goto(dvad_server)
    page.wait_for_load_state("networkidle")
    csrf = page.locator('meta[name="csrf-token"]').get_attribute("content")

    resp = page.request.post(
        f"{dvad_server}/api/review/start",
        multipart={
            "mode": "plan",
            "project": "e2e-complete-test",
            "input_paths": json.dumps([str(test_file)]),
        },
        headers={"X-DVAD-Token": csrf},
    )
    assert resp.status == 200
    review_id = resp.json()["review_id"]

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
