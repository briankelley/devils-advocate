"""Tests for the subscription backends configuration surface (Journey 5 / D5+D6).

Covers the cli_providers status/detection/sign-in/Test helpers against shim
`claude`/`codex` executables, the three GUI endpoints (status/test/provision)
including provision's `.bak` + idempotence + roster-safety, the settings-toggle
allowlist growth, the SUB cost tier, the reviewer ceiling-3 save path, and the
money surfaces (report line, ledger field, completed-table equivalents).

Shim-CLI permutations (found/absent/signed-in) live here rather than in
Playwright because they are deterministic and fast; the e2e suite covers section
placement, toggle persistence, and the visual baseline.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

import devils_advocate.cli_providers as cli
from devils_advocate.types import ModelConfig


# ---------------------------------------------------------------------------
# Shim harness — a `claude` / `codex` pair answering the probe subcommands
# ---------------------------------------------------------------------------

_CLAUDE_SHIM = r'''#!/usr/bin/env python3
import json, os, sys

argv = sys.argv[1:]
if argv[:1] == ["--version"]:
    print("2.1.201 (Claude Code)")
    sys.exit(0)
if argv[:2] == ["auth", "status"]:
    state = os.environ.get("SHIM_CLAUDE_AUTH", "in")
    if state == "in":
        print(json.dumps({"loggedIn": True, "authMethod": "claude.ai",
                          "subscriptionType": "max", "email": "REDACTED"}))
    elif state == "out":
        print(json.dumps({"loggedIn": False}))
    else:  # garbage
        print("not json")
    sys.exit(0)
# `-p` lane call (Test button): emit a banked-shape success envelope.
sys.stdin.read()
print(json.dumps({
    "type": "result", "subtype": "success", "is_error": False,
    "result": "subscription lane OK.",
    "total_cost_usd": 0.001,
    "usage": {"input_tokens": 3, "output_tokens": 5,
              "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
}))
sys.exit(0)
'''

_CODEX_SHIM = r'''#!/usr/bin/env python3
import os, sys, json

argv = sys.argv[1:]
if argv[:1] == ["--version"]:
    print("codex-cli 0.145.0")
    sys.exit(0)
if argv[:2] == ["login", "status"]:
    state = os.environ.get("SHIM_CODEX_AUTH", "in")
    if state == "in":
        # j0 finding: codex writes the logged-in line to STDERR, rc 0.
        sys.stderr.write("Logged in using ChatGPT\n")
        sys.exit(0)
    sys.stderr.write("Not logged in\n")
    sys.exit(1)
# `exec` lane call (Test button): emit an event stream + last-message file.
sys.stdin.read()
# find the -o target
out = "last-message.txt"
if "-o" in argv:
    out = argv[argv.index("-o") + 1]
with open(out, "w") as f:
    f.write("subscription lane OK.")
print(json.dumps({"type": "turn.completed",
                  "usage": {"input_tokens": 4, "output_tokens": 6,
                            "cached_input_tokens": 0, "cache_write_input_tokens": 0}}))
sys.exit(0)
'''


def _install(bindir: Path, name: str, src: str) -> None:
    p = bindir / name
    p.write_text(src)
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def lane_shims(tmp_path, monkeypatch):
    """Install shim claude+codex on PATH, isolate state + codex home + auth."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _install(bindir, "claude", _CLAUDE_SHIM)
    _install(bindir, "codex", _CODEX_SHIM)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])

    state = tmp_path / "state"
    monkeypatch.setenv("DVAD_STATE_HOME", str(state))
    codex_home = tmp_path / "codex-home"
    monkeypatch.setenv("DVAD_CODEX_HOME", str(codex_home))
    auth_src = tmp_path / "auth.json"
    auth_src.write_text("{}")  # a live (present) auth pointer target
    monkeypatch.setenv("DVAD_CODEX_AUTH_SRC", str(auth_src))
    return tmp_path


@pytest.fixture(autouse=True)
def _restore_module_state():
    snap_keys = set(cli._configured_key_envs)
    snap_rates = dict(cli._model_rates)
    yield
    cli._configured_key_envs = snap_keys
    cli._model_rates = snap_rates


# ---------------------------------------------------------------------------
# Detection + version + sign-in (lane_status / subscription_status)
# ---------------------------------------------------------------------------


