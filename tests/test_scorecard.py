"""Tests for the model scorecard — analytics module, GUI routes, CLI command."""

import json

import pytest

from devils_advocate.scorecard import (
    compute_scorecard,
    load_scorable_ledgers,
    matchup_sentence,
    model_family,
    versus_sentence,
)


# ─── fixtures ─────────────────────────────────────────────────────────────────


def _point(
    reviewer: str,
    group: str,
    final: str = "auto_accepted",
    author_resolution: str = "ACCEPTED",
    severity: str = "medium",
    sources: list | None = None,
    rebuttals: list | None = None,
    author_final: str | None = None,
):
    return {
        "point_id": f"{group}.point_001",
        "group_id": group,
        "severity": severity,
        "category": "architecture",
        "description": f"Finding in {group} by {reviewer}",
        "recommendation": "Fix it",
        "location": "file.py:1",
        "reviewer": reviewer,
        "source_reviewers": sources if sources is not None else [reviewer],
        "author_resolution": author_resolution,
        "author_rationale": "",
        "rebuttals": rebuttals or [],
        "author_final_resolution": author_final,
        "author_final_rationale": None,
        "governance_resolution": final,
        "governance_reason": "",
        "final_resolution": final,
        "overrides": [],
    }


def _write_ledger(reviews_dir, review_id, **overrides):
    ledger = {
        "review_id": review_id,
        "result": "success",
        "mode": "plan",
        "input_file": "plan.md",
        "project": "real-project",
        "timestamp": "2026-03-15T12:00:00+00:00",
        "author_model": "author-model",
        "reviewer_models": ["rev-a", "rev-b"],
        "dedup_model": "dedup-model",
        "points": [],
        "summary": {"total_points": 0, "total_groups": 0},
        "cost": {
            "total_usd": 1.0,
            "breakdown": {},
            "role_costs": {"reviewer_1": 0.4, "reviewer_2": 0.5, "author": 0.1},
        },
    }
    ledger.update(overrides)
    review_dir = reviews_dir / review_id
    review_dir.mkdir(parents=True)
    (review_dir / "review-ledger.json").write_text(json.dumps(ledger))
    return ledger


@pytest.fixture
def reviews_dir(tmp_path):
    d = tmp_path / "reviews"
    d.mkdir()
    return d


# ─── hygiene filters ──────────────────────────────────────────────────────────


class TestLedgerFilters:
    def test_stub_reviewers_excluded(self, reviews_dir):
        _write_ledger(
            reviews_dir, "r1",
            reviewer_models=["reviewer1", "reviewer2"],
            points=[_point("reviewer1", "g1")],
        )
        assert load_scorable_ledgers(reviews_dir) == []

    def test_e2e_project_excluded_by_default(self, reviews_dir):
        _write_ledger(
            reviews_dir, "r1",
            project="e2e-live",
            points=[_point("rev-a", "g1")],
        )
        assert load_scorable_ledgers(reviews_dir) == []
        assert len(load_scorable_ledgers(reviews_dir, include_test=True)) == 1

    def test_spec_mode_excluded(self, reviews_dir):
        _write_ledger(
            reviews_dir, "r1",
            mode="spec",
            points=[_point("rev-a", "g1")],
        )
        assert load_scorable_ledgers(reviews_dir) == []

    def test_pending_only_review_excluded(self, reviews_dir):
        _write_ledger(
            reviews_dir, "r1",
            points=[_point("rev-a", "g1", final="pending")],
        )
        assert load_scorable_ledgers(reviews_dir) == []

    def test_real_review_kept_even_with_test_in_name(self, reviews_dir):
        # Early real reviews ran under test-ish project names; reviewer
        # stubs and e2e are the synthetic signals, not the word "test".
        _write_ledger(
            reviews_dir, "r1",
            project="comfy-launcher-test",
            points=[_point("rev-a", "g1")],
        )
        assert len(load_scorable_ledgers(reviews_dir)) == 1

    def test_corrupt_ledger_skipped(self, reviews_dir):
        bad = reviews_dir / "bad"
        bad.mkdir()
        (bad / "review-ledger.json").write_text("{not json")
        _write_ledger(reviews_dir, "r1", points=[_point("rev-a", "g1")])
        assert len(load_scorable_ledgers(reviews_dir)) == 1


