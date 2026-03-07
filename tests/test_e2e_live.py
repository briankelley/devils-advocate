"""Live end-to-end tests — real API calls against configured models.

Gated behind ``@pytest.mark.live`` so they never run during normal development.
Run explicitly::

    pytest -m live tests/test_e2e_live.py -v -s
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from devils_advocate.config import load_config, validate_config_structure
from devils_advocate.storage import StorageManager
from devils_advocate.types import Resolution, ReviewResult

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SAMPLE_SPEC = FIXTURES_DIR / "boardfoot.sample.spec.txt"
SAMPLE_PLAN = FIXTURES_DIR / "boardfoot.sample.plan.md"


# ─── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def live_config():
    """Load the real models.yaml and validate it.  Skip if misconfigured."""
    config = load_config()
    issues = validate_config_structure(config)
    errors = [msg for level, msg in issues if level == "error"]
    if errors:
        pytest.skip(f"Config validation errors: {'; '.join(errors)}")
    return config


@pytest.fixture
def spec_file(tmp_path: Path) -> Path:
    """Copy sample spec into the pytest tmp dir.  Skip if missing."""
    if not SAMPLE_SPEC.exists():
        pytest.skip(f"Sample spec not found: {SAMPLE_SPEC}")
    dest = tmp_path / SAMPLE_SPEC.name
    shutil.copy2(SAMPLE_SPEC, dest)
    return dest


@pytest.fixture
def plan_file(tmp_path: Path) -> Path:
    """Copy sample plan into the pytest tmp dir.  Skip if missing."""
    if not SAMPLE_PLAN.exists():
        pytest.skip(f"Sample plan not found: {SAMPLE_PLAN}")
    dest = tmp_path / SAMPLE_PLAN.name
    shutil.copy2(SAMPLE_PLAN, dest)
    return dest


@pytest.fixture
def live_storage(tmp_path: Path) -> StorageManager:
    """StorageManager that writes to the real data dir (visible in dashboard).

    Uses tmp_path only for the lock dir to avoid interfering with real locks.
    Reviews land in ~/.local/share/devils-advocate/reviews/ (or $DVAD_HOME).
    """
    return StorageManager(project_dir=tmp_path)


# ─── Assertion helpers ───────────────────────────────────────────────────────


def _assert_review_basics(
    result: ReviewResult | None,
    expected_mode: str,
    storage: StorageManager,
    has_adversarial: bool = True,
) -> None:
    """Common post-review assertions shared by every test."""
    assert result is not None, "Review returned None — check logs"
    assert result.mode == expected_mode

    # Groups validation
    assert len(result.groups) > 0, "Expected at least one review group"
    # Real LLM output should produce reasonable number of groups (not exactly 1)
    assert len(result.groups) >= 2, f"Expected multiple groups, got {len(result.groups)}"

    # All groups should have points
    for group in result.groups:
        assert len(group.points) > 0, f"Group {group.group_id} has no points"
        assert group.group_id, f"Group missing group_id"
        assert group.concern, f"Group {group.group_id} missing concern text"
        assert group.combined_severity, f"Group {group.group_id} missing severity"
        assert group.combined_category, f"Group {group.group_id} missing category"
        assert len(group.source_reviewers) > 0, f"Group {group.group_id} has no source reviewers"

        # Validate points in group
        for point in group.points:
            assert point.point_id, f"Point missing point_id"
            assert point.reviewer, f"Point {point.point_id} missing reviewer"
            assert point.severity, f"Point {point.point_id} missing severity"
            assert point.category, f"Point {point.point_id} missing category"
            assert point.description, f"Point {point.point_id} missing description"

    # Cost validation - should have multiple entries from different pipeline phases
    assert result.cost.total_usd > 0, "Expected non-zero cost"
    assert len(result.cost.entries) > 0, "No cost entries recorded"
    # Check that multiple phases contributed to cost
    models_used = set(entry["model"] for entry in result.cost.entries)
    assert len(models_used) > 1, f"Expected multiple models in cost entries, got {models_used}"

    # Validate cost tracking by pipeline phase via role_costs
    tracked_roles = set(result.cost.role_costs.keys())
    if has_adversarial:
        # Adversarial modes must track: reviewer(s) and author at minimum
        assert "author" in tracked_roles, f"Cost missing 'author' role. Tracked: {tracked_roles}"
        # At least one reviewer-like role (reviewer, reviewer_1, etc.)
        has_reviewer_cost = any(r.startswith("reviewer") for r in tracked_roles)
        assert has_reviewer_cost, f"Cost missing reviewer role. Tracked: {tracked_roles}"
        # Dedup is used by plan/code but not integration (single-reviewer, no dedup phase)
        if expected_mode != "integration":
            assert "dedup" in tracked_roles, f"Cost missing 'dedup' role. Tracked: {tracked_roles}"
    else:
        # Spec mode: reviewers + dedup + revision
        assert "dedup" in tracked_roles, f"Cost missing 'dedup' role. Tracked: {tracked_roles}"
        has_reviewer_cost = any(r.startswith("reviewer") for r in tracked_roles)
        assert has_reviewer_cost, f"Cost missing reviewer role. Tracked: {tracked_roles}"

    # Artifacts written to storage
    rd = storage.reviews_dir / result.review_id
    assert (rd / "dvad-report.md").exists(), "Missing dvad-report.md"
    assert (rd / "review-ledger.json").exists(), "Missing review-ledger.json"
    assert (rd / "original_content.txt").exists(), "Missing original_content.txt"

    # Validate report content
    report_text = (rd / "dvad-report.md").read_text()
    assert len(report_text) > 100, "Report suspiciously short"
    assert result.review_id in report_text, "Report missing review_id"
    assert result.mode in report_text or result.mode.title() in report_text, "Report missing mode"

    # Validate ledger structure
    _assert_ledger_valid(rd / "review-ledger.json", result)

    # Validate intermediate artifacts from Round 1
    _assert_round1_artifacts(rd, result)

    if has_adversarial:
        assert len(result.author_responses) > 0, "Expected author responses"
        assert len(result.governance_decisions) > 0, "Expected governance decisions"

        # Validate Round 2 artifacts
        _assert_round2_artifacts(rd, result)

        # Point-to-group integrity
        _assert_group_id_consistency(result)


def _assert_ledger_valid(ledger_path: Path, result: ReviewResult) -> None:
    """Validate ledger JSON structure and cross-reference with ReviewResult."""
    assert ledger_path.exists(), "Ledger file missing"

    # Parse JSON
    ledger_text = ledger_path.read_text()
    assert len(ledger_text) > 0, "Ledger file empty"
    ledger = json.loads(ledger_text)

    # Validate top-level structure
    assert ledger["review_id"] == result.review_id
    assert ledger["mode"] == result.mode
    assert ledger["project"] == result.project
    assert "points" in ledger
    assert "summary" in ledger
    assert "cost" in ledger

    # Validate points array
    assert len(ledger["points"]) > 0, "Ledger has no points"

    # Build group_id set from result
    valid_group_ids = {g.group_id for g in result.groups}

    # Every point in ledger should reference a valid group_id
    for point in ledger["points"]:
        assert "point_id" in point
        assert "group_id" in point
        assert point["group_id"] in valid_group_ids, (
            f"Ledger point {point['point_id']} references unknown group {point['group_id']}"
        )
        assert "severity" in point
        assert "category" in point
        assert "description" in point
        assert "reviewer" in point
        assert "governance_resolution" in point

    # Validate cost structure
    assert "total_usd" in ledger["cost"]
    assert ledger["cost"]["total_usd"] > 0
    assert "breakdown" in ledger["cost"]
    assert len(ledger["cost"]["breakdown"]) > 0

    # Validate summary
    assert ledger["summary"]["total_groups"] == len(result.groups)
    assert ledger["summary"]["total_points"] > 0


def _assert_round1_artifacts(rd: Path, result: ReviewResult) -> None:
    """Validate Round 1 intermediate artifacts."""
    # Check for reviewer raw responses
    for reviewer_name in result.reviewer_models:
        reviewer_file = rd / "round1" / f"{reviewer_name}_raw.txt"
        assert reviewer_file.exists(), f"Missing Round 1 raw response for {reviewer_name}"
        raw_text = reviewer_file.read_text()
        assert len(raw_text) > 100, f"Round 1 response from {reviewer_name} suspiciously short"

    # Round 1 structured data (saved by StorageManager.save_review_artifacts)
    round1_file = rd / "round1" / "round1-data.json"
    assert round1_file.exists(), "Missing round1/round1-data.json"
    round1_data = json.loads(round1_file.read_text())
    assert "points" in round1_data, "round1-data.json missing 'points'"
    assert "groups" in round1_data, "round1-data.json missing 'groups'"
    assert len(round1_data["groups"]) > 0, "round1-data.json has no groups"

    # Cross-check: points across groups should equal total points list
    total_group_points = sum(len(g.points) for g in result.groups)
    assert total_group_points == len(result.points), (
        f"Parser fidelity: {total_group_points} points across groups "
        f"vs {len(result.points)} in result.points"
    )


def _assert_round2_artifacts(rd: Path, result: ReviewResult) -> None:
    """Validate Round 2 intermediate artifacts (author responses, rebuttals, etc)."""
    # Author Round 1 response
    author_raw = rd / "round2" / "author_raw.txt"
    assert author_raw.exists(), "Missing Round 2 author_raw.txt"
    assert len(author_raw.read_text()) > 100, "Author raw response suspiciously short"

    # Author parsed responses
    author_responses_file = rd / "round2" / "author_responses.json"
    assert author_responses_file.exists(), "Missing author_responses.json"
    author_responses_data = json.loads(author_responses_file.read_text())
    assert len(author_responses_data) > 0, "No author responses parsed"

    # Validate each author response has required fields
    for ar in author_responses_data:
        assert "group_id" in ar
        assert "resolution" in ar
        assert "rationale" in ar

    # Governance decisions
    governance_file = rd / "round2" / "governance.json"
    assert governance_file.exists(), "Missing governance.json"
    governance_data = json.loads(governance_file.read_text())
    assert len(governance_data) > 0, "No governance decisions recorded"

    for decision in governance_data:
        assert "group_id" in decision
        assert "author_resolution" in decision
        assert "governance_resolution" in decision
        assert "reason" in decision

    # Round 2 structured data (saved by StorageManager.save_review_artifacts)
    round2_file = rd / "round2" / "round2-data.json"
    assert round2_file.exists(), "Missing round2/round2-data.json"
    round2_data = json.loads(round2_file.read_text())
    assert "author_responses" in round2_data
    assert "governance" in round2_data

    # --- Round 2 rebuttal verification ---
    # Determine whether rebuttals SHOULD exist by checking author responses.
    # If any author response is not ACCEPTED, the pipeline sends rebuttals to
    # reviewers who sourced contested groups.  Empty rebuttals are only valid
    # when the author accepted every single group.
    any_contested = any(
        ar.get("resolution") not in ("ACCEPTED",)
        for ar in author_responses_data
    )

    rebuttal_files = list((rd / "round2").glob("*_rebuttal_raw.txt"))

    if any_contested:
        # Pipeline should have sent rebuttals — verify on-disk artifacts
        # independent of result.rebuttals (which could be empty due to a bug)
        assert len(rebuttal_files) > 0, (
            "Author contested groups exist but no rebuttal raw files found on disk. "
            "Round 2 rebuttal phase may have silently failed."
        )

    # Validate any rebuttal files that do exist
    for rebuttal_file in rebuttal_files:
        rebuttal_text = rebuttal_file.read_text()
        assert len(rebuttal_text) > 50, f"Rebuttal {rebuttal_file.name} suspiciously short"

    # Cross-check: result.rebuttals should match on-disk rebuttal files
    if len(result.rebuttals) > 0:
        assert len(rebuttal_files) > 0, "Rebuttals in result but no rebuttal raw files on disk"

    # Check for author final response if challenges existed
    if len(result.author_final_responses) > 0:
        author_final_raw = rd / "round2" / "author_final_raw.txt"
        assert author_final_raw.exists(), "Author final responses exist but author_final_raw.txt missing"

        author_final_parsed = rd / "round2" / "author_final_parsed.json"
        assert author_final_parsed.exists(), "Missing author_final_parsed.json"

        final_data = json.loads(author_final_parsed.read_text())
        assert len(final_data) > 0, "author_final_parsed.json empty despite author_final_responses in result"


def _assert_group_id_consistency(result: ReviewResult) -> None:
    """Validate that all group_ids are consistent across responses/decisions/rebuttals."""
    valid_group_ids = {g.group_id for g in result.groups}

    # All author responses should reference valid group_ids
    for ar in result.author_responses:
        assert ar.group_id in valid_group_ids, (
            f"Author response references unknown group {ar.group_id}"
        )

    # All governance decisions should reference valid group_ids
    for gd in result.governance_decisions:
        assert gd.group_id in valid_group_ids, (
            f"Governance decision references unknown group {gd.group_id}"
        )

    # All rebuttals should reference valid group_ids
    for rb in result.rebuttals:
        assert rb.group_id in valid_group_ids, (
            f"Rebuttal references unknown group {rb.group_id}"
        )

    # All author final responses should reference valid group_ids
    for af in result.author_final_responses:
        assert af.group_id in valid_group_ids, (
            f"Author final response references unknown group {af.group_id}"
        )


def _assert_governance_rules_applied(result: ReviewResult) -> None:
    """Validate that governance rules were correctly applied.

    This checks basic governance invariants without requiring exact outcomes,
    since LLM responses vary. The goal is to catch logic bugs in the governance
    engine, not to verify specific LLM behavior.
    """
    # Build lookup maps
    response_map = {ar.group_id: ar for ar in result.author_responses}
    decision_map = {gd.group_id: gd for gd in result.governance_decisions}
    rebuttal_map: dict[str, list] = {}
    for rb in result.rebuttals:
        rebuttal_map.setdefault(rb.group_id, []).append(rb)

    # Every group should have a governance decision
    for group in result.groups:
        assert group.group_id in decision_map, (
            f"Group {group.group_id} missing governance decision"
        )

        decision = decision_map[group.group_id]
        author_response = response_map.get(group.group_id)

        # Validate auto_accepted: requires author accepted and no challenge
        if decision.governance_resolution == "auto_accepted":
            # Must have author response
            assert author_response is not None, (
                f"Group {group.group_id} auto_accepted but no author response"
            )
            # Author must have accepted
            assert author_response.resolution == "ACCEPTED", (
                f"Group {group.group_id} auto_accepted but author resolution was {author_response.resolution}"
            )
            # Should not have challenges (or if it does, must be from Round 2 final acceptance)
            group_rebuttals = rebuttal_map.get(group.group_id, [])
            challenges = [rb for rb in group_rebuttals if rb.verdict == "CHALLENGE"]
            # If there are challenges, author must have final response accepting
            if challenges:
                author_final = next(
                    (af for af in result.author_final_responses if af.group_id == group.group_id),
                    None
                )
                assert author_final is not None and author_final.resolution == "ACCEPTED", (
                    f"Group {group.group_id} auto_accepted with challenges but no final acceptance"
                )

        # Validate escalated: must have a reason
        if decision.governance_resolution == "escalated":
            assert decision.reason, f"Group {group.group_id} escalated without reason"

        # Validate auto_dismissed: author rejected single reviewer, no challenges
        if decision.governance_resolution == "auto_dismissed":
            assert author_response is not None, (
                f"Group {group.group_id} auto_dismissed but no author response"
            )
            assert author_response.resolution == "REJECTED", (
                f"Group {group.group_id} auto_dismissed but author resolution was {author_response.resolution}"
            )
            # Single reviewer only
            assert len(group.source_reviewers) == 1, (
                f"Group {group.group_id} auto_dismissed but has {len(group.source_reviewers)} reviewers"
            )

    # All points should belong to their parent group
    for group in result.groups:
        for point in group.points:
            # Point IDs should contain their group ID as prefix
            assert point.point_id.startswith(group.group_id), (
                f"Point {point.point_id} does not belong to group {group.group_id}"
            )


# ─── Tests ───────────────────────────────────────────────────────────────────


@pytest.mark.live
async def test_plan_review_live(live_config, plan_file, spec_file, live_storage):
    """Full adversarial plan review against live APIs."""
    from devils_advocate.orchestrator.plan import run_plan_review

    result = await run_plan_review(
        live_config,
        [plan_file, spec_file],
        project="e2e-live",
        max_cost=5.00,
        storage=live_storage,
    )

    _assert_review_basics(result, "plan", live_storage)

    # Governance decisions should use recognised Resolution values
    valid = {r.value for r in Resolution}
    for d in result.governance_decisions:
        assert d.governance_resolution in valid, (
            f"Unexpected resolution: {d.governance_resolution}"
        )

    # Validate governance correctness
    _assert_governance_rules_applied(result)

    # Actionable findings should produce a revised plan
    actionable = [d for d in result.governance_decisions
                  if d.governance_resolution in ("auto_accepted", "accepted", "overridden")]
    rd = live_storage.reviews_dir / result.review_id
    if actionable:
        revised_plan = rd / "revised-plan.md"
        assert revised_plan.exists(), (
            "Actionable governance decisions present but revised-plan.md missing"
        )
        # Validate revised plan content
        revised_text = revised_plan.read_text()
        assert len(revised_text) > 100, "Revised plan suspiciously short"
        # Plan should contain markdown structure
        assert "#" in revised_text, "Revised plan missing markdown headers"


@pytest.mark.live
async def test_plan_review_no_spec_live(live_config, plan_file, live_storage):
    """Plan review with only a plan file — no spec reference."""
    from devils_advocate.orchestrator.plan import run_plan_review

    result = await run_plan_review(
        live_config,
        [plan_file],
        project="e2e-live",
        max_cost=5.00,
        storage=live_storage,
    )

    _assert_review_basics(result, "plan", live_storage)

    # Governance decisions should use recognised Resolution values
    valid = {r.value for r in Resolution}
    for d in result.governance_decisions:
        assert d.governance_resolution in valid, (
            f"Unexpected resolution: {d.governance_resolution}"
        )

    _assert_governance_rules_applied(result)

    # Should still produce actionable findings even without spec context
    actionable = [d for d in result.governance_decisions
                  if d.governance_resolution in ("auto_accepted", "accepted", "overridden")]
    rd = live_storage.reviews_dir / result.review_id
    if actionable:
        revised_plan = rd / "revised-plan.md"
        assert revised_plan.exists(), (
            "Actionable governance decisions present but revised-plan.md missing"
        )
        revised_text = revised_plan.read_text()
        assert len(revised_text) > 100, "Revised plan suspiciously short"


@pytest.mark.live
@pytest.mark.skip(reason="Needs real code fixtures — spec-only input is not meaningful for code review")
async def test_code_review_live(live_config, spec_file, live_storage):
    """Full adversarial code review against live APIs."""
    from devils_advocate.orchestrator.code import run_code_review

    result = await run_code_review(
        live_config,
        spec_file,
        project="e2e-live",
        max_cost=5.00,
        storage=live_storage,
    )

    _assert_review_basics(result, "code", live_storage)

    # Code-specific validation
    rd = live_storage.reviews_dir / result.review_id

    # Governance decisions should use recognised Resolution values
    valid = {r.value for r in Resolution}
    for d in result.governance_decisions:
        assert d.governance_resolution in valid, (
            f"Unexpected resolution: {d.governance_resolution}"
        )

    # Validate governance correctness
    _assert_governance_rules_applied(result)

    # Code review should produce actionable findings (code always needs fixing)
    actionable = [d for d in result.governance_decisions
                  if d.governance_resolution in ("auto_accepted", "accepted", "overridden")]
    assert len(actionable) > 0, "Code review produced no actionable findings"

    # If actionable findings exist, should have revised diff
    revised_diff = rd / "revised-diff.patch"
    assert revised_diff.exists(), "Actionable findings present but revised-diff.patch missing"

    # Validate diff content
    diff_text = revised_diff.read_text()
    assert len(diff_text) > 50, "Revised diff suspiciously short"


@pytest.mark.live
async def test_spec_review_live(live_config, spec_file, live_storage):
    """Collaborative ideation (non-adversarial) spec review."""
    from devils_advocate.orchestrator.spec import run_spec_review

    result = await run_spec_review(
        live_config,
        [spec_file],
        project="e2e-live",
        max_cost=5.00,
        storage=live_storage,
    )

    _assert_review_basics(result, "spec", live_storage, has_adversarial=False)

    # Spec mode has no author round
    assert len(result.author_responses) == 0, "Spec mode should have no author responses"
    assert len(result.governance_decisions) == 0, "Spec mode should have no governance"
    assert len(result.rebuttals) == 0, "Spec mode should have no rebuttals"
    assert len(result.author_final_responses) == 0, "Spec mode should have no author final responses"

    # Summary should contain spec-specific keys
    assert "total_groups" in result.summary
    assert "multi_consensus" in result.summary
    assert "single_source" in result.summary
    assert "total_points" in result.summary

    # Validate spec summary metrics
    total_groups = result.summary["total_groups"]
    multi_consensus = result.summary["multi_consensus"]
    single_source = result.summary["single_source"]

    assert total_groups > 0, "No groups in spec review"
    assert multi_consensus + single_source == total_groups, (
        f"Consensus counts don't add up: {multi_consensus} + {single_source} != {total_groups}"
    )

    # Spec revision always runs — should produce suggestion report
    rd = live_storage.reviews_dir / result.review_id
    suggestions_file = rd / "revised-spec-suggestions.md"
    assert suggestions_file.exists(), "Missing revised-spec-suggestions.md"

    # Validate suggestion report content
    suggestions_text = suggestions_file.read_text()
    assert len(suggestions_text) > 100, "Spec suggestions suspiciously short"
    # Should contain structured content
    assert "#" in suggestions_text or "-" in suggestions_text, (
        "Spec suggestions missing markdown structure"
    )


@pytest.mark.live
@pytest.mark.skip(reason="Needs real code fixtures — spec-only input is not meaningful for integration mode")
async def test_integration_review_live(live_config, spec_file, live_storage):
    """Integration review against live APIs."""
    from devils_advocate.orchestrator.integration import run_integration_review

    result = await run_integration_review(
        live_config,
        project="e2e-live",
        input_files=[str(spec_file)],
        max_cost=5.00,
        storage=live_storage,
    )

    _assert_review_basics(result, "integration", live_storage)

    # Integration-specific validation
    rd = live_storage.reviews_dir / result.review_id

    # Governance decisions should use recognised Resolution values
    valid = {r.value for r in Resolution}
    for d in result.governance_decisions:
        assert d.governance_resolution in valid, (
            f"Unexpected resolution: {d.governance_resolution}"
        )

    # Validate governance correctness
    _assert_governance_rules_applied(result)

    # Integration reviews should surface cross-cutting concerns
    # Check for reasonable variety in categories (not all the same)
    categories = {g.combined_category for g in result.groups}
    assert len(categories) > 1, (
        f"Integration review found only one category: {categories}"
    )

    # Should have actionable findings or escalations
    has_actionable = any(
        d.governance_resolution in ("auto_accepted", "accepted", "overridden")
        for d in result.governance_decisions
    )
    has_escalated = any(
        d.governance_resolution == "escalated"
        for d in result.governance_decisions
    )
    assert has_actionable or has_escalated, (
        "Integration review produced no actionable findings or escalations"
    )

    # If actionable findings exist, should have remediation plan
    if has_actionable:
        remediation = rd / "remediation-plan.md"
        assert remediation.exists(), "Actionable findings present but remediation-plan.md missing"

        # Validate remediation content
        remediation_text = remediation.read_text()
        assert len(remediation_text) > 100, "Remediation plan suspiciously short"
        # Should contain structured guidance
        assert "#" in remediation_text or "-" in remediation_text, (
            "Remediation plan missing markdown structure"
        )
