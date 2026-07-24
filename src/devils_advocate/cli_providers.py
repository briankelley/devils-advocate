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


# ─── API-twin rate table (codex equivalent-cost) ─────────────────────────────

# Per-model ($/1k input, $/1k output) rates, keyed by model NAME (the models.yaml
# key). Populated by ``config.load_config`` the same way the key-env set is: the
# provider dispatch signature can't carry the whole config, but the codex lane
# needs a twin's rates to compute an API-equivalent (the claude CLI reports its
# own cost; codex does not). ``extra.api_twin`` names the entry whose rates the
# equivalent is priced at. Empty until a config loads — a lane call before any
# load simply omits the equivalent, never guesses.
_model_rates: dict[str, tuple[float, float]] = {}


def note_model_rates(rates) -> None:
    """Record per-model ($/1k in, $/1k out) rates for codex api_twin pricing.

    Called by :func:`devils_advocate.config.load_config` with an iterable of
    ``(name, cost_per_1k_input, cost_per_1k_output)`` triples. Entries whose
    rates are unknown (``None``) are dropped — a missing twin rate means the
    equivalent is simply not computed, never invented. Replaced, not
    accumulated, on each load so a stale entry never lingers.
    """
    global _model_rates
    _model_rates = {
        name: (float(ci), float(co))
        for name, ci, co in rates
        if ci is not None and co is not None
    }


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


# ─── Codex (ChatGPT) lane ────────────────────────────────────────────────────

# Env overrides for test isolation (mirroring DVAD_STATE_HOME). In production
# both resolve to the pinned real paths: the alt CODEX_HOME under the state dir
# and the operator's own codex auth file. A `/tmp` home is deliberately avoided
# (probe R2: it trips a codex PATH-helper warning).
_CODEX_HOME_ENV = "DVAD_CODEX_HOME"
_CODEX_AUTH_SRC_ENV = "DVAD_CODEX_AUTH_SRC"

# The no-commands line prepended to every codex user prompt. `codex exec` is an
# agent turn, not a bare completion — without this it will read files / run shell
# commands (the probe measured two unprompted reads on the contaminated leg). The
# review is answered from the prompt alone; the role dir carries no tools.
_CODEX_NO_COMMANDS_LINE = (
    "Do not execute any commands or read any files; answer from the prompt alone."
)


def _codex_home() -> Path:
    """The alternate ``CODEX_HOME`` the lane runs under (never the operator's).

    Honors ``DVAD_CODEX_HOME`` (test isolation), then the pinned
    ``<state>/codex-home`` under the runtime state root. Kept distinct from the
    operator's ``~/.codex`` so their interactive ``AGENTS.md`` / config never
    reaches a role call.
    """
    override = os.environ.get(_CODEX_HOME_ENV)
    if override:
        return Path(override)
    return _state_dir() / "codex-home"


def _codex_auth_src() -> Path:
    """The operator's real codex auth file — a symlink TARGET, never read here."""
    override = os.environ.get(_CODEX_AUTH_SRC_ENV)
    if override:
        return Path(override)
    return Path.home() / ".codex" / "auth.json"


def ensure_codex_home() -> Path:
    """Provision the alt ``CODEX_HOME`` and (re)point its auth symlink.

    Creates ``<home>`` at 0700 and links ``<home>/auth.json`` to the operator's
    real codex auth file — a POINTER only: the target is never opened, read, or
    copied here (secrets law). Idempotent and re-verified every call: if the
    target is missing (user signed out / moved codex dirs) the symlink is dead
    and the lane is unavailable with a NAMED reason. Shared by the lane and the
    GUI status/test endpoints (one implementation, two consumers).

    Raises :class:`ProviderUnavailableError` when the auth pointer is dead.
    """
    home = _codex_home()
    home.mkdir(parents=True, exist_ok=True)
    os.chmod(home, 0o700)
    link = home / "auth.json"
    src = _codex_auth_src()
    # (Re)establish the pointer without ever reading the target.
    if link.is_symlink():
        if os.readlink(link) != str(src):
            link.unlink()
            link.symlink_to(src)
    elif link.exists():
        # A real file sits where the pointer belongs — replace with the symlink
        # (we never keep a copy of auth material under our control).
        link.unlink()
        link.symlink_to(src)
    else:
        link.symlink_to(src)
    if not src.exists():
        raise ProviderUnavailableError(
            f"codex lane unavailable: auth pointer {link} -> {src} is dead "
            f"(sign in with `codex login`)"
        )
    return home


