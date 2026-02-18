"""Token and cost estimation, context window checks."""

from __future__ import annotations

from .types import ModelConfig

# ─── Constants ────────────────────────────────────────────────────────────────

CHARS_PER_TOKEN = 4
CONTEXT_WINDOW_THRESHOLD = 0.8


# ─── Functions ────────────────────────────────────────────────────────────────


def estimate_tokens(text: str) -> int:
    """Estimate token count from character length."""
    return max(1, len(text) // CHARS_PER_TOKEN)


def estimate_cost(
    model: ModelConfig,
    input_tokens: int,
    output_tokens: int,
) -> float:
    """Estimate USD cost for a model call given token counts."""
    if model.cost_per_1k_input is None or model.cost_per_1k_output is None:
        return 0.0
    return (
        input_tokens / 1000 * model.cost_per_1k_input
        + output_tokens / 1000 * model.cost_per_1k_output
    )


def check_context_window(
    model: ModelConfig,
    text: str,
) -> tuple[bool, int, int]:
    """Check whether text fits within a model's context window.

    Returns:
        (fits, estimated_tokens, limit) where *fits* is True if the
        estimated token count is within CONTEXT_WINDOW_THRESHOLD of
        the model's context window.  When the model has no declared
        context window, *fits* is always True and *limit* is 0.
    """
    est = estimate_tokens(text)
    if model.context_window is None:
        return True, est, 0
    limit = int(model.context_window * CONTEXT_WINDOW_THRESHOLD)
    return est <= limit, est, limit
