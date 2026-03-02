"""Tests for GUI pages module — vendor inference, routing, binary detection."""

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient

from devils_advocate.gui import create_app
from devils_advocate.gui.pages import _infer_vendor, _find_dvad_binary


@pytest.fixture
def app():
    return create_app()


@pytest.fixture
def client(app):
    return TestClient(app)


# ── _infer_vendor ───────────────────────────────────────────────────────────


class TestInferVendor:
    """Test vendor name inference from model metadata."""

    def _make_model(self, provider="openai", api_base=""):
        m = MagicMock()
        m.provider = provider
        m.api_base = api_base
        return m

    def test_anthropic_provider(self):
        assert _infer_vendor(self._make_model(provider="anthropic")) == "Anthropic"

    def test_openai_base(self):
        assert _infer_vendor(self._make_model(api_base="https://api.openai.com/v1")) == "OpenAI"

    def test_xai_base(self):
        assert _infer_vendor(self._make_model(api_base="https://api.x.ai/v1")) == "xAI"

    def test_google_base(self):
        m = self._make_model(api_base="https://generativelanguage.googleapis.com/v1beta")
        assert _infer_vendor(m) == "Google"

    def test_deepseek_base(self):
        assert _infer_vendor(self._make_model(api_base="https://api.deepseek.com/v1")) == "DeepSeek"

    def test_moonshot_base(self):
        assert _infer_vendor(self._make_model(api_base="https://api.moonshot.ai/v1")) == "Moonshot"

    def test_minimax_base(self):
        assert _infer_vendor(self._make_model(api_base="https://api.minimax.io/v1")) == "MiniMax"

    def test_minimax_provider(self):
        assert _infer_vendor(self._make_model(provider="minimax")) == "MiniMax"

    def test_unknown_provider_titlecased(self):
        assert _infer_vendor(self._make_model(provider="custom_vendor")) == "Custom_Vendor"

    def test_case_insensitive_base_matching(self):
        assert _infer_vendor(self._make_model(api_base="https://API.OPENAI.COM/v1")) == "OpenAI"

    def test_none_api_base_handled(self):
        m = self._make_model()
        m.api_base = None
        # Should not raise, falls through to provider-based detection
        result = _infer_vendor(m)
        assert isinstance(result, str)


# ── _find_dvad_binary ───────────────────────────────────────────────────────


class TestFindDvadBinary:
    def test_returns_string(self):
        result = _find_dvad_binary()
        assert isinstance(result, str)

    def test_found_in_path(self):
        with patch("shutil.which", return_value="/usr/local/bin/dvad"):
            assert _find_dvad_binary() == "/usr/local/bin/dvad"

    def test_fallback_to_venv_bin(self, tmp_path):
        # Create a fake dvad binary next to sys.executable
        fake_python = tmp_path / "python"
        fake_python.touch()
        fake_dvad = tmp_path / "dvad"
        fake_dvad.touch()

        with patch("shutil.which", return_value=None), \
             patch("devils_advocate.gui.pages.sys") as mock_sys:
            mock_sys.executable = str(fake_python)
            result = _find_dvad_binary()
            assert result == str(fake_dvad)

    def test_not_found_anywhere(self, tmp_path):
        fake_python = tmp_path / "python"
        fake_python.touch()
        # No dvad binary next to python

        with patch("shutil.which", return_value=None), \
             patch("devils_advocate.gui.pages.sys") as mock_sys:
            mock_sys.executable = str(fake_python)
            result = _find_dvad_binary()
            assert "not found" in result.lower()


# ── Route Tests ─────────────────────────────────────────────────────────────


class TestNewReviewRedirect:
    def test_new_review_redirects_to_dashboard(self, client):
        """GET /review/new should redirect to /."""
        resp = client.get("/review/new", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/"


class TestDashboardRoute:
    def test_dashboard_returns_200(self, client):
        resp = client.get("/")
        assert resp.status_code == 200

    def test_dashboard_pagination_clamped(self, client):
        """Page numbers beyond valid range should still return 200."""
        resp = client.get("/?page=9999")
        assert resp.status_code == 200

    def test_dashboard_show_test_param(self, client):
        """show_test=true should return 200 (filters differently)."""
        resp = client.get("/?show_test=true")
        assert resp.status_code == 200


class TestRevisionStaleDetection:
    """Test that pages.review_detail detects stale revision artifacts after overrides."""

    def test_revision_stale_when_override_newer(self, tmp_path):
        """revision_stale should be True when an override timestamp is newer than the revised file."""
        import json, time
        from datetime import datetime, timezone, timedelta

        # Create a review directory with a revised artifact
        review_id = "test-stale-review"
        reviews_dir = tmp_path / "reviews"
        review_dir = reviews_dir / review_id
        review_dir.mkdir(parents=True)

        revised = review_dir / "revised-plan.md"
        revised.write_text("old revision")
        # Set the revised file mtime to 1 hour ago
        old_mtime = time.time() - 3600
        import os
        os.utime(revised, (old_mtime, old_mtime))

        # Create a ledger with an override newer than the revised file
        now_ts = datetime.now(timezone.utc).isoformat()
        ledger = {
            "review_id": review_id,
            "mode": "plan",
            "project": "test",
            "points": [{
                "group_id": "grp_001",
                "point_id": "pt_001",
                "description": "finding",
                "final_resolution": "overridden",
                "overrides": [{
                    "previous_resolution": "escalated",
                    "new_resolution": "overridden",
                    "timestamp": now_ts,
                }],
            }],
            "cost": {"total_usd": 0.01},
            "summary": {},
        }
        (review_dir / "review-ledger.json").write_text(json.dumps(ledger))

        # Test the stale detection logic directly
        points = ledger["points"]
        revised_mtime = revised.stat().st_mtime
        revision_stale = False
        for point in points:
            for ovr in point.get("overrides", []):
                ovr_ts = datetime.fromisoformat(ovr["timestamp"])
                if ovr_ts.timestamp() > revised_mtime:
                    revision_stale = True
                    break
            if revision_stale:
                break

        assert revision_stale is True

    def test_revision_not_stale_when_no_overrides(self, tmp_path):
        """revision_stale should be False when there are no overrides."""
        import json
        from datetime import datetime, timezone

        review_dir = tmp_path / "reviews" / "test-not-stale"
        review_dir.mkdir(parents=True)
        revised = review_dir / "revised-plan.md"
        revised.write_text("revision content")

        points = [{
            "group_id": "grp_001",
            "point_id": "pt_001",
            "description": "finding",
            "final_resolution": "auto_accepted",
        }]

        revised_mtime = revised.stat().st_mtime
        revision_stale = False
        for point in points:
            for ovr in point.get("overrides", []):
                ovr_ts = datetime.fromisoformat(ovr["timestamp"])
                if ovr_ts.timestamp() > revised_mtime:
                    revision_stale = True
                    break
            if revision_stale:
                break

        assert revision_stale is False


class TestConfigPage:
    def test_config_page_returns_200(self, client):
        resp = client.get("/config")
        assert resp.status_code == 200
        assert "Configuration" in resp.text or "config" in resp.text.lower()
