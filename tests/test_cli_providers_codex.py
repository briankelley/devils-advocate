"""Tests for the codex-cli subscription lane (Journey 3 / design D3).

Covers the codex-cli lane end-to-end against a shim `codex` executable on a
test-controlled PATH: the probe R2 `codex exec` argv/stdin contract (including
the no-commands line and the planted role-dir AGENTS.md), the banked-shape event
parse, the V3-corrected reactive detection (top-level error / turn.failed events
plus non-zero exit — NOT a --json banner), the child-environment key strip, the
alternate CODEX_HOME provisioning with its auth-pointer symlink (and dead-symlink
refusal), AGENTS.md lifecycle, api_twin equivalent computation, and dispatch via
call_model.
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

# A stand-in `codex` binary. Records the call (argv, stdin, cwd, env, and the
# AGENTS.md it finds in cwd) to SHIM_CALL_DUMP when set, writes last-message.txt
# to cwd (honoring `-o`), and emits a --json event stream per SHIM_MODE. The two
# control vars are ordinary env names — not provider keys, not CLAUDE_CODE_* —
# so they survive the child-env strip and reach the shim.
_SHIM_SRC = r'''#!/usr/bin/env python3
import json, os, sys, time

stdin_data = sys.stdin.read()
argv = sys.argv

# Honor `-o <file>`: codex writes the final answer there, relative to cwd.
out_name = "last-message.txt"
if "-o" in argv:
    out_name = argv[argv.index("-o") + 1]

agents_md = None
try:
    with open(os.path.join(os.getcwd(), "AGENTS.md")) as f:
        agents_md = f.read()
except OSError:
    pass

dump = os.environ.get("SHIM_CALL_DUMP")
if dump:
    with open(dump, "w") as f:
        json.dump({
            "argv": argv,
            "stdin": stdin_data,
            "cwd": os.getcwd(),
            "env": dict(os.environ),
            "agents_md": agents_md,
        }, f)

mode = os.environ.get("SHIM_MODE", "ok")


def emit(ev):
    sys.stdout.write(json.dumps(ev) + "\n")


def write_last(text):
    with open(os.path.join(os.getcwd(), out_name), "w") as f:
        f.write(text)


_400 = json.dumps({"type": "error", "status": 400, "error": {
    "type": "invalid_request_error",
    "message": "The 'x' model is not supported when using Codex with a ChatGPT account."}})

if mode == "timeout":
    time.sleep(30)
    sys.exit(0)

if mode == "error_event":
    # rc 0 but a top-level error event: detection must still fire on the event.
    emit({"type": "thread.started", "thread_id": "t-shim"})
    emit({"type": "turn.started"})
    emit({"type": "error", "message": _400})
    emit({"type": "turn.failed", "error": {"message": _400}})
    sys.exit(0)

if mode == "limit":
    emit({"type": "thread.started", "thread_id": "t-shim"})
    emit({"type": "turn.started"})
    emit({"type": "error", "message": "You've hit your usage limit. Try again later."})
    sys.exit(1)

if mode == "turn_failed":
    emit({"type": "thread.started", "thread_id": "t-shim"})
    emit({"type": "turn.started"})
    emit({"type": "turn.failed", "error": {"message": "stream disconnected"}})
    sys.exit(1)

if mode == "nomsg":
    # success-looking events but no last-message.txt written.
    emit({"type": "thread.started", "thread_id": "t-shim"})
    emit({"type": "turn.started"})
    emit({"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 1}})
    sys.exit(0)

if mode == "empty":
    write_last("   ")
    emit({"type": "thread.started", "thread_id": "t-shim"})
    emit({"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 1}})
    sys.exit(0)

# ok — a banked-shape success stream + a review-shaped last message.
write_last(
    "REVIEW POINT 1:\nSEVERITY: high\nCATEGORY: correctness\n"
    "DESCRIPTION: shim codex finding one.\nRECOMMENDATION: fix it.\n\n"
    "REVIEW POINT 2:\nSEVERITY: low\nCATEGORY: testing\n"
    "DESCRIPTION: shim codex finding two.\nRECOMMENDATION: test it."
)
emit({"type": "thread.started", "thread_id": "t-shim"})
emit({"type": "turn.started"})
emit({"type": "item.completed", "item": {"id": "item_0", "type": "agent_message",
      "text": "done"}})
emit({"type": "turn.completed", "usage": {
    "input_tokens": 34656, "cached_input_tokens": 19200,
    "cache_write_input_tokens": 0, "output_tokens": 4333,
    "reasoning_output_tokens": 1080}})
sys.exit(0)
'''


@pytest.fixture
def codex_shim(tmp_path, monkeypatch):
    """Install a shim `codex` on PATH; isolate state + alt home + a fake auth src.

    The auth source is a plain file the lane only ever SYMLINKS to (never reads),
    so a bare touch is enough to keep the pointer alive.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir()
    shim = bindir / "codex"
    shim.write_text(_SHIM_SRC)
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])

    state_home = tmp_path / "state"
    monkeypatch.setenv("DVAD_STATE_HOME", str(state_home))
    alt_home = tmp_path / "codex-home"
    monkeypatch.setenv("DVAD_CODEX_HOME", str(alt_home))
    auth_src = tmp_path / "fake-auth.json"
    auth_src.write_text('{"pretend":"token"}')
    monkeypatch.setenv("DVAD_CODEX_AUTH_SRC", str(auth_src))

    call_dump = tmp_path / "call.json"
    monkeypatch.setenv("SHIM_CALL_DUMP", str(call_dump))

    class Handle:
        rolecalls = state_home / "rolecalls"
        home = alt_home
        auth = auth_src
        dump_path = call_dump

        def mode(self, m):
            monkeypatch.setenv("SHIM_MODE", m)

        def record(self):
            return json.loads(call_dump.read_text())

    return Handle()


