"""Adzuna scout (India). Needs ADZUNA_APP_ID and ADZUNA_APP_KEY in .env."""
from __future__ import annotations

import logging
import os
from typing import List, Optional

import requests

from config import ADZUNA_DAILY_CALLS, ADZUNA_MAX_CALLS_PER_RUN, ADZUNA_MONTHLY_CALLS
from scout.aggregator import api_get, run_queries
from scout.http import html_to_text, infer_remote_type, make_session
from scout.queries import Query
from scout.schema import Job
from scout.usage import UsageTracker

log = logging.getLogger(__name__)
SOURCE = "adzuna"
URL = "https://api.adzuna.com/v1/api/jobs/in/search/1"
LIMITS = {"month": ADZUNA_MONTHLY_CALLS, "day": ADZUNA_DAILY_CALLS}


def credentials() -> Optional[dict]:
    app_id, app_key = os.getenv("ADZUNA_APP_ID", ""), os.getenv("ADZUNA_APP_KEY", "")
    return {"app_id": app_id, "app_key": app_key} if app_id and app_key else None


def to_params(q: Query, creds: dict) -> dict:
    where = "Bengaluru" if q.location == "bengaluru" else "India"
    what = q.stem if q.location == "bengaluru" else f"{q.stem} remote"
    return {**creds, "results_per_page": 50, "what": what, "where": where,
            "sort_by": "date", "content-type": "application/json"}


def parse(raw: dict) -> List[Job]:
    jobs = []
    for item in raw.get("results", []):
        try:
            location = (item.get("location") or {}).get("display_name", "") or ""
            title = html_to_text(item["title"])
            jobs.append(Job.build(
                source=SOURCE, title=title,
                company=(item.get("company") or {}).get("display_name") or "Unknown",
                location=location, url=item["redirect_url"],
                remote_type=infer_remote_type(location, title),
                description=html_to_text(item.get("description", "")),
                posted_date=item.get("created"), jd_partial=True))
        except (KeyError, ValueError) as exc:
            log.warning("skipping malformed adzuna job: %s", exc)
    return jobs


def scout(queries: List[Query], tracker: UsageTracker,
          session: Optional[requests.Session] = None) -> List[Job]:
    creds = credentials()
    if creds is None:
        log.warning("adzuna: ADZUNA_APP_ID / ADZUNA_APP_KEY not set; skipping")
        return []
    session = session or make_session(retry_statuses=(500, 502, 503, 504))
    tracker.limits[SOURCE] = LIMITS
    return run_queries(SOURCE, queries, ADZUNA_MAX_CALLS_PER_RUN, tracker,
                       lambda q: parse(api_get(session, SOURCE, URL, to_params(q, creds))))
