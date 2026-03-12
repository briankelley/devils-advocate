"""Background review execution — task lifecycle, status, and cleanup."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

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
        file_manifest: dict | None = None,
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
                file_manifest=file_manifest,
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
        file_manifest: dict | None = None,
    ) -> None:
        """Execute the review orchestrator in a background task."""
        storage = None
        try:
            from ..config import load_config
            from ..storage import StorageManager

            config = load_config(Path(config_path) if config_path else None)

            # Create a log-hooking storage manager
            actual_project_dir = project_dir or Path.home()
            storage = StorageManager(actual_project_dir)
            storage.set_review_id(review_id)

            # Save file manifest and input copies
            if file_manifest:
                manifest_path = storage.reviews_dir / review_id / "input_files_manifest.json"
                manifest_path.parent.mkdir(parents=True, exist_ok=True)
                StorageManager._atomic_write(manifest_path, json.dumps(file_manifest, indent=2))

                for file_info in file_manifest.get("files", []):
                    if file_info.get("copied"):
                        src = Path(file_info["original_path"])
                        if src.exists():
                            dest = storage.reviews_dir / review_id / f"input_{src.name}"
                            StorageManager._atomic_write(dest, src.read_text())
                            file_info["original_path"] = str(dest)

                # Re-write manifest with updated paths
                manifest_path = storage.reviews_dir / review_id / "input_files_manifest.json"
                StorageManager._atomic_write(manifest_path, json.dumps(file_manifest, indent=2))

            # Monkey-patch storage.log to also emit progress events
            original_log = storage.log
            _last_emitted_msg = [None]

            def hooked_log(msg: str) -> None:
                original_log(msg)
                event = classify_log_message(msg)
                # Deduplicate consecutive identical messages
                if event.message and event.message == _last_emitted_msg[0]:
                    return
                _last_emitted_msg[0] = event.message
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
            if roles.get("integration"):
                role_meta["roles"]["integration"] = roles["integration"].name
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

            # Each orchestrator creates its own HTTP client — no shared client needed.
            # Wrap in a timeout so a hung orchestrator doesn't kill the server.
            # Use create_task + cancel instead of wait_for so we don't block
            # waiting for the cancelled task's cleanup (httpx client close can hang).
            _REVIEW_TIMEOUT = 1800  # 30 minutes

            if mode == "plan":
                from ..orchestrator import run_plan_review
                coro = run_plan_review(
                    config, input_files, project, max_cost, dry_run,
                    storage=storage,
                )
            elif mode == "code":
                from ..orchestrator import run_code_review
                coro = run_code_review(
                    config, input_files[0], project, spec_file, max_cost, dry_run,
                    storage=storage,
                )
            elif mode == "integration":
                from ..orchestrator import run_integration_review
                coro = run_integration_review(
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
                coro = run_spec_review(
                    config, input_files, project, max_cost, dry_run,
                    storage=storage,
                )
            else:
                raise ValueError(f"Unknown mode: {mode}")

            inner_task = asyncio.create_task(coro)
            try:
                result = await asyncio.wait_for(
                    asyncio.shield(inner_task), timeout=_REVIEW_TIMEOUT,
                )
            except asyncio.TimeoutError:
                logger.error("Review %s timed out after %ds", review_id, _REVIEW_TIMEOUT)
                inner_task.cancel()
                # Don't await the cancelled task — its cleanup may block
                if storage is not None:
                    storage.release_lock()
                result = None

            if result is None:
                # Check if orchestrator already saved a ledger (dry_run, cost_exceeded, etc.)
                existing = storage.load_review(review_id) if storage else None
                if existing and existing.get("result") not in (None, "", "running"):
                    # Orchestrator handled it — use its result
                    self.statuses[review_id] = "complete"
                    if review_id in self.active:
                        self.active[review_id]["state"] = "complete"
                    self.emit_event(review_id, make_terminal_event(True))
                else:
                    # Genuinely failed — no ledger saved by orchestrator
                    self.statuses[review_id] = "failed"
                    if review_id in self.active:
                        self.active[review_id]["state"] = "failed"
                    try:
                        from ..orchestrator._common import _save_stub_ledger
                        _save_stub_ledger(
                            storage, review_id, mode, project,
                            str(input_files[0]) if input_files else "none",
                            "failed",
                        )
                    except Exception:
                        pass
                    self.emit_event(review_id, make_terminal_event(False, "Review returned no result"))
            else:
                self.statuses[review_id] = "complete"
                if review_id in self.active:
                    self.active[review_id]["state"] = "complete"
                self.emit_event(review_id, make_terminal_event(True))

        except asyncio.CancelledError:
            self._handle_review_failure(
                review_id, storage, mode, project, input_files, "Review cancelled",
            )
            raise

        except Exception as exc:
            logger.exception("Review %s failed", review_id)
            self._handle_review_failure(
                review_id, storage, mode, project, input_files, str(exc),
            )

        finally:
            self.current_review_id = None
            self.current_task = None

    def _handle_review_failure(
        self,
        review_id: str,
        storage,
        mode: str,
        project: str,
        input_files: list,
        message: str,
    ) -> None:
        """Common failure cleanup: update status, release lock, save stub, emit event."""
        self.statuses[review_id] = "failed"
        if review_id in self.active:
            self.active[review_id]["state"] = "failed"
        if storage is not None:
            storage.release_lock()
        try:
            if storage is not None:
                from ..orchestrator._common import _save_stub_ledger
                _save_stub_ledger(
                    storage, review_id, mode, project,
                    str(input_files[0]) if input_files else "unknown",
                    "failed",
                )
        except Exception:
            pass
        self.emit_event(review_id, make_terminal_event(False, message))

    def cancel_review(self, review_id: str) -> bool:
        """Cancel a running review. Returns True if cancelled."""
        if (
            self.current_review_id == review_id
            and self.current_task
            and not self.current_task.done()
        ):
            self.current_task.cancel()
            return True
        return False

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
