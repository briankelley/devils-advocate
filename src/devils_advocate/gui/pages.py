"""HTML-serving routes: dashboard, review detail, new review, config."""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from ..storage import StorageManager
from ._helpers import get_gui_storage

router = APIRouter()


def _find_dvad_binary() -> str:
    """Find the dvad binary, falling back to the venv bin dir."""
    found = shutil.which("dvad")
    if found:
        return found
    # Fallback: binary should be alongside the running Python interpreter
    candidate = Path(sys.executable).parent / "dvad"
    if candidate.is_file():
        return str(candidate)
    return "(not found in PATH)"


def _build_role_display_entries(config_path: str | None) -> tuple[list[dict], dict]:
    """Build role assignments from raw YAML — single source of truth for both pages.

    Returns (role_assignments list, model_thinking dict).
    """
    from ..config import load_config, find_config
    import yaml

    resolved = Path(config_path) if config_path else find_config()
    config = load_config(resolved)

    with open(str(resolved)) as f:
        raw = yaml.safe_load(f) or {}

    roles = raw.get("roles", {})
    all_models = config.get("all_models", config.get("models", {}))

    # Build model thinking map
    model_thinking = {}
    for name, m in all_models.items():
        model_thinking[name] = bool(getattr(m, "thinking", False))

    reviewers = roles.get("reviewers", [])

    def _entry(label, icon, model_name):
        return {
            "label": label,
            "icon": icon,
            "model": model_name or None,
            "thinking": model_thinking.get(model_name, False) if model_name else False,
        }

    role_assignments = [
        _entry("Author", "pen-tool", roles.get("author")),
        _entry("Reviewer 1", "scan-eye", reviewers[0] if len(reviewers) > 0 else None),
        _entry("Reviewer 2", "scan-eye", reviewers[1] if len(reviewers) > 1 else None),
        _entry("Dedup", "combine", roles.get("deduplication")),
        _entry("Normalization", "scale", roles.get("normalization")),
        _entry("Revision", "file-pen", roles.get("revision")),
        _entry("Integration", "puzzle", roles.get("integration_reviewer")),
    ]

    return role_assignments, model_thinking


# Simple TTL cache for review list
_review_cache: dict = {"data": None, "expires": 0}
_CACHE_TTL = 5  # seconds


def _invalidate_review_cache() -> None:
    """Clear the review list cache."""
    _review_cache["data"] = None


def _list_reviews_cached() -> list[dict]:
    """List reviews with a short TTL cache to avoid re-reading on rapid refresh."""
    now = time.time()
    if _review_cache["data"] is not None and now < _review_cache["expires"]:
        return _review_cache["data"]
    storage = get_gui_storage()
    reviews = storage.list_reviews()
    _review_cache["data"] = reviews
    _review_cache["expires"] = now + _CACHE_TTL
    return reviews


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, page: int = 1, show_test: bool = False):
    """Dashboard — list all reviews with server-side pagination."""
    reviews = await asyncio.to_thread(_list_reviews_cached)
    # Sort newest first
    reviews = sorted(reviews, key=lambda r: r.get("timestamp", ""), reverse=True)

    if not show_test:
        reviews = [r for r in reviews if "test" not in r.get("project", "").lower()]

    per_page = 25
    total = len(reviews)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    page_reviews = reviews[start:start + per_page]

    dvad_binary = _find_dvad_binary()

    # Load role assignments for display — same routine as config page
    role_assignments = []
    mode_readiness = {}
    has_config_errors = False
    config_error_summary = ""
    try:
        from ..config import load_config, get_config_health, get_mode_readiness
        config_path = request.app.state.config_path
        config = load_config(Path(config_path) if config_path else None)

        has_config_errors, config_error_summary = get_config_health(config)
        mode_readiness = get_mode_readiness(config)

        role_assignments, _ = await asyncio.to_thread(
            _build_role_display_entries, request.app.state.config_path
        )
    except Exception:
        has_config_errors = True
        config_error_summary = "Configuration could not be loaded"

    templates = request.app.state.templates
    return templates.TemplateResponse(request, "dashboard.html", {
        "reviews": page_reviews,
        "page": page,
        "total_pages": total_pages,
        "total": total,
        "show_test": show_test,
        "dvad_binary": dvad_binary,
        "role_assignments": role_assignments,
        "has_config_errors": has_config_errors,
        "config_error_summary": config_error_summary,
        "mode_readiness": mode_readiness,
        "csrf_token": request.app.state.csrf_token,
    })