async def test_lane_status_found_and_signed_in(lane_shims):
    st = await cli.lane_status("claude-cli")
    assert st["found"] is True
    assert st["version"] == "2.1.201"
    assert st["signin"]["state"] == "signed_in"
    assert "max" in st["signin"]["detail"]


async def test_lane_status_codex_signin_from_stderr(lane_shims):
    st = await cli.lane_status("codex-cli")
    assert st["found"] is True
    assert st["version"] == "0.145.0"
    # j0 finding: the logged-in line arrives on stderr; we still read it.
    assert st["signin"]["state"] == "signed_in"
    assert "ChatGPT" in st["signin"]["detail"]


async def test_lane_status_signed_out(lane_shims, monkeypatch):
    monkeypatch.setenv("SHIM_CLAUDE_AUTH", "out")
    monkeypatch.setenv("SHIM_CODEX_AUTH", "out")
    cl = await cli.lane_status("claude-cli")
    cx = await cli.lane_status("codex-cli")
    assert cl["signin"]["state"] == "signed_out"
    assert cx["signin"]["state"] == "signed_out"


async def test_lane_status_not_found(tmp_path, monkeypatch):
    # Empty PATH → neither binary resolves.
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    st = await cli.lane_status("claude-cli")
    assert st["found"] is False
    assert st["version"] is None
    assert st["signin"]["state"] == "unknown"


async def test_claude_signin_secrets_law_no_email(lane_shims):
    """Sign-in detail must NEVER carry email/org — only loggedIn+subscription."""
    s = await cli._claude_signin()
    assert "REDACTED" not in s["detail"]
    assert "@" not in s["detail"]


async def test_subscription_status_aggregate(lane_shims):
    cfg = {"all_models": {}, "models": {}, "subscription_backend": True}
    data = await cli.subscription_status(cfg)
    assert data["enabled"] is True
    assert data["has_cli_entries"] is False
    assert {l["lane"] for l in data["lanes"]} == {"claude-cli", "codex-cli"}
    assert all(l["configured"] is False for l in data["lanes"])


async def test_subscription_status_detects_cli_entries(lane_shims):
    m = ModelConfig(name="opus-sub", provider="claude-cli",
                    model_id="claude-opus-4-8", api_key_env="",
                    failover_model="opus", extra={"api_twin": "opus"})
    cfg = {"all_models": {"opus-sub": m}, "models": {"opus-sub": m},
           "subscription_backend": False}
    data = await cli.subscription_status(cfg)
    assert data["has_cli_entries"] is True
    claude_lane = next(l for l in data["lanes"] if l["lane"] == "claude-cli")
    assert claude_lane["configured"] is True


# ---------------------------------------------------------------------------
# The Test button — exercises the REAL lane functions
# ---------------------------------------------------------------------------


async def test_lane_test_claude_ok(lane_shims):
    model = cli.lane_test_model({"all_models": {}}, "claude-cli")
    result = await cli.lane_test("claude-cli", model)
    assert result["ok"] is True
    assert "OK" in result["detail"]
    assert result["input_tokens"] == 3


async def test_lane_test_codex_ok(lane_shims):
    model = cli.lane_test_model({"all_models": {}}, "codex-cli")
    result = await cli.lane_test("codex-cli", model)
    assert result["ok"] is True
    assert "OK" in result["detail"]


async def test_lane_test_failure_is_clean(tmp_path, monkeypatch):
    # No shim on PATH → the lane raises ProviderUnavailableError → ok:false.
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setenv("DVAD_STATE_HOME", str(tmp_path / "state"))
    model = cli.lane_test_model({"all_models": {}}, "claude-cli")
    result = await cli.lane_test("claude-cli", model)
    assert result["ok"] is False
    assert result["detail"]


def test_lane_test_model_prefers_configured_entry():
    m = ModelConfig(name="opus-sub", provider="claude-cli",
                    model_id="claude-opus-4-8", api_key_env="", timeout=1200)
    chosen = cli.lane_test_model({"all_models": {"opus-sub": m}}, "claude-cli")
    assert chosen.model_id == "claude-opus-4-8"
    assert chosen.timeout <= int(cli._TEST_CALL_TIMEOUT)  # capped for the Test


