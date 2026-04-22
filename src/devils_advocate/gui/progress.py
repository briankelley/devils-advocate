"""Progress event model and log parsing / phase detection."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class ProgressEvent:
    """A single progress event emitted during a review."""
    event_type: str  # log, phase, cost, complete, error
    message: str = ""
    phase: str = ""
    detail: dict = field(default_factory=dict)
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")

    def to_sse(self) -> str:
        """Format as SSE data line."""
        import json
        data = {
            "type": self.event_type,
            "message": self.message,
            "phase": self.phase,
            "detail": self.detail,
            "timestamp": self.timestamp,
        }
        return f"data: {json.dumps(data)}\n\n"


# Phase detection patterns (best-effort, matched against storage.log() corpus)
_PHASE_PATTERNS: list[tuple[str, str, dict[str, Any]]] = [
    # Cost events (must be first — suppressed from console log)
    (r"§cost role=(\S+) model=(.+?) cost=([\d.]+) total=([\d.]+)(?: in_tokens=(\d+) out_tokens=(\d+) total_tokens=(\d+))?", "cost_update", {}),

    # Integration reviewer (maps to round1 dots — same breadcrumb position)
    (r"Integration: calling (.+)", "round1_calling", {}),
    (r"Integration: (.+) responded \(", "round1_responded", {}),

    # Round 1
    (r"Round 1: calling author to respond to grouped feedback", "round1_author", {}),
    (r"Round 1: calling (.+)", "round1_calling", {}),
    (r"Round 1: author responded", "round1_author_responded", {}),
    (r"Round 1: (.+) responded \(", "round1_responded", {}),
    (r"No structured points from (.+) -- trying LLM normalization", "normalization", {}),
    (r"Normalization: calling (.+?) \(fallback for (.+?),", "normalization", {}),

    # Dedup
    (r"Deduplication: calling (.+?) \((\d+) points", "dedup_calling", {}),
    (r"Deduplication: (.+) responded \(", "dedup_responded", {}),

    # Round 2
    (r"Round 2: all groups accepted by author -- skipping", "round2_skip", {}),
    (r"Round 2: (.+) has no contested groups -- skipping", "round2_skip_reviewer", {}),
    (r"Round 2: sending author responses", "round2_sending", {}),
    (r"Round 2: calling author to respond to rebuttals", "round2_author_calling", {}),
    (r"Round 2: calling (.+?)(?:\s*\(|$)", "round2_calling", {}),
    (r"Round 2: author responded", "round2_author_responded", {}),
    (r"Round 2: (.+) responded \(", "round2_responded", {}),
    (r"Round 2: rebuttals complete", "round2_complete", {}),
    (r"Round 2: giving author last word", "round2_author", {}),
    (r"Skipping (.+) rebuttal: context exceeded", "round2_skip_context", {}),
    (r"Rebuttal (.+) failed: (.+)", "round2_rebuttal_failed", {}),
    (r"Author final response failed: (.+)", "round2_author_failed", {}),

    # Governance
    (r"Governance: applying", "governance_applying", {}),
    (r"Catastrophic parse failure", "governance_catastrophic", {}),
    (r"Governance complete: (.+)", "governance_complete", {}),

    # Cost
    (r"Cost warning: \$(.+) \(80% of \$(.+)\)", "cost_warning", {}),
    (r"Cost limit exceeded: \$(.+) >= \$(.+)", "cost_exceeded", {}),

    # Revision
    (r"Revision: generating revised artifact", "revision_generating", {}),
    (r"Revision: large context .+ expect ~(\d+) min", "revision_duration_estimate", {}),
    (r"Revision: calling (.+?)(?:\s*\(|$)", "revision_calling", {}),
    (r"Revision: (.+) responded \(", "revision_responded", {}),
    (r"Revision: no actionable findings", "revision_skip", {}),
    (r"Revision: prompt \((\d+) tokens\) exceeds context", "revision_skip_context", {}),
    (r"Revision: extraction failed", "revision_extraction_failed", {}),
    (r"Revision failed \(non-fatal\): (.+)", "revision_failed", {}),

    # Provider retry
    (r"(.+?): API overloaded \(529\) - waiting (.+)", "provider_retry", {}),
    (r"(.+?): HTTP (\d+), retry (.+)", "provider_retry", {}),
    (r"(.+?): (?:TimeoutException|ConnectError), retry (.+)", "provider_retry", {}),

    # Start
    (r"Starting (\w+) review for project '(.+)'", "review_start", {}),
]


def classify_log_message(msg: str) -> ProgressEvent:
    """Parse a log message into a ProgressEvent with best-effort phase detection."""
    for pattern, phase, extra in _PHASE_PATTERNS:
        m = re.search(pattern, msg)
        if m:
            # Cost events carry structured data and suppress console log output
            if phase == "cost_update":
                detail = {
                    "role": m.group(1),
                    "model": m.group(2),
                    "cost": m.group(3),
                    "total": m.group(4),
                }
                if m.group(5):
                    detail["in_tokens"] = m.group(5)
                    detail["out_tokens"] = m.group(6)
                    detail["total_tokens"] = m.group(7)
                return ProgressEvent(
                    event_type="cost",
                    message="",
                    phase=phase,
                    detail=detail,
                )
            return ProgressEvent(
                event_type="phase",
                message=msg,
                phase=phase,
                detail={"groups": list(m.groups()), **extra},
            )

    # Fallback: unclassified log line
    return ProgressEvent(event_type="log", message=msg, phase="unknown")


def make_terminal_event(success: bool, message: str = "") -> ProgressEvent:
    """Create a terminal SSE event (complete or error)."""
    return ProgressEvent(
        event_type="complete" if success else "error",
        message=message or ("Review complete" if success else "Review failed"),
        phase="done" if success else "error",
    )
