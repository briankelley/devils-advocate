"""Tests for prompt template synchronization after revision refactor.

Verifies that modified templates resolve correctly via prompts.py builders
and contain no stale references to {output_instructions} or PART 2 language.
"""

from __future__ import annotations

import pytest

from devils_advocate.prompts import (
    build_author_final_prompt,
    build_dedup_prompt,
    build_integration_prompt,
    build_normalization_prompt,
    build_review_prompt,
    build_reviewer_rebuttal_prompt,
    build_round1_author_prompt,
    build_spec_dedup_prompt,
    build_spec_review_prompt,
    build_spec_revision_prompt,
    get_reviewer_system_prompt,
    get_spec_reviewer_system_prompt,
    load_template,
)
from devils_advocate.types import AdvocateError


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


# ---------------------------------------------------------------------------
# load_template tests
# ---------------------------------------------------------------------------


class TestLoadTemplate:

    def test_missing_template_raises_error(self):
        """Loading a nonexistent template file raises AdvocateError."""
        with pytest.raises(AdvocateError, match="Template not found"):
            load_template("nonexistent-template-that-does-not-exist.txt")

    def test_missing_variable_raises_error(self):
        """Template with an undefined format variable raises AdvocateError."""
        with pytest.raises(AdvocateError, match="missing variable"):
            load_template("dedup-instruct.txt", wrong_variable_name="oops")

    def test_successful_load_with_variable(self):
        """A known template loads and substitutes variables correctly."""
        result = load_template("dedup-instruct.txt", formatted_points="POINT 1: test finding")
        assert "POINT 1: test finding" in result

    def test_successful_load_without_variables(self):
        """Templates with no format placeholders load as-is."""
        result = load_template("reviewer-system.txt")
        assert isinstance(result, str)
        assert len(result) > 0


# ---------------------------------------------------------------------------
# get_reviewer_system_prompt tests
# ---------------------------------------------------------------------------


class TestGetReviewerSystemPrompt:

    def test_returns_non_empty_string(self):
        """get_reviewer_system_prompt returns a non-empty string."""
        result = get_reviewer_system_prompt()
        assert isinstance(result, str)
        assert len(result) > 0

    def test_cached_returns_same_object(self):
        """Subsequent calls return the cached value (same object)."""
        result1 = get_reviewer_system_prompt()
        result2 = get_reviewer_system_prompt()
        assert result1 is result2


# ---------------------------------------------------------------------------
# build_review_prompt tests
# ---------------------------------------------------------------------------


class TestBuildReviewPrompt:

    def test_without_spec(self):
        """build_review_prompt without spec content produces no SPECIFICATION block."""
        result = build_review_prompt(mode="code", content="def foo(): pass")
        assert "def foo(): pass" in result
        assert "=== SPECIFICATION ===" not in result
        assert "CODE" in result

    def test_with_spec(self):
        """build_review_prompt with spec includes SPECIFICATION block."""
        result = build_review_prompt(
            mode="code",
            content="def foo(): pass",
            spec="The foo function must return 42",
        )
        assert "=== SPECIFICATION ===" in result
        assert "The foo function must return 42" in result
        assert "correctly implements the specification" in result

    def test_plan_mode_label(self):
        """Plan mode uses 'PLAN' label."""
        result = build_review_prompt(mode="plan", content="Step 1: do X")
        assert "PLAN" in result

    def test_code_mode_label(self):
        """Code mode uses 'CODE' label."""
        result = build_review_prompt(mode="code", content="def bar(): pass")
        assert "CODE" in result


# ---------------------------------------------------------------------------
# build_reviewer_rebuttal_prompt tests
# ---------------------------------------------------------------------------


class TestBuildReviewerRebuttalPrompt:

    def test_mode_interpolated(self):
        """Mode variable is interpolated into the rebuttal template."""
        result = build_reviewer_rebuttal_prompt(
            mode="plan",
            original_content="# Plan\nStep 1",
            grouped_feedback="GROUP 1: concern",
            author_responses_text="RESPONSE TO GROUP 1: ACCEPTED",
        )
        assert isinstance(result, str)
        assert len(result) > 0
        # The template should contain the mode or mode_upper somewhere
        assert "PLAN" in result

    def test_code_mode(self):
        """Code mode renders without error."""
        result = build_reviewer_rebuttal_prompt(
            mode="code",
            original_content="def foo(): pass",
            grouped_feedback="GROUP 1: missing error handling",
            author_responses_text="RESPONSE: REJECTED",
        )
        assert "CODE" in result


# ---------------------------------------------------------------------------
# build_dedup_prompt tests
# ---------------------------------------------------------------------------


class TestBuildDedupPrompt:

    def test_template_loads_and_substitutes(self):
        """Dedup prompt loads and includes formatted points."""
        result = build_dedup_prompt("POINT 1: SQL injection risk\nPOINT 2: XSS vulnerability")
        assert "POINT 1: SQL injection risk" in result
        assert "POINT 2: XSS vulnerability" in result


# ---------------------------------------------------------------------------
# build_normalization_prompt tests
# ---------------------------------------------------------------------------


class TestBuildNormalizationPrompt:

    def test_template_loads_and_substitutes(self):
        """Normalization prompt loads and includes raw response text."""
        raw_response = "Some malformed LLM output that needs normalization"
        result = build_normalization_prompt(raw_response)
        assert raw_response in result


# ---------------------------------------------------------------------------
# build_integration_prompt tests
# ---------------------------------------------------------------------------


class TestBuildIntegrationPrompt:

    def test_template_loads_and_substitutes(self):
        """Integration prompt loads and includes files content and spec."""
        result = build_integration_prompt(
            files_content="=== FILE: api.py ===\ndef endpoint(): pass",
            spec="API must support pagination",
        )
        assert "api.py" in result
        assert "pagination" in result


# ---------------------------------------------------------------------------
# Spec mode prompt tests
# ---------------------------------------------------------------------------


class TestGetSpecReviewerSystemPrompt:

    def test_returns_non_empty_string(self):
        """get_spec_reviewer_system_prompt returns a non-empty string."""
        result = get_spec_reviewer_system_prompt()
        assert isinstance(result, str)
        assert len(result) > 0

    def test_cached_returns_same_object(self):
        """Subsequent calls return the cached value."""
        result1 = get_spec_reviewer_system_prompt()
        result2 = get_spec_reviewer_system_prompt()
        assert result1 is result2


class TestBuildSpecReviewPrompt:

    def test_template_loads_with_content(self):
        """Spec review prompt loads and includes the content."""
        result = build_spec_review_prompt(content="# Product Spec\n## Features")
        assert "Product Spec" in result


class TestBuildSpecDedupPrompt:

    def test_template_loads_with_points(self):
        """Spec dedup prompt loads and includes formatted points."""
        result = build_spec_dedup_prompt(
            formatted_points="SUGGESTION 1: Dark mode\nSUGGESTION 2: Export"
        )
        assert "SUGGESTION 1: Dark mode" in result


class TestBuildSpecRevisionPrompt:

    def test_template_loads_with_context(self):
        """Spec revision prompt loads with original content and revision context."""
        result = build_spec_revision_prompt(
            original_content="# Product Spec\nVersion 1.0",
            revision_context="=== THEME: Ux ===\nSuggestion about dark mode",
        )
        assert "Product Spec" in result
        assert "dark mode" in result
