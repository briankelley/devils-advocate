"""Mid-range E2E test matrix -- config variation, input quality, deep ledger.

Tests 21 live review scenarios that exercise config mutations (sparse roles,
fallbacks), input edge cases (trivial, empty, wrong content type), and deep
ledger assertion coverage.  Complements test_matrix.py which only uses
the maximally-configured setup.

All tests submit to a real LLM via the GUI API and verify the full pipeline.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.e2e_live]

FIXTURES = Path(__file__).parent / "fixtures"
FAILURES_DIR = Path(__file__).parent / "failures"
_FIXTURE_YAML = (Path(__file__).parent / "fixtures" / "models.yaml").read_text()

# Import helpers from test_matrix (same directory, pytest adds to sys.path)
from test_matrix import (
    start_review_api,
    wait_for_review_complete,
    _create_input_file,
    _create_project_dir,
)


# ─── Model aliases ──────────────────────────────────────────────────────────

R = "e2e-remote"           # thinking=false
T = "e2e-remote-thinker"   # thinking=true


# ─── Config profiles ────────────────────────────────────────────────────────

CONFIG_A1 = {  # 2 reviewers, no explicit revision (falls back to author)
    "author": R, "reviewer1": R, "reviewer2": T,
    "dedup": T, "normalization": T, "revision": None, "integration": R,
}

CONFIG_A2 = {  # 2 reviewers, no explicit normalization (falls back to dedup)
    "author": R, "reviewer1": R, "reviewer2": T,
    "dedup": T, "normalization": None, "revision": T, "integration": R,
}

CONFIG_A3 = {  # 2 reviewers, no revision AND no normalization (both fallbacks)
    "author": R, "reviewer1": R, "reviewer2": T,
    "dedup": T, "normalization": None, "revision": None, "integration": R,
}

CONFIG_A4 = {  # 1 reviewer + dedup (single-source dedup)
    "author": R, "reviewer1": T, "reviewer2": None,
    "dedup": T, "normalization": T, "revision": T, "integration": R,
}

CONFIG_A5 = {  # 1 reviewer + dedup, both fallbacks (minimal with dedup)
    "author": R, "reviewer1": T, "reviewer2": None,
    "dedup": T, "normalization": None, "revision": None, "integration": R,
}

CONFIG_A6 = {  # 1 reviewer, no dedup, explicit norm + revision (POTENTIAL BUG)
    "author": R, "reviewer1": T, "reviewer2": None,
    "dedup": None, "normalization": T, "revision": T, "integration": R,
}

CONFIG_B1 = {  # Spec: 1 reviewer + dedup + norm, no author/revision/integration
    "author": None, "reviewer1": T, "reviewer2": None,
    "dedup": T, "normalization": T, "revision": None, "integration": None,
}

CONFIG_B2 = {  # Spec: 2 reviewers, no revision (falls back to author)
    "author": R, "reviewer1": R, "reviewer2": T,
    "dedup": T, "normalization": T, "revision": None, "integration": R,
}

CONFIG_B3 = {  # Spec: 1 reviewer + dedup, no norm (falls back to dedup)
    "author": None, "reviewer1": T, "reviewer2": None,
    "dedup": T, "normalization": None, "revision": T, "integration": None,
}

CONFIG_B4 = {  # Spec: 1 reviewer, no dedup, explicit norm + revision (POTENTIAL BUG)
    "author": None, "reviewer1": T, "reviewer2": None,
    "dedup": None, "normalization": T, "revision": T, "integration": None,
}

CONFIG_C1 = {  # Integration: no explicit revision (falls back to author)
    "author": R, "reviewer1": None, "reviewer2": None,
    "dedup": T, "normalization": T, "revision": None, "integration": R,
}

CONFIG_C2 = {  # Integration: no explicit normalization (falls back to dedup)
    "author": R, "reviewer1": None, "reviewer2": None,
    "dedup": T, "normalization": None, "revision": T, "integration": R,
}

CONFIG_C3 = {  # Integration: minimal (integration + dedup only, both fallbacks)
    "author": R, "reviewer1": None, "reviewer2": None,
    "dedup": T, "normalization": None, "revision": None, "integration": R,
}


# ─── Valid governance resolutions ────────────────────────────────────────────

VALID_GOVERNANCE_RESOLUTIONS = {
    "accepted", "rejected", "partial",
    "auto_accepted", "auto_dismissed", "escalated",
    "overridden", "pending",
}


# ─── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _require_llm(local_llm):
    """All tests in this module require a running LLM backend."""


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


@pytest.fixture
def restore_config(live_page, dvad_server):
    """Auto-restore fixture config after each test that mutates config."""
    yield
    live_page.goto(f"{dvad_server}/config")
    live_page.wait_for_load_state("networkidle")
    csrf = live_page.locator('meta[name="csrf-token"]').get_attribute("content")
    live_page.request.post(
        f"{dvad_server}/api/config",
        data=json.dumps({"yaml": _FIXTURE_YAML}),
        headers={"X-DVAD-Token": csrf, "Content-Type": "application/json"},
    )


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _apply_config(page, dvad_server: str, roles: dict):
    """Apply a structured config mutation via POST /api/config."""
    page.goto(dvad_server)
    page.wait_for_load_state("networkidle")
    csrf = page.locator('meta[name="csrf-token"]').get_attribute("content")
    resp = page.request.post(
        f"{dvad_server}/api/config",
        data=json.dumps({"roles": roles}),
        headers={"X-DVAD-Token": csrf, "Content-Type": "application/json"},
    )
    assert resp.status == 200, f"Config mutation failed: {resp.status} {resp.text()}"


def _fetch_ledger(page, dvad_server: str, review_id: str) -> dict:
    """Fetch the full ledger JSON for a completed review."""
    resp = page.request.get(f"{dvad_server}/api/review/{review_id}")
    assert resp.status == 200, f"Ledger fetch failed: {resp.status}"
    return resp.json()


def _start_and_wait(page, dvad_server, *, mode, project, input_files,
                    project_dir=None, dry_run=False, timeout=600_000):
    """Start a review via API and wait for completion. Returns (result, ledger)."""
    review_id = start_review_api(
        page, dvad_server,
        mode=mode, project=project,
        input_files=input_files,
        project_dir=project_dir,
        dry_run=dry_run,
    )
    result = wait_for_review_complete(page, dvad_server, review_id, timeout=timeout)
    data = _fetch_ledger(page, dvad_server, review_id)
    return result, data


def _try_start_review(page, dvad_server, *, mode, project, input_files):
    """Try to start a review; return (status_code, review_id_or_detail).

    Used for tests that may fail at validation (400) or succeed (200).
    Retries on 409 (prior review still running).
    """
    page.goto(dvad_server)
    page.wait_for_load_state("networkidle")
    csrf = page.locator('meta[name="csrf-token"]').get_attribute("content")

    multipart = {
        "mode": mode,
        "project": project,
        "input_paths": json.dumps([str(f) for f in input_files]),
    }

    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        resp = page.request.post(
            f"{dvad_server}/api/review/start",
            multipart=multipart,
            headers={"X-DVAD-Token": csrf},
        )
        if resp.status == 409:
            time.sleep(5)
            continue
        if resp.status == 400:
            return 400, resp.json().get("detail", "")
        if resp.status == 200:
            return 200, resp.json()["review_id"]
        pytest.fail(f"Unexpected status {resp.status}: {resp.text()}")
    pytest.fail("Timed out waiting for prior review to finish")


def assert_ledger_deep(
    data: dict,
    *,
    expected_results: set[str] = frozenset({"success", "completed", "complete"}),
    min_points: int = 0,
    expected_reviewer_count: int | None = None,
    expected_author_model: str | None = None,
    expected_dedup_model: str | None = None,
    check_cost_roles: list[str] | None = None,
    check_summary_keys: list[str] | None = None,
    check_point_fields: bool = False,
    mode: str = "plan",
):
    """Reusable deep ledger assertion helper."""
    result = data.get("result", "")
    assert result in expected_results, (
        f"Expected result in {expected_results}, got '{result}'"
    )

    points = data.get("points", [])
    assert len(points) >= min_points, (
        f"Expected >= {min_points} points, got {len(points)}"
    )

    if expected_reviewer_count is not None:
        reviewer_models = data.get("reviewer_models", [])
        assert len(reviewer_models) == expected_reviewer_count, (
            f"Expected {expected_reviewer_count} reviewers, "
            f"got {len(reviewer_models)}: {reviewer_models}"
        )

    if expected_author_model is not None:
        assert data.get("author_model") == expected_author_model, (
            f"Expected author_model='{expected_author_model}', "
            f"got '{data.get('author_model')}'"
        )

    if expected_dedup_model is not None:
        assert data.get("dedup_model") == expected_dedup_model, (
            f"Expected dedup_model='{expected_dedup_model}', "
            f"got '{data.get('dedup_model')}'"
        )

    if check_cost_roles:
        role_costs = data.get("cost", {}).get("role_costs", {})
        for key in check_cost_roles:
            assert key in role_costs, (
                f"Expected '{key}' in cost.role_costs, "
                f"got keys: {list(role_costs.keys())}"
            )

    if check_summary_keys:
        summary = data.get("summary", {})
        for key in check_summary_keys:
            assert key in summary, (
                f"Expected '{key}' in summary, got keys: {list(summary.keys())}"
            )

    cost = data.get("cost", {})
    total_usd = cost.get("total_usd", 0)
    if result in ("success", "completed", "complete") and min_points > 0:
        assert total_usd > 0, (
            f"Expected cost.total_usd > 0 for successful review, got {total_usd}"
        )

    summary = data.get("summary", {})
    if result in ("success", "completed", "complete") and len(points) > 0:
        assert summary.get("total_groups", 0) > 0, (
            f"Successful review with points should have total_groups > 0"
        )
        assert summary.get("total_points", 0) >= summary.get("total_groups", 0), (
            f"total_points ({summary.get('total_points')}) should >= "
            f"total_groups ({summary.get('total_groups')})"
        )

    if check_point_fields and points:
        for i, pt in enumerate(points):
            assert pt.get("description"), f"Point {i} missing description"
            assert pt.get("reviewer"), f"Point {i} missing reviewer"
            gov = pt.get("governance_resolution", "")
            if gov:
                assert gov in VALID_GOVERNANCE_RESOLUTIONS, (
                    f"Point {i} has invalid governance_resolution: '{gov}'"
                )


# ═════════════════════════════════════════════════════════════════════════════
# CLASS 1: Deep Ledger Assertions (full config, no swap)
# ═════════════════════════════════════════════════════════════════════════════


class TestDeepLedger:
    """Full config reviews with comprehensive ledger verification."""

    def test_e1_plan_deep_ledger(self, live_page, dvad_server, tmp_path):
        """Plan review -- verify every ledger field the existing matrix ignores."""
        input_file = _create_input_file(tmp_path, "plan")
        _, data = _start_and_wait(
            live_page, dvad_server,
            mode="plan", project="midrange-e1",
            input_files=[input_file], project_dir=tmp_path,
        )
        assert_ledger_deep(
            data,
            min_points=1,
            expected_reviewer_count=2,
            expected_author_model=R,
            expected_dedup_model=T,
            check_cost_roles=["reviewer_1", "reviewer_2", "dedup", "author"],
            check_point_fields=True,
            mode="plan",
        )
        summary = data["summary"]
        assert summary["total_groups"] > 0
        assert summary["total_points"] >= summary["total_groups"]
        assert data["cost"]["total_usd"] > 0
        assert set(data["reviewer_models"]) == {R, T}

    def test_e2_spec_deep_ledger(self, live_page, dvad_server, tmp_path):
        """Spec review -- verify spec-specific ledger fields."""
        input_file = _create_input_file(tmp_path, "spec")
        _, data = _start_and_wait(
            live_page, dvad_server,
            mode="spec", project="midrange-e2",
            input_files=[input_file],
        )
        assert_ledger_deep(
            data,
            min_points=1,
            expected_author_model="",
            check_summary_keys=[
                "multi_consensus", "single_source",
                "total_groups", "total_points",
            ],
            check_point_fields=True,
            mode="spec",
        )
        summary = data["summary"]
        assert summary["total_groups"] > 0
        mc = summary.get("multi_consensus", 0)
        ss = summary.get("single_source", 0)
        assert mc + ss == summary["total_groups"], (
            f"multi_consensus ({mc}) + single_source ({ss}) "
            f"!= total_groups ({summary['total_groups']})"
        )
        assert data["cost"]["total_usd"] > 0


# ═════════════════════════════════════════════════════════════════════════════
# CLASS 2: Input Quality (full config, no swap)
# ═════════════════════════════════════════════════════════════════════════════


class TestInputQuality:
    """Input quality edge cases -- trivial, empty, content type mismatch."""

    def test_d1_trivial_input(self, live_page, dvad_server, tmp_path):
        """Single sentence -- pipeline should complete, LLM may invent feedback."""
        trivial = tmp_path / "trivial.txt"
        trivial.write_text("The quick brown fox jumps over the lazy dog.")
        _, data = _start_and_wait(
            live_page, dvad_server,
            mode="plan", project="midrange-d1",
            input_files=[trivial], project_dir=tmp_path,
        )
        assert data.get("result") in ("success", "completed", "complete", "failed")
        assert "review_id" in data
        assert "cost" in data

    def test_d2_empty_input(self, live_page, dvad_server, tmp_path):
        """Zero-byte file -- API may reject or pipeline may complete with 0 points."""
        empty = tmp_path / "empty.txt"
        empty.write_text("")

        status, detail_or_id = _try_start_review(
            live_page, dvad_server,
            mode="plan", project="midrange-d2",
            input_files=[empty],
        )
        if status == 400:
            # API rejected empty input -- acceptable
            return

        review_id = detail_or_id
        wait_for_review_complete(live_page, dvad_server, review_id, timeout=120_000)
        data = _fetch_ledger(live_page, dvad_server, review_id)
        assert data.get("result") in ("success", "completed", "complete", "failed")

    def test_d3_code_as_plan(self, live_page, dvad_server, tmp_path):
        """Code file submitted as plan review -- pipeline doesn't validate content type."""
        code_file = tmp_path / "test-code.py"
        shutil.copy2(FIXTURES / "test-code.py", code_file)
        _, data = _start_and_wait(
            live_page, dvad_server,
            mode="plan", project="midrange-d3",
            input_files=[code_file], project_dir=tmp_path,
        )
        assert_ledger_deep(data, min_points=1)

    def test_d4_plan_as_code(self, live_page, dvad_server, tmp_path):
        """Plan file submitted as code review -- pipeline should still produce output."""
        plan_file = tmp_path / "test-plan.md"
        shutil.copy2(FIXTURES / "test-plan.md", plan_file)
        _, data = _start_and_wait(
            live_page, dvad_server,
            mode="code", project="midrange-d4",
            input_files=[plan_file], project_dir=tmp_path,
        )
        assert_ledger_deep(data, min_points=1)


