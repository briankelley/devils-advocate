"""Deduplication engine for grouping review points."""

from __future__ import annotations

import httpx

from .types import CostTracker, ModelConfig, ReviewContext, ReviewGroup, ReviewPoint
from .cost import check_context_window
from .prompts import build_dedup_prompt, build_spec_dedup_prompt
from .providers import MAX_OUTPUT_TOKENS, call_with_retry
from .parser import parse_dedup_response, parse_spec_dedup_response


def promote_points_to_groups(
    points: list[ReviewPoint],
    ctx: ReviewContext,
) -> list[ReviewGroup]:
    """Promote each point to its own group (dedup fallback).

    Used when dedup is skipped (e.g. context overflow or partial reviewer failure).
    """
    groups: list[ReviewGroup] = []
    for i, p in enumerate(points):
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


def format_suggestions_for_dedup(points: list[ReviewPoint]) -> str:
    """Format spec suggestions into the text block expected by the spec dedup prompt."""
    lines: list[str] = []
    for i, p in enumerate(points, 1):
        lines.append(f"SUGGESTION {i}:")
        lines.append(f"REVIEWER: {p.reviewer}")
        lines.append(f"THEME: {p.category}")
        lines.append(f"DESCRIPTION: {p.description}")
        if p.location:
            lines.append(f"CONTEXT: {p.location}")
        lines.append("")
    return "\n".join(lines)


async def deduplicate_points(
    client: httpx.AsyncClient,
    all_points: list[ReviewPoint],
    model: ModelConfig,
    ctx: ReviewContext,
    log_fn=None,
    cost_tracker: CostTracker | None = None,
    mode: str = "plan",
) -> list[ReviewGroup]:
    """Send all review points to the dedup model for grouping.

    When mode="spec", uses spec-specific formatting, prompt, and parser.
    """
    if not all_points:
        return []

    if mode == "spec":
        formatted = format_suggestions_for_dedup(all_points)
        prompt = build_spec_dedup_prompt(formatted)
    else:
        formatted = format_points_for_dedup(all_points)
        prompt = build_dedup_prompt(formatted)

    fits, est, limit = check_context_window(model, prompt)
    if not fits:
        if log_fn:
            log_fn(
                f"  Dedup input ({est} tokens) exceeds {model.name} context ({limit}). "
                "Skipping dedup -- each point becomes its own group."
            )
        return promote_points_to_groups(all_points, ctx)

    if log_fn:
        thinking_str = ", thinking: on" if model.thinking else ""
        log_fn(
            f"  Deduplication: calling {model.name} "
            f"({len(all_points)} points, max_out: {MAX_OUTPUT_TOKENS}{thinking_str})"
        )

    text, usage = await call_with_retry(
        client, model, "", prompt, MAX_OUTPUT_TOKENS, log_fn=log_fn,
        mode="dedup",
    )

    if cost_tracker is not None:
        cost_tracker.add(
            model.name,
            usage.get("input_tokens", 0),
            usage.get("output_tokens", 0),
            model.cost_per_1k_input,
            model.cost_per_1k_output,
            role="dedup",
        )

    if log_fn:
        log_fn(
            f"  Deduplication: {model.name} responded "
            f"({usage.get('output_tokens', 0)} output tokens)"
        )

    if mode == "spec":
        total_reviewers = len(set(p.reviewer for p in all_points))
        return parse_spec_dedup_response(text, all_points, ctx, total_reviewers)
    return parse_dedup_response(text, all_points, ctx)