# ─── reviewer metrics ─────────────────────────────────────────────────────────


class TestReviewerMetrics:
    def test_basic_counts_and_hit_rate(self, reviews_dir):
        _write_ledger(reviews_dir, "r1", points=[
            _point("rev-a", "g1", final="auto_accepted"),
            _point("rev-a", "g2", final="auto_dismissed"),
            _point("rev-a", "g3", final="escalated"),
            _point("rev-a", "g4", final="overridden"),
            _point("rev-b", "g5", final="auto_accepted", severity="critical"),
        ])
        sc = compute_scorecard(reviews_dir)
        a = next(r for r in sc["reviewers"] if r["model"] == "rev-a")
        b = next(r for r in sc["reviewers"] if r["model"] == "rev-b")
        assert a["findings"] == 4
        assert a["accepted"] == 2  # auto_accepted + overridden
        assert a["dismissed"] == 1
        assert a["escalated"] == 1
        assert a["hit_rate"] == 0.5
        assert b["critical"] == 1
        assert b["hit_rate"] == 1.0

    def test_reviewer_cost_attribution_by_seat(self, reviews_dir):
        _write_ledger(reviews_dir, "r1", points=[
            _point("rev-a", "g1"), _point("rev-b", "g2"),
        ])
        sc = compute_scorecard(reviews_dir)
        a = next(r for r in sc["reviewers"] if r["model"] == "rev-a")
        b = next(r for r in sc["reviewers"] if r["model"] == "rev-b")
        assert a["cost_usd"] == 0.4  # reviewer_1 seat
        assert b["cost_usd"] == 0.5  # reviewer_2 seat
        assert a["cost_per_accepted"] == 0.4

    def test_consensus_rate(self, reviews_dir):
        _write_ledger(reviews_dir, "r1", points=[
            _point("rev-a", "g1", sources=["rev-a", "rev-b"]),
            _point("rev-a", "g2", sources=["rev-a"]),
        ])
        sc = compute_scorecard(reviews_dir)
        a = next(r for r in sc["reviewers"] if r["model"] == "rev-a")
        assert a["consensus"] == 1
        assert a["consensus_rate"] == 0.5

    def test_monthly_buckets(self, reviews_dir):
        _write_ledger(reviews_dir, "r1", timestamp="2026-02-01T00:00:00+00:00",
                      points=[_point("rev-a", "g1")])
        _write_ledger(reviews_dir, "r2", timestamp="2026-03-01T00:00:00+00:00",
                      points=[_point("rev-a", "g1"), _point("rev-a", "g2", final="auto_dismissed")])
        sc = compute_scorecard(reviews_dir)
        a = next(r for r in sc["reviewers"] if r["model"] == "rev-a")
        assert a["monthly"]["2026-02"] == {"findings": 1, "accepted": 1}
        assert a["monthly"]["2026-03"] == {"findings": 2, "accepted": 1}
        assert sc["months"] == ["2026-02", "2026-03"]


# ─── conviction ───────────────────────────────────────────────────────────────