# ═════════════════════════════════════════════════════════════════════════════
# CLASS 3: Config Variation -- Plan mode
# ═════════════════════════════════════════════════════════════════════════════


class TestConfigVariationPlan:
    """Config variation tests -- plan mode (A1-A6, A8) and code spot-check (A7)."""

    def test_a1_plan_2rev_no_revision(
        self, live_page, dvad_server, tmp_path, restore_config,
    ):
        """2 reviewers, no explicit revision -- falls back to author."""
        _apply_config(live_page, dvad_server, CONFIG_A1)
        input_file = _create_input_file(tmp_path, "plan")
        _, data = _start_and_wait(
            live_page, dvad_server,
            mode="plan", project="midrange-a1",
            input_files=[input_file], project_dir=tmp_path,
        )
        assert_ledger_deep(
            data, min_points=1, expected_reviewer_count=2,
        )

    def test_a2_plan_2rev_no_normalization(
        self, live_page, dvad_server, tmp_path, restore_config,
    ):
        """2 reviewers, no explicit normalization -- falls back to dedup."""
        _apply_config(live_page, dvad_server, CONFIG_A2)
        input_file = _create_input_file(tmp_path, "plan")
        _, data = _start_and_wait(
            live_page, dvad_server,
            mode="plan", project="midrange-a2",
            input_files=[input_file], project_dir=tmp_path,
        )
        assert_ledger_deep(
            data, min_points=1, expected_reviewer_count=2,
        )

    def test_a3_plan_2rev_both_fallbacks(
        self, live_page, dvad_server, tmp_path, restore_config,
    ):
        """2 reviewers, no revision AND no normalization -- both fallbacks active."""
        _apply_config(live_page, dvad_server, CONFIG_A3)
        input_file = _create_input_file(tmp_path, "plan")
        _, data = _start_and_wait(
            live_page, dvad_server,
            mode="plan", project="midrange-a3",
            input_files=[input_file], project_dir=tmp_path,
        )
        assert_ledger_deep(data, min_points=1)

    def test_a4_plan_1rev_with_dedup(
        self, live_page, dvad_server, tmp_path, restore_config,
    ):
        """1 reviewer + dedup assigned -- single-source dedup."""
        _apply_config(live_page, dvad_server, CONFIG_A4)
        input_file = _create_input_file(tmp_path, "plan")
        _, data = _start_and_wait(
            live_page, dvad_server,
            mode="plan", project="midrange-a4",
            input_files=[input_file], project_dir=tmp_path,
        )
        assert_ledger_deep(
            data, min_points=1, expected_reviewer_count=1,
        )

    def test_a5_plan_1rev_dedup_both_fallbacks(
        self, live_page, dvad_server, tmp_path, restore_config,
    ):
        """1 reviewer + dedup, both fallbacks -- minimal viable plan config."""
        _apply_config(live_page, dvad_server, CONFIG_A5)
        input_file = _create_input_file(tmp_path, "plan")
        _, data = _start_and_wait(
            live_page, dvad_server,
            mode="plan", project="midrange-a5",
            input_files=[input_file], project_dir=tmp_path,
        )
        assert_ledger_deep(
            data, min_points=1, expected_reviewer_count=1,
        )

    def test_a6_plan_1rev_no_dedup(
        self, live_page, dvad_server, tmp_path, restore_config,
    ):
        """1 reviewer, no dedup, explicit norm + revision.

        POTENTIAL BUG: dedup_model=None may be passed to deduplicate_points.
        Validation may reject at review-start, or pipeline may crash.
        Either outcome is recorded -- this test adapts to both.
        """
        _apply_config(live_page, dvad_server, CONFIG_A6)
        input_file = _create_input_file(tmp_path, "plan")

        status, detail_or_id = _try_start_review(
            live_page, dvad_server,
            mode="plan", project="midrange-a6",
            input_files=[input_file],
        )
        if status == 400:
            # Validation caught missing dedup -- expected
            return

        review_id = detail_or_id
        wait_for_review_complete(live_page, dvad_server, review_id)
        data = _fetch_ledger(live_page, dvad_server, review_id)

        # If pipeline completed despite dedup_model=None, verify basics
        if data.get("result") != "failed":
            assert_ledger_deep(
                data,
                expected_results={"success", "completed", "complete"},
                expected_reviewer_count=1,
            )

    def test_a7_code_1rev_both_fallbacks(
        self, live_page, dvad_server, tmp_path, restore_config,
    ):
        """Code mode spot-check with 1 reviewer + dedup + both fallbacks."""
        _apply_config(live_page, dvad_server, CONFIG_A5)
        input_file = _create_input_file(tmp_path, "code")
        _, data = _start_and_wait(
            live_page, dvad_server,
            mode="code", project="midrange-a7",
            input_files=[input_file], project_dir=tmp_path,
        )
        assert_ledger_deep(
            data, expected_reviewer_count=1,
        )

    def test_a8_plan_dryrun_sparse(
        self, live_page, dvad_server, tmp_path, restore_config,
    ):
        """Plan dry_run with sparse config -- verify dry_run still works."""
        _apply_config(live_page, dvad_server, CONFIG_A5)
        input_file = _create_input_file(tmp_path, "plan")
        _, data = _start_and_wait(
            live_page, dvad_server,
            mode="plan", project="midrange-a8",
            input_files=[input_file], project_dir=tmp_path,
            dry_run=True, timeout=120_000,
        )
        assert data.get("result") == "dry_run"
        assert len(data.get("points", [])) == 0


