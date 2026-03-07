"""Shared adversarial pipeline: Round 2 exchange, governance, and revision.

This module contains the core multi-round adversarial flow used by plan,
code, and integration modes.  Extracted from ``_common`` for navigability.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass

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
    ReviewResult,
)
from ..cost import check_context_window, estimate_tokens
from ..providers import (
    AUTHOR_RESPONSE_MAX_OUTPUT_TOKENS,
    MAX_OUTPUT_TOKENS,
    call_with_retry,
)
from ..prompts import (
    build_author_final_prompt,
    build_reviewer_rebuttal_prompt,
    build_round1_author_prompt,
    get_reviewer_system_prompt,
)
from ..parser import (
    parse_author_final_response,
    parse_author_response,
    parse_rebuttal_response,
)
from ..governance import apply_governance
from ..output import generate_ledger, generate_report
from ..revision import run_revision
from ..storage import StorageManager
from ..ui import console

from ._common import _call_info, _check_cost_guardrail, _save_stub_ledger
from ._display import _print_governance_summary, _print_summary_table
from ._formatting import (
    _compute_summary,
    _format_author_responses_for_rebuttal,
    _format_challenged_groups,
    _format_groups_for_author,
    _get_contested_groups_for_reviewer,
    _group_to_dict,
)


# ---- Round 2 exchange --------------------------------------------------------


async def _run_round2_exchange(
    client,
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
    storage.log("Round 2: sending author responses to reviewers for rebuttal")
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

        effective_max_r = r.max_out_configured or MAX_OUTPUT_TOKENS
        storage.log(
            f"Round 2: calling {r.name} "
            f"({_call_info(r, rebuttal_prompt, effective_max_r)})"
        )
        rebuttal_reviewers.append(r)
        rebuttal_coroutines.append(
            call_with_retry(
                client,
                r,
                get_reviewer_system_prompt(),
                rebuttal_prompt,
                effective_max_r,
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
        storage.log(
            f"Round 2: {r.name} responded "
            f"(recv: {rebuttal_usage['output_tokens']})"
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
    storage.log(f"Round 2: rebuttals complete -- {total_challenges} challenge(s)")
    console.print(f"  Challenges: {total_challenges} total")

    # -- Author final response (only if challenges exist) --
    author_final_responses: list[AuthorFinalResponse] = []
    challenged_group_ids = set(
        rb.group_id for rb in all_rebuttals if rb.verdict == "CHALLENGE"
    )

    if challenged_group_ids:
        storage.log(
            f"Round 2: giving author last word on "
            f"{len(challenged_group_ids)} challenge(s)"
        )
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
                storage.log(
                    f"Round 2: calling author to respond to rebuttals "
                    f"({_call_info(author, final_prompt, AUTHOR_RESPONSE_MAX_OUTPUT_TOKENS)})"
                )
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
                storage.log(
                    f"Round 2: author responded "
                    f"(recv: {final_usage['output_tokens']})"
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


# ---- Shared adversarial pipeline --------------------------------------------


@dataclass
class PipelineInputs:
    """Everything the shared adversarial pipeline needs after Round 1
    reviewer calls and dedup have completed."""

    mode: str
    content: str
    input_file_label: str
    project: str
    review_id: str
    timestamp: str
    all_points: list
    groups: list
    author: ModelConfig
    active_reviewers: list
    dedup_model: ModelConfig
    revision_model: ModelConfig
    cost_tracker: CostTracker
    storage: StorageManager
    revision_filename: str
    reviewer_roles: dict


async def _run_adversarial_pipeline(
    client,
    inputs: PipelineInputs,
) -> ReviewResult | None:
    """Shared adversarial pipeline: author response through revision.

    Assumes Round 1 reviewer calls and deduplication have already completed.
    The caller is responsible for content assembly, pre-flight checks, dry run,
    lock acquisition, Round 1 reviewer calls, and deduplication.

    Returns a ReviewResult on success, or None if a cost guardrail aborts.
    """
    mode = inputs.mode
    content = inputs.content
    groups = inputs.groups
    all_points = inputs.all_points
    author = inputs.author
    cost_tracker = inputs.cost_tracker
    storage = inputs.storage
    review_id = inputs.review_id

    # -- Round 1: Author response --
    console.print(
        Panel(
            "[bold]Round 1:[/bold] Author responding to reviewer findings...",
            style="blue",
        )
    )
    grouped_text = _format_groups_for_author(groups)
    round1_author_prompt = build_round1_author_prompt(mode, content, grouped_text)

    fits, est, limit = check_context_window(author, round1_author_prompt)
    if not fits:
        console.print(
            f"[red]Error:[/red] Author prompt ({est} tokens) exceeds "
            f"author context ({limit}). Cannot proceed."
        )
        return None

    console.print(
        f"  Prompt size: ~{estimate_tokens(round1_author_prompt)} tokens"
    )
    storage.log(
        f"Round 1: calling author to respond to grouped feedback "
        f"({_call_info(author, round1_author_prompt, AUTHOR_RESPONSE_MAX_OUTPUT_TOKENS)})"
    )
    author_raw, author_usage = await call_with_retry(
        client,
        author,
        "",
        round1_author_prompt,
        AUTHOR_RESPONSE_MAX_OUTPUT_TOKENS,
        log_fn=storage.log,
        mode=mode,
    )
    cost_tracker.add(
        author.name,
        author_usage["input_tokens"],
        author_usage["output_tokens"],
        author.cost_per_1k_input,
        author.cost_per_1k_output,
        role="author",
    )
    console.print(
        f"  Author responded ({author_usage['output_tokens']} tokens)"
    )
    storage.log(
        f"Round 1: author responded "
        f"(recv: {author_usage['output_tokens']})"
    )
    storage.save_intermediate(review_id, "round2", "author_raw.txt", author_raw)

    # Parse author response
    author_responses = parse_author_response(
        author_raw, groups, log_fn=storage.log
    )
    storage.save_intermediate(
        review_id,
        "round2",
        "author_responses.json",
        [asdict(ar) for ar in author_responses],
    )

    # Log Round 1 author parsing coverage
    parsed_count = len(author_responses)
    total_count = len(groups)
    console.print(f"  Parsed: {parsed_count}/{total_count} groups matched")
    if parsed_count < total_count:
        console.print(
            f"  [yellow]Warning: {total_count - parsed_count} groups "
            f"unmatched -- will be escalated[/yellow]"
        )

    # Cost guardrail checkpoint
    if _check_cost_guardrail(cost_tracker, storage):
        _save_stub_ledger(
            storage, review_id, mode, inputs.project, inputs.input_file_label,
            "cost_aborted", timestamp=inputs.timestamp, cost_tracker=cost_tracker,
        )
        return None

    # -- Round 2: Reviewer rebuttal + Author final response --
    all_rebuttals, author_final_responses, _ = await _run_round2_exchange(
        client,
        mode,
        content,
        groups,
        author_responses,
        grouped_text,
        author,
        inputs.active_reviewers,
        cost_tracker,
        storage,
        review_id,
        reviewer_roles=inputs.reviewer_roles,
    )

    # -- Governance --
    storage.log("Governance: applying deterministic rules")
    console.print(
        Panel(
            "[bold]Governance:[/bold] Applying deterministic rules...",
            style="blue",
        )
    )

    decisions = _apply_governance_or_escalate(
        groups,
        author_responses,
        all_rebuttals,
        author_final_responses,
        mode,
        parsed_count,
        total_count,
        storage,
    )
    storage.save_intermediate(
        review_id,
        "round2",
        "governance.json",
        [asdict(d) for d in decisions],
    )

    _print_governance_summary(decisions)

    # Summary
    summary = _compute_summary(decisions, groups)
    storage.log(f"Governance complete: {summary}")

    # Build result
    result = ReviewResult(
        review_id=review_id,
        mode=mode,
        input_file=inputs.input_file_label,
        project=inputs.project,
        timestamp=inputs.timestamp,
        author_model=author.name,
        reviewer_models=[r.name for r in inputs.active_reviewers],
        dedup_model=inputs.dedup_model.name,
        points=[asdict(p) for p in all_points],
        groups=groups,
        author_responses=author_responses,
        governance_decisions=decisions,
        rebuttals=all_rebuttals,
        author_final_responses=author_final_responses,
        cost=cost_tracker,
        revised_output="",
        summary=summary,
    )

    # Save report and ledger BEFORE revision
    report_str = generate_report(result)
    ledger_dict = generate_ledger(result)
    round1_data = {
        "points": [asdict(p) for p in all_points],
        "groups": [_group_to_dict(g) for g in groups],
    }
    round2_data = {
        "author_responses": [asdict(ar) for ar in author_responses],
        "rebuttals": [asdict(rb) for rb in all_rebuttals],
        "author_final_responses": [asdict(af) for af in author_final_responses],
        "governance": [asdict(d) for d in decisions],
    }
    storage.save_review_artifacts(
        review_id, report_str, ledger_dict, round1_data, round2_data
    )

    # Persist original content for dvad revise
    rd = storage.review_dir(review_id)
    storage._atomic_write(rd / "original_content.txt", content)

    # -- Revision (post-governance) --
    has_actionable = any(
        d.governance_resolution in ("auto_accepted", "accepted", "overridden")
        for d in decisions
    )
    if has_actionable:
        storage.log("Revision: generating revised artifact with authors final input")
        console.print(
            Panel(
                "[bold]Revision:[/bold] Generating revised artifact...",
                style="blue",
            )
        )
        try:
            revised_output = await run_revision(
                client,
                inputs.revision_model,
                content,
                ledger_dict,
                mode=mode,
                cost_tracker=cost_tracker,
                storage=storage,
                review_id=review_id,
            )
            if revised_output:
                storage._atomic_write(rd / inputs.revision_filename, revised_output)
                console.print(
                    f"  Revised artifact saved ({len(revised_output):,} chars)"
                )
        except Exception as e:
            console.print(
                f"  [yellow]Warning: Revision failed: {e}[/yellow]"
            )
            storage.log(f"Revision failed (non-fatal): {e}")
            revised_output = ""

        if not revised_output:
            # Downgrade result from success → completed (non-fatal revision failure)
            ledger_dict["result"] = "completed"
            storage.save_review_artifacts(
                review_id, report_str, ledger_dict, round1_data, round2_data
            )
    else:
        console.print("  [dim]No actionable findings — skipping revision[/dim]")

    # -- Console output --
    console.print(f"\n[green]Review complete.[/green]")
    console.print(f"  Report: {rd / 'dvad-report.md'}")
    console.print(f"  Ledger: {rd / 'review-ledger.json'}")
    revision_path = rd / inputs.revision_filename
    if revision_path.exists():
        console.print(f"  Revised: {revision_path}")

    _print_summary_table(result)
    return result