@pytest.fixture(autouse=True)
def _restore_module_state():
    """Snapshot/restore module-level config-fed state around each test."""
    keys = set(cli._configured_key_envs)
    rates = dict(cli._model_rates)
    yield
    cli._configured_key_envs = keys
    cli._model_rates = rates


def _model(**kw) -> ModelConfig:
    kw.setdefault("name", "gpt-5.5-sub")
    kw.setdefault("provider", "codex-cli")
    kw.setdefault("model_id", "gpt-5.5")
    kw.setdefault("api_key_env", "")
    kw.setdefault("timeout", 30)
    return ModelConfig(**kw)


async def _call(model=None, system="sys prompt", user="user prompt"):
    model = model or _model()
    async with httpx.AsyncClient() as client:
        return await cli.call_codex_cli(client, model, system, user, mode="plan")


# ---------------------------------------------------------------------------
# Registration + dispatch
# ---------------------------------------------------------------------------


def test_registered_at_import():
    assert PROVIDER_REGISTRY["codex-cli"] is cli.call_codex_cli


async def test_dispatch_via_call_model(codex_shim):
    """call_model routes provider=codex-cli through the registry to the lane."""
    async with httpx.AsyncClient() as client:
        text, usage = await call_model(client, _model(), "sys", "usr", mode="plan")
    assert "REVIEW POINT 1" in text
    assert usage["input_tokens"] == 34656


# ---------------------------------------------------------------------------
# Happy path — contract, envelope, usage
# ---------------------------------------------------------------------------


async def test_happy_path_text_and_usage(codex_shim):
    text, usage = await _call()
    assert "REVIEW POINT 1" in text
    assert usage["input_tokens"] == 34656
    assert usage["output_tokens"] == 4333
    assert usage["cache_read_tokens"] == 19200
    assert usage["cache_write_tokens"] == 0
    # No api_twin rates loaded → no invented equivalent.
    assert "api_equivalent" not in usage


async def test_argv_and_stdin_contract(codex_shim):
    await _call(system="SYSTEM-CODEX", user="USER-BODY-42")
    rec = codex_shim.record()
    argv = rec["argv"]
    assert argv[0].endswith("codex")
    # probe R2 contract, verbatim.
    assert argv[1:] == [
        "exec", "--skip-git-repo-check", "--ignore-user-config", "--ephemeral",
        "-s", "read-only", "-m", "gpt-5.5",
        "-c", "model_reasoning_effort=high",
        "--json", "-o", "last-message.txt", "-",
    ]
    # The no-commands line is prepended to the user prompt on stdin.
    assert rec["stdin"].startswith(cli._CODEX_NO_COMMANDS_LINE)
    assert "USER-BODY-42" in rec["stdin"]
    # The role system prompt is planted as the role-dir AGENTS.md.
    assert rec["agents_md"] == "SYSTEM-CODEX"
    # Neutral scratch cwd under rolecalls/.
    assert Path(rec["cwd"]).parent == codex_shim.rolecalls


async def test_api_twin_equivalent_computed(codex_shim):
    # With the twin's rates registered, the lane prices the API-equivalent.
    cli.note_model_rates([("gpt-5.5", 0.00125, 0.010)])
    model = _model(extra={"api_twin": "gpt-5.5"})
    _, usage = await _call(model=model)
    # 34656/1000*0.00125 + 4333/1000*0.010
    expected = 34656 / 1000 * 0.00125 + 4333 / 1000 * 0.010
    assert usage["api_equivalent"] == pytest.approx(expected)
    assert usage["memo"].startswith("pool leg; computed API-equivalent $")
    assert "twin gpt-5.5" in usage["memo"]