class TestConviction:
    def test_stood_ground_and_vindicated(self, reviews_dir):
        rebuttals = [{"group_id": "g1", "reviewer": "rev-a",
                      "verdict": "CHALLENGE", "rationale": "no"}]
        _write_ledger(reviews_dir, "r1", points=[
            _point("rev-a", "g1", final="auto_accepted",
                   author_resolution="REJECTED", rebuttals=rebuttals),
        ])
        sc = compute_scorecard(reviews_dir)
        a = next(r for r in sc["reviewers"] if r["model"] == "rev-a")
        assert a["contested"] == 1
        assert a["stood_ground"] == 1
        assert a["conceded"] == 0
        assert a["conviction_rate"] == 1.0
        assert a["vindicated"] == 1
        assert len(sc["split_decisions"]) == 1
        d = sc["split_decisions"][0]
        assert d["reviewer"] == "rev-a"
        assert d["author"] == "author-model"
        assert d["final_resolution"] == "auto_accepted"

    def test_conceded(self, reviews_dir):
        rebuttals = [{"group_id": "g1", "reviewer": "rev-a",
                      "verdict": "CONCUR", "rationale": "fair"}]
        _write_ledger(reviews_dir, "r1", points=[
            _point("rev-a", "g1", final="auto_dismissed",
                   author_resolution="PARTIAL", rebuttals=rebuttals),
        ])
        sc = compute_scorecard(reviews_dir)
        a = next(r for r in sc["reviewers"] if r["model"] == "rev-a")
        assert a["contested"] == 1
        assert a["conceded"] == 1
        assert a["conviction_rate"] == 0.0
        assert sc["split_decisions"] == []

    def test_peer_rebuttal_not_counted_as_own_stance(self, reviews_dir):
        # rev-b challenging on rev-a's group is not rev-a standing ground.
        rebuttals = [{"group_id": "g1", "reviewer": "rev-b",
                      "verdict": "CHALLENGE", "rationale": "hm"}]
        _write_ledger(reviews_dir, "r1", points=[
            _point("rev-a", "g1", final="escalated",
                   author_resolution="REJECTED", rebuttals=rebuttals),
        ])
        sc = compute_scorecard(reviews_dir)
        a = next(r for r in sc["reviewers"] if r["model"] == "rev-a")
        assert a["contested"] == 1
        assert a["stood_ground"] == 0
        assert a["conceded"] == 0
        assert a["conviction_rate"] is None

    def test_stance_counted_once_per_group(self, reviews_dir):
        rebuttals = [{"group_id": "g1", "reviewer": "rev-a",
                      "verdict": "CHALLENGE", "rationale": "no"}]
        _write_ledger(reviews_dir, "r1", points=[
            _point("rev-a", "g1", author_resolution="REJECTED", rebuttals=rebuttals),
            _point("rev-a", "g1", author_resolution="REJECTED", rebuttals=rebuttals),
        ])
        sc = compute_scorecard(reviews_dir)
        a = next(r for r in sc["reviewers"] if r["model"] == "rev-a")
        assert a["contested"] == 1
        assert a["stood_ground"] == 1


# ─── authors ──────────────────────────────────────────────────────────────────


class TestAuthorMetrics:
    def test_accept_rate_and_hold_rate(self, reviews_dir):
        _write_ledger(reviews_dir, "r1", points=[
            _point("rev-a", "g1", author_resolution="ACCEPTED"),
            _point("rev-a", "g2", author_resolution="REJECTED", author_final="ACCEPTED"),
            _point("rev-a", "g3", author_resolution="REJECTED", author_final="MAINTAINED"),
            _point("rev-a", "g4", author_resolution="PARTIAL"),
        ])
        sc = compute_scorecard(reviews_dir)
        a = sc["authors"][0]
        assert a["model"] == "author-model"
        assert a["responded"] == 4
        assert a["accepted"] == 1
        assert a["rejected"] == 2
        assert a["partial"] == 1
        assert a["accept_rate"] == 0.25
        assert a["contests"] == 3
        assert a["folded"] == 1
        assert a["held"] == 1
        assert a["hold_rate"] == 0.5
        assert a["cost_usd"] == 0.1

    def test_author_response_counted_once_per_group(self, reviews_dir):
        _write_ledger(reviews_dir, "r1", points=[
            _point("rev-a", "g1", author_resolution="ACCEPTED"),
            _point("rev-b", "g1", author_resolution="ACCEPTED"),
        ])
        sc = compute_scorecard(reviews_dir)
        assert sc["authors"][0]["responded"] == 1


