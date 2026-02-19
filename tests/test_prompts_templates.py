"""Tests for prompt template synchronization after revision refactor.

Verifies that modified templates resolve correctly via prompts.py builders
and contain no stale references to {output_instructions} or PART 2 language.
"""

from __future__ import annotations

import pytest

from devils_advocate.prompts import (
    build_author_final_prompt,
    build_round1_author_prompt,
)


# ---------------------------------------------------------------------------
# Round 1 author templates
# ---------------------------------------------------------------------------


class TestRound1AuthorPrompts:

    def test_plan_mode_resolves_without_error(self):
        result = build_round1_author_prompt(
            mode="plan",
            original_content="# Plan\nStep 1: do X",
            grouped_feedback="GROUP 1: concern about X",
        )
        assert "GROUPED REVIEWER FEEDBACK" in result
        assert "ORIGINAL PLAN CONTENT" in result

    def test_code_mode_resolves_without_error(self):
        result = build_round1_author_prompt(
            mode="code",
            original_content="def foo(): pass",
            grouped_feedback="GROUP 1: missing error handling",
        )
        assert "GROUPED REVIEWER FEEDBACK" in result
        assert "ORIGINAL CODE CONTENT" in result

    def test_integration_mode_resolves_without_error(self):
        result = build_round1_author_prompt(
            mode="integration",
            original_content="--- file1.py ---\ndef bar(): pass",
            grouped_feedback="GROUP 1: API contract mismatch",
        )
        assert "GROUPED REVIEWER FEEDBACK" in result
        assert "ORIGINAL CODE CONTENT" in result

    def test_no_output_instructions_in_plan(self):
        result = build_round1_author_prompt("plan", "content", "feedback")
        assert "{output_instructions}" not in result
        assert "output_instructions" not in result
        assert "PART 2" not in result

    def test_no_output_instructions_in_code(self):
        result = build_round1_author_prompt("code", "content", "feedback")
        assert "{output_instructions}" not in result
        assert "output_instructions" not in result

    def test_no_output_instructions_in_integration(self):
        result = build_round1_author_prompt("integration", "content", "feedback")
        assert "{output_instructions}" not in result
        assert "output_instructions" not in result


# ---------------------------------------------------------------------------
# Round 2 author final templates
# ---------------------------------------------------------------------------


class TestAuthorFinalPrompts:

    def test_plan_mode_resolves_without_error(self):
        result = build_author_final_prompt(
            mode="plan",
            original_content="# Plan\nStep 1: do X",
            challenged_groups_text="GROUP [grp_001]: concern",
        )
        assert "CHALLENGED GROUPS" in result
        assert "ORIGINAL PLAN CONTENT" in result

    def test_code_mode_resolves_without_error(self):
        result = build_author_final_prompt(
            mode="code",
            original_content="def foo(): pass",
            challenged_groups_text="GROUP [grp_001]: concern",
        )
        assert "CHALLENGED GROUPS" in result
        assert "ORIGINAL CODE CONTENT" in result

    def test_no_output_instructions_in_plan_final(self):
        result = build_author_final_prompt("plan", "content", "challenges")
        assert "{output_instructions}" not in result
        assert "output_instructions" not in result
        assert "PART 2" not in result

    def test_no_output_instructions_in_code_final(self):
        result = build_author_final_prompt("code", "content", "challenges")
        assert "{output_instructions}" not in result
        assert "output_instructions" not in result
        assert "PART 2" not in result
