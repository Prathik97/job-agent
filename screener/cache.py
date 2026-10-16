"""JSON cache keyed by stage + model + job id + description hash, plus atomic writes."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

from screener.text import sha

log = logging.getLogger(__name__)


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


def cache_key(stage: str, model: str, version: str, job_id: str, description: str,
              extra: str = "") -> str:
    return "|".join([stage, model, version, job_id, sha(description), extra])


class Cache:
    def __init__(self, path: Path):
        self.path = path
        self.data: Dict[str, dict] = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text())
            except json.JSONDecodeError:
                log.warning("cache %s is corrupt; starting empty", path)

    def get(self, key: str) -> Optional[dict]:
        return self.data.get(key)

    def put(self, key: str, value: dict) -> None:
        """Persist immediately so a crash never loses already-paid results."""
        self.data[key] = value
        atomic_write_json(self.path, self.data)