def test_lane_test_model_falls_back_to_default():
    chosen = cli.lane_test_model({"all_models": {}}, "codex-cli")
    assert chosen.provider == "codex-cli"
    assert chosen.model_id == "gpt-5.5"


# ---------------------------------------------------------------------------
# GUI endpoints — status / test / provision
# ---------------------------------------------------------------------------


def _make_request(config_path=None, csrf="tok-123", headers=None):
    from unittest.mock import MagicMock
    request = MagicMock()
    request.app.state.csrf_token = csrf
    request.app.state.config_path = config_path
    hdr = {"X-DVAD-Token": csrf}
    if headers:
        hdr.update(headers)
    request.headers.get = lambda k, d="": hdr.get(k, d)
    return request


_CONFIG_WITH_TWINS = """\
models:
  claude-fable-5:
    provider: anthropic
    model_id: claude-fable-5
    api_key_env: ANTHROPIC_API_KEY
    context_window: 200000
    cost_per_1k_input: 0.001
    cost_per_1k_output: 0.005
    timeout: 900
  claude-opus-4-8:
    provider: anthropic
    model_id: claude-opus-4-8
    api_key_env: ANTHROPIC_API_KEY
    context_window: 200000
    cost_per_1k_input: 0.005
    cost_per_1k_output: 0.025
    timeout: 900
  gpt-5.5:
    provider: openai
    model_id: gpt-5.5
    api_key_env: OPENAI_API_KEY
    api_base: https://api.openai.com/v1
    context_window: 400000
    cost_per_1k_input: 0.002
    cost_per_1k_output: 0.008
    timeout: 900
roles:
  author: claude-opus-4-8
  reviewers: [claude-opus-4-8, gpt-5.5]
  deduplication: gpt-5.5
"""


async def test_status_endpoint(lane_shims, tmp_path):
    from devils_advocate.gui.api import subscription_status_endpoint

    cfg = tmp_path / "models.yaml"
    cfg.write_text(_CONFIG_WITH_TWINS)
    resp = await subscription_status_endpoint(_make_request(str(cfg)))
    body = json.loads(resp.body)
    assert body["enabled"] is False
    assert body["has_cli_entries"] is False
    assert len(body["lanes"]) == 2


async def test_test_endpoint_unknown_lane(tmp_path):
    from fastapi import HTTPException
    from devils_advocate.gui.api import subscription_test

    with pytest.raises(HTTPException) as exc:
        await subscription_test(_make_request(), "bogus-cli")
    assert exc.value.status_code == 404


async def test_test_endpoint_ok(lane_shims, tmp_path):
    from devils_advocate.gui.api import subscription_test

    cfg = tmp_path / "models.yaml"
    cfg.write_text(_CONFIG_WITH_TWINS)
    resp = await subscription_test(_make_request(str(cfg)), "claude-cli")
    body = json.loads(resp.body)
    assert body["ok"] is True


async def test_provision_creates_entries_and_backup(tmp_path):
    from devils_advocate.gui.api import subscription_provision

    cfg = tmp_path / "models.yaml"
    cfg.write_text(_CONFIG_WITH_TWINS)
    resp = await subscription_provision(_make_request(str(cfg)))
    body = json.loads(resp.body)
    assert set(body["created"]) == {
        "claude-fable-5-sub", "claude-opus-4-8-sub", "gpt-5.5-sub"
    }
    # .bak created
    assert (tmp_path / "models.yaml.bak").exists()

    # Entries wired correctly, roster untouched.
    import yaml
    data = yaml.safe_load(cfg.read_text())
    fable = data["models"]["claude-fable-5-sub"]
    assert fable["provider"] == "claude-cli"
    assert fable["failover_model"] == "claude-fable-5"
    assert fable["extra"]["api_twin"] == "claude-fable-5"
    gpt = data["models"]["gpt-5.5-sub"]
    assert gpt["provider"] == "codex-cli"
    assert gpt["min_points_hint"] == 20
    # roles block untouched
    assert data["roles"]["author"] == "claude-opus-4-8"
    assert "reviewers" in data["roles"] and len(data["roles"]["reviewers"]) == 2

    # The provisioned config loads clean (failover validation passes).
    from devils_advocate.config import load_config
    load_config(cfg)


