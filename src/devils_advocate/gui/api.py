"""JSON + SSE API routes."""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from io import StringIO
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import JSONResponse, StreamingResponse, FileResponse
from ruamel.yaml import YAML

from ..storage import StorageManager
from ._helpers import get_gui_storage
from .progress import ProgressEvent

router = APIRouter()

# Limits
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
MAX_FILES = 25


def _get_git_info(filepath: Path) -> dict:
    """Get git commit hash for a file, or 'not tracked'."""
    import subprocess
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%H", "--", str(filepath)],
            capture_output=True, text=True, timeout=5,
            cwd=str(filepath.parent),
        )
        if result.returncode == 0 and result.stdout.strip():
            return {"git_hash": result.stdout.strip()[:12], "git_status": "tracked"}
    except Exception:
        pass
    return {"git_hash": None, "git_status": "not tracked"}


def _check_csrf(request: Request) -> None:
    """Validate CSRF token on mutating requests."""
    expected = request.app.state.csrf_token
    got = request.headers.get("X-DVAD-Token", "")
    if got != expected:
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token")


async def _mutate_yaml_config(request: Request, mutator: Callable[[dict], None]) -> None:
    """Load YAML config, apply a mutation, and atomically save.

    The mutator receives the parsed YAML dict and modifies it in place.
    It may raise HTTPException for validation errors.
    """
    from ..config import find_config

    config_path_str = request.app.state.config_path
    if config_path_str:
        target = Path(config_path_str)
    else:
        try:
            target = find_config()
        except Exception:
            raise HTTPException(status_code=400, detail="Cannot determine config file path")

    try:
        yaml = YAML()
        yaml.preserve_quotes = True
        data = yaml.load(target.read_text())

        mutator(data)

        stream = StringIO()
        yaml.dump(data, stream)
        await asyncio.to_thread(StorageManager._atomic_write, target, stream.getvalue())
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to update config: {exc}")


# ── Review Start ─────────────────────────────────────────────────────────────

@router.post("/review/start")
async def start_review(request: Request):
    """Start a new review. Returns {review_id}. 409 if one is already running."""
    _check_csrf(request)

    form = await request.form()
    mode = form.get("mode", "plan")
    project = form.get("project", "").strip()
    max_cost_str = form.get("max_cost", "")
    dry_run = form.get("dry_run") == "on"
    spec_file_upload = form.get("spec_file")
    project_dir_str = form.get("project_dir", "").strip()

    if not project:
        raise HTTPException(status_code=400, detail="Project name is required")

    if mode not in ("plan", "code", "integration", "spec"):
        raise HTTPException(status_code=400, detail=f"Invalid mode: {mode}")

    max_cost = None
    if max_cost_str:
        try:
            max_cost = float(max_cost_str)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid max_cost value")

    # Handle file uploads — save to temp dir
    tmpdir = tempfile.mkdtemp(prefix="dvad-gui-")
    input_files: list[Path] = []

    # Get uploaded input files
    uploaded_files = form.getlist("input_files")
    file_count = 0

    for upload in uploaded_files:
        if not hasattr(upload, 'filename') or not upload.filename:
            continue
        file_count += 1
        if file_count > MAX_FILES:
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise HTTPException(status_code=400, detail=f"Too many files (max {MAX_FILES})")

        safe_name = Path(upload.filename).name
        dest = Path(tmpdir) / safe_name
        content = await upload.read()
        if len(content) > MAX_FILE_SIZE:
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise HTTPException(
                status_code=400,
                detail=f"File '{safe_name}' exceeds {MAX_FILE_SIZE // (1024*1024)}MB limit",
            )
        dest.write_bytes(content)
        input_files.append(dest)

    # Mode-aware validation
    if mode in ("plan", "spec") and not input_files:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=f"{mode.title()} mode requires at least one input file")
    if mode == "code":
        if len(input_files) != 1:
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise HTTPException(status_code=400, detail="Code mode requires exactly one input file")

    # Handle spec file
    spec_path = None
    if spec_file_upload and hasattr(spec_file_upload, 'filename') and spec_file_upload.filename:
        safe_name = Path(spec_file_upload.filename).name
        spec_dest = Path(tmpdir) / f"_spec_{safe_name}"
        content = await spec_file_upload.read()
        if len(content) > MAX_FILE_SIZE:
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise HTTPException(status_code=400, detail="Spec file too large")
        spec_dest.write_bytes(content)
        spec_path = spec_dest

    project_dir = Path(project_dir_str) if project_dir_str else None

    # Build input files manifest
    manifest = {"files": []}
    for f in input_files:
        entry = {
            "original_path": str(f),
            "filename": f.name,
            "type": "plan" if mode in ("plan", "spec") else "code",
            "size_bytes": f.stat().st_size,
            "copied": mode in ("plan", "spec"),
        }
        entry.update(_get_git_info(f))
        manifest["files"].append(entry)

    if spec_path:
        entry = {
            "original_path": str(spec_path),
            "filename": spec_path.name,
            "type": "spec",
            "size_bytes": spec_path.stat().st_size,
            "copied": True,
        }
        entry.update(_get_git_info(spec_path))
        manifest["files"].append(entry)

    runner = request.app.state.runner
    config_path = request.app.state.config_path

    review_id = await runner.start_review(
        mode=mode,
        input_files=input_files,
        project=project,
        config_path=config_path,
        spec_file=spec_path,
        project_dir=project_dir,
        max_cost=max_cost,
        dry_run=dry_run,
        file_manifest=manifest,
    )

    return JSONResponse({"review_id": review_id})


