"""Automatic model-roster discovery from models.dev.

A daily background pass that keeps ``models.yaml`` aware of new models without
ever editing the operator's own decisions. Two laws govern every write:

**L1 — Additive only.** Anything already present in ``models.yaml`` is
grandfathered unconditionally. The gate answers *what is worth adding*, never
*what belongs here*. Nothing here deletes a model, sets ``enabled: false``, or
touches ``roles:`` — including a role deliberately left empty. Deprecation and
retirement are surfaced as notices, never acted on, because a role pointing at
a missing or disabled model is a fatal ``ConfigError`` (``config.py:253``) and
would leave dvad unable to load at all.

**L2 — Field ownership.** Every field has exactly one owner.

=========================  ===========================================
upstream (models.dev)      ``context_window``, ``max_out_stated``,
                           ``cost_per_1k_input``, ``cost_per_1k_output``
                           — and only on non-subscription rows
dvad (the provider map)    ``provider``, ``api_base``, ``api_key_env``,
                           ``use_completion_tokens``, ``stream``
the operator               ``thinking``, ``timeout``, ``max_out_configured``,
                           ``failover_model``, ``extra``, ``min_points_hint``,
                           and the whole ``roles:`` block
=========================  ===========================================

``thinking`` in particular is never derived from upstream's ``reasoning``
flag. They are different questions: upstream's means *this model can reason*,
the operator's means *ask this model to think for this role*.
"""

from __future__ import annotations

from .gate import Candidate, GateResult, HealthItem, Rejection, gate
from .types import RosterError

__all__ = [
    "Candidate",
    "GateResult",
    "HealthItem",
    "Rejection",
    "RosterError",
    "gate",
]
