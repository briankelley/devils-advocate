"""Synchronous response parsing for reviewer, author, dedup, and rebuttal outputs.

This module is STRICTLY synchronous. No async, no httpx, no provider calls.
All LLM-based normalization lives in ``normalization.py``.
"""

from __future__ import annotations

import re
from collections.abc import Callable

from .ids import resolve_guid
from .types import (
    AuthorFinalResponse,
    AuthorResponse,
    RebuttalResponse,
    ReviewContext,
    ReviewGroup,
    ReviewPoint,
)


# ─── Normalization Maps ──────────────────────────────────────────────────────


_SEVERITY_MAP = {
    "critical": "critical", "crit": "critical",
    "high": "high", "hi": "high",
    "medium": "medium", "med": "medium", "moderate": "medium",
    "low": "low", "lo": "low", "minor": "low",
    "info": "info", "informational": "info", "note": "info",
}

_CATEGORY_MAP = {
    "architecture": "architecture", "arch": "architecture", "design": "architecture",
    "security": "security", "sec": "security",
    "performance": "performance", "perf": "performance",
    "correctness": "correctness", "correct": "correctness", "bug": "correctness",
    "maintainability": "maintainability", "maintain": "maintainability", "readability": "maintainability",
    "error_handling": "error_handling", "error handling": "error_handling", "errors": "error_handling",
    "testing": "testing", "test": "testing", "tests": "testing",
    "documentation": "documentation", "docs": "documentation", "doc": "documentation",
    "other": "other",
}

_THEME_MAP = {
    "ux": "ux", "user_experience": "ux", "usability": "ux",
    "features": "features", "feature": "features", "functionality": "features",
    "integrations": "integrations", "integration": "integrations",
    "data_model": "data_model", "data model": "data_model", "data": "data_model",
    "monetization": "monetization", "revenue": "monetization", "pricing": "monetization",
    "accessibility": "accessibility", "a11y": "accessibility",
    "performance_ux": "performance_ux", "performance ux": "performance_ux",
    "content": "content",
    "social": "social", "community": "social",
    "platform": "platform",
    "security_privacy": "security_privacy", "security privacy": "security_privacy",
    "security": "security_privacy", "privacy": "security_privacy",
    "onboarding": "onboarding",
    "other": "other",
}


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _normalize_severity(raw: str) -> str:
    return _SEVERITY_MAP.get(raw.strip().lower(), "medium")


def _normalize_category(raw: str) -> str:
    key = raw.strip().lower().replace("-", "_").replace(" ", "_")
    return _CATEGORY_MAP.get(key, _CATEGORY_MAP.get(key.split("_")[0], "other"))


def _normalize_theme(raw: str) -> str:
    key = raw.strip().lower().replace("-", "_").replace(" ", "_")
    return _THEME_MAP.get(key, _THEME_MAP.get(key.split("_")[0], "other"))


def _extract_multiline_field(text: str, field_name: str, next_fields: list) -> str:
    """Extract a field value that may span multiple lines."""
    pattern = rf'{field_name}\s*:\s*(.*?)(?=(?:{"|".join(next_fields)})\s*:|REVIEW\s+POINT|$)'
    m = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
    if m:
        return m.group(1).strip()
    return ""


# ─── Shared Grouped Response Core ────────────────────────────────────────────


