"""Monthly/daily API call counter in data/raw/api_usage.json.

A call is counted before it is made, so a call that errors still uses budget. Once an API's
budget is spent, `allowed()` is False and the scout must stop calling it.
"""
from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import Dict, Optional

from screener.cache import atomic_write_json

log = logging.getLogger(__name__)
USAGE_PATH = Path("data/raw/api_usage.json")


class UsageTracker:
    def __init__(self, path: Path = USAGE_PATH, limits: Optional[Dict[str, Dict[str, int]]] = None,
                 today: Optional[date] = None):
        """limits: {api: {"month": n, "day": n (optional)}}."""
        self.path = path
        self.limits = limits or {}
        self.today = today or date.today()
        self.data: Dict[str, dict] = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text())
            except ValueError:
                log.warning("api_usage.json unreadable; starting counters at zero")

    def _entry(self, api: str) -> dict:
        e = self.data.setdefault(api, {})
        if e.get("month") != self.today.strftime("%Y-%m"):
            e.update(month=self.today.strftime("%Y-%m"), month_calls=0, day=None, day_calls=0)
        if e.get("day") != self.today.isoformat():
            e.update(day=self.today.isoformat(), day_calls=0)
        return e

    def month_calls(self, api: str) -> int:
        return self._entry(api)["month_calls"]

    def allowed(self, api: str) -> bool:
        e, lim = self._entry(api), self.limits.get(api, {})
        if "month" in lim and e["month_calls"] >= lim["month"]:
            return False
        return not ("day" in lim and e["day_calls"] >= lim["day"])

    def remaining(self, api: str) -> int:
        e, lim = self._entry(api), self.limits.get(api, {})
        left = [lim[k] - e[f"{k}_calls"] for k in ("month", "day") if k in lim]
        return max(0, min(left)) if left else 0

    def record(self, api: str) -> None:
        e = self._entry(api)
        e["month_calls"] += 1
        e["day_calls"] += 1
        self.save()

    def cursor(self, api: str) -> int:
        return int(self.data.get(api, {}).get("cursor", 0))

    def set_cursor(self, api: str, value: int) -> None:
        self._entry(api)["cursor"] = value
        self.save()

    def save(self) -> None:
        atomic_write_json(self.path, self.data)
