"""Plan review orchestrator."""

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
from ..cost import check_context_window, estimate_tokens
from ..config import get_models_by_role
from ..prompts import build_review_prompt
from ..dedup import deduplicate_points
from ..storage import StorageManager
from ..ui import console

from ._common import (
    PipelineInputs,
    _build_dry_run_estimate_rows,
    _build_role_assignments,
    _call_reviewer,
    _check_cost_guardrail,
    _estimate_total_cost,
    _group_to_dict,
    _print_dry_run,
    _promote_points_to_groups,
    _run_adversarial_pipeline,
    _save_stub_ledger,
)


async def run_plan_review(
    config: dict,
    input_files: list[Path],
    project: str,
    max_cost: float | None = None,
    dry_run: bool = False,
    storage: StorageManager | None = None,
) -> ReviewResult | None:
    """Full plan review orchestration."""
    roles = get_models_by_role(config)
    author = roles["author"]
    reviewers = roles["reviewers"]
    dedup_model = roles["dedup"]
    normalization_model = roles["normalization"]
    if storage is None:
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

    review_id = storage.current_review_id or generate_review_id(content)
    storage.set_review_id(review_id)
    timestamp = datetime.now(timezone.utc).isoformat()
    cost_tracker = CostTracker(max_cost=max_cost, _log_fn=storage.log)
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
        revision_model = roles["revision"]
        cost_estimate_rows = _build_dry_run_estimate_rows(
            content, author, active_reviewers, dedup_model, revision_model,
        )
        role_assignments = _build_role_assignments(roles, active_reviewers)
        _save_stub_ledger(
            storage, review_id, "plan", project, str(primary_file),
            "dry_run", timestamp=timestamp, role_assignments=role_assignments,
            cost_estimate_rows=cost_estimate_rows,
        )
        return None

    # Cost estimate
    if max_cost is not None:
        est_cost = _estimate_total_cost(content, author, active_reviewers, dedup_model)
        if est_cost > max_cost:
            console.print(
                f"[red]Error:[/red] Estimated cost ${est_cost:.4f} exceeds "
                f"--max-cost ${max_cost:.2f}. Aborting."
            )
            role_assignments = _build_role_assignments(roles, active_reviewers)
            _save_stub_ledger(
                storage, review_id, "plan", project, str(primary_file),
                "cost_exceeded", timestamp=timestamp, est_cost=est_cost,
                role_assignments=role_assignments,
            )
            return None
        storage.log(f"Estimated cost: ${est_cost:.4f} (limit: ${max_cost:.2f})")

    # Acquire lock
    if not storage.acquire_lock():
        storage.log("Lock acquisition failed — another review is running or a stale lock exists")
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

        from ..http import make_async_client

        async with make_async_client() as client:
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
                    role_label=f"reviewer_{i+1}",
                    mode="plan",
                )
                for i, r in enumerate(active_reviewers)
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            succeeded_reviewers = 0
            for r, result in zip(active_reviewers, results):
                if isinstance(result, Exception):
                    console.print(f"  [red]x[/red] {r.name}: failed -- {result}")
                    storage.log(f"Reviewer {r.name} failed: {result}")
                    continue
                succeeded_reviewers += 1
                points = result
                all_points.extend(points)
                console.print(f"  {r.name}: {len(points)} review points")
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
            failed_reviewers = len(active_reviewers) - succeeded_reviewers
            if failed_reviewers > 0 and len(active_reviewers) > 1:
                console.print(
                    f"  [yellow]Skipping deduplication: {failed_reviewers} of "
                    f"{len(active_reviewers)} reviewers failed[/yellow]"
                )
                storage.log(
                    f"Skipping deduplication: {failed_reviewers} of "
                    f"{len(active_reviewers)} reviewers failed"
                )
                groups = _promote_points_to_groups(all_points, ctx)
                assign_guids(groups)
            else:
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
                storage.log(
                    f"  Deduplication: combined {len(all_points)} points into {len(groups)} groups"
                )
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

            # -- Shared pipeline: author response -> round 2 -> governance -> revision --
            return await _run_adversarial_pipeline(
                client,
                PipelineInputs(
                    mode="plan",
                    content=content,
                    input_file_label=str(primary_file),
                    project=project,
                    review_id=review_id,
                    timestamp=timestamp,
                    all_points=all_points,
                    groups=groups,
                    author=author,
                    active_reviewers=active_reviewers,
                    dedup_model=dedup_model,
                    revision_model=revision_model,
                    cost_tracker=cost_tracker,
                    storage=storage,
                    revision_filename="revised-plan.md",
                    reviewer_roles={
                        r.name: f"reviewer_{i+1}"
                        for i, r in enumerate(active_reviewers)
                    },
                ),
            )

    finally:
        storage.release_lock()
        storage.close()
