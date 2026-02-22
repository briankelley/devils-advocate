"""Report and ledger generators for review results."""

from __future__ import annotations

from dataclasses import asdict

from .types import (
    GovernanceDecision,
    Resolution,
    ReviewResult,
)


# ─── Report Generator ───────────────────────────────────────────────────────


def generate_report(result: ReviewResult) -> str:
    """Generate human-readable dvad-report.md."""
    if result.mode == "spec":
        return _generate_spec_report(result)

    lines = ["# Devil's Advocate Review Report", ""]
    lines.append(f"**Mode:** {result.mode.title()} Review")
    lines.append(f"**Input:** `{result.input_file}`")
    lines.append(f"**Project:** {result.project}")
    lines.append(f"**Date:** {result.timestamp}")
    lines.append(f"**Review ID:** `{result.review_id}`")
    lines.append(f"**Author Model:** {result.author_model}")
    lines.append(f"**Reviewer Models:** {', '.join(result.reviewer_models)}")
    lines.append(f"**Dedup Model:** {result.dedup_model}")
    lines.append(f"**Total Cost:** ${result.cost.total_usd:.4f}")
    lines.append("")

    # Summary table
    s = result.summary
    lines.append("## Summary")
    lines.append("")
    lines.append("| Resolution | Count |")
    lines.append("|---|---|")
    for key in ["accepted", "auto_accepted", "rejected", "auto_dismissed", "escalated", "partial"]:
        count = s.get(key, 0)
        if count > 0:
            lines.append(f"| {key.replace('_', ' ').title()} | {count} |")
    lines.append(f"| **Total** | **{s.get('total_groups', 0)}** |")
    lines.append("")

    # Decision map
    decision_map = {d.group_id: d for d in result.governance_decisions}
    response_map = {ar.group_id: ar for ar in result.author_responses}

    rebuttal_map: dict = {}
    for rb in result.rebuttals:
        rebuttal_map.setdefault(rb.group_id, []).append(rb)

    final_response_map = {af.group_id: af for af in result.author_final_responses}

    # Escalated items first
    escalated = [
        g for g in result.groups
        if decision_map.get(
            g.group_id, GovernanceDecision("", "", "", "")
        ).governance_resolution == Resolution.ESCALATED.value
    ]
    if escalated:
        lines.append("## Escalated Items (Require Human Decision)")
        lines.append("")
        for g in escalated:
            lines.extend(_format_group_section(
                g, decision_map, response_map,
                rebuttal_map=rebuttal_map,
                final_response_map=final_response_map,
            ))

    # All other groups
    non_escalated = [
        g for g in result.groups
        if decision_map.get(
            g.group_id, GovernanceDecision("", "", "", "")
        ).governance_resolution != Resolution.ESCALATED.value
    ]
    if non_escalated:
        lines.append("## Review Points")
        lines.append("")
        for g in non_escalated:
            lines.extend(_format_group_section(
                g, decision_map, response_map,
                rebuttal_map=rebuttal_map,
                final_response_map=final_response_map,
            ))

    # Revised output
    if result.revised_output:
        if result.mode == "plan":
            label = "Revised Plan"
        elif result.mode == "integration":
            label = "Remediation Plan"
        else:
            label = "Unified Diff"
        lines.append(f"## {label}")
        lines.append("")
        lines.append("```")
        lines.append(result.revised_output)
        lines.append("```")
        lines.append("")

    # Cost breakdown
    lines.append("## Cost Breakdown")
    lines.append("")
    lines.append("| Model | Cost (USD) |")
    lines.append("|---|---|")
    for model, cost in result.cost.breakdown().items():
        lines.append(f"| {model} | ${cost:.4f} |")
    lines.append(f"| **Total** | **${result.cost.total_usd:.4f}** |")
    lines.append("")

    return "\n".join(lines)


# ─── Group Section Formatter ────────────────────────────────────────────────


