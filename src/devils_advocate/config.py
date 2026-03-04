"""XDG-compliant config resolution, loading, and validation."""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from .types import ConfigError, ModelConfig

# Default timeout matches ModelConfig dataclass default
DEFAULT_TIMEOUT = 120


def _load_dotenv(config_path: Path) -> None:
    """Load a .env file from the same directory as models.yaml into os.environ.

    Only sets variables that are not already present in the environment,
    so explicit shell exports or systemd Environment= still take precedence.
    """
    env_file = config_path.parent / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if sep and key not in os.environ:
            os.environ[key] = value


def init_config() -> tuple[str, Path]:
    """Create ~/.config/devils-advocate/ with models.yaml and .env.example.

    Copies the shipped example files from the package's examples/ directory.
    Returns ("exists", path) if already present, ("created", path) if newly created.
    """
    import importlib.resources

    config_dir = Path.home() / ".config" / "devils-advocate"
    config_file = config_dir / "models.yaml"

    if config_file.exists():
        return "exists", config_file

    config_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(config_dir, 0o700)

    # Copy shipped models.yaml.example → models.yaml
    examples_pkg = importlib.resources.files("devils_advocate.examples")
    example_yaml = examples_pkg / "models.yaml.example"
    try:
        config_file.write_text(example_yaml.read_text())
    except Exception:
        # Fallback: minimal config if package examples not found
        config_file.write_text(
            "# Devil's Advocate configuration\n"
            "# See https://github.com/briankelley/devils-advocate for full reference.\n"
            "# Run: dvad config --init after installing from source for the complete example.\n\n"
            "models: {}\nroles: {}\n"
        )
    os.chmod(config_file, 0o600)

    # Copy .env.example if available
    env_dest = config_dir / ".env.example"
    if not env_dest.exists():
        try:
            example_env = examples_pkg / ".env.example"
            env_dest.write_text(example_env.read_text())
            os.chmod(env_dest, 0o600)
        except Exception:
            pass

    return "created", config_file


def find_config(explicit: Path | None = None) -> Path:
    """Locate models.yaml using a priority search order.

    Search order:
        1. *explicit* path (from --config CLI flag)
        2. ./models.yaml (project-local)
        3. $DVAD_HOME/models.yaml (env var override)
        4. ~/.config/devils-advocate/models.yaml (XDG default)
    """
    if explicit is not None:
        p = Path(explicit)
        if p.exists():
            return p
        raise ConfigError(f"Explicit config not found: {p}")

    # Project-local
    local = Path("models.yaml")
    if local.exists():
        return local.resolve()

    # $DVAD_HOME
    dvad_home = os.environ.get("DVAD_HOME")
    if dvad_home:
        p = Path(dvad_home) / "models.yaml"
        if p.exists():
            return p

    # XDG default
    xdg = Path.home() / ".config" / "devils-advocate" / "models.yaml"
    if xdg.exists():
        return xdg

    searched = ["./models.yaml"]
    if dvad_home:
        searched.append(f"$DVAD_HOME/models.yaml ({dvad_home}/models.yaml)")
    searched.append(str(xdg))
    raise ConfigError(
        f"No models.yaml found. Searched: {', '.join(searched)}"
    )


def load_config(path: Path | None = None) -> dict:
    """Load and parse models.yaml into a working config dict.

    If *path* is None, uses :func:`find_config` to locate the file.
    """
    config_path = path or find_config()
    _load_dotenv(config_path)
    with open(config_path) as f:
        raw = yaml.safe_load(f)
    if not raw or "models" not in raw:
        raise ConfigError(f"Invalid config: 'models' key missing in {config_path}")

    # Parse all model specs (roles assigned separately via 'roles' block)
    all_models: dict[str, ModelConfig] = {}
    for name, cfg in raw["models"].items():
        all_models[name] = ModelConfig(
            name=name,
            provider=cfg.get("provider", "openai"),
            model_id=cfg.get("model_id", name),
            api_key_env=cfg.get("api_key_env", ""),
            api_base=cfg.get("api_base", ""),
            roles=set(),
            deduplication=False,
            integration_reviewer=False,
            context_window=cfg.get("context_window"),
            cost_per_1k_input=cfg.get("cost_per_1k_input"),
            cost_per_1k_output=cfg.get("cost_per_1k_output"),
            timeout=cfg.get("timeout", DEFAULT_TIMEOUT),
            max_out_stated=cfg.get("max_out_stated"),
            max_out_configured=cfg.get("max_out_configured"),
            enabled=cfg.get("enabled", True),
            use_completion_tokens=cfg.get("use_completion_tokens", False),
            use_responses_api=cfg.get("use_responses_api", False),
            thinking=cfg.get("thinking", False),
        )

    # Assign roles from the top-level 'roles' block
    roles_block = raw.get("roles") or {}

    active_names: set[str] = set()

    def _resolve(key: str, name: str) -> None:
        if name not in all_models:
            raise ConfigError(f"roles.{key} references unknown model '{name}'")
        if not all_models[name].enabled:
            raise ConfigError(f"roles.{key} references disabled model '{name}'")
        active_names.add(name)

    author_name = roles_block.get("author")
    if author_name:
        _resolve("author", author_name)
        all_models[author_name].roles.add("author")

    for reviewer_name in roles_block.get("reviewers", []):
        _resolve("reviewers", reviewer_name)
        all_models[reviewer_name].roles.add("reviewer")

    dedup_name = roles_block.get("deduplication")
    if dedup_name:
        _resolve("deduplication", dedup_name)
        all_models[dedup_name].deduplication = True

    integ_name = roles_block.get("integration_reviewer")
    if integ_name:
        _resolve("integration_reviewer", integ_name)
        all_models[integ_name].integration_reviewer = True

    # Normalization role: optional, defaults to dedup model in get_models_by_role
    norm_name = roles_block.get("normalization")
    if norm_name:
        _resolve("normalization", norm_name)
        all_models[norm_name].roles.add("normalization")

    # Revision role: optional, defaults to author model in get_models_by_role
    revision_name = roles_block.get("revision")
    if revision_name:
        _resolve("revision", revision_name)
        all_models[revision_name].roles.add("revision")

    # Only active models (referenced in roles) go into the working set
    models = {name: all_models[name] for name in all_models if name in active_names}

    return {
        "models": models,
        "all_models": all_models,
        "config_path": str(config_path),
        "reviewer_order": roles_block.get("reviewers", []),
    }


