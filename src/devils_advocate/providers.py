"""LLM API providers (Anthropic, OpenAI-compatible, MiniMax) with retry logic.

Uses httpx directly rather than vendor SDKs to avoid SDK version lock-in
and keep the dependency footprint minimal.
"""

from __future__ import annotations

import asyncio
import json
import random
import re

import httpx

from .types import APIError, ModelConfig

# ─── Constants ────────────────────────────────────────────────────────────────

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
MAX_OUTPUT_TOKENS = 16384
AUTHOR_RESPONSE_MAX_OUTPUT_TOKENS = 32000
REVISION_MAX_OUTPUT_TOKENS = 64000
DEFAULT_MAX_RETRIES = 3
_529_MAX_RETRIES = 3
_529_BUDGET_SECONDS = 30.0

# Streaming: max seconds between chunks before the connection is considered
# dead. model.timeout still bounds the whole call; this only bounds the gaps.
_STREAM_READ_TIMEOUT = 180.0

# Mode-dependent thinking budgets for Anthropic extended thinking
_ANTHROPIC_THINKING_BUDGETS = {
    "spec": 4096,
    "plan": 10000,
    "code": 10000,
    "integration": 10000,
    "revision": 8000,
    "dedup": 4096,
    "normalization": 4096,
    "": 8192,
}


# ─── Provider Implementations ────────────────────────────────────────────────


def _anthropic_uses_adaptive_thinking(model_id: str) -> bool:
    """Whether an Anthropic model takes adaptive thinking vs. a fixed budget.

    Adaptive thinking (``{"type": "adaptive"}``) is the current form for
    Opus/Sonnet/Haiku 4.6+ and the Fable/Mythos tiers. On Opus 4.7+, Sonnet 5,
    Fable, and Mythos the older ``{"type": "enabled", "budget_tokens": N}`` form
    is rejected outright with an HTTP 400; on 4.6 it is merely deprecated. Older
    models (Opus/Sonnet 4.5-, Haiku 4.5, Claude 3.x) still require the budget
    form. Parsing the version rather than matching a literal ID keeps this from
    breaking on every new model launch (e.g. the old ``"-4-7"`` check silently
    routed opus-4-8 down the rejected path).
    """
    mid = model_id.lower()
    if "fable" in mid or "mythos" in mid:
        return True
    # Pre-4.x IDs are version-first with a date suffix (claude-3-5-sonnet-
    # 20241022); the date must not be mistaken for a version, and none of these
    # models support adaptive thinking anyway. 4.x+ IDs are family-first
    # (claude-opus-4-8), never a digit right after "claude-".
    if re.match(r"claude-\d", mid):
        return False
    m = re.search(r"(?:opus|sonnet|haiku)-(\d+)(?:-(\d+))?", mid)
    if not m:
        return False
    major = int(m.group(1))
    # A trailing date snapshot (…-4-20250514) is not a minor version; real
    # minors are 1-2 digits, dates are 8.
    minor_raw = m.group(2)
    minor = int(minor_raw) if minor_raw and len(minor_raw) <= 2 else 0
    return (major, minor) >= (4, 6)


