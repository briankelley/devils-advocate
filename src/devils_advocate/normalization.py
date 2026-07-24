"""Async LLM-based response normalization.

Separated from ``parser.py`` because it makes async provider calls.
When ``parse_review_response`` yields no points, the caller can invoke
``normalize_review_response`` to send the raw text back through an LLM
for structured extraction.
"""

from __future__ import annotations

import httpx

from .types import CostTracker, ModelConfig, ReviewPoint
from .cost import estimate_tokens
from .prompts import build_normalization_prompt
from .providers import MAX_OUTPUT_TOKENS, call_and_account
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
    config: dict | None = None,
) -> list[ReviewPoint]:
    """LLM normalization fallback: send raw response to a model for structured extraction."""
    prompt = build_normalization_prompt(raw)
    if log_fn:
        sent = estimate_tokens(prompt)
        configured = model.max_out_configured or MAX_OUTPUT_TOKENS
        stated = model.max_out_stated or MAX_OUTPUT_TOKENS
        thinking_str = "on" if model.thinking else "off"
        log_fn(
            f"  Normalization: calling {model.name} "
            f"(fallback for {reviewer_name}, sent: {sent}, timeout: {model.timeout}s, "
            f"max_out: {configured}/{stated}, thinking: {thinking_str})"
        )

    try:
        text, _usage, _served = await call_and_account(
            client, model, config, cost_tracker, "normalization",
            "", prompt, MAX_OUTPUT_TOKENS, log_fn=log_fn,
            mode=mode or "normalization",
        )
        return parse_review_response(text, reviewer_name, start_index)
    except Exception as e:
        if log_fn:
            log_fn(f"  Normalization failed for {reviewer_name}: {e}")
        return []
