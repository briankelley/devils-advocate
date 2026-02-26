"""Visual regression tests — screenshot capture and perceptual diff.

Uses pixelmatch for image comparison. First run generates baselines;
subsequent runs compare against them.

Update baselines: delete tests/e2e/baselines/<name>.png and re-run.
"""

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

BASELINES = Path(__file__).parent / "baselines"
DIFFS = Path(__file__).parent / "diffs"
SEEDED_REVIEW_ID = "captured_e2e_review"

# Diff thresholds
_PIXEL_PERFECT = os.environ.get("DVAD_E2E_PIXEL_PERFECT")
_THRESHOLD = 0.0 if _PIXEL_PERFECT else 0.2
_MAX_DIFF_RATIO = 0 if _PIXEL_PERFECT else 0.005


def _compare_or_create(page, name: str, full_page: bool = True):
    """Capture screenshot and compare against baseline.

    If no baseline exists, creates one (first run). On mismatch,
    saves a diff image and fails.
    """
    from PIL import Image
    from pixelmatch.contrib.PIL import pixelmatch as pm
    import io

    screenshot_bytes = page.screenshot(full_page=full_page)
    actual = Image.open(io.BytesIO(screenshot_bytes))

    baseline_path = BASELINES / name
    if not baseline_path.exists():
        # First run — save as baseline
        actual.save(baseline_path)
        return

    expected = Image.open(baseline_path)

    # Resize to match if dimensions differ (viewport variance)
    if actual.size != expected.size:
        actual = actual.resize(expected.size, Image.LANCZOS)

    diff_img = Image.new("RGBA", expected.size)
    num_diff = pm(expected, actual, diff_img, threshold=_THRESHOLD)

    total_pixels = expected.size[0] * expected.size[1]
    diff_ratio = num_diff / total_pixels if total_pixels > 0 else 0

    if diff_ratio > _MAX_DIFF_RATIO:
        DIFFS.mkdir(exist_ok=True)
        diff_img.save(DIFFS / f"diff_{name}")
        actual.save(DIFFS / f"actual_{name}")
        pytest.fail(
            f"Visual regression: {name} differs by {diff_ratio:.4%} "
            f"({num_diff} pixels). Diff saved to tests/e2e/diffs/"
        )


def test_dashboard_visual(page, dvad_server):
    """Screenshot baseline for the dashboard with seeded data."""
    page.goto(dvad_server)
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(500)
    _compare_or_create(page, "dashboard_with_reviews.png")


def test_review_detail_visual(page, dvad_server):
    """Screenshot baseline for the completed review detail page."""
    page.goto(f"{dvad_server}/review/{SEEDED_REVIEW_ID}")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(500)
    _compare_or_create(page, "review_complete.png")


def test_config_structured_visual(page, dvad_server):
    """Screenshot baseline for the config page (structured tab)."""
    page.goto(f"{dvad_server}/config")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(500)
    _compare_or_create(page, "config_structured.png")


def test_config_raw_yaml_visual(page, dvad_server):
    """Screenshot baseline for the config page (raw YAML tab)."""
    page.goto(f"{dvad_server}/config")
    page.wait_for_load_state("networkidle")
    page.locator('.tab-btn[data-tab="raw"]').click()
    page.wait_for_timeout(300)
    _compare_or_create(page, "config_raw_yaml.png")