def _extract_codex_error(message) -> str:
    """Pull a human string out of a codex error event's ``message`` field.

    The message is often a JSON-encoded envelope
    (``{"type":"error","status":400,"error":{"message": …}}``); dig out the
    inner ``error.message`` when present, else return the raw text.
    """
    if not isinstance(message, str):
        return str(message) if message else "codex reported an error"
    try:
        obj = json.loads(message)
    except (json.JSONDecodeError, ValueError):
        return message.strip()
    if isinstance(obj, dict):
        err = obj.get("error")
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
        if obj.get("message"):
            return str(obj["message"])
    return message.strip()


def _parse_codex_events(stdout: str) -> tuple[dict | None, str | None]:
    """Scan the ``codex exec --json`` event stream (one JSON object per line).

    Returns ``(usage, error_message)``: the ``turn.completed`` event's ``usage``
    block (or ``None``), and the first failure reason found (or ``None``).
    Detection keys on TOP-LEVEL ``type:"error"`` and ``type:"turn.failed"``
    events per the V3-corrected D3.3 — NOT a startup banner (there is none in
    ``--json`` mode) and NOT the nested ``item.completed`` metadata warning
    (which rides on an unsupported model but is not itself the failure). Lines
    that are not JSON objects are tolerated (never crash the parse).
    """
    usage: dict | None = None
    error_msg: str | None = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(ev, dict):
            continue
        etype = ev.get("type")
        if etype == "turn.completed":
            usage = ev.get("usage") or {}
        elif etype == "error" and error_msg is None:
            error_msg = _extract_codex_error(ev.get("message"))
        elif etype == "turn.failed" and error_msg is None:
            err = ev.get("error") or {}
            error_msg = _extract_codex_error(err.get("message"))
    return usage, error_msg


def _codex_usage(raw: dict, model: ModelConfig) -> dict:
    """Map codex ``turn.completed`` usage onto dvad's standard usage dict.

    Field mapping (banked-shape U5, stable trivial→champion):
    ``input_tokens`` → ``input_tokens``; ``output_tokens`` → ``output_tokens``;
    ``cached_input_tokens`` → ``cache_read_tokens``; ``cache_write_input_tokens``
    → ``cache_write_tokens``. codex reports no cost, so the API-equivalent is
    COMPUTED from the ``extra.api_twin`` entry's rates (resolved from the loaded
    config via :func:`note_model_rates`) when both are available.
    """
    usage: dict = {
        "input_tokens": raw.get("input_tokens", 0) or 0,
        "output_tokens": raw.get("output_tokens", 0) or 0,
        "cache_write_tokens": raw.get("cache_write_input_tokens", 0) or 0,
        "cache_read_tokens": raw.get("cached_input_tokens", 0) or 0,
    }
    twin = (model.extra or {}).get("api_twin")
    rates = _model_rates.get(twin) if twin else None
    if rates:
        ci, co = rates
        equiv = usage["input_tokens"] / 1000 * ci + usage["output_tokens"] / 1000 * co
        usage["api_equivalent"] = equiv
        usage["memo"] = (
            f"pool leg; computed API-equivalent ${equiv:.3f} (twin {twin})"
        )
    return usage


