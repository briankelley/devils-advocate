"""D4 — failover, the subscription master switch, and equivalent-cost accounting.

Deck items V11 and V12 (PLAN-20260724 §9). Exercises the four moving parts that
land in Journey 4 against real machinery — a shim `claude` on PATH for the CLI
lane, respx for the API twin, and load_config for the validation gate:

  V11  a limit-exiting lane fails over ONE hop to its API twin; the ledger
       attributes the SERVING model at its rates and carries a failover memo;
       the one-hop cap holds.
  V12  the master switch — OFF routes a CLI role to its twin with no subprocess;
       ON dispatches the lane; a CLI entry with no failover is a load error; the
       cost estimate follows the effective model; the §cost line stays
       backward-compatible with the progress parser.
"""

from __future__ import annotations

import os
import re
import stat

import httpx
import pytest
import respx

from devils_advocate import cli_providers as cli
from devils_advocate.config import load_config, resolve_effective_model
from devils_advocate.gui.progress import _PHASE_PATTERNS
from devils_advocate.orchestrator._display import _estimate_total_cost
from devils_advocate.providers import (
    PROVIDER_REGISTRY,
    _FAILOVER_MAX_HOPS,
    call_and_account,
    call_with_retry,
    register_provider,
)
from devils_advocate.types import ConfigError, CostTracker, ModelConfig, ProviderUnavailableError

# asyncio_mode = "auto" (pyproject) runs coroutine tests without a per-test mark.


# ─── Shim `claude` (mirrors test_cli_providers) ──────────────────────────────

_SHIM_SRC = r'''#!/usr/bin/env python3
import json, os, sys
sys.stdin.read()
mode = os.environ.get("SHIM_MODE", "ok")
if mode == "limit":
    sys.stderr.write("Error: usage limit reached for your plan\n")
    sys.exit(1)
print(json.dumps({
    "type": "result", "subtype": "success", "is_error": False,
    "result": "REVIEW POINT 1:\nSEVERITY: high\nCATEGORY: correctness\n"
              "DESCRIPTION: shim finding.\nRECOMMENDATION: fix it.",
    "total_cost_usd": 0.1400,
    "usage": {"input_tokens": 3, "output_tokens": 41,
              "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
}))
'''

_TWIN_BASE = "https://api.twin.test"

_TWIN_RESPONSE = {
    "choices": [{"message": {"content": (
        "REVIEW POINT 1:\nSEVERITY: high\nCATEGORY: correctness\n"
        "DESCRIPTION: twin finding.\nRECOMMENDATION: patch it."
    )}}],
    "usage": {"prompt_tokens": 100, "completion_tokens": 50},
}


