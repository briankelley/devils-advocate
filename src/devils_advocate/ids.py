"""ID generation, assignment, and resolution for reviews, groups, and points."""

from __future__ import annotations

import hashlib
import random
import re
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


# ─── GUID Resolution ────────────────────────────────────────────────────────


def resolve_guid(
    raw_gid: str,
    groups: list[ReviewGroup],
    log_fn=None,
    silent: bool = False,
) -> str | None:
    """Resolve a GUID from an LLM response to a real group_id.

    Extracts UUID pattern from whatever the LLM wrapped around it.
    Fuzzy-matches up to 2 character differences to handle LLM transcription errors.
    """
    raw_gid = raw_gid.strip()
    guid_map = {g.guid: g.group_id for g in groups if g.guid}

    # Direct match
    if raw_gid in guid_map:
        if log_fn and not silent:
            log_fn(f"  ID match: exact '{raw_gid}' -> '{guid_map[raw_gid]}'")
        return guid_map[raw_gid]

    # Extract UUID from surrounding noise (e.g. "1 [uuid" or "GROUP 3 uuid")
    uuid_match = re.search(
        r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}',
        raw_gid, re.IGNORECASE,
    )
    if uuid_match:
        extracted = uuid_match.group(0).lower()
        if extracted in guid_map:
            if log_fn and not silent:
                log_fn(f"  ID match: extracted '{extracted}' -> '{guid_map[extracted]}'")
            return guid_map[extracted]

        # Fuzzy match: LLM sometimes miscopies 1-2 characters of the UUID
        best_match = None
        best_dist = float('inf')
        for guid in guid_map:
            dist = sum(a != b for a, b in zip(extracted, guid.lower()))
            dist += abs(len(extracted) - len(guid))
            if dist < best_dist:
                best_dist = dist
                best_match = guid
        if best_match is not None and best_dist <= 2:
            if log_fn and not silent:
                log_fn(f"  ID match: fuzzy '{extracted}' -> '{guid_map[best_match]}' (dist={best_dist})")
            return guid_map[best_match]

    if log_fn and not silent:
        log_fn(f"  ID match: FAILED for '{raw_gid}'")
    return None
