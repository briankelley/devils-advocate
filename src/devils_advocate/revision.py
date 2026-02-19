"""Isolated post-governance revision engine.

Produces a revised artifact (plan, diff, or remediation) using only the
final governance outcomes and the original artifact as input.
"""

from __future__ import annotations

import re
from collections import defaultdict

from .cost import check_context_window
from .prompts import load_template
from .providers import call_with_retry
from .types import CostTracker, ModelConfig
from .storage import StorageManager
from .ui import console

# Token limit for revision artifact generation
REVISION_MAX_OUTPUT_TOKENS = 64000

# Canonical delimiters per mode
_DELIMITERS = {
    "plan": ("=== REVISED PLAN ===", "=== END REVISED PLAN ==="),
    "code": ("=== UNIFIED DIFF ===", "=== END UNIFIED DIFF ==="),
    "integration": ("=== REMEDIATION PLAN ===", "=== END REMEDIATION PLAN ==="),
}

# Resolutions treated as actionable
_ACTIONABLE_RESOLUTIONS = frozenset({"auto_accepted", "accepted", "overridden"})


def build_revision_context(ledger_data: dict) -> str:
    """Build a slim text summary of governance outcomes for the revision prompt.

    Groups all ledger points by ``group_id``, categorizes each group as
    actionable, dismissed, or unresolved, and returns a text block the
    revision LLM can consume.
    """
    points = ledger_data.get("points", [])

    # Group points by group_id
    groups: dict[str, list[dict]] = defaultdict(list)
    for p in points:
        gid = p.get("group_id", "unknown")
        groups[gid].append(p)

    actionable: list[str] = []
    dismissed: list[str] = []
    unresolved: list[str] = []

    for gid, group_points in groups.items():
        # Determine final_resolution for the group: check consistency
        resolutions = set(p.get("final_resolution", "pending") for p in group_points)
        if len(resolutions) == 1:
            final_res = resolutions.pop()
        else:
            # Inconsistent resolutions within group — treat as unresolved
            final_res = "unresolved"

        # Collect distinct (description, recommendation, location, reviewer) tuples
        seen = set()
        items: list[str] = []
        for p in group_points:
            key = (
                p.get("description", ""),
                p.get("recommendation", ""),
                p.get("location", ""),
                p.get("reviewer", ""),
            )
            if key not in seen:
                seen.add(key)
                item_lines = [f"    - [{p.get('reviewer', '?')}] {p.get('description', '?')}"]
                if p.get("recommendation"):
                    item_lines.append(f"      Recommendation: {p['recommendation']}")
                if p.get("location"):
                    item_lines.append(f"      Location: {p['location']}")
                items.append("\n".join(item_lines))

        concern = group_points[0].get("concern", group_points[0].get("description", "?"))
        severity = group_points[0].get("severity", "medium")
        author_rationale = group_points[0].get("author_rationale", "")

        block_lines = [
            f"  GROUP {gid}:",
            f"    Concern: {concern}",
            f"    Severity: {severity}",
            f"    Resolution: {final_res}",
        ]
        if items:
            block_lines.append("    Recommendations:")
            block_lines.extend(items)
        if author_rationale:
            block_lines.append(f"    Author rationale: {author_rationale}")

        block = "\n".join(block_lines)

        if final_res in _ACTIONABLE_RESOLUTIONS:
            actionable.append(block)
        elif final_res in ("auto_dismissed",):
            dismissed.append(block)
        else:
            unresolved.append(block)

    sections = []
    if actionable:
        sections.append("=== ACCEPTED FINDINGS (incorporate these) ===")
        sections.extend(actionable)
        sections.append("")
    if dismissed:
        sections.append("=== DISMISSED FINDINGS (ignore these) ===")
        sections.extend(dismissed)
        sections.append("")
    if unresolved:
        sections.append("=== UNRESOLVED FINDINGS (ignore these) ===")
        sections.extend(unresolved)
        sections.append("")

    return "\n".join(sections)


def _extract_revision_strict(raw: str, mode: str) -> str:
    """Strict delimiter extractor for revision responses.

    Only accepts canonical delimiters — no fallback to PART 2 or
    markdown heading patterns. Returns empty string if not found.
    """
    start_delim, end_delim = _DELIMITERS.get(mode, _DELIMITERS["plan"])
    pattern = re.escape(start_delim) + r"(.*?)" + re.escape(end_delim)
    m = re.search(pattern, raw, re.DOTALL)
    return m.group(1).strip() if m else ""


def build_revision_prompt(
    mode: str,
    original_content: str,
    revision_context: str,
) -> str:
    """Build the revision prompt from a mode-specific template."""
    template_map = {
        "plan": "revision-plan-instruct.txt",
        "code": "revision-code-instruct.txt",
        "integration": "revision-integration-instruct.txt",
    }
    template_name = template_map.get(mode, template_map["plan"])
    return load_template(
        template_name,
        original_content=original_content,
        revision_context=revision_context,
    )


async def run_revision(
    client,
    revision_model: ModelConfig,
    original_content: str,
    ledger_data: dict,
    mode: str,
    cost_tracker: CostTracker,
    storage: StorageManager,
    review_id: str,
) -> str:
    """Run the isolated revision LLM call.

    Returns the extracted revised artifact, or empty string if revision
    is not needed or extraction fails. Callers should wrap this in
    try/except — revision failure is non-fatal.
    """
    # Build context from ledger
    revision_context = build_revision_context(ledger_data)

    # Check for actionable findings
    if "=== ACCEPTED FINDINGS" not in revision_context:
        console.print("  [dim]No actionable findings — skipping revision[/dim]")
        storage.log("Revision: no actionable findings — skipping")
        return ""

    # Build prompt
    prompt = build_revision_prompt(mode, original_content, revision_context)

    # Context window check
    fits, est, limit = check_context_window(revision_model, prompt)
    if not fits:
        console.print(
            f"  [yellow]Warning: Revision prompt ({est} tokens) exceeds "
            f"{revision_model.name} context ({limit}) — skipping revision[/yellow]"
        )
        storage.log(
            f"Revision: prompt ({est} tokens) exceeds context ({limit}) — skipping"
        )
        return ""

    # Call LLM
    storage.log(f"Revision: calling {revision_model.name}")
    raw, usage = await call_with_retry(
        client,
        revision_model,
        "",
        prompt,
        REVISION_MAX_OUTPUT_TOKENS,
        log_fn=storage.log,
    )
    cost_tracker.add(
        revision_model.name,
        usage["input_tokens"],
        usage["output_tokens"],
        revision_model.cost_per_1k_input,
        revision_model.cost_per_1k_output,
    )
    console.print(
        f"  Revision model responded ({usage['output_tokens']} tokens)"
    )
    storage.log(
        f"Revision: {revision_model.name} responded "
        f"({usage['output_tokens']} output tokens)"
    )

    # Always save raw response
    storage.save_intermediate(review_id, "revision", "revision_raw.txt", raw)

    # Strict extraction
    extracted = _extract_revision_strict(raw, mode)
    if not extracted:
        console.print(
            "  [yellow]Warning: Revision response missing canonical delimiters "
            "— revised artifact not saved[/yellow]"
        )
        storage.log(
            "Revision: extraction failed — canonical delimiters not found in response"
        )
        return ""

    return extracted
