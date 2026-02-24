"""Tests for devils_advocate.cost module and CostTracker type."""

import pytest

from devils_advocate.cost import (
    CHARS_PER_TOKEN,
    check_context_window,
    estimate_cost,
    estimate_tokens,
)
from devils_advocate.types import CostTracker, ModelConfig

from conftest import make_model_config


# ─── TestEstimateTokens ────────────────────────────────────────────────────


class TestEstimateTokens:
    """Tests for estimate_tokens."""

    def test_400_chars_is_100_tokens(self):
        """400 characters -> 100 tokens (CHARS_PER_TOKEN = 4)."""
        text = "a" * 400
        assert estimate_tokens(text) == 100

    def test_empty_string_returns_1(self):
        """Empty string returns minimum of 1 token."""
        assert estimate_tokens("") == 1

    def test_short_string_returns_at_least_1(self):
        """Very short strings still return at least 1."""
        assert estimate_tokens("hi") >= 1

    def test_chars_per_token_is_4(self):
        """Constant CHARS_PER_TOKEN is 4."""
        assert CHARS_PER_TOKEN == 4

    def test_exact_multiple(self):
        """Exact multiples of CHARS_PER_TOKEN divide cleanly."""
        assert estimate_tokens("a" * 40) == 10
        assert estimate_tokens("a" * 4) == 1


# ─── TestEstimateCost ──────────────────────────────────────────────────────


class TestEstimateCost:
    """Tests for estimate_cost."""

    def test_basic_cost_calculation(self):
        """Cost is computed correctly from model pricing."""
        model = make_model_config(
            cost_per_1k_input=0.03,
            cost_per_1k_output=0.06,
        )
        # 1000 input tokens * 0.03/1k = 0.03
        # 500 output tokens * 0.06/1k = 0.03
        # Total = 0.06
        cost = estimate_cost(model, 1000, 500)
        assert abs(cost - 0.06) < 1e-9

    def test_zero_tokens(self):
        """Zero tokens should produce zero cost."""
        model = make_model_config()
        assert estimate_cost(model, 0, 0) == 0.0

    def test_none_pricing_returns_zero(self):
        """Model with None pricing returns 0.0."""
        model = make_model_config(
            cost_per_1k_input=None,
            cost_per_1k_output=None,
        )
        assert estimate_cost(model, 1000, 1000) == 0.0

    def test_partial_none_pricing(self):
        """If either cost field is None, returns 0.0."""
        model = make_model_config(cost_per_1k_input=0.03, cost_per_1k_output=None)
        assert estimate_cost(model, 1000, 1000) == 0.0


# ─── TestCostTracker ───────────────────────────────────────────────────────


