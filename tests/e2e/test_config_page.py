"""E2E tests for the config page."""

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e


def test_config_page_loads(page, dvad_server):
    """Config page renders with title and structured tab."""
    page.goto(f"{dvad_server}/config")
    expect(page).to_have_title("Config — dvad")
    expect(page.locator(".page-header h1")).to_have_text("Configuration")


def test_config_tab_bar(page, dvad_server):
    """Tab bar with Structured and Raw YAML tabs is present."""
    page.goto(f"{dvad_server}/config")
    structured_tab = page.locator('.tab-btn[data-tab="structured"]')
    raw_tab = page.locator('.tab-btn[data-tab="raw"]')
    expect(structured_tab).to_be_visible()
    expect(raw_tab).to_be_visible()
    # Structured tab should be active by default
    expect(structured_tab).to_have_class("tab-btn active")


def test_config_paths_displayed(page, dvad_server):
    """Config meta section shows path information."""
    page.goto(f"{dvad_server}/config")
    paths = page.locator(".config-paths")
    expect(paths).to_be_visible()
    # Should show binary, config, data paths
    path_text = paths.inner_text()
    assert "Binary:" in path_text
    assert "Config:" in path_text


def test_config_model_cards_rendered(page, dvad_server):
    """Structured tab renders model cards."""
    page.goto(f"{dvad_server}/config")
    page.wait_for_load_state("networkidle")
    model_cards = page.locator(".model-card-collapsible")
    # Should have at least one model card (from E2E config or user config)
    assert model_cards.count() >= 1


def test_config_switch_to_raw_tab(page, dvad_server):
    """Switching to raw YAML tab shows the editor."""
    page.goto(f"{dvad_server}/config")
    page.locator('.tab-btn[data-tab="raw"]').click()
    raw_tab = page.locator("#tab-raw")
    expect(raw_tab).to_be_visible()


def test_config_api_returns_json(page, dvad_server):
    """The config API endpoint returns model data as JSON."""
    resp = page.request.get(f"{dvad_server}/api/config")
    assert resp.status == 200
    data = resp.json()
    assert "models" in data
    assert "config_path" in data