# ─── head-to-head ─────────────────────────────────────────────────────────────


class TestHeadToHead:
    def test_pair_stats(self, reviews_dir):
        _write_ledger(reviews_dir, "r1", points=[
            _point("rev-a", "g1", sources=["rev-a", "rev-b"]),
            _point("rev-b", "g1", sources=["rev-a", "rev-b"]),
            _point("rev-a", "g2", sources=["rev-a"]),
            _point("rev-b", "g3", sources=["rev-b"], final="auto_dismissed"),
        ])
        sc = compute_scorecard(reviews_dir)
        assert len(sc["head_to_head"]) == 1
        pair = sc["head_to_head"][0]
        assert {pair["model_a"], pair["model_b"]} == {"rev-a", "rev-b"}
        assert pair["reviews"] == 1
        assert pair["both_found"] == 1
        assert pair["a_solo"] == 1
        assert pair["b_solo"] == 1
        a_key = "a" if pair["model_a"] == "rev-a" else "b"
        assert pair[f"{a_key}_findings"] == 2
        assert pair[f"{a_key}_hit_rate"] == 1.0
        sentence = matchup_sentence(pair)
        assert "reviewed the same artifact once" in sentence

    def test_reviewer_vs_author(self, reviews_dir):
        rebuttals = [{"group_id": "g2", "reviewer": "rev-a",
                      "verdict": "CHALLENGE", "rationale": "no"}]
        _write_ledger(reviews_dir, "r1", points=[
            _point("rev-a", "g1"),
            _point("rev-a", "g2", final="escalated",
                   author_resolution="REJECTED", rebuttals=rebuttals),
        ])
        sc = compute_scorecard(reviews_dir)
        row = next(v for v in sc["reviewer_vs_author"] if v["reviewer"] == "rev-a")
        assert row["author"] == "author-model"
        assert row["reviews"] == 1
        assert row["findings"] == 2
        assert row["accepted"] == 1
        assert row["author_pushback"] == 1
        assert row["stood_ground"] == 1
        assert row["escalated"] == 1
        sentence = versus_sentence(row)
        assert "refused to withdraw 1" in sentence
        assert "author-model" in sentence


# ─── service roles / family ───────────────────────────────────────────────────


class TestServiceAndFamily:
    def test_dedup_service_role(self, reviews_dir):
        _write_ledger(
            reviews_dir, "r1",
            points=[_point("rev-a", "g1")],
            cost={"total_usd": 1.0, "breakdown": {},
                  "role_costs": {"dedup": 0.02}},
        )
        sc = compute_scorecard(reviews_dir)
        dedup = next(s for s in sc["service_roles"] if s["role"] == "dedup")
        assert dedup["model"] == "dedup-model"
        assert dedup["reviews"] == 1
        assert dedup["cost_usd"] == 0.02

    def test_role_assignments_service_roles(self, reviews_dir):
        _write_ledger(
            reviews_dir, "r1",
            points=[_point("rev-a", "g1")],
            role_assignments={"author": "author-model", "reviewers": ["rev-a", "rev-b"],
                              "dedup": "dedup-model", "normalization": "norm-model",
                              "revision": "rev-model"},
        )
        sc = compute_scorecard(reviews_dir)
        roles = {(s["role"], s["model"]) for s in sc["service_roles"]}
        assert ("normalization", "norm-model") in roles
        assert ("revision", "rev-model") in roles

    def test_model_family(self):
        assert model_family("claude-opus-4-6") == "claude-opus"
        assert model_family("claude-opus-4-8") == "claude-opus"
        assert model_family("gpt-5.2") == "gpt"
        assert model_family("gemini-3.1-pro-preview") == "gemini-pro"
        assert model_family("gpt-5.3-codex") == "gpt-codex"