class TestCostTracker:
    """Tests for CostTracker dataclass."""

    def test_add_accumulates_total(self):
        """CostTracker.add() accumulates total_usd."""
        ct = CostTracker(max_cost=10.0)
        ct.add("model-a", 1000, 500, 0.03, 0.06)
        # Cost = 1000/1000*0.03 + 500/1000*0.06 = 0.03 + 0.03 = 0.06
        assert abs(ct.total_usd - 0.06) < 1e-9
        ct.add("model-a", 1000, 500, 0.03, 0.06)
        assert abs(ct.total_usd - 0.12) < 1e-9

    def test_warned_80_threshold(self):
        """warned_80 is set when total >= 80% of max_cost."""
        ct = CostTracker(max_cost=1.0)
        assert ct.warned_80 is False
        # Add enough to reach 80% ($0.80)
        # 1000 input tokens * $10/1k = $10 per call -- too much
        # Use pricing: 0.80/1k input, 0 output -> 1000 tokens = $0.80
        ct.add("model-a", 1000, 0, 0.80, 0.0)
        assert ct.warned_80 is True

    def test_exceeded_threshold(self):
        """exceeded is set when total >= 100% of max_cost."""
        ct = CostTracker(max_cost=0.10)
        assert ct.exceeded is False
        # Add $0.10 exactly
        ct.add("model-a", 1000, 0, 0.10, 0.0)
        assert ct.exceeded is True

    def test_warned_before_exceeded(self):
        """80% warning triggers before 100% exceeded on incremental adds."""
        ct = CostTracker(max_cost=1.0)
        # Add $0.80 -> should trigger warned but not exceeded
        ct.add("model-a", 1000, 0, 0.80, 0.0)
        assert ct.warned_80 is True
        assert ct.exceeded is False
        # Add $0.20 more -> should trigger exceeded
        ct.add("model-a", 1000, 0, 0.20, 0.0)
        assert ct.exceeded is True

    def test_breakdown_groups_by_model(self):
        """breakdown() groups costs by model name."""
        ct = CostTracker()
        ct.add("model-a", 1000, 0, 0.01, 0.0)  # $0.01
        ct.add("model-b", 1000, 0, 0.02, 0.0)  # $0.02
        ct.add("model-a", 1000, 0, 0.01, 0.0)  # $0.01

        bd = ct.breakdown()
        assert "model-a" in bd
        assert "model-b" in bd
        assert abs(bd["model-a"] - 0.02) < 1e-9
        assert abs(bd["model-b"] - 0.02) < 1e-9

    def test_no_max_cost_no_warnings(self):
        """Without max_cost, warned_80 and exceeded remain False."""
        ct = CostTracker()  # max_cost is None
        ct.add("model-a", 100000, 100000, 1.0, 1.0)
        assert ct.warned_80 is False
        assert ct.exceeded is False

    def test_add_with_none_costs(self):
        """add() with None cost fields accumulates zero cost."""
        ct = CostTracker(max_cost=1.0)
        ct.add("model-a", 1000, 500, None, None)
        assert ct.total_usd == 0.0
        assert len(ct.entries) == 1

    def test_log_fn_callback_emitted_on_add_with_role(self):
        """_log_fn callback is called during add() when role is provided."""
        log_messages = []
        ct = CostTracker(_log_fn=lambda msg: log_messages.append(msg))
        ct.add("model-a", 1000, 500, 0.03, 0.06, role="reviewer")
        assert len(log_messages) == 1
        assert "cost" in log_messages[0]
        assert "role=reviewer" in log_messages[0]
        assert "model=model-a" in log_messages[0]

    def test_log_fn_not_called_without_role(self):
        """_log_fn callback is NOT called when no role is provided."""
        log_messages = []
        ct = CostTracker(_log_fn=lambda msg: log_messages.append(msg))
        ct.add("model-a", 1000, 500, 0.03, 0.06)  # no role
        assert len(log_messages) == 0

    def test_log_fn_none_no_error(self):
        """No error when _log_fn is None and role is provided."""
        ct = CostTracker(_log_fn=None)
        ct.add("model-a", 1000, 500, 0.03, 0.06, role="reviewer")
        # Should complete without error
        assert ct.total_usd > 0

    def test_role_costs_tracking_single_role(self):
        """role_costs tracks accumulated cost per role."""
        ct = CostTracker()
        ct.add("model-a", 1000, 0, 0.01, 0.0, role="reviewer")  # $0.01
        ct.add("model-a", 1000, 0, 0.02, 0.0, role="reviewer")  # $0.02
        assert "reviewer" in ct.role_costs
        assert abs(ct.role_costs["reviewer"] - 0.03) < 1e-9

    def test_role_costs_tracking_multiple_roles(self):
        """role_costs tracks separate totals for different roles."""
        ct = CostTracker()
        ct.add("model-a", 1000, 0, 0.01, 0.0, role="reviewer")   # $0.01
        ct.add("model-b", 1000, 0, 0.02, 0.0, role="author")     # $0.02
        ct.add("model-a", 1000, 0, 0.01, 0.0, role="reviewer")   # $0.01
        ct.add("model-c", 1000, 0, 0.03, 0.0, role="revision")   # $0.03
        assert abs(ct.role_costs["reviewer"] - 0.02) < 1e-9
        assert abs(ct.role_costs["author"] - 0.02) < 1e-9
        assert abs(ct.role_costs["revision"] - 0.03) < 1e-9

    def test_role_costs_empty_role_not_tracked(self):
        """Empty string role does not create a role_costs entry."""
        ct = CostTracker()
        ct.add("model-a", 1000, 0, 0.01, 0.0, role="")
        assert "" not in ct.role_costs
        assert len(ct.role_costs) == 0

    def test_warned_80_flag_stays_set(self):
        """Once warned_80 is True, it stays True even if cost drops (it never drops)."""
        ct = CostTracker(max_cost=1.0)
        ct.add("model-a", 1000, 0, 0.80, 0.0)  # $0.80 >= 80%
        assert ct.warned_80 is True
        # Additional small add doesn't unset it
        ct.add("model-a", 1, 0, 0.001, 0.0)
        assert ct.warned_80 is True

    def test_exceeded_flag_stays_set(self):
        """Once exceeded is True, it stays True on subsequent adds."""
        ct = CostTracker(max_cost=0.10)
        ct.add("model-a", 1000, 0, 0.10, 0.0)  # $0.10 = 100%
        assert ct.exceeded is True
        ct.add("model-a", 1, 0, 0.001, 0.0)
        assert ct.exceeded is True

    def test_warned_80_not_triggered_below_threshold(self):
        """warned_80 stays False when cost is below 80% of max_cost."""
        ct = CostTracker(max_cost=1.0)
        ct.add("model-a", 1000, 0, 0.79, 0.0)  # $0.79 < 80%
        assert ct.warned_80 is False

    def test_exceeded_not_triggered_below_threshold(self):
        """exceeded stays False when cost is below max_cost."""
        ct = CostTracker(max_cost=1.0)
        ct.add("model-a", 1000, 0, 0.99, 0.0)  # $0.99 < $1.00
        assert ct.exceeded is False

    def test_log_fn_includes_total_cost(self):
        """Log message includes running total cost."""
        log_messages = []
        ct = CostTracker(_log_fn=lambda msg: log_messages.append(msg))
        ct.add("model-a", 1000, 0, 0.01, 0.0, role="reviewer")
        ct.add("model-a", 1000, 0, 0.02, 0.0, role="author")
        assert len(log_messages) == 2
        # Second message should reference total of $0.03
        assert "total=" in log_messages[1]


# ─── TestCheckContextWindow ────────────────────────────────────────────────


class TestCheckContextWindow:
    """Tests for check_context_window."""

    def test_within_limit(self):
        """Text that fits within context window returns (True, est, limit)."""
        model = make_model_config(context_window=128000)
        # 400 chars = 100 tokens. limit = 128000 * 0.8 = 102400
        fits, est, limit = check_context_window(model, "a" * 400)
        assert fits is True
        assert est == 100
        assert limit == 102400

    def test_exceeds_limit(self):
        """Text exceeding context window returns (False, est, limit)."""
        model = make_model_config(context_window=100)
        # 100 * 0.8 = 80 token limit. "a"*400 = 100 tokens -> exceeds
        fits, est, limit = check_context_window(model, "a" * 400)
        assert fits is False
        assert est == 100
        assert limit == 80

    def test_no_context_window(self):
        """Model with context_window=None always fits, limit=0."""
        model = make_model_config(context_window=None)
        fits, est, limit = check_context_window(model, "a" * 40000)
        assert fits is True
        assert limit == 0
        assert est == 10000
