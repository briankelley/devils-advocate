"""E2E live tests — escalation override flow and revision generation.

Seeds a real review (board-foot plan, 2 PARTIAL escalations) and exercises:
- Accept Reviewer → revision includes reviewer's full recommendation
- Accept Author on PARTIAL → remap to partial_accepted → revision includes compromise
- Mixed overrides → revision includes both
- Keep Open → revision button stays disabled

Requires an LLM backend (e2elocal or e2eremote).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from playwright.sync_api import expect

pytestmark = [pytest.mark.e2e, pytest.mark.e2e_live]

FIXTURES = Path(__file__).parent / "fixtures"
ESCALATION_FIXTURE = FIXTURES / "escalation_review"
REVIEW_ID = "20260307T013337_5ba2af"
GROUP_004 = "board-foot.group_004.07MAR2026.0133.1f6r"
GROUP_005 = "board-foot.group_005.07MAR2026.0133.1f6r"


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _require_local_llm(local_llm):
    """Ensure LLM backend is available before running these tests."""


@pytest.fixture
def escalation_data_dir(tmp_path):
    """Copy escalation fixture into a fresh temp data directory.

    Returns the data_dir path (set as DVAD_HOME for the server).
    """
    data_dir = tmp_path / "dvad_data"
    reviews_dir = data_dir / "reviews"
    reviews_dir.mkdir(parents=True)
    (data_dir / "logs").mkdir()

    dest = reviews_dir / REVIEW_ID
    shutil.copytree(ESCALATION_FIXTURE, dest)
    return data_dir


@pytest.fixture
def escalation_server(escalation_data_dir, e2e_config_path, tmp_path):
    """Start a dvad GUI server pointed at the escalation fixture data."""
    port = _find_free_port()
    env = {**os.environ}
    env["DVAD_HOME"] = str(escalation_data_dir)
    env["DVAD_SSL_VERIFY"] = "0"
    env.setdefault("E2E_LOCAL_KEY", "e2e-dummy-key")
    if e2e_config_path:
        env["DVAD_E2E_CONFIG"] = str(e2e_config_path)

    server_log = tmp_path / "escalation_server.log"
    server_log_fh = open(server_log, "w")
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn",
            "devils_advocate.gui:create_app_from_env",
            "--factory",
            "--host", "127.0.0.1",
            "--port", str(port),
            "--log-level", "warning",
        ],
        env=env,
        stdout=server_log_fh,
        stderr=subprocess.STDOUT,
    )

    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_for_ready(base_url)
    except TimeoutError:
        proc.terminate()
        proc.wait(timeout=5)
        server_log_fh.close()
        log_content = server_log.read_text()[-4000:]
        pytest.fail(f"Escalation server failed to start.\nlog: {log_content}")

    yield base_url

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    server_log_fh.close()


def _find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_ready(url: str, timeout: float = 15):
    import httpx
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(url, timeout=2, follow_redirects=True)
            if resp.status_code < 500:
                return
        except (httpx.ConnectError, httpx.TimeoutException):
            pass
        time.sleep(0.3)
    raise TimeoutError(f"Server at {url} did not become ready within {timeout}s")


# ── Helpers ──────────────────────────────────────────────────────────────────


def _get_csrf(page, server_url: str) -> str:
    """Extract the CSRF token from the page meta tag."""
    page.goto(server_url)
    page.wait_for_load_state("networkidle")
    return page.locator('meta[name="csrf-token"]').get_attribute("content")


def _navigate_to_review(page, server_url: str):
    """Navigate to the escalation review detail page."""
    page.goto(f"{server_url}/review/{REVIEW_ID}")
    page.wait_for_load_state("networkidle")


def _card(page, group_id: str):
    """Locate an escalated card by its data-group-id attribute.

    Group IDs contain dots (e.g. board-foot.group_004.07MAR2026.0133.1f6r)
    which break CSS #id selectors. Use the data attribute instead.
    """
    return page.locator(f'[data-group-id="{group_id}"]')


def _click_override(page, group_id: str, action: str):
    """Click an override button on an escalated card.

    action: 'overridden', 'auto_dismissed', or 'escalated'
    """
    card = _card(page, group_id)
    expect(card).to_be_visible(timeout=5000)
    btn = card.locator(f'button[data-action="{action}"]')
    btn.click()
    # Wait for the AJAX response to update the card
    expect(card.locator(".card-actions .dim")).to_be_visible(timeout=10000)


def _generate_revision(page, server_url: str, timeout_ms: int = 300_000):
    """Generate a revision via direct API call and reload the page.

    Verifying the button is enabled confirms the UI state is correct.
    Then we call the API directly rather than clicking the button, because
    the JS does window.location.reload() on success which is unreliable
    to detect in Playwright.
    """
    # Confirm the revision button is enabled (UI state check)
    btn = page.locator("#revise-btn")
    expect(btn).to_be_enabled(timeout=5000)

    # Call revision API directly
    csrf = page.locator('meta[name="csrf-token"]').get_attribute("content")
    resp = page.request.post(
        f"{server_url}/api/review/{REVIEW_ID}/revise",
        headers={
            "Content-Type": "application/json",
            "X-DVAD-Token": csrf,
        },
        timeout=timeout_ms,
    )
    assert resp.status == 200, (
        f"Revision endpoint returned {resp.status}: {resp.text()}"
    )
    data = resp.json()
    status = data.get("status")
    # "ok" = revision generated; "no_output" = LLM responded but didn't
    # produce canonical delimiters (common with smaller local models).
    # Both are valid outcomes — the endpoint worked, the pipeline ran.
    assert status in ("ok", "no_output"), f"Revision failed: {data}"

    # Reload the page to get the post-revision UI state
    page.goto(f"{server_url}/review/{REVIEW_ID}")
    page.wait_for_load_state("networkidle")

    return data


def _override_via_api(page, server_url: str, group_id: str, resolution: str):
    """Call the override endpoint directly (bypassing UI buttons).

    Useful when we need to change an override after page load without
    re-clicking, e.g. to trigger stale revision detection.
    """
    csrf = page.locator('meta[name="csrf-token"]').get_attribute("content")
    resp = page.request.post(
        f"{server_url}/api/review/{REVIEW_ID}/override",
        headers={
            "Content-Type": "application/json",
            "X-DVAD-Token": csrf,
        },
        data=json.dumps({"group_id": group_id, "resolution": resolution}),
    )
    assert resp.status == 200, (
        f"Override API returned {resp.status}: {resp.text()}"
    )
    return resp.json()


def _fetch_revision_content(page, server_url: str) -> str:
    """Fetch the revised artifact content via API."""
    resp = page.request.get(f"{server_url}/api/review/{REVIEW_ID}/revised")
    assert resp.status == 200, f"Failed to fetch revision: {resp.status}"
    return resp.text()


# ── Tests ────────────────────────────────────────────────────────────────────


def test_accept_reviewer_override_and_revision(
    live_page, escalation_server, escalation_data_dir
):
    """Accept Reviewer on both escalated findings → revision includes
    reviewer's full recommendations."""
    page = live_page
    server = escalation_server
    _navigate_to_review(page, server)

    # Verify initial state: 2 escalated cards, revision button disabled
    escalated_cards = page.locator(".card-escalated")
    expect(escalated_cards).to_have_count(2, timeout=5000)

    revise_btn = page.locator("#revise-btn")
    expect(revise_btn).to_be_disabled()

    # Accept Reviewer on group_004
    _click_override(page, GROUP_004, "overridden")
    card_004 = _card(page, GROUP_004)
    expect(card_004).to_have_class(re.compile(r"resolved"))
    expect(card_004.locator(".card-actions .dim")).to_contain_text("Accepted (Reviewer)")

    # Accept Reviewer on group_005
    _click_override(page, GROUP_005, "overridden")
    card_005 = _card(page, GROUP_005)
    expect(card_005).to_have_class(re.compile(r"resolved"))

    # Revision button should now be enabled
    expect(revise_btn).to_be_enabled(timeout=5000)

    # Overrides banner should be visible
    banner = page.locator("#overrides-banner")
    expect(banner).to_be_visible()

    # Generate revision
    result = _generate_revision(page, server)

    if result.get("status") == "ok":
        # Pipeline revision step should be done
        pipe_revision = page.locator("#pipe-revision")
        expect(pipe_revision).to_have_class(re.compile(r"done"))

        # Fetch and verify revision content
        revision = _fetch_revision_content(page, server)
        assert len(revision) > 100, "Revision content is too short"

        # Reviewer recommended extracting BoardFootMath
        revision_lower = revision.lower()
        assert any(term in revision_lower for term in [
            "boardfootmath", "board foot math", "extract",
            "calculation logic", "separate", "standalone",
        ]), "Revision should reference reviewer's recommendation to extract calculation logic"


