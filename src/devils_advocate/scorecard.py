"""Model scorecard — deterministic analytics over review ledger history.

Computes per-model performance statistics for every model that ever held a
role, plus head-to-head comparisons, from ``review-ledger.json`` files. No
LLM calls: like the governance engine, every number here is reproducible
from the ledgers alone.

Vocabulary used throughout:

- **accepted**: final resolution in ``auto_accepted`` / ``accepted`` /
  ``partial_accepted`` / ``overridden`` (an override means the human
  resolved it by hand — the finding earned attention either way).
- **contested**: the author answered the finding with REJECTED or PARTIAL.
- **stood ground**: on a contested finding, the originating reviewer's own
  round-2 rebuttal verdict was CHALLENGE — it refused to withdraw.
- **conceded**: own round-2 verdict was CONCUR — it accepted the author's
  pushback.
- **split decision**: a contested finding where the reviewer stood ground,
  forcing governance to escalate or the author to fold.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ACCEPT_RESOLUTIONS = {"auto_accepted", "accepted", "partial_accepted", "overridden"}
DISMISS_RESOLUTIONS = {"auto_dismissed", "dismissed", "rejected"}
CONTESTED_AUTHOR = {"REJECTED", "PARTIAL"}

# Reviewer names that are test-harness stand-ins, not models. A ledger whose
# reviewer list touches this set is synthetic and never scored.
STUB_REVIEWERS = {"reviewer1", "reviewer2", "e2e-remote", "e2e-remote-thinker", "author", ""}

_VERSION_TOKEN = re.compile(r"^v?\d+(\.\d+)*$")
_NOISE_TOKENS = {"preview", "latest", "exp"}


def model_family(name: str) -> str:
    """Collapse a versioned model name to its lineage (claude-opus-4-6 -> claude-opus).

    Used for display grouping only — all stats stay per exact model+version.
    """
    tokens = [
        t for t in name.split("-")
        if t and not _VERSION_TOKEN.match(t) and t.lower() not in _NOISE_TOKENS
    ]
    return "-".join(tokens) if tokens else name


def _is_test_project(project: str) -> bool:
    """Same convention as the dashboard filter: e2e/test projects are synthetic."""
    proj = (project or "").lower()
    return "e2e" in proj or "test" in proj


def load_scorable_ledgers(reviews_dir: Path, include_test: bool = False) -> list[dict]:
    """Load ledgers that represent real adversarial reviews.

    Kept: real model reviewers, at least one governance-resolved point.
    Dropped: stub reviewers, spec mode (no governance pass), unreadable files,
    and — unless ``include_test`` — e2e/test projects. Projects that merely
    *contain* "test" survive if their reviewers are real models and points
    resolved (early real reviews were run under test-ish names).
    """
    ledgers = []
    for ledger_path in sorted(reviews_dir.glob("*/review-ledger.json")):
        try:
            ledger = json.loads(ledger_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if ledger.get("mode") == "spec":
            continue
        reviewers = set(ledger.get("reviewer_models") or [])
        if not reviewers or reviewers & STUB_REVIEWERS:
            continue
        if not include_test and "e2e" in (ledger.get("project") or "").lower():
            continue
        resolved = [
            p for p in ledger.get("points") or []
            if p.get("final_resolution") not in (None, "pending")
        ]
        if not resolved:
            continue
        ledger["_resolved_points"] = resolved
        ledgers.append(ledger)
    return ledgers


def _own_rebuttal_verdict(point: dict) -> str | None:
    """The originating reviewer's own round-2 verdict on its finding's group."""
    for rb in point.get("rebuttals") or []:
        if rb.get("reviewer") == point.get("reviewer"):
            return rb.get("verdict")
    return None


def _reviewer_cost(ledger: dict, model: str) -> float:
    """Cost attributed to *model* for its reviewer seat in this review."""
    role_costs = (ledger.get("cost") or {}).get("role_costs") or {}
    total = 0.0
    for i, name in enumerate(ledger.get("reviewer_models") or [], 1):
        if name == model:
            total += role_costs.get(f"reviewer_{i}", 0.0)
    return total


def _blank_reviewer() -> dict:
    return {
        "reviews": 0, "findings": 0,
        "accepted": 0, "dismissed": 0, "escalated": 0, "overridden": 0,
        "critical": 0, "high": 0,
        "consensus": 0,
        "contested": 0, "stood_ground": 0, "conceded": 0, "vindicated": 0,
        "cost_usd": 0.0,
        "monthly": {},
    }


def _finalize_reviewer(model: str, s: dict) -> dict:
    findings = s["findings"]
    stances = s["stood_ground"] + s["conceded"]
    out = {
        "model": model,
        "family": model_family(model),
        **{k: v for k, v in s.items() if k != "monthly"},
        "findings_per_review": round(findings / s["reviews"], 1) if s["reviews"] else 0.0,
        "hit_rate": round(s["accepted"] / findings, 3) if findings else None,
        "escalation_rate": round(s["escalated"] / findings, 3) if findings else None,
        "consensus_rate": round(s["consensus"] / findings, 3) if findings else None,
        "conviction_rate": round(s["stood_ground"] / stances, 3) if stances else None,
        "cost_usd": round(s["cost_usd"], 4),
        "cost_per_accepted": (
            round(s["cost_usd"] / s["accepted"], 4)
            if s["accepted"] and s["cost_usd"] else None
        ),
        "monthly": {
            m: v for m, v in sorted(s["monthly"].items())
        },
    }
    return out


def compute_scorecard(reviews_dir: Path, include_test: bool = False) -> dict:
    """Full scorecard: per-model stats for every role, head-to-head, split decisions."""
    ledgers = load_scorable_ledgers(reviews_dir, include_test=include_test)

    reviewers: dict[str, dict] = {}
    authors: dict[str, dict] = {}
    service: dict[tuple[str, str], dict] = {}  # (role, model) -> stats
    pairs: dict[tuple[str, str], dict] = {}    # reviewer pair (sorted) -> stats
    versus: dict[tuple[str, str], dict] = {}   # (reviewer, author) -> stats
    split_decisions: list[dict] = []
    months: set[str] = set()

    for ledger in ledgers:
        month = (ledger.get("timestamp") or "")[:7]
        if month:
            months.add(month)
        author = ledger.get("author_model") or ""
        reviewer_models = ledger.get("reviewer_models") or []
        points = ledger["_resolved_points"]
        role_costs = (ledger.get("cost") or {}).get("role_costs") or {}

        # ---- reviewer seats -------------------------------------------------
        # (group_id, reviewer) pairs already counted for contest/stance stats,
        # so a multi-point group doesn't double-count a single stand.
        seen_stances: set[tuple[str, str]] = set()

        for model in set(reviewer_models):
            r = reviewers.setdefault(model, _blank_reviewer())
            r["reviews"] += 1
            r["cost_usd"] += _reviewer_cost(ledger, model)

        for p in points:
            model = p.get("reviewer") or ""
            if model not in reviewers:
                # A point attributed to a model not in reviewer_models
                # (shouldn't happen, but ledgers are history — keep it).
                reviewers.setdefault(model, _blank_reviewer())
            r = reviewers[model]
            final = p.get("final_resolution")
            r["findings"] += 1
            mstats = r["monthly"].setdefault(month, {"findings": 0, "accepted": 0})
            mstats["findings"] += 1
            if final in ACCEPT_RESOLUTIONS:
                r["accepted"] += 1
                mstats["accepted"] += 1
            if final in DISMISS_RESOLUTIONS:
                r["dismissed"] += 1
            if final == "escalated":
                r["escalated"] += 1
            if final == "overridden":
                r["overridden"] += 1
            sev = (p.get("severity") or "").lower()
            if sev == "critical":
                r["critical"] += 1
            elif sev == "high":
                r["high"] += 1
            if len(p.get("source_reviewers") or []) >= 2:
                r["consensus"] += 1

            # Conviction: only score each (group, reviewer) stand once.
            stance_key = (p.get("group_id") or p.get("point_id") or "", model)
            if p.get("author_resolution") in CONTESTED_AUTHOR and stance_key not in seen_stances:
                seen_stances.add(stance_key)
                r["contested"] += 1
                verdict = _own_rebuttal_verdict(p)
                if verdict == "CHALLENGE":
                    r["stood_ground"] += 1
                    if final in ACCEPT_RESOLUTIONS:
                        r["vindicated"] += 1
                    split_decisions.append({
                        "reviewer": model,
                        "author": author,
                        "project": ledger.get("project") or "",
                        "mode": ledger.get("mode") or "",
                        "date": (ledger.get("timestamp") or "")[:10],
                        "severity": p.get("severity") or "",
                        "category": p.get("category") or "",
                        "description": (p.get("description") or "")[:280],
                        "author_resolution": p.get("author_resolution"),
                        "author_final_resolution": p.get("author_final_resolution"),
                        "final_resolution": final,
                        "review_id": ledger.get("review_id") or "",
                    })
                elif verdict == "CONCUR":
                    r["conceded"] += 1

            # Reviewer-vs-author ledger
            if author:
                v = versus.setdefault((model, author), {
                    "findings": 0, "accepted": 0, "author_pushback": 0,
                    "escalated": 0, "stood_ground": 0, "review_ids": set(),
                })
                v["review_ids"].add(ledger.get("review_id"))
                v["findings"] += 1
                if final in ACCEPT_RESOLUTIONS:
                    v["accepted"] += 1
                if p.get("author_resolution") in CONTESTED_AUTHOR:
                    v["author_pushback"] += 1
                    if _own_rebuttal_verdict(p) == "CHALLENGE":
                        v["stood_ground"] += 1
                if final == "escalated":
                    v["escalated"] += 1

        # ---- reviewer head-to-head (same review, same artifact) -------------
        distinct = sorted(set(reviewer_models))
        for i, a in enumerate(distinct):
            for b in distinct[i + 1:]:
                pair = pairs.setdefault((a, b), {
                    "reviews": 0,
                    "a_findings": 0, "b_findings": 0,
                    "a_accepted": 0, "b_accepted": 0,
                    "both_found": 0, "a_solo": 0, "b_solo": 0,
                })
                pair["reviews"] += 1
                groups: dict[str, set] = {}
                for p in points:
                    gid = p.get("group_id") or p.get("point_id") or ""
                    groups.setdefault(gid, set()).update(p.get("source_reviewers") or [p.get("reviewer")])
                for p in points:
                    final = p.get("final_resolution")
                    if p.get("reviewer") == a:
                        pair["a_findings"] += 1
                        pair["a_accepted"] += final in ACCEPT_RESOLUTIONS
                    elif p.get("reviewer") == b:
                        pair["b_findings"] += 1
                        pair["b_accepted"] += final in ACCEPT_RESOLUTIONS
                for gid, sources in groups.items():
                    if a in sources and b in sources:
                        pair["both_found"] += 1
                    elif a in sources:
                        pair["a_solo"] += 1
                    elif b in sources:
                        pair["b_solo"] += 1

        # ---- author seat -----------------------------------------------------
        if author:
            a = authors.setdefault(author, {
                "reviews": 0, "responded": 0,
                "accepted": 0, "partial": 0, "rejected": 0,
                "contests": 0, "folded": 0, "held": 0,
                "cost_usd": 0.0,
            })
            a["reviews"] += 1
            a["cost_usd"] += role_costs.get("author", 0.0)
            seen_groups: set[str] = set()
            for p in points:
                gid = p.get("group_id") or p.get("point_id") or ""
                if gid in seen_groups:
                    continue
                seen_groups.add(gid)
                res = p.get("author_resolution")
                if res in ("ACCEPTED", "PARTIAL", "REJECTED"):
                    a["responded"] += 1
                    a[res.lower() if res != "ACCEPTED" else "accepted"] += 1
                if res in CONTESTED_AUTHOR:
                    a["contests"] += 1
                    final_pos = p.get("author_final_resolution")
                    if final_pos == "ACCEPTED":
                        a["folded"] += 1
                    elif final_pos == "MAINTAINED":
                        a["held"] += 1

        # ---- service roles ---------------------------------------------------
        if ledger.get("dedup_model"):
            s = service.setdefault(("dedup", ledger["dedup_model"]), {"reviews": 0, "cost_usd": 0.0})
            s["reviews"] += 1
            s["cost_usd"] += role_costs.get("dedup", 0.0)
        assignments = ledger.get("role_assignments") or {}
        for role_key, cost_key in (
            ("normalization", "normalization"),
            ("revision", "revision"),
            ("integration", "integration"),
        ):
            name = assignments.get(role_key)
            if name:
                s = service.setdefault((role_key, name), {"reviews": 0, "cost_usd": 0.0})
                s["reviews"] += 1
                s["cost_usd"] += role_costs.get(cost_key, 0.0)
        # Per-call cost entries (persisted from CostTracker as of 0.9.42)
        for entry in (ledger.get("cost") or {}).get("entries") or []:
            role = entry.get("role") or ""
            if role in ("normalization", "revision", "integration") and not assignments.get(role):
                s = service.setdefault((role, entry.get("model") or ""), {"reviews": 0, "cost_usd": 0.0})
                s["cost_usd"] += entry.get("cost_usd", 0.0)
                s.setdefault("_review_ids", set()).add(ledger.get("review_id"))

    for s in service.values():
        ids = s.pop("_review_ids", None)
        if ids:
            s["reviews"] += len(ids)

    reviewer_rows = sorted(
        (_finalize_reviewer(m, s) for m, s in reviewers.items()),
        key=lambda r: (-(r["accepted"]), r["model"]),
    )

    author_rows = []
    for model, s in sorted(authors.items()):
        contests_settled = s["folded"] + s["held"]
        author_rows.append({
            "model": model,
            "family": model_family(model),
            **{k: (round(v, 4) if k == "cost_usd" else v) for k, v in s.items()},
            "accept_rate": round(s["accepted"] / s["responded"], 3) if s["responded"] else None,
            "hold_rate": round(s["held"] / contests_settled, 3) if contests_settled else None,
        })
    author_rows.sort(key=lambda a: -a["reviews"])

    service_rows = [
        {"role": role, "model": model, "reviews": s["reviews"], "cost_usd": round(s["cost_usd"], 4)}
        for (role, model), s in sorted(service.items())
    ]

    pair_rows = []
    for (a, b), s in sorted(pairs.items()):
        pair_rows.append({
            "model_a": a, "model_b": b,
            **s,
            "a_hit_rate": round(s["a_accepted"] / s["a_findings"], 3) if s["a_findings"] else None,
            "b_hit_rate": round(s["b_accepted"] / s["b_findings"], 3) if s["b_findings"] else None,
        })
    pair_rows.sort(key=lambda p: -p["reviews"])

    versus_rows = []
    for (reviewer, author), s in sorted(versus.items()):
        versus_rows.append({
            "reviewer": reviewer, "author": author,
            "reviews": len(s["review_ids"]),
            "findings": s["findings"],
            "accepted": s["accepted"],
            "author_pushback": s["author_pushback"],
            "stood_ground": s["stood_ground"],
            "escalated": s["escalated"],
            "accept_rate": round(s["accepted"] / s["findings"], 3) if s["findings"] else None,
        })
    versus_rows.sort(key=lambda v: -v["findings"])

    split_decisions.sort(key=lambda d: d["date"], reverse=True)

    return {
        "review_count": len(ledgers),
        "months": sorted(months),
        "reviewers": reviewer_rows,
        "authors": author_rows,
        "service_roles": service_rows,
        "head_to_head": pair_rows,
        "reviewer_vs_author": versus_rows,
        "split_decisions": split_decisions,
    }


def matchup_sentence(pair: dict) -> str:
    """One-sentence plain-English summary of a reviewer head-to-head row."""
    a, b = pair["model_a"], pair["model_b"]
    n = pair["reviews"]
    times = "once" if n == 1 else f"{n} times"
    a_hit = f"{pair['a_hit_rate']:.0%}" if pair["a_hit_rate"] is not None else "n/a"
    b_hit = f"{pair['b_hit_rate']:.0%}" if pair["b_hit_rate"] is not None else "n/a"
    return (
        f"{a} and {b} reviewed the same artifact {times}: "
        f"{a} raised {pair['a_findings']} findings ({a_hit} accepted) to "
        f"{b}'s {pair['b_findings']} ({b_hit} accepted); "
        f"they independently converged on {pair['both_found']} of the same issues, "
        f"while {a} found {pair['a_solo']} nothing else caught and {b} found {pair['b_solo']}."
    )


def versus_sentence(row: dict) -> str:
    """One-sentence summary of a reviewer-vs-author matchup."""
    n = row["reviews"]
    times = "once" if n == 1 else f"{n} reviews"
    rate = f"{row['accept_rate']:.0%}" if row["accept_rate"] is not None else "n/a"
    parts = [
        f"Across {times} against {row['author']} as author, {row['reviewer']} "
        f"raised {row['findings']} findings and landed {row['accepted']} ({rate})"
    ]
    if row["author_pushback"]:
        parts.append(
            f"; the author pushed back on {row['author_pushback']}, and "
            f"{row['reviewer']} refused to withdraw {row['stood_ground']} of those"
        )
    if row["escalated"]:
        parts.append(f"; {row['escalated']} went to human escalation")
    return "".join(parts) + "."