async def call_anthropic(
    client: httpx.AsyncClient,
    model: ModelConfig,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = MAX_OUTPUT_TOKENS,
    mode: str = "",
    cache_prefix: str = "",
) -> tuple:
    """Call the Anthropic Messages API. Returns (response_text, usage_dict).

    When ``cache_prefix`` is a leading substring of ``user_prompt``, the prompt
    is sent as two content blocks with ``cache_control`` on the prefix block,
    so subsequent calls sharing the same prefix (and model) read it from the
    prompt cache at 10% of the input price instead of re-paying full rate.
    """
    headers = {
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    if model.api_key:
        headers["x-api-key"] = model.api_key
    if (
        cache_prefix
        and user_prompt.startswith(cache_prefix)
        and len(user_prompt) > len(cache_prefix)
    ):
        user_content = [
            {
                "type": "text",
                "text": cache_prefix,
                "cache_control": {"type": "ephemeral"},
            },
            {"type": "text", "text": user_prompt[len(cache_prefix):]},
        ]
    else:
        user_content = user_prompt
    body = {
        "model": model.model_id,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": user_content}],
    }
    if system_prompt:
        body["system"] = system_prompt

    # Thinking / reasoning support
    if model.thinking:
        budget = _ANTHROPIC_THINKING_BUDGETS.get(mode, 8192)
        if _anthropic_uses_adaptive_thinking(model.model_id):
            # Opus/Sonnet/Haiku 4.6+ and Fable/Mythos use adaptive thinking; the
            # fixed-budget form is deprecated (4.6) or rejected with a 400 (4.7+).
            body["thinking"] = {"type": "adaptive"}
        else:
            body["thinking"] = {"type": "enabled", "budget_tokens": budget}
        # Pad max_tokens so thinking tokens don't crowd out the visible answer.
        body["max_tokens"] = body["max_tokens"] + budget

    resp = await client.post(
        ANTHROPIC_API_URL, json=body, headers=headers, timeout=model.timeout
    )
    resp.raise_for_status()
    data = resp.json()
    text = ""
    for block in data.get("content", []):
        if block.get("type") == "text":
            text += block.get("text", "")
    usage = data.get("usage") or {}
    output_tokens = usage.get("output_tokens", 0)

    if not text and output_tokens > 0:
        import logging
        logging.getLogger("devils_advocate").warning(
            "%s returned 0 visible content but %d output tokens — "
            "thinking likely consumed the entire token budget (max_tokens=%d). "
            "Consider increasing max_out_configured for this model.",
            model.name, output_tokens, max_tokens,
        )

    return text, {
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": output_tokens,
        "cache_write_tokens": usage.get("cache_creation_input_tokens", 0) or 0,
        "cache_read_tokens": usage.get("cache_read_input_tokens", 0) or 0,
    }


async def call_openai_compatible(
    client: httpx.AsyncClient,
    model: ModelConfig,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = MAX_OUTPUT_TOKENS,
    mode: str = "",
) -> tuple:
    """Call an OpenAI-compatible chat completions API. Returns (response_text, usage_dict)."""
    url = f"{model.api_base.rstrip('/')}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if model.api_key:
        headers["Authorization"] = f"Bearer {model.api_key}"
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_prompt})

    body: dict = {"model": model.model_id, "messages": messages}
    if model.use_completion_tokens or model.model_id.startswith(("o3", "o4")):
        body["max_completion_tokens"] = max_tokens
    else:
        body["max_tokens"] = max_tokens

    # Thinking / reasoning support
    if model.thinking:
        base = (model.api_base or "").lower()
        if "api.openai.com" in base:
            effort = "medium" if mode == "spec" else "high"
            body["reasoning_effort"] = effort
        elif "moonshot" in base:
            body["thinking"] = {"type": "enabled"}

    if model.stream:
        text, usage = await _stream_chat_completions(client, model, url, headers, body)
    else:
        resp = await client.post(url, json=body, headers=headers, timeout=model.timeout)
        resp.raise_for_status()
        data = resp.json()

        text = ""
        choices = data.get("choices", [])
        if choices:
            msg = choices[0].get("message", {})
            text = msg.get("content", "") or ""
            # reasoning_content is internal CoT — never use as response text

        usage = data.get("usage") or {}
    output_tokens = usage.get("completion_tokens", 0)

    if not text and output_tokens > 0:
        import logging
        logging.getLogger("devils_advocate").warning(
            "%s returned 0 visible content but %d output tokens — "
            "reasoning likely consumed the entire token budget (max_tokens=%d). "
            "Set a higher max_out_configured for this model.",
            model.name, output_tokens, max_tokens,
        )

    return text, {
        "input_tokens": usage.get("prompt_tokens", 0),
        "output_tokens": output_tokens,
    }