@pytest.fixture
def claude_shim(tmp_path, monkeypatch):
    """Install a shim `claude` on PATH; isolate lane scratch under tmp."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    shim = bindir / "claude"
    shim.write_text(_SHIM_SRC)
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("DVAD_STATE_HOME", str(tmp_path / "state"))

    class Handle:
        def mode(self, m):
            monkeypatch.setenv("SHIM_MODE", m)

    return Handle()


def _sub(**kw) -> ModelConfig:
    kw.setdefault("name", "claude-opus-4-8-sub")
    kw.setdefault("provider", "claude-cli")
    kw.setdefault("model_id", "claude-opus-4-8")
    kw.setdefault("api_key_env", "")
    kw.setdefault("failover_model", "gpt-twin")
    kw.setdefault("timeout", 30)
    return ModelConfig(**kw)


def _twin(**kw) -> ModelConfig:
    kw.setdefault("name", "gpt-twin")
    kw.setdefault("provider", "openai")
    kw.setdefault("model_id", "gpt-x")
    kw.setdefault("api_key_env", "")
    kw.setdefault("api_base", _TWIN_BASE)
    kw.setdefault("cost_per_1k_input", 0.01)
    kw.setdefault("cost_per_1k_output", 0.03)
    kw.setdefault("timeout", 30)
    return ModelConfig(**kw)


def _config(sub, twin, *, on: bool) -> dict:
    return {
        "all_models": {sub.name: sub, twin.name: twin},
        "subscription_backend": on,
    }


# ─── V11 — failover to the API twin, served-model accounting, one-hop cap ─────


@respx.mock
async def test_v11_limit_exit_fails_over_to_twin_and_accounts_served_model(claude_shim):
    """The lane hits a usage limit → the SAME call re-dispatches to the twin.

    The ledger records the twin at the twin's rates (real API dollars, computed
    from the twin's token usage), with a memo naming the failover — not the dead
    CLI lane, not $0.
    """
    claude_shim.mode("limit")
    respx.post(f"{_TWIN_BASE}/chat/completions").mock(
        return_value=httpx.Response(200, json=_TWIN_RESPONSE)
    )
    sub, twin = _sub(), _twin()
    config = _config(sub, twin, on=True)  # ON → the lane is dispatched, then fails over
    ct = CostTracker()

    async with httpx.AsyncClient() as client:
        text, usage, served = await call_and_account(
            client, sub, config, ct, "reviewer", "sys", "user prompt",
        )

    assert served.name == "gpt-twin"
    assert "twin finding" in text
    entry = ct.entries[-1]
    assert entry["model"] == "gpt-twin"
    # Twin rates applied to the twin's tokens: 100/1k*0.01 + 50/1k*0.03 = 0.0025.
    assert entry["cost_usd"] == pytest.approx(0.0025)
    assert ct.total_usd == pytest.approx(0.0025)
    assert "failover" in entry["memo"]
    assert "claude-opus-4-8-sub" in entry["memo"]
    # A failover leg bills real dollars — it is NOT a pool leg, so no equivalent.
    assert "api_equivalent" not in entry
    assert ct.total_api_equivalent_usd == 0.0


async def test_v11_failover_cap_is_one_hop():
    """A twin that is itself unavailable does NOT chain a second failover hop."""
    calls: list[str] = []

    async def _boom(client, model, system_prompt, user_prompt, max_tokens=0, mode=""):
        calls.append(model.name)
        raise ProviderUnavailableError(f"{model.name} down")

    register_provider("boom-cap-test", _boom)
    try:
        m1 = ModelConfig(name="m1", provider="boom-cap-test", model_id="m1",
                         api_key_env="", failover_model="m2")
        m2 = ModelConfig(name="m2", provider="boom-cap-test", model_id="m2",
                         api_key_env="", failover_model="m3")
        m3 = ModelConfig(name="m3", provider="boom-cap-test", model_id="m3",
                         api_key_env="", failover_model="")
        config = {"all_models": {"m1": m1, "m2": m2, "m3": m3},
                  "subscription_backend": True}
        async with httpx.AsyncClient() as client:
            with pytest.raises(ProviderUnavailableError):
                await call_with_retry(client, m1, "s", "u", config=config)
        # m1 (hop 0) → m2 (hop 1). At hop 1 the cap re-raises; m3 is never reached.
        assert calls == ["m1", "m2"]
        assert _FAILOVER_MAX_HOPS == 1
    finally:
        PROVIDER_REGISTRY.pop("boom-cap-test", None)


# ─── V12 — the master switch, load validation, estimate, §cost compat ────────


async def test_v12_switch_off_routes_to_twin_without_spawning_lane():
    """Switch OFF: a CLI role dispatches straight to its twin — no subprocess."""
    async def _must_not_run(*a, **k):
        raise AssertionError("CLI lane dispatched while the switch was OFF")

    saved = PROVIDER_REGISTRY["claude-cli"]
    register_provider("claude-cli", _must_not_run)
    try:
        with respx.mock:
            respx.post(f"{_TWIN_BASE}/chat/completions").mock(
                return_value=httpx.Response(200, json=_TWIN_RESPONSE)
            )
            sub, twin = _sub(), _twin()
            config = _config(sub, twin, on=False)
            # resolve_effective_model must pick the twin before any dispatch.
            assert resolve_effective_model(config, sub).name == "gpt-twin"
            ct = CostTracker()
            async with httpx.AsyncClient() as client:
                text, usage, served = await call_and_account(
                    client, sub, config, ct, "reviewer", "sys", "user",
                )
        assert served.name == "gpt-twin"
        assert "twin finding" in text
        assert ct.entries[-1]["model"] == "gpt-twin"
        assert ct.entries[-1]["cost_usd"] == pytest.approx(0.0025)
        assert "memo" not in ct.entries[-1]  # a plain twin call, not a failover
    finally:
        register_provider("claude-cli", saved)


async def test_v12_switch_on_dispatches_the_lane(claude_shim):
    """Switch ON: the lane runs; the ledger shows $0 billed + the pool memo."""
    claude_shim.mode("ok")
    sub, twin = _sub(), _twin()
    config = _config(sub, twin, on=True)
    assert resolve_effective_model(config, sub).name == "claude-opus-4-8-sub"
    ct = CostTracker()
    async with httpx.AsyncClient() as client:
        text, usage, served = await call_and_account(
            client, sub, config, ct, "reviewer", "sys", "user",
        )
    assert served.name == "claude-opus-4-8-sub"
    assert "shim finding" in text
    entry = ct.entries[-1]
    assert entry["model"] == "claude-opus-4-8-sub"
    assert entry["cost_usd"] == 0.0            # the -sub entry carries no rates
    assert entry["api_equivalent"] == pytest.approx(0.14)  # CLI-reported cost
    assert "pool leg" in entry["memo"]
    assert ct.total_api_equivalent_usd == pytest.approx(0.14)


async def test_v12_cli_entry_without_failover_is_a_load_error(tmp_path):
    """A CLI-lane role with no failover_model fails at config load, not mid-run."""
    yaml_text = (
        "models:\n"
        "  claude-opus-4-8-sub:\n"
        "    provider: claude-cli\n"
        "    model_id: claude-opus-4-8\n"
        "  gpt-twin:\n"
        "    provider: openai\n"
        "    model_id: gpt-x\n"
        f"    api_base: {_TWIN_BASE}\n"
        "roles:\n"
        "  author: gpt-twin\n"
        "  reviewers: [claude-opus-4-8-sub]\n"
        "  deduplication: gpt-twin\n"
    )
    cfg_file = tmp_path / "models.yaml"
    cfg_file.write_text(yaml_text)
    with pytest.raises(ConfigError, match="failover_model"):
        load_config(cfg_file)


async def test_v12_cli_entry_with_valid_failover_loads(tmp_path):
    """The same config WITH a failover_model loads clean and parses the switch."""
    yaml_text = (
        "settings:\n"
        "  subscription_backend: true\n"
        "models:\n"
        "  claude-opus-4-8-sub:\n"
        "    provider: claude-cli\n"
        "    model_id: claude-opus-4-8\n"
        "    failover_model: gpt-twin\n"
        "  gpt-twin:\n"
        "    provider: openai\n"
        "    model_id: gpt-x\n"
        f"    api_base: {_TWIN_BASE}\n"
        "roles:\n"
        "  author: gpt-twin\n"
        "  reviewers: [claude-opus-4-8-sub]\n"
        "  deduplication: gpt-twin\n"
    )
    cfg_file = tmp_path / "models.yaml"
    cfg_file.write_text(yaml_text)
    config = load_config(cfg_file)
    assert config["subscription_backend"] is True


async def test_v12_non_bool_switch_is_a_load_error(tmp_path):
    """A non-boolean subscription_backend is rejected, never coerced to truthy."""
    yaml_text = (
        "settings:\n"
        "  subscription_backend: \"yes\"\n"
        "models:\n"
        "  gpt-twin:\n"
        "    provider: openai\n"
        "    model_id: gpt-x\n"
        f"    api_base: {_TWIN_BASE}\n"
        "roles:\n"
        "  author: gpt-twin\n"
        "  reviewers: [gpt-twin]\n"
    )
    cfg_file = tmp_path / "models.yaml"
    cfg_file.write_text(yaml_text)
    with pytest.raises(ConfigError, match="subscription_backend"):
        load_config(cfg_file)


def test_v12_estimate_follows_the_effective_model():
    """The dry-run estimate prices the twin when OFF and $0 when ON."""
    sub, twin = _sub(), _twin()
    content = "x" * 4000  # ~1000 estimated tokens
    off = _estimate_total_cost(
        content, sub, [sub], sub, config=_config(sub, twin, on=False)
    )
    on = _estimate_total_cost(
        content, sub, [sub], sub, config=_config(sub, twin, on=True)
    )
    # OFF resolves every role to the twin (real rates) → a positive estimate.
    assert off > 0.0
    # ON keeps the -sub entries (no rates) → the pool reads as $0.
    assert on == 0.0


def _cost_regex() -> re.Pattern:
    for pat, phase, _ in _PHASE_PATTERNS:
        if phase == "cost_update":
            return re.compile(pat)
    raise AssertionError("cost_update pattern not found in progress patterns")


def test_v12_section_cost_line_backward_compatible():
    """The progress cost regex parses both the old line and the new pool line."""
    rx = _cost_regex()
    lines: list[str] = []

    # Old-format line (no pool leg): no trailing equiv tokens at all.
    ct_old = CostTracker(_log_fn=lines.append)
    ct_old.add("gpt-x", 100, 50, 0.01, 0.03, role="reviewer")
    old_line = lines[-1]
    assert "equiv=" not in old_line
    m_old = rx.match(old_line)
    assert m_old and m_old.group(1) == "reviewer" and m_old.group(2) == "gpt-x"

    # New pool line: same mandatory prefix, appended optional equiv/channel.
    ct_new = CostTracker(_log_fn=lines.append)
    ct_new.add("claude-sub", 3, 41, None, None, role="reviewer",
               memo="pool leg; CLI-reported API-equivalent $0.140",
               api_equivalent=0.14)
    new_line = lines[-1]
    assert "equiv=0.140000 channel=pool" in new_line
    m_new = rx.match(new_line)
    # The regex — no end anchor — still captures the prefix fields intact.
    assert m_new and m_new.group(1) == "reviewer" and m_new.group(2) == "claude-sub"
    assert m_new.group(5) == "3" and m_new.group(6) == "41"
