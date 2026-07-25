"""Shared types for the roster scanner."""

from __future__ import annotations


class RosterError(Exception):
    """Base error for the roster scanner. Never raised mid-review."""


class FetchError(RosterError):
    """Upstream payload was unreachable, malformed, or failed a sanity gate."""


class ReasonerError(RosterError):
    """The judgement stage failed or returned something unusable."""


class ApplyError(RosterError):
    """A proposal failed validation, or the config could not be written."""