async def _stream_chat_completions(
    client: httpx.AsyncClient,
    model: ModelConfig,
    url: str,
    headers: dict,
    body: dict,
) -> tuple[str, dict]:
    """Consume an OpenAI-compatible SSE stream. Returns (text, raw_usage_dict).

    Some providers (z.ai) buffer non-streaming responses at the edge and drop
    the connection before long generations finish; streaming keeps bytes
    flowing from the first token. The httpx read timeout only bounds the gap
    between chunks — the whole-call wall clock is enforced separately with
    model.timeout so long generations survive as long as tokens keep arriving.
    """
    body = {**body, "stream": True, "stream_options": {"include_usage": True}}
    timeout = httpx.Timeout(model.timeout, read=_STREAM_READ_TIMEOUT)
    text_parts: list[str] = []
    usage: dict = {}
    try:
        async with asyncio.timeout(model.timeout):
            async with client.stream(
                "POST", url, json=body, headers=headers, timeout=timeout
            ) as resp:
                if resp.status_code >= 400:
                    # Body must be read before raise_for_status so the retry
                    # engine can include it in the APIError message.
                    await resp.aread()
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    choices = chunk.get("choices") or []
                    if choices:
                        delta = choices[0].get("delta") or {}
                        piece = delta.get("content")
                        if piece:
                            text_parts.append(piece)
                        # delta.reasoning_content is internal CoT — never response text
    except TimeoutError as e:
        # Normalize the wall-clock expiry so the retry engine (which only
        # catches httpx exceptions) handles it like any other timeout.
        raise httpx.ReadTimeout(
            f"streaming call exceeded {model.timeout}s wall clock"
        ) from e
    return "".join(text_parts), usage


async def call_openai_responses(
    client: httpx.AsyncClient,
    model: ModelConfig,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = MAX_OUTPUT_TOKENS,
    mode: str = "",
) -> tuple:
    """Call OpenAI Responses API (/v1/responses). Returns (response_text, usage_dict).

    Required for models only available via the Responses API (e.g. gpt-5.3-codex).
    """
    url = f"{model.api_base.rstrip('/')}/responses"
    headers = {
        "Authorization": f"Bearer {model.api_key}",
        "Content-Type": "application/json",
    }
    input_messages = []
    if system_prompt:
        input_messages.append({"role": "system", "content": system_prompt})
    input_messages.append({"role": "user", "content": user_prompt})

    body: dict = {
        "model": model.model_id,
        "input": input_messages,
        "max_output_tokens": max_tokens,
    }

    if model.thinking:
        effort = "medium" if mode == "spec" else "high"
        body["reasoning"] = {"effort": effort}

    resp = await client.post(url, json=body, headers=headers, timeout=model.timeout)
    resp.raise_for_status()
    data = resp.json()

    text = ""
    for block in data.get("output", []):
        for part in block.get("content", []):
            if part.get("type") == "output_text":
                text += part.get("text", "")

    usage = data.get("usage") or {}
    output_tokens = usage.get("output_tokens", 0)

    if not text and output_tokens > 0:
        import logging
        logging.getLogger("devils_advocate").warning(
            "%s returned 0 visible content but %d output tokens — "
            "reasoning likely consumed the entire token budget (max_output_tokens=%d). "
            "Set a higher max_out_configured for this model.",
            model.name, output_tokens, max_tokens,
        )

    return text, {
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": output_tokens,
    }


