"""Paranoid test infrastructure - Write Registry, Loss Annotations, State Snapshots.

This module is the keystone for the paranoid E2E test layer. Every other
test_paranoid_*.py file imports from here. The WRITE_ENDPOINTS registry
catalogs every state-mutating operation in the dvad GUI, and the
LOSS_ANNOTATIONS layer enriches each entry with semantic data-loss metadata.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

import yaml


# ---------------------------------------------------------------------------
# Write Registry - mechanical catalog of every mutating operation
# ---------------------------------------------------------------------------

WRITE_ENDPOINTS: dict[str, dict[str, Any]] = {
    "POST /api/review/start": {
        "method": "POST",
        "path": "/api/review/start",
        "content_type": "multipart/form-data",
        "writes_to": "~/.local/share/devils-advocate/reviews/<new_id>/",
        "destroys": "nothing (creates new directory)",
        "empty_payload": {
            "mode": "plan",
            "project": "",
            "input_paths": "[]",
        },
        "valid_payload": {
            "mode": "plan",
            "project": "test-project",
            "input_paths": "[]",  # needs real file paths at runtime
        },
    },
    "POST /api/review/{id}/cancel": {
        "method": "POST",
        "path": "/api/review/{id}/cancel",
        "content_type": "application/json",
        "writes_to": "in-memory runner state (cancels asyncio task)",
        "destroys": "running review progress (partial artifacts may remain)",
        "empty_payload": {},
        "valid_payload": {},
    },
    "POST /api/review/{id}/override": {
        "method": "POST",
        "path": "/api/review/{id}/override",
        "content_type": "application/json",
        "writes_to": "review-ledger.json (appends override entry, changes final_resolution)",
        "destroys": "previous final_resolution value for the point/group",
        "empty_payload": {"group_id": "", "resolution": ""},
        "valid_payload": {"group_id": "some_group_id", "resolution": "overridden"},
    },
    "POST /api/config/model-timeout": {
        "method": "POST",
        "path": "/api/config/model-timeout",
        "content_type": "application/json",
        "writes_to": "models.yaml (single model timeout field)",
        "destroys": "previous timeout value for the model",
        "empty_payload": {"model_name": "", "timeout": None},
        "valid_payload": {"model_name": "e2e-remote", "timeout": 300},
    },
    "POST /api/config/model-thinking": {
        "method": "POST",
        "path": "/api/config/model-thinking",
        "content_type": "application/json",
        "writes_to": "models.yaml (single model thinking toggle)",
        "destroys": "previous thinking value for the model",
        "empty_payload": {"model_name": "", "thinking": False},
        "valid_payload": {"model_name": "e2e-remote", "thinking": True},
    },
    "POST /api/config/model-max-tokens": {
        "method": "POST",
        "path": "/api/config/model-max-tokens",
        "content_type": "application/json",
        "writes_to": "models.yaml (single model max_out_configured)",
        "destroys": "previous max_out_configured value for the model",
        "empty_payload": {"model_name": "", "max_out_configured": None},
        "valid_payload": {"model_name": "e2e-remote", "max_out_configured": 4096},
    },
    "POST /api/config/settings-toggle": {
        "method": "POST",
        "path": "/api/config/settings-toggle",
        "content_type": "application/json",
        "writes_to": "models.yaml (settings block boolean flag)",
        "destroys": "previous value of the toggled setting",
        "empty_payload": {"key": "", "value": False},
        "valid_payload": {"key": "live_testing", "value": True},
    },
    "POST /api/config/validate": {
        "method": "POST",
        "path": "/api/config/validate",
        "content_type": "application/json",
        "writes_to": "temp file (deleted immediately)",
        "destroys": "nothing (validation only)",
        "empty_payload": {"yaml": ""},
        "valid_payload": {"yaml": "models: {}\nroles: {}"},
    },
    "POST /api/config": {
        "method": "POST",
        "path": "/api/config",
        "content_type": "application/json",
        "writes_to": "models.yaml (full overwrite)",
        "destroys": "entire previous models.yaml content",
        "empty_payload": {"yaml": ""},
        "valid_payload": None,  # populated at runtime from fixture YAML
    },
    "PUT /api/config/env/{env_name}": {
        "method": "PUT",
        "path": "/api/config/env/{env_name}",
        "content_type": "application/json",
        "writes_to": ".env file (single key), os.environ",
        "destroys": "previous value of the key in .env and os.environ",
        "empty_payload": {"value": ""},
        "valid_payload": {"value": "sk-test-key-12345"},
    },
    "DELETE /api/config/env/{env_name}": {
        "method": "DELETE",
        "path": "/api/config/env/{env_name}",
        "content_type": "application/json",
        "writes_to": ".env file (removes key), os.environ (removes key)",
        "destroys": "the key-value pair entirely from .env and os.environ",
        "empty_payload": {},
        "valid_payload": {},
    },
    "POST /api/config/env": {
        "method": "POST",
        "path": "/api/config/env",
        "content_type": "application/json",
        "writes_to": ".env file (batch update), os.environ (batch update)",
        "destroys": "keys with empty string values are REMOVED from .env and os.environ",
        "empty_payload": {"env_vars": {}},
        "valid_payload": {"env_vars": {"E2E_LOCAL_KEY": "test-value"}},
    },
    "POST /api/review/{id}/revise": {
        "method": "POST",
        "path": "/api/review/{id}/revise",
        "content_type": "application/json",
        "writes_to": "review dir (revised-plan.md, revised-diff.patch, or remediation-plan.md)",
        "destroys": "previous revised artifact if re-running revision",
        "empty_payload": {},
        "valid_payload": {},
    },
    "POST /api/review/{id}/revise-full": {
        "method": "POST",
        "path": "/api/review/{id}/revise-full",
        "content_type": "application/json",
        "writes_to": "review dir (revised-{filename})",
        "destroys": "previous revised file if re-running full revision",
        "empty_payload": {},
        "valid_payload": {},
    },
}


# ---------------------------------------------------------------------------
# Loss Annotations - semantic enrichment of each registry entry
# ---------------------------------------------------------------------------

LOSS_ANNOTATIONS: dict[str, dict[str, Any]] = {
    "POST /api/review/start": {
        "on_empty_input": "400 error - project name required, input files required",
        "on_all_empty": "400 error - validation catches empty project",
        "reversible": False,
        "backup_exists": False,
        "confirmation_required": True,
        "precondition": "No review currently running (409 if busy)",
    },
    "POST /api/review/{id}/cancel": {
        "on_empty_input": "Cancels the running review (no payload needed)",
        "on_all_empty": "Cancels the running review",
        "reversible": False,
        "backup_exists": False,
        "confirmation_required": True,
        "precondition": "A review must be running with the given ID",
    },
    "POST /api/review/{id}/override": {
        "on_empty_input": "400 error - group_id required, resolution validated",
        "on_all_empty": "400 error - validation catches empty fields",
        "reversible": True,  # can override again
        "backup_exists": True,  # overrides list preserves history
        "confirmation_required": False,
        "precondition": "Review must exist, group_id must exist in ledger",
    },
    "POST /api/config/model-timeout": {
        "on_empty_input": "400 error - model_name required, timeout must be int 10-7200",
        "on_all_empty": "400 error - validation catches empty model_name",
        "reversible": True,  # can set back to original value
        "backup_exists": False,
        "confirmation_required": False,
        "precondition": "Model must exist in models.yaml",
    },
    "POST /api/config/model-thinking": {
        "on_empty_input": "400 error - model_name required",
        "on_all_empty": "400 error - empty model_name rejected",
        "reversible": True,
        "backup_exists": False,
        "confirmation_required": False,
        "precondition": "Model must exist in models.yaml",
    },
    "POST /api/config/model-max-tokens": {
        "on_empty_input": "400 error - model_name required",
        "on_all_empty": "400 error - requires clear=true flag to remove key",
        "reversible": True,
        "backup_exists": False,
        "confirmation_required": False,
        "precondition": "Model must exist in models.yaml",
    },
    "POST /api/config/settings-toggle": {
        "on_empty_input": "400 error - unknown setting key",
        "on_all_empty": "400 error - empty key not in valid_keys",
        "reversible": True,
        "backup_exists": False,
        "confirmation_required": False,
        "precondition": "Key must be in valid_keys set (currently: live_testing)",
    },
    "POST /api/config/validate": {
        "on_empty_input": "Returns invalid - YAML parse error on empty string",
        "on_all_empty": "Returns invalid - no persistent side effects",
        "reversible": True,  # no persistent write
        "backup_exists": True,  # no persistent write
        "confirmation_required": False,
        "precondition": "None",
    },
    "POST /api/config": {
        "on_empty_input": "400 error - YAML parse error on empty string",
        "on_all_empty": "400 error - missing models key",
        "reversible": False,
        "backup_exists": True,
        "confirmation_required": True,
        "precondition": "YAML must parse, must have models and roles keys, must pass validation",
    },
    "PUT /api/config/env/{env_name}": {
        "on_empty_input": "400 error - value cannot be empty",
        "on_all_empty": "400 error - empty value rejected",
        "reversible": True,  # can PUT again with old value
        "backup_exists": False,
        "confirmation_required": False,
        "precondition": "env_name must be in allowed_env_names from config",
    },
    "DELETE /api/config/env/{env_name}": {
        "on_empty_input": "400 error - requires X-Confirm-Destructive header",
        "on_all_empty": "400 error - requires X-Confirm-Destructive header",
        "reversible": False,  # value is gone unless user remembers it
        "backup_exists": True,
        "confirmation_required": True,
        "precondition": "env_name must be in allowed_env_names",
    },
    "POST /api/config/env": {
        "on_empty_input": "400 error - no environment variables provided",
        "on_all_empty": "400 error - empty env_vars dict rejected",
        "reversible": False,  # empty string values DELETE keys irreversibly
        "backup_exists": True,
        "confirmation_required": True,
        "precondition": "Keys must be in allowed_env_names",
    },
    "POST /api/review/{id}/revise": {
        "on_empty_input": "Attempts revision (no input fields - uses ledger data)",
        "on_all_empty": "Attempts revision - depends on review state",
        "reversible": True,  # can re-run revision
        "backup_exists": False,
        "confirmation_required": False,
        "precondition": "Review must exist, original_content.txt must exist",
    },
    "POST /api/review/{id}/revise-full": {
        "on_empty_input": "Attempts full-file revision (no input fields)",
        "on_all_empty": "Attempts full-file revision",
        "reversible": True,
        "backup_exists": False,
        "confirmation_required": False,
        "precondition": "Review must exist and be code mode, original_content.txt must exist",
    },
}


# Computed: endpoints where data loss is unguarded
UNGUARDED_DESTRUCTIVE_ENDPOINTS = [
    key for key, ann in LOSS_ANNOTATIONS.items()
    if not ann["reversible"]
    and not ann["backup_exists"]
    and not ann["confirmation_required"]
]


# ---------------------------------------------------------------------------
# State Snapshot - file-level before/after comparison
# ---------------------------------------------------------------------------

@dataclass
class FileSnapshot:
    """Snapshot of a single file's content and hash."""
    path: Path
    exists: bool
    content_hash: str = ""
    content: str = ""
    size: int = 0

    @classmethod
    def capture(cls, path: Path) -> FileSnapshot:
        if not path.exists():
            return cls(path=path, exists=False)
        content = path.read_text()
        return cls(
            path=path,
            exists=True,
            content_hash=hashlib.sha256(content.encode()).hexdigest(),
            content=content,
            size=len(content),
        )


