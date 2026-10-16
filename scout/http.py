"""Shared HTTP helpers: retrying session, polite delay, content-hash cache."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from html import unescape
from pathlib import Path
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser
from typing import Any, Dict, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)

TIMEOUT = 20
MIN_DELAY_SECONDS = 1.0
USER_AGENT = "job-agent/0.1 (personal job search; public job board APIs)"
CACHE_DIR = Path("data/raw/cache")

_last_call = 0.0


def make_session(retry_statuses: tuple = (429, 500, 502, 503, 504)) -> requests.Session:
    """Retrying session. Metered APIs pass a list without 429 so a quota error surfaces at once."""
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=1.0, status_forcelist=retry_statuses,
                  allowed_methods=("GET", "POST"), respect_retry_after_header=True)
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers["User-Agent"] = USER_AGENT
    return session


class RobotsDisallowed(ValueError):
    """robots.txt forbids fetching this URL for our user agent (or could not be read)."""


_robots: Dict[str, Optional[RobotFileParser]] = {}


def _throttle(delay: float) -> None:
    wait = delay - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)


def robots_allowed(session: requests.Session, url: str, delay: float = MIN_DELAY_SECONDS) -> bool:
    """True if robots.txt for the URL's host allows our user agent. A missing robots.txt (4xx)
    allows everything; an unreadable one (5xx, network error) is treated as disallowed."""
    global _last_call
    parts = urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    if origin not in _robots:
        parser: Optional[RobotFileParser] = RobotFileParser()
        _throttle(delay)
        try:
            resp = session.get(f"{origin}/robots.txt", timeout=TIMEOUT)
            if resp.status_code == 200:
                parser.parse(resp.text.splitlines())
            elif resp.status_code < 500:
                parser = None  # no robots.txt: nothing is disallowed
            else:
                parser.parse(["User-agent: *", "Disallow: /"])
        except requests.RequestException as exc:
            log.warning("could not read robots.txt for %s: %s", origin, exc)
            parser.parse(["User-agent: *", "Disallow: /"])
        finally:
            _last_call = time.monotonic()
        _robots[origin] = parser
    parser = _robots[origin]
    return parser is None or parser.can_fetch(USER_AGENT, url)


def request_json(session: requests.Session, method: str, url: str, params: Optional[dict] = None,
                 body: Optional[dict] = None, delay: float = MIN_DELAY_SECONDS,
                 respect_robots: bool = False) -> Optional[Any]:
    """JSON request with rate limiting. Returns None on 404 (board does not exist).
    With respect_robots, raises RobotsDisallowed instead of fetching a forbidden URL."""
    global _last_call
    if respect_robots and not robots_allowed(session, url, delay):
        raise RobotsDisallowed(f"robots.txt disallows {url}")
    _throttle(delay)
    try:
        resp = session.request(method, url, params=params, json=body, timeout=TIMEOUT)
    finally:
        _last_call = time.monotonic()
    if resp.status_code == 404:
        log.warning("404 for %s", url)
        return None
    resp.raise_for_status()
    return resp.json()


def get_json(session: requests.Session, url: str, params: Optional[dict] = None,
             delay: float = MIN_DELAY_SECONDS, respect_robots: bool = False) -> Optional[Any]:
    return request_json(session, "GET", url, params=params, delay=delay,
                        respect_robots=respect_robots)


def post_json(session: requests.Session, url: str, body: dict,
              delay: float = MIN_DELAY_SECONDS, respect_robots: bool = False) -> Optional[Any]:
    return request_json(session, "POST", url, body=body, delay=delay,
                        respect_robots=respect_robots)


def load_details(source: str, slug: str, cache_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Per-posting detail cache. Postings are immutable enough that each detail is fetched once."""
    path = (cache_dir or CACHE_DIR) / f"{source}_{slug}_details.json"
    try:
        return json.loads(path.read_text()) if path.exists() else {}
    except ValueError:
        return {}


def save_details(source: str, slug: str, details: Dict[str, Any],
                 cache_dir: Optional[Path] = None) -> None:
    folder = cache_dir or CACHE_DIR
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{source}_{slug}_details.json").write_text(json.dumps(details))


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
