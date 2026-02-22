"""Prompt template loading and builder functions.

Templates are package data files under ``devils_advocate/templates/*.txt``,
loaded via :mod:`importlib.resources` so no filesystem path assumptions are
needed.
"""

from __future__ import annotations

import importlib.resources

from .types import AdvocateError

# ─── Constants ────────────────────────────────────────────────────────────────

CONTENT_START = "=== FILE CONTENT ==="
CONTENT_END = "=== END FILE CONTENT ==="

# ─── Template Loader ─────────────────────────────────────────────────────────

_reviewer_system_cache: str | None = None


def load_template(name: str, **kwargs: str) -> str:
    """Load a template from package data and apply str.format() substitution.

    Raises :class:`~devils_advocate.types.AdvocateError` if the template file
    is missing or a required variable is undefined.
    """
    templates = importlib.resources.files("devils_advocate.templates")
    resource = templates.joinpath(name)
    try:
        raw = resource.read_text(encoding="utf-8")
    except (FileNotFoundError, TypeError, OSError) as exc:
        raise AdvocateError(f"Template not found: {name}") from exc
    try:
        return raw.format(**kwargs) if kwargs else raw
    except KeyError as exc:
        raise AdvocateError(f"Template '{name}' missing variable: {exc}") from exc


# ─── Prompt Builders ─────────────────────────────────────────────────────────


def get_reviewer_system_prompt() -> str:
    """Lazy-loaded reviewer system prompt from template."""
    global _reviewer_system_cache
    if _reviewer_system_cache is None:
        _reviewer_system_cache = load_template("reviewer-system.txt")
    return _reviewer_system_cache


def _load_governance_block() -> str:
    """Load Round 1 governance rules from template."""
    return load_template("governance-rules.txt")


def _load_governance_final_block() -> str:
    """Load final round governance rules from template."""
    return load_template("governance-rules-final.txt")


def build_review_prompt(
    mode: str,
    content: str,
    spec: str | None = None,
) -> str:
    """Build the Round 1 reviewer instruction prompt."""
    mode_label = "PLAN" if mode == "plan" else "CODE"
    spec_line = (
        "Include whether the code correctly implements the specification." if spec else ""
    )
    spec_block = (
        f"\n\n=== SPECIFICATION ===\n{spec}\n=== END SPECIFICATION ===" if spec else ""
    )
    return load_template(
        "round1-reviewer-instruct.txt",
        mode_label=mode_label,
        content=content,
        spec_line=spec_line,
        spec_block=spec_block,
    )


def build_round1_author_prompt(
    mode: str,
    original_content: str,
    grouped_feedback: str,
) -> str:
    """Build the Round 1 author response prompt.

    Renamed from the monolith's ``build_round2_prompt`` to eliminate naming
    confusion with the actual Round 2 exchange.
    """
    if mode == "plan":
        template = "round1-author-plan-instruct.txt"
    elif mode == "integration":
        template = "round1-author-code-instruct.txt"
    else:
        template = "round1-author-code-instruct.txt"
    return load_template(
        template,
        governance_rules=_load_governance_block(),
        grouped_feedback=grouped_feedback,
        original_content=original_content,
    )


def build_reviewer_rebuttal_prompt(
    mode: str,
    original_content: str,
    grouped_feedback: str,
    author_responses_text: str,
) -> str:
    """Build the Round 2 rebuttal prompt for reviewers."""
    return load_template(
        "round2-reviewer-rebuttal-instruct.txt",
        mode=mode,
        mode_upper=mode.upper(),
        original_content=original_content,
        grouped_feedback=grouped_feedback,
        author_responses_text=author_responses_text,
    )


def build_author_final_prompt(
    mode: str,
    original_content: str,
    challenged_groups_text: str,
) -> str:
    """Build the author's final response prompt for challenged groups only."""
    if mode == "plan":
        template = "round2-author-final-plan-instruct.txt"
    else:
        template = "round2-author-final-code-instruct.txt"
    return load_template(
        template,
        governance_rules_final=_load_governance_final_block(),
        challenged_groups_text=challenged_groups_text,
        original_content=original_content,
    )


def build_dedup_prompt(formatted_points: str) -> str:
    """Build the deduplication instruction prompt."""
    return load_template("dedup-instruct.txt", formatted_points=formatted_points)


def build_normalization_prompt(raw_response: str) -> str:
    """Build the response normalization instruction prompt."""
    return load_template("normalization-instruct.txt", raw_response=raw_response)


def build_integration_prompt(files_content: str, spec: str) -> str:
    """Build the integration reviewer instruction prompt."""
    return load_template(
        "integration-reviewer-instruct.txt",
        files_content=files_content,
        spec=spec,
    )


# ─── Spec Mode Prompts ──────────────────────────────────────────────────────

_spec_reviewer_system_cache: str | None = None


def get_spec_reviewer_system_prompt() -> str:
    """Lazy-loaded spec reviewer system prompt from template."""
    global _spec_reviewer_system_cache
    if _spec_reviewer_system_cache is None:
        _spec_reviewer_system_cache = load_template("spec-reviewer-system.txt")
    return _spec_reviewer_system_cache


def build_spec_review_prompt(content: str) -> str:
    """Build the spec mode reviewer instruction prompt."""
    return load_template("spec-reviewer-instruct.txt", content=content)


def build_spec_dedup_prompt(formatted_points: str) -> str:
    """Build the spec mode dedup prompt."""
    return load_template("spec-dedup-instruct.txt", formatted_points=formatted_points)


def build_spec_revision_prompt(
    original_content: str,
    revision_context: str,
) -> str:
    """Build the spec mode revision prompt."""
    return load_template(
        "spec-revision-instruct.txt",
        original_content=original_content,
        revision_context=revision_context,
    )


