#!/usr/bin/env python3
"""Diagnostic probe for OpenAI-provider models in models.yaml.

Sends a minimal prompt to each OpenAI model via Chat Completions,
and for 5.3-codex models also tries the Responses API.
Reports per-model: status, HTTP code, error, response snippet, tokens.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx

# Allow running from repo root without install
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from devils_advocate.config import load_config  # noqa: E402

PROBE_PROMPT = "Reply with exactly: PROBE OK"


async def probe_chat_completions(
    client: httpx.AsyncClient, model_cfg, timeout: int = 30
) -> dict:
    """Try Chat Completions endpoint."""
    url = f"{model_cfg.api_base.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {model_cfg.api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": model_cfg.model_id,
        "messages": [{"role": "user", "content": PROBE_PROMPT}],
        "max_tokens": 64,
    }
    if model_cfg.use_completion_tokens:
        body.pop("max_tokens")
        body["max_completion_tokens"] = 64

    try:
        resp = await client.post(url, json=body, headers=headers, timeout=timeout)
        data = resp.json()
        if resp.status_code >= 400:
            err = data.get("error", {})
            return {
                "api": "chat_completions",
                "status": "FAIL",
                "http": resp.status_code,
                "error": err.get("message", str(data))[:120],
                "snippet": "",
                "tokens": 0,
            }
        choices = data.get("choices", [])
        text = ""
        if choices:
            text = (choices[0].get("message", {}).get("content", "") or "")[:80]
        usage = data.get("usage", {})
        tokens = usage.get("completion_tokens", 0) + usage.get("prompt_tokens", 0)
        return {
            "api": "chat_completions",
            "status": "PASS",
            "http": resp.status_code,
            "error": "",
            "snippet": text,
            "tokens": tokens,
        }
    except Exception as e:
        return {
            "api": "chat_completions",
            "status": "FAIL",
            "http": 0,
            "error": str(e)[:120],
            "snippet": "",
            "tokens": 0,
        }


async def probe_responses_api(
    client: httpx.AsyncClient, model_cfg, timeout: int = 30
) -> dict:
    """Try OpenAI Responses API endpoint."""
    url = f"{model_cfg.api_base.rstrip('/')}/responses"
    headers = {
        "Authorization": f"Bearer {model_cfg.api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": model_cfg.model_id,
        "input": [{"role": "user", "content": PROBE_PROMPT}],
        "max_output_tokens": 64,
    }

    try:
        resp = await client.post(url, json=body, headers=headers, timeout=timeout)
        data = resp.json()
        if resp.status_code >= 400:
            err = data.get("error", {})
            return {
                "api": "responses",
                "status": "FAIL",
                "http": resp.status_code,
                "error": err.get("message", str(data))[:120],
                "snippet": "",
                "tokens": 0,
            }
        # Extract text from output[].content[].text
        text = ""
        for block in data.get("output", []):
            for part in block.get("content", []):
                if part.get("type") == "output_text":
                    text += part.get("text", "")
        text = text[:80]
        usage = data.get("usage", {})
        tokens = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
        return {
            "api": "responses",
            "status": "PASS",
            "http": resp.status_code,
            "error": "",
            "snippet": text,
            "tokens": tokens,
        }
    except Exception as e:
        return {
            "api": "responses",
            "status": "FAIL",
            "http": 0,
            "error": str(e)[:120],
            "snippet": "",
            "tokens": 0,
        }


def print_row(name: str, result: dict) -> None:
    status_icon = "✓" if result["status"] == "PASS" else "✗"
    print(
        f"  {status_icon} {name:<25} {result['api']:<20} "
        f"HTTP {result['http']:<4} {result['tokens']:>5} tok  "
        f"{result['snippet'] or result['error']}"
    )


async def main() -> None:
    config = load_config()
    all_models = config["all_models"]

    openai_models = {
        name: m
        for name, m in all_models.items()
        if m.provider == "openai" and "api.openai.com" in (m.api_base or "")
    }

    if not openai_models:
        print("No OpenAI-provider models found in models.yaml")
        return

    print(f"\nProbing {len(openai_models)} OpenAI models...\n")
    print(f"  {'':1} {'Model':<25} {'API':<20} {'HTTP':<8} {'Tokens':>5}     Response/Error")
    print(f"  {'─' * 100}")

    async with httpx.AsyncClient() as client:
        for name, model in sorted(openai_models.items()):
            if not model.api_key:
                print(f"  ✗ {name:<25} {'—':<20} {'—':<8}     0 tok  OPENAI_API_KEY not set")
                continue

            # Always try Chat Completions
            cc_result = await probe_chat_completions(client, model)
            print_row(name, cc_result)

            # For codex/pro models, also try Responses API
            if "codex" in model.model_id or "pro" in model.model_id:
                resp_result = await probe_responses_api(client, model)
                print_row(name, resp_result)

    print()


if __name__ == "__main__":
    asyncio.run(main())