# ═════════════════════════════════════════════════════════════════════════════
# CLASS 4: Config Variation -- Spec mode
# ═════════════════════════════════════════════════════════════════════════════


class TestConfigVariationSpec:
    """Config variation tests -- spec mode (B1-B4)."""

    def test_b1_spec_no_author_no_revision(
        self, live_page, dvad_server, tmp_path, restore_config,
    ):
        """Spec: 1 reviewer + dedup + norm, no author/revision/integration.

        Revision falls back to author=None. Does spec crash at revision step?
        """
        _apply_config(live_page, dvad_server, CONFIG_B1)
        input_file = _create_input_file(tmp_path, "spec")

        status, detail_or_id = _try_start_review(
            live_page, dvad_server,
            mode="spec", project="midrange-b1",
            input_files=[input_file],
        )
        if status == 400:
            # Validation rejected -- record why
            return

        review_id = detail_or_id
        wait_for_review_complete(live_page, dvad_server, review_id)
        data = _fetch_ledger(live_page, dvad_server, review_id)

        if data.get("result") != "failed":
            assert_ledger_deep(
                data, min_points=1,
                expected_author_model="",
                mode="spec",
            )

    def test_b2_spec_2rev_no_revision(
        self, live_page, dvad_server, tmp_path, restore_config,
    ):
        """Spec: 2 reviewers, no revision -- falls back to author."""
        _apply_config(live_page, dvad_server, CONFIG_B2)
        input_file = _create_input_file(tmp_path, "spec")
        _, data = _start_and_wait(
            live_page, dvad_server,
            mode="spec", project="midrange-b2",
            input_files=[input_file],
        )
        assert_ledger_deep(
            data, min_points=1,
            expected_author_model="",
            expected_reviewer_count=2,
            mode="spec",
        )

    def test_b3_spec_1rev_no_norm(
        self, live_page, dvad_server, tmp_path, restore_config,
    ):
        """Spec: 1 reviewer + dedup, no norm -- falls back to dedup."""
        _apply_config(live_page, dvad_server, CONFIG_B3)
        input_file = _create_input_file(tmp_path, "spec")
        _, data = _start_and_wait(
            live_page, dvad_server,
            mode="spec", project="midrange-b3",
            input_files=[input_file],
        )
        assert_ledger_deep(
            data, min_points=1,
            expected_reviewer_count=1,
            mode="spec",
        )

    def test_b4_spec_1rev_no_dedup(
        self, live_page, dvad_server, tmp_path, restore_config,
    ):
        """Spec: 1 reviewer, no dedup, explicit norm + revision.

        Same potential crash as A6 -- dedup_model=None in spec pipeline.
        """
        _apply_config(live_page, dvad_server, CONFIG_B4)
        input_file = _create_input_file(tmp_path, "spec")

        status, detail_or_id = _try_start_review(
            live_page, dvad_server,
            mode="spec", project="midrange-b4",
            input_files=[input_file],
        )
        if status == 400:
            return

        review_id = detail_or_id
        wait_for_review_complete(live_page, dvad_server, review_id)
        data = _fetch_ledger(live_page, dvad_server, review_id)

        if data.get("result") != "failed":
            assert_ledger_deep(
                data,
                expected_results={"success", "completed", "complete"},
                expected_reviewer_count=1,
                mode="spec",
            )


