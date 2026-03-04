"""E2E tests for the config page."""

import re
from pathlib import Path

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e

_FIXTURE_YAML = (Path(__file__).parent / "fixtures" / "models.yaml").read_text()


# ── Restore fixture ──────────────────────────────────────────────────────────

@pytest.fixture
def restore_config(page, dvad_server):
    """Auto-restore fixture config after each interactive test."""
    yield
    _restore_fixture_config(page, dvad_server)


def _restore_fixture_config(page, dvad_server):
    """POST original fixture YAML back via API to restore clean state."""
    page.goto(f"{dvad_server}/config")
    page.wait_for_load_state("networkidle")
    csrf = page.locator('meta[name="csrf-token"]').get_attribute("content")
    import json
    page.request.post(
        f"{dvad_server}/api/config",
        data=json.dumps({"yaml": _FIXTURE_YAML}),
        headers={"X-DVAD-Token": csrf, "Content-Type": "application/json"},
    )


# ── Helpers ──────────────────────────────────────────────────────────────────

def _save_and_wait(page):
    """Click the save toast button and wait for API response."""
    with page.expect_response("**/api/config") as resp_info:
        page.locator("#save-roles-toast .btn-accent").click()
    resp = resp_info.value
    assert resp.status == 200


def _goto_config(page, dvad_server):
    """Navigate to config and wait for networkidle."""
    page.goto(f"{dvad_server}/config")
    page.wait_for_load_state("networkidle")


def _goto_dashboard(page, dvad_server):
    """Navigate to dashboard and wait for networkidle."""
    page.goto(f"{dvad_server}/")
    page.wait_for_load_state("networkidle")


def _verify_dashboard_roles(page, dvad_server, expected):
    """Navigate to dashboard and verify sidebar role assignments.

    expected: list of dicts {label, model, thinking} in order:
    Author, Reviewer 1, Reviewer 2, Dedup, Normalization, Revision, Integration
    """
    _goto_dashboard(page, dvad_server)
    rows = page.locator(".dashboard-roles .role-summary-row")
    assert rows.count() == len(expected)
    for i, exp in enumerate(expected):
        row = rows.nth(i)
        if exp.get("label"):
            expect(row.locator(".role-summary-label")).to_contain_text(exp["label"])
        value = row.locator(".role-summary-value")
        if exp["model"]:
            expect(value).to_have_text(exp["model"])
        else:
            expect(value).to_have_text("unassigned")
        cot = row.locator(".role-summary-cot")
        if exp.get("thinking"):
            expect(cot).to_have_class(re.compile(r"cot-active"))
        else:
            expect(cot).not_to_have_class(re.compile(r"cot-active"))


# Default fixture state for dashboard verification
_FIXTURE_DASHBOARD = [
    {"label": "Author", "model": "e2e-remote", "thinking": False},
    {"label": "Reviewer 1", "model": "e2e-remote", "thinking": False},
    {"label": "Reviewer 2", "model": "e2e-remote-thinker", "thinking": True},
    {"label": "Dedup", "model": "e2e-remote-thinker", "thinking": True},
    {"label": "Normalization", "model": "e2e-remote-thinker", "thinking": True},
    {"label": "Revision", "model": "e2e-remote-thinker", "thinking": True},
    {"label": "Integration", "model": "e2e-remote", "thinking": False},
]


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


# ── Interactive role & CoT tests ─────────────────────────────────────────────


