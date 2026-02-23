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
    """Create ~/.config/devils-advocate/ with an example models.yaml.

    Returns ("exists", path) if already present, ("created", path) if newly created.
    """
    config_dir = Path.home() / ".config" / "devils-advocate"
    config_file = config_dir / "models.yaml"

    if config_file.exists():
        return "exists", config_file

    config_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(config_dir, 0o700)

    example = """\
# Devil's Advocate configuration
# API keys must be set via environment variables; do not put secrets in this file.

models:
  # Author model -- generates responses and revisions
  claude-sonnet:
    provider: anthropic
    model_id: claude-sonnet-4-20250514
    api_key_env: ANTHROPIC_API_KEY
    context_window: 200000
    cost_per_1k_input: 0.003
    cost_per_1k_output: 0.015
    timeout: 180

  # Reviewer 1
  gpt-4o:
    provider: openai
    model_id: gpt-4o
    api_key_env: OPENAI_API_KEY
    api_base: https://api.openai.com/v1
    context_window: 128000
    cost_per_1k_input: 0.005
    cost_per_1k_output: 0.015

  # Reviewer 2
  gemini-pro:
    provider: openai
    model_id: gemini-2.0-flash
    api_key_env: GOOGLE_API_KEY
    api_base: https://generativelanguage.googleapis.com/v1beta/openai
    context_window: 1000000
    cost_per_1k_input: 0.0001
    cost_per_1k_output: 0.0004

  # Dedup / normalization model
  claude-haiku:
    provider: anthropic
    model_id: claude-3-5-haiku-20241022
    api_key_env: ANTHROPIC_API_KEY
    context_window: 200000
    cost_per_1k_input: 0.0008
    cost_per_1k_output: 0.004

roles:
  author: claude-sonnet
  reviewers:
    - gpt-4o
    - gemini-pro
  deduplication: claude-haiku
  integration_reviewer: gpt-4o
  # normalization: claude-haiku  # optional; defaults to deduplication model
  # revision: claude-sonnet  # optional; defaults to author model
"""

    config_file.write_text(example)
    os.chmod(config_file, 0o600)

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
            use_completion_tokens=cfg.get("use_completion_tokens", False),
            thinking=cfg.get("thinking", False),
        )

    # Assign roles from the top-level 'roles' block
    roles_block = raw.get("roles")
    if not roles_block:
        raise ConfigError(f"Config missing 'roles' block in {config_path}")

    active_names: set[str] = set()

    def _resolve(key: str, name: str) -> None:
        if name not in all_models:
            raise ConfigError(f"roles.{key} references unknown model '{name}'")
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
    models = {name: all_models[name] for name in active_names}

    return {"models": models, "all_models": all_models, "config_path": str(config_path)}


def validate_config(config: dict) -> list[tuple[str, str]]:
    """Validate a loaded config. Returns list of (level, message) tuples.

    level is 'error' (fatal) or 'warn' (continue).
    """
    issues: list[tuple[str, str]] = []
    models = config["models"]

    authors = [m for m in models.values() if "author" in m.roles]
    reviewers = [m for m in models.values() if "reviewer" in m.roles]
    dedup = [m for m in models.values() if m.deduplication]
    integ = [m for m in models.values() if m.integration_reviewer]

    if len(authors) != 1:
        issues.append(("error", f"Need exactly 1 author, found {len(authors)}"))
    if len(reviewers) < 2:
        issues.append(("error", f"Need at least 2 reviewers, found {len(reviewers)}"))
    if len(integ) != 1:
        issues.append(("error", f"Need exactly 1 integration_reviewer, found {len(integ)}"))
    if len(dedup) == 0:
        issues.append(("error", "Need at least 1 model with deduplication: true"))
    if authors and dedup:
        if any(d.name == authors[0].name for d in dedup):
            issues.append(("error", "Deduplication model must NOT be the author"))

    for name, m in models.items():
        if not m.api_key:
            issues.append(("error", f"{name}: env var {m.api_key_env} is empty or unset"))
        if m.context_window is None:
            issues.append(("warn", f"{name}: context_window not set — pre-flight checks skipped"))
        if m.cost_per_1k_input is None or m.cost_per_1k_output is None:
            issues.append(("warn", f"{name}: cost not set — cost guardrails skipped for this model"))

    return issues


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
        "reviewers": [m for m in models.values() if "reviewer" in m.roles],
        "dedup": dedup_model,
        "integration": next((m for m in models.values() if m.integration_reviewer), None),
        "normalization": norm_model,
        "revision": revision_model,
    }
