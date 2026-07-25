"""Stage 3 — the one place a model is asked for a judgement.

Deliberately narrow. The reasoner is asked exactly two things about each
candidate: should it join the roster, and what tier is it. Everything else
about the resulting entry is derived deterministically in :mod:`apply`, because
every field that is not "which models" is either upstream fact or an operator
preference the automation must not invent.

That narrowness is a safety property, not an economy. A confused reasoner can
only ever produce a wrong *shortlist*, which is visible and reversible. It
cannot produce a malformed entry, a surprising timeout, or extended thinking
switched on behind the operator's back.

The reasoner never sees the 3.2 MB catalogue and never touches models.yaml. It
reads a few KB of structured summary and returns JSON on stdout.
"""

from __future__ import annotations

import json
import re
import subprocess

from .probe import _clean_env
from .types import ReasonerError

DEFAULT_REASONER_MODEL = "claude-fable-5"
REASONER_TIMEOUT = 300

SYSTEM_PROMPT = """\
You curate the model roster for Devil's Advocate, a multi-model adversarial \
code-review tool. Reviewers, an author, a deduplicator, a normaliser and a \
revision model are each assigned from this roster by hand.

You are given models that already passed a mechanical filter. Decide which \
deserve a place in the roster, and what tier each occupies.

Judge on fitness for long-context critical reading and writing: context window, \
output ceiling, whether the model is a genuine frontier or a distilled/mini \
variant, and price sanity for the tier. Prefer a small, high-signal roster over \
a complete one — every admission is another line in a hand-operated picker.

Coverage goal, per provider: at least two top-tier models and at least one \
budget model. "Budget" is relative to that provider's own range, not an \
absolute price.

Return ONLY a JSON object, no prose and no code fence:
{"admit":[{"model_id":"<exact id>","tier":"top|mid|budget","why":"<one short sentence>"}],
 "decline":[{"model_id":"<exact id>","why":"<one short sentence>"}]}

Every candidate must appear in exactly one of the two lists. Use model ids \
exactly as given."""

VALID_TIERS = ("top", "mid", "budget")


def build_prompt(result, raw_models: dict) -> str:
    """Render the gate's findings as a compact briefing."""
    from .gate import _blended

    lines: list[str] = ["## Roster today", ""]
    by_provider: dict[str, list[str]] = {}
    for name, entry in sorted(raw_models.items()):
        if not isinstance(entry, dict):
            continue
        provider = entry.get("provider", "openai")
        base = (entry.get("api_base") or "").split("//")[-1].split("/")[0] or provider
        cost_in = entry.get("cost_per_1k_input")
        cost_out = entry.get("cost_per_1k_output")
        by_provider.setdefault(base, []).append(
            f"  {name} (ctx {entry.get('context_window')}, "
            f"${cost_in}/${cost_out} per 1k)"
        )
    for base, entries in sorted(by_provider.items()):
        lines.append(f"{base}:")
        lines.extend(entries)
    lines.append("")

    lines.append("## Coverage gaps the filter found")
    gaps = result.actionable_shortfalls
    lines.extend([f"  {s.describe()}" for s in gaps] or ["  none"])
    lines.append("")

    lines.append("## Candidates")
    lines.append("")
    for candidate in result.candidates:
        record = candidate.record
        cost = record.get("cost") or {}
        limit = record.get("limit") or {}
        lines.append(
            f"- {candidate.model_id} [{candidate.provider}/{candidate.family}] "
            f"${cost.get('input')}/${cost.get('output')} per 1M, "
            f"ctx {limit.get('context')}, max_out {limit.get('output')}, "
            f"released {record.get('release_date')}, "
            f"reasoning-capable={record.get('reasoning')}, "
            f"blended ${_blended(record):.2f}"
        )
        description = (record.get("description") or "").strip()
        if description:
            lines.append(f"    {description[:160]}")
    return "\n".join(lines)


def _extract_json(text: str) -> dict:
    """Pull the JSON object out of a reply, tolerating fences and preamble."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            text = text[start : end + 1]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ReasonerError(f"reasoner did not return JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ReasonerError(f"reasoner returned {type(parsed).__name__}, expected an object")
    return parsed


def validate(parsed: dict, candidate_ids: set[str]) -> list[dict]:
    """Return the admitted entries, rejecting anything malformed or invented.

    A reasoner that hallucinates a model id, or admits something the gate never
    offered, is silently ignored rather than trusted — the gate's output is the
    complete universe of what may be added on this pass.
    """
    admitted: list[dict] = []
    seen: set[str] = set()
    for item in parsed.get("admit") or []:
        if not isinstance(item, dict):
            continue
        model_id = item.get("model_id")
        if model_id not in candidate_ids or model_id in seen:
            continue
        tier = item.get("tier")
        if tier not in VALID_TIERS:
            tier = "mid"
        seen.add(model_id)
        admitted.append(
            {
                "model_id": model_id,
                "tier": tier,
                "why": str(item.get("why") or "").strip()[:200],
            }
        )
    return admitted


def run(
    result,
    raw_models: dict,
    *,
    model: str = DEFAULT_REASONER_MODEL,
    timeout: int = REASONER_TIMEOUT,
) -> list[dict]:
    """Ask the reasoner which candidates to admit. Returns validated entries."""
    if not result.candidates:
        return []

    argv = [
        "claude", "-p", "--model", model,
        "--system-prompt", SYSTEM_PROMPT,
        "--tools", "", "--setting-sources", "", "--strict-mcp-config",
        "--no-session-persistence", "--output-format", "json",
    ]
    try:
        proc = subprocess.run(
            argv,
            input=build_prompt(result, raw_models),
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_clean_env(),
        )
    except FileNotFoundError as exc:
        raise ReasonerError("the claude CLI is not on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise ReasonerError(f"reasoner timed out after {timeout}s") from exc

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()[:300]
        raise ReasonerError(f"reasoner exited {proc.returncode}: {detail}")

    try:
        envelope = json.loads(proc.stdout)
        text = envelope.get("result") if isinstance(envelope, dict) else proc.stdout
    except json.JSONDecodeError:
        text = proc.stdout

    parsed = _extract_json(text or "")
    return validate(parsed, {c.model_id for c in result.candidates})