def test_role_toggle_author_radio(page, dvad_server, restore_config):
    """Reassign author via radio-select, verify persistence across navigation."""
    _goto_config(page, dvad_server)

    # Initial: author on e2e-remote
    author_remote = page.locator('.role-icon[data-role="author"][data-model="e2e-remote"]')
    author_thinker = page.locator('.role-icon[data-role="author"][data-model="e2e-remote-thinker"]')
    expect(author_remote).to_have_class(re.compile(r"role-active"))
    expect(author_thinker).not_to_have_class(re.compile(r"role-active"))

    # Click to reassign author to thinker
    author_thinker.click()
    expect(author_thinker).to_have_class(re.compile(r"role-active"))
    expect(author_remote).not_to_have_class(re.compile(r"role-active"))
    expect(page.locator("#rs-author")).to_have_text("e2e-remote-thinker")

    _save_and_wait(page)

    # Dashboard check
    expected = [d.copy() for d in _FIXTURE_DASHBOARD]
    expected[0] = {"label": "Author", "model": "e2e-remote-thinker", "thinking": True}
    _verify_dashboard_roles(page, dvad_server, expected)

    # Navigate back to config (forward), verify persistence
    _goto_config(page, dvad_server)
    expect(page.locator('.role-icon[data-role="author"][data-model="e2e-remote-thinker"]')).to_have_class(re.compile(r"role-active"))
    expect(page.locator("#rs-author")).to_have_text("e2e-remote-thinker")


def test_role_toggle_dedup_radio(page, dvad_server, restore_config):
    """Reassign dedup from thinker to remote."""
    _goto_config(page, dvad_server)

    dedup_thinker = page.locator('.role-icon[data-role="deduplication"][data-model="e2e-remote-thinker"]')
    dedup_remote = page.locator('.role-icon[data-role="deduplication"][data-model="e2e-remote"]')
    expect(dedup_thinker).to_have_class(re.compile(r"role-active"))

    dedup_remote.click()
    expect(dedup_remote).to_have_class(re.compile(r"role-active"))
    expect(dedup_thinker).not_to_have_class(re.compile(r"role-active"))
    expect(page.locator("#rs-deduplication")).to_have_text("e2e-remote")

    _save_and_wait(page)

    expected = [d.copy() for d in _FIXTURE_DASHBOARD]
    expected[3] = {"label": "Dedup", "model": "e2e-remote", "thinking": False}
    _verify_dashboard_roles(page, dvad_server, expected)

    _goto_config(page, dvad_server)
    expect(page.locator('.role-icon[data-role="deduplication"][data-model="e2e-remote"]')).to_have_class(re.compile(r"role-active"))


def test_role_toggle_normalization_radio(page, dvad_server, restore_config):
    """Reassign normalization from thinker to remote."""
    _goto_config(page, dvad_server)

    page.locator('.role-icon[data-role="normalization"][data-model="e2e-remote"]').click()
    expect(page.locator('.role-icon[data-role="normalization"][data-model="e2e-remote"]')).to_have_class(re.compile(r"role-active"))
    expect(page.locator('.role-icon[data-role="normalization"][data-model="e2e-remote-thinker"]')).not_to_have_class(re.compile(r"role-active"))
    expect(page.locator("#rs-normalization")).to_have_text("e2e-remote")

    _save_and_wait(page)

    expected = [d.copy() for d in _FIXTURE_DASHBOARD]
    expected[4] = {"label": "Normalization", "model": "e2e-remote", "thinking": False}
    _verify_dashboard_roles(page, dvad_server, expected)

    _goto_config(page, dvad_server)
    expect(page.locator('.role-icon[data-role="normalization"][data-model="e2e-remote"]')).to_have_class(re.compile(r"role-active"))


def test_role_toggle_revision_radio(page, dvad_server, restore_config):
    """Reassign revision from thinker to remote."""
    _goto_config(page, dvad_server)

    page.locator('.role-icon[data-role="revision"][data-model="e2e-remote"]').click()
    expect(page.locator('.role-icon[data-role="revision"][data-model="e2e-remote"]')).to_have_class(re.compile(r"role-active"))
    expect(page.locator('.role-icon[data-role="revision"][data-model="e2e-remote-thinker"]')).not_to_have_class(re.compile(r"role-active"))
    expect(page.locator("#rs-revision")).to_have_text("e2e-remote")

    _save_and_wait(page)

    expected = [d.copy() for d in _FIXTURE_DASHBOARD]
    expected[5] = {"label": "Revision", "model": "e2e-remote", "thinking": False}
    _verify_dashboard_roles(page, dvad_server, expected)

    _goto_config(page, dvad_server)
    expect(page.locator('.role-icon[data-role="revision"][data-model="e2e-remote"]')).to_have_class(re.compile(r"role-active"))


