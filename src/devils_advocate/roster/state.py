"""Scanner bookkeeping — kept beside models.yaml, never inside it.

Everything the scan learns that is not a config value lives here: the last
payload digest, subscription-probe verdicts, which deprecations have already
been reported, and the notice queue the GUI reads on launch.

Keeping this out of models.yaml matters for two reasons. The operator's config
is a hand-tuned artefact they read and edit directly, so filling it with
machine bookkeeping would degrade it. And on the overwhelming majority of days
the scan finds nothing to add, which under this split means it does not open
models.yaml at all.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..storage import StorageManager

STATE_VERSION = 1

# Notice levels, in ascending urgency. ``critical`` is reserved for a condition
# that will break the next review if untouched.
LEVEL_INFO = "info"
LEVEL_WARN = "warn"
LEVEL_CRITICAL = "critical"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def roster_dir(data_dir: Path | None = None) -> Path:
    """Resolve (and create) the scanner's state directory."""
    base = StorageManager._resolve_data_dir(data_dir)
    path = base / "roster"
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class Notice:
    """One thing the operator should see next time they open the browser.

    ``id`` is stable and derived from the finding, so a condition rediscovered
    on every daily pass updates one notice instead of breeding a new one each
    morning. A dismissed notice stays dismissed unless the finding changes.
    """

    id: str
    level: str
    title: str
    body: str
    created: str = field(default_factory=now_iso)
    updated: str = field(default_factory=now_iso)
    dismissed: bool = False


def _read_json(path: Path, fallback: dict) -> dict:
    """Read JSON, treating any damage as absence. State is never load-bearing."""
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else dict(fallback)
    except (OSError, json.JSONDecodeError):
        return dict(fallback)


def _write_json(path: Path, data: dict) -> None:
    StorageManager._atomic_write(path, json.dumps(data, indent=2, sort_keys=True))


# ─── Scan state ────────────────────────────────────────────────────────────


def state_path(data_dir: Path | None = None) -> Path:
    return roster_dir(data_dir) / "state.json"


def load_state(data_dir: Path | None = None) -> dict:
    return _read_json(
        state_path(data_dir),
        {"version": STATE_VERSION, "availability": {}, "reported": {}},
    )


def save_state(state: dict, data_dir: Path | None = None) -> None:
    state["version"] = STATE_VERSION
    _write_json(state_path(data_dir), state)


# ─── Notices ───────────────────────────────────────────────────────────────


def notices_path(data_dir: Path | None = None) -> Path:
    return roster_dir(data_dir) / "notices.json"


def load_notices(data_dir: Path | None = None) -> list[Notice]:
    raw = _read_json(notices_path(data_dir), {"version": STATE_VERSION, "notices": []})
    out = []
    for item in raw.get("notices") or []:
        if not isinstance(item, dict) or "id" not in item:
            continue
        known = {f: item.get(f) for f in Notice.__dataclass_fields__ if f in item}
        try:
            out.append(Notice(**known))
        except TypeError:
            continue
    return out


def save_notices(notices: list[Notice], data_dir: Path | None = None) -> None:
    _write_json(
        notices_path(data_dir),
        {"version": STATE_VERSION, "notices": [asdict(n) for n in notices]},
    )


def upsert(notices: list[Notice], candidate: Notice) -> list[Notice]:
    """Merge a finding into the queue by stable id.

    If the body is unchanged the existing notice is left completely alone —
    including its dismissed flag, so a permanent condition the operator has
    already acknowledged does not reappear every morning. If the body *has*
    changed the notice is revived, because it is now telling them something new.
    """
    for i, existing in enumerate(notices):
        if existing.id != candidate.id:
            continue
        if existing.body == candidate.body and existing.level == candidate.level:
            return notices
        revived = Notice(
            id=candidate.id,
            level=candidate.level,
            title=candidate.title,
            body=candidate.body,
            created=existing.created,
            updated=now_iso(),
            dismissed=False,
        )
        notices[i] = revived
        return notices
    notices.append(candidate)
    return notices


def prune(notices: list[Notice], live_ids: set[str]) -> list[Notice]:
    """Drop notices whose finding no longer holds.

    A shortfall the operator closed, or a deprecation that vanished because the
    model was removed by hand, should stop being reported without being
    dismissed. Notices whose id is not in *live_ids* are simply gone.
    """
    return [n for n in notices if n.id in live_ids]


def dismiss(notice_id: str, data_dir: Path | None = None) -> bool:
    notices = load_notices(data_dir)
    for notice in notices:
        if notice.id == notice_id:
            notice.dismissed = True
            notice.updated = now_iso()
            save_notices(notices, data_dir)
            return True
    return False


def active(data_dir: Path | None = None) -> list[Notice]:
    """Undismissed notices, most urgent first, then newest."""
    order = {LEVEL_CRITICAL: 0, LEVEL_WARN: 1, LEVEL_INFO: 2}
    return sorted(
        (n for n in load_notices(data_dir) if not n.dismissed),
        key=lambda n: (order.get(n.level, 3), n.updated),
    )
