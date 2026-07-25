"""Stage 4 — write, under every guard we have.

This is the only module that touches models.yaml, and it is written on the
assumption that doing so is dangerous. The operator runs dvad several times a
day and a corrupted or subtly-changed config is worse than no automation at all.

Guards, in order of application:

* **The review lock.** A scan never writes while a review is running. The lock
  is dvad's existing one, so the exclusion works in both directions.
* **Compare-and-swap.** The file is hashed before and after the mutation is
  built; if anything else wrote in between, the pass aborts rather than
  clobbering it. The GUI's config endpoints take no lock, so this is the only
  thing standing between a background pass and a concurrent hand edit.
* **Differential validation.** The candidate config is loaded through the real
  loader in a temp file. It must not introduce any error the current config
  does not already have — pre-existing problems are the operator's business,
  not grounds for the scan to refuse.
* **Backup, then atomic replace**, through the same helper the GUI uses.

Field derivation is deliberately dull. Only tier comes from judgement; timeout
and output caps follow fixed tables, and ``thinking`` is always seeded false so
extended reasoning is never switched on for a model the operator has not
chosen to switch it on for.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from io import StringIO
from pathlib import Path

from ruamel.yaml import YAML

from ..storage import StorageManager
from .provider_map import CLI_LANES, TRANSPORT_FIELDS
from .types import ApplyError

# Fields upstream owns. Refreshed on existing rows; never on a subscription row,
# whose zeroed costs are the whole point of prepaid accounting.
UPSTREAM_FIELDS = (
    "context_window",
    "max_out_stated",
    "cost_per_1k_input",
    "cost_per_1k_output",
)

# Tier -> (timeout seconds, output cap). Matches the shape of the operator's
# existing entries: heavy models get room, cheap fast ones get held short.
TIER_TIMEOUT = {"top": 1200, "mid": 900, "budget": 300}
TIER_MAX_OUT = {"top": 60_000, "mid": 50_000, "budget": 30_000}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _cost_per_1k(value) -> float | None:
    """models.dev prices per million tokens; models.yaml stores per thousand."""
    if value is None:
        return None
    return round(value / 1000, 10)


def build_entry(candidate, tier: str, conventions: dict) -> dict:
    """Compose a complete models.yaml entry for a newly admitted model."""
    record = candidate.record
    limit = record.get("limit") or {}
    cost = record.get("cost") or {}

    entry: dict = {}
    transport = conventions.get(candidate.provider, {})
    for field in TRANSPORT_FIELDS:
        if field in transport:
            entry[field] = transport[field]
    entry["model_id"] = candidate.model_id

    # Operator-owned fields, seeded conservatively. thinking is never inferred
    # from upstream's `reasoning` flag: that says the model *can* reason, while
    # this says dvad should *ask* it to, which is the operator's call per role.
    entry["thinking"] = False

    stated = limit.get("output")
    entry["context_window"] = limit.get("context")
    entry["cost_per_1k_input"] = _cost_per_1k(cost.get("input"))
    entry["cost_per_1k_output"] = _cost_per_1k(cost.get("output"))
    entry["timeout"] = TIER_TIMEOUT.get(tier, 900)
    if stated:
        entry["max_out_stated"] = stated
        entry["max_out_configured"] = min(stated, TIER_MAX_OUT.get(tier, 50_000))
    return {k: v for k, v in entry.items() if v is not None}


def plan_refreshes(raw_models: dict, upstream: dict[str, dict]) -> tuple[list, list]:
    """Work out which upstream-owned values drifted on rows we already have.

    Returns ``(changes, conflicts)``. A conflict is a refresh withheld because
    applying it would contradict something the operator set — currently only
    the case where upstream lowers a model's stated ceiling below the cap the
    operator configured. Those are reported, never forced.
    """
    changes: list[tuple[str, str, object, object]] = []
    conflicts: list[str] = []

    for name, entry in raw_models.items():
        if not isinstance(entry, dict):
            continue
        # L2: a subscription row's costs are zero on purpose.
        if entry.get("provider") in CLI_LANES:
            continue
        record = upstream.get(entry.get("model_id"))
        if not record:
            continue
        limit = record.get("limit") or {}
        cost = record.get("cost") or {}
        desired = {
            "context_window": limit.get("context"),
            "max_out_stated": limit.get("output"),
            "cost_per_1k_input": _cost_per_1k(cost.get("input")),
            "cost_per_1k_output": _cost_per_1k(cost.get("output")),
        }
        configured_cap = entry.get("max_out_configured")
        for field in UPSTREAM_FIELDS:
            new = desired.get(field)
            if new is None or entry.get(field) == new:
                continue
            if (
                field == "max_out_stated"
                and configured_cap is not None
                and new < configured_cap
            ):
                conflicts.append(
                    f"{name}: upstream lowered max_out_stated to {new}, below your "
                    f"max_out_configured of {configured_cap} — left unchanged"
                )
                continue
            changes.append((name, field, entry.get(field), new))
    return changes, conflicts


def _validate(candidate_text: str, current_path: Path) -> None:
    """Load the candidate through the real loader; refuse only *new* errors."""
    from ..config import load_config, validate_config_structure

    def errors_of(path: Path) -> set[str]:
        config = load_config(path)
        return {
            message
            for level, message in validate_config_structure(config)
            if level == "error"
        }

    try:
        baseline = errors_of(current_path)
    except Exception:
        # The live config is already unloadable. Nothing we add can improve
        # that, and we must not use it as a licence to write.
        raise ApplyError("current config does not load; refusing to write over it")

    # Validate in the config's own directory, not /tmp. load_config resolves the
    # sidecar .env and any relative settings.provider_plugins path against the
    # config file's parent, so validating elsewhere would exercise a different
    # configuration than the one about to be written.
    fd, name = tempfile.mkstemp(
        suffix=".yaml", prefix=".dvad-roster-", dir=str(current_path.parent)
    )
    tmp = Path(name)
    try:
        os.close(fd)
        tmp.write_text(candidate_text)
        try:
            introduced = errors_of(tmp) - baseline
        except Exception as exc:
            raise ApplyError(f"proposed config does not load: {exc}") from exc
        if introduced:
            raise ApplyError(
                "proposed config introduces errors: " + "; ".join(sorted(introduced))
            )
    finally:
        tmp.unlink(missing_ok=True)


def apply(
    config_path: Path,
    additions: dict[str, dict],
    refreshes: list[tuple[str, str, object, object]],
    *,
    storage: StorageManager | None = None,
) -> bool:
    """Write additions and refreshes to models.yaml. Returns True if it wrote.

    L1 is structural here: this function has no code path that removes a model,
    disables one, or reads ``roles:`` — let alone writes it.
    """
    if not additions and not refreshes:
        return False

    storage = storage or StorageManager(Path.home())
    if not storage.acquire_lock():
        raise ApplyError("a review is in progress; deferring to the next pass")

    try:
        before = _sha(config_path)

        yaml = YAML()
        yaml.preserve_quotes = True
        data = yaml.load(config_path.read_text())
        if "models" not in data:
            raise ApplyError("config has no models: block")

        for name, entry in additions.items():
            if name in data["models"]:
                continue  # L1: never overwrite an existing entry
            data["models"][name] = entry

        for name, field, _old, new in refreshes:
            if name in data["models"]:
                data["models"][name][field] = new

        stream = StringIO()
        yaml.dump(data, stream)
        candidate_text = stream.getvalue()

        # Every addition collided with an existing name, or every refresh was a
        # no-op. Writing anyway would roll the single .bak for nothing and
        # destroy the operator's previous backup.
        if candidate_text == config_path.read_text():
            return False

        _validate(candidate_text, config_path)

        if _sha(config_path) != before:
            raise ApplyError("config changed while the pass was running; aborting")

        shutil.copy2(config_path, config_path.with_suffix(config_path.suffix + ".bak"))
        StorageManager._atomic_write(config_path, candidate_text)
        return True
    finally:
        storage.release_lock()
