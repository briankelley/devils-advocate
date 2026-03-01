"""E2E tests for the config page."""

import re

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


# --- Role & CoT icon state tests (fixture: e2e-remote + e2e-remote-thinker) ---

# Expected role assignments from fixtures/models.yaml
_ROLE_MAP = {
    "author": ["e2e-remote"],
    "reviewer": ["e2e-remote", "e2e-remote-thinker"],
    "deduplication": ["e2e-remote-thinker"],
    "normalization": ["e2e-remote-thinker"],
    "revision": ["e2e-remote-thinker"],
    "integration_reviewer": ["e2e-remote"],
}
_ALL_MODELS = ["e2e-remote", "e2e-remote-thinker"]
_ALL_ROLES = list(_ROLE_MAP.keys())


def test_role_icon_active_states(page, dvad_server):
    """Role icons have role-active class only for assigned (model, role) pairs."""
    page.goto(f"{dvad_server}/config")
    page.wait_for_load_state("networkidle")

    for role in _ALL_ROLES:
        for model in _ALL_MODELS:
            icon = page.locator(f'.role-icon[data-role="{role}"][data-model="{model}"]')
            if model in _ROLE_MAP[role]:
                expect(icon).to_have_class(re.compile(r"role-active"))
            else:
                expect(icon).not_to_have_class(re.compile(r"role-active"))


def test_thinking_icon_states(page, dvad_server):
    """Thinking icons reflect the model's thinking flag."""
    page.goto(f"{dvad_server}/config")
    page.wait_for_load_state("networkidle")

    expect(
        page.locator('.thinking-icon[data-model="e2e-remote-thinker"]')
    ).to_have_class(re.compile(r"thinking-active"))
    expect(
        page.locator('.thinking-icon[data-model="e2e-remote"]')
    ).not_to_have_class(re.compile(r"thinking-active"))


def test_role_summary_values(page, dvad_server):
    """Role summary section shows correct model names for each role."""
    page.goto(f"{dvad_server}/config")
    page.wait_for_load_state("networkidle")

    expectations = {
        "rs-author": "e2e-remote",
        "rs-reviewer1": "e2e-remote",
        "rs-reviewer2": "e2e-remote-thinker",
        "rs-deduplication": "e2e-remote-thinker",
        "rs-normalization": "e2e-remote-thinker",
        "rs-revision": "e2e-remote-thinker",
        "rs-integration_reviewer": "e2e-remote",
    }
    for element_id, expected_text in expectations.items():
        expect(page.locator(f"#{element_id}")).to_have_text(expected_text)


def test_role_summary_cot_icons(page, dvad_server):
    """Role summary CoT icons reflect thinking state of assigned model."""
    page.goto(f"{dvad_server}/config")
    page.wait_for_load_state("networkidle")

    cot_active = ["rsc-reviewer2", "rsc-normalization", "rsc-revision", "rsc-deduplication"]
    cot_inactive = [
        "rsc-author",
        "rsc-reviewer1",
        "rsc-integration_reviewer",
    ]
    # Note: e2e-remote has thinking=false, e2e-remote-thinker has thinking=true

    for element_id in cot_active:
        expect(page.locator(f"#{element_id}")).to_have_class(re.compile(r"cot-active"))
    for element_id in cot_inactive:
        expect(page.locator(f"#{element_id}")).not_to_have_class(re.compile(r"cot-active"))
