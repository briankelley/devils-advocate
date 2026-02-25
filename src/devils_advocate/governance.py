"""Deterministic governance engine.

Applies rules to author responses, reviewer rebuttals, and author final
responses to produce governance decisions for each review group.

Core invariant: NO finding passes through governance without the author
demonstrating engagement. Implicit acceptance and rote acceptance both
escalate to human review.

Uses the author's final position (Round 2) for challenged groups,
falling back to the Round 1 response for unchallenged groups.

Zero external dependencies beyond ``types.py``.
"""

from __future__ import annotations

import re

from .types import (
    AuthorFinalResponse,
    AuthorResponse,
    GovernanceDecision,
    RebuttalResponse,
    Resolution,
    ReviewGroup,
)


# ─── Validation Helpers ─────────────────────────────────────────────────────


def validate_rejection(rationale: str) -> bool:
    """Returns True if the rejection meets all 3 validity criteria.

    Criteria:
      1. Specific technical reason
      2. Concrete scenario or mechanism
      3. Reference to specific part of codebase/spec/runtime

    If heuristic is uncertain, defaults to invalid (safe default = auto-accept
    the point).
    """
    has_technical = bool(re.search(
        r'\b(function|class|method|variable|parameter|return|type|interface|'
        r'module|import|dependency|thread|memory|latency|throughput|'
        r'complexity|O\(|runtime|compile|parse|serialize|async|await|'
        r'mutex|lock|buffer|stack|heap|pointer|reference|null|exception|'
        r'token|endpoint|schema|query|index|constraint|transaction)\b',
        rationale, re.IGNORECASE,
    ))

    has_mechanism = bool(re.search(
        r'\b(because|since|would cause|would break|would result|would fail|'
        r'leads to|introduces|creates|prevents|if .+ then|when .+ occurs|'
        r'this means|the consequence|the effect|resulting in|due to)\b',
        rationale, re.IGNORECASE,
    ))

    has_reference = bool(re.search(
        r'(`[^`]+`|"[^"]+"|\'[^\']+\'|'
        r'/[\w/.-]+\.\w+|'
        r'\b\w+\.\w+\(|'
        r'line\s+\d+|'
        r'section\s+\d+|'
        r'\bspec\b|'
        r'the\s+\w+\s+(file|module|class|function|method|handler|endpoint))',
        rationale, re.IGNORECASE,
    ))

    return all([has_technical, has_mechanism, has_reference])


# ─── Acceptance Validation ──────────────────────────────────────────────────


# Minimum word count for a substantive acceptance rationale
ACCEPTANCE_MIN_WORDS = 15

# Rote phrases that don't constitute substantive engagement
ROTE_ACCEPTANCE_PHRASES = [
    r'^accepted\.?$',
    r'^agree\.?$',
    r'^agreed\.?$',
    r'^acknowledged\.?$',
    r'^will\s+(do|implement|fix|apply|change|address|update|incorporate)\.?$',
    r'^makes?\s+sense\.?$',
    r'^good\s+(point|catch|find|finding|suggestion|call|observation)\.?$',
    r'^sounds?\s+(good|right|correct|reasonable|fair)\.?$',
    r'^no\s+(objection|issue|problem|concern|disagreement)s?\.?$',
    r'^lgtm\.?$',
    r'^fair\s+(point|enough)\.?$',
    r'^the\s+reviewer\s+is\s+(correct|right)\.?$',
    r'^this\s+is\s+(correct|right|valid|fair|reasonable)\.?$',
    r'^i\s+accept\s+this\.?$',
    r'^noted\.?$',
    r'^understood\.?$',
]


def validate_acceptance(rationale: str) -> bool:
    """Returns True if the acceptance rationale demonstrates substantive engagement.

    The author must explain WHY the finding is correct or WHY the proposed
    change should be made. This forces the author to actually evaluate the
    reviewer's claim rather than rubber-stamping it.

    A substantive rationale must:
      - Be at least ACCEPTANCE_MIN_WORDS words long
      - Not be a rote acknowledgment phrase

    The bar is deliberately low. One real sentence is enough. The goal is not
    to burden the author but to prevent findings from passing through governance
    without the author ever reading them.

    Triggered by:
      - openwakeword incident: reviewer's factually wrong API claim auto-accepted
        through author non-response, broke production
      - Self-referential failure: critical finding about silent mis-governance
        was itself silently accepted through the same mechanism
    """
    text = rationale.strip()
    if not text:
        return False

    # Check against rote phrases
    for pattern in ROTE_ACCEPTANCE_PHRASES:
        if re.match(pattern, text, re.IGNORECASE):
            return False

    # Word count check
    words = text.split()
    if len(words) < ACCEPTANCE_MIN_WORDS:
        return False

    return True


# ─── Governance Decision Engine ─────────────────────────────────────────────


