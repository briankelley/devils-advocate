"""The daily pass — orchestration only.

Runs unattended and stays silent. On the overwhelming majority of days the
upstream catalogue is byte-identical to yesterday's, the digest matches, and
the pass exits having read one HTTP response and written nothing at all. When
something has changed it gathers findings into the notice queue, which the GUI
surfaces the next time the operator opens a browser — never a mail, never a
desktop alert, never a prompt.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml as pyyaml

from . import reason as reasoner
from . import state as st
from .apply import apply, build_entry, plan_refreshes
from .fetch import fetch
from .gate import gate
from .probe import VERDICT_SERVES, check
from .provider_map import SUPPORTED_PROVIDERS, learn_conventions
from .types import ReasonerError, RosterError

log = logging.getLogger("devils_advocate.roster")

# Which CLI lane serves which upstream provider, when subscriptions are on.
LANE_FOR_PROVIDER = {"anthropic": "claude-cli", "openai": "codex-cli"}


@dataclass
class ScanReport:
    changed: bool = False
    added: list[str] = field(default_factory=list)
    refreshed: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    skipped: str | None = None
    wrote: bool = False

    def summary(self) -> str:
        if self.skipped:
            return self.skipped
        if not self.changed:
            return "upstream catalogue unchanged"
        bits = []
        if self.added:
            bits.append(f"{len(self.added)} added")
        if self.refreshed:
            bits.append(f"{len(self.refreshed)} refreshed")
        if self.conflicts:
            bits.append(f"{len(self.conflicts)} withheld")
        return ", ".join(bits) or "catalogue moved, nothing actionable"


def _upstream_records(payload: dict) -> dict[str, dict]:
    index: dict[str, dict] = {}
    for provider_id in SUPPORTED_PROVIDERS:
        for model_id, record in ((payload.get(provider_id) or {}).get("models") or {}).items():
            index.setdefault(model_id, record)
    return index


def _sub_entry(api_name: str, api_entry: dict, lane: str) -> dict:
    """Build a subscription twin for a freshly added API model.

    Shaped to satisfy the loader's CLI-lane totality check: a lane entry must
    name an enabled, non-CLI failover twin, which is the API entry added in the
    same pass. Costs stay zero — the equivalent price rides on ``api_twin``.
    """
    return {
        "provider": lane,
        "model_id": api_entry["model_id"],
        "api_key_env": "",
        "thinking": False,
        "context_window": api_entry.get("context_window"),
        "cost_per_1k_input": 0,
        "cost_per_1k_output": 0,
        "timeout": api_entry.get("timeout", 1200),
        "max_out_stated": api_entry.get("max_out_stated"),
        "max_out_configured": api_entry.get("max_out_configured"),
        "failover_model": api_name,
        "extra": {"api_twin": api_name},
    }


def run_scan(
    config_path: Path,
    *,
    today: date | None = None,
    force: bool = False,
    dry_run: bool = False,
    use_reasoner: bool = True,
    data_dir: Path | None = None,
) -> ScanReport:
    """Execute one pass. Never raises on ordinary failure — reports instead."""
    report = ScanReport()
    today = today or date.today()
    scan_state = st.load_state(data_dir)

    try:
        payload, digest = fetch()
    except RosterError as exc:
        log.warning("roster scan: %s", exc)
        report.skipped = f"upstream unavailable: {exc}"
        return report

    if digest == scan_state.get("last_digest") and not force:
        scan_state["last_scan"] = st.now_iso()
        if not dry_run:
            st.save_state(scan_state, data_dir)
        return report

    report.changed = True
    raw = pyyaml.safe_load(config_path.read_text()) or {}
    raw_models = raw.get("models") or {}
    roles = raw.get("roles") or {}
    subs_on = bool((raw.get("settings") or {}).get("subscription_backend"))

    result = gate(payload, raw_models, roles, today=today)
    conventions = learn_conventions(payload, raw_models)
    upstream = _upstream_records(payload)

    admitted: list[dict] = []
    if use_reasoner and result.candidates:
        try:
            admitted = reasoner.run(result, raw_models)
        except ReasonerError as exc:
            # A judgement failure is not a config failure. Refreshes and
            # notices still land; additions wait for a healthier pass.
            log.warning("roster scan: reasoner unavailable (%s)", exc)
            report.notices.append(f"reasoner unavailable: {exc}")

    by_id = {c.model_id: c for c in result.candidates}
    additions: dict[str, dict] = {}
    for item in admitted:
        candidate = by_id.get(item["model_id"])
        if candidate is None:
            continue
        name = candidate.model_id
        if name in raw_models:
            continue  # L1
        entry = build_entry(candidate, item["tier"], conventions)
        additions[name] = entry

        lane = LANE_FOR_PROVIDER.get(candidate.provider) if subs_on else None
        if lane and f"{name}-sub" not in raw_models:
            if check(lane, candidate.model_id, scan_state) == VERDICT_SERVES:
                additions[f"{name}-sub"] = _sub_entry(name, entry, lane)

    refreshes, conflicts = plan_refreshes(raw_models, upstream)
    report.added = sorted(additions)
    report.refreshed = sorted({name for name, *_ in refreshes})
    report.conflicts = conflicts

    if not dry_run:
        try:
            report.wrote = apply(config_path, additions, refreshes)
        except RosterError as exc:
            log.warning("roster scan: %s", exc)
            report.skipped = str(exc)
            return report

        _publish_notices(result, report, data_dir)
        scan_state["last_digest"] = digest
        scan_state["last_scan"] = st.now_iso()
        st.save_state(scan_state, data_dir)

    return report


def _publish_notices(result, report: ScanReport, data_dir: Path | None) -> None:
    """Fold this pass's findings into the queue the GUI reads."""
    notices = st.load_notices(data_dir)
    live: set[str] = set()

    if report.added:
        nid = "roster:additions"
        live.add(nid)
        st.upsert(
            notices,
            st.Notice(
                id=nid,
                level=st.LEVEL_INFO,
                title=f"{len(report.added)} model(s) added to the roster",
                body=", ".join(report.added)
                + ". They are inert until you assign one to a role.",
            ),
        )
    else:
        # Keep an existing additions notice alive; it describes earlier passes.
        live |= {n.id for n in notices if n.id == "roster:additions"}

    for item in result.health:
        nid = f"roster:{item.condition.split()[0]}:{item.name}"
        live.add(nid)
        critical = item.role_assigned
        st.upsert(
            notices,
            st.Notice(
                id=nid,
                level=st.LEVEL_CRITICAL if critical else st.LEVEL_WARN,
                title=f"{item.name} is {item.condition}",
                body=(
                    f"{item.name} is assigned to a role. It will keep working "
                    "until the provider withdraws it, but pick a replacement "
                    "before that happens."
                    if critical
                    else f"{item.name} is {item.condition} and no role uses it. "
                    "Nothing is broken; remove it when convenient."
                ),
            ),
        )

    for shortfall in result.actionable_shortfalls:
        nid = f"roster:shortfall:{shortfall.provider}:{shortfall.kind}"
        live.add(nid)
        st.upsert(
            notices,
            st.Notice(
                id=nid,
                level=st.LEVEL_INFO,
                title=f"{shortfall.provider}: thin {shortfall.kind} coverage",
                body=shortfall.describe(),
            ),
        )

    for conflict in report.conflicts:
        name = conflict.split(":")[0]
        nid = f"roster:conflict:{name}"
        live.add(nid)
        st.upsert(
            notices,
            st.Notice(
                id=nid,
                level=st.LEVEL_WARN,
                title=f"{name}: upstream value withheld",
                body=conflict,
            ),
        )

    st.save_notices(st.prune(notices, live), data_dir)
