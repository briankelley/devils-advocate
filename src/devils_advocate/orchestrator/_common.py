"""Shared helpers for review orchestrators.

Core protocol logic: reviewer calls, Round 2 exchange, governance,
and cost guardrails. Display and formatting helpers live in
``_display`` and ``_formatting`` respectively.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict

import httpx
from rich.panel import Panel

from ..types import (
    APIError,
    AuthorFinalResponse,
    AuthorResponse,
    CostTracker,
    GovernanceDecision,
    ModelConfig,
    RebuttalResponse,
    Resolution,
    ReviewGroup,
    ReviewPoint,
)
from ..cost import check_context_window
from ..providers import (
    AUTHOR_RESPONSE_MAX_OUTPUT_TOKENS,
    MAX_OUTPUT_TOKENS,
    call_with_retry,
)
from ..prompts import (
    build_author_final_prompt,
    build_reviewer_rebuttal_prompt,
    get_reviewer_system_prompt,
)
from ..parser import (
    parse_author_final_response,
    parse_rebuttal_response,
    parse_review_response,
)
from ..normalization import normalize_review_response
from ..governance import apply_governance
from ..storage import StorageManager
from ..ui import console

# -- Re-exports for backward compatibility ------------------------------------
# Orchestrator modules (plan.py, code.py, integration.py, spec.py) import
# these names from ._common, so we re-export them here.

from ._display import (  # noqa: F401
    _estimate_total_cost,
    _print_dry_run,
    _print_governance_summary,
    _print_summary_table,
)
from ._formatting import (  # noqa: F401
    _compute_summary,
    _format_author_responses_for_rebuttal,
    _format_challenged_groups,
    _format_groups_for_author,
    _get_contested_groups_for_reviewer,
    _group_to_dict,
)


# ---- Reviewer call -----------------------------------------------------------


async def _call_reviewer(
    client: httpx.AsyncClient,
    reviewer: ModelConfig,
    normalization_model: ModelConfig,
    prompt: str,
    review_id: str,
    cost_tracker: CostTracker,
    storage: StorageManager,
    system_prompt: str | None = None,
    point_parser=None,
    role_label: str = "reviewer",
    mode: str = "",
) -> list[ReviewPoint]:
    """Call a single reviewer and return parsed points.

    If ``parse_review_response`` yields no points, falls back to LLM
    normalization using *normalization_model* (Bug 4 fix: this is NOT the
    author model).

    Parameters
    ----------
    system_prompt : str | None
        Override the default reviewer system prompt. Used by spec mode.
    point_parser : callable | None
        Override the default ``parse_review_response`` parser. When provided,
        called as ``point_parser(text, reviewer.name)`` instead of the default.
    """
    storage.log(f"Round 1: calling {reviewer.name}")
    sys_prompt = system_prompt if system_prompt is not None else get_reviewer_system_prompt()
    text, usage = await call_with_retry(
        client,
        reviewer,
        sys_prompt,
        prompt,
        MAX_OUTPUT_TOKENS,
        log_fn=storage.log,
        mode=mode,
    )
    cost_tracker.add(
        reviewer.name,
        usage["input_tokens"],
        usage["output_tokens"],
        reviewer.cost_per_1k_input,
        reviewer.cost_per_1k_output,
        role=role_label,
    )
    storage.log(
        f"Round 1: {reviewer.name} responded ({usage['output_tokens']} output tokens)"
    )

    # Save raw response
    storage.save_intermediate(review_id, "round1", f"{reviewer.name}_raw.txt", text)

    # Parse
    parser_fn = point_parser if point_parser is not None else parse_review_response
    points = parser_fn(text, reviewer.name)

    # LLM normalization fallback if no points extracted
    if not points:
        storage.log(
            f"  No structured points from {reviewer.name} -- trying LLM normalization"
        )
        points = await normalize_review_response(
            client, text, normalization_model, reviewer.name,
            log_fn=storage.log, cost_tracker=cost_tracker,
            mode=mode or "normalization",
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
    reviewer_roles: dict[str, str] | None = None,
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
                mode=mode,
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
            role=reviewer_roles.get(r.name, "reviewer") if reviewer_roles else "reviewer",
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
                    AUTHOR_RESPONSE_MAX_OUTPUT_TOKENS,
                    log_fn=storage.log,
                    mode=mode,
                )
                cost_tracker.add(
                    author.name,
                    final_usage["input_tokens"],
                    final_usage["output_tokens"],
                    author.cost_per_1k_input,
                    author.cost_per_1k_output,
                    role="author",
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
            except APIError as e:
                console.print(
                    f"  [yellow]Warning: Author final response failed: {e}[/yellow]"
                )
                storage.log(f"Author final response failed: {e}")
                console.print("  [dim]Proceeding with Round 1 positions only[/dim]")
    else:
        console.print("  No challenges -- skipping author final response")

    return all_rebuttals, author_final_responses, None


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