@router.get("/review/new")
async def new_review_redirect():
    """Redirect legacy new review URL to dashboard."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/", status_code=302)


def _group_points_by_resolution(points: list[dict]) -> tuple[list, list, list, list]:
    """Group review points by resolution status."""
    escalated = []
    auto_accepted = []
    auto_dismissed = []
    overridden = []

    groups: dict[str, list[dict]] = {}
    for p in points:
        gid = p.get("group_id", p.get("point_id", "unknown"))
        groups.setdefault(gid, []).append(p)

    for gid, group_points in groups.items():
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

    escalated.sort(key=lambda g: len(g.get("source_reviewers", [])), reverse=True)
    auto_accepted.sort(key=lambda g: len(g.get("source_reviewers", [])), reverse=True)

    return escalated, auto_accepted, auto_dismissed, overridden


def _compute_elapsed_time(log_path: Path) -> str | None:
    """Compute elapsed time from first/last log timestamps."""
    if not log_path.exists():
        return None
    try:
        from datetime import datetime as _dt
        log_text = log_path.read_text()
        lines = [l for l in log_text.strip().splitlines() if l.startswith("[")]
        if len(lines) >= 2:
            first_ts = lines[0].split("]")[0].lstrip("[")
            last_ts = lines[-1].split("]")[0].lstrip("[")
            t0 = _dt.fromisoformat(first_ts.replace("Z", "+00:00"))
            t1 = _dt.fromisoformat(last_ts.replace("Z", "+00:00"))
            delta = t1 - t0
            total_secs = int(delta.total_seconds())
            if total_secs >= 60:
                return f"{total_secs // 60}m {total_secs % 60}s"
            else:
                return f"{total_secs}s"
    except Exception:
        pass
    return None


def _build_role_cost_rows(ledger: dict, normalization_model: str, revision_model: str) -> list[tuple[str, str, float]]:
    """Build per-role cost rows for the completed cost table."""
    role_costs = ledger.get("cost", {}).get("role_costs", {})
    role_cost_rows: list[tuple[str, str, float]] = []
    result_type = ledger.get("result", "success")
    no_cost_results = ("dry_run", "cost_exceeded", "cost_aborted", "failed")

    if result_type in no_cost_results:
        ra = ledger.get("role_assignments", {})
        if ra:
            if ra.get("author"):
                role_cost_rows.append(("author", ra["author"], 0.0))
            for i, rv in enumerate(ra.get("reviewers", []), 1):
                label = f"reviewer {i}" if len(ra.get("reviewers", [])) > 1 else "reviewer"
                role_cost_rows.append((label, rv, 0.0))
            if ra.get("dedup"):
                role_cost_rows.append(("dedup", ra["dedup"], 0.0))
            if ra.get("normalization"):
                role_cost_rows.append(("normalization", ra["normalization"], 0.0))
            if ra.get("revision"):
                role_cost_rows.append(("revision", ra["revision"], 0.0))
            if ra.get("integration"):
                role_cost_rows.append(("integration", ra["integration"], 0.0))
    else:
        if ledger.get("author_model"):
            role_cost_rows.append(("author", ledger["author_model"], role_costs.get("author", 0.0)))
        reviewer_models = ledger.get("reviewer_models", [])
        for i, rv in enumerate(reviewer_models, 1):
            role_key = f"reviewer_{i}"
            label = f"reviewer {i}" if len(reviewer_models) > 1 else "reviewer"
            role_cost_rows.append((label, rv, role_costs.get(role_key, 0.0)))
        if ledger.get("dedup_model"):
            role_cost_rows.append(("dedup", ledger["dedup_model"], role_costs.get("dedup", 0.0)))
        if normalization_model != "\u2014":
            role_cost_rows.append(("normalization", normalization_model, role_costs.get("normalization", 0.0)))
        if revision_model != "\u2014" and role_costs.get("revision", 0.0) > 0:
            role_cost_rows.append(("revision", revision_model, role_costs.get("revision", 0.0)))

    return role_cost_rows


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
    storage = get_gui_storage()
    ledger = await asyncio.to_thread(storage.load_review, review_id)

    if ledger is None:
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/", status_code=302)

    # Group points by resolution for display
    points = ledger.get("points", [])
    escalated, auto_accepted, auto_dismissed, overridden = _group_points_by_resolution(points)

    # Load file manifest
    review_dir = storage.reviews_dir / review_id
    manifest_path = review_dir / "input_files_manifest.json"
    input_files_manifest = []
    if manifest_path.exists():
        try:
            input_files_manifest = json.loads(manifest_path.read_text()).get("files", [])
        except Exception:
            pass

    # Check for revised artifacts
    revised_artifact_names = [
        "revised-plan.md",
        "remediation-plan.md", "revised-spec-suggestions.md",
    ]
    revised_path = None
    for name in revised_artifact_names:
        candidate = review_dir / name
        if candidate.exists():
            revised_path = candidate
            break
    # Code mode: look for revised-{original_name} (e.g. revised-orchestrator.py)
    if revised_path is None:
        for candidate in sorted(review_dir.glob("revised-*")):
            if candidate.name == "revised-diff.patch":
                continue
            if candidate.is_file():
                revised_path = candidate
                break
    has_revised = revised_path is not None
    has_diff = (review_dir / "revised-diff.patch").exists()
    has_original = (review_dir / "original_content.txt").exists()
    has_report = (review_dir / "dvad-report.md").exists()

    # Detect stale revision: overrides newer than the revised artifact
    revision_stale = False
    if has_revised and revised_path is not None:
        revised_mtime = revised_path.stat().st_mtime
        for point in points:
            for ovr in point.get("overrides", []):
                try:
                    from datetime import datetime, timezone
                    ovr_ts = datetime.fromisoformat(ovr["timestamp"])
                    if ovr_ts.timestamp() > revised_mtime:
                        revision_stale = True
                        break
                except (KeyError, ValueError):
                    continue
            if revision_stale:
                break

    # Elapsed time from log file timestamps
    log_path = storage.data_dir / "logs" / f"{review_id}.log"
    elapsed_str = _compute_elapsed_time(log_path)

    # Cost breakdown for tooltip
    cost_breakdown = ledger.get("cost", {}).get("breakdown", {})

    # Whether any overrides have been applied
    has_overrides = len(overridden) > 0

    # Resolve normalization and revision model names from config
    try:
        from ..config import load_config, get_models_by_role
        config_path = request.app.state.config_path
        config = load_config(Path(config_path) if config_path else None)
        roles = get_models_by_role(config)
        normalization_model = roles["normalization"].name if roles.get("normalization") else "\u2014"
        revision_model = roles["revision"].name if roles.get("revision") else "\u2014"
    except Exception:
        normalization_model = "\u2014"
        revision_model = "\u2014"

    # Build per-role cost rows for the completed cost table
    role_cost_rows = _build_role_cost_rows(ledger, normalization_model, revision_model)
    total_cost = ledger.get("cost", {}).get("total_usd", 0.0)

    # Cost estimate rows (for dry_run details page)
    cost_estimate_rows = ledger.get("cost_estimate_rows", [])

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
        "has_diff": has_diff,
        "revision_stale": revision_stale,
        "has_original": has_original,
        "has_report": has_report,
        "cost_breakdown": cost_breakdown,
        "has_overrides": has_overrides,
        "normalization_model": normalization_model,
        "revision_model": revision_model,
        "review_mode": ledger.get("mode", "plan"),
        "role_cost_rows": role_cost_rows,
        "total_cost": total_cost,
        "input_files_manifest": input_files_manifest,
        "cost_estimate_rows": cost_estimate_rows,
        "elapsed_str": elapsed_str,
        "csrf_token": request.app.state.csrf_token,
    })


_VENDOR_PATTERNS: list[tuple[str, str]] = [
    ("api.openai.com", "OpenAI"),
    ("api.x.ai", "xAI"),
    ("generativelanguage.googleapis.com", "Google"),
    ("api.deepseek.com", "DeepSeek"),
    ("api.moonshot.ai", "Moonshot"),
    ("api.minimax.io", "MiniMax"),
]


def _infer_vendor(model) -> str:
    """Derive a display-friendly vendor name from model metadata."""
    provider = getattr(model, "provider", "unknown")

    if provider == "anthropic":
        return "Anthropic"
    if provider == "minimax":
        return "MiniMax"

    api_base = (getattr(model, "api_base", "") or "").lower()
    for pattern, vendor in _VENDOR_PATTERNS:
        if pattern in api_base:
            return vendor

    return provider.title()


@router.get("/config", response_class=HTMLResponse)
async def config_page(request: Request):
    """Config editor page."""
    from ..config import find_config, load_config, validate_config_structure

    config_path = request.app.state.config_path
    try:
        config = await asyncio.to_thread(
            load_config, Path(config_path) if config_path else None
        )
        config_file = config.get("config_path", "")
        raw_yaml = await asyncio.to_thread(Path(config_file).read_text)
        issues = validate_config_structure(config)

        all_models = config.get("all_models", config.get("models", {}))
        model_names = sorted(all_models.keys())

        # Extract current roles and thinking — shared routine with dashboard
        raw = await asyncio.to_thread(_load_raw_yaml, config_file)
        roles_block = raw.get("roles", {})
        settings_block = raw.get("settings", {})
        role_assignments, model_thinking = await asyncio.to_thread(
            _build_role_display_entries, request.app.state.config_path
        )

        # Build initial_role_state for JS pendingState initialization
        reviewers_list = [r for r in roles_block.get("reviewers", []) if r]
        initial_role_state = {
            "roles": {
                "author": roles_block.get("author") or None,
                "reviewer1": reviewers_list[0] if len(reviewers_list) > 0 else None,
                "reviewer2": reviewers_list[1] if len(reviewers_list) > 1 else None,
                "dedup": roles_block.get("deduplication") or None,
                "normalization": roles_block.get("normalization") or None,
                "revision": roles_block.get("revision") or None,
                "integration": roles_block.get("integration_reviewer") or None,
            },
            "thinking": {k: bool(v) for k, v in model_thinking.items()},
        }

        # Group models by vendor (derived from api_base or provider)
        models_by_provider: dict[str, list[tuple[str, object]]] = {}
        for name, m in all_models.items():
            vendor = _infer_vendor(m)
            models_by_provider.setdefault(vendor, []).append((name, m))
        for provider in models_by_provider:
            models_by_provider[provider].sort(
                key=lambda x: getattr(x[1], "cost_per_1k_output", 0) or 0,
                reverse=True,
            )
        models_by_provider = dict(sorted(models_by_provider.items()))

        # Flat alphabetical list for collapsible card layout
        sorted_models = sorted(all_models.items(), key=lambda x: x[0].lower())

        # .env file path
        env_file_path = str(Path(config_file).parent / ".env")
        env_file_exists = Path(env_file_path).is_file()

        # dvad binary path
        dvad_binary = _find_dvad_binary()

        # Directory paths
        storage = StorageManager(Path.home())
        data_dir = str(storage.data_dir)
        reviews_dir = str(storage.reviews_dir)
        logs_dir = str(storage.logs_dir)

        import importlib.resources
        templates_dir = str(importlib.resources.files("devils_advocate") / "templates")

        # Model vendors map (used by config template)
        model_vendors = {}
        for name, m in all_models.items():
            model_vendors[name] = _infer_vendor(m)

    except Exception as exc:
        import logging
        logging.getLogger("devils_advocate.gui").exception("Config page failed to load")
        config = None
        config_file = ""
        raw_yaml = ""
        issues = [("error", str(exc))]
        model_names = []
        roles_block = {}
        settings_block = {}
        models_by_provider = {}
        sorted_models = []
        env_file_path = ""
        env_file_exists = False
        dvad_binary = "(not found in PATH)"
        data_dir = ""
        reviews_dir = ""
        logs_dir = ""
        templates_dir = ""
        model_vendors = {}
        model_thinking = {}
        role_assignments = []
        initial_role_state = {"roles": {}, "thinking": {}}

    templates = request.app.state.templates
    return templates.TemplateResponse(request, "config.html", {
        "config": config,
        "config_file": config_file,
        "raw_yaml": raw_yaml,
        "issues": issues,
        "model_names": model_names,
        "roles": roles_block,
        "all_models": config.get("all_models", {}) if config else {},
        "models_by_provider": models_by_provider,
        "sorted_models": sorted_models,
        "env_file_path": env_file_path,
        "env_file_exists": env_file_exists,
        "dvad_binary": dvad_binary,
        "data_dir": data_dir,
        "reviews_dir": reviews_dir,
        "logs_dir": logs_dir,
        "templates_dir": templates_dir,
        "model_vendors": model_vendors,
        "model_thinking": model_thinking,
        "role_assignments": role_assignments,
        "initial_role_state": initial_role_state,
        "settings": settings_block,
        "csrf_token": request.app.state.csrf_token,
    })


def _load_raw_yaml(path: str) -> dict:
    """Load raw YAML as dict (for roles block extraction)."""
    import yaml
    with open(path) as f:
        return yaml.safe_load(f) or {}
