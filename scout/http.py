"""Shared HTTP helpers: retrying session, polite delay, content-hash cache."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from html import unescape
from pathlib import Path
from typing import Any, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)

TIMEOUT = 20
MIN_DELAY_SECONDS = 1.0
USER_AGENT = "job-agent/0.1 (personal job search; public job board APIs)"
CACHE_DIR = Path("data/raw/cache")

_last_call = 0.0


def make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=1.0, status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=("GET",), respect_retry_after_header=True)
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers["User-Agent"] = USER_AGENT
    return session


def get_json(session: requests.Session, url: str, params: Optional[dict] = None,
             delay: float = MIN_DELAY_SECONDS) -> Optional[Any]:
    """GET JSON with rate limiting. Returns None on 404 (board does not exist)."""
    global _last_call
    wait = delay - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)
    try:
        resp = session.get(url, params=params, timeout=TIMEOUT)
    finally:
        _last_call = time.monotonic()
    if resp.status_code == 404:
        log.warning("404 for %s", url)
        return None
    resp.raise_for_status()
    return resp.json()


def content_hash(payload: Any) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def cache_changed(source: str, slug: str, payload: Any, cache_dir: Path = CACHE_DIR) -> bool:
    """Record the payload hash; return True if it differs from the cached one."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{source}_{slug}.hash"
    new = content_hash(payload)
    old = path.read_text().strip() if path.exists() else None
    if old == new:
        return False
    path.write_text(new)
    return True


_TAG = re.compile(r"<[^>]+>")
_BLOCK = re.compile(r"</?(p|div|br|li|ul|ol|h[1-6])[^>]*>", re.I)


def html_to_text(raw: str) -> str:
    """Strip HTML (handles Greenhouse's entity-escaped HTML) to plain text."""
    text = unescape(raw or "")
    text = _BLOCK.sub("\n", text)
    text = unescape(_TAG.sub("", text))
    return re.sub(r"\n\s*\n+", "\n", text).strip()


def infer_remote_type(location: str, *hints: str) -> str:
    """Best-effort remote/hybrid/onsite from location text and explicit hints."""
    blob = " ".join([location or "", *[h for h in hints if h]]).lower()
    if "hybrid" in blob:
        return "hybrid"
    if "remote" in blob:
        return "remote"
    if "on-site" in blob or "onsite" in blob or "on site" in blob:
        return "onsite"
    return "unknown"
