"""Type definitions, dataclasses, enums, and exceptions for Devil's Advocate."""

from __future__ import annotations

import enum
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


# ─── Enums ────────────────────────────────────────────────────────────────────


class Severity(enum.Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class Category(enum.Enum):
    ARCHITECTURE = "architecture"
    SECURITY = "security"
    PERFORMANCE = "performance"
    CORRECTNESS = "correctness"
    MAINTAINABILITY = "maintainability"
    ERROR_HANDLING = "error_handling"
    TESTING = "testing"
    DOCUMENTATION = "documentation"
    OTHER = "other"


class Resolution(enum.Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    PARTIAL = "partial"
    AUTO_ACCEPTED = "auto_accepted"
    AUTO_DISMISSED = "auto_dismissed"
    ESCALATED = "escalated"
    OVERRIDDEN = "overridden"
    PENDING = "pending"


# ─── Exceptions ───────────────────────────────────────────────────────────────


class AdvocateError(Exception):
    """Base exception for all Devil's Advocate errors."""
    pass


class ConfigError(AdvocateError):
    """Configuration loading or validation error."""
    pass


class APIError(AdvocateError):
    """LLM provider API call failure."""
    pass


class CostLimitError(AdvocateError):
    """Cost budget exceeded."""
    pass


class StorageError(AdvocateError):
    """Storage or file I/O error."""
    pass


# ─── Data Classes ─────────────────────────────────────────────────────────────


@dataclass
class ModelConfig:
    name: str
    provider: str
    model_id: str
    api_key_env: str
    api_base: str = ""
    roles: set = field(default_factory=set)
    deduplication: bool = False
    integration_reviewer: bool = False
    context_window: int | None = None
    cost_per_1k_input: float | None = None
    cost_per_1k_output: float | None = None
    timeout: int = 120
    use_completion_tokens: bool = False
    thinking: bool = False

    @property
    def api_key(self) -> str:
        return os.environ.get(self.api_key_env, "")


@dataclass
class ReviewPoint:
    point_id: str
    reviewer: str
    severity: str
    category: str
    description: str
    recommendation: str
    location: str = ""


@dataclass
class ReviewGroup:
    group_id: str
    concern: str
    points: list  # list of ReviewPoint
    combined_severity: str = "medium"
    combined_category: str = "other"
    source_reviewers: list = field(default_factory=list)
    guid: str = ""  # Assigned after dedup for prompt round-trip correlation


@dataclass
class AuthorResponse:
    group_id: str
    resolution: str  # ACCEPTED, REJECTED, PARTIAL
    rationale: str
    diff: str = ""


@dataclass
class RebuttalResponse:
    """Models a single reviewer's rebuttal of one group during Round 2."""
    group_id: str
    reviewer: str
    verdict: str  # CONCUR or CHALLENGE
    rationale: str


@dataclass
class AuthorFinalResponse:
    """Models the author's final position on a challenged group during Round 2."""
    group_id: str
    resolution: str  # ACCEPTED, REJECTED, MAINTAINED
    rationale: str


@dataclass
class GovernanceDecision:
    group_id: str
    author_resolution: str
    governance_resolution: str  # Resolution enum value
    reason: str


@dataclass
class CostTracker:
    entries: list = field(default_factory=list)
    total_usd: float = 0.0
    max_cost: float | None = None
    warned_80: bool = False
    exceeded: bool = False
    role_costs: dict = field(default_factory=dict)
    _log_fn: Any = field(default=None, repr=False)

    def add(
        self,
        model_name: str,
        input_tokens: int,
        output_tokens: int,
        cost_input: float | None,
        cost_output: float | None,
        role: str = "",
    ) -> None:
        cost = 0.0
        if cost_input is not None and cost_output is not None:
            cost = (
                input_tokens / 1000 * cost_input
                + output_tokens / 1000 * cost_output
            )
        self.entries.append({
            "model": model_name,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": round(cost, 6),
        })
        self.total_usd += cost

        if role:
            self.role_costs[role] = self.role_costs.get(role, 0.0) + cost

        # Emit structured cost event for GUI consumption
        if self._log_fn and role:
            self._log_fn(
                f"§cost role={role} model={model_name} "
                f"cost={cost:.6f} total={self.total_usd:.6f}"
            )

        # Update cost guardrail flags when a budget is set
        if self.max_cost is not None:
            if not self.warned_80 and self.total_usd >= self.max_cost * 0.8:
                self.warned_80 = True
            if not self.exceeded and self.total_usd >= self.max_cost:
                self.exceeded = True

    def breakdown(self) -> dict:
        by_model: dict[str, float] = {}
        for e in self.entries:
            by_model[e["model"]] = by_model.get(e["model"], 0.0) + e["cost_usd"]
        return by_model


@dataclass
class ReviewResult:
    review_id: str
    mode: str
    input_file: str
    project: str
    timestamp: str
    author_model: str
    reviewer_models: list
    dedup_model: str
    points: list  # list of dicts for ledger
    groups: list  # list of ReviewGroup
    author_responses: list  # list of AuthorResponse
    governance_decisions: list  # list of GovernanceDecision
    rebuttals: list = field(default_factory=list)  # list of RebuttalResponse
    author_final_responses: list = field(default_factory=list)  # list of AuthorFinalResponse
    cost: CostTracker = field(default_factory=CostTracker)
    revised_output: str = ""
    summary: dict = field(default_factory=dict)


@dataclass
class ReviewContext:
    """Carries project, timing, and ID state through the review pipeline.

    Created once per review run, passed to dedup and group creation.
    """
    project: str
    review_id: str
    review_start_time: datetime
    id_suffix: str = ""

    def __post_init__(self) -> None:
        if not self.id_suffix:
            from .ids import _random_suffix
            self.id_suffix = _random_suffix()

    def make_group_id(self, index: int) -> str:
        from .ids import generate_new_group_id
        return generate_new_group_id(self.project, index, self.review_start_time,
                                     self.id_suffix)

    def make_point_id(self, group_id: str, point_index: int) -> str:
        from .ids import generate_new_point_id
        return generate_new_point_id(group_id, point_index)
