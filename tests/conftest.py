"""Shared test fixtures."""

import pytest
import yaml as _yaml
from datetime import datetime, timezone

from devils_advocate.types import (
    AuthorFinalResponse,
    AuthorResponse,
    CostTracker,
    GovernanceDecision,
    ModelConfig,
    RebuttalResponse,
    Resolution,
    ReviewContext,
    ReviewGroup,
    ReviewPoint,
)


# ─── Live Test Gate ──────────────────────────────────────────────────────────


def _is_live_testing_enabled() -> bool:
    """Check models.yaml for settings.live_testing flag."""
    try:
        from devils_advocate.config import find_config
        config_path = find_config()
        with open(config_path) as f:
            raw = _yaml.safe_load(f)
        return bool(raw.get("settings", {}).get("live_testing", False))
    except Exception:
        return False


def pytest_collection_modifyitems(config, items):
    """Auto-skip @pytest.mark.live tests unless explicitly opted in."""
    markexpr = config.option.markexpr
    if markexpr and "live" in markexpr:
        return
    if _is_live_testing_enabled():
        return

    skip_live = pytest.mark.skip(
        reason="Live tests require: -m live flag, or settings.live_testing: true in models.yaml"
    )
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


# ─── Factory Helpers ─────────────────────────────────────────────────────────


def make_review_point(
    point_id="temp_001",
    reviewer="reviewer_a",
    severity="medium",
    category="correctness",
    description="Some finding description",
    recommendation="Fix it",
    location="src/foo.py",
):
    return ReviewPoint(
        point_id=point_id,
        reviewer=reviewer,
        severity=severity,
        category=category,
        description=description,
        recommendation=recommendation,
        location=location,
    )


def make_review_group(
    group_id="grp_001",
    concern="Test concern",
    points=None,
    combined_severity="medium",
    combined_category="correctness",
    source_reviewers=None,
    guid="",
):
    return ReviewGroup(
        group_id=group_id,
        concern=concern,
        points=points or [make_review_point()],
        combined_severity=combined_severity,
        combined_category=combined_category,
        source_reviewers=source_reviewers or ["reviewer_a"],
        guid=guid,
    )


def make_author_response(
    group_id="grp_001",
    resolution="ACCEPTED",
    rationale="This is a substantive rationale with enough words to pass the minimum word count validation check easily",
):
    return AuthorResponse(
        group_id=group_id,
        resolution=resolution,
        rationale=rationale,
    )


def make_rebuttal(
    group_id="grp_001",
    reviewer="reviewer_b",
    verdict="CHALLENGE",
    rationale="I disagree because the approach is flawed",
):
    return RebuttalResponse(
        group_id=group_id,
        reviewer=reviewer,
        verdict=verdict,
        rationale=rationale,
    )


def make_author_final(
    group_id="grp_001",
    resolution="ACCEPTED",
    rationale="After consideration this is a detailed enough rationale with more than fifteen words to pass the word count check",
):
    return AuthorFinalResponse(
        group_id=group_id,
        resolution=resolution,
        rationale=rationale,
    )


def make_model_config(
    name="test-model",
    provider="openai",
    model_id="gpt-4",
    api_key_env="TEST_KEY",
    cost_per_1k_input=0.03,
    cost_per_1k_output=0.06,
    context_window=128000,
):
    return ModelConfig(
        name=name,
        provider=provider,
        model_id=model_id,
        api_key_env=api_key_env,
        cost_per_1k_input=cost_per_1k_input,
        cost_per_1k_output=cost_per_1k_output,
        context_window=context_window,
    )


# ─── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def fixed_time():
    """A fixed datetime for deterministic ID testing."""
    return datetime(2026, 2, 14, 18, 26, 0, tzinfo=timezone.utc)


@pytest.fixture
def review_context(fixed_time):
    return ReviewContext(
        project="test-project",
        review_id="test_review",
        review_start_time=fixed_time,
        id_suffix="abcd",
    )


@pytest.fixture
def sample_group():
    return make_review_group()


@pytest.fixture
def sample_model():
    return make_model_config()