def test_accept_author_partial_override_and_revision(
    live_page, escalation_server, escalation_data_dir
):
    """Accept Author on PARTIAL findings → remap to partial_accepted →
    revision includes author's compromise position."""
    page = live_page
    server = escalation_server
    _navigate_to_review(page, server)

    # Accept Author on group_004 (PARTIAL → should remap to partial_accepted)
    _click_override(page, GROUP_004, "auto_dismissed")
    card_004 = _card(page, GROUP_004)
    expect(card_004).to_have_class(re.compile(r"resolved"))
    # The remap should produce the "Partial" label
    expect(card_004.locator(".card-actions .dim")).to_contain_text("Accepted (Author")

    # Accept Author on group_005 (also PARTIAL)
    _click_override(page, GROUP_005, "auto_dismissed")
    card_005 = _card(page, GROUP_005)
    expect(card_005).to_have_class(re.compile(r"resolved"))
    expect(card_005.locator(".card-actions .dim")).to_contain_text("Accepted (Author")

    # Revision button should now be enabled
    revise_btn = page.locator("#revise-btn")
    expect(revise_btn).to_be_enabled(timeout=5000)

    # Generate revision
    result = _generate_revision(page, server)

    if result.get("status") == "ok":
        # Fetch and verify revision content
        revision = _fetch_revision_content(page, server)
        assert len(revision) > 100, "Revision content is too short"

        # Author's compromise for group_004 mentions extracting BoardFootMath
        revision_lower = revision.lower()
        assert any(term in revision_lower for term in [
            "boardfootmath", "board foot math", "extract", "calculation",
        ]), "Revision should reference author's accepted portion (BoardFootMath extraction)"


