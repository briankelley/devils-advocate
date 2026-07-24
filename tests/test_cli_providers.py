"""Tests for the subscription CLI provider lanes (Journey 2 / design D2).

Covers the claude-cli lane end-to-end against a shim `claude` executable on a
test-controlled PATH: the probe §2 argv/stdin contract, the banked-shape
envelope parse, the child-environment key strip (the load-bearing silent-flip
guard), every failure mode mapping to ProviderUnavailableError, scratch-dir
lifecycle (deleted on success, retained on failure), dispatch via call_model,
and the branch-A concurrency posture.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import httpx
import pytest

import devils_advocate.cli_providers as cli
from devils_advocate.providers import PROVIDER_REGISTRY, call_model
from devils_advocate.types import ModelConfig, ProviderUnavailableError


# ---------------------------------------------------------------------------
# Shim harness
# ---------------------------------------------------------------------------

# A stand-in `claude` binary. Records the call (argv, stdin, cwd, env) to
# SHIM_CALL_DUMP when set, then behaves per SHIM_MODE. These two control vars
# are ordinary env names — not provider keys, not CLAUDE_CODE_* — so they
# survive the child-env strip and reach the shim.
_SHIM_SRC = r'''#!/usr/bin/env python3
import json, os, sys, time

stdin_data = sys.stdin.read()
dump = os.environ.get("SHIM_CALL_DUMP")
if dump:
    with open(dump, "w") as f:
        json.dump({
            "argv": sys.argv,
            "stdin": stdin_data,
            "cwd": os.getcwd(),
            "env": dict(os.environ),
        }, f)

mode = os.environ.get("SHIM_MODE", "ok")

if mode == "nonzero":
    sys.stderr.write("Error: usage limit reached for your plan\n")
    sys.exit(1)
if mode == "malformed":
    sys.stdout.write("not-json{{{ oops")
    sys.exit(0)
if mode == "is_error":
    print(json.dumps({"type": "result", "subtype": "error_during_execution",
                      "is_error": True, "api_error_status": 529,
                      "result": None, "usage": {}}))
    sys.exit(0)
if mode == "empty":
    print(json.dumps({"type": "result", "is_error": False, "result": "   ",
                      "usage": {"input_tokens": 1, "output_tokens": 0}}))
    sys.exit(0)
if mode == "timeout":
    time.sleep(30)
    sys.exit(0)

# ok — a banked-shape success envelope with a couple of review points.
print(json.dumps({
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": ("REVIEW POINT 1:\nSEVERITY: high\nCATEGORY: correctness\n"
               "DESCRIPTION: shim finding one.\nRECOMMENDATION: fix it.\n\n"
               "REVIEW POINT 2:\nSEVERITY: low\nCATEGORY: testing\n"
               "DESCRIPTION: shim finding two.\nRECOMMENDATION: test it."),
    "total_cost_usd": 0.123456,
    "usage": {
        "input_tokens": 2,
        "cache_creation_input_tokens": 6530,
        "cache_read_input_tokens": 0,
        "output_tokens": 40,
    },
}))
'''


@pytest.fixture
def claude_shim(tmp_path, monkeypatch):
    """Install a shim `claude` on PATH and isolate scratch under tmp.

    Returns a small handle exposing the call-record path and a mode setter.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    shim = bindir / "claude"
    shim.write_text(_SHIM_SRC)
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    state_home = tmp_path / "state"
    monkeypatch.setenv("DVAD_STATE_HOME", str(state_home))
    call_dump = tmp_path / "call.json"
    monkeypatch.setenv("SHIM_CALL_DUMP", str(call_dump))

    class Handle:
        rolecalls = state_home / "rolecalls"
        dump_path = call_dump

        def mode(self, m):
            monkeypatch.setenv("SHIM_MODE", m)

        def record(self):
            return json.loads(call_dump.read_text())

    return Handle()


@pytest.fixture(autouse=True)
def _restore_key_envs():
    """Snapshot/restore the module-level configured-key set around each test."""
    snap = set(cli._configured_key_envs)
    yield
    cli._configured_key_envs = snap


def _model(**kw) -> ModelConfig:
    kw.setdefault("name", "claude-opus-4-8-sub")
    kw.setdefault("provider", "claude-cli")
    kw.setdefault("model_id", "claude-opus-4-8")
    kw.setdefault("api_key_env", "")
    kw.setdefault("timeout", 30)
    return ModelConfig(**kw)


async def _call(model=None, system="sys prompt", user="user prompt"):
    model = model or _model()
    async with httpx.AsyncClient() as client:
        return await cli.call_claude_cli(client, model, system, user, mode="plan")


# ---------------------------------------------------------------------------
# Registration + dispatch
# ---------------------------------------------------------------------------


def test_registered_at_import():
    assert PROVIDER_REGISTRY["claude-cli"] is cli.call_claude_cli


async def test_dispatch_via_call_model(claude_shim):
    """call_model routes provider=claude-cli through the registry to the lane."""
    async with httpx.AsyncClient() as client:
        text, usage = await call_model(
            client, _model(), "sys", "usr", mode="plan"
        )
    assert "REVIEW POINT 1" in text
    assert usage["input_tokens"] == 2


