"""Tests for devils_advocate.ids module."""

import re
import pytest
from datetime import datetime, timezone

from devils_advocate.ids import (
    assign_guids,
    generate_new_group_id,
    generate_new_point_id,
    generate_review_id,
    _content_hash,
)
from devils_advocate.types import ReviewGroup

from conftest import make_review_group, make_review_point


# ─── TestGenerateReviewId ───────────────────────────────────────────────────


class TestGenerateReviewId:
    """Tests for generate_review_id."""

    def test_format(self):
        """Review ID follows YYYYMMDDThhmmss_<6-hex> format."""
        rid = generate_review_id("some content")
        pattern = r'^\d{8}T\d{6}_[0-9a-f]{6}$'
        assert re.match(pattern, rid), f"Review ID {rid!r} does not match expected format"

    def test_deterministic_hash(self):
        """Same content produces the same hash portion."""
        content = "identical content for hashing"
        h1 = _content_hash(content)
        h2 = _content_hash(content)
        assert h1 == h2

    def test_different_content_different_hash(self):
        """Different content produces different hash portions."""
        h1 = _content_hash("content A")
        h2 = _content_hash("content B")
        assert h1 != h2


# ─── TestGenerateNewGroupId ─────────────────────────────────────────────────


class TestGenerateNewGroupId:
    """Tests for generate_new_group_id."""

    def test_format(self):
        """Group ID follows project.group_NNN.ddMMMyyyy.HHMM.suffix format."""
        dt = datetime(2026, 2, 14, 18, 26, 0, tzinfo=timezone.utc)
        gid = generate_new_group_id("atlas-voice", 1, dt, "4g9a")
        assert gid == "atlas-voice.group_001.14FEB2026.1826.4g9a"

    def test_zero_padded_index(self):
        """Index is zero-padded to 3 digits."""
        dt = datetime(2026, 2, 14, 18, 26, 0, tzinfo=timezone.utc)
        gid = generate_new_group_id("proj", 42, dt, "abcd")
        assert ".group_042." in gid

    def test_different_projects(self):
        """Different project names produce different IDs."""
        dt = datetime(2026, 2, 14, 18, 26, 0, tzinfo=timezone.utc)
        g1 = generate_new_group_id("proj-a", 1, dt, "abcd")
        g2 = generate_new_group_id("proj-b", 1, dt, "abcd")
        assert g1 != g2
        assert g1.startswith("proj-a.")
        assert g2.startswith("proj-b.")


# ─── TestGenerateNewPointId ─────────────────────────────────────────────────


class TestGenerateNewPointId:
    """Tests for generate_new_point_id."""

    def test_format(self):
        """Point ID appends .point_NNN to group ID."""
        group_id = "proj.group_001.14FEB2026.1826.4g9a"
        pid = generate_new_point_id(group_id, 1)
        assert pid == "proj.group_001.14FEB2026.1826.4g9a.point_001"

    def test_zero_padded_index(self):
        """Point index is zero-padded to 3 digits."""
        group_id = "proj.group_001.14FEB2026.1826.4g9a"
        pid = generate_new_point_id(group_id, 12)
        assert pid.endswith(".point_012")

    def test_derived_from_group(self):
        """Point ID contains the full group ID as a prefix."""
        group_id = "atlas-voice.group_003.14FEB2026.1826.xxxx"
        pid = generate_new_point_id(group_id, 5)
        assert pid.startswith(group_id)


# ─── TestAssignGuids ────────────────────────────────────────────────────────


class TestAssignGuids:
    """Tests for assign_guids."""

    def test_assigns_uuid_to_all_groups(self):
        """Every group gets a non-empty UUID4 string."""
        g1 = make_review_group(group_id="grp_001")
        g2 = make_review_group(group_id="grp_002")
        assert g1.guid == ""
        assert g2.guid == ""

        assign_guids([g1, g2])

        assert g1.guid != ""
        assert g2.guid != ""
        # Validate UUID4 format
        uuid_pattern = r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        assert re.match(uuid_pattern, g1.guid, re.IGNORECASE)
        assert re.match(uuid_pattern, g2.guid, re.IGNORECASE)

    def test_unique_guids(self):
        """Each group receives a unique GUID."""
        groups = [make_review_group(group_id=f"grp_{i:03d}") for i in range(10)]
        assign_guids(groups)
        guids = [g.guid for g in groups]
        assert len(set(guids)) == 10

    def test_empty_list(self):
        """Assigning GUIDs to an empty list is a no-op."""
        assign_guids([])  # Should not raise


# ─── GUID determinism ───────────────────────────────────────────────────────


class TestGuidDeterminism:
    """Same content should produce the same hash portion in review_id."""

    def test_same_content_same_hash(self):
        content = "deterministic content test"
        id1 = generate_review_id(content)
        id2 = generate_review_id(content)
        # Hash portion is between first _ and second _
        hash1 = id1.split("_")[1]
        hash2 = id2.split("_")[1]
        assert hash1 == hash2


# ─── TestExtractRandomSuffix ────────────────────────────────────────────────


