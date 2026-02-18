"""ID generation for reviews, groups, and points."""

from __future__ import annotations

import hashlib
import random
import uuid
from datetime import datetime, timezone

from .types import ReviewGroup


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _random_suffix(length: int = 4) -> str:
    """Generate a random alphanumeric suffix for IDs."""
    chars = "abcdefghijklmnopqrstuvwxyz0123456789"
    return "".join(random.choice(chars) for _ in range(length))


def _format_id_timestamp(dt: datetime) -> str:
    """Format datetime as ddMMMyyyy.HHMM for ID generation (e.g. 14FEB2026.1826)."""
    return dt.strftime("%d%b%Y.%H%M").upper()


def extract_random_suffix(id_str: str) -> str:
    """Extract the 4-char random suffix from a new-format ID.

    E.g. 'atlas-voice.group_001.14FEB2026.1826.4g9a' -> '4g9a'
    Returns empty string if not a new-format ID.
    """
    parts = id_str.split(".")
    if len(parts) >= 5:
        return parts[-1]
    return ""


# ─── ID Generators ────────────────────────────────────────────────────────────


def _timestamp_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()[:6]


def generate_review_id(content: str) -> str:
    """Generate a review ID from timestamp and content hash.

    Format: YYYYMMDDThhmmss_<sha256-6>_review
    """
    return f"{_timestamp_str()}_{_content_hash(content)}_review"


def generate_new_group_id(
    project: str,
    index: int,
    review_start_time: datetime,
    suffix: str,
) -> str:
    """Generate new-format group ID: project.group_NNN.ddMMMyyyy.HHMM.suffix"""
    ts = _format_id_timestamp(review_start_time)
    return f"{project}.group_{index:03d}.{ts}.{suffix}"


def generate_new_point_id(group_id: str, point_index: int) -> str:
    """Generate new-format point ID derived from parent group ID.

    E.g. 'atlas-voice.group_001.14FEB2026.1826.4g9a.point_001'
    """
    return f"{group_id}.point_{point_index:03d}"


# ─── GUID Assignment ─────────────────────────────────────────────────────────


def assign_guids(groups: list[ReviewGroup]) -> None:
    """Assign a UUID4 to each group for prompt round-trip correlation."""
    for g in groups:
        g.guid = str(uuid.uuid4())
