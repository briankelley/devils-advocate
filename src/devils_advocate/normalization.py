"""Async LLM-based response normalization.

Separated from ``parser.py`` because it makes async provider calls.
When ``parse_review_response`` yields no points, the caller can invoke
``normalize_review_response`` to send the raw text back through an LLM
for structured extraction.
"""

from __future__ import annotations

import httpx

from .types import APIError, CostTracker, ModelConfig, ReviewPoint
from .prompts import build_normalization_prompt
from .providers import MAX_OUTPUT_TOKENS, call_with_retry
from .parser import parse_review_response


async def normalize_review_response(
    client: httpx.AsyncClient,
    raw: str,
    model: ModelConfig,
    reviewer_name: str,
    start_index: int = 0,
    log_fn=None,
    cost_tracker: CostTracker | None = None,
    mode: str = "",
) -> list[ReviewPoint]:
    """LLM normalization fallback: send raw response to a model for structured extraction."""
    prompt = build_normalization_prompt(raw)
    if log_fn:
        log_fn(f"  Normalization fallback for {reviewer_name} via {model.name}")

    try:
        text, usage = await call_with_retry(
            client, model, "", prompt, MAX_OUTPUT_TOKENS, log_fn=log_fn,
            mode=mode or "normalization",
        )
        if cost_tracker:
            cost_tracker.add(
                model.name,
                usage.get("input_tokens", 0),
                usage.get("output_tokens", 0),
                model.cost_per_1k_input,
                model.cost_per_1k_output,
                role="normalization",
            )
        return parse_review_response(text, reviewer_name, start_index)
    except (APIError, Exception) as e:
        if log_fn:
            log_fn(f"  Normalization failed for {reviewer_name}: {e}")
        return []