async def call_minimax(
    client: httpx.AsyncClient,
    model: ModelConfig,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = MAX_OUTPUT_TOKENS,
    mode: str = "",
) -> tuple:
    """Call MiniMax native chatcompletion_v2 API. Returns (response_text, usage_dict)."""
    url = f"{model.api_base.rstrip('/')}/v1/text/chatcompletion_v2"
    headers = {
        "Authorization": f"Bearer {model.api_key}",
        "Content-Type": "application/json",
    }
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_prompt})

    body: dict = {
        "model": model.model_id,
        "messages": messages,
        "max_tokens": max_tokens,
    }
    if model.thinking:
        body["reasoning_split"] = True

    resp = await client.post(url, json=body, headers=headers, timeout=model.timeout)
    resp.raise_for_status()
    data = resp.json()

    text = ""
    choices = data.get("choices", [])
    if choices:
        msg = choices[0].get("message", {})
        text = msg.get("content", "") or ""

    usage = data.get("usage") or {}
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
    mode: str = "",
    cache_prefix: str = "",
) -> tuple:
    """Unified dispatcher. Returns (response_text, usage_dict).

    ``cache_prefix`` enables explicit prompt caching on Anthropic models.
    Other providers cache shared prefixes automatically server-side, so the
    parameter is ignored for them — the full prompt string is sent either way.
    """
    # Honour per-model output cap from models.yaml
    if model.max_out_configured and model.max_out_configured < max_tokens:
        max_tokens = model.max_out_configured

    if model.provider == "anthropic":
        return await call_anthropic(
            client, model, system_prompt, user_prompt, max_tokens, mode,
            cache_prefix=cache_prefix,
        )
    elif model.provider == "minimax":
        return await call_minimax(client, model, system_prompt, user_prompt, max_tokens, mode)
    elif model.provider == "local":
        return await call_openai_compatible(client, model, system_prompt, user_prompt, max_tokens, mode)
    elif model.use_responses_api:
        return await call_openai_responses(client, model, system_prompt, user_prompt, max_tokens, mode)
    else:
        return await call_openai_compatible(client, model, system_prompt, user_prompt, max_tokens, mode)


# ─── Retry Engine ────────────────────────────────────────────────────────────


async def call_with_retry(
    client: httpx.AsyncClient,
    model: ModelConfig,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = MAX_OUTPUT_TOKENS,
    max_retries: int = DEFAULT_MAX_RETRIES,
    log_fn=None,
    mode: str = "",
    cache_prefix: str = "",
) -> tuple:
    """Wrap call_model with exponential backoff + jitter, respects Retry-After."""
    import time as _time

    last_exc = None
    elapsed_529 = 0.0
    attempts_529 = 0
    for attempt in range(max_retries + 1):
        try:
            return await call_model(
                client, model, system_prompt, user_prompt, max_tokens, mode,
                cache_prefix=cache_prefix,
            )
        except httpx.HTTPStatusError as e:
            last_exc = e
            status = e.response.status_code
            if status == 529:
                attempts_529 += 1
                retry_after = float(e.response.headers.get("retry-after", 0))
                wait = max(retry_after, (4 ** (attempt + 1) / 4) + random.random())
                remaining = _529_BUDGET_SECONDS - elapsed_529
                if wait > remaining or attempts_529 > _529_MAX_RETRIES:
                    raise APIError(
                        f"{model.name}: API overloaded (529) - exhausted "
                        f"{elapsed_529:.0f}s retry budget after {attempts_529} attempt(s)"
                    ) from e
                if log_fn:
                    log_fn(
                        f"  {model.name}: API overloaded (529) - waiting "
                        f"{wait:.1f}s ({remaining - wait:.0f}s budget remaining)"
                    )
                t0 = _time.monotonic()
                await asyncio.sleep(wait)
                elapsed_529 += _time.monotonic() - t0
                continue
            elif status == 429:
                retry_after = float(e.response.headers.get("retry-after", 0))
                wait = max(retry_after, (2 ** attempt) + random.random())
            elif status >= 500:
                wait = (2 ** attempt) + random.random()
            else:
                raise APIError(
                    f"{model.name}: HTTP {status} - {e.response.text[:200]}"
                ) from e
            if log_fn:
                log_fn(
                    f"  {model.name}: HTTP {status}, retry {attempt + 1}/{max_retries} "
                    f"in {wait:.1f}s"
                )
            await asyncio.sleep(wait)
        except httpx.TransportError as e:
            last_exc = e
            wait = (2 ** attempt) + random.random()
            if log_fn:
                log_fn(
                    f"  {model.name}: {type(e).__name__}, retry {attempt + 1}/{max_retries} "
                    f"in {wait:.1f}s"
                )
                if attempt == 0 and isinstance(e, httpx.TimeoutException):
                    log_fn(
                        f"  hint: consider increasing the timeout for {model.name} "
                        f"in models.yaml (current: {model.timeout}s)"
                    )
            await asyncio.sleep(wait)
    raise APIError(f"{model.name}: failed after {max_retries} retries") from last_exc
