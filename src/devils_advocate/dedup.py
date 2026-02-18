"""Deduplication engine for grouping review points."""

from __future__ import annotations

import httpx

from .types import CostTracker, ModelConfig, ReviewContext, ReviewGroup, ReviewPoint
from .cost import check_context_window
from .prompts import build_dedup_prompt
from .providers import MAX_OUTPUT_TOKENS, call_with_retry
from .parser import parse_dedup_response


def format_points_for_dedup(points: list[ReviewPoint]) -> str:
    """Format review points into the text block expected by the dedup prompt."""
    lines: list[str] = []
    for i, p in enumerate(points, 1):
        lines.append(f"POINT {i}:")
        lines.append(f"REVIEWER: {p.reviewer}")
        lines.append(f"SEVERITY: {p.severity}")
        lines.append(f"CATEGORY: {p.category}")
        lines.append(f"DESCRIPTION: {p.description}")
        lines.append(f"RECOMMENDATION: {p.recommendation}")
        if p.location:
            lines.append(f"LOCATION: {p.location}")
        lines.append("")
    return "\n".join(lines)


async def deduplicate_points(
    client: httpx.AsyncClient,
    all_points: list[ReviewPoint],
    model: ModelConfig,
    ctx: ReviewContext,
    log_fn=None,
    cost_tracker: CostTracker | None = None,
) -> list[ReviewGroup]:
    """Send all review points to the dedup model for grouping."""
    if not all_points:
        return []

    formatted = format_points_for_dedup(all_points)
    prompt = build_dedup_prompt(formatted)

    fits, est, limit = check_context_window(model, prompt)
    if not fits:
        if log_fn:
            log_fn(
                f"  Dedup input ({est} tokens) exceeds {model.name} context ({limit}). "
                "Skipping dedup -- each point becomes its own group."
            )
        # Fall back: each point is its own group
        groups: list[ReviewGroup] = []
        for i, p in enumerate(all_points):
            gid = ctx.make_group_id(i + 1)
            p.point_id = ctx.make_point_id(gid, 1)
            groups.append(ReviewGroup(
                group_id=gid,
                concern=p.description,
                points=[p],
                combined_severity=p.severity,
                combined_category=p.category,
                source_reviewers=[p.reviewer],
            ))
        return groups

    if log_fn:
        log_fn(f"  Deduplication: sending {len(all_points)} points to {model.name}")

    text, usage = await call_with_retry(
        client, model, "", prompt, MAX_OUTPUT_TOKENS, log_fn=log_fn,
    )

    if cost_tracker is not None:
        cost_tracker.add(
            model.name,
            usage.get("input_tokens", 0),
            usage.get("output_tokens", 0),
            model.cost_per_1k_input,
            model.cost_per_1k_output,
        )

    if log_fn:
        log_fn(
            f"  Deduplication: {model.name} responded "
            f"({usage.get('output_tokens', 0)} output tokens)"
        )

    return parse_dedup_response(text, all_points, ctx)