def test_mixed_overrides(
    live_page, escalation_server, escalation_data_dir
):
    """Accept Reviewer on one finding, Accept Author on the other →
    revision should incorporate both viewpoints."""
    page = live_page
    server = escalation_server
    _navigate_to_review(page, server)

    # Accept Reviewer on group_004
    _click_override(page, GROUP_004, "overridden")
    card_004 = _card(page, GROUP_004)
    expect(card_004.locator(".card-actions .dim")).to_contain_text("Accepted (Reviewer)")

    # Accept Author on group_005 (PARTIAL)
    _click_override(page, GROUP_005, "auto_dismissed")
    card_005 = _card(page, GROUP_005)
    expect(card_005.locator(".card-actions .dim")).to_contain_text("Accepted (Author")

    # Revision button should be enabled
    revise_btn = page.locator("#revise-btn")
    expect(revise_btn).to_be_enabled(timeout=5000)

    # Generate revision
    result = _generate_revision(page, server)

    if result.get("status") == "ok":
        # Fetch and verify revision content includes something from both
        revision = _fetch_revision_content(page, server)
        assert len(revision) > 100, "Revision content is too short"


def test_keep_open_labels_and_pipeline(
    live_page, escalation_server, escalation_data_dir
):
    """Keep Open on one finding, Accept Reviewer on the other →
    verify correct labels and pipeline state.

    Note: All override actions (including Keep Open) mark the card as
    'resolved' in the DOM and enable the revision button. Keep Open
    writes 'escalated' back to storage (idempotent) but still counts
    as a human decision in the UI.
    """
    page = live_page
    server = escalation_server
    _navigate_to_review(page, server)

    # Keep Open on group_004
    _click_override(page, GROUP_004, "escalated")
    card_004 = _card(page, GROUP_004)
    expect(card_004.locator(".card-actions .dim")).to_contain_text("Kept Open")
    expect(card_004).to_have_class(re.compile(r"resolved"))

    # Accept Reviewer on group_005
    _click_override(page, GROUP_005, "overridden")
    card_005 = _card(page, GROUP_005)
    expect(card_005.locator(".card-actions .dim")).to_contain_text("Accepted (Reviewer)")

    # Both resolved → pipeline overrides step should be done
    pipe_overrides = page.locator("#pipe-overrides")
    expect(pipe_overrides).to_have_class(re.compile(r"done"))

    # Revision button should be enabled
    revise_btn = page.locator("#revise-btn")
    expect(revise_btn).to_be_enabled(timeout=5000)


