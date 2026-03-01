"""Integration review orchestrator."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import httpx
from rich.panel import Panel

from ..types import (
    CostTracker,
    ReviewContext,
    ReviewResult,
)
from ..ids import assign_guids, generate_review_id
from ..cost import check_context_window
from ..config import get_models_by_role
from ..providers import MAX_OUTPUT_TOKENS, call_with_retry
from ..prompts import build_integration_prompt, get_reviewer_system_prompt
from ..parser import parse_review_response
from ..normalization import normalize_review_response
from ..storage import StorageManager
from ..ui import console

from ._common import (
    PipelineInputs,
    _build_dry_run_estimate_rows,
    _build_role_assignments,
    _check_cost_guardrail,
    _estimate_total_cost,
    _print_dry_run,
    _promote_points_to_groups,
    _run_adversarial_pipeline,
    _save_stub_ledger,
)


async def run_integration_review(
    config: dict,
    project: str,
    input_files: list[str | Path] | None = None,
    spec_file: Path | None = None,
    project_dir: Path | None = None,
    max_cost: float | None = None,
    dry_run: bool = False,
    storage: StorageManager | None = None,
) -> ReviewResult | None:
    """Integration review across completed project files."""
    roles = get_models_by_role(config)
    author = roles["author"]
    integ_reviewer = roles["integration"]
    dedup_model = roles["dedup"]
    normalization_model = roles["normalization"]
    if storage is None:
        storage = StorageManager(Path.cwd())

    if not integ_reviewer:
        console.print(
            "[red]Error:[/red] No integration_reviewer model configured."
        )
        return None

    # Discover files
    files_to_review: dict[str, str] = {}

    # Integration spec discovery (per plan)
    spec_content = ""
    if spec_file:
        spec_content = spec_file.read_text()
    elif project_dir and (project_dir / "000-strategic-summary.md").exists():
        spec_content = (project_dir / "000-strategic-summary.md").read_text()
    elif project_dir and (project_dir / "strategic-summary.md").exists():
        spec_content = (project_dir / "strategic-summary.md").read_text()

    if input_files:
        for fp in input_files:
            p = Path(fp)
            if p.exists():
                files_to_review[str(p)] = p.read_text()
    else:
        manifest = storage.load_manifest()
        if manifest:
            for task in manifest.get("tasks", []):
                if task.get("status") == "completed":
                    for fp in task.get("files", []):
                        p = Path(fp)
                        if not p.is_absolute() and project_dir:
                            p = project_dir / p
                        if p.exists():
                            files_to_review[str(p)] = p.read_text()
            # Look for strategic summary from manifest dir if not yet found
            if not spec_content and project_dir:
                summary_path = project_dir / "000-strategic-summary.md"
                if summary_path.exists():
                    spec_content = summary_path.read_text()
        else:
            console.print(
                "[red]Error:[/red] No manifest.json found and no --input files specified."
            )
            console.print("  Create .dvad/manifest.json or pass files via --input.")
            return None

    if not files_to_review:
        console.print("[red]Error:[/red] No files to review.")
        return None

    # Build combined content
    file_sections = []
    for path, file_content in files_to_review.items():
        file_sections.append(
            f"--- {path} ---\n{file_content}\n--- END {path} ---"
        )
    combined = "\n\n".join(file_sections)

    # Reuse review_id from caller (GUI runner) if already set, otherwise generate
    review_id = storage.current_review_id or generate_review_id(combined)
    storage.set_review_id(review_id)
    timestamp = datetime.now(timezone.utc).isoformat()
    cost_tracker = CostTracker(max_cost=max_cost, _log_fn=storage.log)
    review_start_time = datetime.now(timezone.utc)
    ctx = ReviewContext(
        project=project,
        review_id=review_id,
        review_start_time=review_start_time,
    )

    storage.log(f"Starting integration review for project '{project}'")
    storage.log(f"Files: {', '.join(files_to_review.keys())}")

    prompt = build_integration_prompt(
        combined, spec_content or "(No strategic overview available)"
    )

    fits, est, limit = check_context_window(integ_reviewer, prompt)
    if not fits:
        console.print(
            f"[red]Error:[/red] Combined content ({est} tokens) exceeds "
            f"{integ_reviewer.name} context ({limit}). Chunking deferred to v2."
        )
        return None

    if dry_run:
        _print_dry_run(
            "integration",
            combined,
            author,
            [integ_reviewer],
            dedup_model,
            max_cost,
        )
        revision_model = roles["revision"]
        cost_estimate_rows = _build_dry_run_estimate_rows(
            combined, author, [integ_reviewer], dedup_model, revision_model,
        )
        role_assignments = _build_role_assignments(roles, [integ_reviewer])
        role_assignments["integration"] = integ_reviewer.name
        _save_stub_ledger(
            storage, review_id, "integration", project,
            ", ".join(files_to_review.keys()),
            "dry_run", timestamp=timestamp, role_assignments=role_assignments,
            cost_estimate_rows=cost_estimate_rows,
        )
        return None

    # Pre-flight cost estimate (abort before any LLM calls if over budget)
    if max_cost is not None:
        est_cost = _estimate_total_cost(combined, author, [integ_reviewer], dedup_model)
        if est_cost > max_cost:
            console.print(
                f"[red]Error:[/red] Estimated cost ${est_cost:.4f} exceeds "
                f"--max-cost ${max_cost:.2f}. Aborting."
            )
            role_assignments = _build_role_assignments(roles, [integ_reviewer])
            role_assignments["integration"] = integ_reviewer.name
            _save_stub_ledger(
                storage, review_id, "integration", project,
                ", ".join(files_to_review.keys()),
                "cost_exceeded", timestamp=timestamp, est_cost=est_cost,
                role_assignments=role_assignments,
            )
            return None
        storage.log(f"Estimated cost: ${est_cost:.4f} (limit: ${max_cost:.2f})")

    if not storage.acquire_lock():
        console.print("[red]Error:[/red] Lock held.")
        return None

    try:
        revision_model = roles["revision"]

        from ..http import make_async_client

        async with make_async_client() as client:
            console.print(
                Panel(
                    "[bold]Integration Review:[/bold] Analyzing codebase...",
                    style="blue",
                )
            )

            text, usage = await call_with_retry(
                client,
                integ_reviewer,
                get_reviewer_system_prompt(),
                prompt,
                integ_reviewer.max_out_configured or MAX_OUTPUT_TOKENS,
                log_fn=storage.log,
                mode="integration",
            )
            cost_tracker.add(
                integ_reviewer.name,
                usage["input_tokens"],
                usage["output_tokens"],
                integ_reviewer.cost_per_1k_input,
                integ_reviewer.cost_per_1k_output,
                role="reviewer_1",
            )
            storage.save_intermediate(
                review_id, "round1", f"{integ_reviewer.name}_raw.txt", text
            )

            points = parse_review_response(text, integ_reviewer.name)
            if not points:
                points = await normalize_review_response(
                    client,
                    text,
                    normalization_model,
                    integ_reviewer.name,
                    log_fn=storage.log,
                    cost_tracker=cost_tracker,
                    mode="integration",
                )

            if not points:
                console.print("[yellow]No integration issues found.[/yellow]")
                return None

            console.print(f"  {len(points)} integration points identified")

            # For integration review, each point is its own group (single reviewer)
            groups = _promote_points_to_groups(points, ctx)
            assign_guids(groups)

            # Cost guardrail checkpoint
            if _check_cost_guardrail(cost_tracker, storage):
                return None

            # -- Shared pipeline: author response -> round 2 -> governance -> revision --
            return await _run_adversarial_pipeline(
                client,
                PipelineInputs(
                    mode="integration",
                    content=combined,
                    input_file_label=", ".join(files_to_review.keys()),
                    project=project,
                    review_id=review_id,
                    timestamp=timestamp,
                    all_points=points,
                    groups=groups,
                    author=author,
                    active_reviewers=[integ_reviewer],
                    dedup_model=dedup_model,
                    revision_model=revision_model,
                    cost_tracker=cost_tracker,
                    storage=storage,
                    revision_filename="remediation-plan.md",
                    reviewer_roles={integ_reviewer.name: "reviewer_1"},
                ),
            )

    finally:
        storage.release_lock()
        storage.close()