def test_role_toggle_integration_radio(page, dvad_server, restore_config):
    """Reassign integration_reviewer from remote to thinker."""
    _goto_config(page, dvad_server)

    page.locator('.role-icon[data-role="integration_reviewer"][data-model="e2e-remote-thinker"]').click()
    expect(page.locator('.role-icon[data-role="integration_reviewer"][data-model="e2e-remote-thinker"]')).to_have_class(re.compile(r"role-active"))
    expect(page.locator('.role-icon[data-role="integration_reviewer"][data-model="e2e-remote"]')).not_to_have_class(re.compile(r"role-active"))
    expect(page.locator("#rs-integration_reviewer")).to_have_text("e2e-remote-thinker")

    _save_and_wait(page)

    expected = [d.copy() for d in _FIXTURE_DASHBOARD]
    expected[6] = {"label": "Integration", "model": "e2e-remote-thinker", "thinking": True}
    _verify_dashboard_roles(page, dvad_server, expected)

    _goto_config(page, dvad_server)
    expect(page.locator('.role-icon[data-role="integration_reviewer"][data-model="e2e-remote-thinker"]')).to_have_class(re.compile(r"role-active"))


def test_role_toggle_reviewer_ceiling(page, dvad_server, restore_config):
    """Reviewer uses checkbox with ceiling=2. Remove one, re-add."""
    _goto_config(page, dvad_server)

    rev_remote = page.locator('.role-icon[data-role="reviewer"][data-model="e2e-remote"]')
    rev_thinker = page.locator('.role-icon[data-role="reviewer"][data-model="e2e-remote-thinker"]')
    expect(rev_remote).to_have_class(re.compile(r"role-active"))
    expect(rev_thinker).to_have_class(re.compile(r"role-active"))

    # Unassign e2e-remote as reviewer
    rev_remote.click()
    expect(rev_remote).not_to_have_class(re.compile(r"role-active"))
    expect(page.locator("#rs-reviewer1")).to_have_text("e2e-remote-thinker")
    # reviewer2 should show em-dash or unassigned
    rv2_text = page.locator("#rs-reviewer2").inner_text()
    assert rv2_text in ("\u2014", "unassigned", "—")

    _save_and_wait(page)

    expected = [d.copy() for d in _FIXTURE_DASHBOARD]
    expected[1] = {"label": "Reviewer 1", "model": "e2e-remote-thinker", "thinking": True}
    expected[2] = {"label": "Reviewer 2", "model": None, "thinking": False}
    _verify_dashboard_roles(page, dvad_server, expected)

    # Navigate back and re-add
    _goto_config(page, dvad_server)
    expect(page.locator('.role-icon[data-role="reviewer"][data-model="e2e-remote"]')).not_to_have_class(re.compile(r"role-active"))

    page.locator('.role-icon[data-role="reviewer"][data-model="e2e-remote"]').click()
    expect(page.locator('.role-icon[data-role="reviewer"][data-model="e2e-remote"]')).to_have_class(re.compile(r"role-active"))

    _save_and_wait(page)

    # Verify 2 reviewers restored on dashboard
    expected2 = [d.copy() for d in _FIXTURE_DASHBOARD]
    # After re-add, JS sends reviewers in DOM order (remote first, thinker second)
    expected2[1] = {"label": "Reviewer 1", "model": "e2e-remote", "thinking": False}
    expected2[2] = {"label": "Reviewer 2", "model": "e2e-remote-thinker", "thinking": True}
    _verify_dashboard_roles(page, dvad_server, expected2)