def validate_config_structure(config: dict) -> list[tuple[str, str]]:
    """Structural validation - checks that don't depend on review mode.

    Returns list of (level, message) tuples.
    level is 'error' (fatal) or 'warn' (continue).
    """
    issues: list[tuple[str, str]] = []
    all_models = config.get("all_models", config.get("models", {}))
    models = config["models"]

    # Empty models dict - nothing to work with
    if not all_models:
        issues.append(("error", "No models defined"))
        return issues

    # No roles assigned - models exist but none are active
    if not models:
        issues.append(("error", "No roles assigned - add a roles block with at least an author and reviewers"))
        return issues

    # Author-dedup collision (warn at save time, error at review-start time)
    authors = [m for m in models.values() if "author" in m.roles]
    dedup = [m for m in models.values() if m.deduplication]
    if authors and dedup:
        if any(d.name == authors[0].name for d in dedup):
            issues.append(("warn", "Deduplication model should NOT be the author"))

    # Per-model checks
    for name, m in models.items():
        if not m.api_key:
            issues.append(("error", f"{name}: env var {m.api_key_env} is empty or unset"))
        if m.context_window is None:
            issues.append(("warn", f"{name}: context_window not set - pre-flight checks skipped"))
        if m.cost_per_1k_input is None or m.cost_per_1k_output is None:
            issues.append(("warn", f"{name}: cost not set - cost guardrails skipped for this model"))

    return issues


# Deprecated: use validate_config_structure + validate_review_readiness
validate_config = validate_config_structure


def validate_review_readiness(config: dict, mode: str) -> list[tuple[str, str]]:
    """Check whether config has the roles needed for the given review mode.

    Returns list of (level, message) tuples.
    level is 'error' (fatal) or 'warn' (advisory).
    """
    issues: list[tuple[str, str]] = []
    models = config["models"]

    authors = [m for m in models.values() if "author" in m.roles]
    reviewers = [m for m in models.values() if "reviewer" in m.roles]
    dedup = [m for m in models.values() if m.deduplication]
    integ = [m for m in models.values() if m.integration_reviewer]
    revision = [m for m in models.values() if "revision" in m.roles]
    # Revision falls back to author in get_models_by_role, so check that too
    has_revision = bool(revision) or bool(authors)

    # Author-dedup collision (applies to any mode that uses both)
    if authors and dedup:
        if any(d.name == authors[0].name for d in dedup):
            issues.append(("error", "Deduplication model must NOT be the author. Assign different models to these roles on the Config page."))

    if mode in ("plan", "code"):
        if len(authors) < 1:
            issues.append(("error", f"{mode.title()} mode requires an Author role. Assign a model to the Author role on the Config page."))
        if len(reviewers) < 1:
            issues.append(("error", f"{mode.title()} mode requires at least 1 Reviewer. Assign a model to a Reviewer role on the Config page."))
        elif len(reviewers) == 1:
            issues.append(("warn", f"{mode.title()} review with 1 reviewer - adversarial coverage is significantly reduced."))
        if len(reviewers) >= 2 and len(dedup) == 0:
            issues.append(("error", "Dedup role is required when 2 reviewers are assigned. Assign a model to the Dedup role on the Config page."))
        if not has_revision:
            issues.append(("error", f"{mode.title()} mode requires a Revision role (or Author as fallback)."))

    elif mode == "spec":
        if len(reviewers) < 1:
            issues.append(("error", "Spec mode requires at least 1 Reviewer. Assign a model to a Reviewer role on the Config page."))
        elif len(reviewers) == 1:
            issues.append(("warn", "Spec review with 1 reviewer - adversarial coverage is significantly reduced."))
        if len(reviewers) >= 2 and len(dedup) == 0:
            issues.append(("error", "Dedup role is required when 2 reviewers are assigned. Assign a model to the Dedup role on the Config page."))
        if not has_revision:
            issues.append(("error", "Spec mode requires a Revision role (or Author as fallback)."))

    elif mode == "integration":
        if len(integ) < 1:
            issues.append(("error", "Integration mode requires an Integration Reviewer. Assign a model to the Integration Reviewer role on the Config page."))
        if not has_revision:
            issues.append(("error", "Integration mode requires a Revision role (or Author as fallback)."))

    return issues


