"""Which output/resumes folder was prepared for which job (data/clean/prepared.json)."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Dict

from dashboard.store import atomic_write


class PreparedStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def load(self) -> Dict[str, str]:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def set(self, job_id: str, folder: str) -> None:
        with self._lock:
            data = self.load()
            data[job_id] = folder
            atomic_write(self.path, data)