def _parse_grouped_response(
    raw: str,
    all_points: list[ReviewPoint],
    ctx: ReviewContext,
    extract_fields: Callable[[str], dict | None],
    point_ref_pattern: str,
    build_group_attrs: Callable[[dict, list[ReviewPoint]], dict],
    build_singleton_attrs: Callable[[ReviewPoint], dict],
) -> list[ReviewGroup]:
    """Shared core for dedup and spec-dedup response parsing.

    Parameters
    ----------
    extract_fields :
        Called per block. Returns a dict of extracted fields with at least
        'concern_text' and 'points_str' keys, or None to skip the block.
    point_ref_pattern :
        Regex with one capture group for the point index number.
    build_group_attrs :
        Given (fields_dict, found_points), returns a dict with keys
        'concern', 'combined_severity', 'combined_category'.
    build_singleton_attrs :
        Given a single ungrouped ReviewPoint, returns a dict with keys
        'combined_severity', 'combined_category'.
    """
    groups: list[ReviewGroup] = []
    idx_point_map = {i + 1: p for i, p in enumerate(all_points)}
    claimed_indices: set[int] = set()

    blocks = re.split(r'(?=GROUP\s+\d+\s*:)', raw, flags=re.IGNORECASE)

    group_idx = 0
    for block in blocks:
        if not block.strip():
            continue

        fields = extract_fields(block)
        if fields is None:
            continue

        # Parse point references (first claim wins)
        found_points: list[ReviewPoint] = []
        points_str = fields.get("points_str", "")
        if points_str:
            for num_match in re.finditer(point_ref_pattern, points_str, re.IGNORECASE):
                num = int(num_match.group(1))
                if num in idx_point_map and num not in claimed_indices:
                    found_points.append(idx_point_map[num])
                    claimed_indices.add(num)

        # Keyword fallback
        concern_text = fields.get("concern_text", "")
        if not found_points and concern_text:
            for idx_key, p in idx_point_map.items():
                if idx_key not in claimed_indices:
                    if any(word.lower() in concern_text.lower()
                           for word in p.description.split()[:5]):
                        found_points.append(p)
                        claimed_indices.add(idx_key)
                        break

        if not found_points:
            continue

        group_idx += 1
        group_id = ctx.make_group_id(group_idx)

        for pi, p in enumerate(found_points, 1):
            p.point_id = ctx.make_point_id(group_id, pi)

        reviewers = list(set(p.reviewer for p in found_points))
        attrs = build_group_attrs(fields, found_points)

        groups.append(ReviewGroup(
            group_id=group_id,
            concern=attrs["concern"],
            points=found_points,
            combined_severity=attrs["combined_severity"],
            combined_category=attrs["combined_category"],
            source_reviewers=reviewers,
        ))

    # Catch ungrouped points as singletons
    for idx_key, p in idx_point_map.items():
        if idx_key not in claimed_indices:
            group_idx += 1
            group_id = ctx.make_group_id(group_idx)
            p.point_id = ctx.make_point_id(group_id, 1)
            singleton_attrs = build_singleton_attrs(p)
            groups.append(ReviewGroup(
                group_id=group_id,
                concern=p.description,
                points=[p],
                combined_severity=singleton_attrs["combined_severity"],
                combined_category=singleton_attrs["combined_category"],
                source_reviewers=[p.reviewer],
            ))

    return groups


# ─── Review Response Parsing ────────────────────────────────────────────────


def parse_review_response(
    raw: str,
    reviewer_name: str,
    start_index: int = 0,
) -> list[ReviewPoint]:
    """Parse structured review points from a reviewer's response.

    Returns list of ReviewPoint. Falls back gracefully on partial matches.
    """
    points = []

    # Strip known reasoning delimiters before parsing
    raw = re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL | re.IGNORECASE)
    raw = re.sub(r'<thinking>.*?</thinking>', '', raw, flags=re.DOTALL | re.IGNORECASE)
    raw = re.sub(r'<reasoning>.*?</reasoning>', '', raw, flags=re.DOTALL | re.IGNORECASE)
    raw = re.sub(r'\*\*Thinking:\*\*.*?(?=REVIEW\s+POINT|\Z)', '', raw, flags=re.DOTALL)

    # Split into blocks by REVIEW POINT headers
    blocks = re.split(r'(?=(?:REVIEW\s+POINT|POINT|ISSUE)\s*#?\d+\s*:?)', raw, flags=re.IGNORECASE)

    idx = start_index
    for block in blocks:
        if not block.strip():
            continue

        severity = _extract_multiline_field(
            block, "SEVERITY",
            ["CATEGORY", "DESCRIPTION", "RECOMMENDATION", "LOCATION"],
        )
        category = _extract_multiline_field(
            block, "CATEGORY",
            ["DESCRIPTION", "RECOMMENDATION", "LOCATION"],
        )
        description = _extract_multiline_field(
            block, "DESCRIPTION",
            ["RECOMMENDATION", "LOCATION"],
        )
        recommendation = _extract_multiline_field(
            block, "RECOMMENDATION",
            ["LOCATION"],
        )
        location = _extract_multiline_field(
            block, "LOCATION",
            ["REVIEW POINT", "POINT", "ISSUE"],
        )

        if not description:
            continue

        idx += 1
        points.append(ReviewPoint(
            point_id=f"temp_{idx:03d}",
            reviewer=reviewer_name,
            severity=_normalize_severity(severity) if severity else "medium",
            category=_normalize_category(category) if category else "other",
            description=description,
            recommendation=recommendation or "No specific recommendation provided.",
            location=location or "",
        ))

    return points


# ─── Spec Response Parsing ──────────────────────────────────────────────────


