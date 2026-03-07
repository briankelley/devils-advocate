"""Tests for API .env file helpers and filesystem browser endpoint.

Covers _read_env_file, _write_env_file, _get_env_file_path,
_get_allowed_env_names, and the /fs/ls endpoint.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ═══════════════════════════════════════════════════════════════════════════
# 1. _read_env_file
# ═══════════════════════════════════════════════════════════════════════════


class TestReadEnvFile:
    def test_nonexistent_file(self, tmp_path):
        from devils_advocate.gui.api import _read_env_file

        path = tmp_path / ".env"
        lines, kv = _read_env_file(path)
        assert lines == []
        assert kv == {}

    def test_empty_file(self, tmp_path):
        from devils_advocate.gui.api import _read_env_file

        path = tmp_path / ".env"
        path.write_text("")
        lines, kv = _read_env_file(path)
        assert lines == [""]
        assert kv == {}

    def test_simple_key_value(self, tmp_path):
        from devils_advocate.gui.api import _read_env_file

        path = tmp_path / ".env"
        path.write_text("FOO=bar\nBAZ=qux\n")
        lines, kv = _read_env_file(path)

        assert kv["FOO"] == "bar"
        assert kv["BAZ"] == "qux"
        assert len(lines) == 3  # FOO=bar, BAZ=qux, trailing empty

    def test_comments_preserved(self, tmp_path):
        from devils_advocate.gui.api import _read_env_file

        path = tmp_path / ".env"
        path.write_text("# Comment line\nKEY=value\n")
        lines, kv = _read_env_file(path)

        assert "# Comment line" in lines
        assert kv["KEY"] == "value"

    def test_blank_lines_preserved(self, tmp_path):
        from devils_advocate.gui.api import _read_env_file

        path = tmp_path / ".env"
        path.write_text("KEY1=val1\n\nKEY2=val2\n")
        lines, kv = _read_env_file(path)

        assert len(kv) == 2
        assert "" in lines  # blank line preserved

    def test_value_with_equals(self, tmp_path):
        from devils_advocate.gui.api import _read_env_file

        path = tmp_path / ".env"
        path.write_text("KEY=val=with=equals\n")
        lines, kv = _read_env_file(path)

        assert kv["KEY"] == "val=with=equals"

    def test_no_value(self, tmp_path):
        from devils_advocate.gui.api import _read_env_file

        path = tmp_path / ".env"
        path.write_text("KEY=\n")
        lines, kv = _read_env_file(path)

        assert kv["KEY"] == ""


# ═══════════════════════════════════════════════════════════════════════════
# 2. _write_env_file
# ═══════════════════════════════════════════════════════════════════════════


class TestWriteEnvFile:
    def test_write_new_file(self, tmp_path):
        from devils_advocate.gui.api import _write_env_file

        path = tmp_path / ".env"
        _write_env_file(path, [], updates={"FOO": "bar"})

        content = path.read_text()
        assert "FOO=bar" in content

    def test_update_existing_key(self, tmp_path):
        from devils_advocate.gui.api import _write_env_file

        path = tmp_path / ".env"
        existing = ["FOO=old_value", "BAR=keep"]
        _write_env_file(path, existing, updates={"FOO": "new_value"})

        content = path.read_text()
        assert "FOO=new_value" in content
        assert "BAR=keep" in content
        assert "old_value" not in content

    def test_remove_key(self, tmp_path):
        from devils_advocate.gui.api import _write_env_file

        path = tmp_path / ".env"
        existing = ["FOO=bar", "BAR=baz"]
        _write_env_file(path, existing, remove_keys={"FOO"})

        content = path.read_text()
        assert "FOO" not in content
        assert "BAR=baz" in content

    def test_preserves_comments(self, tmp_path):
        from devils_advocate.gui.api import _write_env_file

        path = tmp_path / ".env"
        existing = ["# This is a comment", "KEY=val"]
        _write_env_file(path, existing, updates={"KEY": "new_val"})

        content = path.read_text()
        assert "# This is a comment" in content
        assert "KEY=new_val" in content

    def test_new_keys_appended(self, tmp_path):
        from devils_advocate.gui.api import _write_env_file

        path = tmp_path / ".env"
        existing = ["EXISTING=val"]
        _write_env_file(path, existing, updates={"NEW_KEY": "new_val"})

        content = path.read_text()
        assert "EXISTING=val" in content
        assert "NEW_KEY=new_val" in content

    def test_trailing_newline(self, tmp_path):
        from devils_advocate.gui.api import _write_env_file

        path = tmp_path / ".env"
        _write_env_file(path, [], updates={"KEY": "val"})

        content = path.read_text()
        assert content.endswith("\n")

    def test_file_permissions_0600(self, tmp_path):
        from devils_advocate.gui.api import _write_env_file

        path = tmp_path / ".env"
        _write_env_file(path, [], updates={"SECRET": "sk-123"})

        mode = oct(path.stat().st_mode & 0o777)
        assert mode == "0o600"

    def test_update_and_remove_simultaneously(self, tmp_path):
        from devils_advocate.gui.api import _write_env_file

        path = tmp_path / ".env"
        existing = ["KEEP=yes", "REMOVE=gone", "UPDATE=old"]
        _write_env_file(
            path, existing,
            updates={"UPDATE": "new"},
            remove_keys={"REMOVE"},
        )

        content = path.read_text()
        assert "KEEP=yes" in content
        assert "UPDATE=new" in content
        assert "REMOVE" not in content


# ═══════════════════════════════════════════════════════════════════════════
# 3. _get_allowed_env_names
# ═══════════════════════════════════════════════════════════════════════════


class TestGetAllowedEnvNames:
    def test_extracts_unique_env_names(self):
        from devils_advocate.gui.api import _get_allowed_env_names
        from helpers import make_model_config

        m1 = make_model_config(name="model-a", api_key_env="KEY_A")
        m2 = make_model_config(name="model-b", api_key_env="KEY_B")
        m3 = make_model_config(name="model-c", api_key_env="KEY_A")  # duplicate

        config = {"all_models": {"a": m1, "b": m2, "c": m3}}
        result = _get_allowed_env_names(config)

        assert result == {"KEY_A", "KEY_B"}

    def test_empty_models(self):
        from devils_advocate.gui.api import _get_allowed_env_names

        config = {"all_models": {}}
        assert _get_allowed_env_names(config) == set()

    def test_falls_back_to_models_key(self):
        from devils_advocate.gui.api import _get_allowed_env_names
        from helpers import make_model_config

        m = make_model_config(name="model", api_key_env="FALLBACK_KEY")
        config = {"models": {"m": m}}
        result = _get_allowed_env_names(config)

        assert result == {"FALLBACK_KEY"}


# ═══════════════════════════════════════════════════════════════════════════
# 4. CSRF Validation
# ═══════════════════════════════════════════════════════════════════════════


class TestCSRFValidation:
    def test_missing_token_raises_403(self):
        from fastapi import HTTPException
        from devils_advocate.gui.api import _check_csrf

        request = MagicMock()
        request.app.state.csrf_token = "valid-token-123"
        request.headers.get.return_value = ""

        with pytest.raises(HTTPException) as exc_info:
            _check_csrf(request)
        assert exc_info.value.status_code == 403

    def test_wrong_token_raises_403(self):
        from fastapi import HTTPException
        from devils_advocate.gui.api import _check_csrf

        request = MagicMock()
        request.app.state.csrf_token = "valid-token-123"
        request.headers.get.return_value = "wrong-token"

        with pytest.raises(HTTPException) as exc_info:
            _check_csrf(request)
        assert exc_info.value.status_code == 403

    def test_correct_token_passes(self):
        from devils_advocate.gui.api import _check_csrf

        request = MagicMock()
        request.app.state.csrf_token = "valid-token-123"
        request.headers.get.return_value = "valid-token-123"

        _check_csrf(request)  # should not raise


# ═══════════════════════════════════════════════════════════════════════════
# 5. Review Data Formats
# ═══════════════════════════════════════════════════════════════════════════


class TestReviewDataFormats:
    """Test helper functions related to review data serialization."""

    def test_review_point_dataclass(self):
        from helpers import make_review_point
        from dataclasses import asdict

        p = make_review_point(
            point_id="p1",
            reviewer="gemini-flash",
            severity="high",
            category="security",
            description="SQL injection risk",
            recommendation="Use parameterized queries",
            location="src/db.py:42",
        )

        d = asdict(p)
        assert d["point_id"] == "p1"
        assert d["severity"] == "high"
        assert d["location"] == "src/db.py:42"

    def test_review_group_dataclass(self):
        from helpers import make_review_group
        from dataclasses import asdict

        g = make_review_group(
            group_id="g1",
            concern="Database security",
            source_reviewers=["gemini-flash", "kimi-k2"],
        )

        d = asdict(g)
        assert d["group_id"] == "g1"
        assert len(d["source_reviewers"]) == 2

    def test_governance_decision_dataclass(self):
        from devils_advocate.types import GovernanceDecision
        from dataclasses import asdict

        d = GovernanceDecision(
            group_id="g1",
            author_resolution="accepted",
            governance_resolution="auto_accepted",
            reason="Author accepted with substantive rationale",
        )

        dd = asdict(d)
        assert dd["governance_resolution"] == "auto_accepted"


# ═══════════════════════════════════════════════════════════════════════════
# 6. Governance Edge Cases
# ═══════════════════════════════════════════════════════════════════════════


class TestGovernanceEdgeCases:
    def test_partial_acceptance_always_escalates(self):
        from devils_advocate.governance import apply_governance
        from helpers import make_review_group, make_author_response

        groups = [make_review_group(group_id="g1")]
        responses = [make_author_response(
            group_id="g1",
            resolution="PARTIAL",
            rationale="Partially applicable with enough words to describe the situation thoroughly and comprehensively",
        )]

        decisions = apply_governance(groups, responses)
        assert decisions[0].governance_resolution == "escalated"
        assert "Partial" in decisions[0].reason

    def test_unknown_resolution_escalates(self):
        from devils_advocate.governance import apply_governance
        from helpers import make_review_group, make_author_response

        groups = [make_review_group(group_id="g1")]
        responses = [make_author_response(
            group_id="g1",
            resolution="WONTFIX",
            rationale="Not fixing this",
        )]

        decisions = apply_governance(groups, responses)
        assert decisions[0].governance_resolution == "escalated"
        assert "Unrecognized" in decisions[0].reason

    def test_maintained_position_single_reviewer_escalates(self):
        """Single reviewer challenge + author MAINTAINED → escalated."""
        from devils_advocate.governance import apply_governance
        from helpers import make_review_group, make_author_response, make_rebuttal, make_author_final

        groups = [make_review_group(
            group_id="g1",
            source_reviewers=["reviewer-1"],
        )]
        responses = [make_author_response(
            group_id="g1", resolution="REJECTED",
            rationale="The function handles this case",
        )]
        rebuttals = [make_rebuttal(
            group_id="g1", reviewer="reviewer-1",
            verdict="CHALLENGE",
        )]
        finals = [make_author_final(
            group_id="g1", resolution="MAINTAINED",
            rationale="I stand by my original assessment",
        )]

        decisions = apply_governance(
            groups, responses,
            rebuttals=rebuttals,
            author_final_responses=finals,
        )
        assert decisions[0].governance_resolution == "escalated"

    def test_integration_mode_single_reviewer_rejection_escalates(self):
        """Integration mode: even single-reviewer rejections are escalated."""
        from devils_advocate.governance import apply_governance
        from helpers import make_review_group, make_author_response

        groups = [make_review_group(
            group_id="g1",
            source_reviewers=["integ-reviewer"],
        )]
        responses = [make_author_response(
            group_id="g1",
            resolution="REJECTED",
            rationale="No issues found",
        )]

        decisions = apply_governance(groups, responses, mode="integration")
        assert decisions[0].governance_resolution == "escalated"
        assert "Integration" in decisions[0].reason

    def test_empty_groups_produces_empty_decisions(self):
        from devils_advocate.governance import apply_governance

        decisions = apply_governance([], [])
        assert decisions == []

    def test_acceptance_challenged_no_final_escalates(self):
        """Author accepts, reviewer challenges, but no final response → escalate."""
        from devils_advocate.governance import apply_governance
        from helpers import make_review_group, make_author_response, make_rebuttal

        groups = [make_review_group(group_id="g1")]
        responses = [make_author_response(
            group_id="g1",
            resolution="ACCEPTED",
            rationale="This is a substantive rationale with enough words to pass the minimum word count validation check easily",
        )]
        rebuttals = [make_rebuttal(
            group_id="g1", reviewer="reviewer-1",
            verdict="CHALLENGE",
        )]

        decisions = apply_governance(
            groups, responses, rebuttals=rebuttals,
        )
        assert decisions[0].governance_resolution == "escalated"
        assert "challenged" in decisions[0].reason.lower()

    def test_rote_acceptance_escalates(self):
        """Rote acceptance phrases should escalate."""
        from devils_advocate.governance import apply_governance
        from helpers import make_review_group, make_author_response

        for rote in ["Accepted.", "LGTM.", "Will do.", "Good point.", "Sounds good."]:
            groups = [make_review_group(group_id="g1")]
            responses = [make_author_response(
                group_id="g1",
                resolution="ACCEPTED",
                rationale=rote,
            )]

            decisions = apply_governance(groups, responses)
            assert decisions[0].governance_resolution == "escalated", (
                f"Expected escalated for rote acceptance '{rote}', "
                f"got {decisions[0].governance_resolution}"
            )


# ═══════════════════════════════════════════════════════════════════════════
# 7. validate_acceptance
# ═══════════════════════════════════════════════════════════════════════════


class TestValidateAcceptance:
    def test_empty_rationale(self):
        from devils_advocate.governance import validate_acceptance
        assert validate_acceptance("") is False

    def test_whitespace_rationale(self):
        from devils_advocate.governance import validate_acceptance
        assert validate_acceptance("   ") is False

    def test_too_short_rationale(self):
        from devils_advocate.governance import validate_acceptance
        assert validate_acceptance("This is short") is False

    def test_rote_phrases(self):
        from devils_advocate.governance import validate_acceptance

        rote_phrases = [
            "Accepted.",
            "Agree.",
            "Agreed.",
            "Acknowledged.",
            "Will do.",
            "Will implement.",
            "Will fix.",
            "Makes sense.",
            "Good point.",
            "Good catch.",
            "Good finding.",
            "Sounds good.",
            "Sounds right.",
            "No objection.",
            "No objections.",
            "LGTM.",
            "Fair point.",
            "Fair enough.",
            "The reviewer is correct.",
            "This is correct.",
            "I accept this.",
            "Noted.",
            "Understood.",
        ]
        for phrase in rote_phrases:
            assert validate_acceptance(phrase) is False, f"Expected False for '{phrase}'"

    def test_substantive_acceptance(self):
        from devils_advocate.governance import validate_acceptance

        assert validate_acceptance(
            "The reviewer correctly identifies that the API endpoint needs "
            "rate limiting to prevent abuse from unauthenticated clients"
        ) is True

    def test_exactly_15_words(self):
        from devils_advocate.governance import validate_acceptance

        # Exactly 15 words
        text = "one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen"
        assert validate_acceptance(text) is True

    def test_14_words_fails(self):
        from devils_advocate.governance import validate_acceptance

        text = "one two three four five six seven eight nine ten eleven twelve thirteen fourteen"
        assert validate_acceptance(text) is False