def _format_group_section(
    group,
    decision_map: dict,
    response_map: dict,
    rebuttal_map: dict | None = None,
    final_response_map: dict | None = None,
) -> list:
    """Format a single review group for the report."""
    lines: list[str] = []
    dec = decision_map.get(group.group_id)
    ar = response_map.get(group.group_id)
    resolution_label = dec.governance_resolution if dec else "pending"

    lines.append(f"### {group.group_id}: {group.concern[:80]}")
    lines.append(
        f"**Consensus:** {resolution_label.replace('_', ' ').title()}"
        f" ({len(group.source_reviewers)} reviewer"
        f"{'s' if len(group.source_reviewers) != 1 else ''})"
    )
    lines.append(
        f"**Severity:** {group.combined_severity.title()} | "
        f"**Category:** {group.combined_category.replace('_', ' ').title()}"
    )
    lines.append("")

    lines.append("**Reviewer Feedback:**")
    for p in group.points:
        lines.append(f"- **{p.reviewer}:** {p.description}")
        if p.recommendation:
            lines.append(f"  - *Recommendation:* {p.recommendation}")
    lines.append("")

    # Author Round 1 Response -- ALWAYS shown
    lines.append("**Author Response (Round 1):**")
    if ar:
        lines.append(f"  **Resolution:** {ar.resolution}")
        if ar.rationale:
            lines.append(f"> {ar.rationale}")
        else:
            lines.append("> *(No rationale provided)*")
    else:
        lines.append(
            "> **Author did not respond to this group.** "
            "The response may exist in `author_raw.txt` but failed "
            "to parse/match to this group ID."
        )
    lines.append("")

    # Reviewer Rebuttals (Round 2)
    group_rebuttals = rebuttal_map.get(group.group_id, []) if rebuttal_map else []
    if group_rebuttals:
        lines.append("**Reviewer Rebuttals (Round 2):**")
        for rb in group_rebuttals:
            icon = "+" if rb.verdict == "CONCUR" else "x"
            lines.append(f"- {icon} **{rb.reviewer}:** {rb.verdict}")
            if rb.rationale:
                lines.append(f"  > {rb.rationale}")
        lines.append("")

    # Author Final Response (Round 2) -- only for challenged groups
    af = final_response_map.get(group.group_id) if final_response_map else None
    has_challenges = any(rb.verdict == "CHALLENGE" for rb in group_rebuttals)
    if has_challenges:
        lines.append("**Author Final Response (Round 2):**")
        if af:
            lines.append(f"  **Resolution:** {af.resolution}")
            if af.rationale:
                lines.append(f"> {af.rationale}")
            else:
                lines.append("> *(No rationale provided)*")
        else:
            lines.append("> **Author did not provide a final response to this challenge.**")
        lines.append("")

    if dec:
        lines.append(f"**Governance:** {dec.reason}")
        lines.append("")

    lines.append("---")
    lines.append("")
    return lines


# ─── Ledger Generator ───────────────────────────────────────────────────────


def generate_ledger(result: ReviewResult) -> dict:
    """Generate review-ledger.json structure."""
    decision_map = {d.group_id: d for d in result.governance_decisions}
    response_map = {ar.group_id: ar for ar in result.author_responses}

    rebuttal_map: dict = {}
    for rb in result.rebuttals:
        rebuttal_map.setdefault(rb.group_id, []).append(rb)
    final_map = {af.group_id: af for af in result.author_final_responses}

    points_out: list[dict] = []
    for group in result.groups:
        dec = decision_map.get(group.group_id)
        ar = response_map.get(group.group_id)
        af = final_map.get(group.group_id)
        group_rebuttals = rebuttal_map.get(group.group_id, [])
        for p in group.points:
            points_out.append({
                "point_id": p.point_id,
                "group_id": group.group_id,
                "severity": p.severity,
                "category": p.category,
                "description": p.description,
                "recommendation": p.recommendation,
                "location": p.location,
                "reviewer": p.reviewer,
                "source_reviewers": group.source_reviewers,
                "author_resolution": ar.resolution if ar else "no_response",
                "author_rationale": ar.rationale if ar else "",
                "rebuttals": [asdict(rb) for rb in group_rebuttals],
                "author_final_resolution": af.resolution if af else None,
                "author_final_rationale": af.rationale if af else None,
                "governance_resolution": dec.governance_resolution if dec else "pending",
                "governance_reason": dec.reason if dec else "",
                "final_resolution": dec.governance_resolution if dec else "pending",
                "overrides": [],
            })

    # Compute summary
    gov_counts: dict[str, int] = {}
    for d in result.governance_decisions:
        gov_counts[d.governance_resolution] = gov_counts.get(d.governance_resolution, 0) + 1

    summary = {
        "total_points": sum(len(g.points) for g in result.groups),
        "total_groups": len(result.groups),
    }
    summary.update(gov_counts)

    return {
        "review_id": result.review_id,
        "mode": result.mode,
        "input_file": result.input_file,
        "project": result.project,
        "timestamp": result.timestamp,
        "author_model": result.author_model,
        "reviewer_models": result.reviewer_models,
        "dedup_model": result.dedup_model,
        "points": points_out,
        "summary": summary,
        "cost": {
            "total_usd": round(result.cost.total_usd, 6),
            "breakdown": {k: round(v, 6) for k, v in result.cost.breakdown().items()},
            "role_costs": {k: round(v, 6) for k, v in result.cost.role_costs.items()},
        },
    }


