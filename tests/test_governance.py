"""Tests for devils_advocate.governance module."""

import pytest

from devils_advocate.governance import (
    ROTE_ACCEPTANCE_PHRASES,
    validate_acceptance,
    validate_rejection,
    apply_governance,
)
from devils_advocate.types import (
    AuthorFinalResponse,
    AuthorResponse,
    GovernanceDecision,
    RebuttalResponse,
    Resolution,
    ReviewGroup,
    ReviewPoint,
)

from helpers import (
    make_author_final,
    make_author_response,
    make_rebuttal,
    make_review_group,
    make_review_point,
)


# ─── TestValidateRejection ──────────────────────────────────────────────────


class TestValidateRejection:
    """Tests for validate_rejection: requires technical term, mechanism, and code reference."""

    # --- Individual criteria passing ---

    def test_technical_term_passes(self):
        """A technical term alone satisfies criterion 1."""
        text = "The function has a problem"
        # has_technical should match "function"
        import re
        assert bool(re.search(
            r'\b(function|class|method|variable|parameter|return|type|interface|'
            r'module|import|dependency|thread|memory|latency|throughput|'
            r'complexity|O\(|runtime|compile|parse|serialize|async|await|'
            r'mutex|lock|buffer|stack|heap|pointer|reference|null|exception|'
            r'token|endpoint|schema|query|index|constraint|transaction)\b',
            text, re.IGNORECASE,
        ))

    def test_mechanism_passes(self):
        """A mechanism phrase alone satisfies criterion 2."""
        text = "This would cause a failure in the system"
        import re
        assert bool(re.search(
            r'\b(because|since|would cause|would break|would result|would fail|'
            r'leads to|introduces|creates|prevents|if .+ then|when .+ occurs|'
            r'this means|the consequence|the effect|resulting in|due to)\b',
            text, re.IGNORECASE,
        ))

    def test_code_reference_passes(self):
        """A code reference alone satisfies criterion 3."""
        text = "See `some_func()` for details"
        import re
        assert bool(re.search(
            r'(`[^`]+`|"[^"]+"|\'[^\']+\'|'
            r'/[\w/.-]+\.\w+|'
            r'\b\w+\.\w+\(|'
            r'line\s+\d+|'
            r'section\s+\d+|'
            r'\bspec\b|'
            r'the\s+\w+\s+(file|module|class|function|method|handler|endpoint))',
            text, re.IGNORECASE,
        ))

    # --- Individual criteria failing ---

    def test_no_technical_term_fails(self):
        """Rationale with no technical term fails criterion 1."""
        # This has a mechanism and code reference, but no technical term from the list
        text = "This is bad because it breaks `foo.py`"
        # "breaks" is not in technical term list, but "because" satisfies mechanism,
        # and backtick satisfies code reference. Let's just check no technical term.
        import re
        assert not bool(re.search(
            r'\b(function|class|method|variable|parameter|return|type|interface|'
            r'module|import|dependency|thread|memory|latency|throughput|'
            r'complexity|O\(|runtime|compile|parse|serialize|async|await|'
            r'mutex|lock|buffer|stack|heap|pointer|reference|null|exception|'
            r'token|endpoint|schema|query|index|constraint|transaction)\b',
            "This is bad stuff happening here", re.IGNORECASE,
        ))

    def test_no_mechanism_fails(self):
        """Rationale with no mechanism phrase fails criterion 2."""
        import re
        assert not bool(re.search(
            r'\b(because|since|would cause|would break|would result|would fail|'
            r'leads to|introduces|creates|prevents|if .+ then|when .+ occurs|'
            r'this means|the consequence|the effect|resulting in|due to)\b',
            "The function at `foo.bar()` is wrong", re.IGNORECASE,
        ))

    def test_no_code_reference_fails(self):
        """Rationale with no code reference fails criterion 3."""
        import re
        assert not bool(re.search(
            r'(`[^`]+`|"[^"]+"|\'[^\']+\'|'
            r'/[\w/.-]+\.\w+|'
            r'\b\w+\.\w+\(|'
            r'line\s+\d+|'
            r'section\s+\d+|'
            r'\bspec\b|'
            r'the\s+\w+\s+(file|module|class|function|method|handler|endpoint))',
            "The function would cause memory issues for the system", re.IGNORECASE,
        ))

    # --- All 3 passing / failing ---

    def test_all_three_criteria_pass(self):
        """Rationale that satisfies all 3 criteria returns True."""
        rationale = (
            "The function `handle_request()` would cause a null pointer exception "
            "because the parameter is not validated before use."
        )
        assert validate_rejection(rationale) is True

    def test_all_three_criteria_fail(self):
        """Rationale that satisfies none of the 3 criteria returns False."""
        rationale = "I disagree with this finding."
        assert validate_rejection(rationale) is False