# ── SSE Progress ─────────────────────────────────────────────────────────────

@router.get("/review/{review_id}/progress")
async def review_progress(request: Request, review_id: str):
    """SSE stream of progress events for a running review."""
    runner = request.app.state.runner

    async def event_stream():
        # Send buffered events first (for late-connecting clients)
        for ev in runner.get_buffered_events(review_id):
            yield f"data: {json.dumps(ev)}\n\n"

        queue = runner.get_queue(review_id)
        if queue is None:
            # Review not active — send status
            status = runner.get_status(review_id)
            terminal = {
                "type": "complete" if status == "complete" else "error",
                "message": f"Review {status}",
                "phase": "done",
                "detail": {},
                "timestamp": "",
            }
            yield f"data: {json.dumps(terminal)}\n\n"
            return

        idle_count = 0
        while True:
            try:
                ev = await asyncio.wait_for(queue.get(), timeout=15.0)
                idle_count = 0
                yield f"data: {json.dumps(ev)}\n\n"
                # Terminal event — stop streaming
                if ev.get("type") in ("complete", "error"):
                    return
            except asyncio.TimeoutError:
                # Keepalive ping
                yield ": ping\n\n"
                idle_count += 1
                # Check if review is still running
                status = runner.get_status(review_id)
                if status in ("complete", "failed"):
                    terminal = {
                        "type": "complete" if status == "complete" else "error",
                        "message": f"Review {status}",
                        "phase": "done",
                        "detail": {},
                        "timestamp": "",
                    }
                    yield f"data: {json.dumps(terminal)}\n\n"
                    return
            except asyncio.CancelledError:
                return

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ── Review Data ──────────────────────────────────────────────────────────────

@router.get("/review/{review_id}")
async def get_review_json(request: Request, review_id: str):
    """Return review ledger as JSON."""
    storage = get_gui_storage()
    ledger = await asyncio.to_thread(storage.load_review, review_id)
    if ledger is None:
        raise HTTPException(status_code=404, detail="Review not found")
    return JSONResponse(ledger)


# ── Override ─────────────────────────────────────────────────────────────────

