"""Plan review orchestrator."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import httpx
from rich.panel import Panel

from ..types import (
    APIError,
    CostTracker,
    ReviewContext,
    ReviewPoint,
    ReviewResult,
)
from ..ids import assign_guids, generate_review_id
from ..cost import check_context_window, estimate_tokens
from ..config import get_models_by_role
from ..providers import AUTHOR_RESPONSE_MAX_OUTPUT_TOKENS, call_with_retry
from ..prompts import (
    build_review_prompt,
    build_round1_author_prompt,
)
from ..parser import parse_author_response
from ..revision import run_revision
from ..dedup import deduplicate_points
from ..output import generate_ledger, generate_report
from ..storage import StorageManager
from ..ui import console

from ._common import (
    _apply_governance_or_escalate,
    _call_reviewer,
    _check_cost_guardrail,
    _compute_summary,
    _estimate_total_cost,
    _format_groups_for_author,
    _group_to_dict,
    _print_dry_run,
    _print_governance_summary,
    _print_summary_table,
    _run_round2_exchange,
)


async def run_plan_review(
    config: dict,
    input_files: list[Path],
    project: str,
    max_cost: float | None = None,
    dry_run: bool = False,
) -> ReviewResult | None:
    """Full plan review orchestration."""
    roles = get_models_by_role(config)
    author = roles["author"]
    reviewers = roles["reviewers"]
    dedup_model = roles["dedup"]
    normalization_model = roles["normalization"]
    storage = StorageManager(Path.cwd())

    primary_file = input_files[0]
    primary_content = primary_file.read_text()

    if len(input_files) > 1:
        reference_sections = []
        for ref_file in input_files[1:]:
            ref_content = ref_file.read_text()
            reference_sections.append(
                f"=== REFERENCE FILE: {ref_file.name} ===\n"
                f"{ref_content}\n"
                f"=== END REFERENCE FILE: {ref_file.name} ==="
            )
        content = (
            f"=== PRIMARY ARTIFACT (under review) ===\n"
            f"{primary_content}\n"
            f"=== END PRIMARY ARTIFACT ===\n\n"
            f"The following files are provided as REFERENCE CONTEXT. Do not review these files\n"
            f"directly -- they are provided so you can verify claims, check interfaces, and\n"
            f"validate assumptions made in the primary artifact above.\n\n"
            + "\n\n".join(reference_sections)
        )
    else:
        content = primary_content

    review_id = generate_review_id(content)
    storage.set_review_id(review_id)
    timestamp = datetime.now(timezone.utc).isoformat()
    cost_tracker = CostTracker(max_cost=max_cost)
    review_start_time = datetime.now(timezone.utc)
    ctx = ReviewContext(
        project=project,
        review_id=review_id,
        review_start_time=review_start_time,
    )

    storage.log(f"Starting plan review for project '{project}'")
    storage.log(
        f"Primary input: {primary_file} ({len(primary_content)} chars, "
        f"~{estimate_tokens(primary_content)} tokens)"
    )
    if len(input_files) > 1:
        for ref_file in input_files[1:]:
            ref_size = ref_file.stat().st_size
            storage.log(f"Reference input: {ref_file} ({ref_size} chars)")
    storage.log(
        f"Total prompt content: {len(content)} chars, ~{estimate_tokens(content)} tokens"
    )
    storage.log(f"Review ID: {review_id}")
    storage.log(
        f"Author: {author.name}, Reviewers: {', '.join(r.name for r in reviewers)}"
    )
    storage.log(f"Dedup: {dedup_model.name}")

    # Pre-flight: context window checks (Bug 3 fix: active_reviewers accumulator)
    review_prompt = build_review_prompt("plan", content)
    active_reviewers = []
    for r in reviewers:
        fits, est, limit = check_context_window(r, review_prompt)
        if not fits:
            console.print(
                f"[yellow]Warning:[/yellow] Input ({est} tokens) exceeds "
                f"{r.name} context limit ({limit}). Skipping this reviewer."
            )
            storage.log(
                f"Skipping {r.name}: input ({est} tokens) exceeds context ({limit})"
            )
        else:
            active_reviewers.append(r)

    if len(active_reviewers) < 1:
        console.print(
            "[red]Error:[/red] No reviewers available after context window checks."
        )
        return None

    # Dry run
    if dry_run:
        _print_dry_run("plan", content, author, active_reviewers, dedup_model, max_cost)
        return None

    # Cost estimate
    if max_cost is not None:
        est_cost = _estimate_total_cost(content, author, active_reviewers, dedup_model)
        if est_cost > max_cost:
            console.print(
                f"[red]Error:[/red] Estimated cost ${est_cost:.4f} exceeds "
                f"--max-cost ${max_cost:.2f}. Aborting."
            )
            return None
        storage.log(f"Estimated cost: ${est_cost:.4f} (limit: ${max_cost:.2f})")

    # Acquire lock
    if not storage.acquire_lock():
        console.print(
            "[red]Error:[/red] Another dvad review is running for this project. "
            "Wait or remove .dvad/.lock if stale."
        )
        return None

    try:
        # -- Round 1: Parallel reviewer calls --
        console.print(
            Panel("[bold]Round 1:[/bold] Sending to reviewers...", style="blue")
        )
        all_points: list[ReviewPoint] = []
        revision_model = roles["revision"]

        async with httpx.AsyncClient() as client:
            # Fire all reviewer calls in parallel
            tasks = [
                _call_reviewer(
                    client,
                    r,
                    normalization_model,
                    review_prompt,
                    review_id,
                    cost_tracker,
                    storage,
                )
                for r in active_reviewers
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            for r, result in zip(active_reviewers, results):
                if isinstance(result, Exception):
                    console.print(f"  [red]x[/red] {r.name}: failed -- {result}")
                    storage.log(f"Reviewer {r.name} failed: {result}")
                    continue
                points = result
                all_points.extend(points)
                console.print(f"  {r.name}: {len(points)} review points")
                storage.log(f"Parsed {len(points)} review points from {r.name}")
                storage.save_intermediate(
                    review_id,
                    "round1",
                    f"{r.name}_parsed.json",
                    [asdict(p) for p in points],
                )

            if not all_points:
                console.print(
                    "[red]Error:[/red] No review points from any reviewer. Aborting."
                )
                return None

            # Cost guardrail checkpoint
            if _check_cost_guardrail(cost_tracker, storage):
                return None

            console.print(f"  Total review points: {len(all_points)}")

            # -- Deduplication --
            console.print(
                Panel(
                    "[bold]Deduplication:[/bold] Grouping feedback...",
                    style="blue",
                )
            )
            groups = await deduplicate_points(
                client,
                all_points,
                dedup_model,
                ctx,
                log_fn=storage.log,
                cost_tracker=cost_tracker,
            )
            assign_guids(groups)
            storage.save_intermediate(
                review_id,
                "round1",
                "deduplication.json",
                [_group_to_dict(g) for g in groups],
            )
            console.print(
                f"  {len(groups)} groups identified from {len(all_points)} points"
            )

            # Cost guardrail checkpoint
            if _check_cost_guardrail(cost_tracker, storage):
                return None

            # -- Round 1: Author response --
            console.print(
                Panel(
                    "[bold]Round 1:[/bold] Author responding to reviewer findings...",
                    style="blue",
                )
            )
            grouped_text = _format_groups_for_author(groups)
            round1_author_prompt = build_round1_author_prompt(
                "plan", content, grouped_text
            )

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
            storage.log("Round 1: sending grouped feedback to author")
            author_raw, author_usage = await call_with_retry(
                client,
                author,
                "",
                round1_author_prompt,
                AUTHOR_RESPONSE_MAX_OUTPUT_TOKENS,
                log_fn=storage.log,
            )
            cost_tracker.add(
                author.name,
                author_usage["input_tokens"],
                author_usage["output_tokens"],
                author.cost_per_1k_input,
                author.cost_per_1k_output,
            )
            console.print(
                f"  Author responded ({author_usage['output_tokens']} tokens)"
            )
            storage.log(
                f"Round 1: author responded ({author_usage['output_tokens']} output tokens)"
            )
            storage.save_intermediate(
                review_id, "round2", "author_raw.txt", author_raw
            )

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
                return None

            # -- Round 2: Reviewer rebuttal + Author final response --
            all_rebuttals, author_final_responses, _ = (
                await _run_round2_exchange(
                    client,
                    "plan",
                    content,
                    groups,
                    author_responses,
                    grouped_text,
                    author,
                    active_reviewers,
                    cost_tracker,
                    storage,
                    review_id,
                )
            )

            # -- Governance --
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
                "plan",
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
                mode="plan",
                input_file=str(primary_file),
                project=project,
                timestamp=timestamp,
                author_model=author.name,
                reviewer_models=[r.name for r in active_reviewers],
                dedup_model=dedup_model.name,
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
            storage.save_review_artifacts(review_id, report_str, ledger_dict, round1_data, round2_data)

            # Persist original content for dvad revise
            rd = storage.review_dir(review_id)
            storage._atomic_write(rd / "original_content.txt", content)

            # -- Revision (post-governance) --
            has_actionable = any(
                d.governance_resolution in ("auto_accepted", "accepted", "overridden")
                for d in decisions
            )
            if has_actionable:
                console.print(
                    Panel(
                        "[bold]Revision:[/bold] Generating revised artifact...",
                        style="blue",
                    )
                )
                try:
                    revised_output = await run_revision(
                        client,
                        revision_model,
                        content,
                        ledger_dict,
                        mode="plan",
                        cost_tracker=cost_tracker,
                        storage=storage,
                        review_id=review_id,
                    )
                    if revised_output:
                        storage._atomic_write(rd / "revised-plan.md", revised_output)
                        console.print(
                            f"  Revised plan saved ({len(revised_output):,} chars)"
                        )
                except Exception as e:
                    console.print(
                        f"  [yellow]Warning: Revision failed: {e}[/yellow]"
                    )
                    storage.log(f"Revision failed (non-fatal): {e}")
            else:
                console.print("  [dim]No actionable findings — skipping revision[/dim]")

        console.print(f"\n[green]Review complete.[/green] Results saved to:")
        console.print(f"  Report:  {rd / 'dvad-report.md'}")
        console.print(f"  Ledger:  {rd / 'review-ledger.json'}")
        if (rd / "revised-plan.md").exists():
            console.print(f"  Revised: {rd / 'revised-plan.md'}")

        # Print summary table
        _print_summary_table(result)
        return result

    finally:
        storage.release_lock()
        storage.close()