# ---------------------------------------------------------------------------
# CODEX_HOME + auth pointer
# ---------------------------------------------------------------------------


def test_ensure_codex_home_symlinks_pointer(codex_shim):
    home = cli.ensure_codex_home()
    assert home == codex_shim.home
    assert (home.stat().st_mode & 0o777) == 0o700
    link = home / "auth.json"
    assert link.is_symlink()
    assert os.readlink(link) == str(codex_shim.auth)


def test_ensure_codex_home_dead_symlink_refuses(codex_shim):
    # The auth source vanishes (user signed out / moved dirs) → named refusal.
    codex_shim.auth.unlink()
    with pytest.raises(ProviderUnavailableError) as ei:
        cli.ensure_codex_home()
    assert "dead" in str(ei.value)


async def test_dead_symlink_fails_the_call(codex_shim):
    codex_shim.auth.unlink()
    with pytest.raises(ProviderUnavailableError) as ei:
        await _call()
    assert "dead" in str(ei.value)


async def test_codex_home_reaches_child(codex_shim):
    await _call()
    child_env = codex_shim.record()["env"]
    assert child_env.get("CODEX_HOME") == str(codex_shim.home)


# ---------------------------------------------------------------------------
# The child-env key strip (load-bearing)
# ---------------------------------------------------------------------------


async def test_env_strip_removes_every_provider_key(codex_shim, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-secret")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.setenv("CUSTOM_VENDOR_KEY", "vendor-secret")
    monkeypatch.setenv("MY_MODEL_KEY", "model-secret")
    cli.note_configured_key_envs(["CUSTOM_VENDOR_KEY", ""])

    await _call(model=_model(api_key_env="MY_MODEL_KEY"))
    child_env = codex_shim.record()["env"]

    for gone in (
        "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CLAUDECODE",
        "CLAUDE_CODE_ENTRYPOINT", "CUSTOM_VENDOR_KEY", "MY_MODEL_KEY",
    ):
        assert gone not in child_env, f"{gone} leaked into the codex child env"
    # The CLI still needs its own auth state: HOME/PATH pass through.
    assert child_env.get("HOME") == os.environ["HOME"]
    assert "PATH" in child_env


# ---------------------------------------------------------------------------
# Reactive detection → ProviderUnavailableError (V3-corrected D3.3)
# ---------------------------------------------------------------------------


async def test_error_event_detected_even_on_zero_exit(codex_shim):
    # rc 0, but a top-level error/turn.failed event → unavailable. Detection
    # keys on the EVENT, not just the exit code (the corrected D3.3 mechanism).
    codex_shim.mode("error_event")
    with pytest.raises(ProviderUnavailableError) as ei:
        await _call()
    assert "is not supported" in str(ei.value)


async def test_limit_shaped_exit_raises(codex_shim):
    codex_shim.mode("limit")
    with pytest.raises(ProviderUnavailableError) as ei:
        await _call()
    assert "usage limit" in str(ei.value)


async def test_turn_failed_raises(codex_shim):
    codex_shim.mode("turn_failed")
    with pytest.raises(ProviderUnavailableError) as ei:
        await _call()
    assert "stream disconnected" in str(ei.value)


async def test_missing_last_message_raises(codex_shim):
    codex_shim.mode("nomsg")
    with pytest.raises(ProviderUnavailableError) as ei:
        await _call()
    assert "no last-message" in str(ei.value)


async def test_empty_answer_raises(codex_shim):
    codex_shim.mode("empty")
    with pytest.raises(ProviderUnavailableError) as ei:
        await _call()
    assert "no answer text" in str(ei.value)


async def test_timeout_kills_and_raises(codex_shim):
    codex_shim.mode("timeout")
    with pytest.raises(ProviderUnavailableError) as ei:
        await _call(model=_model(timeout=1))
    assert "timed out" in str(ei.value)


# ---------------------------------------------------------------------------
# AGENTS.md lifecycle + scratch-dir lifecycle
# ---------------------------------------------------------------------------


async def test_scratch_deleted_on_success(codex_shim):
    await _call()
    leftovers = list(codex_shim.rolecalls.glob("codex-*"))
    assert leftovers == []


async def test_scratch_and_agents_retained_on_failure(codex_shim):
    codex_shim.mode("limit")
    with pytest.raises(ProviderUnavailableError):
        await _call()
    leftovers = list(codex_shim.rolecalls.glob("codex-*"))
    assert len(leftovers) == 1 and leftovers[0].is_dir()
    # The planted AGENTS.md is preserved as evidence on the retained failure.
    assert (leftovers[0] / "AGENTS.md").read_text() == "sys prompt"
