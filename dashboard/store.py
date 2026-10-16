"""Application tracking in data/clean/applications.json, keyed by job id.

Kept apart from screened_jobs.json so re-running the pipeline never touches it. Standard
library only. Writes are atomic (temp file, fsync, rename) and serialized with a lock.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, Optional

STATUSES = ("new", "shortlisted", "applied", "interview", "offer", "rejected", "withdrawn", "skipped")
PIPELINE_STATUSES = ("applied", "interview", "offer", "rejected", "withdrawn")
MAX_NOTES = 5000


class StoreError(Exception):
    """Bad input or an unreadable store file. The file is never overwritten in the latter case."""


def atomic_write(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        with tmp.open("w") as fh:
            json.dump(data, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ApplicationsStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def load(self) -> Dict[str, dict]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text())
        except ValueError as exc:
            raise StoreError(f"{self.path} is not valid JSON; fix or move it (not overwriting)") from exc
        if not isinstance(data, dict):
            raise StoreError(f"{self.path} must hold a JSON object")
        return data

    def update(self, job_id: str, *, status: Optional[str] = None, notes: Optional[str] = None,
               mark_applied: bool = False, snapshot: Optional[dict] = None,
               now: Optional[str] = None, today: Optional[date] = None) -> dict:
        """Apply a change and return the entry. `snapshot` (company, title, url, score, verdict)
        is stored the first time the status leaves "new"."""
        if mark_applied:
            status = "applied"
        if status is not None and status not in STATUSES:
            raise StoreError(f"unknown status {status!r}")
        if notes is not None:
            if not isinstance(notes, str) or len(notes) > MAX_NOTES:
                raise StoreError("notes must be text of at most %d characters" % MAX_NOTES)
        if not job_id or len(job_id) > 100:
            raise StoreError("bad job id")
        now, today = now or now_iso(), today or date.today()
        with self._lock:
            data = self.load()
            entry = data.get(job_id) or {"status": "new", "updated_at": None, "applied_at": None,
                                         "notes": "", "history": [], "snapshot": None}
            changed = False
            if status is not None and status != entry["status"]:
                entry["history"].append({"at": now, "from": entry["status"], "to": status})
                if entry["status"] == "new" and entry["snapshot"] is None:
                    entry["snapshot"] = snapshot
                if status == "applied" and not entry["applied_at"]:
                    entry["applied_at"] = today.isoformat()
                entry["status"] = status
                changed = True
            if notes is not None and notes != entry["notes"]:
                entry["notes"] = notes
                changed = True
            if changed:
                entry["updated_at"] = now
                data[job_id] = entry
                atomic_write(self.path, data)
            return entry
