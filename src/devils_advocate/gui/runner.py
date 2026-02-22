"""Background review execution — task lifecycle, status, and cleanup."""

from __future__ import annotations

import asyncio
import logging
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .progress import ProgressEvent, classify_log_message, make_terminal_event

logger = logging.getLogger(__name__)


class ReviewRunner:
    """Manages single-review-at-a-time background execution.

    Enforces one concurrent review per server instance. Maintains
    per-review status, event queues, and buffered events for late-
    connecting SSE clients.
    """

    def __init__(self) -> None:
        self.current_review_id: str | None = None
        self.current_task: asyncio.Task | None = None
        self.statuses: dict[str, str] = {}  # review_id -> running|complete|failed|unknown
        self.active: dict[str, dict] = {}   # review_id -> {queue, buffered, state, ...}

    async def start_review(
        self,
        mode: str,
        input_files: list[Path],
        project: str,
        config_path: str | None = None,
        spec_file: Path | None = None,
        project_dir: Path | None = None,
        max_cost: float | None = None,
        dry_run: bool = False,
    ) -> str:
        """Start a background review. Returns review_id. Raises HTTPException(409) if busy."""
        from fastapi import HTTPException

        if self.current_task and not self.current_task.done():
            raise HTTPException(
                status_code=409,
                detail="A review is already running. Wait for it to complete.",
            )

        # Read input content for review ID generation (content hash)
        from ..ids import generate_review_id
        content_parts = []
        for f in input_files:
            try:
                content_parts.append(f.read_text())
            except OSError:
                content_parts.append(str(f))
        content = "\n".join(content_parts)
        review_id = generate_review_id(content)

        queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        buffered: list[dict] = []

        self.current_review_id = review_id
        self.statuses[review_id] = "running"
        self.active[review_id] = {
            "queue": queue,
            "buffered": buffered,
            "state": "running",
            "created_at": time.time(),
            "last_event_at": time.time(),
        }

        # Launch background task
        self.current_task = asyncio.create_task(
            self._run(
                review_id=review_id,
                mode=mode,
                input_files=input_files,
                project=project,
                config_path=config_path,
                spec_file=spec_file,
                project_dir=project_dir,
                max_cost=max_cost,
                dry_run=dry_run,
                queue=queue,
                buffered=buffered,
            )
        )
        return review_id

    def emit_event(self, review_id: str, event: ProgressEvent) -> None:
        """Push an event to the review's queue and buffer."""
        entry = self.active.get(review_id)
        if not entry:
            return
        data = {
            "type": event.event_type,
            "message": event.message,
            "phase": event.phase,
            "detail": event.detail,
            "timestamp": event.timestamp,
        }
        entry["buffered"].append(data)
        entry["last_event_at"] = time.time()
        try:
            entry["queue"].put_nowait(data)
        except asyncio.QueueFull:
            # Drop oldest to make room
            try:
                entry["queue"].get_nowait()
            except asyncio.QueueEmpty:
                pass
            try:
                entry["queue"].put_nowait(data)
            except asyncio.QueueFull:
                pass

    async def _run(
        self,
        review_id: str,
        mode: str,
        input_files: list[Path],
        project: str,
        config_path: str | None,
        spec_file: Path | None,
        project_dir: Path | None,
        max_cost: float | None,
        dry_run: bool,
        queue: asyncio.Queue,
        buffered: list[dict],
    ) -> None:
        """Execute the review orchestrator in a background task."""
        tmpdir = None
        try:
            from ..config import load_config
            from ..storage import StorageManager

            config = load_config(Path(config_path) if config_path else None)

            # Create a log-hooking storage manager
            actual_project_dir = project_dir or Path.home()
            storage = StorageManager(actual_project_dir)
            storage.set_review_id(review_id)

            # Monkey-patch storage.log to also emit progress events
            original_log = storage.log

            def hooked_log(msg: str) -> None:
                original_log(msg)
                event = classify_log_message(msg)
                self.emit_event(review_id, event)

            storage.log = hooked_log

            # Emit metadata event with role→model mapping for live cost table
            from ..config import get_models_by_role
            roles = get_models_by_role(config)
            role_meta: dict = {"mode": mode, "project": project, "roles": {}}
            if roles.get("author"):
                role_meta["roles"]["author"] = roles["author"].name
            for i, r in enumerate(roles.get("reviewers", []), 1):
                role_meta["roles"][f"reviewer_{i}"] = r.name
            if roles.get("dedup"):
                role_meta["roles"]["dedup"] = roles["dedup"].name
            if roles.get("normalization"):
                role_meta["roles"]["normalization"] = roles["normalization"].name
            if roles.get("revision"):
                role_meta["roles"]["revision"] = roles["revision"].name
            self.emit_event(review_id, ProgressEvent(
                event_type="metadata", phase="review_metadata", detail=role_meta,
            ))

            self.emit_event(
                review_id,
                ProgressEvent(
                    event_type="phase",
                    message=f"Starting {mode} review for project '{project}'",
                    phase="review_start",
                ),
            )

            import httpx

            async with httpx.AsyncClient() as client:
                if mode == "plan":
                    from ..orchestrator import run_plan_review
                    result = await run_plan_review(
                        config, input_files, project, max_cost, dry_run,
                        storage=storage,
                    )
                elif mode == "code":
                    from ..orchestrator import run_code_review
                    result = await run_code_review(
                        config, input_files[0], project, spec_file, max_cost, dry_run,
                        storage=storage,
                    )
                elif mode == "integration":
                    from ..orchestrator import run_integration_review
                    result = await run_integration_review(
                        config, project,
                        input_files=input_files if input_files else None,
                        spec_file=spec_file,
                        project_dir=project_dir,
                        max_cost=max_cost,
                        dry_run=dry_run,
                        storage=storage,
                    )
                elif mode == "spec":
                    from ..orchestrator import run_spec_review
                    result = await run_spec_review(
                        config, input_files, project, max_cost, dry_run,
                        storage=storage,
                    )
                else:
                    raise ValueError(f"Unknown mode: {mode}")

            self.statuses[review_id] = "complete"
            if review_id in self.active:
                self.active[review_id]["state"] = "complete"
            self.emit_event(review_id, make_terminal_event(True))

        except asyncio.CancelledError:
            self.statuses[review_id] = "failed"
            if review_id in self.active:
                self.active[review_id]["state"] = "failed"
            self.emit_event(review_id, make_terminal_event(False, "Review cancelled"))
            raise

        except Exception as exc:
            logger.exception("Review %s failed", review_id)
            self.statuses[review_id] = "failed"
            if review_id in self.active:
                self.active[review_id]["state"] = "failed"
            self.emit_event(review_id, make_terminal_event(False, str(exc)))

        finally:
            self.current_review_id = None
            self.current_task = None
            if tmpdir:
                try:
                    import shutil
                    shutil.rmtree(tmpdir, ignore_errors=True)
                except Exception:
                    pass

    def get_status(self, review_id: str) -> str:
        """Return status for a review: running|complete|failed|unknown."""
        return self.statuses.get(review_id, "unknown")

    def get_buffered_events(self, review_id: str) -> list[dict]:
        """Return all buffered events for a review (for late-connecting clients)."""
        entry = self.active.get(review_id)
        if entry:
            return list(entry["buffered"])
        return []

    def get_queue(self, review_id: str) -> asyncio.Queue | None:
        """Return the event queue for a review."""
        entry = self.active.get(review_id)
        if entry:
            return entry["queue"]
        return None