def parse_spec_response(
    raw: str,
    reviewer_name: str,
    start_index: int = 0,
) -> list[ReviewPoint]:
    """Parse SUGGESTION N: formatted responses into ReviewPoints.

    Maps spec suggestion fields into ReviewPoint:
      theme -> category, title+description -> description, context -> location
    """
    points = []

    # Strip reasoning delimiters
    raw = re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL | re.IGNORECASE)
    raw = re.sub(r'<thinking>.*?</thinking>', '', raw, flags=re.DOTALL | re.IGNORECASE)
    raw = re.sub(r'<reasoning>.*?</reasoning>', '', raw, flags=re.DOTALL | re.IGNORECASE)

    # Split into blocks by SUGGESTION headers
    blocks = re.split(r'(?=SUGGESTION\s+#?\d+\s*:?)', raw, flags=re.IGNORECASE)

    idx = start_index
    for block in blocks:
        if not block.strip():
            continue

        theme = _extract_multiline_field(
            block, "THEME",
            ["TITLE", "DESCRIPTION", "CONTEXT"],
        )
        title = _extract_multiline_field(
            block, "TITLE",
            ["DESCRIPTION", "CONTEXT"],
        )
        description = _extract_multiline_field(
            block, "DESCRIPTION",
            ["CONTEXT", "SUGGESTION"],
        )
        context = _extract_multiline_field(
            block, "CONTEXT",
            ["SUGGESTION"],
        )

        if not description and not title:
            continue

        idx += 1
        # Combine title and description for the ReviewPoint description field
        full_desc = f"{title}: {description}" if title and description else (title or description)
        points.append(ReviewPoint(
            point_id=f"temp_{idx:03d}",
            reviewer=reviewer_name,
            severity="info",  # Suggestions don't have severity
            category=_normalize_theme(theme) if theme else "other",
            description=full_desc,
            recommendation="",  # Not applicable for suggestions
            location=context or "",
        ))

    return points


def parse_spec_dedup_response(
    raw: str,
    all_points: list[ReviewPoint],
    ctx: ReviewContext,
    total_reviewers: int = 2,
) -> list[ReviewGroup]:
    """Parse spec dedup response into ReviewGroup objects.

    Maps spec dedup fields: theme -> combined_category, title -> concern,
    consensus -> source_reviewers.
    """

    def _extract(block: str) -> dict | None:
        theme = _extract_multiline_field(block, "THEME", ["TITLE", "DESCRIPTION", "CONSENSUS", "SUGGESTIONS"])
        title = _extract_multiline_field(block, "TITLE", ["DESCRIPTION", "CONSENSUS", "SUGGESTIONS"])
        description = _extract_multiline_field(block, "DESCRIPTION", ["CONSENSUS", "SUGGESTIONS"])
        if not title and not description:
            return None
        suggestions_str = _extract_multiline_field(block, "SUGGESTIONS", ["GROUP"])
        return {
            "theme": theme,
            "title": title,
            "description": description,
            "points_str": suggestions_str,
            "concern_text": (title or "") + " " + (description or ""),
        }

    def _attrs(fields: dict, found_points: list[ReviewPoint]) -> dict:
        title = fields["title"]
        description = fields["description"]
        concern = f"{title}: {description}" if title and description else (title or description or "")
        return {
            "concern": concern,
            "combined_severity": "info",
            "combined_category": _normalize_theme(fields["theme"]) if fields["theme"] else "other",
        }

    def _singleton_attrs(p: ReviewPoint) -> dict:
        return {"combined_severity": "info", "combined_category": p.category}

    return _parse_grouped_response(
        raw, all_points, ctx,
        extract_fields=_extract,
        point_ref_pattern=r'(?:SUGGESTION\s+)?(\d+)',
        build_group_attrs=_attrs,
        build_singleton_attrs=_singleton_attrs,
    )


# ─── Author Response Parsing ────────────────────────────────────────────────