@router.post("/review/{review_id}/override")
async def override_group(request: Request, review_id: str):
    """Override an escalated group's resolution."""
    _check_csrf(request)
    body = await request.json()
    group_id = body.get("group_id", "")
    resolution = body.get("resolution", "")

    valid_resolutions = {"overridden", "auto_dismissed", "escalated"}
    if resolution not in valid_resolutions:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid resolution. Must be one of: {', '.join(valid_resolutions)}",
        )

    if not group_id:
        raise HTTPException(status_code=400, detail="group_id is required")

    storage = get_gui_storage()
    try:
        await asyncio.to_thread(
            storage.update_point_override, review_id, group_id, resolution
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Invalidate cache
    from .pages import _review_cache
    _review_cache["data"] = None

    return JSONResponse({"status": "ok", "group_id": group_id, "resolution": resolution})


# ── Revision ─────────────────────────────────────────────────────────────────

@router.post("/review/{review_id}/revise")
async def revise_review(request: Request, review_id: str):
    """Generate a revised artifact from a completed review."""
    _check_csrf(request)

    storage = get_gui_storage()
    ledger = await asyncio.to_thread(storage.load_review, review_id)
    if ledger is None:
        raise HTTPException(status_code=404, detail="Review not found")

    mode = ledger.get("mode", "plan")
    review_dir = storage.reviews_dir / review_id

    # Load original content
    oc_path = review_dir / "original_content.txt"
    if not oc_path.exists():
        raise HTTPException(
            status_code=400,
            detail="original_content.txt not found. Cannot generate revision.",
        )
    original_content = await asyncio.to_thread(oc_path.read_text)

    # Load config and resolve revision model
    from ..config import load_config, get_models_by_role
    from ..types import CostTracker

    config_path = request.app.state.config_path
    config = await asyncio.to_thread(
        load_config, Path(config_path) if config_path else None
    )
    roles = get_models_by_role(config)
    revision_model = roles["revision"]

    cost_tracker = CostTracker()

    # Run revision
    import httpx
    from ..revision import run_revision

    try:
        async with httpx.AsyncClient() as client:
            revised = await run_revision(
                client, revision_model, original_content, ledger,
                mode=mode, cost_tracker=cost_tracker,
                storage=storage, review_id=review_id,
            )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Revision failed: {exc}")

    if not revised:
        return JSONResponse({
            "status": "no_output",
            "message": "No revised artifact produced",
            "cost": cost_tracker.total_usd,
        })

    # Save revised artifact
    output_names = {
        "plan": "revised-plan.md",
        "code": "revised-diff.patch",
        "integration": "remediation-plan.md",
    }
    output_name = output_names.get(mode, "revised-plan.md")
    await asyncio.to_thread(
        StorageManager._atomic_write, review_dir / output_name, revised
    )

    return JSONResponse({
        "status": "ok",
        "content": revised,
        "filename": output_name,
        "cost": cost_tracker.total_usd,
    })


# ── Downloads ────────────────────────────────────────────────────────────────

@router.get("/review/{review_id}/report")
async def download_report(request: Request, review_id: str):
    """Download dvad-report.md."""
    storage = get_gui_storage()
    path = storage.reviews_dir / review_id / "dvad-report.md"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Report not found")
    return FileResponse(path, filename="dvad-report.md", media_type="text/markdown")


@router.get("/review/{review_id}/revised")
async def download_revised(request: Request, review_id: str):
    """Download the revised artifact."""
    storage = get_gui_storage()
    review_dir = storage.reviews_dir / review_id
    for name in ["revised-plan.md", "revised-diff.patch", "remediation-plan.md", "revised-spec-suggestions.md"]:
        path = review_dir / name
        if path.exists():
            return FileResponse(path, filename=name)
    raise HTTPException(status_code=404, detail="Revised artifact not found")


# ── Config ───────────────────────────────────────────────────────────────────

@router.get("/config")
async def get_config_json(request: Request):
    """Return current config as JSON."""
    from ..config import load_config

    config_path = request.app.state.config_path
    try:
        config = await asyncio.to_thread(
            load_config, Path(config_path) if config_path else None
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    # Serialize models
    models_data = {}
    for name, m in config.get("all_models", config.get("models", {})).items():
        models_data[name] = {
            "provider": m.provider,
            "model_id": m.model_id,
            "api_key_env": m.api_key_env,
            "api_base": m.api_base,
            "context_window": m.context_window,
            "timeout": m.timeout,
            "cost_per_1k_input": m.cost_per_1k_input,
            "cost_per_1k_output": m.cost_per_1k_output,
            "has_key": bool(m.api_key),
            "roles": sorted(m.roles),
            "deduplication": m.deduplication,
            "integration_reviewer": m.integration_reviewer,
            "use_completion_tokens": m.use_completion_tokens,
        }

    return JSONResponse({
        "config_path": config.get("config_path", ""),
        "models": models_data,
    })


@router.post("/config/model-timeout")
async def set_model_timeout(request: Request):
    """Update a single model's timeout value in the config file."""
    _check_csrf(request)
    body = await request.json()
    model_name = body.get("model_name", "")
    timeout = body.get("timeout")

    if not model_name:
        raise HTTPException(status_code=400, detail="model_name is required")
    try:
        timeout = int(timeout)
        if timeout < 10 or timeout > 7200:
            raise ValueError
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="timeout must be an integer between 10 and 7200")

    def _apply(data: dict) -> None:
        if "models" not in data or model_name not in data["models"]:
            raise HTTPException(status_code=404, detail=f"Model '{model_name}' not found in config")
        data["models"][model_name]["timeout"] = timeout

    await _mutate_yaml_config(request, _apply)
    return JSONResponse({"status": "ok", "model_name": model_name, "timeout": timeout})


@router.post("/config/model-thinking")
async def set_model_thinking(request: Request):
    """Toggle a model's thinking/reasoning setting."""
    _check_csrf(request)
    body = await request.json()
    model_name = body.get("model_name", "")
    thinking = body.get("thinking", False)

    if not model_name:
        raise HTTPException(status_code=400, detail="model_name is required")

    def _apply(data: dict) -> None:
        if "models" not in data or model_name not in data["models"]:
            raise HTTPException(status_code=404, detail=f"Model '{model_name}' not found in config")
        data["models"][model_name]["thinking"] = bool(thinking)

    await _mutate_yaml_config(request, _apply)
    return JSONResponse({"status": "ok", "model_name": model_name, "thinking": bool(thinking)})


@router.post("/config/model-max-tokens")
async def set_model_max_tokens(request: Request):
    """Update a single model's max_output_tokens value in the config file."""
    _check_csrf(request)
    body = await request.json()
    model_name = body.get("model_name", "")
    max_tokens = body.get("max_output_tokens")

    if not model_name:
        raise HTTPException(status_code=400, detail="model_name is required")

    if max_tokens is not None:
        try:
            max_tokens = int(max_tokens)
            if max_tokens < 1 or max_tokens > 1000000:
                raise ValueError
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="max_output_tokens must be an integer between 1 and 1000000")

    def _apply(data: dict) -> None:
        if "models" not in data or model_name not in data["models"]:
            raise HTTPException(status_code=404, detail=f"Model '{model_name}' not found in config")
        if max_tokens is not None:
            data["models"][model_name]["max_output_tokens"] = max_tokens
        else:
            data["models"][model_name].pop("max_output_tokens", None)

    await _mutate_yaml_config(request, _apply)
    return JSONResponse({"status": "ok", "model_name": model_name, "max_output_tokens": max_tokens})


