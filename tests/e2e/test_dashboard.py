"""E2E tests for the dashboard page."""

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e


def test_dashboard_loads(page, dvad_server):
    """Dashboard renders with title, nav, and mode cards."""
    page.goto(dvad_server)
    expect(page).to_have_title("Dashboard — dvad")
    expect(page.locator(".mode-cards")).to_be_visible()
    expect(page.locator("#review-form")).to_be_visible()


def test_dashboard_mode_cards(page, dvad_server):
    """All four mode cards (spec, code, integration, plan) are present."""
    page.goto(dvad_server)
    expect(page.locator(".mode-card")).to_have_count(4)
    # Plan radio is checked by default
    expect(page.locator('input[type="radio"][name="mode"][value="spec"]')).to_be_checked()


def test_dashboard_mode_selection(page, dvad_server):
    """Clicking a mode card selects its radio button."""
    page.goto(dvad_server)
    page.locator(".mode-card--code").click()
    expect(page.locator('input[type="radio"][name="mode"][value="code"]')).to_be_checked()


def test_dashboard_with_seeded_review(page, dvad_server):
    """Seeded review appears in the reviews table."""
    page.goto(dvad_server)
    page.wait_for_load_state("networkidle")
    table = page.locator("#reviews-table")
    expect(table).to_be_visible()
    # The seeded review's project name should appear in the table
    expect(table.locator("text=board-foot")).to_be_visible(timeout=5000)


def test_dashboard_review_table_columns(page, dvad_server):
    """Reviews table has the expected column headers."""
    page.goto(dvad_server)
    headers = page.locator("#reviews-table thead th")
    header_texts = [h.lower() for h in headers.all_inner_texts()]
    assert "review id" in header_texts[0]
    assert "project" in header_texts[1]
    assert "result" in header_texts[2]
    assert "mode" in header_texts[3]


def test_dashboard_review_row_clickable(page, dvad_server):
    """Clicking a review row navigates to the detail page."""
    page.goto(dvad_server)
    page.wait_for_load_state("networkidle")
    row = page.locator(".clickable-row").first
    expect(row).to_be_visible(timeout=5000)
    href = row.get_attribute("data-href")
    assert href and href.startswith("/review/")


def test_dashboard_project_field(page, dvad_server):
    """Project name input is present and required."""
    page.goto(dvad_server)
    project = page.locator("#project")
    expect(project).to_be_visible()
    expect(project).to_have_attribute("required", "")


def test_dashboard_nav_to_config(page, dvad_server):
    """Nav bar has a link to the config page."""
    page.goto(dvad_server)
    config_link = page.locator('.nav-links a[href="/config"]')
    expect(config_link).to_be_visible()


def test_dashboard_new_review_button(page, dvad_server):
    """Submit button for new reviews is present."""
    page.goto(dvad_server)
    expect(page.locator("#submit-btn")).to_be_visible()
    expect(page.locator("#submit-btn")).to_have_text("New Review")