# ─── Spec Report Generator ────────────────────────────────────────────────


def _generate_spec_report(result: ReviewResult) -> str:
    """Generate spec-mode report — themed suggestion list with consensus indicators."""
    lines = ["# Specification Enrichment Report", ""]
    lines.append(f"**Mode:** Spec Review (Collaborative Ideation)")
    lines.append(f"**Input:** `{result.input_file}`")
    lines.append(f"**Project:** {result.project}")
    lines.append(f"**Date:** {result.timestamp}")
    lines.append(f"**Review ID:** `{result.review_id}`")
    lines.append(f"**Reviewer Models:** {', '.join(result.reviewer_models)}")
    lines.append(f"**Dedup Model:** {result.dedup_model}")
    lines.append(f"**Total Cost:** ${result.cost.total_usd:.4f}")
    lines.append("")

    # Summary
    s = result.summary
    total_reviewers = len(result.reviewer_models)
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- **Total Suggestions:** {s.get('total_points', 0)}")
    lines.append(f"- **Suggestion Groups:** {s.get('total_groups', 0)}")
    lines.append(f"- **Multi-Reviewer Consensus:** {s.get('multi_consensus', 0)}")
    lines.append(f"- **Single Source:** {s.get('single_source', 0)}")
    lines.append("")

    # Group suggestions by theme
    by_theme: dict[str, list] = {}
    for g in result.groups:
        theme = (g.combined_category or "other").replace("_", " ").title()
        by_theme.setdefault(theme, []).append(g)

    # Sort themes alphabetically, but put "Other" last
    theme_order = sorted(k for k in by_theme if k != "Other")
    if "Other" in by_theme:
        theme_order.append("Other")

    for theme in theme_order:
        groups = by_theme[theme]
        lines.append(f"## {theme}")
        lines.append("")
        # Sort by consensus count descending
        groups.sort(key=lambda g: len(g.source_reviewers), reverse=True)
        for g in groups:
            consensus = len(g.source_reviewers)
            if consensus > 1:
                indicator = f" -- {consensus}/{total_reviewers} reviewers"
            else:
                indicator = ""
            lines.append(f"### {g.concern[:120]}{indicator}")
            lines.append("")
            for p in g.points:
                lines.append(f"- **{p.reviewer}:** {p.description}")
                if p.location:
                    lines.append(f"  - *Context:* {p.location}")
            lines.append("")

    # High-consensus section
    high_consensus = [g for g in result.groups if len(g.source_reviewers) > 1]
    if high_consensus:
        lines.append("## High-Consensus Ideas")
        lines.append("")
        lines.append(
            "The following suggestions were independently raised by multiple reviewers, "
            "indicating strong signal:"
        )
        lines.append("")
        for g in high_consensus:
            consensus = len(g.source_reviewers)
            lines.append(
                f"- **{g.concern[:120]}** "
                f"({consensus}/{total_reviewers} reviewers: "
                f"{', '.join(g.source_reviewers)})"
            )
        lines.append("")

    # Revised output (suggestion report from revision LLM)
    if result.revised_output:
        lines.append("## Compiled Suggestion Report")
        lines.append("")
        lines.append(result.revised_output)
        lines.append("")

    # Cost breakdown
    lines.append("## Cost Breakdown")
    lines.append("")
    lines.append("| Model | Cost (USD) |")
    lines.append("|---|---|")
    for model, cost in result.cost.breakdown().items():
        lines.append(f"| {model} | ${cost:.4f} |")
    lines.append(f"| **Total** | **${result.cost.total_usd:.4f}** |")
    lines.append("")

    return "\n".join(lines)
