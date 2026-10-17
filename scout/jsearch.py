"""JSearch (RapidAPI) scout, India. Needs RAPIDAPI_KEY in .env."""
from __future__ import annotations

import logging
import os
from typing import List, Optional

import requests

from config import JSEARCH_MAX_CALLS_PER_RUN, JSEARCH_MONTHLY_CALLS
from scout.aggregator import api_get, run_queries
from scout.http import html_to_text, make_session
from scout.queries import Query
from scout.schema import Job
from scout.usage import UsageTracker

log = logging.getLogger(__name__)
SOURCE = "jsearch"
URL = "https://jsearch.p.rapidapi.com/search-v2"   # /search now answers 404 "Endpoint does not exist"
HOST = "jsearch.p.rapidapi.com"
LIMITS = {"month": JSEARCH_MONTHLY_CALLS}


def to_params(q: Query) -> dict:
    if q.location == "bengaluru":
        query, remote = f"{q.stem} in Bengaluru, India", "false"
    else:
        query, remote = f"{q.stem} remote in India", "true"
    return {"query": query, "page": 1, "num_pages": 1, "country": "in",
            "date_posted": "month", "work_from_home": remote}


def parse(raw: dict) -> List[Job]:
    jobs = []
    data = raw.get("data") or []
    items = data.get("jobs", []) if isinstance(data, dict) else data   # search-v2 nests jobs under data
    for item in items:
        try:
            parts = [item.get("job_city"), item.get("job_state"), item.get("job_country")]
            location = ", ".join(p for p in parts if p)
            remote = bool(item.get("job_is_remote"))
            if remote:
                location = f"Remote, {location}" if location else "Remote"
            jobs.append(Job.build(
                source=SOURCE, title=item["job_title"], company=item.get("employer_name") or "Unknown",
                location=location, url=item["job_apply_link"],
                remote_type="remote" if remote else "unknown",
                description=html_to_text(item.get("job_description", "")),
                posted_date=item.get("job_posted_at_datetime_utc"), jd_partial=True))
        except (KeyError, ValueError) as exc:
            log.warning("skipping malformed jsearch job: %s", exc)
    return jobs


def scout(queries: List[Query], tracker: UsageTracker,
          session: Optional[requests.Session] = None) -> List[Job]:
    key = os.getenv("RAPIDAPI_KEY", "")
    if not key:
        log.warning("jsearch: RAPIDAPI_KEY not set; skipping")
        return []
    session = session or make_session(retry_statuses=(500, 502, 503, 504))
    tracker.limits[SOURCE] = LIMITS
    headers = {"X-RapidAPI-Key": key, "X-RapidAPI-Host": HOST}
    return run_queries(SOURCE, queries, JSEARCH_MAX_CALLS_PER_RUN, tracker,
                       lambda q: parse(api_get(session, SOURCE, URL, to_params(q), headers)))
