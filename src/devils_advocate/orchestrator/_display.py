"""Display helpers for review orchestrators.

Console output for dry-run summaries, post-review summary tables,
and governance resolution counts.
"""

from __future__ import annotations

from rich.panel import Panel
from rich.table import Table

from ..types import (
    GovernanceDecision,
    ModelConfig,
    ReviewResult,
)
from ..cost import estimate_cost, estimate_tokens
from ..providers import (
    AUTHOR_RESPONSE_MAX_OUTPUT_TOKENS,
    MAX_OUTPUT_TOKENS,
    REVISION_MAX_OUTPUT_TOKENS,
)
from ..ui import console


def _estimate_total_cost(
    content: str,
    author: ModelConfig,
    reviewers: list[ModelConfig],
    dedup: ModelConfig,
    revision_model: ModelConfig | None = None,
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
    total += estimate_cost(author, input_tokens * 2, AUTHOR_RESPONSE_MAX_OUTPUT_TOKENS)
    # Round 2: reviewer rebuttal (same reviewers, similar input size)
    for r in reviewers:
        total += estimate_cost(r, input_tokens * 2, MAX_OUTPUT_TOKENS)
    # Round 2: author final response (estimated -- only triggered if challenges)
    total += estimate_cost(author, input_tokens * 2, AUTHOR_RESPONSE_MAX_OUTPUT_TOKENS // 2)
    # Revision (post-governance)
    rev = revision_model or author
    total += estimate_cost(rev, input_tokens * 2, REVISION_MAX_OUTPUT_TOKENS)
    return total


def _build_dry_run_estimate_rows(
    content: str,
    author: ModelConfig,
    reviewers: list[ModelConfig],
    dedup: ModelConfig,
    revision_model: ModelConfig | None = None,
) -> list[dict]:
    """Build cost estimate rows for dry run display (CLI table + GUI details page)."""
    rows = []
    input_tokens = estimate_tokens(content)

    for r in reviewers:
        cost = estimate_cost(r, input_tokens, MAX_OUTPUT_TOKENS)
        rows.append({
            "step": "Round 1 (review)",
            "model": r.name,
            "est_input_tokens": input_tokens,
            "est_output_tokens": MAX_OUTPUT_TOKENS,
            "est_cost_usd": round(cost, 6),
        })

    rows.append({
        "step": "Normalization (if needed)",
        "model": author.name,
        "est_input_tokens": MAX_OUTPUT_TOKENS,
        "est_output_tokens": MAX_OUTPUT_TOKENS,
        "est_cost_usd": round(estimate_cost(author, MAX_OUTPUT_TOKENS, MAX_OUTPUT_TOKENS), 6),
    })

    dedup_in = input_tokens // 2
    rows.append({
        "step": "Deduplication",
        "model": dedup.name,
        "est_input_tokens": dedup_in,
        "est_output_tokens": MAX_OUTPUT_TOKENS // 2,
        "est_cost_usd": round(estimate_cost(dedup, dedup_in, MAX_OUTPUT_TOKENS // 2), 6),
    })

    r2_in = input_tokens * 2
    rows.append({
        "step": "Round 1 (author response)",
        "model": author.name,
        "est_input_tokens": r2_in,
        "est_output_tokens": AUTHOR_RESPONSE_MAX_OUTPUT_TOKENS,
        "est_cost_usd": round(estimate_cost(author, r2_in, AUTHOR_RESPONSE_MAX_OUTPUT_TOKENS), 6),
    })

    for r in reviewers:
        rows.append({
            "step": "Round 2 (rebuttal)",
            "model": r.name,
            "est_input_tokens": r2_in,
            "est_output_tokens": MAX_OUTPUT_TOKENS,
            "est_cost_usd": round(estimate_cost(r, r2_in, MAX_OUTPUT_TOKENS), 6),
        })

    rows.append({
        "step": "Round 2 (author final, if challenges)",
        "model": author.name,
        "est_input_tokens": r2_in,
        "est_output_tokens": AUTHOR_RESPONSE_MAX_OUTPUT_TOKENS // 2,
        "est_cost_usd": round(estimate_cost(author, r2_in, AUTHOR_RESPONSE_MAX_OUTPUT_TOKENS // 2), 6),
    })

    rev = revision_model or author
    rows.append({
        "step": "Revision (post-governance)",
        "model": rev.name,
        "est_input_tokens": r2_in,
        "est_output_tokens": REVISION_MAX_OUTPUT_TOKENS,
        "est_cost_usd": round(estimate_cost(rev, r2_in, REVISION_MAX_OUTPUT_TOKENS), 6),
    })

    return rows


def _print_dry_run(
    mode: str,
    content: str,
    author: ModelConfig,
    reviewers: list[ModelConfig],
    dedup: ModelConfig,
    max_cost: float | None,
    revision_model: ModelConfig | None = None,
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

    rows = _build_dry_run_estimate_rows(content, author, reviewers, dedup, revision_model)
    for row in rows:
        table.add_row(
            row["step"],
            row["model"],
            str(row["est_input_tokens"]),
            str(row["est_output_tokens"]),
            f"${row['est_cost_usd']:.4f}",
        )

    console.print(table)

    total = _estimate_total_cost(content, author, reviewers, dedup, revision_model)
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
        if res == "auto_accepted":
            color = "green"
        elif res == "escalated":
            color = "yellow"
        elif res == "auto_dismissed":
            color = "cyan"
        else:
            color = "red"
        console.print(f"  [{color}]{label}: {count}[/{color}]")