# ─── GUI routes ───────────────────────────────────────────────────────────────


class TestScorecardRoutes:
    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DVAD_HOME", str(tmp_path / "dvad-data"))
        reviews = tmp_path / "dvad-data" / "reviews"
        reviews.mkdir(parents=True)
        _write_ledger(reviews, "r1", points=[
            _point("rev-a", "g1"), _point("rev-b", "g2"),
        ])
        from fastapi.testclient import TestClient
        from devils_advocate.gui import create_app
        return TestClient(create_app())

    def test_page_renders(self, client):
        resp = client.get("/scorecard")
        assert resp.status_code == 200
        assert "Model Scorecard" in resp.text
        assert "rev-a" in resp.text
        assert "Split decisions" in resp.text
        assert "swords" in resp.text

    def test_api_json(self, client):
        resp = client.get("/api/scorecard")
        assert resp.status_code == 200
        data = resp.json()
        assert data["review_count"] == 1
        assert {r["model"] for r in data["reviewers"]} == {"rev-a", "rev-b"}

    def test_nav_link_present(self, client):
        resp = client.get("/scorecard")
        assert 'href="/scorecard"' in resp.text


# ─── CLI ──────────────────────────────────────────────────────────────────────


class TestStatsCli:
    def test_stats_table(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DVAD_HOME", str(tmp_path / "dvad-data"))
        reviews = tmp_path / "dvad-data" / "reviews"
        reviews.mkdir(parents=True)
        _write_ledger(reviews, "r1", points=[_point("rev-a", "g1")])

        from click.testing import CliRunner
        from devils_advocate.cli import cli
        result = CliRunner().invoke(cli, ["stats"])
        assert result.exit_code == 0
        assert "rev-a" in result.output
        assert "Reviewer Scorecard" in result.output

    def test_stats_json(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DVAD_HOME", str(tmp_path / "dvad-data"))
        reviews = tmp_path / "dvad-data" / "reviews"
        reviews.mkdir(parents=True)
        _write_ledger(reviews, "r1", points=[_point("rev-a", "g1")])

        from click.testing import CliRunner
        from devils_advocate.cli import cli
        result = CliRunner().invoke(cli, ["stats", "--json"])
        assert result.exit_code == 0
        assert '"review_count"' in result.output

    def test_stats_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DVAD_HOME", str(tmp_path / "dvad-data"))
        from click.testing import CliRunner
        from devils_advocate.cli import cli
        result = CliRunner().invoke(cli, ["stats"])
        assert result.exit_code == 0
        assert "No scorable reviews" in result.output


# ─── cost entry persistence ───────────────────────────────────────────────────


class TestCostEntryPersistence:
    def test_entry_includes_role(self):
        from devils_advocate.types import CostTracker
        ct = CostTracker()
        ct.add("model-x", 100, 50, 0.001, 0.002, role="reviewer_1")
        assert ct.entries[0]["role"] == "reviewer_1"
        assert ct.entries[0]["model"] == "model-x"

    def test_generate_ledger_persists_entries(self):
        from devils_advocate.types import CostTracker, ReviewResult
        from devils_advocate.output import generate_ledger
        ct = CostTracker()
        ct.add("model-x", 100, 50, 0.001, 0.002, role="reviewer_1")
        result = ReviewResult(
            review_id="r1", mode="plan", input_file="plan.md",
            project="p", timestamp="2026-03-15T12:00:00+00:00",
            author_model="a", reviewer_models=["model-x"], dedup_model="d",
            points=[], groups=[], author_responses=[],
            governance_decisions=[], cost=ct,
        )
        ledger = generate_ledger(result)
        assert ledger["cost"]["entries"] == ct.entries
        assert ledger["cost"]["entries"][0]["role"] == "reviewer_1"
