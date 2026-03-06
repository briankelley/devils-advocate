"""Paranoid test infrastructure: Write Registry, Loss Annotations, StateSnapshot.

Central registry of every state-mutating operation in the dvad GUI.
Every paranoid test file imports from here. A new write path added to the
application without a registry entry causes a test failure in
test_paranoid_inventory.py.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from io import StringIO
from pathlib import Path
from typing import Any

import yaml
from ruamel.yaml import YAML

# ── Write Registry ──────────────────────────────────────────────────────────

WRITE_ENDPOINTS: dict[str, dict[str, Any]] = {
    # ── Config mutations ─────────────────────────────────────────────
    "POST /api/config (raw yaml)": {
        "method": "POST",
        "path": "/api/config",
        "writes_to": "models.yaml config file on disk",
        "destroys": "entire previous config file content",
        "requires_csrf": True,
        "empty_payload": {"yaml": ""},
        "valid_payload": {
            "yaml": (
                "models:\n"
                "  test-model:\n"
                "    provider: openai\n"
                "    model_id: gpt-4\n"
                "    api_key_env: TEST_KEY\n"
                "    api_base: https://api.openai.com/v1\n"
                "    context_window: 128000\n"
                "    cost_per_1k_input: 0.01\n"
                "    cost_per_1k_output: 0.03\n"
                "roles:\n"
                "  author: test-model\n"
                "  reviewers:\n"
                "    - test-model\n"
            )
        },
    },
    "POST /api/config (structured roles)": {
        "method": "POST",
        "path": "/api/config",
        "writes_to": "models.yaml roles + thinking blocks",
        "destroys": "previous role assignments and thinking flags",
        "requires_csrf": True,
        "empty_payload": {"roles": {}, "thinking": {}},
        "valid_payload": {
            "roles": {
                "author": "test-model",
                "reviewer1": "test-model",
                "reviewer2": None,
                "dedup": None,
                "normalization": None,
                "revision": None,
                "integration": None,
            },
            "thinking": {},
        },
    },
    "POST /api/config/validate": {
        "method": "POST",
        "path": "/api/config/validate",
        "writes_to": "temporary file (cleaned up)",
        "destroys": "nothing (read-only validation)",
        "requires_csrf": True,
        "empty_payload": {"yaml": ""},
        "valid_payload": {"yaml": "models:\n  m: {}\nroles:\n  author: m\n"},
    },
    "POST /api/config/model-timeout": {
        "method": "POST",
        "path": "/api/config/model-timeout",
        "writes_to": "models.yaml per-model timeout field",
        "destroys": "previous timeout value for the named model",
        "requires_csrf": True,
        "empty_payload": {"model_name": "", "timeout": None},
        "valid_payload": {"model_name": "test-model", "timeout": 120},
    },
    "POST /api/config/model-max-tokens": {
        "method": "POST",
        "path": "/api/config/model-max-tokens",
        "writes_to": "models.yaml per-model max_out_configured field",
        "destroys": "previous max_out_configured value",
        "requires_csrf": True,
        "empty_payload": {"model_name": "", "max_out_configured": None},
        "valid_payload": {"model_name": "test-model", "max_out_configured": 4096},
    },
    "POST /api/config/settings-toggle": {
        "method": "POST",
        "path": "/api/config/settings-toggle",
        "writes_to": "models.yaml settings block boolean flag",
        "destroys": "previous value of the toggled setting",
        "requires_csrf": True,
        "empty_payload": {"key": "", "value": False},
        "valid_payload": {"key": "live_testing", "value": False},
    },
    # ── Env file mutations ────────────────────────────────────────────
    "PUT /api/config/env/{name}": {
        "method": "PUT",
        "path": "/api/config/env/{env_name}",
        "writes_to": ".env file + os.environ",
        "destroys": "previous value of the env var (if existing)",
        "requires_csrf": True,
        "empty_payload": {"value": ""},
        "valid_payload": {"value": "sk-test-key-1234567890"},
    },
    "DELETE /api/config/env/{name}": {
        "method": "DELETE",
        "path": "/api/config/env/{env_name}",
        "writes_to": ".env file + os.environ",
        "destroys": "the entire env var key+value (irrecoverable without backup)",
        "requires_csrf": True,
        "requires_confirm_header": True,
        "empty_payload": {},
        "valid_payload": {},
    },
    "POST /api/config/env": {
        "method": "POST",
        "path": "/api/config/env",
        "writes_to": ".env file + os.environ (batch)",
        "destroys": "previous values of all updated keys; empty values DELETE keys",
        "requires_csrf": True,
        "empty_payload": {"env_vars": {}},
        "valid_payload": {"env_vars": {"TEST_KEY": "sk-abc123"}},
    },
    # ── Review mutations ──────────────────────────────────────────────
    "POST /api/review/start": {
        "method": "POST",
        "path": "/api/review/start",
        "writes_to": "review directory, ledger JSON, log files, temp uploads",
        "destroys": "nothing (creates new review artifacts)",
        "requires_csrf": True,
        "empty_payload": {"mode": "", "project": ""},
        "valid_payload": {
            "mode": "plan",
            "project": "test-project",
            "input_paths": '["dummy.md"]',
        },
    },
    "POST /api/review/{id}/cancel": {
        "method": "POST",
        "path": "/api/review/{review_id}/cancel",
        "writes_to": "review state (in-memory), stub ledger on disk",
        "destroys": "partial in-flight review results",
        "requires_csrf": True,
        "empty_payload": {},
        "valid_payload": {},
    },
    "POST /api/review/{id}/override": {
        "method": "POST",
        "path": "/api/review/{review_id}/override",
        "writes_to": "review-ledger.json (point resolution + overrides array)",
        "destroys": "previous resolution of the targeted point/group",
        "requires_csrf": True,
        "empty_payload": {"group_id": "", "resolution": ""},
        "valid_payload": {"group_id": "grp_01", "resolution": "overridden"},
    },
    "POST /api/review/{id}/revise": {
        "method": "POST",
        "path": "/api/review/{review_id}/revise",
        "writes_to": "revised artifact file in review directory",
        "destroys": "previous revised artifact (overwritten)",
        "requires_csrf": True,
        "empty_payload": {},
        "valid_payload": {},
    },
    "POST /api/review/{id}/revise-full": {
        "method": "POST",
        "path": "/api/review/{review_id}/revise-full",
        "writes_to": "revised full-file artifact in review directory",
        "destroys": "previous revised artifact (overwritten)",
        "requires_csrf": True,
        "empty_payload": {},
        "valid_payload": {},
    },
}


# ── Loss Annotations ────────────────────────────────────────────────────────

LOSS_ANNOTATIONS: dict[str, dict[str, Any]] = {
    "POST /api/config (raw yaml)": {
        "on_empty_input": "HTTPException 400 (YAML parse error or missing models key)",
        "on_all_empty": "HTTPException 400 (missing models key)",
        "reversible": True,
        "backup_exists": True,  # .bak file created before overwrite
        "confirmation_required": True,  # frontend shows confirm dialog
        "precondition": "YAML must parse, must contain 'models' and 'roles' keys, must pass validation",
    },
    "POST /api/config (structured roles)": {
        "on_empty_input": "Writes empty roles block, potentially clearing all role assignments",
        "on_all_empty": "All roles set to None/empty, reviewers list emptied, thinking flags cleared",
        "reversible": True,
        "backup_exists": False,  # NOTE: _mutate_yaml_config does NOT create .bak
        "confirmation_required": True,  # frontend shows confirm dialog
        "precondition": "Config file must be locatable",
        "FINDING": (
            "Empty roles payload accepted without rejection. "
            "Sending {roles: {}, thinking: {}} clears all role assignments. "
            "Unlike raw YAML save, structured save via _mutate_yaml_config does NOT create a .bak backup."
        ),
    },
    "POST /api/config/validate": {
        "on_empty_input": "Returns valid=False with issues list",
        "on_all_empty": "Returns valid=False (missing models key)",
        "reversible": True,
        "backup_exists": True,  # N/A - read-only
        "confirmation_required": False,
        "precondition": "None (validation only)",
    },
    "POST /api/config/model-timeout": {
        "on_empty_input": "HTTPException 400 (model_name required, timeout validation)",
        "on_all_empty": "HTTPException 400",
        "reversible": True,
        "backup_exists": False,  # _mutate_yaml_config does NOT create .bak
        "confirmation_required": False,  # inline edit, no dialog
        "precondition": "model_name must exist in config, timeout must be 10-7200",
    },
    "POST /api/config/model-max-tokens": {
        "on_empty_input": "HTTPException 400 (model_name required)",
        "on_all_empty": "HTTPException 400",
        "reversible": True,
        "backup_exists": False,
        "confirmation_required": False,
        "precondition": "model_name must exist, max_out_configured must be 1-1000000 or clear=true",
    },
    "POST /api/config/settings-toggle": {
        "on_empty_input": "HTTPException 400 (unknown setting key)",
        "on_all_empty": "HTTPException 400",
        "reversible": True,
        "backup_exists": False,
        "confirmation_required": False,
        "precondition": "key must be in {live_testing}",
    },
    "PUT /api/config/env/{name}": {
        "on_empty_input": "HTTPException 400 (value cannot be empty)",
        "on_all_empty": "HTTPException 400",
        "reversible": False,
        "backup_exists": False,  # No backup before single-key write
        "confirmation_required": False,
        "precondition": "env_name must be in allowed set, value must be non-empty",
        "FINDING": (
            "No backup of .env before single-key write. "
            "If the user saves a bad key, the old value is gone. "
            "Contrast with DELETE which does create a .bak."
        ),
    },
    "DELETE /api/config/env/{name}": {
        "on_empty_input": "N/A (no body required)",
        "on_all_empty": "N/A",
        "reversible": False,
        "backup_exists": True,  # .bak created before deletion
        "confirmation_required": True,  # X-Confirm-Destructive header + frontend dialog
        "precondition": "env_name must be in allowed set",
    },
    "POST /api/config/env": {
        "on_empty_input": "HTTPException 400 (no environment variables provided)",
        "on_all_empty": "HTTPException 400",
        "reversible": False,
        "backup_exists": True,  # .bak created only when deletions present
        "confirmation_required": True,  # X-Confirm-Destructive for empty values
        "precondition": "All keys must be in allowed set",
        "FINDING": (
            "Backup only created when deletions (empty values) are present. "
            "A batch update that overwrites existing keys with new values "
            "does NOT create a backup of the .env file."
        ),
    },
    "POST /api/review/start": {
        "on_empty_input": "HTTPException 400 (project required, mode validation)",
        "on_all_empty": "HTTPException 400",
        "reversible": False,
        "backup_exists": False,
        "confirmation_required": True,  # frontend interstitial + confirm dialog
        "precondition": "Config loaded and valid, mode-specific file requirements met",
    },
    "POST /api/review/{id}/cancel": {
        "on_empty_input": "404 if no running review with that ID",
        "on_all_empty": "404",
        "reversible": False,
        "backup_exists": False,
        "confirmation_required": True,  # frontend confirm dialog
        "precondition": "Review must be currently running",
    },
    "POST /api/review/{id}/override": {
        "on_empty_input": "HTTPException 400 (group_id required, invalid resolution)",
        "on_all_empty": "HTTPException 400",
        "reversible": False,
        "backup_exists": False,
        "confirmation_required": False,  # NO confirmation dialog for overrides
        "precondition": "review_id must exist, group_id must exist in ledger, resolution in valid set",
        "FINDING": (
            "Override is irreversible, has no backup, and has no confirmation dialog. "
            "A mis-click on the override button permanently alters the review ledger. "
            "The overrides array provides an audit trail but does not provide undo."
        ),
    },
    "POST /api/review/{id}/revise": {
        "on_empty_input": "HTTPException 404 (review not found) or 400 (no original_content)",
        "on_all_empty": "HTTPException 404 or 400",
        "reversible": True,  # can regenerate
        "backup_exists": False,
        "confirmation_required": False,
        "precondition": "Review must exist, original_content.txt must exist, config must be valid",
    },
    "POST /api/review/{id}/revise-full": {
        "on_empty_input": "HTTPException 404 (review not found) or 400",
        "on_all_empty": "HTTPException 404 or 400",
        "reversible": True,
        "backup_exists": False,
        "confirmation_required": False,
        "precondition": "Review must exist, mode must be 'code', original_content.txt must exist",
    },
}


# ── StateSnapshot ───────────────────────────────────────────────────────────

class StateSnapshot:
    """Capture and compare file-system state before/after a write operation.

    Usage:
        snap = StateSnapshot(target_dir)
        snap.capture('before')
        ... do something ...
        snap.capture('after')
        assert snap.is_identical()
        # or: assert snap.no_data_loss()
    """

    def __init__(self, *paths: Path):
        self.paths = list(paths)
        self.snapshots: dict[str, dict[str, str]] = {}

    def capture(self, label: str) -> None:
        """Hash all files under the tracked paths."""
        hashes: dict[str, str] = {}
        for root in self.paths:
            if root.is_file():
                hashes[str(root)] = self._hash_file(root)
            elif root.is_dir():
                for f in sorted(root.rglob("*")):
                    if f.is_file():
                        hashes[str(f)] = self._hash_file(f)
        self.snapshots[label] = hashes

    @staticmethod
    def _hash_file(path: Path) -> str:
        h = hashlib.sha256()
        try:
            h.update(path.read_bytes())
        except OSError:
            h.update(b"<unreadable>")
        return h.hexdigest()

    def is_identical(self, before: str = "before", after: str = "after") -> bool:
        """True if snapshots are byte-identical."""
        return self.snapshots.get(before) == self.snapshots.get(after)

    def no_data_loss(self, before: str = "before", after: str = "after") -> tuple[bool, list[str]]:
        """Check for data loss: keys removed or content changed in destructive way.

        Returns (ok, list_of_issues). Allows additive changes (new files).
        """
        before_snap = self.snapshots.get(before, {})
        after_snap = self.snapshots.get(after, {})
        issues = []

        # Files that existed before but are missing after
        for path in before_snap:
            if path not in after_snap:
                issues.append(f"FILE DELETED: {path}")

        # Files whose content changed
        for path in before_snap:
            if path in after_snap and before_snap[path] != after_snap[path]:
                issues.append(f"CONTENT CHANGED: {path}")

        return len(issues) == 0, issues

    def diff_report(self, before: str = "before", after: str = "after") -> str:
        """Generate a human-readable diff report."""
        before_snap = self.snapshots.get(before, {})
        after_snap = self.snapshots.get(after, {})
        lines = []

        removed = set(before_snap) - set(after_snap)
        added = set(after_snap) - set(before_snap)
        changed = {
            p for p in set(before_snap) & set(after_snap)
            if before_snap[p] != after_snap[p]
        }

        if removed:
            lines.append("REMOVED FILES:")
            for p in sorted(removed):
                lines.append(f"  - {p}")
        if added:
            lines.append("ADDED FILES:")
            for p in sorted(added):
                lines.append(f"  + {p}")
        if changed:
            lines.append("CHANGED FILES:")
            for p in sorted(changed):
                lines.append(f"  ~ {p}")
        if not lines:
            lines.append("NO CHANGES")

        return "\n".join(lines)


# ── Helpers for test fixtures ───────────────────────────────────────────────

MINIMAL_VALID_YAML = """\
models:
  test-model:
    provider: openai
    model_id: gpt-4
    api_key_env: TEST_KEY
    api_base: https://api.openai.com/v1
    context_window: 128000
    cost_per_1k_input: 0.01
    cost_per_1k_output: 0.03
  reviewer-model:
    provider: openai
    model_id: gpt-4o
    api_key_env: TEST_KEY
    api_base: https://api.openai.com/v1
    context_window: 128000
    cost_per_1k_input: 0.005
    cost_per_1k_output: 0.015
  integ-model:
    provider: openai
    model_id: gpt-4
    api_key_env: TEST_KEY
    api_base: https://api.openai.com/v1
    context_window: 128000
    cost_per_1k_input: 0.01
    cost_per_1k_output: 0.03