@router.post("/config/settings-toggle")
async def set_settings_toggle(request: Request):
    """Toggle a boolean flag in the settings block."""
    _check_csrf(request)
    body = await request.json()
    key = body.get("key", "")
    value = body.get("value", False)

    valid_keys = {"live_testing"}
    if key not in valid_keys:
        raise HTTPException(status_code=400, detail=f"Unknown setting: {key}")

    def _apply(data: dict) -> None:
        if "settings" not in data:
            data["settings"] = {}
        data["settings"][key] = bool(value)

    await _mutate_yaml_config(request, _apply)
    return JSONResponse({"status": "ok", "key": key, "value": bool(value)})


@router.post("/config/validate")
async def validate_config_endpoint(request: Request):
    """Validate config YAML. Returns issues list."""
    _check_csrf(request)
    body = await request.json()
    yaml_content = body.get("yaml", "")

    import yaml
    try:
        raw = yaml.safe_load(yaml_content)
    except yaml.YAMLError as exc:
        return JSONResponse({"valid": False, "issues": [["error", f"YAML parse error: {exc}"]]})

    if not raw or "models" not in raw:
        return JSONResponse({"valid": False, "issues": [["error", "Missing 'models' key"]]})

    # Try loading through the config pipeline
    from ..config import load_config, validate_config
    import tempfile, os

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".yaml")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            f.write(yaml_content)
        config = await asyncio.to_thread(load_config, Path(tmp_path))
        issues = validate_config(config)
    except Exception as exc:
        return JSONResponse({"valid": False, "issues": [["error", str(exc)]]})
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    return JSONResponse({
        "valid": not any(level == "error" for level, _ in issues),
        "issues": [[level, msg] for level, msg in issues],
    })


@router.post("/config")
async def save_config(request: Request):
    """Save config YAML (overwrites the loaded config file only)."""
    _check_csrf(request)
    body = await request.json()
    yaml_content = body.get("yaml", "")

    import yaml
    try:
        raw = yaml.safe_load(yaml_content)
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=400, detail=f"YAML parse error: {exc}")

    if not raw or "models" not in raw:
        raise HTTPException(status_code=400, detail="Missing 'models' key")

    # Validate before saving
    from ..config import load_config, validate_config, find_config
    import tempfile, os

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".yaml")
    try:
        with os.fdopen(tmp_fd, "w") as f:
            f.write(yaml_content)
        config = await asyncio.to_thread(load_config, Path(tmp_path))
        issues = validate_config(config)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    errors = [msg for level, msg in issues if level == "error"]
    if errors:
        raise HTTPException(status_code=400, detail=f"Validation errors: {'; '.join(errors)}")

    # Determine the config file to write
    config_path_str = request.app.state.config_path
    if config_path_str:
        target = Path(config_path_str)
    else:
        try:
            target = find_config()
        except Exception:
            raise HTTPException(status_code=400, detail="Cannot determine config file path")

    # Round-trip save with ruamel.yaml to preserve comments
    try:
        yaml_rt = YAML()
        yaml_rt.preserve_quotes = True
        new_data = yaml_rt.load(yaml_content)
        stream = StringIO()
        yaml_rt.dump(new_data, stream)
        final_content = stream.getvalue()
    except Exception:
        final_content = yaml_content

    # Atomic write
    await asyncio.to_thread(StorageManager._atomic_write, target, final_content)

    return JSONResponse({"status": "ok", "path": str(target)})