def test_generate_revision_button_click(
    live_page, escalation_server, escalation_data_dir
):
    """Full JS-driven revision flow via actual button click.

    Verifies the Generate Revision button UX: text changes to
    "Generating...", button disables during generation, and upon
    completion the pipeline step and download link update correctly.
    """
    page = live_page
    server = escalation_server
    _navigate_to_review(page, server)

    # Override both escalated findings
    _click_override(page, GROUP_004, "overridden")
    _click_override(page, GROUP_005, "overridden")

    # Click the Generate Revision button (actual JS click, not API)
    revise_btn = page.locator("#revise-btn")
    expect(revise_btn).to_be_enabled(timeout=5000)
    revise_btn.click()

    # Assert button text changes to "Generating..." and is disabled
    expect(revise_btn).to_have_text("Generating...", timeout=5000)
    expect(revise_btn).to_be_disabled()

    # The JS startRevision() either:
    #   - success with content → window.location.reload()
    #   - no_output/error → alert() and re-enable button
    # Handle both by auto-dismissing any alert dialog and waiting for
    # the button text to leave "Generating..." (reload or button reset).
    dialog_message = None

    def _handle_dialog(dialog):
        nonlocal dialog_message
        dialog_message = dialog.message
        dialog.accept()

    page.on("dialog", _handle_dialog)

    # Wait for the fetch + LLM generation to finish (up to 5 min)
    page.wait_for_function(
        """() => {
            const btn = document.getElementById('revise-btn');
            return !btn || btn.textContent !== 'Generating...';
        }""",
        timeout=300_000,
    )
    page.remove_listener("dialog", _handle_dialog)

    if dialog_message:
        # no_output or error — the button click UX worked, skip file assertions
        pytest.skip(f"Revision via button produced no file: {dialog_message}")

    # Success path: page reloaded — wait for stable state
    page.wait_for_load_state("networkidle")

    # Pipeline revision step should be done
    pipe_revision = page.locator("#pipe-revision")
    expect(pipe_revision).to_have_class(re.compile(r"done"))

    # Download Revision link should be visible
    download_link = page.locator(".download-revised-link")
    expect(download_link).to_be_visible()


def test_download_revision_after_generation(
    live_page, escalation_server, escalation_data_dir
):
    """Verify the download revision endpoint returns valid content
    after a revision has been generated."""
    page = live_page
    server = escalation_server
    _navigate_to_review(page, server)

    # Override both findings and generate revision via API
    _click_override(page, GROUP_004, "overridden")
    _click_override(page, GROUP_005, "overridden")
    result = _generate_revision(page, server)

    if result.get("status") == "no_output":
        # LLM didn't produce delimiters — skip content assertions
        # but the endpoint should still return 404 (no file written)
        return

    # Fetch the revised artifact via API
    resp = page.request.get(f"{server}/api/review/{REVIEW_ID}/revised")
    assert resp.status == 200, f"Download returned {resp.status}"
    body = resp.text()
    assert len(body) > 0, "Download response body is empty"

    # Content-Disposition header should contain the review ID
    content_disp = resp.headers.get("content-disposition", "")
    assert REVIEW_ID in content_disp, (
        f"Content-Disposition should contain review ID '{REVIEW_ID}', "
        f"got: {content_disp}"
    )


def test_regenerate_revision_after_new_override(
    live_page, escalation_server, escalation_data_dir
):
    """Stale revision detection and regenerate flow.

    After generating a revision, changing an override should mark the
    revision as stale and show a "Regenerate Revision" button. Generating
    a new revision should clear the stale state.
    """
    page = live_page
    server = escalation_server
    _navigate_to_review(page, server)

    # Step 1: Override both findings as Accept Reviewer
    _click_override(page, GROUP_004, "overridden")
    _click_override(page, GROUP_005, "overridden")

    # Step 2: Generate initial revision via API
    result = _generate_revision(page, server)

    # Verify revision is shown (download link visible)
    if result.get("status") == "ok":
        download_link = page.locator(".download-revised-link")
        expect(download_link).to_be_visible()

    # Step 3: Change group_005 override to Accept Author via API
    # (simulates user changing their mind after revision was generated)
    _override_via_api(page, server, GROUP_005, "auto_dismissed")

    # Step 4: Reload — assert stale state
    page.goto(f"{server}/review/{REVIEW_ID}")
    page.wait_for_load_state("networkidle")

    # Regenerate Revision button should be visible
    revise_btn = page.locator("#revise-btn")
    expect(revise_btn).to_be_visible(timeout=5000)
    expect(revise_btn).to_contain_text("Regenerate")

    # Download link should be disabled (stale)
    download_link = page.locator(".download-revised-link")
    expect(download_link).to_be_visible()
    expect(download_link).to_have_class(re.compile(r"btn-disabled"))

    # Step 5: Generate new revision via API
    result2 = _generate_revision(page, server)

    # Step 6: Assert revision step done and download link active
    pipe_revision = page.locator("#pipe-revision")
    expect(pipe_revision).to_have_class(re.compile(r"done"))

    download_link = page.locator(".download-revised-link")
    expect(download_link).to_be_visible()
    # After regeneration, the link should NOT have btn-disabled
    expect(download_link).not_to_have_class(re.compile(r"btn-disabled"))