def parse_author_response(
    raw: str,
    groups: list[ReviewGroup],
    log_fn=None,
) -> list[AuthorResponse]:
    """Parse the author's round 1 response into AuthorResponse objects."""
    responses = []
    matched_group_ids: set[str] = set()
    id_attempts = 0
    id_failures = 0

    # Split by RESPONSE TO GROUP headers
    blocks = re.split(r'(?=RESPONSE\s+TO\s+GROUP)', raw, flags=re.IGNORECASE)

    for block in blocks:
        if not block.strip():
            continue

        # Extract group ID
        gid_match = re.search(
            r'RESPONSE\s+TO\s+GROUP\s+\[?([^\]\n:]+)\]?\s*:?',
            block, re.IGNORECASE,
        )
        if not gid_match:
            continue

        raw_gid = gid_match.group(1).strip()
        id_attempts += 1
        matched_gid = resolve_guid(raw_gid, groups, log_fn=log_fn, silent=True)

        if not matched_gid:
            # Positional fallback: author writes "RESPONSE TO GROUP N [uuid]" -- use N
            num_match = re.match(r'^(\d+)', raw_gid)
            if num_match:
                seq = int(num_match.group(1))
                if 1 <= seq <= len(groups):
                    candidate = groups[seq - 1].group_id
                    if candidate not in matched_group_ids:
                        matched_gid = candidate
                        if log_fn:
                            log_fn(f"  ID match: positional fallback {seq} -> '{matched_gid}'")

        if not matched_gid:
            id_failures += 1
            continue

        matched_group_ids.add(matched_gid)

        resolution = _extract_multiline_field(block, "RESOLUTION", ["RATIONALE", "DIFF"])
        rationale = _extract_multiline_field(
            block, "RATIONALE",
            ["RESPONSE TO GROUP", "RESOLUTION", "=== UNIFIED DIFF", "=== REVISED PLAN"],
        )

        res_val = resolution.strip().upper()
        if "ACCEPT" in res_val and "PARTIAL" not in res_val:
            res_val = "ACCEPTED"
        elif "REJECT" in res_val:
            res_val = "REJECTED"
        elif "PARTIAL" in res_val:
            res_val = "PARTIAL"
        else:
            res_val = "UNKNOWN"  # Unrecognized -- governance will escalate

        responses.append(AuthorResponse(
            group_id=matched_gid,
            resolution=res_val,
            rationale=rationale.strip(),
        ))

    if id_failures and log_fn:
        log_fn(f"  ID mapping: {id_failures} of {id_attempts} unmatched for author — recovered gracefully")

    return responses


# ─── Dedup Response Parsing ─────────────────────────────────────────────────


def parse_dedup_response(
    raw: str,
    all_points: list[ReviewPoint],
    ctx: ReviewContext,
) -> list[ReviewGroup]:
    """Parse deduplication model response into ReviewGroup objects.

    Assigns final group and point IDs using ReviewContext.
    Each point is assigned to at most one group (first match wins).
    """

    def _extract(block: str) -> dict | None:
        concern = _extract_multiline_field(block, "CONCERN", ["POINTS", "COMBINED_SEVERITY", "COMBINED_CATEGORY"])
        points_str = _extract_multiline_field(block, "POINTS", ["COMBINED_SEVERITY", "COMBINED_CATEGORY", "GROUP"])
        if not concern and not points_str:
            return None
        severity = _extract_multiline_field(block, "COMBINED_SEVERITY", ["COMBINED_CATEGORY", "GROUP"])
        category = _extract_multiline_field(block, "COMBINED_CATEGORY", ["GROUP"])
        return {
            "concern": concern,
            "points_str": points_str,
            "concern_text": concern,
            "severity": severity,
            "category": category,
        }

    def _attrs(fields: dict, found_points: list[ReviewPoint]) -> dict:
        severity_raw = fields["severity"]
        category_raw = fields["category"]
        return {
            "concern": fields["concern"] or found_points[0].description,
            "combined_severity": _normalize_severity(severity_raw) if severity_raw else found_points[0].severity,
            "combined_category": _normalize_category(category_raw) if category_raw else found_points[0].category,
        }

    def _singleton_attrs(p: ReviewPoint) -> dict:
        return {"combined_severity": p.severity, "combined_category": p.category}

    return _parse_grouped_response(
        raw, all_points, ctx,
        extract_fields=_extract,
        point_ref_pattern=r'(?:POINT\s+)?(\d+)',
        build_group_attrs=_attrs,
        build_singleton_attrs=_singleton_attrs,
    )


# ─── Revised Output Extraction ──────────────────────────────────────────────


def extract_revised_output(raw: str, mode: str) -> str:
    """Extract revised plan, unified diff, remediation plan, or spec suggestions from response."""
    if mode == "spec":
        m = re.search(
            r'=== SPEC SUGGESTIONS ===(.*?)=== END SPEC SUGGESTIONS ===',
            raw, re.DOTALL,
        )
    elif mode == "plan":
        m = re.search(
            r'=== REVISED PLAN ===(.*?)=== END REVISED PLAN ===',
            raw, re.DOTALL,
        )
    elif mode == "integration":
        m = re.search(
            r'=== REMEDIATION PLAN ===(.*?)=== END REMEDIATION PLAN ===',
            raw, re.DOTALL,
        )
    else:
        m = re.search(
            r'=== UNIFIED DIFF ===(.*?)=== END UNIFIED DIFF ===',
            raw, re.DOTALL,
        )
    return m.group(1).strip() if m else ""