def test_role_unassign_and_reassign(page, dvad_server, restore_config):
    """Unassign dedup entirely, save, verify, then reassign."""
    _goto_config(page, dvad_server)

    # Unassign dedup (click thinker to toggle off — radio role click on active = unassign)
    page.locator('.role-icon[data-role="deduplication"][data-model="e2e-remote-thinker"]').click()
    # Both should be inactive now
    expect(page.locator('.role-icon[data-role="deduplication"][data-model="e2e-remote-thinker"]')).not_to_have_class(re.compile(r"role-active"))
    expect(page.locator('.role-icon[data-role="deduplication"][data-model="e2e-remote"]')).not_to_have_class(re.compile(r"role-active"))
    dedup_text = page.locator("#rs-deduplication").inner_text()
    assert dedup_text.lower() in ("unassigned", "—", "\u2014")

    _save_and_wait(page)

    expected = [d.copy() for d in _FIXTURE_DASHBOARD]
    expected[3] = {"label": "Dedup", "model": None, "thinking": False}
    _verify_dashboard_roles(page, dvad_server, expected)

    # Re-assign
    _goto_config(page, dvad_server)
    page.locator('.role-icon[data-role="deduplication"][data-model="e2e-remote-thinker"]').click()
    expect(page.locator('.role-icon[data-role="deduplication"][data-model="e2e-remote-thinker"]')).to_have_class(re.compile(r"role-active"))
    expect(page.locator("#rs-deduplication")).to_have_text("e2e-remote-thinker")

    _save_and_wait(page)
    _verify_dashboard_roles(page, dvad_server, _FIXTURE_DASHBOARD)


def test_multi_role_reassignment_single_save(page, dvad_server, restore_config):
    """Multiple role changes saved in one operation."""
    _goto_config(page, dvad_server)

    # Reassign author to thinker
    page.locator('.role-icon[data-role="author"][data-model="e2e-remote-thinker"]').click()
    # Reassign dedup to remote
    page.locator('.role-icon[data-role="deduplication"][data-model="e2e-remote"]').click()
    # Reassign normalization to remote
    page.locator('.role-icon[data-role="normalization"][data-model="e2e-remote"]').click()

    expect(page.locator("#rs-author")).to_have_text("e2e-remote-thinker")
    expect(page.locator("#rs-deduplication")).to_have_text("e2e-remote")
    expect(page.locator("#rs-normalization")).to_have_text("e2e-remote")

    _save_and_wait(page)

    expected = [d.copy() for d in _FIXTURE_DASHBOARD]
    expected[0] = {"label": "Author", "model": "e2e-remote-thinker", "thinking": True}
    expected[3] = {"label": "Dedup", "model": "e2e-remote", "thinking": False}
    expected[4] = {"label": "Normalization", "model": "e2e-remote", "thinking": False}
    _verify_dashboard_roles(page, dvad_server, expected)

    _goto_config(page, dvad_server)
    expect(page.locator("#rs-author")).to_have_text("e2e-remote-thinker")
    expect(page.locator("#rs-deduplication")).to_have_text("e2e-remote")
    expect(page.locator("#rs-normalization")).to_have_text("e2e-remote")


