"""Your own labels in data/clean/labels.json, keyed by job id (separate from applications.json).

Each label keeps a snapshot of what the agent said when you made the judgment (score, verdict,
or drop reason), so re-scoring later does not change the history. Standard library only.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Dict, Optional

from dashboard.store import MAX_NOTES, StoreError, atomic_write, now_iso

LABELS = ("yes", "maybe", "no")
REASONS = ("location", "seniority", "domain", "role type", "company", "compensation", "other")


class LabelStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def load(self) -> Dict[str, dict]:
        import json
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text())
        except ValueError as exc:
            raise StoreError(f"{self.path} is not valid JSON; fix or move it (not overwriting)") from exc
        if not isinstance(data, dict):
            raise StoreError(f"{self.path} must hold a JSON object")
        return data

    def set(self, job_id: str, *, kind: str, label: Optional[str], snapshot: dict,
            reason: Optional[str] = None, note: str = "", now: Optional[str] = None) -> Optional[dict]:
        """Create or update a label; label=None removes it. kind is "scored" or "filtered"
        (a filtered job is "yes" = should have been kept, or "no" = correctly dropped).
        The snapshot is refreshed only when the label value itself changes, so editing the
        reason or note keeps the agent output you originally judged."""
        if kind not in ("scored", "filtered"):
            raise StoreError("bad kind")
        if not job_id or len(job_id) > 100:
            raise StoreError("bad job id")
        if label is not None and label not in LABELS:
            raise StoreError(f"unknown label {label!r}")
        if kind == "filtered" and label not in (None, "yes", "no"):
            raise StoreError("a filtered-out job is 'yes' (should have been kept) or 'no' (correctly dropped)")
        if reason in ("", None):
            reason = None
        elif reason not in REASONS:
            raise StoreError(f"unknown reason {reason!r}")
        if not isinstance(note, str) or len(note) > MAX_NOTES:
            raise StoreError("note must be text of at most %d characters" % MAX_NOTES)
        with self._lock:
            data = self.load()
            if label is None:
                if data.pop(job_id, None) is not None:
                    atomic_write(self.path, data)
                return None
            old = data.get(job_id)
            keep_snapshot = old is not None and old["label"] == label and old["kind"] == kind
            entry = {"kind": kind, "label": label, "reason": reason, "note": note,
                     "labelled_at": old["labelled_at"] if keep_snapshot else (now or now_iso()),
                     "snapshot": old["snapshot"] if keep_snapshot else snapshot}
            if old != entry:
                data[job_id] = entry
                atomic_write(self.path, data)
            return entry