async def call_codex_cli(
    client: httpx.AsyncClient,
    model: ModelConfig,
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = MAX_OUTPUT_TOKENS,
    mode: str = "",
) -> tuple[str, dict]:
    """Drive ``codex exec`` (ChatGPT subscription) as a dvad transport.

    Encodes the probe R2 posture verbatim: an alternate ``CODEX_HOME`` whose only
    content is the auth pointer, a per-call role-dir ``AGENTS.md`` carrying the
    role system prompt (codex exec has NO system-prompt flag — the project doc IS
    the slot), the no-commands line prepended to the user prompt, read-only
    sandbox, ``--ignore-user-config``, ``--ephemeral``, one-shot ``--json`` with
    the answer written to ``last-message.txt``. ``project_doc_max_bytes=0`` is NOT
    set (it would kill the planted AGENTS.md); it stands as the degrade lever
    only. ``client``/``max_tokens`` are accepted for dispatch uniformity and
    unused. Returns the standard ``(text, usage)`` 2-tuple; any failure raises
    :class:`ProviderUnavailableError`.
    """
    home = ensure_codex_home()  # dead auth pointer → ProviderUnavailableError
    scratch = _new_scratch_dir("codex")
    # Plant the role system prompt as the role-dir AGENTS.md (the instruction
    # slot). If it can't be written, the call refuses to launch (no slot = no
    # call) rather than run an unguided review.
    try:
        (scratch / "AGENTS.md").write_text(system_prompt, encoding="utf-8")
    except OSError as exc:
        _cleanup_scratch(scratch, keep=True)
        raise ProviderUnavailableError(
            f"{model.name}: codex role-dir AGENTS.md could not be written ({exc})"
        ) from exc

    stdin_text = f"{_CODEX_NO_COMMANDS_LINE}\n\n{user_prompt}"
    argv = [
        "codex", "exec", "--skip-git-repo-check", "--ignore-user-config",
        "--ephemeral", "-s", "read-only", "-m", model.model_id,
        "-c", "model_reasoning_effort=high",
        "--json", "-o", "last-message.txt", "-",
    ]
    env = _child_env(model, {"CODEX_HOME": str(home)})
    succeeded = False
    try:
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
                f"{model.name}: codex CLI could not be launched ({exc})"
            ) from exc
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(input=stdin_text.encode("utf-8")),
                timeout=model.timeout,
            )
        except (asyncio.TimeoutError, TimeoutError) as exc:
            proc.kill()
            await proc.wait()
            raise ProviderUnavailableError(
                f"{model.name}: codex CLI timed out after {model.timeout}s"
            ) from exc

        stdout = stdout_b.decode("utf-8", "replace")
        usage_raw, error_msg = _parse_codex_events(stdout)

        # Reactive detection (D3.3, V3-corrected): error/turn.failed event OR a
        # non-zero exit → unavailable → failover. Refuse-and-failover, never a
        # silent substitution or a silently empty review.
        if proc.returncode != 0 or error_msg is not None:
            detail = error_msg
            if detail is None:
                tail = stderr_b.decode("utf-8", "replace").strip()[-500:]
                detail = f"exit {proc.returncode}" + (f": {tail}" if tail else "")
            raise ProviderUnavailableError(
                f"{model.name}: codex CLI unavailable: {detail}"
            )

        last_message = scratch / "last-message.txt"
        try:
            text = last_message.read_text(encoding="utf-8")
        except OSError as exc:
            raise ProviderUnavailableError(
                f"{model.name}: codex CLI wrote no last-message.txt ({exc})"
            ) from exc
        if not text.strip():
            raise ProviderUnavailableError(
                f"{model.name}: codex CLI returned no answer text"
            )

        usage = _codex_usage(usage_raw or {}, model)
        succeeded = True
        return text, usage
    finally:
        _cleanup_scratch(scratch, keep=not succeeded)


# ─── Registration ────────────────────────────────────────────────────────────

# The built-in subscription CLI lane provider names. resolve_effective_model
# (the master switch) and the load-time failover validation both key on this
# set to decide which entries route to a twin when the switch is off and which
# must declare an enabled non-CLI failover_model. Kept here so the lane roster
# lives in one place alongside the registrations below.
CLI_LANE_PROVIDERS: frozenset[str] = frozenset({"claude-cli", "codex-cli"})

register_provider("claude-cli", call_claude_cli)
register_provider("codex-cli", call_codex_cli)
