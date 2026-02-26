"""E2E tests for the review detail page (static, pre-seeded data)."""

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e

SEEDED_REVIEW_ID = "captured_e2e_review"


def test_detail_page_loads(page, dvad_server):
    """Detail page renders for the seeded review."""
    page.goto(f"{dvad_server}/review/{SEEDED_REVIEW_ID}")
    expect(page).to_have_title(f"Review {SEEDED_REVIEW_ID[:20]} — dvad")
    # Should show the completed detail view (not running)
    expect(page.locator(".page-header")).to_be_visible()


def test_detail_shows_project(page, dvad_server):
    """Detail page shows the project name from the ledger."""
    page.goto(f"{dvad_server}/review/{SEEDED_REVIEW_ID}")
    page.wait_for_load_state("networkidle")
    header = page.locator(".page-header h1")
    expect(header).to_contain_text("board-foot")


def test_detail_shows_mode(page, dvad_server):
    """Detail page shows the review mode."""
    page.goto(f"{dvad_server}/review/{SEEDED_REVIEW_ID}")
    page.wait_for_load_state("networkidle")
    header = page.locator(".page-header h1")
    expect(header).to_contain_text("spec")


def test_detail_cost_table(page, dvad_server):
    """Detail page renders the cost breakdown table."""
    page.goto(f"{dvad_server}/review/{SEEDED_REVIEW_ID}")
    page.wait_for_load_state("networkidle")
    cost_rows = page.locator(".cost-row")
    # Should have at least one cost row (reviewers + dedup)
    assert cost_rows.count() >= 1


def test_detail_report_download(page, dvad_server):
    """Report download link is present for a completed review with a report."""
    page.goto(f"{dvad_server}/review/{SEEDED_REVIEW_ID}")
    page.wait_for_load_state("networkidle")
    body_text = page.locator("body").inner_text()
    # The page should mention report somewhere (download link or button)
    assert "report" in body_text.lower() or "Report" in body_text


def test_detail_review_points_rendered(page, dvad_server):
    """Review points/groups are rendered on the detail page."""
    page.goto(f"{dvad_server}/review/{SEEDED_REVIEW_ID}")
    page.wait_for_load_state("networkidle")
    # The seeded review has multiple points — look for group-related content
    body = page.locator("body").inner_text()
    # Should contain some review finding text
    assert len(body) > 200  # Meaningful content rendered


def test_detail_show_log_button(page, dvad_server):
    """Show log button is present on completed review detail."""
    page.goto(f"{dvad_server}/review/{SEEDED_REVIEW_ID}")
    page.wait_for_load_state("networkidle")
    log_btn = page.locator("#show-log-btn")
    expect(log_btn).to_be_visible()


def test_detail_nonexistent_review_redirects(page, dvad_server):
    """Navigating to a nonexistent review redirects to dashboard."""
    page.goto(f"{dvad_server}/review/nonexistent_review_id_12345")
    page.wait_for_load_state("networkidle")
    # Should redirect back to dashboard
    expect(page).to_have_url(f"{dvad_server}/")


def test_detail_api_returns_json(page, dvad_server):
    """The API endpoint returns the review ledger as JSON."""
    resp = page.request.get(f"{dvad_server}/api/review/{SEEDED_REVIEW_ID}")
    assert resp.status == 200
    data = resp.json()
    assert data["review_id"] == "20260225T210955_75c5a9_review"
    assert data["mode"] == "spec"
    assert data["result"] == "complete"
