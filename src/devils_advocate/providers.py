"""LLM API providers (Anthropic, OpenAI-compatible, MiniMax) with retry logic.

Uses httpx directly rather than vendor SDKs to avoid SDK version lock-in
and keep the dependency footprint minimal.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
from dataclasses import dataclass
from typing import Awaitable, Callable

import httpx

from .types import (
    AdvocateError,
    APIError,
    ConfigError,
    CostTracker,
    ModelConfig,
    ProviderUnavailableError,
)

# ─── Constants ────────────────────────────────────────────────────────────────

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
MAX_OUTPUT_TOKENS = 16384
AUTHOR_RESPONSE_MAX_OUTPUT_TOKENS = 32000
REVISION_MAX_OUTPUT_TOKENS = 64000
DEFAULT_MAX_RETRIES = 3
# One failover hop only: a CLI lane that raises ProviderUnavailableError
# re-dispatches to its (non-CLI, validated) failover_model exactly once. The
# twin is an API transport that cannot itself raise ProviderUnavailableError, so
# a second hop is impossible by construction; the cap is a belt-and-braces stop.
_FAILOVER_MAX_HOPS = 1
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
                lines = resp.aiter_lines()
                try:
                    async for line in lines:
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
                finally:
                    # Breaking out at [DONE] leaves the generator suspended;
                    # without an explicit aclose() the GC-time finalizer task
                    # can outlive the event loop ("Task was destroyed but it
                    # is pending!").
                    await lines.aclose()
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


# ─── Provider Registry ───────────────────────────────────────────────────────

# A provider is any coroutine ``(client, model, system_prompt, user_prompt,
# max_tokens, mode) -> (text, usage_dict)``. ``call_anthropic`` additionally
# accepts a ``cache_prefix`` keyword; the dispatcher passes it only to that
# built-in, exactly as the historical if/elif did.
ProviderFn = Callable[..., Awaitable[tuple]]

PROVIDER_REGISTRY: dict[str, ProviderFn] = {
    "anthropic": call_anthropic,
    "minimax": call_minimax,
    "local": call_openai_compatible,
}


def register_provider(name: str, fn: ProviderFn) -> None:
    """Register a provider transport under *name* in the shared registry.

    New transports (subscription CLI lanes, external plugins) become entries
    here instead of new branches in :func:`call_model`.
    """
    PROVIDER_REGISTRY[name] = fn


@dataclass(frozen=True)
class ProviderPluginAPI:
    """The stable surface a provider plugin's ``register(api)`` receives.

    Carries the registration hook, the built-in provider callables, the retry
    engine, and the exception types so a plugin can build a transport that
    behaves like a first-class built-in without importing package internals
    directly.
    """
    register_provider: Callable[[str, ProviderFn], None]
    call_anthropic: ProviderFn
    call_openai_compatible: ProviderFn
    call_minimax: ProviderFn
    call_openai_responses: ProviderFn
    call_with_retry: Callable[..., Awaitable[tuple]]
    AdvocateError: type
    APIError: type
    ConfigError: type


def make_plugin_api() -> ProviderPluginAPI:
    """Build the namespace handed to each provider plugin's ``register``."""
    return ProviderPluginAPI(
        register_provider=register_provider,
        call_anthropic=call_anthropic,
        call_openai_compatible=call_openai_compatible,
        call_minimax=call_minimax,
        call_openai_responses=call_openai_responses,
        call_with_retry=call_with_retry,
        AdvocateError=AdvocateError,
        APIError=APIError,
        ConfigError=ConfigError,
    )


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

    Dispatch resolves ``model.provider`` against :data:`PROVIDER_REGISTRY`
    first (built-ins plus any plugin- or lane-registered transports). Only when
    the provider is unregistered does it fall back to the Responses API (when
    ``use_responses_api`` is set) or the OpenAI-compatible catch-all — which
    keeps ``local`` outranking ``use_responses_api`` exactly as before.
    """
    # Honour per-model output cap from models.yaml
    if model.max_out_configured and model.max_out_configured < max_tokens:
        max_tokens = model.max_out_configured

    fn = PROVIDER_REGISTRY.get(model.provider)
    if fn is not None:
        # Thin adapter: only call_anthropic consumes cache_prefix.
        if fn is call_anthropic:
            return await fn(
                client, model, system_prompt, user_prompt, max_tokens, mode,
                cache_prefix=cache_prefix,
            )
        return await fn(client, model, system_prompt, user_prompt, max_tokens, mode)

    if model.use_responses_api:
        return await call_openai_responses(client, model, system_prompt, user_prompt, max_tokens, mode)
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
    config: dict | None = None,
    _failover_hops: int = 0,
) -> tuple:
    """Wrap call_model with exponential backoff + jitter, respects Retry-After.

    When *config* is supplied and the transport raises
    :class:`ProviderUnavailableError` (a subscription CLI lane that cannot serve
    the call — pool exhausted, auth lapsed, binary missing, model unsupported),
    the SAME call re-dispatches once down the model's ``failover_model`` API twin
    (one hop, :data:`_FAILOVER_MAX_HOPS`). With no ``failover_model`` (or no
    *config*) the error propagates loudly — dvad never silently drops a review.
    The served twin's name is stamped into the returned usage under
    ``_served_model_name`` so :func:`call_and_account` charges the twin's rates.
    """
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
        except ProviderUnavailableError as e:
            # The lane has already exhausted its own in-lane handling before
            # raising, so this is a definitive "cannot serve" — fail over rather
            # than retry the dead lane. One hop, to the validated non-CLI twin.
            if (
                config is None
                or _failover_hops >= _FAILOVER_MAX_HOPS
                or not model.failover_model
            ):
                raise
            twin = (config.get("all_models") or {}).get(model.failover_model)
            if twin is None:
                raise
            if log_fn:
                log_fn(
                    f"  {model.name}: provider unavailable ({e}); "
                    f"failing over to {twin.name} on the API path"
                )
            text, usage = await call_with_retry(
                client, twin, system_prompt, user_prompt, max_tokens,
                max_retries=max_retries, log_fn=log_fn, mode=mode,
                cache_prefix=cache_prefix, config=config,
                _failover_hops=_failover_hops + 1,
            )
            usage["_served_model_name"] = twin.name
            # Annotate the ledger: this leg billed real API dollars because the
            # subscription lane was down. api_equivalent stays absent (it IS the
            # real cost, not a pool substitute); the memo tells the operator why
            # a CLI-lane role shows up on the twin's rates.
            usage.setdefault(
                "memo", f"failover from {model.name}: served by {twin.name} via API"
            )
            return text, usage
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


# ─── Call + account (the one home for the dispatch→cost pattern) ──────────────


async def call_and_account(
    client: httpx.AsyncClient,
    model: ModelConfig,
    config: dict | None,
    cost_tracker: CostTracker | None,
    role: str,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = MAX_OUTPUT_TOKENS,
    max_retries: int = DEFAULT_MAX_RETRIES,
    log_fn=None,
    mode: str = "",
    cache_prefix: str = "",
) -> tuple:
    """Resolve the effective model, call it with retry+failover, and account.

    The single consolidation point every role call flows through. It:

    1. Resolves *model* to the model that should actually run (D4.5): the
       subscription master switch, when OFF, routes a CLI-lane model to its API
       twin, so a run with the switch off is byte-identical to pre-feature dvad.
    2. Dispatches through :func:`call_with_retry` (which may itself fail over a
       live CLI lane to its twin).
    3. Learns which config actually SERVED the text — the effective model, or,
       after a failover hop, the twin — and performs the single
       ``cost_tracker.add`` at THAT model's rates, passing the pool leg's
       ``memo``/``api_equivalent`` through to the ledger.

    Returns ``(text, usage, served_model)``. *cost_tracker* may be None (dedup /
    normalization call it that way); the dispatch still runs, accounting is
    simply skipped. With *config* None the resolution and failover are inert and
    the served model is *model* itself — bit-identical to a bare
    ``call_with_retry`` + ``cost_tracker.add`` pair.
    """
    if config is not None:
        from .config import resolve_effective_model
        effective = resolve_effective_model(config, model)
    else:
        effective = model

    text, usage = await call_with_retry(
        client, effective, system_prompt, user_prompt, max_tokens,
        max_retries=max_retries, log_fn=log_fn, mode=mode,
        cache_prefix=cache_prefix, config=config,
    )

    # Which config produced the text? call_with_retry stamps the twin's name
    # when it failed over; absent that, the effective model served. Pop the
    # private marker so it never rides downstream into parsers or serialization.
    served_name = usage.pop("_served_model_name", effective.name)
    served = None
    if config is not None:
        served = (config.get("all_models") or {}).get(served_name)
    if served is None:
        served = effective

    if cost_tracker is not None:
        cost_tracker.add(
            served.name,
            usage.get("input_tokens", 0),
            usage.get("output_tokens", 0),
            served.cost_per_1k_input,
            served.cost_per_1k_output,
            role=role,
            cache_write_tokens=usage.get("cache_write_tokens", 0),
            cache_read_tokens=usage.get("cache_read_tokens", 0),
            memo=usage.get("memo"),
            api_equivalent=usage.get("api_equivalent"),
        )

    return text, usage, served