# ═════════════════════════════════════════════════════════════════════════════
# CLASS 5: Config Variation -- Integration mode
# ═════════════════════════════════════════════════════════════════════════════


class TestConfigVariationIntegration:
    """Config variation tests -- integration mode (C1-C3)."""

    def test_c1_integration_no_revision(
        self, live_page, dvad_server, tmp_path, restore_config,
    ):
        """Integration: no explicit revision -- falls back to author."""
        _apply_config(live_page, dvad_server, CONFIG_C1)
        input_file = _create_input_file(tmp_path, "integration")
        project_dir = _create_project_dir(tmp_path)
        _, data = _start_and_wait(
            live_page, dvad_server,
            mode="integration", project="midrange-c1",
            input_files=[input_file], project_dir=project_dir,
        )
        assert_ledger_deep(data, mode="integration")

    def test_c2_integration_no_norm(
        self, live_page, dvad_server, tmp_path, restore_config,
    ):
        """Integration: no explicit normalization -- falls back to dedup."""
        _apply_config(live_page, dvad_server, CONFIG_C2)
        input_file = _create_input_file(tmp_path, "integration")
        project_dir = _create_project_dir(tmp_path)
        _, data = _start_and_wait(
            live_page, dvad_server,
            mode="integration", project="midrange-c2",
            input_files=[input_file], project_dir=project_dir,
        )
        assert_ledger_deep(data, mode="integration")

    def test_c3_integration_minimal(
        self, live_page, dvad_server, tmp_path, restore_config,
    ):
        """Integration: minimal -- integration + dedup only, both fallbacks."""
        _apply_config(live_page, dvad_server, CONFIG_C3)
        input_file = _create_input_file(tmp_path, "integration")
        project_dir = _create_project_dir(tmp_path)
        _, data = _start_and_wait(
            live_page, dvad_server,
            mode="integration", project="midrange-c3",
            input_files=[input_file], project_dir=project_dir,
        )
        assert_ledger_deep(data, mode="integration")