def test_multi_model_role_swap(page, dvad_server, restore_config):
    """Swap all singular roles between the two models in one save."""
    _goto_config(page, dvad_server)

    # Swap each singular role to the other model
    singular_roles = ["author", "deduplication", "normalization", "revision", "integration_reviewer"]
    swap_targets = {
        "author": "e2e-remote-thinker",
        "deduplication": "e2e-remote",
        "normalization": "e2e-remote",
        "revision": "e2e-remote",
        "integration_reviewer": "e2e-remote-thinker",
    }
    for role, target_model in swap_targets.items():
        page.locator(f'.role-icon[data-role="{role}"][data-model="{target_model}"]').click()

    expect(page.locator("#rs-author")).to_have_text("e2e-remote-thinker")
    expect(page.locator("#rs-deduplication")).to_have_text("e2e-remote")
    expect(page.locator("#rs-normalization")).to_have_text("e2e-remote")
    expect(page.locator("#rs-revision")).to_have_text("e2e-remote")
    expect(page.locator("#rs-integration_reviewer")).to_have_text("e2e-remote-thinker")

    _save_and_wait(page)

    expected = [d.copy() for d in _FIXTURE_DASHBOARD]
    expected[0] = {"label": "Author", "model": "e2e-remote-thinker", "thinking": True}
    expected[3] = {"label": "Dedup", "model": "e2e-remote", "thinking": False}
    expected[4] = {"label": "Normalization", "model": "e2e-remote", "thinking": False}
    expected[5] = {"label": "Revision", "model": "e2e-remote", "thinking": False}
    expected[6] = {"label": "Integration", "model": "e2e-remote-thinker", "thinking": True}
    _verify_dashboard_roles(page, dvad_server, expected)

    _goto_config(page, dvad_server)
    expect(page.locator("#rs-author")).to_have_text("e2e-remote-thinker")
    expect(page.locator("#rs-integration_reviewer")).to_have_text("e2e-remote-thinker")


def test_thinking_toggle_persists_across_navigation(page, dvad_server, restore_config):
    """Disable CoT on thinker, verify persistence, restore."""
    _goto_config(page, dvad_server)

    brain_thinker = page.locator('.thinking-icon[data-model="e2e-remote-thinker"]')
    expect(brain_thinker).to_have_class(re.compile(r"thinking-active"))

    # Verify CoT active in role summary for thinker-assigned roles
    for eid in ["rsc-reviewer2", "rsc-deduplication", "rsc-normalization", "rsc-revision"]:
        expect(page.locator(f"#{eid}")).to_have_class(re.compile(r"cot-active"))

    # Click to disable
    with page.expect_response("**/api/config/model-thinking"):
        brain_thinker.click()

    expect(brain_thinker).not_to_have_class(re.compile(r"thinking-active"))
    for eid in ["rsc-reviewer2", "rsc-deduplication", "rsc-normalization", "rsc-revision"]:
        expect(page.locator(f"#{eid}")).not_to_have_class(re.compile(r"cot-active"))

    # Dashboard verification
    expected = [d.copy() for d in _FIXTURE_DASHBOARD]
    for i in [2, 3, 4, 5]:  # reviewer2, dedup, norm, revision
        expected[i]["thinking"] = False
    _verify_dashboard_roles(page, dvad_server, expected)

    # Config persistence
    _goto_config(page, dvad_server)
    expect(page.locator('.thinking-icon[data-model="e2e-remote-thinker"]')).not_to_have_class(re.compile(r"thinking-active"))

    # Teardown: re-enable
    with page.expect_response("**/api/config/model-thinking"):
        page.locator('.thinking-icon[data-model="e2e-remote-thinker"]').click()
    expect(page.locator('.thinking-icon[data-model="e2e-remote-thinker"]')).to_have_class(re.compile(r"thinking-active"))


def test_thinking_toggle_enable_on_non_thinker(page, dvad_server, restore_config):
    """Enable thinking on e2e-remote (starts with thinking=false)."""
    _goto_config(page, dvad_server)

    brain_remote = page.locator('.thinking-icon[data-model="e2e-remote"]')
    expect(brain_remote).not_to_have_class(re.compile(r"thinking-active"))
    expect(brain_remote).to_have_class(re.compile(r"thinking-eligible"))

    with page.expect_response("**/api/config/model-thinking"):
        brain_remote.click()

    expect(brain_remote).to_have_class(re.compile(r"thinking-active"))
    # CoT should now be active for e2e-remote assigned roles
    for eid in ["rsc-author", "rsc-reviewer1", "rsc-integration_reviewer"]:
        expect(page.locator(f"#{eid}")).to_have_class(re.compile(r"cot-active"))

    # Dashboard
    expected = [d.copy() for d in _FIXTURE_DASHBOARD]
    expected[0]["thinking"] = True   # Author
    expected[1]["thinking"] = True   # Reviewer 1
    expected[6]["thinking"] = True   # Integration
    _verify_dashboard_roles(page, dvad_server, expected)

    # Persistence
    _goto_config(page, dvad_server)
    expect(page.locator('.thinking-icon[data-model="e2e-remote"]')).to_have_class(re.compile(r"thinking-active"))

    # Teardown: disable
    with page.expect_response("**/api/config/model-thinking"):
        page.locator('.thinking-icon[data-model="e2e-remote"]').click()


