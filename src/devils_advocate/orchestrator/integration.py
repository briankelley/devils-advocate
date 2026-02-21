"""Integration review orchestrator."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import httpx
from rich.panel import Panel

from ..types import (
    CostTracker,
    ReviewContext,
    ReviewGroup,
    ReviewResult,
)
from ..ids import assign_guids, generate_review_id
from ..cost import check_context_window, estimate_tokens
from ..config import get_models_by_role
from ..providers import AUTHOR_RESPONSE_MAX_OUTPUT_TOKENS, MAX_OUTPUT_TOKENS, call_with_retry
from ..prompts import (
    build_integration_prompt,
    build_round1_author_prompt,
    get_reviewer_system_prompt,
)
from ..parser import parse_author_response, parse_review_response
from ..revision import run_revision
from ..normalization import normalize_review_response
from ..output import generate_ledger, generate_report
from ..storage import StorageManager
from ..ui import console

from ._common import (
    _apply_governance_or_escalate,
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


async def run_integration_review(
    config: dict,
    project: str,
    input_files: list | None = None,
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

    review_id = generate_review_id(combined)
    storage.set_review_id(review_id)
    timestamp = datetime.now(timezone.utc).isoformat()
    cost_tracker = CostTracker(max_cost=max_cost)
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
        return None

    if not storage.acquire_lock():
        console.print("[red]Error:[/red] Lock held.")
        return None

    try:
        revision_model = roles["revision"]

        async with httpx.AsyncClient() as client:
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
                MAX_OUTPUT_TOKENS,
                log_fn=storage.log,
            )
            cost_tracker.add(
                integ_reviewer.name,
                usage["input_tokens"],
                usage["output_tokens"],
                integ_reviewer.cost_per_1k_input,
                integ_reviewer.cost_per_1k_output,
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
                )

            if not points:
                console.print("[yellow]No integration issues found.[/yellow]")
                return None

            console.print(f"  {len(points)} integration points identified")

            # For integration review, each point is its own group (single reviewer)
            groups: list[ReviewGroup] = []
            for i, p in enumerate(points):
                gid = ctx.make_group_id(i + 1)
                p.point_id = ctx.make_point_id(gid, 1)
                groups.append(
                    ReviewGroup(
                        group_id=gid,
                        concern=p.description,
                        points=[p],
                        combined_severity=p.severity,
                        combined_category=p.category,
                        source_reviewers=[integ_reviewer.name],
                    )
                )
            assign_guids(groups)

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
                "integration", combined, grouped_text
            )

            console.print(
                f"  Prompt size: ~{estimate_tokens(round1_author_prompt)} tokens"
            )
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
            storage.save_intermediate(
                review_id, "round2", "author_raw.txt", author_raw
            )

            author_responses = parse_author_response(
                author_raw, groups, log_fn=storage.log
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
                    "integration",
                    combined,
                    groups,
                    author_responses,
                    grouped_text,
                    author,
                    [integ_reviewer],
                    cost_tracker,
                    storage,
                    review_id,
                )
            )

            # Governance
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
                "integration",
                parsed_count,
                total_count,
                storage,
            )

            _print_governance_summary(decisions)

            summary = _compute_summary(decisions, groups)

            result = ReviewResult(
                review_id=review_id,
                mode="integration",
                input_file=", ".join(files_to_review.keys()),
                project=project,
                timestamp=timestamp,
                author_model=author.name,
                reviewer_models=[integ_reviewer.name],
                dedup_model=dedup_model.name,
                points=[asdict(p) for p in points],
                groups=groups,
                author_responses=author_responses,
                governance_decisions=decisions,
                rebuttals=all_rebuttals,
                author_final_responses=author_final_responses,
                cost=cost_tracker,
                revised_output="",
                summary=summary,
            )

            report_str = generate_report(result)
            ledger_dict = generate_ledger(result)
            round1_data = {
                "points": [asdict(p) for p in points],
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
            storage._atomic_write(rd / "original_content.txt", combined)

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
                        combined,
                        ledger_dict,
                        mode="integration",
                        cost_tracker=cost_tracker,
                        storage=storage,
                        review_id=review_id,
                    )
                    if revised_output:
                        storage._atomic_write(rd / "remediation-plan.md", revised_output)
                        console.print(
                            f"  Remediation plan saved ({len(revised_output):,} chars)"
                        )
                except Exception as e:
                    console.print(
                        f"  [yellow]Warning: Revision failed: {e}[/yellow]"
                    )
                    storage.log(f"Revision failed (non-fatal): {e}")
            else:
                console.print("  [dim]No actionable findings — skipping revision[/dim]")

        console.print(f"\n[green]Integration review complete.[/green]")
        console.print(f"  Report: {rd / 'dvad-report.md'}")
        if (rd / "remediation-plan.md").exists():
            console.print(f"  Remediation: {rd / 'remediation-plan.md'}")
        _print_summary_table(result)
        return result

    finally:
        storage.release_lock()
        storage.close()
