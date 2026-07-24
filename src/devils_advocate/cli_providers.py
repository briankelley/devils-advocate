"""Subscription CLI provider lanes — drive vendors' official CLIs as transports.

These providers route dvad role calls through the vendors' official
command-line tools in their documented headless modes: usage bills to the
user's own subscription plan (Claude Max, ChatGPT), and authentication belongs
entirely to those CLIs. dvad never sees, stores, or forwards subscription
credentials — it only spawns the CLI as-is and reads its output.

Each lane registers itself into ``PROVIDER_REGISTRY`` at import, so a
``provider: claude-cli`` (and, in a later journey, ``codex-cli``) models.yaml
entry dispatches here through the normal :func:`~devils_advocate.providers.call_model`
path with no special-casing.

The load-bearing safety property is the child-environment key strip
(:func:`_child_env`): the CLI subprocess must never inherit a provider API key,
or the pool leg would be silently rebilled to the paid API. dvad's own process
routinely holds those keys (the GUI writes them into ``os.environ``), so the
strip is structural, applied on every call.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import time
from pathlib import Path

import httpx  # dispatch hands every provider an AsyncClient; unused by CLI lanes

from .providers import MAX_OUTPUT_TOKENS, register_provider
from .types import ModelConfig, ProviderUnavailableError

# ─── Child-environment key strip (load-bearing) ──────────────────────────────

# Harness residue + the two hazard keys stripped from every CLI child. The
# `CLAUDE_CODE_*` prefix (added below) covers the session vars a Claude Code
# terminal exports; these two bare names are not prefixed. ANTHROPIC/OPENAI keys
# are the silent-flip hazard: present in the child, the CLI would bill the API.
_HARNESS_STRIP: frozenset[str] = frozenset(
    {"CLAUDECODE", "CLAUDE_EFFORT", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"}
)

# Every ``api_key_env`` name in the loaded config. Populated by
# ``config.load_config`` (which knows the full model set) so the strip removes
# *all* configured provider keys, not just the two hazard names above — the GUI
# process holds every one of them in os.environ. Replaced, not accumulated, on
# each config load so a stale key name never lingers.
_configured_key_envs: set[str] = set()


def note_configured_key_envs(names) -> None:
    """Record the configured ``api_key_env`` names for the child-env strip.

    Called by :func:`devils_advocate.config.load_config`. Falsy names (the
    empty ``api_key_env`` of a subscription ``-sub`` entry) are dropped.
    """
    global _configured_key_envs
    _configured_key_envs = {n for n in names if n}


def _child_env(model: ModelConfig, extra: dict | None = None) -> dict:
    """Build the subprocess environment: os.environ minus every provider key.

    Strips the harness/hazard set, every ``CLAUDE_CODE_*`` var, every configured
    ``api_key_env`` value, and this model's own ``api_key_env`` (defensive, in
    case the config registry was never populated). ``HOME``/``PATH``/``TERM`` and
    everything else pass through untouched — the CLI needs its own auth state
    under ``$HOME``. Over-stripping is always safe here (the CLI authenticates
    via its subscription, never a key); under-stripping is the hazard.
    """
    strip = set(_HARNESS_STRIP) | set(_configured_key_envs)
    if model.api_key_env:
        strip.add(model.api_key_env)
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in strip and not k.startswith("CLAUDE_CODE_")
    }
    if extra:
        env.update(extra)
    return env


# ─── Scratch (role) directories ──────────────────────────────────────────────

_STATE_HOME_ENV = "DVAD_STATE_HOME"


def _state_dir() -> Path:
    """Runtime state root for scratch dirs (and, later, the codex alt home).

    Distinct from the append-only data dir. Honors ``DVAD_STATE_HOME`` (test
    isolation), then ``XDG_STATE_HOME``, else the pinned ``~/.local/state/dvad``.
    A ``/tmp`` home is deliberately avoided (it trips a codex PATH-helper
    warning); tests point ``DVAD_STATE_HOME`` at a tmp path explicitly.
    """
    override = os.environ.get(_STATE_HOME_ENV)
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg) / "dvad"
    return Path.home() / ".local" / "state" / "dvad"


def _new_scratch_dir(prefix: str) -> Path:
    """Create a fresh, uniquely-named neutral cwd under ``rolecalls/``.

    The provider dispatch signature carries neither role name nor run stamp, so
    the directory is keyed by pid + monotonic nanoseconds — the actual
    requirement is collision-freedom under concurrent fan-out, which this
    guarantees.
    """
    root = _state_dir() / "rolecalls"
    root.mkdir(parents=True, exist_ok=True)
    d = root / f"{prefix}-{os.getpid()}-{time.monotonic_ns()}"
    d.mkdir(parents=False, exist_ok=False)
    return d


def _cleanup_scratch(path: Path, keep: bool) -> None:
    """Delete the scratch dir on success; retain it on failure (evidence)."""
    if keep:
        return
    shutil.rmtree(path, ignore_errors=True)


# ─── Concurrency posture (U2 → branch A) ─────────────────────────────────────

# U2 (deck V2) resolved BRANCH A: two concurrent `claude -p` calls on one Max
# account both succeed with real parallelism, so no lane semaphore is needed.
# The constant stays None (unset). Set it to a positive int to serialize claude
# calls (branch B) should a future CLI/plan ever reject concurrency.
_CLAUDE_MAX_CONCURRENCY: int | None = None
_claude_semaphore: asyncio.Semaphore | None = None


@contextlib.asynccontextmanager
async def _claude_slot():
    """Bound concurrent claude calls when a semaphore limit is set; else no-op."""
    global _claude_semaphore
    if _CLAUDE_MAX_CONCURRENCY is None:
        yield
        return
    if _claude_semaphore is None:
        _claude_semaphore = asyncio.Semaphore(_CLAUDE_MAX_CONCURRENCY)
    async with _claude_semaphore:
        yield


# ─── Claude Max lane ─────────────────────────────────────────────────────────


def _parse_claude_envelope(stdout: str, model_name: str) -> tuple[str, dict]:
    """Parse the `claude -p --output-format json` envelope (banked-shape U5).

    Returns ``(text, usage)`` where usage carries the standard token fields plus
    ``memo``/``api_equivalent`` when the CLI reports a cost. Any shape the parser
    cannot honor — unparseable JSON, ``is_error``, missing/empty ``result`` —
    raises :class:`ProviderUnavailableError` (fail loud, never a silent empty
    review).
    """
    try:
        env = json.loads(stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ProviderUnavailableError(
            f"{model_name}: claude CLI returned unparseable JSON ({exc})"
        ) from exc
    if not isinstance(env, dict):
        raise ProviderUnavailableError(
            f"{model_name}: claude CLI JSON was not an object"
        )
    if env.get("is_error"):
        raise ProviderUnavailableError(
            f"{model_name}: claude CLI reported is_error "
            f"(subtype={env.get('subtype')!r}, status={env.get('api_error_status')!r})"
        )
    text = env.get("result")
    if not isinstance(text, str) or not text.strip():
        raise ProviderUnavailableError(
            f"{model_name}: claude CLI returned no result text"
        )

    raw = env.get("usage") or {}
    usage: dict = {
        "input_tokens": raw.get("input_tokens", 0) or 0,
        "output_tokens": raw.get("output_tokens", 0) or 0,
        "cache_write_tokens": raw.get("cache_creation_input_tokens", 0) or 0,
        "cache_read_tokens": raw.get("cache_read_input_tokens", 0) or 0,
    }
    api_equiv = env.get("total_cost_usd")
    if isinstance(api_equiv, (int, float)) and not isinstance(api_equiv, bool):
        usage["api_equivalent"] = float(api_equiv)
        usage["memo"] = (
            f"pool leg; CLI-reported API-equivalent ${float(api_equiv):.3f}"
        )
    return text, usage


async def call_claude_cli(
    client: httpx.AsyncClient,
    model: ModelConfig,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = MAX_OUTPUT_TOKENS,
    mode: str = "",
) -> tuple[str, dict]:
    """Drive ``claude -p`` (Claude Max subscription) as a dvad transport.

    Encodes the probe §2 headless contract verbatim: a neutral empty cwd, the
    child-env key strip, tools/settings/MCP disabled, no session persistence,
    one-shot JSON output. The user prompt is delivered on stdin; the model's own
    subscription auth under ``$HOME`` does the signing. ``client``/``max_tokens``
    are accepted for dispatch-signature uniformity — the CLI exposes no HTTP
    client and no output-cap flag — and are unused. Returns the standard
    ``(text, usage)`` 2-tuple; usage carries the CLI-reported API-equivalent as
    ``memo``/``api_equivalent`` for the ledger. Any failure raises
    :class:`ProviderUnavailableError`.
    """
    scratch = _new_scratch_dir("claude")
    argv = [
        "claude", "-p", "--model", model.model_id,
        "--system-prompt", system_prompt,
        "--tools", "", "--setting-sources", "", "--strict-mcp-config",
        "--no-session-persistence", "--output-format", "json",
    ]
    env = _child_env(model)
    succeeded = False
    try:
        async with _claude_slot():
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(scratch),
                    env=env,
                )
            except (FileNotFoundError, OSError) as exc:
                raise ProviderUnavailableError(
                    f"{model.name}: claude CLI could not be launched ({exc})"
                ) from exc
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(input=user_prompt.encode("utf-8")),
                    timeout=model.timeout,
                )
            except (asyncio.TimeoutError, TimeoutError) as exc:
                proc.kill()
                await proc.wait()
                raise ProviderUnavailableError(
                    f"{model.name}: claude CLI timed out after {model.timeout}s"
                ) from exc

        if proc.returncode != 0:
            tail = stderr_b.decode("utf-8", "replace").strip()[-500:]
            raise ProviderUnavailableError(
                f"{model.name}: claude CLI exited {proc.returncode}"
                + (f": {tail}" if tail else "")
            )

        text, usage = _parse_claude_envelope(
            stdout_b.decode("utf-8", "replace"), model.name
        )
        succeeded = True
        return text, usage
    finally:
        _cleanup_scratch(scratch, keep=not succeeded)


# ─── Registration ────────────────────────────────────────────────────────────

register_provider("claude-cli", call_claude_cli)