roles:
  author: test-model
  reviewers:
    - reviewer-model
    - test-model
  deduplication: reviewer-model
  normalization: reviewer-model
  revision: test-model
  integration_reviewer: integ-model
"""

SAMPLE_ENV_CONTENT = """\
# API keys for testing
TEST_KEY=sk-test-1234567890abcdef
OTHER_KEY=sk-other-9876543210fedcba
"""

SAMPLE_LEDGER = {
    "review_id": "test-review-001",
    "mode": "plan",
    "project": "test-project",
    "input_file": "plan.md",
    "timestamp": "2026-03-01T12:00:00Z",
    "result": "success",
    "author_model": "test-model",
    "reviewer_models": ["reviewer-model"],
    "dedup_model": "reviewer-model",
    "points": [
        {
            "point_id": "pt-001",
            "group_id": "grp-001",
            "final_resolution": "escalated",
            "severity": "high",
            "category": "correctness",
            "concern": "Test concern",
            "description": "A test finding",
            "recommendation": "Fix this",
            "source_reviewers": ["reviewer-model"],
        },
        {
            "point_id": "pt-002",
            "group_id": "grp-002",
            "final_resolution": "auto_accepted",
            "severity": "medium",
            "category": "security",
            "concern": "Another concern",
            "description": "Another finding",
            "recommendation": "Fix that too",
            "source_reviewers": ["reviewer-model"],
        },
    ],
    "summary": {
        "total_points": 2,
        "total_groups": 2,
        "escalated": 1,
    },
    "cost": {
        "total_usd": 0.05,
        "breakdown": {"test-model": 0.03, "reviewer-model": 0.02},
        "role_costs": {"author": 0.02, "reviewer_1": 0.02, "dedup": 0.01},
    },
}


def make_temp_config_dir(
    yaml_content: str = MINIMAL_VALID_YAML,
    env_content: str | None = SAMPLE_ENV_CONTENT,
) -> Path:
    """Create a temp directory with models.yaml and optional .env.

    CRITICAL: Tests MUST use this instead of pointing at the real config.
    Returns the temp directory path. Caller is responsible for cleanup.
    """
    tmpdir = Path(tempfile.mkdtemp(prefix="dvad-paranoid-"))
    config_file = tmpdir / "models.yaml"
    config_file.write_text(yaml_content)
    if env_content is not None:
        env_file = tmpdir / ".env"
        env_file.write_text(env_content)
    return tmpdir


def make_temp_review_dir(
    review_id: str = "test-review-001",
    ledger: dict | None = None,
    original_content: str | None = "Original plan content here.\n",
) -> tuple[Path, Path]:
    """Create a temp review directory with ledger and original content.

    Returns (data_dir, review_dir) where review_dir = data_dir/reviews/{review_id}.
    Caller is responsible for cleanup of data_dir.
    """
    data_dir = Path(tempfile.mkdtemp(prefix="dvad-paranoid-data-"))
    reviews_dir = data_dir / "reviews"
    reviews_dir.mkdir(parents=True)
    review_dir = reviews_dir / review_id
    review_dir.mkdir(parents=True)
    (review_dir / "round1").mkdir(exist_ok=True)
    (review_dir / "round2").mkdir(exist_ok=True)
    (review_dir / "revision").mkdir(exist_ok=True)

    actual_ledger = ledger or SAMPLE_LEDGER
    (review_dir / "review-ledger.json").write_text(
        json.dumps(actual_ledger, indent=2)
    )

    if original_content:
        (review_dir / "original_content.txt").write_text(original_content)

    # Create logs directory
    logs_dir = data_dir / "logs"
    logs_dir.mkdir(parents=True)

    return data_dir, review_dir


# ── All mutating route patterns for inventory validation ─────────────────

MUTATING_ROUTE_PATTERNS: list[tuple[str, str]] = [
    # (method, path_pattern)
    ("POST", "/api/config"),
    ("POST", "/api/config/validate"),
    ("POST", "/api/config/model-timeout"),
    ("POST", "/api/config/model-max-tokens"),
    ("POST", "/api/config/settings-toggle"),
    ("PUT", "/api/config/env/{env_name}"),
    ("DELETE", "/api/config/env/{env_name}"),
    ("POST", "/api/config/env"),
    ("POST", "/api/review/start"),
    ("POST", "/api/review/{review_id}/cancel"),
    ("POST", "/api/review/{review_id}/override"),
    ("POST", "/api/review/{review_id}/revise"),
    ("POST", "/api/review/{review_id}/revise-full"),
]
