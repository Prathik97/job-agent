"""Shared plumbing for metered aggregator APIs (Adzuna, JSearch)."""
from __future__ import annotations

import logging
import time
from typing import Callable, Dict, List, Optional

import requests

from scout.http import TIMEOUT
from scout.queries import Query, select_queries
from scout.schema import Job
from scout.usage import UsageTracker

log = logging.getLogger(__name__)


# Pacing: per-API time of the last call in this process. Tests replace these two.
_last_call: Dict[str, float] = {}
monotonic = time.monotonic
sleep = time.sleep


def pace(api: str, min_interval: float) -> None:
    """Wait until at least min_interval seconds have passed since this API's previous call."""
    if min_interval > 0 and api in _last_call:
        wait = min_interval - (monotonic() - _last_call[api])
        if wait > 0:
            sleep(wait)
    _last_call[api] = monotonic()


class QuotaStop(Exception):
    """Stop calling this API for the rest of the run (quota, rate limit or auth problem)."""


def api_get(session: requests.Session, api: str, url: str, params: dict,
            headers: Optional[dict] = None) -> dict:
    """GET JSON. Never puts the URL or keys in an error. Raises QuotaStop on 429/402/401/403."""
    try:
        resp = session.get(url, params=params, headers=headers, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise ValueError(f"{api} request failed: {exc.__class__.__name__}") from None
    if resp.status_code in (429, 402):
        raise QuotaStop(f"{api} quota or rate limit hit (HTTP {resp.status_code})")
    if resp.status_code in (401, 403):
        raise QuotaStop(f"{api} rejected the credentials or plan (HTTP {resp.status_code})")
    if resp.status_code >= 400:
        raise ValueError(f"{api} returned HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError:
        raise ValueError(f"{api} returned invalid JSON") from None


def run_queries(api: str, queries: List[Query], max_calls: int, tracker: UsageTracker,
                fetch: Callable[[Query], List[Job]], min_interval: float = 0.0) -> List[Job]:
    """Run a rotating slice of queries inside the budget. Never raises: on a quota error it
    logs and returns what it has so the other scouts continue."""
    picked, new_cursor = select_queries(queries, max_calls, tracker.cursor(api))
    found: Dict[str, Job] = {}
    for q in picked:
        if not tracker.allowed(api):
            log.warning("%s budget used up (%d calls this month); stopping %s",
                        api, tracker.month_calls(api), api)
            break
        tracker.record(api)
        pace(api, min_interval)
        try:
            for job in fetch(q):
                found.setdefault(job.id, job)
        except QuotaStop as exc:
            log.error("%s: stopping for the rest of this run", exc)
            break
        except ValueError as exc:
            log.error("%s query '%s/%s' failed: %s", api, q.stem, q.location, exc)
    else:
        tracker.set_cursor(api, new_cursor)
    log.info("%s: %d jobs from %d queries", api, len(found), len(picked))
    return list(found.values())
