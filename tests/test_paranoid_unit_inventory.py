"""Approach 1: Destructive Action Inventory.

Validates that the Write Registry accounts for every mutating endpoint
in the application. If the app adds a new POST/PUT/DELETE route that
isn't in the registry, these tests fail.

Also verifies that every registered write endpoint enforces CSRF
and rejects trivially empty payloads.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from paranoid_unit_helpers import (
    WRITE_ENDPOINTS,
    MUTATING_ROUTE_PATTERNS,
)

# Import conftest_paranoid fixtures
pytest_plugins = ["conftest_paranoid_unit"]


# ── Route Discovery ─────────────────────────────────────────────────────────


def _get_app_mutating_routes(app) -> list[tuple[str, str]]:
    """Extract all mutating routes (POST/PUT/DELETE/PATCH) from the FastAPI app."""
    mutating_methods = {"POST", "PUT", "DELETE", "PATCH"}
    routes = []
    for route in app.routes:
        if not hasattr(route, "methods"):
            continue
        for method in route.methods:
            if method in mutating_methods:
                routes.append((method, route.path))
    return routes


class TestRegistryCompleteness:
    """Every mutating route in the app must appear in the Write Registry."""

    def test_all_mutating_routes_are_registered(self, paranoid_app):
        """FAIL if a mutating route exists that isn't in MUTATING_ROUTE_PATTERNS."""
        app_routes = _get_app_mutating_routes(paranoid_app)
        registered = set(MUTATING_ROUTE_PATTERNS)

        unregistered = []
        for method, path in app_routes:
            if (method, path) not in registered:
                unregistered.append(f"{method} {path}")

        assert not unregistered, (
            f"Unregistered mutating routes found. Add these to MUTATING_ROUTE_PATTERNS "
            f"and WRITE_ENDPOINTS in paranoid_unit_helpers.py:\n"
            + "\n".join(f"  - {r}" for r in unregistered)
        )

    def test_all_registry_entries_have_corresponding_routes(self, paranoid_app):
        """FAIL if a registry entry references a route that doesn't exist."""
        app_routes = set(_get_app_mutating_routes(paranoid_app))

        for entry_key, entry in WRITE_ENDPOINTS.items():
            method = entry["method"]
            # Normalize the path: WRITE_ENDPOINTS uses {review_id} etc.
            path = entry["path"]
            found = (method, path) in app_routes
            assert found, (
                f"Registry entry '{entry_key}' references {method} {path} "
                f"which does not exist in the app routes."
            )


# ── CSRF Enforcement ────────────────────────────────────────────────────────


class TestCSRFEnforcement:
    """Every mutating endpoint that requires CSRF must reject requests without the token."""

    @pytest.mark.parametrize(
        "entry_key",
        [k for k, v in WRITE_ENDPOINTS.items() if v.get("requires_csrf", False)],
    )
    def test_csrf_required_on_all_mutating_endpoints(
        self, paranoid_client, entry_key,
    ):
        """Send a request without CSRF token. Must get 403."""
        entry = WRITE_ENDPOINTS[entry_key]
        method = entry["method"].lower()
        path = entry["path"]

        # Substitute path params with dummy values
        path = path.replace("{review_id}", "nonexistent_xyz")
        path = path.replace("{env_name}", "FAKE_VAR")

        payload = entry.get("valid_payload", {})

        # Choose request method
        client_method = getattr(paranoid_client, method)

        # Build kwargs based on endpoint type
        kwargs: dict = {"headers": {}}  # intentionally no CSRF token

        if method == "post" and "review/start" in path:
            # Form data endpoint
            kwargs["data"] = payload
        elif method in ("post", "put"):
            kwargs["json"] = payload
        elif method == "delete":
            pass  # no body needed

        resp = client_method(path, **kwargs)
        assert resp.status_code == 403, (
            f"{entry['method']} {path} accepted request without CSRF token "
            f"(got {resp.status_code}, expected 403)"
        )


# ── Empty Payload Rejection ─────────────────────────────────────────────────


class TestEmptyPayloadRejection:
    """Write endpoints must reject trivially empty/invalid payloads.

    This catches the class of bug where an empty form submission
    is interpreted as "clear everything."
    """

    @pytest.mark.parametrize(
        "entry_key",
        [
            k for k, v in WRITE_ENDPOINTS.items()
            if v.get("requires_csrf")
            # Skip validate (it's read-only) and cancel/revise (no meaningful empty payload)
            and "validate" not in k
            and "cancel" not in k
            and "revise" not in k.lower()
        ],
    )
    def test_empty_payload_does_not_succeed(
        self, paranoid_client, csrf_token, entry_key,
    ):
        """Send the empty_payload for each endpoint. Must NOT return 200."""
        entry = WRITE_ENDPOINTS[entry_key]
        method = entry["method"].lower()
        path = entry["path"]

        path = path.replace("{review_id}", "nonexistent_xyz")
        path = path.replace("{env_name}", "FAKE_VAR")

        payload = entry["empty_payload"]
        headers = {"X-DVAD-Token": csrf_token}

        client_method = getattr(paranoid_client, method)

        if method == "post" and "review/start" in path:
            resp = client_method(path, data=payload, headers=headers)
        elif method in ("post", "put"):
            resp = client_method(path, json=payload, headers=headers)
        elif method == "delete":
            headers["X-Confirm-Destructive"] = "true"
            resp = client_method(path, headers=headers)
        else:
            resp = client_method(path, json=payload, headers=headers)

        if resp.status_code == 200:
            # Known finding: structured roles endpoint accepts empty payload
            if entry_key == "POST /api/config (structured roles)":
                pytest.xfail(
                    "FINDING: Structured config save accepts empty roles payload, "
                    "clearing all role assignments without rejection."
                )
            else:
                pytest.fail(
                    f"{entry['method']} {path} with empty payload returned 200. "
                    f"Empty input should be rejected, not silently accepted. "
                    f"Payload was: {payload}"
                )


# ── Confirm-Destructive Header Enforcement ──────────────────────────────────


class TestConfirmDestructiveHeader:
    """Endpoints that require X-Confirm-Destructive must reject without it."""

    @pytest.mark.parametrize(
        "entry_key",
        [k for k, v in WRITE_ENDPOINTS.items() if v.get("requires_confirm_header")],
    )
    def test_destructive_ops_require_confirm_header(
        self, paranoid_client, csrf_token, entry_key,
    ):
        entry = WRITE_ENDPOINTS[entry_key]
        method = entry["method"].lower()
        path = entry["path"]

        path = path.replace("{review_id}", "nonexistent_xyz")
        path = path.replace("{env_name}", "TEST_KEY")

        headers = {"X-DVAD-Token": csrf_token}
        # Intentionally omit X-Confirm-Destructive

        client_method = getattr(paranoid_client, method)
        resp = client_method(path, headers=headers)

        assert resp.status_code == 400, (
            f"{entry['method']} {path} accepted destructive operation without "
            f"X-Confirm-Destructive header (got {resp.status_code})"
        )