def apply_governance(
    groups: list[ReviewGroup],
    author_responses: list[AuthorResponse],
    rebuttals: list[RebuttalResponse] | None = None,
    author_final_responses: list[AuthorFinalResponse] | None = None,
    mode: str = "plan",
) -> list[GovernanceDecision]:
    """Apply deterministic governance rules. Returns list of GovernanceDecision.

    Core invariant: NO finding passes through governance without the author
    demonstrating engagement. Implicit acceptance and rote acceptance both
    escalate to human review.

    Uses the author's final position (Round 2) for challenged groups,
    falling back to the Round 1 response for unchallenged groups.
    """
    # Build effective response: final response overrides Round 1 for challenged groups
    response_map = {ar.group_id: ar for ar in author_responses}
    final_map = {af.group_id: af for af in (author_final_responses or [])}

    # Build rebuttal lookup
    challenge_map: dict[str, list[RebuttalResponse]] = {}
    for rb in (rebuttals or []):
        if rb.verdict == "CHALLENGE":
            challenge_map.setdefault(rb.group_id, []).append(rb)

    decisions: list[GovernanceDecision] = []

    for group in groups:
        ar = response_map.get(group.group_id)
        af = final_map.get(group.group_id)
        group_challenges = challenge_map.get(group.group_id, [])
        num_reviewers = len(group.source_reviewers)

        # Determine effective resolution and rationale
        # Final response supersedes Round 1 for challenged groups
        if af:
            effective_resolution = af.resolution
            effective_rationale = af.rationale
            effective_source = "final"
        elif ar:
            effective_resolution = ar.resolution
            effective_rationale = ar.rationale
            effective_source = "round1"
        else:
            effective_resolution = None
            effective_rationale = ""
            effective_source = "none"

        if effective_resolution is None:
            # No response in either round
            decisions.append(GovernanceDecision(
                group_id=group.group_id,
                author_resolution="no_response",
                governance_resolution=Resolution.ESCALATED.value,
                reason="Author did not respond — escalated to human (no implicit acceptance)",
            ))
            continue

        if effective_resolution == "MAINTAINED":
            # Author stood by original position after being challenged
            # Treat same as a rejection -- needs 3-criteria validation if multi-reviewer
            if num_reviewers >= 2:
                if validate_rejection(effective_rationale):
                    decisions.append(GovernanceDecision(
                        group_id=group.group_id,
                        author_resolution="maintained",
                        governance_resolution=Resolution.ESCALATED.value,
                        reason=f"Author maintained position despite challenge from "
                               f"{len(group_challenges)} reviewer(s) — escalated to human",
                    ))
                else:
                    decisions.append(GovernanceDecision(
                        group_id=group.group_id,
                        author_resolution="maintained",
                        governance_resolution=Resolution.AUTO_ACCEPTED.value,
                        reason=f"Author maintained position but rationale failed validation "
                               f"vs {num_reviewers} reviewers — reviewer finding auto-accepted",
                    ))
            else:
                # Single reviewer challenged, author maintained
                decisions.append(GovernanceDecision(
                    group_id=group.group_id,
                    author_resolution="maintained",
                    governance_resolution=Resolution.ESCALATED.value,
                    reason="Author maintained position after reviewer challenge — escalated to human",
                ))
            continue

        if effective_resolution in ("ACCEPTED", "REJECTED", "PARTIAL"):
            if effective_resolution == "ACCEPTED":
                # Was this acceptance challenged?
                if group_challenges and effective_source == "round1":
                    # Challenged but no final response -- escalate
                    challenger_names = ", ".join(set(rb.reviewer for rb in group_challenges))
                    decisions.append(GovernanceDecision(
                        group_id=group.group_id,
                        author_resolution="accepted",
                        governance_resolution=Resolution.ESCALATED.value,
                        reason=f"Acceptance challenged by {challenger_names}, "
                               f"author did not provide final response — escalated to human",
                    ))
                elif validate_acceptance(effective_rationale):
                    decisions.append(GovernanceDecision(
                        group_id=group.group_id,
                        author_resolution="accepted",
                        governance_resolution=Resolution.AUTO_ACCEPTED.value,
                        reason="Author accepted with substantive rationale"
                               + (", unchallenged" if not group_challenges else
                                  ", final position after challenge"),
                    ))
                else:
                    decisions.append(GovernanceDecision(
                        group_id=group.group_id,
                        author_resolution="accepted",
                        governance_resolution=Resolution.ESCALATED.value,
                        reason="Author accepted without substantive rationale — escalated to human",
                    ))

            elif effective_resolution == "PARTIAL":
                decisions.append(GovernanceDecision(
                    group_id=group.group_id,
                    author_resolution="partial",
                    governance_resolution=Resolution.ESCALATED.value,
                    reason="Partial acceptance — escalated to human for review (not yet incorporated into revision)",
                ))

            elif effective_resolution == "REJECTED":
                if num_reviewers >= 2:
                    if validate_rejection(effective_rationale):
                        decisions.append(GovernanceDecision(
                            group_id=group.group_id,
                            author_resolution="rejected",
                            governance_resolution=Resolution.ESCALATED.value,
                            reason=f"Rejection with valid objection vs {num_reviewers} "
                                   f"reviewers — escalated to human",
                        ))
                    else:
                        decisions.append(GovernanceDecision(
                            group_id=group.group_id,
                            author_resolution="rejected",
                            governance_resolution=Resolution.AUTO_ACCEPTED.value,
                            reason=f"Rejection failed validation vs {num_reviewers} "
                                   f"reviewers — auto-accepted",
                        ))
                else:
                    if mode == "integration":
                        decisions.append(GovernanceDecision(
                            group_id=group.group_id,
                            author_resolution="rejected",
                            governance_resolution=Resolution.ESCALATED.value,
                            reason="Integration — single-reviewer rejection escalated",
                        ))
                    elif group_challenges:
                        decisions.append(GovernanceDecision(
                            group_id=group.group_id,
                            author_resolution="rejected",
                            governance_resolution=Resolution.ESCALATED.value,
                            reason="Single reviewer challenged author's rejection — escalated to human",
                        ))
                    else:
                        decisions.append(GovernanceDecision(
                            group_id=group.group_id,
                            author_resolution="rejected",
                            governance_resolution=Resolution.AUTO_DISMISSED.value,
                            reason="Single reviewer, author objects, unchallenged — auto-dismissed",
                        ))
            continue

        # Unknown resolution
        decisions.append(GovernanceDecision(
            group_id=group.group_id,
            author_resolution=effective_resolution.lower(),
            governance_resolution=Resolution.ESCALATED.value,
            reason="Unrecognized resolution — escalated to human",
        ))

    return decisions
