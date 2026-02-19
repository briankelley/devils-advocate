"""Shared helpers for review orchestrators.

Internal helpers for prompt formatting, cost estimation, governance,
and the Round 2 exchange protocol.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx
from rich.panel import Panel
from rich.table import Table

from ..types import (
    APIError,
    AuthorFinalResponse,
    AuthorResponse,
    CostLimitError,
    CostTracker,
    GovernanceDecision,
    ModelConfig,
    RebuttalResponse,
    Resolution,
    ReviewContext,
    ReviewGroup,
    ReviewPoint,
    ReviewResult,
)
from ..ids import assign_guids, generate_review_id
from ..cost import check_context_window, estimate_cost, estimate_tokens
from ..config import get_models_by_role
from ..providers import (
    AUTHOR_MAX_OUTPUT_TOKENS,
    MAX_OUTPUT_TOKENS,
    call_with_retry,
)
from ..prompts import (
    build_author_final_prompt,
    build_integration_prompt,
    build_review_prompt,
    build_reviewer_rebuttal_prompt,
    build_revised_diff_followup_prompt,
    build_revised_plan_followup_prompt,
    build_round1_author_prompt,
    get_reviewer_system_prompt,
)
from ..parser import (
    extract_revised_output,
    parse_author_final_response,
    parse_author_response,
    parse_rebuttal_response,
    parse_review_response,
)
from ..normalization import normalize_review_response
from ..dedup import deduplicate_points
from ..governance import apply_governance
from ..output import generate_ledger, generate_report
from ..storage import StorageManager
from ..ui import console


# ---- Internal helpers --------------------------------------------------------


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


def _estimate_total_cost(
    content: str,
    author: ModelConfig,
    reviewers: list[ModelConfig],
    dedup: ModelConfig,
) -> float:
    """Rough cost estimate covering both rounds of the review protocol."""
    input_tokens = estimate_tokens(content)
    est_output = min(input_tokens, MAX_OUTPUT_TOKENS)
    total = 0.0
    # Round 1: reviewers
    for r in reviewers:
        total += estimate_cost(r, input_tokens, est_output)
    # Dedup
    total += estimate_cost(dedup, input_tokens, est_output // 2)
    # Round 1 author response
    total += estimate_cost(author, input_tokens * 2, AUTHOR_MAX_OUTPUT_TOKENS)
    # Round 2: reviewer rebuttal (same reviewers, similar input size)
    for r in reviewers:
        total += estimate_cost(r, input_tokens * 2, MAX_OUTPUT_TOKENS)
    # Round 2: author final response (estimated -- only triggered if challenges)
    total += estimate_cost(author, input_tokens * 2, AUTHOR_MAX_OUTPUT_TOKENS // 2)
    return total


def _print_dry_run(
    mode: str,
    content: str,
    author: ModelConfig,
    reviewers: list[ModelConfig],
    dedup: ModelConfig,
    max_cost: float | None,
) -> None:
    """Print a dry-run summary table without making API calls."""
    console.print(
        Panel(
            "[bold yellow]DRY RUN[/bold yellow] -- No API calls will be made",
            style="yellow",
        )
    )
    table = Table(title="Planned API Calls")
    table.add_column("Step", style="cyan")
    table.add_column("Model", style="green")
    table.add_column("Est. Input Tokens")
    table.add_column("Est. Output Tokens")
    table.add_column("Est. Cost (USD)")

    input_tokens = estimate_tokens(content)

    for r in reviewers:
        cost = estimate_cost(r, input_tokens, MAX_OUTPUT_TOKENS)
        table.add_row(
            "Round 1 (review)",
            r.name,
            str(input_tokens),
            str(MAX_OUTPUT_TOKENS),
            f"${cost:.4f}",
        )

    # Normalization fallback (potential)
    table.add_row(
        "Normalization (if needed)",
        author.name,
        str(MAX_OUTPUT_TOKENS),
        str(MAX_OUTPUT_TOKENS),
        f"${estimate_cost(author, MAX_OUTPUT_TOKENS, MAX_OUTPUT_TOKENS):.4f}",
    )

    dedup_in = input_tokens // 2
    cost_d = estimate_cost(dedup, dedup_in, MAX_OUTPUT_TOKENS // 2)
    table.add_row(
        "Deduplication",
        dedup.name,
        str(dedup_in),
        str(MAX_OUTPUT_TOKENS // 2),
        f"${cost_d:.4f}",
    )

    r2_in = input_tokens * 2
    cost_a = estimate_cost(author, r2_in, AUTHOR_MAX_OUTPUT_TOKENS)
    table.add_row(
        "Round 1 (author response)",
        author.name,
        str(r2_in),
        str(AUTHOR_MAX_OUTPUT_TOKENS),
        f"${cost_a:.4f}",
    )

    # Round 2: reviewer rebuttal
    for r in reviewers:
        cost_rb = estimate_cost(r, r2_in, MAX_OUTPUT_TOKENS)
        table.add_row(
            "Round 2 (rebuttal)",
            r.name,
            str(r2_in),
            str(MAX_OUTPUT_TOKENS),
            f"${cost_rb:.4f}",
        )

    # Round 2: author final (if challenges)
    cost_af = estimate_cost(author, r2_in, AUTHOR_MAX_OUTPUT_TOKENS // 2)
    table.add_row(
        "Round 2 (author final, if challenges)",
        author.name,
        str(r2_in),
        str(AUTHOR_MAX_OUTPUT_TOKENS // 2),
        f"${cost_af:.4f}",
    )

    console.print(table)

    total = _estimate_total_cost(content, author, reviewers, dedup)
    console.print(f"\nEstimated total cost: [bold]${total:.4f}[/bold]")
    if max_cost:
        color = "green" if total <= max_cost else "red"
        console.print(f"Cost limit: [{color}]${max_cost:.2f}[/{color}]")


def _print_summary_table(result: ReviewResult) -> None:
    """Print a post-review summary table to the console."""
    table = Table(title="Review Summary")
    table.add_column("Resolution", style="cyan")
    table.add_column("Count", justify="right")

    for key, label in [
        ("auto_accepted", "Auto-Accepted"),
        ("accepted", "Accepted"),
        ("auto_dismissed", "Auto-Dismissed"),
        ("escalated", "Escalated"),
    ]:
        count = result.summary.get(key, 0)
        if count > 0:
            style = {
                "auto_accepted": "green",
                "accepted": "green",
                "auto_dismissed": "dim",
                "escalated": "yellow",
            }.get(key, "")
            table.add_row(f"[{style}]{label}[/{style}]", str(count))

    table.add_row("[bold]Total Groups[/bold]", str(result.summary.get("total_groups", 0)))
    table.add_row("[bold]Total Points[/bold]", str(result.summary.get("total_points", 0)))
    table.add_row("[bold]Total Cost[/bold]", f"${result.cost.total_usd:.4f}")
    console.print(table)


def _print_governance_summary(decisions: list[GovernanceDecision]) -> None:
    """Print a per-resolution count summary after governance."""
    counts: dict[str, int] = {}
    for d in decisions:
        counts[d.governance_resolution] = counts.get(d.governance_resolution, 0) + 1
    for res, count in counts.items():
        label = res.replace("_", " ").title()
        color = (
            "green" if res == "auto_accepted"
            else "yellow" if res == "escalated"
            else "cyan" if res == "auto_dismissed"
            else "red"
        )
        console.print(f"  [{color}]{label}: {count}[/{color}]")


# ---- Reviewer call -----------------------------------------------------------


async def _call_reviewer(
    client: httpx.AsyncClient,
    reviewer: ModelConfig,
    normalization_model: ModelConfig,
    prompt: str,
    review_id: str,
    cost_tracker: CostTracker,
    storage: StorageManager,
) -> list[ReviewPoint]:
    """Call a single reviewer and return parsed points.

    If ``parse_review_response`` yields no points, falls back to LLM
    normalization using *normalization_model* (Bug 4 fix: this is NOT the
    author model).
    """
    storage.log(f"Round 1: calling {reviewer.name}")
    text, usage = await call_with_retry(
        client,
        reviewer,
        get_reviewer_system_prompt(),
        prompt,
        MAX_OUTPUT_TOKENS,
        log_fn=storage.log,
    )
    cost_tracker.add(
        reviewer.name,
        usage["input_tokens"],
        usage["output_tokens"],
        reviewer.cost_per_1k_input,
        reviewer.cost_per_1k_output,
    )
    storage.log(
        f"Round 1: {reviewer.name} responded ({usage['output_tokens']} output tokens)"
    )

    # Save raw response
    storage.save_intermediate(review_id, "round1", f"{reviewer.name}_raw.txt", text)

    # Parse
    points = parse_review_response(text, reviewer.name)

    # LLM normalization fallback if no points extracted
    if not points:
        storage.log(
            f"  No structured points from {reviewer.name} -- trying LLM normalization"
        )
        points = await normalize_review_response(
            client, text, normalization_model, reviewer.name, log_fn=storage.log
        )

    return points


# ---- Round 2 exchange --------------------------------------------------------


async def _run_round2_exchange(
    client: httpx.AsyncClient,
    mode: str,
    content: str,
    groups: list[ReviewGroup],
    author_responses: list[AuthorResponse],
    grouped_text: str,
    author: ModelConfig,
    reviewers: list[ModelConfig],
    cost_tracker: CostTracker,
    storage: StorageManager,
    review_id: str,
) -> tuple[list[RebuttalResponse], list[AuthorFinalResponse], str | None]:
    """Shared Round 2 exchange: reviewer rebuttals + author final response.

    Returns (all_rebuttals, author_final_responses, updated_revised_output_or_None).

    Only sends rebuttal prompts to reviewers who sourced contested groups
    (rejected/partial/no-response).  If the author accepted every group,
    rebuttals are skipped entirely.
    """
    response_map = {ar.group_id: ar for ar in author_responses}

    # Determine if ANY groups are contested (anything other than ACCEPTED)
    any_contested = False
    for group in groups:
        ar = response_map.get(group.group_id)
        if not ar or ar.resolution not in ("ACCEPTED",):
            any_contested = True
            break

    if not any_contested:
        console.print("  All groups accepted -- skipping reviewer rebuttals")
        storage.log("Round 2: all groups accepted by author -- skipping rebuttals")
        return [], [], None

    # -- Reviewer rebuttal phase --
    console.print(
        Panel(
            "[bold]Round 2:[/bold] Sending author responses to reviewers...",
            style="blue",
        )
    )

    rebuttal_coroutines = []
    rebuttal_reviewers: list[ModelConfig] = []
    reviewer_contested_groups: dict[str, list[ReviewGroup]] = {}

    for r in reviewers:
        contested = _get_contested_groups_for_reviewer(r.name, groups, author_responses)
        if not contested:
            storage.log(f"Round 2: {r.name} has no contested groups -- skipping")
            continue

        reviewer_contested_groups[r.name] = contested

        # Build per-reviewer rebuttal prompt with only their contested groups
        reviewer_grouped_text = _format_groups_for_author(contested)
        reviewer_author_text = _format_author_responses_for_rebuttal(
            contested, author_responses
        )
        rebuttal_prompt = build_reviewer_rebuttal_prompt(
            mode, content, reviewer_grouped_text, reviewer_author_text
        )

        fits, est, limit = check_context_window(r, rebuttal_prompt)
        if not fits:
            console.print(
                f"  [yellow]Warning: Skipping {r.name}: "
                f"prompt ({est} tokens) exceeds context ({limit})[/yellow]"
            )
            storage.log(f"Skipping {r.name} rebuttal: context exceeded")
            continue

        rebuttal_reviewers.append(r)
        rebuttal_coroutines.append(
            call_with_retry(
                client,
                r,
                get_reviewer_system_prompt(),
                rebuttal_prompt,
                MAX_OUTPUT_TOKENS,
                log_fn=storage.log,
            )
        )

    console.print(f"  Sending rebuttals to {len(rebuttal_reviewers)} reviewer(s)")

    rebuttal_results = await asyncio.gather(
        *rebuttal_coroutines, return_exceptions=True
    )

    all_rebuttals: list[RebuttalResponse] = []
    for r, rb_result in zip(rebuttal_reviewers, rebuttal_results):
        if isinstance(rb_result, Exception):
            console.print(f"  [yellow]Warning: {r.name} failed: {rb_result}[/yellow]")
            storage.log(f"Rebuttal {r.name} failed: {rb_result}")
            continue
        rebuttal_raw, rebuttal_usage = rb_result
        cost_tracker.add(
            r.name,
            rebuttal_usage["input_tokens"],
            rebuttal_usage["output_tokens"],
            r.cost_per_1k_input,
            r.cost_per_1k_output,
        )
        storage.save_intermediate(
            review_id, "round2", f"{r.name}_rebuttal_raw.txt", rebuttal_raw
        )

        # Parse against the groups this reviewer actually received
        contested = reviewer_contested_groups.get(r.name, groups)
        rebuttals = parse_rebuttal_response(
            rebuttal_raw, r.name, contested, log_fn=storage.log
        )
        storage.save_intermediate(
            review_id,
            "round2",
            f"{r.name}_rebuttal_parsed.json",
            [asdict(rb) for rb in rebuttals],
        )

        concur = sum(1 for rb in rebuttals if rb.verdict == "CONCUR")
        challenge = sum(1 for rb in rebuttals if rb.verdict == "CHALLENGE")
        console.print(
            f"  {r.name}: {concur} concur, {challenge} challenge "
            f"({rebuttal_usage['output_tokens']} tokens)"
        )
        all_rebuttals.extend(rebuttals)

    total_challenges = sum(1 for rb in all_rebuttals if rb.verdict == "CHALLENGE")
    console.print(f"  Challenges: {total_challenges} total")

    # -- Author final response (only if challenges exist) --
    author_final_responses: list[AuthorFinalResponse] = []
    updated_revised: str | None = None
    challenged_group_ids = set(
        rb.group_id for rb in all_rebuttals if rb.verdict == "CHALLENGE"
    )

    if challenged_group_ids:
        console.print(
            Panel(
                f"[bold]Round 2:[/bold] Author responding to "
                f"{len(challenged_group_ids)} challenges...",
                style="blue",
            )
        )

        challenged_text = _format_challenged_groups(
            groups, author_responses, all_rebuttals
        )
        final_prompt = build_author_final_prompt(mode, content, challenged_text)

        fits, est, limit = check_context_window(author, final_prompt)
        if not fits:
            console.print(
                f"[red]Error:[/red] Final prompt ({est} tokens) "
                f"exceeds author context ({limit})."
            )
            # Fall through -- governance runs on Round 1 positions only
        else:
            try:
                final_raw, final_usage = await call_with_retry(
                    client,
                    author,
                    "",
                    final_prompt,
                    AUTHOR_MAX_OUTPUT_TOKENS,
                    log_fn=storage.log,
                )
                cost_tracker.add(
                    author.name,
                    final_usage["input_tokens"],
                    final_usage["output_tokens"],
                    author.cost_per_1k_input,
                    author.cost_per_1k_output,
                )
                storage.save_intermediate(
                    review_id, "round2", "author_final_raw.txt", final_raw
                )

                author_final_responses = parse_author_final_response(
                    final_raw, groups, log_fn=storage.log
                )
                storage.save_intermediate(
                    review_id,
                    "round2",
                    "author_final_parsed.json",
                    [asdict(af) for af in author_final_responses],
                )

                console.print(
                    f"  Parsed: {len(author_final_responses)}/{len(challenged_group_ids)} "
                    f"challenges matched ({final_usage['output_tokens']} tokens)"
                )

                # Extract updated revised output if author produced one
                final_revised = extract_revised_output(final_raw, mode)
                if final_revised:
                    updated_revised = final_revised
                    console.print(
                        f"  Updated revised output extracted ({len(updated_revised):,} chars)"
                    )
            except APIError as e:
                console.print(
                    f"  [yellow]Warning: Author final response failed: {e}[/yellow]"
                )
                storage.log(f"Author final response failed: {e}")
                console.print("  [dim]Proceeding with Round 1 positions only[/dim]")
    else:
        console.print("  No challenges -- skipping author final response")

    return all_rebuttals, author_final_responses, updated_revised


# ---- Catastrophic parse / governance helpers --------------------------------


def _apply_governance_or_escalate(
    groups: list[ReviewGroup],
    author_responses: list[AuthorResponse],
    all_rebuttals: list[RebuttalResponse],
    author_final_responses: list[AuthorFinalResponse],
    mode: str,
    parsed_count: int,
    total_count: int,
    storage: StorageManager,
) -> list[GovernanceDecision]:
    """Apply governance, or escalate everything on catastrophic parse failure."""
    if total_count > 0 and parsed_count / total_count < 0.25:
        storage.log("Catastrophic parse failure (<25% coverage) -- escalating all groups")
        console.print(
            "[red]Error:[/red] Author response parsing below 25% -- "
            "likely prompt or model failure. Escalating all groups."
        )
        return [
            GovernanceDecision(
                group_id=g.group_id,
                author_resolution="parse_failure",
                governance_resolution=Resolution.ESCALATED.value,
                reason="Catastrophic parse failure (<25% coverage) -- escalating all groups",
            )
            for g in groups
        ]
    return apply_governance(
        groups,
        author_responses,
        rebuttals=all_rebuttals,
        author_final_responses=author_final_responses,
        mode=mode,
    )


# ---- Cost guardrail checkpoint -----------------------------------------------


def _check_cost_guardrail(cost_tracker: CostTracker, storage: StorageManager) -> bool:
    """Check cost guardrail flags.  Returns True if should abort.

    Emits 80% warning if threshold just crossed; returns True on exceeded.
    """
    if cost_tracker.warned_80:
        console.print(
            f"[yellow]Warning:[/yellow] Cost usage at "
            f"${cost_tracker.total_usd:.4f} — approaching limit "
            f"${cost_tracker.max_cost:.2f}"
        )
        storage.log(
            f"Cost warning: ${cost_tracker.total_usd:.4f} "
            f"(80% of ${cost_tracker.max_cost:.2f})"
        )
        # Reset so we don't print every checkpoint
        cost_tracker.warned_80 = False

    if cost_tracker.exceeded:
        console.print(
            f"[red]Error:[/red] Cost limit exceeded: "
            f"${cost_tracker.total_usd:.4f} >= ${cost_tracker.max_cost:.2f}. "
            f"Aborting gracefully."
        )
        storage.log(
            f"Cost limit exceeded: ${cost_tracker.total_usd:.4f} >= "
            f"${cost_tracker.max_cost:.2f}"
        )
        return True
    return False