async def test_provision_idempotent(tmp_path):
    from devils_advocate.gui.api import subscription_provision

    cfg = tmp_path / "models.yaml"
    cfg.write_text(_CONFIG_WITH_TWINS)
    await subscription_provision(_make_request(str(cfg)))
    resp2 = await subscription_provision(_make_request(str(cfg)))
    body2 = json.loads(resp2.body)
    assert body2["created"] == []
    assert any("already present" in s for s in body2["skipped"])


async def test_provision_skips_when_no_twin(tmp_path):
    from devils_advocate.gui.api import subscription_provision

    # Only a gpt twin exists → the two claude -sub entries skip-with-message.
    cfg = tmp_path / "models.yaml"
    cfg.write_text("""\
models:
  gpt-5.5:
    provider: openai
    model_id: gpt-5.5
    api_key_env: OPENAI_API_KEY
    api_base: https://api.openai.com/v1
    cost_per_1k_input: 0.002
    cost_per_1k_output: 0.008
    timeout: 900
roles:
  author: gpt-5.5
  reviewers: [gpt-5.5]
  deduplication: gpt-5.5
""")
    resp = await subscription_provision(_make_request(str(cfg)))
    body = json.loads(resp.body)
    assert body["created"] == ["gpt-5.5-sub"]
    assert any("claude-fable-5-sub" in s and "no API twin" in s for s in body["skipped"])


# ---------------------------------------------------------------------------
# Settings-toggle allowlist growth
# ---------------------------------------------------------------------------


async def test_settings_toggle_accepts_subscription_backend(tmp_path):
    from devils_advocate.gui.api import set_settings_toggle

    cfg = tmp_path / "models.yaml"
    cfg.write_text("models: {}\nsettings:\n  live_testing: false\n")
    req = _make_request(str(cfg))
    req.json = _async_return({"key": "subscription_backend", "value": True})
    resp = await set_settings_toggle(req)
    assert json.loads(resp.body)["value"] is True
    assert "subscription_backend" in cfg.read_text()


def _async_return(value):
    from unittest.mock import AsyncMock
    return AsyncMock(return_value=value)


# ---------------------------------------------------------------------------
# SUB cost tier
# ---------------------------------------------------------------------------


def test_sub_cost_tier():
    from devils_advocate.gui.pages import _compute_cost_tiers

    models = {
        "opus-sub": ModelConfig(name="opus-sub", provider="claude-cli",
                                model_id="claude-opus-4-8", api_key_env="",
                                cost_per_1k_input=0, cost_per_1k_output=0),
        "gpt": ModelConfig(name="gpt", provider="openai", model_id="gpt-5.5",
                           api_key_env="K", cost_per_1k_input=0.002,
                           cost_per_1k_output=0.008),
    }
    tiers = _compute_cost_tiers(models)
    assert tiers["opus-sub"] == "sub"
    assert isinstance(tiers["gpt"], int) and tiers["gpt"] >= 1


# ---------------------------------------------------------------------------
# Reviewer ceiling 3 (save path)
# ---------------------------------------------------------------------------


async def test_structured_save_three_reviewers(tmp_path):
    from devils_advocate.gui.api import _save_structured_config

    cfg = tmp_path / "models.yaml"
    cfg.write_text("models: {}\nroles: {}\n")
    body = {"roles": {"author": "a", "reviewer1": "r1", "reviewer2": "r2",
                      "reviewer3": "r3"}, "thinking": {}}
    await _save_structured_config(_make_request(str(cfg)), body)
    import yaml
    data = yaml.safe_load(cfg.read_text())
    assert data["roles"]["reviewers"] == ["r1", "r2", "r3"]


def test_config_page_has_reviewer3_slot(tmp_path):
    from devils_advocate.gui.pages import _build_role_display_entries

    cfg = tmp_path / "models.yaml"
    cfg.write_text("""\
models:
  a: {provider: openai, model_id: a, api_key_env: K, api_base: http://x/v1, cost_per_1k_input: 1, cost_per_1k_output: 1}
  b: {provider: openai, model_id: b, api_key_env: K, api_base: http://x/v1, cost_per_1k_input: 1, cost_per_1k_output: 1}
  c: {provider: openai, model_id: c, api_key_env: K, api_base: http://x/v1, cost_per_1k_input: 1, cost_per_1k_output: 1}
roles:
  author: a
  reviewers: [a, b, c]
  deduplication: a
""")
    # Config page passes 3 slots; the dashboard default keeps 2.
    entries3, _ = _build_role_display_entries(str(cfg), 3)
    labels3 = [e["label"] for e in entries3]
    assert "Reviewer 3" in labels3
    r3 = next(e for e in entries3 if e["label"] == "Reviewer 3")
    assert r3["model"] == "c"

    entries2, _ = _build_role_display_entries(str(cfg))
    assert "Reviewer 3" not in [e["label"] for e in entries2]


