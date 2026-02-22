"""Spec review orchestrator — collaborative ideation mode.

Unlike plan/code/integration modes, spec mode is NOT adversarial. It asks
remote LLMs to enrich a specification with suggestions, then groups them
by theme with consensus indicators. The pipeline is:

    Reviewers (parallel, single pass) -> Dedup (consensus) -> Revision (themed report)

Skipped entirely: author response, rebuttal, governance, escalation.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import httpx
from rich.panel import Panel

from ..types import (
    CostTracker,
    ReviewContext,
    ReviewPoint,
    ReviewResult,
)
from ..ids import assign_guids, generate_review_id
from ..cost import check_context_window, estimate_cost, estimate_tokens
from ..config import get_models_by_role
from ..providers import MAX_OUTPUT_TOKENS
from ..prompts import (
    build_spec_review_prompt,
    get_spec_reviewer_system_prompt,
)
from ..parser import parse_spec_response
from ..revision import run_spec_revision, REVISION_MAX_OUTPUT_TOKENS
from ..dedup import deduplicate_points
from ..output import generate_report, generate_ledger
from ..storage import StorageManager
from ..ui import console

from ._common import (
    _call_reviewer,
    _check_cost_guardrail,
    _group_to_dict,
    _print_summary_table,
)


async def run_spec_review(
    config: dict,
    input_files: list[Path],
    project: str,
    max_cost: float | None = None,
    dry_run: bool = False,
    storage: StorageManager | None = None,
) -> ReviewResult | None:
    """Full spec review orchestration — collaborative ideation."""
    roles = get_models_by_role(config)
    reviewers = roles["reviewers"]
    dedup_model = roles["dedup"]
    normalization_model = roles["normalization"]
    revision_model = roles["revision"]
    if storage is None:
        storage = StorageManager(Path.cwd())

    primary_file = input_files[0]
    primary_content = primary_file.read_text()

    # Support multiple input files (primary + reference context)
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
            f"=== PRIMARY SPECIFICATION (under review) ===\n"
            f"{primary_content}\n"
            f"=== END PRIMARY SPECIFICATION ===\n\n"
            f"The following files are provided as REFERENCE CONTEXT. Do not review\n"
            f"these directly — they provide background for the primary specification.\n\n"
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

    storage.log(f"Starting spec review for project '{project}'")
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
        f"Reviewers: {', '.join(r.name for r in reviewers)}"
    )
    storage.log(f"Dedup: {dedup_model.name}, Revision: {revision_model.name}")

    # Pre-flight: context window checks
    review_prompt = build_spec_review_prompt(content)
    spec_system_prompt = get_spec_reviewer_system_prompt()
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
        _print_spec_dry_run(content, active_reviewers, dedup_model, revision_model, max_cost)
        return None

    # Cost estimate
    if max_cost is not None:
        est_cost = _estimate_spec_cost(content, active_reviewers, dedup_model, revision_model)
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
        # -- Reviewers: parallel single pass --
        console.print(
            Panel("[bold]Spec Review:[/bold] Sending to reviewers...", style="blue")
        )
        all_points: list[ReviewPoint] = []

        async with httpx.AsyncClient() as client:
            tasks = [
                _call_reviewer(
                    client,
                    r,
                    normalization_model,
                    review_prompt,
                    review_id,
                    cost_tracker,
                    storage,
                    system_prompt=spec_system_prompt,
                    point_parser=parse_spec_response,
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
                console.print(f"  {r.name}: {len(points)} suggestions")
                storage.log(f"Parsed {len(points)} suggestions from {r.name}")
                storage.save_intermediate(
                    review_id,
                    "round1",
                    f"{r.name}_parsed.json",
                    [asdict(p) for p in points],
                )

            if not all_points:
                console.print(
                    "[red]Error:[/red] No suggestions from any reviewer. Aborting."
                )
                return None

            if _check_cost_guardrail(cost_tracker, storage):
                return None

            console.print(f"  Total suggestions: {len(all_points)}")

            # -- Deduplication with consensus --
            console.print(
                Panel(
                    "[bold]Deduplication:[/bold] Grouping suggestions by theme...",
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
                mode="spec",
            )
            assign_guids(groups)
            storage.save_intermediate(
                review_id,
                "round1",
                "deduplication.json",
                [_group_to_dict(g) for g in groups],
            )

            # Count consensus
            multi_source = sum(1 for g in groups if len(g.source_reviewers) > 1)
            console.print(
                f"  {len(groups)} suggestion groups from {len(all_points)} suggestions "
                f"({multi_source} with multi-reviewer consensus)"
            )

            if _check_cost_guardrail(cost_tracker, storage):
                return None

            # Build result (no governance, no author responses, no rebuttals)
            summary = {
                "total_groups": len(groups),
                "total_points": sum(len(g.points) for g in groups),
                "multi_consensus": multi_source,
                "single_source": len(groups) - multi_source,
            }

            result = ReviewResult(
                review_id=review_id,
                mode="spec",
                input_file=str(primary_file),
                project=project,
                timestamp=timestamp,
                author_model="",  # No author in spec mode
                reviewer_models=[r.name for r in active_reviewers],
                dedup_model=dedup_model.name,
                points=[asdict(p) for p in all_points],
                groups=groups,
                author_responses=[],
                governance_decisions=[],
                rebuttals=[],
                author_final_responses=[],
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
            storage.save_review_artifacts(
                review_id, report_str, ledger_dict, round1_data, {}
            )

            # Persist original content
            rd = storage.review_dir(review_id)
            storage._atomic_write(rd / "original_content.txt", content)

            # -- Revision: compile themed suggestion report --
            console.print(
                Panel(
                    "[bold]Revision:[/bold] Compiling suggestion report...",
                    style="blue",
                )
            )
            try:
                revised_output = await run_spec_revision(
                    client,
                    revision_model,
                    content,
                    groups,
                    len(active_reviewers),
                    cost_tracker,
                    storage,
                    review_id,
                )
                if revised_output:
                    result.revised_output = revised_output
                    storage._atomic_write(rd / "revised-spec-suggestions.md", revised_output)
                    console.print(
                        f"  Suggestion report saved ({len(revised_output):,} chars)"
                    )

                    # Re-save report with revised output included
                    report_str = generate_report(result)
                    storage._atomic_write(rd / "dvad-report.md", report_str)
            except Exception as e:
                console.print(
                    f"  [yellow]Warning: Revision failed: {e}[/yellow]"
                )
                storage.log(f"Revision failed (non-fatal): {e}")

        console.print(f"\n[green]Spec review complete.[/green] Results saved to:")
        console.print(f"  Report:  {rd / 'dvad-report.md'}")
        console.print(f"  Ledger:  {rd / 'review-ledger.json'}")
        if (rd / "revised-spec-suggestions.md").exists():
            console.print(f"  Suggestions: {rd / 'revised-spec-suggestions.md'}")

        _print_spec_summary_table(result)
        return result

    finally:
        storage.release_lock()
        storage.close()


# ---- Spec-specific helpers --------------------------------------------------


def _estimate_spec_cost(
    content: str,
    reviewers: list,
    dedup,
    revision_model,
) -> float:
    """Rough cost estimate for spec mode (no round 2)."""
    input_tokens = estimate_tokens(content)
    est_output = min(input_tokens, MAX_OUTPUT_TOKENS)
    total = 0.0
    for r in reviewers:
        total += estimate_cost(r, input_tokens, est_output)
    total += estimate_cost(dedup, input_tokens, est_output // 2)
    total += estimate_cost(revision_model, input_tokens * 2, REVISION_MAX_OUTPUT_TOKENS)
    return total


def _print_spec_dry_run(
    content: str,
    reviewers: list,
    dedup,
    revision_model,
    max_cost: float | None,
) -> None:
    """Print a spec-mode dry-run summary."""
    from rich.table import Table

    console.print(
        Panel(
            "[bold yellow]DRY RUN[/bold yellow] -- No API calls will be made",
            style="yellow",
        )
    )
    table = Table(title="Planned API Calls (Spec Mode)")
    table.add_column("Step", style="cyan")
    table.add_column("Model", style="green")
    table.add_column("Est. Input Tokens")
    table.add_column("Est. Output Tokens")
    table.add_column("Est. Cost (USD)")

    input_tokens = estimate_tokens(content)

    for r in reviewers:
        cost = estimate_cost(r, input_tokens, MAX_OUTPUT_TOKENS)
        table.add_row(
            "Reviewer (suggestions)",
            r.name,
            str(input_tokens),
            str(MAX_OUTPUT_TOKENS),
            f"${cost:.4f}",
        )

    dedup_in = input_tokens // 2
    cost_d = estimate_cost(dedup, dedup_in, MAX_OUTPUT_TOKENS // 2)
    table.add_row(
        "Deduplication (consensus)",
        dedup.name,
        str(dedup_in),
        str(MAX_OUTPUT_TOKENS // 2),
        f"${cost_d:.4f}",
    )

    cost_rev = estimate_cost(revision_model, input_tokens * 2, REVISION_MAX_OUTPUT_TOKENS)
    table.add_row(
        "Revision (suggestion report)",
        revision_model.name,
        str(input_tokens * 2),
        str(REVISION_MAX_OUTPUT_TOKENS),
        f"${cost_rev:.4f}",
    )

    console.print(table)

    total = _estimate_spec_cost(content, reviewers, dedup, revision_model)
    console.print(f"\nEstimated total cost: [bold]${total:.4f}[/bold]")
    if max_cost:
        color = "green" if total <= max_cost else "red"
        console.print(f"Cost limit: [{color}]${max_cost:.2f}[/{color}]")


def _print_spec_summary_table(result: ReviewResult) -> None:
    """Print a post-spec-review summary table."""
    from rich.table import Table

    table = Table(title="Spec Review Summary")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", justify="right")

    table.add_row("Total Suggestions", str(result.summary.get("total_points", 0)))
    table.add_row("Suggestion Groups", str(result.summary.get("total_groups", 0)))
    table.add_row(
        "[green]Multi-Reviewer Consensus[/green]",
        str(result.summary.get("multi_consensus", 0)),
    )
    table.add_row(
        "[dim]Single Source[/dim]",
        str(result.summary.get("single_source", 0)),
    )
    table.add_row("[bold]Total Cost[/bold]", f"${result.cost.total_usd:.4f}")
    console.print(table)
