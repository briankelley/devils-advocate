"""FastAPI route tests for GUI endpoints."""

import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient

from devils_advocate.gui import create_app


@pytest.fixture
def app():
    """Create a test app instance."""
    return create_app()


@pytest.fixture
def client(app):
    """Create a test client."""
    return TestClient(app)


@pytest.fixture
def token(app):
    """Get the CSRF token."""
    return app.state.csrf_token


class TestDashboard:
    def test_dashboard_returns_200(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "dvad" in resp.text

    def test_dashboard_contains_table(self, client):
        resp = client.get("/")
        assert "reviews-table" in resp.text

    def test_dashboard_pagination(self, client):
        resp = client.get("/?page=1")
        assert resp.status_code == 200


class TestNewReviewForm:
    def test_new_review_returns_200(self, client):
        resp = client.get("/review/new")
        assert resp.status_code == 200
        assert "review-form" in resp.text

    def test_form_has_mode_selector(self, client):
        resp = client.get("/review/new")
        assert 'name="mode"' in resp.text
        assert "plan" in resp.text
        assert "code" in resp.text
        assert "integration" in resp.text


class TestReviewDetail:
    def test_nonexistent_review_redirects(self, client):
        resp = client.get("/review/nonexistent_review_id_xyz", follow_redirects=False)
        assert resp.status_code == 302

    def test_existing_review_returns_200(self, client):
        """If reviews exist, detail page should work."""
        from devils_advocate.storage import StorageManager
        storage = StorageManager(Path.home())
        reviews = storage.list_reviews()
        if reviews:
            rid = reviews[0]["review_id"]
            resp = client.get(f"/review/{rid}")
            assert resp.status_code == 200


class TestOverrideEndpoint:
    def test_override_requires_csrf(self, client):
        resp = client.post(
            "/api/review/test/override",
            json={"group_id": "g1", "resolution": "overridden"},
        )
        assert resp.status_code == 403

    def test_override_rejects_invalid_resolution(self, client, token):
        resp = client.post(
            "/api/review/test/override",
            json={"group_id": "g1", "resolution": "invalid_value"},
            headers={"X-DVAD-Token": token},
        )
        assert resp.status_code == 400
        assert "Invalid resolution" in resp.json()["detail"]

    def test_override_requires_group_id(self, client, token):
        resp = client.post(
            "/api/review/test/override",
            json={"group_id": "", "resolution": "overridden"},
            headers={"X-DVAD-Token": token},
        )
        assert resp.status_code == 400

    def test_override_accepts_valid_resolutions(self, client, token):
        """Validate all three resolution values are accepted (will fail on storage lookup)."""
        for resolution in ["overridden", "auto_dismissed", "escalated"]:
            resp = client.post(
                "/api/review/nonexistent/override",
                json={"group_id": "g1", "resolution": resolution},
                headers={"X-DVAD-Token": token},
            )
            # 400 is expected since review doesn't exist,
            # but it should NOT be 400 for "Invalid resolution"
            if resp.status_code == 400:
                assert "Invalid resolution" not in resp.json().get("detail", "")


class TestStartReview:
    def test_start_requires_csrf(self, client):
        resp = client.post("/api/review/start", data={"mode": "plan", "project": "test"})
        assert resp.status_code == 403

    def test_start_requires_project(self, client, token):
        resp = client.post(
            "/api/review/start",
            data={"mode": "plan", "project": ""},
            headers={"X-DVAD-Token": token},
        )
        assert resp.status_code == 400
        assert "Project" in resp.json()["detail"]

    def test_start_plan_requires_input(self, client, token):
        resp = client.post(
            "/api/review/start",
            data={"mode": "plan", "project": "test"},
            headers={"X-DVAD-Token": token},
        )
        assert resp.status_code == 400
        assert "input file" in resp.json()["detail"].lower()

    def test_start_invalid_mode(self, client, token):
        resp = client.post(
            "/api/review/start",
            data={"mode": "invalid", "project": "test"},
            headers={"X-DVAD-Token": token},
        )
        assert resp.status_code == 400


class TestConfigEndpoints:
    def test_get_config_json(self, client):
        resp = client.get("/api/config")
        assert resp.status_code == 200
        data = resp.json()
        assert "models" in data
        assert "config_path" in data

    def test_config_page_returns_200(self, client):
        resp = client.get("/config")
        assert resp.status_code == 200
        assert "Configuration" in resp.text

    def test_validate_requires_csrf(self, client):
        resp = client.post(
            "/api/config/validate",
            json={"yaml": "models: {}"},
        )
        assert resp.status_code == 403

    def test_validate_bad_yaml(self, client, token):
        resp = client.post(
            "/api/config/validate",
            json={"yaml": "{{{{invalid yaml"},
            headers={"X-DVAD-Token": token},
        )
        data = resp.json()
        assert data["valid"] is False

    def test_save_requires_csrf(self, client):
        resp = client.post(
            "/api/config",
            json={"yaml": "models: {}"},
        )
        assert resp.status_code == 403


class TestDownloadEndpoints:
    def test_report_404_for_nonexistent(self, client):
        resp = client.get("/api/review/nonexistent/report")
        assert resp.status_code == 404

    def test_revised_404_for_nonexistent(self, client):
        resp = client.get("/api/review/nonexistent/revised")
        assert resp.status_code == 404


class TestSSEProgress:
    def test_sse_nonexistent_review(self, client):
        """SSE for nonexistent review should return a terminal event."""
        resp = client.get("/api/review/nonexistent/progress")
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        # Should contain a complete/error terminal event
        body = resp.text
        assert "data:" in body
