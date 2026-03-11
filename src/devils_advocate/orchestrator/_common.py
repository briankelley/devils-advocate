"""Shared helpers for review orchestrators.

Utility functions (reviewer calls, cost guardrails, stub ledgers) used by
individual orchestrator modules.  The multi-round adversarial pipeline lives
in ``_pipeline``.  Display and formatting helpers live in ``_display`` and
``_formatting`` respectively.
"""

from __future__ import annotations

from ..types import (
    CostTracker,
    ModelConfig,
    ReviewPoint,
)
from ..cost import estimate_tokens
from ..providers import (
    MAX_OUTPUT_TOKENS,
    call_with_retry,
)
from ..prompts import get_reviewer_system_prompt
from ..parser import parse_review_response
from ..normalization import normalize_review_response
from ..dedup import promote_points_to_groups as _promote_points_to_groups  # noqa: F401 — re-exported
from ..storage import StorageManager
from ..ui import console

# -- Re-exports ----------------------------------------------------------------
# Orchestrator modules (plan.py, code.py, integration.py, spec.py) import
# these names from ._common.  Pipeline-only helpers live in ``_pipeline``.

from ._display import (  # noqa: F401
    _build_dry_run_estimate_rows,
    _estimate_total_cost,
    _print_dry_run,
    _print_summary_table,
)
from ._formatting import _group_to_dict  # noqa: F401


def _call_info(model: ModelConfig, prompt: str, effective_max: int) -> str:
    """Build the standard parenthetical for 'calling' log lines."""
    sent = estimate_tokens(prompt)
    configured = model.max_out_configured or effective_max
    stated = model.max_out_stated or effective_max
    thinking_str = "on" if model.thinking else "off"
    return (
        f"sent: {sent}, timeout: {model.timeout}s, "
        f"max_out: {configured}/{stated}, thinking: {thinking_str}"
    )


def _save_stub_ledger(
    storage: StorageManager,
    review_id: str,
    mode: str,
    project: str,
    input_file_label: str,
    result: str,
    timestamp: str | None = None,
    est_cost: float = 0.0,
    cost_tracker: CostTracker | None = None,
    role_assignments: dict | None = None,
    cost_estimate_rows: list | None = None,
) -> None:
    """Save a minimal ledger for dry runs, cost-exceeded, cost-aborted, and failed reviews."""
    from datetime import datetime, timezone as tz

    ts = timestamp or datetime.now(tz.utc).isoformat()
    ledger = {
        "review_id": review_id,
        "result": result,
        "mode": mode,
        "input_file": input_file_label,
        "project": project,
        "timestamp": ts,
        "author_model": "",
        "reviewer_models": [],
        "dedup_model": "",
        "points": [],
        "summary": {
            "total_points": 0,
            "total_groups": 0,
        },
        "cost": {
            "total_usd": round(est_cost, 6),
            "breakdown": {},
            "role_costs": {},
        },
    }
    if cost_tracker is not None:
        ledger["cost"] = {
            "total_usd": round(cost_tracker.total_usd, 6),
            "breakdown": {k: round(v, 6) for k, v in cost_tracker.breakdown().items()},
            "role_costs": {k: round(v, 6) for k, v in cost_tracker.role_costs.items()},
        }
    # Save role assignments if provided
    if role_assignments is not None:
        ledger["author_model"] = role_assignments.get("author", "")
        ledger["reviewer_models"] = role_assignments.get("reviewers", [])
        ledger["dedup_model"] = role_assignments.get("dedup", "")
        ledger["role_assignments"] = role_assignments
    # Save cost estimate rows if provided (for dry run display)
    if cost_estimate_rows is not None:
        ledger["cost_estimate_rows"] = cost_estimate_rows
    storage.save_review_artifacts(review_id, "", ledger, {}, {})



def _build_role_assignments(roles: dict, active_reviewers: list) -> dict:
    """Build the role_assignments dict used in stub ledgers."""
    return {
        "author": roles["author"].name if roles.get("author") else "",
        "reviewers": [r.name for r in active_reviewers],
        "dedup": roles["dedup"].name if roles.get("dedup") else "",
        "normalization": roles["normalization"].name if roles.get("normalization") else "",
        "revision": roles["revision"].name if roles.get("revision") else "",
    }


# ---- Reviewer call -----------------------------------------------------------


async def _call_reviewer(
    client,
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
    effective_max = reviewer.max_out_configured or MAX_OUTPUT_TOKENS
    storage.log(
        f"Round 1: calling {reviewer.name} "
        f"({_call_info(reviewer, prompt, effective_max)})"
    )
    sys_prompt = system_prompt if system_prompt is not None else get_reviewer_system_prompt()
    text, usage = await call_with_retry(
        client,
        reviewer,
        sys_prompt,
        prompt,
        effective_max,
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
        f"Round 1: {reviewer.name} responded "
        f"(recv: {usage['output_tokens']})"
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
