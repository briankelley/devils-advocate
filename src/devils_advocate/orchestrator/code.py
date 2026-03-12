"""Code review orchestrator."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from rich.panel import Panel

from ..types import (
    CostTracker,
    ReviewContext,
    ReviewPoint,
    ReviewResult,
)
from ..ids import assign_guids, generate_review_id
from ..cost import check_context_window
from ..config import get_models_by_role
from ..prompts import build_review_prompt
from ..dedup import deduplicate_points
from ..storage import StorageManager
from ..ui import console

from ._common import (
    _build_dry_run_estimate_rows,
    _build_role_assignments,
    _call_reviewer,
    _check_cost_guardrail,
    _estimate_total_cost,
    _group_to_dict,
    _print_dry_run,
    _promote_points_to_groups,
    _save_stub_ledger,
)
from ._pipeline import PipelineInputs, _run_adversarial_pipeline


async def run_code_review(
    config: dict,
    input_file: Path,
    project: str,
    spec_file: Path | None = None,
    max_cost: float | None = None,
    dry_run: bool = False,
    storage: StorageManager | None = None,
) -> ReviewResult | None:
    """Full code review orchestration."""
    roles = get_models_by_role(config)
    author = roles["author"]
    reviewers = roles["reviewers"]
    dedup_model = roles["dedup"]
    normalization_model = roles["normalization"]
    if storage is None:
        storage = StorageManager(Path.cwd())

    content = input_file.read_text()
    spec_content = spec_file.read_text() if spec_file else None
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

    storage.log(f"Starting code review for project '{project}'")
    storage.log(f"Input: {input_file} ({len(content)} chars)")
    if spec_file:
        storage.log(f"Spec: {spec_file}")

    review_prompt = build_review_prompt("code", content, spec_content)

    # Pre-flight (Bug 3 fix: active_reviewers accumulator)
    active_reviewers = []
    for r in reviewers:
        fits, est, limit = check_context_window(r, review_prompt)
        if not fits:
            console.print(
                f"[yellow]Warning:[/yellow] Skipping {r.name}: "
                f"input ({est} tokens) exceeds context ({limit})"
            )
            storage.log(f"Skipping {r.name}: context exceeded")
        else:
            active_reviewers.append(r)

    if len(active_reviewers) < 1:
        console.print("[red]Error:[/red] No reviewers available.")
        return None

    if dry_run:
        _print_dry_run("code", content, author, active_reviewers, dedup_model, max_cost)
        revision_model = roles["revision"]
        cost_estimate_rows = _build_dry_run_estimate_rows(
            content, author, active_reviewers, dedup_model, revision_model,
        )
        role_assignments = _build_role_assignments(roles, active_reviewers)
        _save_stub_ledger(
            storage, review_id, "code", project, str(input_file),
            "dry_run", timestamp=timestamp, role_assignments=role_assignments,
            cost_estimate_rows=cost_estimate_rows,
        )
        return None

    if max_cost is not None:
        est_cost = _estimate_total_cost(content, author, active_reviewers, dedup_model)
        if est_cost > max_cost:
            console.print(
                f"[red]Error:[/red] Estimated cost ${est_cost:.4f} exceeds limit."
            )
            role_assignments = _build_role_assignments(roles, active_reviewers)
            _save_stub_ledger(
                storage, review_id, "code", project, str(input_file),
                "cost_exceeded", timestamp=timestamp, est_cost=est_cost,
                role_assignments=role_assignments,
            )
            return None

    if not storage.acquire_lock():
        storage.log("Lock acquisition failed — another review is running or a stale lock exists")
        console.print("[red]Error:[/red] Lock held by another process.")
        return None

    try:
        all_points: list[ReviewPoint] = []
        revision_model = roles["revision"]

        from ..http import make_async_client

        async with make_async_client() as client:
            # Round 1
            console.print(
                Panel("[bold]Round 1:[/bold] Sending to reviewers...", style="blue")
            )
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
                    mode="code",
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
                all_points.extend(result)
                console.print(f"  {r.name}: {len(result)} review points")
                storage.save_intermediate(
                    review_id,
                    "round1",
                    f"{r.name}_parsed.json",
                    [asdict(p) for p in result],
                )

            if not all_points:
                console.print("[red]Error:[/red] No review points. Aborting.")
                return None

            # Cost guardrail checkpoint
            if _check_cost_guardrail(cost_tracker, storage):
                return None

            console.print(f"  Total review points: {len(all_points)}")

            # Dedup
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
                console.print(Panel("[bold]Deduplication...[/bold]", style="blue"))
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
                f"  {len(groups)} groups from {len(all_points)} points"
            )

            # Cost guardrail checkpoint
            if _check_cost_guardrail(cost_tracker, storage):
                return None

            # -- Shared pipeline: author response -> round 2 -> governance -> revision --
            return await _run_adversarial_pipeline(
                client,
                PipelineInputs(
                    mode="code",
                    content=content,
                    input_file_label=str(input_file),
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
                    revision_filename=f"revised-{input_file.name}",
                    reviewer_roles={
                        r.name: f"reviewer_{i+1}"
                        for i, r in enumerate(active_reviewers)
                    },
                ),
            )

    finally:
        storage.release_lock()
        storage.close()