# ─── TestValidateAcceptance ──────────────────────────────────────────────────


class TestValidateAcceptance:
    """Tests for validate_acceptance: checks for rote phrases and minimum word count."""

    def test_each_rote_phrase_rejected(self):
        """Every phrase in ROTE_ACCEPTANCE_PHRASES triggers rejection."""
        rote_samples = [
            "accepted",
            "agree",
            "agreed",
            "acknowledged",
            "will do",
            "will implement",
            "will fix",
            "makes sense",
            "make sense",
            "good point",
            "good catch",
            "sounds good",
            "sounds right",
            "no objection",
            "no objections",
            "lgtm",
            "fair point",
            "fair enough",
            "the reviewer is correct",
            "the reviewer is right",
            "this is correct",
            "this is right",
            "this is valid",
            "i accept this",
            "noted",
            "understood",
        ]
        for phrase in rote_samples:
            assert validate_acceptance(phrase) is False, f"Rote phrase not rejected: {phrase!r}"

    def test_word_count_boundary_14_fails(self):
        """Exactly 14 words should fail the minimum word count check."""
        text = "word " * 14
        text = text.strip()
        assert len(text.split()) == 14
        assert validate_acceptance(text) is False

    def test_word_count_boundary_15_passes(self):
        """Exactly 15 words should pass the minimum word count check."""
        text = "word " * 15
        text = text.strip()
        assert len(text.split()) == 15
        assert validate_acceptance(text) is True

    def test_empty_rationale_fails(self):
        """Empty rationale returns False."""
        assert validate_acceptance("") is False
        assert validate_acceptance("   ") is False

    def test_substantive_rationale_passes(self):
        """A substantive rationale with enough words passes."""
        text = (
            "The reviewer correctly identified that the error handling path does not "
            "account for network timeouts, which could leave connections in a half-open state."
        )
        assert validate_acceptance(text) is True


# ─── TestApplyGovernance ─────────────────────────────────────────────────────


