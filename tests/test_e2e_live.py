"""Live end-to-end tests — real API calls against configured models.

Gated behind ``@pytest.mark.live`` so they never run during normal development.
Run explicitly::

    pytest -m live tests/test_e2e_live.py -v -s
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from devils_advocate.config import load_config, validate_config
from devils_advocate.storage import StorageManager
from devils_advocate.types import Resolution, ReviewResult

SAMPLE_SPEC = Path.home() / "Desktop" / "sample.spec.txt"


# ─── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def live_config():
    """Load the real models.yaml and validate it.  Skip if misconfigured."""
    config = load_config()
    issues = validate_config(config)
    errors = [msg for level, msg in issues if level == "error"]
    if errors:
        pytest.skip(f"Config validation errors: {'; '.join(errors)}")
    return config


@pytest.fixture
def spec_file(tmp_path: Path) -> Path:
    """Copy sample.spec.txt into the pytest tmp dir.  Skip if missing."""
    if not SAMPLE_SPEC.exists():
        pytest.skip(f"Sample spec not found: {SAMPLE_SPEC}")
    dest = tmp_path / "sample.spec.txt"
    shutil.copy2(SAMPLE_SPEC, dest)
    return dest


@pytest.fixture
def live_storage(tmp_path: Path) -> StorageManager:
    """Isolated StorageManager — all artifacts stay in pytest's tmp dir."""
    return StorageManager(project_dir=tmp_path, data_dir=tmp_path / "data")


# ─── Assertion helper ────────────────────────────────────────────────────────


def _assert_review_basics(
    result: ReviewResult | None,
    expected_mode: str,
    storage: StorageManager,
    has_adversarial: bool = True,
) -> None:
    """Common post-review assertions shared by every test."""
    assert result is not None, "Review returned None — check logs"
    assert result.mode == expected_mode
    assert len(result.groups) > 0, "Expected at least one review group"
    assert result.cost.total_usd > 0, "Expected non-zero cost"

    # Artifacts written to storage
    rd = storage.reviews_dir / result.review_id
    assert (rd / "dvad-report.md").exists(), "Missing dvad-report.md"
    assert (rd / "review-ledger.json").exists(), "Missing review-ledger.json"
    assert (rd / "original_content.txt").exists(), "Missing original_content.txt"

    if has_adversarial:
        assert len(result.author_responses) > 0, "Expected author responses"
        assert len(result.governance_decisions) > 0, "Expected governance decisions"


# ─── Tests ───────────────────────────────────────────────────────────────────


@pytest.mark.live
async def test_plan_review_live(live_config, spec_file, live_storage):
    """Full adversarial plan review against live APIs."""
    from devils_advocate.orchestrator.plan import run_plan_review

    result = await run_plan_review(
        live_config,
        [spec_file],
        project="e2e-live",
        max_cost=0.50,
        storage=live_storage,
    )

    _assert_review_basics(result, "plan", live_storage)

    # Governance decisions should use recognised Resolution values
    valid = {r.value for r in Resolution}
    for d in result.governance_decisions:
        assert d.governance_resolution in valid, (
            f"Unexpected resolution: {d.governance_resolution}"
        )

    # Actionable findings should produce a revised plan
    actionable = {d for d in result.governance_decisions
                  if d.governance_resolution in ("auto_accepted", "accepted", "overridden")}
    rd = live_storage.reviews_dir / result.review_id
    if actionable:
        assert (rd / "revised-plan.md").exists(), (
            "Actionable governance decisions present but revised-plan.md missing"
        )


@pytest.mark.live
async def test_code_review_live(live_config, spec_file, live_storage):
    """Full adversarial code review against live APIs."""
    from devils_advocate.orchestrator.code import run_code_review

    result = await run_code_review(
        live_config,
        spec_file,
        project="e2e-live",
        max_cost=0.50,
        storage=live_storage,
    )

    _assert_review_basics(result, "code", live_storage)


@pytest.mark.live
async def test_spec_review_live(live_config, spec_file, live_storage):
    """Collaborative ideation (non-adversarial) spec review."""
    from devils_advocate.orchestrator.spec import run_spec_review

    result = await run_spec_review(
        live_config,
        [spec_file],
        project="e2e-live",
        max_cost=0.50,
        storage=live_storage,
    )

    _assert_review_basics(result, "spec", live_storage, has_adversarial=False)

    # Spec mode has no author round
    assert len(result.author_responses) == 0, "Spec mode should have no author responses"

    # Summary should contain spec-specific keys
    assert "total_groups" in result.summary
    assert "multi_consensus" in result.summary

    # Spec revision always runs — should produce suggestion report
    rd = live_storage.reviews_dir / result.review_id
    assert (rd / "revised-spec-suggestions.md").exists(), (
        "Missing revised-spec-suggestions.md"
    )


@pytest.mark.live
async def test_integration_review_live(live_config, spec_file, live_storage):
    """Integration review against live APIs."""
    from devils_advocate.orchestrator.integration import run_integration_review

    result = await run_integration_review(
        live_config,
        project="e2e-live",
        input_files=[str(spec_file)],
        max_cost=0.50,
        storage=live_storage,
    )

    _assert_review_basics(result, "integration", live_storage)
