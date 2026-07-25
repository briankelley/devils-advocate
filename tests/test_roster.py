"""Tests for the model-roster scanner.

The laws are the point. Most of what follows checks that the scanner cannot do
the destructive thing rather than that it does the useful thing — a gate that
admits one model too few is a mild disappointment, whereas one that evicts a
role-assigned model makes dvad refuse to load.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml as pyyaml

from devils_advocate.roster import probe as probe_mod
from devils_advocate.roster import reason as reason_mod
from devils_advocate.roster import state as state_mod
from devils_advocate.roster.apply import apply, build_entry, plan_refreshes
from devils_advocate.roster.fetch import digest, sanity_check
from devils_advocate.roster.gate import Candidate, age_days, gate
from devils_advocate.roster.provider_map import learn_conventions
from devils_advocate.roster.types import ApplyError, FetchError

TODAY = date(2026, 7, 25)


def model(**kw):
    """A minimally valid models.dev record."""
    base = {
        "modalities": {"input": ["text"], "output": ["text"]},
        "cost": {"input": 5.0, "output": 25.0},
        "limit": {"context": 200_000, "output": 64_000},
        "release_date": "2026-07-01",
        "family": "fam",
    }
    base.update(kw)
    return base


def payload(**models):
    return {
        "anthropic": {"id": "anthropic", "models": models or {"claude-x": model()}},
        "openai": {"id": "openai", "models": {"gpt-x": model()}},
    }


# ─── L1: additive only ─────────────────────────────────────────────────────


def test_configured_model_failing_a_rule_is_never_a_candidate():
    """A live roster entry that fails the gate is reported, never proposed."""
    data = payload(**{f"claude-{i}": model(release_date=f"2026-0{i}-01") for i in range(1, 6)})
    raw_models = {"claude-1": {"model_id": "claude-1"}}
    result = gate(data, raw_models, {"author": "claude-1"}, today=TODAY, family_cap=2)

    assert "claude-1" not in {c.model_id for c in result.candidates}
    dropped = [r for r in result.rejections if r.model_id == "claude-1"]
    assert dropped and dropped[0].in_config is True


def test_role_assigned_model_beyond_family_cap_survives_as_config():
    """The exact shape that would brick the config: deep-in-family + a role."""
    data = payload(**{f"m{i}": model(release_date=f"2026-0{i}-01") for i in range(1, 5)})
    raw_models = {"m1": {"model_id": "m1"}}
    result = gate(data, raw_models, {"integration_reviewer": "m1"}, today=TODAY, family_cap=2)

    # It is out of the candidate pool but there is no removal signal anywhere.
    assert all(c.model_id != "m1" for c in result.candidates)
    assert all(h.name != "m1" for h in result.health)  # not flagged as unhealthy


def test_apply_never_overwrites_an_existing_entry(tmp_path):
    config = tmp_path / "models.yaml"
    config.write_text(
        pyyaml.safe_dump(
            {
                "models": {"keep": {"model_id": "keep", "thinking": True, "timeout": 999}},
                "roles": {"author": "keep"},
            }
        )
    )
    original = config.read_text()

    # An addition colliding with an existing name is dropped, not applied.
    # Nothing then differs, so apply() reports no write and leaves the file —
    # and crucially the operator's hand-set thinking/timeout — exactly as found.
    wrote = apply(config, {"keep": {"model_id": "other"}}, [], storage=_FakeStorage())

    assert wrote is False
    assert config.read_text() == original
    kept = pyyaml.safe_load(config.read_text())["models"]["keep"]
    assert kept == {"model_id": "keep", "thinking": True, "timeout": 999}


def test_apply_leaves_roles_untouched(tmp_path, monkeypatch):
    config = tmp_path / "models.yaml"
    config.write_text(
        pyyaml.safe_dump(
            {
                "models": {"a": {"model_id": "a", "provider": "openai"}},
                "roles": {"author": "a", "reviewers": ["a"], "revision": None},
            }
        )
    )
    monkeypatch.setattr("devils_advocate.roster.apply._validate", lambda *a, **k: None)

    apply(config, {"b": {"model_id": "b", "provider": "openai"}}, [], storage=_FakeStorage())

    after = pyyaml.safe_load(config.read_text())
    assert after["roles"] == {"author": "a", "reviewers": ["a"], "revision": None}
    assert set(after["models"]) == {"a", "b"}


# ─── L2: field ownership ───────────────────────────────────────────────────


def test_thinking_is_never_taken_from_upstream_reasoning():
    """upstream `reasoning` is a capability claim; `thinking` is an operator choice."""
    candidate = Candidate("anthropic", "fam", "m", model(reasoning=True))
    entry = build_entry(candidate, "top", {"anthropic": {"provider": "anthropic"}})
    assert entry["thinking"] is False


def test_subscription_rows_keep_their_zero_costs():
    raw_models = {
        "x-sub": {
            "model_id": "x",
            "provider": "claude-cli",
            "cost_per_1k_input": 0,
            "cost_per_1k_output": 0,
            "context_window": 1_000_000,
        }
    }
    upstream = {"x": model(cost={"input": 5.0, "output": 25.0})}
    changes, _ = plan_refreshes(raw_models, upstream)
    assert changes == []


def test_refresh_touches_only_upstream_owned_fields():
    raw_models = {
        "x": {
            "model_id": "x",
            "provider": "openai",
            "context_window": 1,
            "thinking": False,
            "timeout": 120,
            "max_out_configured": 100,
        }
    }
    upstream = {"x": model(limit={"context": 200_000, "output": 64_000})}
    changes, _ = plan_refreshes(raw_models, upstream)
    assert {field for _, field, _, _ in changes} <= {
        "context_window",
        "max_out_stated",
        "cost_per_1k_input",
        "cost_per_1k_output",
    }


def test_lowered_ceiling_below_operator_cap_is_withheld():
    raw_models = {
        "x": {"model_id": "x", "provider": "openai", "max_out_configured": 60_000}
    }
    upstream = {"x": model(limit={"context": 200_000, "output": 8_000})}
    changes, conflicts = plan_refreshes(raw_models, upstream)
    assert not any(field == "max_out_stated" for _, field, _, _ in changes)
    assert conflicts and "left unchanged" in conflicts[0]


def test_conventions_prefer_the_operators_key_name_over_upstream():
    """models.dev calls Z.AI's key ZHIPU_API_KEY; the operator's config wins."""
    data = {"zai": {"id": "zai", "models": {"glm-9": model()}}}
    raw_models = {"glm-9": {"model_id": "glm-9", "api_key_env": "MY_ZAI_KEY", "stream": True}}
    assert learn_conventions(data, raw_models)["zai"]["api_key_env"] == "MY_ZAI_KEY"


def test_subscription_rows_do_not_teach_conventions():
    """A CLI-lane row has an empty api_key_env; it must not become the rule."""
    data = {"anthropic": {"id": "anthropic", "models": {"c": model()}}}
    raw_models = {"c-sub": {"model_id": "c", "provider": "claude-cli", "api_key_env": ""}}
    assert learn_conventions(data, raw_models)["anthropic"]["api_key_env"] == "ANTHROPIC_API_KEY"


# ─── Filtering rules ───────────────────────────────────────────────────────


def test_free_tier_models_are_refused():
    data = payload(free=model(cost={"input": 0, "output": 0}))
    result = gate(data, {}, {}, today=TODAY)
    reasons = {r.model_id: r.reason for r in result.rejections}
    assert "free tier" in reasons["free"]


def test_deprecated_models_are_refused():
    data = payload(old=model(status="deprecated"))
    result = gate(data, {}, {}, today=TODAY)
    assert any(r.model_id == "old" and "DEPRECATED" in r.reason for r in result.rejections)


def test_dated_alias_yields_to_its_floating_id():
    data = payload(**{"m": model(), "m-20260101": model()})
    result = gate(data, {}, {}, today=TODAY)
    assert any(
        r.model_id == "m-20260101" and "dated alias" in r.reason for r in result.rejections
    )


def test_latest_alias_collapses_onto_an_identical_twin():
    same = dict(cost={"input": 1.0, "output": 2.0}, limit={"context": 200_000, "output": 8_000})
    data = payload(**{"gem-latest": model(**same), "gem-3": model(**same)})
    result = gate(data, {}, {}, today=TODAY)
    assert any(
        r.model_id == "gem-latest" and "floating alias" in r.reason for r in result.rejections
    )
    assert "gem-3" in {c.model_id for c in result.candidates}


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026-07-25", 0),
        ("2026-07", 24),          # widens to the 1st
        ("2026", 205),            # widens to Jan 1
        (None, None),
        ("not-a-date", None),
    ],
)
def test_partial_and_broken_dates(raw, expected):
    assert age_days(raw, TODAY) == expected


def test_missing_release_date_is_not_grounds_for_rejection():
    """kimi-k2.5 ships with '2026-01'; an absent date must not evict a model."""
    data = payload(undated=model(release_date=None))
    result = gate(data, {}, {}, today=TODAY)
    assert "undated" in {c.model_id for c in result.candidates}


def test_health_reports_a_deprecated_roster_entry_with_its_role_status():
    data = payload(old=model(status="deprecated"))
    raw_models = {"old": {"model_id": "old", "provider": "openai"}}
    result = gate(data, raw_models, {"author": "old"}, today=TODAY)
    item = next(h for h in result.health if h.name == "old")
    assert item.role_assigned is True and "deprecated" in item.condition


def test_unfixable_shortfalls_are_not_actionable():
    data = payload(only=model())
    result = gate(data, {"only": {"model_id": "only"}}, {}, today=TODAY)
    assert all(s.fixable is False for s in result.shortfalls if s.provider == "anthropic")
    assert result.actionable_shortfalls == [
        s for s in result.shortfalls if s.fixable
    ]


# ─── Probe classifier ──────────────────────────────────────────────────────


def test_only_the_named_400_counts_as_a_rejection(monkeypatch):
    stream = (
        '{"type":"error","message":"Model metadata for `x` not found. '
        'Defaulting to fallback metadata."}\n'
        '{"type":"error","message":"{\\"type\\":\\"error\\",\\"status\\":400,'
        '\\"error\\":{\\"type\\":\\"invalid_request_error\\",\\"message\\":'
        '\\"The \'x\' model is not supported when using Codex with a ChatGPT account.\\"}}"}'
    )
    monkeypatch.setattr(probe_mod, "_run", lambda *a, **k: (1, stream))
    verdict, _ = probe_mod.probe_codex("x")
    assert verdict == probe_mod.VERDICT_REJECTED


def test_the_metadata_warning_alone_is_only_transient(monkeypatch):
    """The first error event is a red herring and must never prune a model."""
    stream = '{"type":"error","message":"Model metadata for `x` not found."}'
    monkeypatch.setattr(probe_mod, "_run", lambda *a, **k: (1, stream))
    verdict, _ = probe_mod.probe_codex("x")
    assert verdict == probe_mod.VERDICT_TRANSIENT


def test_a_timeout_is_transient_not_a_rejection(monkeypatch):
    monkeypatch.setattr(probe_mod, "_run", lambda *a, **k: (124, "probe timed out"))
    assert probe_mod.probe_codex("x")[0] == probe_mod.VERDICT_TRANSIENT


def test_transient_verdicts_are_never_cached(monkeypatch):
    monkeypatch.setattr(probe_mod, "_run", lambda *a, **k: (124, "probe timed out"))
    state = {}
    assert probe_mod.check("codex-cli", "x", state) == probe_mod.VERDICT_TRANSIENT
    assert state.get("availability", {}) == {}


def test_a_stale_verdict_is_re_probed(monkeypatch):
    old = (datetime.now(timezone.utc) - timedelta(days=probe_mod.RECHECK_DAYS + 1)).isoformat()
    state = {"availability": {"codex-cli/x": {"verdict": "rejected", "checked": old}}}
    monkeypatch.setattr(probe_mod, "_run", lambda *a, **k: (0, "ok"))
    assert probe_mod.check("codex-cli", "x", state) == probe_mod.VERDICT_SERVES


# ─── Reasoner guardrails ───────────────────────────────────────────────────


def test_hallucinated_ids_are_discarded():
    parsed = {"admit": [{"model_id": "invented", "tier": "top"}]}
    assert reason_mod.validate(parsed, {"real"}) == []


def test_unknown_tier_falls_back_to_mid():
    parsed = {"admit": [{"model_id": "real", "tier": "platinum"}]}
    assert reason_mod.validate(parsed, {"real"})[0]["tier"] == "mid"


def test_duplicate_admissions_collapse():
    parsed = {"admit": [{"model_id": "real", "tier": "top"}, {"model_id": "real", "tier": "mid"}]}
    assert len(reason_mod.validate(parsed, {"real"})) == 1


# ─── Fetch guards ──────────────────────────────────────────────────────────


def test_truncated_payload_is_refused():
    with pytest.raises(FetchError, match="floor"):
        sanity_check({"anthropic": {"models": {"a": {}}}}, raw_bytes=10)


def test_payload_without_bedrock_providers_is_refused():
    big = {f"p{i}": {"models": {"m": {}}} for i in range(60)}
    with pytest.raises(FetchError, match="missing models"):
        sanity_check(big, raw_bytes=1_000_000)


def test_digest_is_order_independent():
    assert digest({"a": 1, "b": 2}) == digest({"b": 2, "a": 1})


# ─── Notices ───────────────────────────────────────────────────────────────


def test_an_unchanged_finding_does_not_revive_a_dismissed_notice():
    notice = state_mod.Notice("id", "info", "t", "body", dismissed=True)
    merged = state_mod.upsert([notice], state_mod.Notice("id", "info", "t", "body"))
    assert merged[0].dismissed is True


def test_a_changed_finding_revives_the_notice():
    notice = state_mod.Notice("id", "info", "t", "old", dismissed=True)
    merged = state_mod.upsert([notice], state_mod.Notice("id", "info", "t", "new"))
    assert merged[0].dismissed is False and merged[0].body == "new"


def test_resolved_findings_are_pruned():
    notices = [state_mod.Notice("gone", "info", "t", "b"), state_mod.Notice("live", "info", "t", "b")]
    assert [n.id for n in state_mod.prune(notices, {"live"})] == ["live"]


def test_damaged_state_reads_as_absent(tmp_path):
    bad = tmp_path / "state.json"
    bad.write_text("{not json")
    assert state_mod._read_json(bad, {"version": 1}) == {"version": 1}


class _FakeStorage:
    """Stands in for StorageManager without touching ~/.dvad."""

    def acquire_lock(self) -> bool:
        return True

    def release_lock(self) -> None:
        return None
