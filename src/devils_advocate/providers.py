"""LLM API providers (Anthropic, OpenAI-compatible) with retry logic.

Uses httpx directly rather than vendor SDKs to avoid SDK version lock-in
and keep the dependency footprint minimal.
"""

from __future__ import annotations

import asyncio
import random

import httpx

from .types import APIError, ModelConfig

# ─── Constants ────────────────────────────────────────────────────────────────

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
MAX_OUTPUT_TOKENS = 8192
AUTHOR_MAX_OUTPUT_TOKENS = 64000
DEFAULT_TIMEOUT = 120
DEFAULT_MAX_RETRIES = 3


# ─── Provider Implementations ────────────────────────────────────────────────


async def call_anthropic(
    client: httpx.AsyncClient,
    model: ModelConfig,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = MAX_OUTPUT_TOKENS,
) -> tuple:
    """Call the Anthropic Messages API. Returns (response_text, usage_dict)."""
    headers = {
        "x-api-key": model.api_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    body = {
        "model": model.model_id,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": user_prompt}],
    }
    if system_prompt:
        body["system"] = system_prompt

    resp = await client.post(
        ANTHROPIC_API_URL, json=body, headers=headers, timeout=model.timeout
    )
    resp.raise_for_status()
    data = resp.json()
    text = ""
    for block in data.get("content", []):
        if block.get("type") == "text":
            text += block.get("text", "")
    usage = data.get("usage", {})
    return text, {
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
    }


async def call_openai_compatible(
    client: httpx.AsyncClient,
    model: ModelConfig,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = MAX_OUTPUT_TOKENS,
) -> tuple:
    """Call an OpenAI-compatible chat completions API. Returns (response_text, usage_dict)."""
    url = f"{model.api_base.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {model.api_key}",
        "Content-Type": "application/json",
    }
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_prompt})

    body: dict = {"model": model.model_id, "messages": messages}
    if model.use_completion_tokens or model.model_id.startswith(("o3", "o4")):
        body["max_completion_tokens"] = max_tokens
    else:
        body["max_tokens"] = max_tokens

    resp = await client.post(url, json=body, headers=headers, timeout=model.timeout)
    resp.raise_for_status()
    data = resp.json()

    text = ""
    choices = data.get("choices", [])
    if choices:
        msg = choices[0].get("message", {})
        text = msg.get("content", "") or ""
        # DeepSeek reasoner fallback
        if not text and msg.get("reasoning_content"):
            text = msg["reasoning_content"]

    usage = data.get("usage", {})
    return text, {
        "input_tokens": usage.get("prompt_tokens", 0),
        "output_tokens": usage.get("completion_tokens", 0),
    }


# ─── Unified Dispatcher ─────────────────────────────────────────────────────


async def call_model(
    client: httpx.AsyncClient,
    model: ModelConfig,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = MAX_OUTPUT_TOKENS,
) -> tuple:
    """Unified dispatcher. Returns (response_text, usage_dict)."""
    if model.provider == "anthropic":
        return await call_anthropic(client, model, system_prompt, user_prompt, max_tokens)
    else:
        return await call_openai_compatible(client, model, system_prompt, user_prompt, max_tokens)


# ─── Retry Engine ────────────────────────────────────────────────────────────


async def call_with_retry(
    client: httpx.AsyncClient,
    model: ModelConfig,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = MAX_OUTPUT_TOKENS,
    max_retries: int = DEFAULT_MAX_RETRIES,
    log_fn=None,
) -> tuple:
    """Wrap call_model with exponential backoff + jitter, respects Retry-After."""
    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            return await call_model(client, model, system_prompt, user_prompt, max_tokens)
        except httpx.HTTPStatusError as e:
            last_exc = e
            status = e.response.status_code
            if status == 529:
                raise APIError(
                    f"{model.name}: API overloaded (529). "
                    f"All models must be available for a valid review run. Aborting."
                ) from e
            elif status == 429:
                retry_after = float(e.response.headers.get("retry-after", 0))
                wait = max(retry_after, (2 ** attempt) + random.random())
            elif status >= 500:
                wait = (2 ** attempt) + random.random()
            else:
                raise APIError(
                    f"{model.name}: HTTP {status} — {e.response.text[:200]}"
                ) from e
            if log_fn:
                log_fn(
                    f"  {model.name}: HTTP {status}, retry {attempt + 1}/{max_retries} "
                    f"in {wait:.1f}s"
                )
            await asyncio.sleep(wait)
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            last_exc = e
            wait = (2 ** attempt) + random.random()
            if log_fn:
                log_fn(
                    f"  {model.name}: {type(e).__name__}, retry {attempt + 1}/{max_retries} "
                    f"in {wait:.1f}s"
                )
            await asyncio.sleep(wait)
    raise APIError(f"{model.name}: failed after {max_retries} retries") from last_exc
