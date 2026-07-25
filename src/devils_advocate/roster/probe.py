"""Subscription-lane availability, established by asking rather than assuming.

models.dev describes what a provider sells through its API. It cannot describe
what a *subscription* will serve, and the two sets differ in ways no rule could
predict: on 2026-07-25 the codex lane served ``gpt-5.6-sol``, ``gpt-5.6-terra``
and ``gpt-5.6-luna`` while rejecting the bare ``gpt-5.6`` alias all three
descend from. So a model is only ever added to a CLI lane after the lane has
served it once.

The classifier here is deliberately narrower than the one dvad uses at review
time. ``_parse_codex_events`` raises ``ProviderUnavailableError`` on any error,
which is correct for failover — a dead lane should fall back whatever the
reason. It would be wrong here: a timeout would read as "model unavailable" and
quietly keep a perfectly good model out of the roster forever. A verdict of
*rejected* therefore requires positive proof, and everything else is transient.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone

from .state import now_iso

VERDICT_SERVES = "serves"
VERDICT_REJECTED = "rejected"
VERDICT_TRANSIENT = "transient"

# Re-probe this often. A plan upgrade or a provider rollout can turn a
# rejection into a service, so a "rejected" verdict is never permanent state.
RECHECK_DAYS = 7

PROBE_PROMPT = "Reply with exactly: OK"
PROBE_TIMEOUT = 120

# The only evidence that counts as a permanent, plan-level refusal.
CODEX_REJECTION = re.compile(
    r"not supported when using Codex with a ChatGPT account", re.IGNORECASE
)
# Claude has no observed rejection string yet; these are the shapes a bad model
# name would plausibly take. Absent a match, the verdict stays transient.
CLAUDE_REJECTION = re.compile(
    r"(unknown|invalid|unsupported|not found).{0,30}model|model.{0,30}(not found|not available)",
    re.IGNORECASE,
)


def _clean_env() -> dict:
    """Subprocess environment with every provider key stripped.

    Reuses the lane's own hazard set: dvad's GUI process holds all provider
    keys in ``os.environ``, and an unstripped child could silently bill a
    subscription probe to the paid API.
    """
    # Read through the module rather than binding names: the configured-key set
    # is a module global that load_config rebinds, and a stale snapshot here
    # would mean a key we meant to strip stays in the child environment.
    from .. import cli_providers as lanes

    strip = set(lanes._HARNESS_STRIP) | set(lanes._configured_key_envs)
    return {
        k: v
        for k, v in os.environ.items()
        if k not in strip and not k.startswith("CLAUDE_CODE_")
    }


def _run(argv: list[str], timeout: int) -> tuple[int, str]:
    """Run a probe, returning ``(returncode, stdout+stderr)``. Never raises."""
    try:
        proc = subprocess.run(
            argv,
            input=PROBE_PROMPT,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_clean_env(),
        )
        return proc.returncode, f"{proc.stdout}\n{proc.stderr}"
    except FileNotFoundError:
        return 127, "binary not found"
    except subprocess.TimeoutExpired:
        return 124, "probe timed out"
    except OSError as exc:
        return 126, f"probe could not launch: {exc}"


def _codex_errors(stdout: str) -> list[str]:
    """Every error message in the event stream, nested payloads unwrapped.

    Both error events matter to look at, but only one of them means anything:
    the first is a "model metadata not found, defaulting to fallback" warning
    that reads like the cause and is not. Scanning all of them and matching on
    the 400 is what keeps the warning from being mistaken for a rejection.
    """
    messages: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") not in ("error", "turn.failed"):
            continue
        raw = event.get("message") or (event.get("error") or {}).get("message") or ""
        messages.append(str(raw))
        # A nested payload arrives as a JSON string inside ``message``.
        try:
            inner = json.loads(raw)
            if isinstance(inner, dict):
                messages.append(str((inner.get("error") or {}).get("message", "")))
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
    return [m for m in messages if m]


def probe_codex(model_id: str, timeout: int = PROBE_TIMEOUT) -> tuple[str, str]:
    """Ask the codex lane whether it will serve *model_id*."""
    argv = [
        "codex", "exec", "--skip-git-repo-check", "--ignore-user-config",
        "--ephemeral", "-s", "read-only", "-m", model_id, "--json", "-",
    ]
    code, output = _run(argv, timeout)
    if code == 0:
        return VERDICT_SERVES, "lane returned a completed turn"
    for message in _codex_errors(output):
        if CODEX_REJECTION.search(message):
            return VERDICT_REJECTED, message.strip()[:300]
    return VERDICT_TRANSIENT, f"exit {code}, no plan-level rejection found"


def probe_claude(model_id: str, timeout: int = PROBE_TIMEOUT) -> tuple[str, str]:
    """Ask the claude lane whether it will serve *model_id*."""
    argv = [
        "claude", "-p", "--model", model_id,
        "--tools", "", "--setting-sources", "", "--strict-mcp-config",
        "--no-session-persistence", "--output-format", "json",
    ]
    code, output = _run(argv, timeout)
    if code == 0:
        try:
            envelope = json.loads(output.strip().splitlines()[0])
            if isinstance(envelope, dict) and envelope.get("is_error"):
                return VERDICT_TRANSIENT, "lane reported is_error on a zero exit"
        except (json.JSONDecodeError, ValueError, IndexError):
            pass
        return VERDICT_SERVES, "lane returned a successful result envelope"
    if CLAUDE_REJECTION.search(output):
        return VERDICT_REJECTED, output.strip()[:300]
    return VERDICT_TRANSIENT, f"exit {code}, no model-level rejection found"


PROBES = {"codex-cli": probe_codex, "claude-cli": probe_claude}


def is_fresh(entry: dict | None, *, now: datetime | None = None) -> bool:
    """Whether a cached verdict is recent enough to reuse."""
    if not entry or not entry.get("checked"):
        return False
    try:
        checked = datetime.fromisoformat(entry["checked"])
    except (ValueError, TypeError):
        return False
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) - checked < timedelta(days=RECHECK_DAYS)


def check(lane: str, model_id: str, state: dict, *, force: bool = False) -> str:
    """Return a verdict for *model_id* on *lane*, caching it in *state*.

    A transient verdict is never cached — an unreachable lane today says
    nothing about tomorrow, and caching it would turn a network blip into a
    week-long blackout for that model.
    """
    availability = state.setdefault("availability", {})
    key = f"{lane}/{model_id}"
    cached = availability.get(key)
    if not force and is_fresh(cached):
        return cached["verdict"]

    probe = PROBES.get(lane)
    if probe is None:
        return VERDICT_TRANSIENT

    verdict, detail = probe(model_id)
    if verdict != VERDICT_TRANSIENT:
        availability[key] = {
            "verdict": verdict,
            "detail": detail,
            "checked": now_iso(),
        }
    return verdict