def get_config_health(config: dict) -> tuple[bool, str]:
    """Check config for errors and return a human-readable summary.

    Returns (has_errors, summary_string).  When has_errors is False the
    summary is empty.
    """
    issues = validate_config_structure(config)
    errors = [msg for level, msg in issues if level == "error"]
    if not errors:
        return False, ""
    if len(errors) == 1:
        return True, errors[0]
    return True, f"{len(errors)} configuration errors found"


def get_models_by_role(config: dict) -> dict:
    """Extract models organized by their assigned role.

    Returns dict with keys: author, reviewers, dedup, integration, normalization.
    The normalization role defaults to the dedup model when not explicitly configured.
    """
    models = config["models"]
    dedup_model = next((m for m in models.values() if m.deduplication), None)

    # Normalization: explicit role or fallback to dedup
    norm_model = next(
        (m for m in models.values() if "normalization" in m.roles),
        None,
    )
    if norm_model is None:
        norm_model = dedup_model

    # Revision: explicit role or fallback to author
    revision_model = next(
        (m for m in models.values() if "revision" in m.roles),
        None,
    )
    if revision_model is None:
        revision_model = next((m for m in models.values() if "author" in m.roles), None)

    return {
        "author": next((m for m in models.values() if "author" in m.roles), None),
        "reviewers": (
            [models[name] for name in config.get("reviewer_order", []) if name in models]
            or [m for m in models.values() if "reviewer" in m.roles]
        ),
        "dedup": dedup_model,
        "integration": next((m for m in models.values() if m.integration_reviewer), None),
        "normalization": norm_model,
        "revision": revision_model,
    }


MODE_ROLES: dict[str, list[dict[str, object]]] = {
    "spec": [
        {"key": "reviewers", "label": "Reviewer 1", "required": True},
        {"key": "reviewers", "label": "Reviewer 2", "required": False},
        {"key": "dedup", "label": "Dedup", "required": "conditional"},
        {"key": "normalization", "label": "Normalization", "required": False},
        {"key": "revision", "label": "Revision", "required": True},
    ],
    "plan": [
        {"key": "author", "label": "Author", "required": True},
        {"key": "reviewers", "label": "Reviewer 1", "required": True},
        {"key": "reviewers", "label": "Reviewer 2", "required": False},
        {"key": "dedup", "label": "Dedup", "required": "conditional"},
        {"key": "normalization", "label": "Normalization", "required": False},
        {"key": "revision", "label": "Revision", "required": True},
    ],
    "code": [
        {"key": "author", "label": "Author", "required": True},
        {"key": "reviewers", "label": "Reviewer 1", "required": True},
        {"key": "reviewers", "label": "Reviewer 2", "required": False},
        {"key": "dedup", "label": "Dedup", "required": "conditional"},
        {"key": "normalization", "label": "Normalization", "required": False},
        {"key": "revision", "label": "Revision", "required": True},
    ],
    "integration": [
        {"key": "integration", "label": "Integration Reviewer", "required": True},
        {"key": "revision", "label": "Revision", "required": True},
    ],
}


def get_mode_readiness(config: dict) -> dict[str, dict]:
    """Return per-mode readiness state for dashboard display.

    Returns dict keyed by mode name, each containing:
      - ready: bool (no errors)
      - errors: list[str]
      - warnings: list[str]
      - roles: list[dict] with keys: label, required, assigned (bool), model (str|None)
    """
    roles = get_models_by_role(config)
    result = {}

    for mode, role_defs in MODE_ROLES.items():
        issues = validate_review_readiness(config, mode)
        errors = [msg for level, msg in issues if level == "error"]
        warnings = [msg for level, msg in issues if level == "warn"]

        role_entries = []
        reviewer_idx = 0
        for rd in role_defs:
            key = rd["key"]
            if key == "reviewers":
                reviewer_list = roles.get("reviewers", [])
                model = reviewer_list[reviewer_idx] if reviewer_idx < len(reviewer_list) else None
                reviewer_idx += 1
            else:
                model = roles.get(key)

            role_entries.append({
                "label": rd["label"],
                "required": rd["required"],
                "assigned": model is not None,
                "model": model.name if model else None,
            })

        result[mode] = {
            "ready": len(errors) == 0,
            "errors": errors,
            "warnings": warnings,
            "roles": role_entries,
        }

    return result
