"""Workday scout: public CXS job-search endpoint of a company's myworkdayjobs.com site.

The companies.yaml slug is `tenant.wdN.site`, for example `browserstack.wd3.External`, which maps
to https://browserstack.wd3.myworkdayjobs.com/External. The list call has no description or exact
date, so each posting's detail is fetched once and cached.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

import requests

from scout.http import (get_json, html_to_text, infer_remote_type, load_details, post_json,
                        save_details)
from scout.schema import Job

log = logging.getLogger(__name__)
SOURCE = "workday"
PAGE_SIZE = 20  # Workday rejects larger pages
MAX_JOBS = 500
_AGO = re.compile(r"posted\s+(\d+)\+?\s+(day|week|month)s?\s+ago", re.I)


def split_slug(slug: str) -> Tuple[str, str, str]:
    tenant, wd, site = slug.split(".", 2)
    return tenant, wd, site


def base_url(slug: str) -> str:
    tenant, wd, site = split_slug(slug)
    return f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"


def public_url(slug: str, external_path: str) -> str:
    tenant, wd, site = split_slug(slug)
    return f"https://{tenant}.{wd}.myworkdayjobs.com/{site}{external_path}"


def fetch_raw(session: requests.Session, slug: str) -> Optional[dict]:
    base = base_url(slug)
    postings: List[dict] = []
    total = None
    while total is None or len(postings) < min(total, MAX_JOBS):
        page = post_json(session, f"{base}/jobs", {"appliedFacets": {}, "limit": PAGE_SIZE,
                         "offset": len(postings), "searchText": ""}, respect_robots=True)
        if page is None:
            return None
        batch = page.get("jobPostings") or []
        total = page.get("total", len(postings) + len(batch))
        if not batch:
            break
        postings.extend(batch)
    details = load_details(SOURCE, slug)
    fresh = False
    for item in postings:
        path = item.get("externalPath")
        if not path or path in details:
            continue
        detail = get_json(session, f"{base}{path}", respect_robots=True)
        if detail:
            info = detail.get("jobPostingInfo") or {}
            details[path] = {k: info.get(k) for k in
                             ("jobDescription", "location", "startDate", "externalUrl", "remoteType")}
            fresh = True
    if fresh:
        save_details(SOURCE, slug, details)
    return {"slug": slug, "postings": postings,
            "details": {i["externalPath"]: details[i["externalPath"]]
                        for i in postings if i.get("externalPath") in details}}


def _relative_date(text: str, now: datetime) -> Optional[str]:
    """'Posted Today' / 'Posted 3 Days Ago' to an ISO date; None when it cannot be told."""
    lowered = (text or "").lower()
    if "today" in lowered:
        days = 0
    elif "yesterday" in lowered:
        days = 1
    else:
        m = _AGO.search(lowered)
        if not m:
            return None
        days = int(m.group(1)) * {"day": 1, "week": 7, "month": 30}[m.group(2)]
    return (now - timedelta(days=days)).date().isoformat()


def parse(raw: dict, company: str, now: Optional[datetime] = None) -> List[Job]:
    now = now or datetime.now(timezone.utc)
    slug = raw["slug"]
    jobs = []
    for item in raw.get("postings", []):
        try:
            path = item["externalPath"]
            detail = (raw.get("details") or {}).get(path) or {}
            location = detail.get("location") or item.get("locationsText") or ""
            jobs.append(Job.build(
                source=SOURCE, title=item["title"], company=company, location=location,
                url=detail.get("externalUrl") or public_url(slug, path),
                remote_type=infer_remote_type(location, str(detail.get("remoteType") or "")),
                description=html_to_text(detail.get("jobDescription") or ""),
                posted_date=detail.get("startDate") or _relative_date(item.get("postedOn", ""), now),
            ))
        except (KeyError, ValueError) as exc:
            log.warning("skipping malformed workday job for %s: %s", company, exc)
    return jobs