# ---------------------------------------------------------------------------
# Money surfaces — report line, ledger field, completed-table equivalents
# ---------------------------------------------------------------------------


def _pool_result():
    from devils_advocate.types import CostTracker, ReviewResult
    ct = CostTracker()
    ct.add("gpt-5.5", 100, 50, 0.002, 0.008, role="reviewer_1")  # paid leg
    ct.add("claude-opus-4-8-sub", 2, 40, 0, 0, role="reviewer_2",
           memo="pool leg", api_equivalent=0.14)  # pool leg
    return ReviewResult(
        review_id="r1", mode="plan", input_file="p.md", project="proj",
        timestamp="2026-07-24", author_model="claude-opus-4-8",
        reviewer_models=["gpt-5.5", "claude-opus-4-8-sub"],
        dedup_model="gpt-5.5", points=[], groups=[], author_responses=[],
        governance_decisions=[], cost=ct,
    )


def test_report_shows_subscription_covered_line():
    from devils_advocate.output import generate_report
    result = _pool_result()
    report = generate_report(result)
    assert "Subscription-covered (API-equivalent):" in report
    assert "$0.14" in report


def test_report_omits_line_without_pool():
    from devils_advocate.types import CostTracker, ReviewResult
    from devils_advocate.output import generate_report
    ct = CostTracker()
    ct.add("gpt-5.5", 100, 50, 0.002, 0.008, role="reviewer_1")
    result = ReviewResult(
        review_id="r1", mode="plan", input_file="p.md", project="proj",
        timestamp="t", author_model="gpt-5.5", reviewer_models=["gpt-5.5"],
        dedup_model="gpt-5.5", points=[], groups=[], author_responses=[],
        governance_decisions=[], cost=ct,
    )
    assert "Subscription-covered" not in generate_report(result)


def test_ledger_carries_total_api_equivalent():
    from devils_advocate.output import generate_ledger
    result = _pool_result()
    ledger = generate_ledger(result)
    assert ledger["cost"]["total_api_equivalent_usd"] == pytest.approx(0.14)


def test_ledger_omits_equivalent_without_pool():
    from devils_advocate.types import CostTracker, ReviewResult
    from devils_advocate.output import generate_ledger
    ct = CostTracker()
    ct.add("gpt-5.5", 100, 50, 0.002, 0.008, role="reviewer_1")
    result = ReviewResult(
        review_id="r1", mode="plan", input_file="p.md", project="proj",
        timestamp="t", author_model="gpt-5.5", reviewer_models=["gpt-5.5"],
        dedup_model="gpt-5.5", points=[], groups=[], author_responses=[],
        governance_decisions=[], cost=ct,
    )
    assert "total_api_equivalent_usd" not in generate_ledger(result)["cost"]


def test_completed_cost_rows_carry_equivalents():
    from devils_advocate.gui.pages import _build_role_cost_rows
    ledger = {
        "result": "success",
        "author_model": "claude-opus-4-8",
        "reviewer_models": ["gpt-5.5", "claude-opus-4-8-sub"],
        "cost": {
            "role_costs": {"reviewer_1": 0.002, "reviewer_2": 0.0},
            "entries": [
                {"role": "reviewer_1", "cost_usd": 0.002},
                {"role": "reviewer_2", "cost_usd": 0.0, "api_equivalent": 0.14},
            ],
        },
    }
    rows = _build_role_cost_rows(ledger, "—", "—", "—")
    # 4-tuple: (label, model, cost, equiv)
    by_label = {r[0]: r for r in rows}
    assert by_label["reviewer 2"][3] == pytest.approx(0.14)
    assert by_label["reviewer 1"][3] is None
