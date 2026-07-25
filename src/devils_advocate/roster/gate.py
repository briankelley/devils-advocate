"""Stage 2 — the deterministic candidate gate.

Reduces a models.dev payload (172 providers, several thousand entries) to a
short list worth a judgement call, using only mechanical rules. No model is
consulted here; nothing is written.

Enforces L1: entries already in the operator's config are reported when they
fail a rule, but never proposed for removal. The gate answers *what is worth
adding*, and the caller may not read its output as a whitelist. A live roster
routinely contains models this gate would not admit today — a model assigned to
a role can be several generations deep in its family, and evicting it would
make the config fail to load.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from .provider_map import SUPPORTED_PROVIDERS, upstream_index

# ─── Tunables ──────────────────────────────────────────────────────────────

CONTEXT_FLOOR = 100_000
WINDOW_DAYS = 365
FAMILY_CAP = 3

# Products that are not review-shaped, matched by id. Blunt by design: this is
# the rule most likely to cut something it shouldn't, so every hit is reported
# in the rejection list rather than dropped silently.
JUNK_PATTERN = re.compile(
    r"embedding|image|realtime|live|tts|audio|veo|imagen|lyria|robotics|"
    r"deep-research|moderation|whisper|sora|guard|translate|chat-latest"
)

# Tool, vision and harness variants of a base model.
VARIANT_PATTERN = re.compile(r"customtools|computer-use|multi-agent|\dv-|\dv$|-vision")

DATED_ALIAS = re.compile(r"^(.*)-(\d{8})$")

# Top-tier band: blended cost within this fraction of the provider's dearest
# survivor. Budget band: within this multiple of its cheapest priced survivor.
TOP_TIER_FRACTION = 0.5
BUDGET_MULTIPLE = 1.5


# ─── Result types ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Candidate:
    provider: str
    family: str
    model_id: str
    record: dict

    @property
    def blended_cost(self) -> float:
        return _blended(self.record)


@dataclass(frozen=True)
class Rejection:
    provider: str
    model_id: str
    reason: str
    in_config: bool


@dataclass(frozen=True)
class HealthItem:
    """A model already in the roster that upstream says something about."""

    name: str
    condition: str
    role_assigned: bool


@dataclass(frozen=True)
class Shortfall:
    """A provider that misses the coverage rule.

    ``fixable`` is False when the provider simply does not offer enough models
    in that band — a fact, not a task. Those are recorded and never surfaced.
    """

    provider: str
    kind: str
    have: int
    want: int
    available: list[str]
    fixable: bool

    def describe(self) -> str:
        if not self.fixable:
            return (
                f"{self.provider}: {self.have} of {self.want} {self.kind} "
                "— provider offers no more"
            )
        return (
            f"{self.provider}: {self.have} of {self.want} {self.kind} "
            f"(available: {', '.join(self.available)})"
        )


@dataclass(frozen=True)
class GateResult:
    candidates: list[Candidate] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)
    health: list[HealthItem] = field(default_factory=list)
    shortfalls: list[Shortfall] = field(default_factory=list)

    @property
    def actionable_shortfalls(self) -> list[Shortfall]:
        """Only the gaps the operator could actually close."""
        return [s for s in self.shortfalls if s.fixable]

    def candidates_for(self, provider: str) -> list[Candidate]:
        return [c for c in self.candidates if c.provider == provider]


# ─── Helpers ───────────────────────────────────────────────────────────────


def age_days(raw: str | None, today: date) -> int | None:
    """Days since release. ``None`` when upstream gives nothing usable.

    ``release_date`` is *not* schema-guaranteed to be ``YYYY-MM-DD``; the wild
    contains ``YYYY-MM`` (moonshotai/kimi-k2.5 is ``'2026-01'``). Partial dates
    widen to the earliest instant they could mean. A value that cannot be
    parsed at all returns None, and callers must not treat that as grounds for
    rejecting a model — an unparseable date is an upstream defect, not a fact
    about the model.
    """
    if not raw:
        return None
    parts = str(raw).split("-")[:3]
    try:
        year = int(parts[0])
        month = int(parts[1]) if len(parts) > 1 else 1
        day = int(parts[2]) if len(parts) > 2 else 1
        return (today - date(year, month, day)).days
    except (ValueError, TypeError):
        return None


def _blended(record: dict) -> float:
    cost = record.get("cost") or {}
    return ((cost.get("input") or 0) + (cost.get("output") or 0)) / 2


def _reject_reason(model_id: str, record: dict, floating: set[str], today: date) -> str | None:
    """Return why this model is not a candidate, or None if it survives."""
    modalities = record.get("modalities") or {}
    cost = record.get("cost") or {}
    limit = record.get("limit") or {}

    if JUNK_PATTERN.search(model_id):
        return "non-text product"
    if VARIANT_PATTERN.search(model_id):
        return "variant of a base model"
    if (modalities.get("output") or []) != ["text"]:
        return "does not emit text-only"
    if "text" not in (modalities.get("input") or []):
        return "does not accept text input"
    if cost.get("input") is None or cost.get("output") is None:
        return "no published price"
    # Free tiers are rate-limited, slow, and small-context. Operator ruling
    # 2026-07-25: never propose one. Local models are unaffected — they are
    # not sourced from upstream at all.
    if cost.get("input") == 0 and cost.get("output") == 0:
        return "free tier (rate-limited, degraded)"
    if record.get("status") == "deprecated":
        return "DEPRECATED upstream"
    if record.get("status") == "alpha":
        return "alpha status"
    if (limit.get("context") or 0) < CONTEXT_FLOOR:
        return f"context {limit.get('context')} below {CONTEXT_FLOOR} floor"
    dated = DATED_ALIAS.match(model_id)
    if dated and dated.group(1) in floating:
        return "dated alias of a floating id"

    age = age_days(record.get("release_date"), today)
    if age is None:
        if record.get("release_date"):
            return f"unparseable release_date {record.get('release_date')!r}"
        # No date at all is not disqualifying; let the family cap sort it out.
        return None
    if age > WINDOW_DAYS:
        return f"released {age}d ago (>{WINDOW_DAYS}d window)"
    return None


def _collapse_latest(
    members: list[tuple[str, dict]],
) -> tuple[list[tuple[str, dict]], list[tuple[str, str]]]:
    """Split floating ``-latest`` ids that duplicate a concrete sibling exactly.

    Returns ``(survivors, [(dropped_id, twin_id), ...])``. Upstream publishes
    both ``gemini-flash-latest`` and the concrete ``gemini-3.5-flash`` it
    currently points at; admitting both would put the same model in the picker
    twice under two names.
    """
    survivors = list(members)
    collapsed: list[tuple[str, str]] = []
    for model_id, record in list(survivors):
        if not model_id.endswith("-latest"):
            continue
        twin = next(
            (
                other_id
                for other_id, other in survivors
                if other_id != model_id
                and other.get("cost") == record.get("cost")
                and other.get("limit") == record.get("limit")
            ),
            None,
        )
        if twin:
            survivors.remove((model_id, record))
            collapsed.append((model_id, twin))
    return survivors, collapsed


# ─── Constraint evaluation ─────────────────────────────────────────────────


def _shortfalls(
    provider: str, survivors: list[tuple[str, dict]], configured_ids: set[str]
) -> list[Shortfall]:
    """Report where the roster misses the two-top-tier / one-budget rule.

    Bands are relative to what the provider actually offers, not to an absolute
    dollar line. xAI's cheapest model costs ten times Google's; an absolute
    threshold would declare a permanent, unfixable budget shortfall there and
    nag about it forever.

    A shortfall with nothing available to fix it is recorded but marked
    unfixable, and the notice layer stays quiet about it — the operator cannot
    act on a model the provider does not sell.
    """
    priced = [(mid, _blended(rec)) for mid, rec in survivors if _blended(rec) > 0]
    if not priced:
        return []
    dearest = max(cost for _, cost in priced)
    cheapest = min(cost for _, cost in priced)

    top_band = {mid for mid, cost in priced if cost >= dearest * TOP_TIER_FRACTION}
    budget_band = {mid for mid, cost in priced if cost <= cheapest * BUDGET_MULTIPLE}

    out: list[Shortfall] = []
    for kind, band, want in (("top-tier", top_band, 2), ("budget", budget_band, 1)):
        have = len(band & configured_ids)
        if have >= want:
            continue
        available = sorted(band - configured_ids)
        out.append(
            Shortfall(
                provider=provider,
                kind=kind,
                have=have,
                want=want,
                available=available,
                fixable=len(available) >= (want - have),
            )
        )
    return out


# ─── Health of the existing roster ─────────────────────────────────────────


def _health(payload: dict, raw_models: dict, assigned: set[str]) -> list[HealthItem]:
    """What upstream says about models the operator already runs. Report only."""
    index: dict[str, dict] = {}
    for provider_id in SUPPORTED_PROVIDERS:
        for model_id, record in ((payload.get(provider_id) or {}).get("models") or {}).items():
            index.setdefault(model_id, record)

    items: list[HealthItem] = []
    for name, entry in raw_models.items():
        if not isinstance(entry, dict):
            continue
        record = index.get(entry.get("model_id"))
        if record is None:
            # Local models are expected to be absent from a hosted-model index.
            if entry.get("provider") not in ("local",):
                items.append(HealthItem(name, "absent from models.dev", name in assigned))
            continue
        if record.get("status") == "deprecated":
            items.append(HealthItem(name, "deprecated upstream", name in assigned))
    return items


# ─── Entry point ───────────────────────────────────────────────────────────


def gate(
    payload: dict,
    raw_models: dict,
    roles: dict | None = None,
    *,
    today: date,
    family_cap: int = FAMILY_CAP,
) -> GateResult:
    """Reduce an upstream payload to candidates worth judging.

    *raw_models* and *roles* come straight from models.yaml. *today* is injected
    so a scan is reproducible and testable.
    """
    configured_ids = {
        entry.get("model_id")
        for entry in raw_models.values()
        if isinstance(entry, dict)
    }
    assigned: set[str] = set()
    for value in (roles or {}).values():
        assigned |= set(value) if isinstance(value, list) else {value}

    rejections: list[Rejection] = []
    surviving: dict[tuple[str, str], list[tuple[str, dict]]] = {}

    for provider_id in SUPPORTED_PROVIDERS:
        models = (payload.get(provider_id) or {}).get("models") or {}
        floating = set(models)
        for model_id, record in models.items():
            reason = _reject_reason(model_id, record, floating, today)
            if reason:
                rejections.append(
                    Rejection(provider_id, model_id, reason, model_id in configured_ids)
                )
                continue
            family = record.get("family") or model_id
            surviving.setdefault((provider_id, family), []).append((model_id, record))

    candidates: list[Candidate] = []
    per_provider: dict[str, list[tuple[str, dict]]] = {}

    for (provider_id, family), members in surviving.items():
        members, collapsed = _collapse_latest(members)
        for dropped_id, twin_id in collapsed:
            rejections.append(
                Rejection(
                    provider_id,
                    dropped_id,
                    f"floating alias of {twin_id} (identical price and limits)",
                    dropped_id in configured_ids,
                )
            )

        members.sort(key=lambda pair: pair[1].get("release_date") or "", reverse=True)
        for rank, (model_id, record) in enumerate(members, start=1):
            if rank <= family_cap:
                candidates.append(Candidate(provider_id, family, model_id, record))
                per_provider.setdefault(provider_id, []).append((model_id, record))
            else:
                rejections.append(
                    Rejection(
                        provider_id,
                        model_id,
                        f'rank {rank} in family "{family}" (cap {family_cap})',
                        model_id in configured_ids,
                    )
                )

    shortfalls: list[str] = []
    for provider_id, survivors in sorted(per_provider.items()):
        shortfalls.extend(_shortfalls(provider_id, survivors, configured_ids))

    # L1: a candidate the operator already runs is not a candidate.
    novel = [c for c in candidates if c.model_id not in configured_ids]

    return GateResult(
        candidates=sorted(novel, key=lambda c: (c.provider, -c.blended_cost)),
        rejections=rejections,
        health=_health(payload, raw_models, assigned),
        shortfalls=shortfalls,
    )