def test_thinking_toggle_blocked_without_role(page, dvad_server, restore_config):
    """Brain click is no-op when model has no role assignments."""
    _goto_config(page, dvad_server)

    # Remove all roles from e2e-remote: author, reviewer, integration
    page.locator('.role-icon[data-role="author"][data-model="e2e-remote"]').click()
    page.locator('.role-icon[data-role="reviewer"][data-model="e2e-remote"]').click()
    page.locator('.role-icon[data-role="integration_reviewer"][data-model="e2e-remote"]').click()

    _save_and_wait(page)

    # Reload to get clean state from server
    _goto_config(page, dvad_server)

    brain_remote = page.locator('.thinking-icon[data-model="e2e-remote"]')
    expect(brain_remote).not_to_have_class(re.compile(r"thinking-eligible"))
    expect(brain_remote).not_to_have_class(re.compile(r"thinking-active"))

    # Click should be a no-op
    brain_remote.click()
    # Small wait to ensure any async request would have fired
    page.wait_for_timeout(500)
    expect(brain_remote).not_to_have_class(re.compile(r"thinking-active"))


def test_thinking_plus_role_change_combined(page, dvad_server, restore_config):
    """Role change + thinking change in sequence, verify both persist."""
    _goto_config(page, dvad_server)

    # Enable thinking on e2e-remote
    with page.expect_response("**/api/config/model-thinking"):
        page.locator('.thinking-icon[data-model="e2e-remote"]').click()
    expect(page.locator('.thinking-icon[data-model="e2e-remote"]')).to_have_class(re.compile(r"thinking-active"))

    # Reassign author and integration to thinker
    page.locator('.role-icon[data-role="author"][data-model="e2e-remote-thinker"]').click()
    page.locator('.role-icon[data-role="integration_reviewer"][data-model="e2e-remote-thinker"]').click()

    expect(page.locator("#rs-author")).to_have_text("e2e-remote-thinker")
    expect(page.locator("#rs-integration_reviewer")).to_have_text("e2e-remote-thinker")

    _save_and_wait(page)

    # Dashboard: author=thinker(cot), integration=thinker(cot), reviewer1=remote(cot now on)
    expected = [d.copy() for d in _FIXTURE_DASHBOARD]
    expected[0] = {"label": "Author", "model": "e2e-remote-thinker", "thinking": True}
    expected[1] = {"label": "Reviewer 1", "model": "e2e-remote", "thinking": True}  # thinking enabled
    expected[6] = {"label": "Integration", "model": "e2e-remote-thinker", "thinking": True}
    _verify_dashboard_roles(page, dvad_server, expected)

    _goto_config(page, dvad_server)
    expect(page.locator("#rs-author")).to_have_text("e2e-remote-thinker")
    expect(page.locator('.thinking-icon[data-model="e2e-remote"]')).to_have_class(re.compile(r"thinking-active"))


