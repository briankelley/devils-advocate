"""Prompt formatting helpers for review orchestrators.

Functions that transform ReviewGroups, AuthorResponses, and
RebuttalResponses into text for LLM prompts, plus serialization
and summary computation.
"""

from __future__ import annotations

from dataclasses import asdict

from ..types import (
    AuthorResponse,
    GovernanceDecision,
    RebuttalResponse,
    ReviewGroup,
)


def _format_groups_for_author(groups: list[ReviewGroup]) -> str:
    """Format grouped review points for the author prompt."""
    lines = []
    for i, g in enumerate(groups, 1):
        lines.append(f"GROUP {i} [{g.guid}]:")
        lines.append(f"CONCERN: {g.concern}")
        lines.append(f"SEVERITY: {g.combined_severity}")
        lines.append(f"CATEGORY: {g.combined_category}")
        lines.append(
            f"REVIEWERS: {', '.join(g.source_reviewers)} "
            f"({len(g.source_reviewers)} reviewer"
            f"{'s' if len(g.source_reviewers) != 1 else ''})"
        )
        lines.append("FEEDBACK:")
        for p in g.points:
            lines.append(f"  [{p.reviewer}] {p.description}")
            if p.recommendation:
                lines.append(f"    Recommendation: {p.recommendation}")
            if p.location:
                lines.append(f"    Location: {p.location}")
        lines.append("")
    return "\n".join(lines)


def _format_author_responses_for_rebuttal(
    groups: list[ReviewGroup],
    author_responses: list[AuthorResponse],
) -> str:
    """Format author responses for the reviewer rebuttal prompt."""
    response_map = {ar.group_id: ar for ar in author_responses}
    lines = []
    for group in groups:
        ar = response_map.get(group.group_id)
        lines.append(f"GROUP [{group.guid}]: {group.concern[:120]}")
        if ar:
            lines.append(f"  RESOLUTION: {ar.resolution}")
            lines.append(f"  RATIONALE: {ar.rationale}")
        else:
            lines.append("  [NO AUTHOR RESPONSE]")
        lines.append("")
    return "\n".join(lines)


def _get_contested_groups_for_reviewer(
    reviewer_name: str,
    groups: list[ReviewGroup],
    author_responses: list[AuthorResponse],
) -> list[ReviewGroup]:
    """Return only groups where the reviewer was a source AND the author
    rejected or partially accepted (or did not respond).  Groups where the
    author fully accepted are excluded -- there is nothing to contest."""
    response_map = {ar.group_id: ar for ar in author_responses}
    contested = []
    for group in groups:
        if reviewer_name not in group.source_reviewers:
            continue
        ar = response_map.get(group.group_id)
        # No response, non-acceptance, or unrecognized resolution -> contested
        if not ar or ar.resolution not in ("ACCEPTED",):
            contested.append(group)
    return contested


def _format_challenged_groups(
    groups: list[ReviewGroup],
    author_responses: list[AuthorResponse],
    all_rebuttals: list[RebuttalResponse],
) -> str:
    """Format only challenged groups with full context for author's final response."""
    response_map = {ar.group_id: ar for ar in author_responses}
    rebuttal_map: dict[str, list[RebuttalResponse]] = {}
    for rb in all_rebuttals:
        rebuttal_map.setdefault(rb.group_id, []).append(rb)

    lines = []
    for group in groups:
        group_rebuttals = rebuttal_map.get(group.group_id, [])
        challenges = [rb for rb in group_rebuttals if rb.verdict == "CHALLENGE"]
        if not challenges:
            continue

        lines.append(f"GROUP [{group.guid}]: {group.concern}")
        lines.append("")
        lines.append("  ORIGINAL REVIEWER FINDINGS:")
        for p in group.points:
            lines.append(f"    - {p.reviewer}: {p.description}")
            if p.recommendation:
                lines.append(f"      Recommendation: {p.recommendation}")
        lines.append("")

        ar = response_map.get(group.group_id)
        if ar:
            lines.append(f"  YOUR ROUND 1 RESPONSE: {ar.resolution}")
            lines.append(f"    {ar.rationale}")
        else:
            lines.append("  YOUR ROUND 1 RESPONSE: [none]")
        lines.append("")

        lines.append("  REVIEWER CHALLENGES:")
        for rb in challenges:
            lines.append(f"    - {rb.reviewer}: {rb.rationale}")
        lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines)


def _group_to_dict(g: ReviewGroup) -> dict:
    """Serialize a ReviewGroup to a plain dict for intermediate storage."""
    d = {
        "group_id": g.group_id,
        "concern": g.concern,
        "points": [asdict(p) for p in g.points],
        "combined_severity": g.combined_severity,
        "combined_category": g.combined_category,
        "source_reviewers": g.source_reviewers,
    }
    if g.guid:
        d["guid"] = g.guid
    return d


def _compute_summary(
    decisions: list[GovernanceDecision],
    groups: list[ReviewGroup],
) -> dict:
    """Compute a summary dict from governance decisions."""
    summary: dict = {
        "total_groups": len(groups),
        "total_points": sum(len(g.points) for g in groups),
    }
    for d in decisions:
        key = d.governance_resolution
        summary[key] = summary.get(key, 0) + 1
    return summary