# ---------------------------------------------------------------------------
# Happy path — contract, envelope, usage
# ---------------------------------------------------------------------------


async def test_happy_path_text_and_usage(claude_shim):
    text, usage = await _call()
    assert "REVIEW POINT 1" in text
    assert usage["input_tokens"] == 2
    assert usage["output_tokens"] == 40
    assert usage["cache_write_tokens"] == 6530
    assert usage["cache_read_tokens"] == 0
    # CLI-reported API-equivalent surfaces for the ledger (J4 consumes it).
    assert usage["api_equivalent"] == pytest.approx(0.123456)
    assert usage["memo"].startswith("pool leg; CLI-reported API-equivalent $")


async def test_argv_and_stdin_contract(claude_shim):
    await _call(system="SYSTEM-X", user="USER-BODY-42")
    rec = claude_shim.record()
    argv = rec["argv"]
    # probe §2 contract, verbatim.
    assert argv[0].endswith("claude")
    assert argv[1:] == [
        "-p", "--model", "claude-opus-4-8",
        "--system-prompt", "SYSTEM-X",
        "--tools", "", "--setting-sources", "", "--strict-mcp-config",
        "--no-session-persistence", "--output-format", "json",
    ]
    assert rec["stdin"] == "USER-BODY-42"
    # Neutral scratch cwd under rolecalls/.
    assert Path(rec["cwd"]).parent == claude_shim.rolecalls


# ---------------------------------------------------------------------------
# The child-env key strip (load-bearing)
# ---------------------------------------------------------------------------


async def test_env_strip_removes_every_provider_key(claude_shim, monkeypatch):
    # Hazard keys, harness residue, a config-registered custom key, and the
    # model's own api_key_env — all must be gone from the child.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-secret")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_EFFORT", "high")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc")
    monkeypatch.setenv("CUSTOM_VENDOR_KEY", "vendor-secret")
    monkeypatch.setenv("MY_MODEL_KEY", "model-secret")
    cli.note_configured_key_envs(["CUSTOM_VENDOR_KEY", ""])

    await _call(model=_model(api_key_env="MY_MODEL_KEY"))
    child_env = claude_shim.record()["env"]

    for gone in (
        "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CLAUDECODE", "CLAUDE_EFFORT",
        "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID",
        "CUSTOM_VENDOR_KEY", "MY_MODEL_KEY",
    ):
        assert gone not in child_env, f"{gone} leaked into the CLI child env"

    # The CLI still needs its own auth state: HOME/PATH pass through.
    assert child_env.get("HOME") == os.environ["HOME"]
    assert "PATH" in child_env


# ---------------------------------------------------------------------------
# Failure modes → ProviderUnavailableError (fail loud)
# ---------------------------------------------------------------------------


async def test_nonzero_exit_raises_with_stderr_tail(claude_shim):
    claude_shim.mode("nonzero")
    with pytest.raises(ProviderUnavailableError) as ei:
        await _call()
    assert "exited 1" in str(ei.value)
    assert "usage limit reached" in str(ei.value)


async def test_malformed_json_raises(claude_shim):
    claude_shim.mode("malformed")
    with pytest.raises(ProviderUnavailableError) as ei:
        await _call()
    assert "unparseable JSON" in str(ei.value)


async def test_is_error_raises(claude_shim):
    claude_shim.mode("is_error")
    with pytest.raises(ProviderUnavailableError) as ei:
        await _call()
    assert "is_error" in str(ei.value)


async def test_empty_result_raises(claude_shim):
    claude_shim.mode("empty")
    with pytest.raises(ProviderUnavailableError) as ei:
        await _call()
    assert "no result text" in str(ei.value)


async def test_timeout_kills_and_raises(claude_shim):
    claude_shim.mode("timeout")
    with pytest.raises(ProviderUnavailableError) as ei:
        await _call(model=_model(timeout=1))
    assert "timed out" in str(ei.value)


# ---------------------------------------------------------------------------
# Scratch-dir lifecycle
# ---------------------------------------------------------------------------


async def test_scratch_deleted_on_success(claude_shim):
    await _call()
    leftovers = list(claude_shim.rolecalls.glob("claude-*"))
    assert leftovers == []


async def test_scratch_retained_on_failure(claude_shim):
    claude_shim.mode("nonzero")
    with pytest.raises(ProviderUnavailableError):
        await _call()
    leftovers = list(claude_shim.rolecalls.glob("claude-*"))
    assert len(leftovers) == 1 and leftovers[0].is_dir()


# ---------------------------------------------------------------------------
# Concurrency posture (U2 → branch A)
# ---------------------------------------------------------------------------


def test_concurrency_constant_unset():
    # V2 resolved branch A: no semaphore. The constant stays None.
    assert cli._CLAUDE_MAX_CONCURRENCY is None


async def test_two_concurrent_calls_both_run(claude_shim):
    import asyncio

    r1, r2 = await asyncio.gather(_call(), _call())
    assert "REVIEW POINT 1" in r1[0]
    assert "REVIEW POINT 1" in r2[0]