@dataclass
class StateSnapshot:
    """Snapshot of multiple files for before/after comparison."""
    files: dict[str, FileSnapshot] = field(default_factory=dict)
    timestamp: float = 0.0

    @classmethod
    def capture(cls, paths: list[Path]) -> StateSnapshot:
        import time
        snap = cls(timestamp=time.time())
        for p in paths:
            snap.files[str(p)] = FileSnapshot.capture(p)
        return snap

    def assert_no_mutation(self, other: StateSnapshot, label: str = "") -> None:
        """Assert byte-identical state between two snapshots.

        Raises AssertionError with details if any file changed.
        """
        prefix = f"[{label}] " if label else ""
        for path_str, before in self.files.items():
            after = other.files.get(path_str)
            assert after is not None, f"{prefix}File disappeared from snapshot: {path_str}"
            assert before.exists == after.exists, (
                f"{prefix}File existence changed for {path_str}: "
                f"{'existed' if before.exists else 'missing'} -> "
                f"{'exists' if after.exists else 'missing'}"
            )
            if before.exists and after.exists:
                assert before.content_hash == after.content_hash, (
                    f"{prefix}File content changed for {path_str}: "
                    f"hash {before.content_hash[:12]} -> {after.content_hash[:12]}"
                )

    def assert_no_data_loss(self, other: StateSnapshot, label: str = "") -> None:
        """Assert no key removal or content truncation (additive changes allowed).

        For YAML files, checks that no top-level keys were removed.
        For all files, checks that content was not truncated significantly.
        """
        prefix = f"[{label}] " if label else ""
        for path_str, before in self.files.items():
            after = other.files.get(path_str)
            assert after is not None, f"{prefix}File disappeared from snapshot: {path_str}"

            if before.exists and not after.exists:
                raise AssertionError(f"{prefix}File was deleted: {path_str}")

            if not before.exists:
                continue

            # Check for significant truncation (>50% size reduction)
            if after.size < before.size * 0.5 and before.size > 10:
                raise AssertionError(
                    f"{prefix}File severely truncated: {path_str} "
                    f"({before.size} -> {after.size} bytes)"
                )

            # For YAML files, check key preservation
            if path_str.endswith((".yaml", ".yml")):
                try:
                    before_data = yaml.safe_load(before.content)
                    after_data = yaml.safe_load(after.content)
                    if isinstance(before_data, dict) and isinstance(after_data, dict):
                        lost_keys = set(before_data.keys()) - set(after_data.keys())
                        if lost_keys:
                            raise AssertionError(
                                f"{prefix}YAML top-level keys lost in {path_str}: {lost_keys}"
                            )
                        # Check models block specifically
                        if "models" in before_data and "models" in after_data:
                            before_models = before_data["models"] or {}
                            after_models = after_data["models"] or {}
                            if isinstance(before_models, dict) and isinstance(after_models, dict):
                                lost_models = set(before_models.keys()) - set(after_models.keys())
                                if lost_models:
                                    raise AssertionError(
                                        f"{prefix}Models lost in {path_str}: {lost_models}"
                                    )
                except yaml.YAMLError:
                    pass  # Not valid YAML, skip semantic check

            # For .env files, check key preservation
            if path_str.endswith(".env") or "/.env" in path_str:
                before_keys = _parse_env_keys(before.content)
                after_keys = _parse_env_keys(after.content)
                lost_keys = before_keys - after_keys
                if lost_keys:
                    raise AssertionError(
                        f"{prefix}.env keys lost in {path_str}: {lost_keys}"
                    )


