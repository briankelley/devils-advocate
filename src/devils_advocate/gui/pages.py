"""HTML-serving routes: dashboard, review detail, new review, config."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ..storage import StorageManager

router = APIRouter()

# Simple TTL cache for review list
_review_cache: dict = {"data": None, "expires": 0}
_CACHE_TTL = 5  # seconds


def _get_gui_storage() -> StorageManager:
    """Instantiate a read-oriented storage with a stable project_dir."""
    return StorageManager(Path.home())


def _list_reviews_cached() -> list[dict]:
    """List reviews with a short TTL cache to avoid re-reading on rapid refresh."""
    now = time.time()
    if _review_cache["data"] is not None and now < _review_cache["expires"]:
        return _review_cache["data"]
    storage = _get_gui_storage()
    reviews = storage.list_reviews()
    _review_cache["data"] = reviews
    _review_cache["expires"] = now + _CACHE_TTL
    return reviews


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, page: int = 1):
    """Dashboard — list all reviews with server-side pagination."""
    reviews = await asyncio.to_thread(_list_reviews_cached)
    # Sort newest first
    reviews = sorted(reviews, key=lambda r: r.get("timestamp", ""), reverse=True)

    per_page = 25
    total = len(reviews)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    page_reviews = reviews[start:start + per_page]

    templates = request.app.state.templates
    return templates.TemplateResponse(request, "dashboard.html", {
        "reviews": page_reviews,
        "page": page,
        "total_pages": total_pages,
        "total": total,
        "csrf_token": request.app.state.csrf_token,
    })


@router.get("/review/new", response_class=HTMLResponse)
async def new_review_form(request: Request):
    """New review form page."""
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "new_review.html", {
        "csrf_token": request.app.state.csrf_token,
    })


@router.get("/review/{review_id}", response_class=HTMLResponse)
async def review_detail(request: Request, review_id: str):
    """Review detail page — shows progress if running, full detail if complete."""
    runner = request.app.state.runner
    status = runner.get_status(review_id)

    if status == "running":
        templates = request.app.state.templates
        return templates.TemplateResponse("review_detail.html", {
            "request": request,
            "review_id": review_id,
            "status": "running",
            "ledger": None,
            "csrf_token": request.app.state.csrf_token,
        })

    # Load from storage
    storage = _get_gui_storage()
    ledger = await asyncio.to_thread(storage.load_review, review_id)

    if ledger is None:
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/", status_code=302)

    # Group points by resolution for display
    points = ledger.get("points", [])
    escalated = []
    auto_accepted = []
    auto_dismissed = []
    overridden = []

    # Group by group_id
    groups: dict[str, list[dict]] = {}
    for p in points:
        gid = p.get("group_id", p.get("point_id", "unknown"))
        groups.setdefault(gid, []).append(p)

    for gid, group_points in groups.items():
        # Use first point's final_resolution as group resolution
        resolution = group_points[0].get("final_resolution", "pending")
        group_info = {
            "group_id": gid,
            "points": group_points,
            "resolution": resolution,
            "severity": group_points[0].get("severity", "medium"),
            "category": group_points[0].get("category", "other"),
            "concern": group_points[0].get("concern", group_points[0].get("description", "")),
            "source_reviewers": group_points[0].get("source_reviewers", []),
            "author_resolution": group_points[0].get("author_resolution", ""),
            "author_rationale": group_points[0].get("author_rationale", ""),
            "rebuttals": group_points[0].get("rebuttals", []),
            "author_final_resolution": group_points[0].get("author_final_resolution", ""),
            "author_final_rationale": group_points[0].get("author_final_rationale", ""),
            "governance_resolution": group_points[0].get("governance_resolution", ""),
            "governance_reason": group_points[0].get("governance_reason", ""),
        }

        if resolution == "escalated":
            escalated.append(group_info)
        elif resolution == "auto_accepted" or resolution == "accepted":
            auto_accepted.append(group_info)
        elif resolution == "auto_dismissed":
            auto_dismissed.append(group_info)
        elif resolution == "overridden":
            overridden.append(group_info)
        else:
            escalated.append(group_info)

    # Check for revised artifacts
    review_dir = storage.reviews_dir / review_id
    has_revised = (review_dir / "revised-plan.md").exists() or \
                  (review_dir / "revised-diff.patch").exists() or \
                  (review_dir / "remediation-plan.md").exists()
    has_original = (review_dir / "original_content.txt").exists()
    has_report = (review_dir / "dvad-report.md").exists()

    templates = request.app.state.templates
    return templates.TemplateResponse(request, "review_detail.html", {
        "review_id": review_id,
        "status": status or "complete",
        "ledger": ledger,
        "escalated": escalated,
        "auto_accepted": auto_accepted,
        "auto_dismissed": auto_dismissed,
        "overridden": overridden,
        "has_revised": has_revised,
        "has_original": has_original,
        "has_report": has_report,
        "csrf_token": request.app.state.csrf_token,
    })


@router.get("/config", response_class=HTMLResponse)
async def config_page(request: Request):
    """Config editor page."""
    from ..config import find_config, load_config, validate_config

    config_path = request.app.state.config_path
    try:
        config = await asyncio.to_thread(
            load_config, Path(config_path) if config_path else None
        )
        config_file = config.get("config_path", "")
        raw_yaml = await asyncio.to_thread(Path(config_file).read_text)
        issues = validate_config(config)

        all_models = config.get("all_models", config.get("models", {}))
        model_names = sorted(all_models.keys())

        # Extract current roles
        raw = await asyncio.to_thread(_load_raw_yaml, config_file)
        roles_block = raw.get("roles", {})
    except Exception as exc:
        config = None
        config_file = ""
        raw_yaml = ""
        issues = [("error", str(exc))]
        model_names = []
        roles_block = {}

    templates = request.app.state.templates
    return templates.TemplateResponse(request, "config.html", {
        "config": config,
        "config_file": config_file,
        "raw_yaml": raw_yaml,
        "issues": issues,
        "model_names": model_names,
        "roles": roles_block,
        "all_models": config.get("all_models", {}) if config else {},
        "csrf_token": request.app.state.csrf_token,
    })


def _load_raw_yaml(path: str) -> dict:
    """Load raw YAML as dict (for roles block extraction)."""
    import yaml
    with open(path) as f:
        return yaml.safe_load(f) or {}