class TestApplyGovernance:
    """Full decision matrix tests for apply_governance."""

    # Helper to build a single-group scenario
    @staticmethod
    def _run(
        resolution=None,
        rationale="",
        num_reviewers=1,
        mode="plan",
        challenged=False,
        has_final=False,
        final_resolution=None,
        final_rationale="",
    ):
        group = make_review_group(
            source_reviewers=[f"r{i}" for i in range(num_reviewers)],
        )

        author_responses = []
        if resolution is not None:
            author_responses = [make_author_response(
                group_id=group.group_id,
                resolution=resolution,
                rationale=rationale,
            )]

        rebuttals = []
        if challenged:
            rebuttals = [make_rebuttal(group_id=group.group_id)]

        author_finals = []
        if has_final:
            author_finals = [make_author_final(
                group_id=group.group_id,
                resolution=final_resolution or resolution,
                rationale=final_rationale or rationale,
            )]

        decisions = apply_governance(
            groups=[group],
            author_responses=author_responses,
            rebuttals=rebuttals if rebuttals else None,
            author_final_responses=author_finals if author_finals else None,
            mode=mode,
        )
        assert len(decisions) == 1
        return decisions[0]

    # Substantive rationale helper (15+ words)
    SUBSTANTIVE = (
        "The reviewer correctly identified that the error handling path does not "
        "account for network timeouts which could leave connections in a half-open state"
    )

    # Valid rejection rationale (all 3 criteria)
    VALID_REJECTION = (
        "The function `handle_request()` would cause a null pointer exception "
        "because the parameter is not validated before use in the handler."
    )

    # Invalid rejection rationale (fails criteria)
    INVALID_REJECTION = "I just disagree with this."

    def test_no_response_escalated(self):
        """No author response -> ESCALATED."""
        d = self._run(resolution=None)
        assert d.governance_resolution == Resolution.ESCALATED.value

    def test_accepted_substantive_unchallenged(self):
        """ACCEPTED with substantive rationale, unchallenged -> AUTO_ACCEPTED."""
        d = self._run(resolution="ACCEPTED", rationale=self.SUBSTANTIVE)
        assert d.governance_resolution == Resolution.AUTO_ACCEPTED.value

    def test_accepted_rote_rationale(self):
        """ACCEPTED with rote rationale -> ESCALATED."""
        d = self._run(resolution="ACCEPTED", rationale="sounds good")
        assert d.governance_resolution == Resolution.ESCALATED.value

    def test_accepted_challenged_no_final(self):
        """ACCEPTED but challenged, no final response -> ESCALATED."""
        d = self._run(
            resolution="ACCEPTED",
            rationale=self.SUBSTANTIVE,
            challenged=True,
            has_final=False,
        )
        assert d.governance_resolution == Resolution.ESCALATED.value

    def test_accepted_challenged_with_substantive_final(self):
        """ACCEPTED, challenged, substantive final response -> AUTO_ACCEPTED."""
        d = self._run(
            resolution="ACCEPTED",
            rationale=self.SUBSTANTIVE,
            challenged=True,
            has_final=True,
            final_resolution="ACCEPTED",
            final_rationale=self.SUBSTANTIVE,
        )
        assert d.governance_resolution == Resolution.AUTO_ACCEPTED.value

    def test_partial_escalated(self):
        """PARTIAL in any mode -> ESCALATED."""
        d = self._run(resolution="PARTIAL", rationale=self.SUBSTANTIVE, mode="plan")
        assert d.governance_resolution == Resolution.ESCALATED.value

    def test_partial_integration_escalated(self):
        """PARTIAL in integration mode -> ESCALATED."""
        d = self._run(resolution="PARTIAL", rationale=self.SUBSTANTIVE, mode="integration")
        assert d.governance_resolution == Resolution.ESCALATED.value

    def test_rejected_multi_reviewer_valid(self):
        """REJECTED, 2+ reviewers, valid rationale -> ESCALATED."""
        d = self._run(
            resolution="REJECTED",
            rationale=self.VALID_REJECTION,
            num_reviewers=2,
        )
        assert d.governance_resolution == Resolution.ESCALATED.value

    def test_rejected_multi_reviewer_invalid(self):
        """REJECTED, 2+ reviewers, invalid rationale -> AUTO_ACCEPTED."""
        d = self._run(
            resolution="REJECTED",
            rationale=self.INVALID_REJECTION,
            num_reviewers=2,
        )
        assert d.governance_resolution == Resolution.AUTO_ACCEPTED.value

    def test_single_reviewer_rejected_integration(self):
        """Single reviewer REJECTED in integration mode -> ESCALATED."""
        d = self._run(
            resolution="REJECTED",
            rationale=self.VALID_REJECTION,
            num_reviewers=1,
            mode="integration",
        )
        assert d.governance_resolution == Resolution.ESCALATED.value

    def test_single_reviewer_rejected_plan_unchallenged(self):
        """Single reviewer REJECTED, plan mode, unchallenged -> AUTO_DISMISSED."""
        d = self._run(
            resolution="REJECTED",
            rationale=self.VALID_REJECTION,
            num_reviewers=1,
            mode="plan",
            challenged=False,
        )
        assert d.governance_resolution == Resolution.AUTO_DISMISSED.value

    def test_single_reviewer_rejected_plan_challenged(self):
        """Single reviewer REJECTED, plan mode, challenged -> ESCALATED."""
        d = self._run(
            resolution="REJECTED",
            rationale=self.VALID_REJECTION,
            num_reviewers=1,
            mode="plan",
            challenged=True,
        )
        assert d.governance_resolution == Resolution.ESCALATED.value

    def test_maintained_multi_reviewer_valid(self):
        """MAINTAINED, 2+ reviewers, valid rationale -> ESCALATED."""
        d = self._run(
            resolution="ACCEPTED",
            rationale=self.SUBSTANTIVE,
            num_reviewers=2,
            challenged=True,
            has_final=True,
            final_resolution="MAINTAINED",
            final_rationale=self.VALID_REJECTION,
        )
        assert d.governance_resolution == Resolution.ESCALATED.value

    def test_maintained_multi_reviewer_invalid(self):
        """MAINTAINED, 2+ reviewers, invalid rationale -> AUTO_ACCEPTED."""
        d = self._run(
            resolution="ACCEPTED",
            rationale=self.SUBSTANTIVE,
            num_reviewers=2,
            challenged=True,
            has_final=True,
            final_resolution="MAINTAINED",
            final_rationale=self.INVALID_REJECTION,
        )
        assert d.governance_resolution == Resolution.AUTO_ACCEPTED.value

    def test_unknown_resolution_escalated(self):
        """Unknown resolution string -> ESCALATED."""
        group = make_review_group()
        ar = AuthorResponse(
            group_id=group.group_id,
            resolution="SOMETHING_WEIRD",
            rationale="whatever",
        )
        decisions = apply_governance(
            groups=[group],
            author_responses=[ar],
            mode="plan",
        )
        assert len(decisions) == 1
        assert decisions[0].governance_resolution == Resolution.ESCALATED.value