def _parse_env_keys(content: str) -> set[str]:
    """Extract key names from .env file content."""
    keys = set()
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, sep, _ = stripped.partition("=")
        if sep:
            keys.add(key)
    return keys


# ---------------------------------------------------------------------------
# Input Strategies for Permutation Fuzzing
# ---------------------------------------------------------------------------

class InputStrategy:
    ALL_EMPTY = "all_empty"
    ALL_FILLED = "all_filled"
    PARTIALLY_FILLED = "partially_filled"
    FILLED_THEN_CLEARED = "filled_then_cleared"
    PREEXISTING_WITH_EMPTY_SUBMIT = "preexisting_with_empty_submit"
    BOUNDARY_MIN = "boundary_min"
    BOUNDARY_MAX = "boundary_max"
    BOUNDARY_OVER = "boundary_over"
    BOUNDARY_UNDER = "boundary_under"
    TYPE_CONFUSION = "type_confusion"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_csrf_token(page) -> str:
    """Extract the CSRF token from the page's meta tag."""
    return page.locator('meta[name="csrf-token"]').get_attribute("content")


def api_post(page, base_url: str, path: str, csrf: str, payload: dict,
             content_type: str = "application/json") -> Any:
    """Send a POST request via Playwright's request context."""
    return page.request.post(
        f"{base_url}{path}",
        data=json.dumps(payload) if content_type == "application/json" else payload,
        headers={
            "X-DVAD-Token": csrf,
            "Content-Type": content_type,
        },
    )


def api_put(page, base_url: str, path: str, csrf: str, payload: dict) -> Any:
    """Send a PUT request via Playwright's request context."""
    return page.request.put(
        f"{base_url}{path}",
        data=json.dumps(payload),
        headers={
            "X-DVAD-Token": csrf,
            "Content-Type": "application/json",
        },
    )


def api_delete(page, base_url: str, path: str, csrf: str) -> Any:
    """Send a DELETE request via Playwright's request context."""
    return page.request.delete(
        f"{base_url}{path}",
        headers={"X-DVAD-Token": csrf},
    )


def load_fixture_yaml() -> str:
    """Load the E2E fixture models.yaml content."""
    fixture_path = Path(__file__).parent / "fixtures" / "models.yaml"
    return fixture_path.read_text()


def restore_config_via_api(page, base_url: str) -> None:
    """Restore the fixture config via API call."""
    page.goto(f"{base_url}/config")
    page.wait_for_load_state("networkidle")
    csrf = get_csrf_token(page)
    fixture_yaml = load_fixture_yaml()
    resp = api_post(page, base_url, "/api/config", csrf, {"yaml": fixture_yaml})
    assert resp.status == 200, f"Config restore failed: {resp.status}"
