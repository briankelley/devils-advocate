"""Combinatorial E2E test matrix — every mode x option permutation.

Phase 1: Form → interstitial command validation (no LLM calls)
Phase 2: CLI command execution spot-check (runs scraped commands)
Phase 3: Live GUI review flow (full matrix against remote LLM)
Phase 4: Input variation tests (multi-file, spec, project-dir)
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from playwright.sync_api import expect

FIXTURES = Path(__file__).parent / "fixtures"
FAILURES_DIR = Path(__file__).parent / "failures"

pytestmark = [pytest.mark.e2e]

# ─── Test dimensions ─────────────────────────────────────────────────────────

MODES = ["plan", "spec", "code", "integration"]
DRY_RUN_VALUES = [False, True]
MAX_COST_VALUES = [None, "0.001", "5.00"]


# ─── Fixture file creators ───────────────────────────────────────────────────


def _create_input_file(tmp_path: Path, mode: str) -> Path:
    """Create a minimal input file appropriate for the given mode."""
    if mode == "code":
        src = FIXTURES / "test-code.py"
        dest = tmp_path / "test-code.py"
    elif mode == "spec":
        src = FIXTURES / "test-spec.md"
        dest = tmp_path / "test-spec.md"
    else:
        src = FIXTURES / "test-plan.md"
        dest = tmp_path / "test-plan.md"
    shutil.copy2(src, dest)
    return dest


def _create_project_dir(tmp_path: Path) -> Path:
    """Create a project directory with manifest for integration mode."""
    proj = tmp_path / "test-project"
    shutil.copytree(FIXTURES / "test-project", proj)
    return proj


# ─── Helper: fill form and submit to interstitial ────────────────────────────


def fill_form_and_submit(
    page,
    dvad_server: str,
    *,
    mode: str,
    project: str,
    input_files: list[Path],
    spec_file: Path | None = None,
    reference_files: list[Path] | None = None,
    project_dir: Path | None = None,
    max_cost: str | None = None,
    dry_run: bool = False,
):
    """Navigate to dashboard, fill the review form, submit to show interstitial.

    Injects file paths directly into hidden inputs and dvad._selectedPaths
    to avoid driving the file picker modal.
    """
    page.goto(dvad_server)
    page.wait_for_load_state("networkidle")

    # Select mode radio
    page.locator(f'input[type="radio"][name="mode"][value="{mode}"]').click()
    # Wait for mode UI update
    page.wait_for_timeout(200)

    # Fill project name
    page.fill("#project", project)

    # Inject input file paths
    input_path_data = [{"path": str(f), "name": f.name} for f in input_files]
    page.evaluate(
        f"""() => {{
            const paths = {json.dumps(input_path_data)};
            document.getElementById('input_files_paths').value = JSON.stringify(paths.map(p => p.path));
            document.getElementById('input_files_display').innerHTML =
                paths.map(p => '<div class="selected-file">' + p.name + '</div>').join('');
            if (typeof dvad !== 'undefined') {{
                dvad._selectedPaths = dvad._selectedPaths || {{}};
                dvad._selectedPaths.input_files = paths;
            }}
        }}"""
    )

    # Inject spec file if provided
    if spec_file:
        spec_data = [{"path": str(spec_file), "name": spec_file.name}]
        page.evaluate(
            f"""() => {{
                const paths = {json.dumps(spec_data)};
                document.getElementById('spec_file_paths').value = paths[0].path;
                const display = document.getElementById('spec_file_display');
                if (display) display.innerHTML = '<div class="selected-file">' + paths[0].name + '</div>';
                if (typeof dvad !== 'undefined') {{
                    dvad._selectedPaths = dvad._selectedPaths || {{}};
                    dvad._selectedPaths.spec_file = paths;
                }}
            }}"""
        )

    # Inject reference files if provided
    if reference_files:
        ref_data = [{"path": str(f), "name": f.name} for f in reference_files]
        page.evaluate(
            f"""() => {{
                const paths = {json.dumps(ref_data)};
                document.getElementById('reference_files_paths').value = JSON.stringify(paths.map(p => p.path));
                const display = document.getElementById('reference_files_display');
                if (display) display.innerHTML =
                    paths.map(p => '<div class="selected-file">' + p.name + '</div>').join('');
                if (typeof dvad !== 'undefined') {{
                    dvad._selectedPaths = dvad._selectedPaths || {{}};
                    dvad._selectedPaths.reference_files = paths;
                }}
            }}"""
        )

    # Fill project dir if provided
    if project_dir:
        page.fill("#project_dir", str(project_dir))

    # Fill max cost if provided
    if max_cost is not None:
        page.fill("#max_cost", max_cost)

    # Toggle dry run
    if dry_run:
        page.check("#dry_run")

    # Submit the form via JS dispatch to bypass HTML5 step validation
    # (max_cost values like 0.001 fail native validation with step=0.01)
    page.evaluate(
        "document.getElementById('review-form').dispatchEvent("
        "new Event('submit', {cancelable: true, bubbles: true}))"
    )

    # Wait for interstitial to appear
    page.wait_for_selector("#interstitial", state="visible", timeout=5000)


def get_interstitial_command(page) -> str:
    """Return the CLI command text from the interstitial."""
    return page.locator("#command-preview-text").inner_text()


def validate_cli_command(
    cmd_text: str,
    *,
    mode: str,
    project: str,
    input_files: list[Path],
    spec_file: Path | None = None,
    reference_files: list[Path] | None = None,
    max_cost: str | None = None,
    dry_run: bool = False,
    project_dir: Path | None = None,
):
    """Parse a CLI command string and verify it contains the expected flags."""
    parts = shlex.split(cmd_text)

    # Find dvad binary and 'review' subcommand
    assert "review" in parts, f"'review' not found in command: {cmd_text}"

    # Check --mode
    mode_idx = parts.index("--mode")
    assert parts[mode_idx + 1] == mode, f"Expected mode '{mode}', got '{parts[mode_idx + 1]}'"

    # Check --project
    proj_idx = parts.index("--project")
    assert parts[proj_idx + 1] == project

    # Check --input for each file
    input_indices = [i for i, p in enumerate(parts) if p == "--input"]
    input_paths_in_cmd = [parts[i + 1] for i in input_indices]
    expected_input_paths = [str(f) for f in input_files]
    if reference_files:
        expected_input_paths += [str(f) for f in reference_files]
    assert sorted(input_paths_in_cmd) == sorted(expected_input_paths), (
        f"Input paths mismatch.\nExpected: {expected_input_paths}\nGot: {input_paths_in_cmd}"
    )

    # Check --dry-run
    if dry_run:
        assert "--dry-run" in parts, f"--dry-run expected but not found in: {cmd_text}"
    else:
        assert "--dry-run" not in parts, f"--dry-run found but not expected in: {cmd_text}"

    # Check --max-cost
    if max_cost is not None:
        mc_idx = parts.index("--max-cost")
        assert parts[mc_idx + 1] == max_cost
    else:
        assert "--max-cost" not in parts

    # Check --spec
    if spec_file:
        spec_idx = parts.index("--spec")
        assert parts[spec_idx + 1] == str(spec_file)
    else:
        assert "--spec" not in parts

    # Check --project-dir
    if project_dir:
        pd_idx = parts.index("--project-dir")
        assert parts[pd_idx + 1] == str(project_dir)
    else:
        assert "--project-dir" not in parts


# ─── Helper: start review via GUI and wait for completion ────────────────────


def click_run_and_wait(page, dvad_server: str, *, timeout: int = 600_000) -> str:
    """Click 'Run Review' on interstitial, wait for completion. Return review_id."""
    # Click the run button
    page.click("#run-review-btn")

    # Wait for navigation to progress page
    page.wait_for_url("**/review/*", timeout=30_000)

    # Extract review_id from URL
    url = page.url
    review_id = url.rstrip("/").split("/")[-1]

    # Poll API for completion instead of relying on SSE-driven DOM transitions
    wait_for_review_complete(page, dvad_server, review_id, timeout=timeout)

    return review_id


def wait_for_review_complete(
    page, dvad_server: str, review_id: str, *, timeout: int = 600_000
) -> str:
    """Poll /api/review/{id} until the review ledger exists (completion or failure).

    Returns the result string (e.g. 'success', 'dry_run', 'cost_exceeded', 'failed').
    More reliable than waiting for DOM transitions which depend on SSE and page reloads.
    """
    import time as _time

    deadline = _time.monotonic() + timeout / 1000
    while _time.monotonic() < deadline:
        resp = page.request.get(f"{dvad_server}/api/review/{review_id}")
        if resp.status == 200:
            data = resp.json()
            result = data.get("result", "")
            if result and result != "running":
                # Review is done — navigate to the detail page
                page.goto(f"{dvad_server}/review/{review_id}")
                page.wait_for_load_state("domcontentloaded")
                return result
        page.wait_for_timeout(5000)

    pytest.fail(f"Review {review_id} did not complete within {timeout / 1000:.0f}s")


def assert_review_result(page, *, expected_result: str, mode: str, actual_result: str | None = None):
    """Assert the review completed with the expected result.

    Uses the API result string (from wait_for_review_complete) for the primary check,
    then verifies page content is consistent with that result.
    """
    body = page.locator("body").inner_text()

    if actual_result is not None:
        assert actual_result == expected_result, (
            f"Expected result '{expected_result}' but API returned '{actual_result}'\n"
            f"Page text: {body[:500]}"
        )

    if expected_result == "dry_run":
        # Dry run pages show cost estimate table
        assert "Cost Estimate" in body or "Estimated" in body or "dry" in body.lower(), (
            f"Dry run page content unexpected: {body[:500]}"
        )
    elif expected_result == "cost_exceeded":
        # Cost exceeded — review detail page renders but no report download
        assert page.locator('a:has-text("Download Report")').count() == 0, (
            "Cost exceeded review should not offer report download"
        )
    elif expected_result == "success":
        # Successful reviews show cost rows and download links
        cost_rows = page.locator(".cost-row")
        assert cost_rows.count() >= 1, "No cost rows found on success page"


# ─── Helper: start review via API (bypass form for speed) ────────────────────


def start_review_api(
    page,
    dvad_server: str,
    *,
    mode: str,
    project: str,
    input_files: list[Path],
    spec_file: Path | None = None,
    project_dir: Path | None = None,
    max_cost: str | None = None,
    dry_run: bool = False,
) -> str:
    """Start a review via POST to /api/review/start. Returns review_id.

    Retries on 409 (review already running).
    """
    page.goto(dvad_server)
    page.wait_for_load_state("networkidle")
    csrf = page.locator('meta[name="csrf-token"]').get_attribute("content")

    multipart: dict = {
        "mode": mode,
        "project": project,
        "input_paths": json.dumps([str(f) for f in input_files]),
    }
    if spec_file:
        multipart["spec_path"] = str(spec_file)
    if project_dir:
        multipart["project_dir"] = str(project_dir)
    if max_cost is not None:
        multipart["max_cost"] = max_cost
    if dry_run:
        multipart["dry_run"] = "on"

    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        resp = page.request.post(
            f"{dvad_server}/api/review/start",
            multipart=multipart,
            headers={"X-DVAD-Token": csrf},
        )
        if resp.status == 200:
            return resp.json()["review_id"]
        if resp.status == 409:
            time.sleep(5)
            continue
        pytest.fail(f"Unexpected status {resp.status}: {resp.text()}")
    pytest.fail("Timed out waiting for prior review to finish")


# ─── Failure capture ─────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def capture_on_failure(request, live_page):
    """Capture screenshot and page HTML on test failure."""
    yield
    if hasattr(request.node, "rep_call") and request.node.rep_call.failed:
        fail_dir = FAILURES_DIR / request.node.name
        fail_dir.mkdir(parents=True, exist_ok=True)
        try:
            live_page.screenshot(path=str(fail_dir / "screenshot.png"))
            (fail_dir / "page.html").write_text(live_page.content())
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 1: Form → Interstitial Command Validation (no LLM)
# ═══════════════════════════════════════════════════════════════════════════════


class TestFormToInterstitial:
    """Verify that every mode x option combination produces a correct CLI command."""

    @pytest.mark.parametrize("mode", MODES)
    @pytest.mark.parametrize("dry_run", DRY_RUN_VALUES, ids=["no-dry", "dry"])
    @pytest.mark.parametrize("max_cost", MAX_COST_VALUES, ids=["no-cost", "tiny-cost", "normal-cost"])
    def test_command_matches_form(self, live_page, dvad_server, tmp_path, mode, dry_run, max_cost):
        """Fill form, submit, verify interstitial command has correct flags."""
        page = live_page
        input_file = _create_input_file(tmp_path, mode)
        input_files = [input_file]

        # Integration mode: add project dir
        project_dir = None
        if mode == "integration":
            project_dir = _create_project_dir(tmp_path)

        project_name = f"e2e-{mode}-matrix"

        fill_form_and_submit(
            page, dvad_server,
            mode=mode,
            project=project_name,
            input_files=input_files,
            project_dir=project_dir,
            max_cost=max_cost,
            dry_run=dry_run,
        )

        cmd = get_interstitial_command(page)
        assert cmd, "Interstitial command is empty"

        validate_cli_command(
            cmd,
            mode=mode,
            project=project_name,
            input_files=input_files,
            max_cost=max_cost,
            dry_run=dry_run,
            project_dir=project_dir,
        )

    def test_plan_with_spec_command(self, live_page, dvad_server, tmp_path):
        """Plan mode with spec file produces --spec flag."""
        page = live_page
        plan_file = _create_input_file(tmp_path, "plan")
        spec_file = tmp_path / "spec.md"
        shutil.copy2(FIXTURES / "test-spec.md", spec_file)

        fill_form_and_submit(
            page, dvad_server,
            mode="plan",
            project="e2e-plan-spec",
            input_files=[plan_file],
            spec_file=spec_file,
        )

        cmd = get_interstitial_command(page)
        validate_cli_command(
            cmd, mode="plan", project="e2e-plan-spec",
            input_files=[plan_file], spec_file=spec_file,
        )

    def test_plan_with_reference_files_command(self, live_page, dvad_server, tmp_path):
        """Plan mode with reference files includes them as --input flags."""
        page = live_page
        plan_file = _create_input_file(tmp_path, "plan")
        ref_file = tmp_path / "reference.md"
        shutil.copy2(FIXTURES / "test-reference.md", ref_file)

        fill_form_and_submit(
            page, dvad_server,
            mode="plan",
            project="e2e-plan-ref",
            input_files=[plan_file],
            reference_files=[ref_file],
        )

        cmd = get_interstitial_command(page)
        validate_cli_command(
            cmd, mode="plan", project="e2e-plan-ref",
            input_files=[plan_file], reference_files=[ref_file],
        )

    def test_code_with_spec_command(self, live_page, dvad_server, tmp_path):
        """Code mode with spec file produces --spec flag."""
        page = live_page
        code_file = _create_input_file(tmp_path, "code")
        spec_file = tmp_path / "spec.md"
        shutil.copy2(FIXTURES / "test-spec.md", spec_file)

        fill_form_and_submit(
            page, dvad_server,
            mode="code",
            project="e2e-code-spec",
            input_files=[code_file],
            spec_file=spec_file,
        )

        cmd = get_interstitial_command(page)
        validate_cli_command(
            cmd, mode="code", project="e2e-code-spec",
            input_files=[code_file], spec_file=spec_file,
        )

    def test_integration_with_project_dir_command(self, live_page, dvad_server, tmp_path):
        """Integration mode with project-dir produces --project-dir flag."""
        page = live_page
        input_file = _create_input_file(tmp_path, "integration")
        project_dir = _create_project_dir(tmp_path)

        fill_form_and_submit(
            page, dvad_server,
            mode="integration",
            project="e2e-integ-dir",
            input_files=[input_file],
            project_dir=project_dir,
        )

        cmd = get_interstitial_command(page)
        validate_cli_command(
            cmd, mode="integration", project="e2e-integ-dir",
            input_files=[input_file], project_dir=project_dir,
        )


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 2: CLI Command Execution Spot-Check
# ═══════════════════════════════════════════════════════════════════════════════


class TestCLICommandExecution:
    """Scrape CLI commands from interstitial and actually execute them."""

    pytestmark = [pytest.mark.e2e, pytest.mark.e2e_live]

    @pytest.fixture(autouse=True)
    def _require_remote(self, local_llm):
        """Ensure LLM is available."""

    def test_plan_command_executes(self, live_page, dvad_server, tmp_path, e2e_config_path):
        """Scrape a plan review CLI command and run it via subprocess."""
        page = live_page
        input_file = _create_input_file(tmp_path, "plan")

        fill_form_and_submit(
            page, dvad_server,
            mode="plan", project="e2e-cli-plan",
            input_files=[input_file],
        )

        cmd = get_interstitial_command(page)
        parts = shlex.split(cmd)

        # Add --config pointing to e2e config
        if e2e_config_path:
            parts.extend(["--config", str(e2e_config_path)])

        result = subprocess.run(
            parts, capture_output=True, text=True, timeout=300,
            env={**__import__("os").environ, "E2E_LOCAL_KEY": "e2e-dummy-key"},
        )

        assert result.returncode == 0, (
            f"CLI command failed (exit {result.returncode}):\n"
            f"stdout: {result.stdout[-2000:]}\n"
            f"stderr: {result.stderr[-2000:]}"
        )

    def test_dry_run_command_executes(self, live_page, dvad_server, tmp_path, e2e_config_path):
        """Scrape a dry-run CLI command and verify it exits cleanly."""
        page = live_page
        input_file = _create_input_file(tmp_path, "plan")

        fill_form_and_submit(
            page, dvad_server,
            mode="plan", project="e2e-cli-dry",
            input_files=[input_file], dry_run=True,
        )

        cmd = get_interstitial_command(page)
        parts = shlex.split(cmd)

        if e2e_config_path:
            parts.extend(["--config", str(e2e_config_path)])

        result = subprocess.run(
            parts, capture_output=True, text=True, timeout=60,
            env={**__import__("os").environ, "E2E_LOCAL_KEY": "e2e-dummy-key"},
        )

        assert result.returncode == 0, (
            f"Dry-run CLI command failed (exit {result.returncode}):\n"
            f"stderr: {result.stderr[-2000:]}"
        )

    def test_spec_command_executes(self, live_page, dvad_server, tmp_path, e2e_config_path):
        """Scrape a spec review CLI command and run it."""
        page = live_page
        input_file = _create_input_file(tmp_path, "spec")

        fill_form_and_submit(
            page, dvad_server,
            mode="spec", project="e2e-cli-spec",
            input_files=[input_file],
        )

        cmd = get_interstitial_command(page)
        parts = shlex.split(cmd)

        if e2e_config_path:
            parts.extend(["--config", str(e2e_config_path)])

        result = subprocess.run(
            parts, capture_output=True, text=True, timeout=300,
            env={**__import__("os").environ, "E2E_LOCAL_KEY": "e2e-dummy-key"},
        )

        assert result.returncode == 0, (
            f"CLI command failed (exit {result.returncode}):\n"
            f"stderr: {result.stderr[-2000:]}"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 3: Live GUI Reviews — Full Matrix
# ═══════════════════════════════════════════════════════════════════════════════


class TestLiveReviewMatrix:
    """Full GUI review flow for every mode x option permutation."""

    pytestmark = [pytest.mark.e2e, pytest.mark.e2e_live]

    @pytest.fixture(autouse=True)
    def _require_remote(self, local_llm):
        """Ensure LLM is available."""

    @staticmethod
    def _expected_result(dry_run: bool, max_cost: str | None) -> str:
        """Determine expected review result based on options."""
        if dry_run:
            return "dry_run"
        if max_cost == "0.001":
            return "cost_exceeded"
        return "success"

    @pytest.mark.parametrize("mode", MODES)
    @pytest.mark.parametrize("dry_run", DRY_RUN_VALUES, ids=["run", "dry"])
    @pytest.mark.parametrize("max_cost", [None, "0.001"], ids=["no-limit", "tiny-limit"])
    def test_review_no_thinking(self, live_page, dvad_server, tmp_path, mode, dry_run, max_cost):
        """Review with thinking=off for each mode x dry_run x max_cost."""
        page = live_page
        input_file = _create_input_file(tmp_path, mode)
        project_dir = _create_project_dir(tmp_path) if mode == "integration" else tmp_path
        project = f"e2e-{mode}-d{int(dry_run)}-c{max_cost or 'none'}"

        review_id = start_review_api(
            page, dvad_server,
            mode=mode, project=project,
            input_files=[input_file],
            project_dir=project_dir,
            max_cost=max_cost, dry_run=dry_run,
        )

        # Wait for review completion via API polling (more reliable than DOM waits)
        actual = wait_for_review_complete(page, dvad_server, review_id)

        expected = self._expected_result(dry_run, max_cost)
        assert_review_result(page, expected_result=expected, mode=mode, actual_result=actual)

    @pytest.mark.parametrize("mode", MODES)
    @pytest.mark.parametrize("dry_run", DRY_RUN_VALUES, ids=["run", "dry"])
    @pytest.mark.parametrize("max_cost", [None, "0.001"], ids=["no-limit", "tiny-limit"])
    def test_review_with_thinking(
        self, live_page, dvad_server, tmp_path, mode, dry_run, max_cost, enable_thinking,
    ):
        """Review with thinking=on for each mode x dry_run x max_cost."""
        page = live_page
        input_file = _create_input_file(tmp_path, mode)
        project_dir = _create_project_dir(tmp_path) if mode == "integration" else tmp_path
        project = f"e2e-think-{mode}-d{int(dry_run)}-c{max_cost or 'none'}"

        review_id = start_review_api(
            page, dvad_server,
            mode=mode, project=project,
            input_files=[input_file],
            project_dir=project_dir,
            max_cost=max_cost, dry_run=dry_run,
        )

        actual = wait_for_review_complete(page, dvad_server, review_id)

        expected = self._expected_result(dry_run, max_cost)
        assert_review_result(page, expected_result=expected, mode=mode, actual_result=actual)

    def test_dry_run_with_max_cost(self, live_page, dvad_server, tmp_path):
        """Explicit test: dry_run=True + max_cost=0.001 — unknown interaction."""
        page = live_page
        input_file = _create_input_file(tmp_path, "plan")

        review_id = start_review_api(
            page, dvad_server,
            mode="plan", project="e2e-dryrun-maxcost",
            input_files=[input_file],
            max_cost="0.001", dry_run=True,
        )

        actual = wait_for_review_complete(page, dvad_server, review_id, timeout=120_000)

        # We don't know which takes precedence — just verify it doesn't crash
        body = page.locator("body").inner_text()
        assert "e2e-dryrun-maxcost" in body, "Review page should show project name"
        # It should be one of these outcomes
        assert actual in ("dry_run", "cost_exceeded", "cost_aborted", "success"), (
            f"Unexpected result: {actual}"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 4: Input Variations
# ═══════════════════════════════════════════════════════════════════════════════


class TestInputVariations:
    """Test different input combinations per mode."""

    pytestmark = [pytest.mark.e2e, pytest.mark.e2e_live]

    @pytest.fixture(autouse=True)
    def _require_remote(self, local_llm):
        """Ensure LLM is available."""

    def test_plan_multi_file(self, live_page, dvad_server, tmp_path):
        """Plan review with multiple input files."""
        page = live_page
        plan1 = tmp_path / "plan1.md"
        plan2 = tmp_path / "plan2.md"
        shutil.copy2(FIXTURES / "test-plan.md", plan1)
        shutil.copy2(FIXTURES / "test-plan-2.md", plan2)

        review_id = start_review_api(
            page, dvad_server,
            mode="plan", project="e2e-plan-multi",
            input_files=[plan1, plan2],
            project_dir=tmp_path,
        )

        actual = wait_for_review_complete(page, dvad_server, review_id)
        assert_review_result(page, expected_result="success", mode="plan", actual_result=actual)

    def test_plan_with_spec_file(self, live_page, dvad_server, tmp_path):
        """Plan review with an additional spec file."""
        page = live_page
        plan = _create_input_file(tmp_path, "plan")
        spec = tmp_path / "spec.md"
        shutil.copy2(FIXTURES / "test-spec.md", spec)

        review_id = start_review_api(
            page, dvad_server,
            mode="plan", project="e2e-plan-spec",
            input_files=[plan], spec_file=spec,
            project_dir=tmp_path,
        )

        actual = wait_for_review_complete(page, dvad_server, review_id)
        assert_review_result(page, expected_result="success", mode="plan", actual_result=actual)

    def test_plan_with_reference_files(self, live_page, dvad_server, tmp_path):
        """Plan review with reference files for cross-checking."""
        page = live_page
        plan = _create_input_file(tmp_path, "plan")
        ref = tmp_path / "reference.md"
        shutil.copy2(FIXTURES / "test-reference.md", ref)

        # Reference files are sent as additional input_paths
        review_id = start_review_api(
            page, dvad_server,
            mode="plan", project="e2e-plan-refs",
            input_files=[plan, ref],
            project_dir=tmp_path,
        )

        actual = wait_for_review_complete(page, dvad_server, review_id)
        assert_review_result(page, expected_result="success", mode="plan", actual_result=actual)

    def test_code_with_spec(self, live_page, dvad_server, tmp_path):
        """Code review with spec file."""
        page = live_page
        code = _create_input_file(tmp_path, "code")
        spec = tmp_path / "spec.md"
        shutil.copy2(FIXTURES / "test-spec.md", spec)

        review_id = start_review_api(
            page, dvad_server,
            mode="code", project="e2e-code-spec",
            input_files=[code], spec_file=spec,
            project_dir=tmp_path,
        )

        actual = wait_for_review_complete(page, dvad_server, review_id)
        assert_review_result(page, expected_result="success", mode="code", actual_result=actual)

    def test_integration_with_project_dir(self, live_page, dvad_server, tmp_path):
        """Integration review using project-dir manifest discovery."""
        page = live_page
        project_dir = _create_project_dir(tmp_path)

        review_id = start_review_api(
            page, dvad_server,
            mode="integration", project="e2e-integ-dir",
            input_files=[],
            project_dir=project_dir,
        )

        wait_for_review_complete(page, dvad_server, review_id)

        body = page.locator("body").inner_text()
        assert "e2e-integ-dir" in body

    def test_integration_with_files_and_project_dir(self, live_page, dvad_server, tmp_path):
        """Integration review with both explicit files and project-dir."""
        page = live_page
        input_file = _create_input_file(tmp_path, "integration")
        project_dir = _create_project_dir(tmp_path)

        review_id = start_review_api(
            page, dvad_server,
            mode="integration", project="e2e-integ-both",
            input_files=[input_file],
            project_dir=project_dir,
        )

        wait_for_review_complete(page, dvad_server, review_id)

        body = page.locator("body").inner_text()
        assert "e2e-integ-both" in body

    def test_integration_no_inputs_no_dir(self, live_page, dvad_server, tmp_path):
        """Integration review with no files and no project-dir — should handle gracefully."""
        page = live_page

        # This might return 400 or might run with empty content — find out
        page.goto(dvad_server)
        page.wait_for_load_state("networkidle")
        csrf = page.locator('meta[name="csrf-token"]').get_attribute("content")

        resp = page.request.post(
            f"{dvad_server}/api/review/start",
            multipart={
                "mode": "integration",
                "project": "e2e-integ-empty",
                "input_paths": json.dumps([]),
            },
            headers={"X-DVAD-Token": csrf},
        )

        # Retry on 409 (prior review still running)
        if resp.status == 409:
            page.wait_for_timeout(10_000)
            resp = page.request.post(
                f"{dvad_server}/api/review/start",
                multipart={
                    "mode": "integration",
                    "project": "e2e-integ-empty",
                    "input_paths": json.dumps([]),
                },
                headers={"X-DVAD-Token": csrf},
            )

        # Record what happens — we expect either 400 (validation) or 200 (proceeds)
        if resp.status == 400:
            # Valid — server rejected empty integration input
            assert "detail" in resp.json()
        else:
            # Also valid — server accepted and will try to run
            assert resp.status == 200
            review_id = resp.json()["review_id"]
            wait_for_review_complete(page, dvad_server, review_id, timeout=120_000)