def test_dashboard_reflects_unsaved_does_not_leak(page, dvad_server):
    """Unsaved config page changes must NOT appear on dashboard."""
    _goto_config(page, dvad_server)

    # Reassign author but do NOT save
    page.locator('.role-icon[data-role="author"][data-model="e2e-remote-thinker"]').click()
    expect(page.locator("#rs-author")).to_have_text("e2e-remote-thinker")

    # Dashboard should still show original
    _goto_dashboard(page, dvad_server)
    rows = page.locator(".dashboard-roles .role-summary-row")
    author_row = rows.nth(0)
    expect(author_row.locator(".role-summary-value")).to_have_text("e2e-remote")

    # Navigate back to config — page reloads from disk, original restored
    _goto_config(page, dvad_server)
    expect(page.locator("#rs-author")).to_have_text("e2e-remote")


def test_rapid_navigation_cycle(page, dvad_server, restore_config):
    """Multiple rapid round-trips between config and dashboard."""
    _goto_config(page, dvad_server)

    # Record initial state
    author_text = page.locator("#rs-author").inner_text()
    assert author_text == "e2e-remote"

    # Rapid round-trips — state should be stable
    for _ in range(2):
        _goto_dashboard(page, dvad_server)
        _goto_config(page, dvad_server)
        expect(page.locator("#rs-author")).to_have_text("e2e-remote")

    # Make a change, save
    page.locator('.role-icon[data-role="author"][data-model="e2e-remote-thinker"]').click()
    _save_and_wait(page)

    # Rapid verification cycles
    for _ in range(2):
        _goto_dashboard(page, dvad_server)
        rows = page.locator(".dashboard-roles .role-summary-row")
        expect(rows.nth(0).locator(".role-summary-value")).to_have_text("e2e-remote-thinker")
        _goto_config(page, dvad_server)
        expect(page.locator("#rs-author")).to_have_text("e2e-remote-thinker")


def test_full_role_wipe_and_rebuild(page, dvad_server, restore_config):
    """Unassign every role, save, verify empty, then rebuild from scratch."""
    _goto_config(page, dvad_server)

    # Click every active role icon to unassign all roles
    active_icons = page.locator(".role-icon.role-active")
    count = active_icons.count()
    # Click each one (collect data-role+data-model first to avoid stale refs)
    icon_targets = []
    for i in range(count):
        icon = active_icons.nth(i)
        role = icon.get_attribute("data-role")
        model = icon.get_attribute("data-model")
        icon_targets.append((role, model))

    for role, model in icon_targets:
        icon = page.locator(f'.role-icon[data-role="{role}"][data-model="{model}"]')
        if icon.evaluate("el => el.classList.contains('role-active')"):
            icon.click()

    _save_and_wait(page)

    # Dashboard: all unassigned
    expected_empty = [
        {"label": "Author", "model": None, "thinking": False},
        {"label": "Reviewer 1", "model": None, "thinking": False},
        {"label": "Reviewer 2", "model": None, "thinking": False},
        {"label": "Dedup", "model": None, "thinking": False},
        {"label": "Normalization", "model": None, "thinking": False},
        {"label": "Revision", "model": None, "thinking": False},
        {"label": "Integration", "model": None, "thinking": False},
    ]
    _verify_dashboard_roles(page, dvad_server, expected_empty)

    # Config: all inactive
    _goto_config(page, dvad_server)
    active_after = page.locator(".role-icon.role-active")
    assert active_after.count() == 0

    # Rebuild: reassign all roles back to fixture state
    fixture_assignments = [
        ("author", "e2e-remote"),
        ("reviewer", "e2e-remote"),
        ("reviewer", "e2e-remote-thinker"),
        ("deduplication", "e2e-remote-thinker"),
        ("normalization", "e2e-remote-thinker"),
        ("revision", "e2e-remote-thinker"),
        ("integration_reviewer", "e2e-remote"),
    ]
    for role, model in fixture_assignments:
        page.locator(f'.role-icon[data-role="{role}"][data-model="{model}"]').click()

    _save_and_wait(page)
    _verify_dashboard_roles(page, dvad_server, _FIXTURE_DASHBOARD)
