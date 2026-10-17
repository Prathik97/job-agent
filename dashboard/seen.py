"""When the dashboard was last opened, kept in data/clean/dashboard_seen.json.

`since` is the baseline for the "New" badge: jobs first seen after it are new. Opening the
dashboard moves `last_seen` to now. A reopen within SAME_VISIT_MINUTES keeps the old baseline,
so an accidental reload does not erase the badges. The very first open has no baseline, so
nothing is marked new. first_seen_at on each job stays the source of truth.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from dashboard.store import atomic_write

SAME_VISIT_MINUTES = 30


def _parse(value: object) -> Optional[datetime]:
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


class SeenStore:
    def __init__(self, path: Path):
        self.path = path

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def last_seen(self) -> Optional[str]:
        return self._load().get("last_seen")

    def since(self) -> Optional[str]:
        """Baseline of the current visit, or None before the first one."""
        return self._load().get("since")

    def open_visit(self, now: Optional[datetime] = None) -> Optional[str]:
        """Record an open of the dashboard; returns the baseline for this visit."""
        now = now or datetime.now(timezone.utc)
        state = self._load()
        previous = _parse(state.get("last_seen"))
        since = state.get("since")
        if previous is not None and now - previous >= timedelta(minutes=SAME_VISIT_MINUTES):
            since = state["last_seen"]
        elif previous is None:
            since = None
        atomic_write(self.path, {"last_seen": now.isoformat(timespec="seconds"), "since": since})
        return since