# ─── Rebuttal Response Parsing ──────────────────────────────────────────────


def parse_rebuttal_response(
    raw: str,
    reviewer_name: str,
    groups: list[ReviewGroup],
    log_fn=None,
) -> list[RebuttalResponse]:
    """Parse reviewer rebuttal into RebuttalResponse objects."""
    responses: list[RebuttalResponse] = []
    id_attempts = 0
    id_failures = 0
    blocks = re.split(r'(?=REBUTTAL\s+TO\s+GROUP)', raw, flags=re.IGNORECASE)

    for block in blocks:
        if not block.strip():
            continue

        gid_match = re.search(
            r'REBUTTAL\s+TO\s+GROUP\s+\[?([^\]\n:]+)\]?\s*:?',
            block, re.IGNORECASE,
        )
        if not gid_match:
            continue

        raw_gid = gid_match.group(1).strip()
        id_attempts += 1
        matched_gid = resolve_guid(raw_gid, groups, log_fn=log_fn, silent=True)
        if not matched_gid:
            id_failures += 1
            continue

        verdict = _extract_multiline_field(block, "VERDICT", ["RATIONALE"])
        rationale = _extract_multiline_field(
            block, "RATIONALE",
            ["REBUTTAL TO GROUP", "VERDICT"],
        )

        verdict_val = verdict.strip().upper()
        if "CHALLENGE" in verdict_val:
            verdict_val = "CHALLENGE"
        elif "CONCUR" in verdict_val:
            verdict_val = "CONCUR"
        else:
            verdict_val = "CONCUR"  # Default if unparseable

        responses.append(RebuttalResponse(
            group_id=matched_gid,
            reviewer=reviewer_name,
            verdict=verdict_val,
            rationale=rationale.strip(),
        ))

    if id_failures and log_fn:
        log_fn(f"  ID mapping: {id_failures} of {id_attempts} unmatched for {reviewer_name} rebuttal — recovered gracefully")

    return responses


# ─── Author Final Response Parsing ──────────────────────────────────────────


def parse_author_final_response(
    raw: str,
    groups: list[ReviewGroup],
    log_fn=None,
) -> list[AuthorFinalResponse]:
    """Parse the author's final response to challenged groups."""
    responses: list[AuthorFinalResponse] = []
    id_attempts = 0
    id_failures = 0
    blocks = re.split(r'(?=FINAL\s+RESPONSE\s+TO\s+GROUP)', raw, flags=re.IGNORECASE)

    for block in blocks:
        if not block.strip():
            continue

        gid_match = re.search(
            r'FINAL\s+RESPONSE\s+TO\s+GROUP\s+\[?([^\]\n:]+)\]?\s*:?',
            block, re.IGNORECASE,
        )
        if not gid_match:
            continue

        raw_gid = gid_match.group(1).strip()
        id_attempts += 1
        matched_gid = resolve_guid(raw_gid, groups, log_fn=log_fn, silent=True)
        if not matched_gid:
            id_failures += 1
            continue

        resolution = _extract_multiline_field(
            block, "RESOLUTION",
            ["RATIONALE", "FINAL RESPONSE TO GROUP"],
        )
        rationale = _extract_multiline_field(
            block, "RATIONALE",
            ["FINAL RESPONSE TO GROUP", "RESOLUTION",
             "=== UNIFIED DIFF", "=== REVISED PLAN"],
        )

        res_val = resolution.strip().upper()
        if "MAINTAIN" in res_val:
            res_val = "MAINTAINED"
        elif "ACCEPT" in res_val and "PARTIAL" not in res_val:
            res_val = "ACCEPTED"
        elif "REJECT" in res_val:
            res_val = "REJECTED"
        elif "PARTIAL" in res_val:
            res_val = "PARTIAL"
        else:
            res_val = "MAINTAINED"  # Default: author holds position

        responses.append(AuthorFinalResponse(
            group_id=matched_gid,
            resolution=res_val,
            rationale=rationale.strip(),
        ))

    if id_failures and log_fn:
        log_fn(f"  ID mapping: {id_failures} of {id_attempts} unmatched for author final — recovered gracefully")

    return responses
