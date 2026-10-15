"""Ashby scout: public posting-api job board endpoint."""
from __future__ import annotations

import logging
from typing import List, Optional

import requests

from scout.http import get_json, infer_remote_type
from scout.schema import Job

log = logging.getLogger(__name__)
SOURCE = "ashby"
URL = "https://api.ashbyhq.com/posting-api/job-board/{slug}"


def fetch_raw(session: requests.Session, slug: str) -> Optional[dict]:
    return get_json(session, URL.format(slug=slug))


def parse(raw: dict, company: str) -> List[Job]:
    jobs = []
    for item in raw.get("jobs", []):
        if item.get("isListed") is False:
            continue
        try:
            location = item.get("location") or ""
            remote = "remote" if item.get("isRemote") else ""
            jobs.append(Job.build(
                source=SOURCE, title=item["title"], company=company, location=location,
                url=item["jobUrl"],
                remote_type=infer_remote_type(location, item.get("workplaceType", ""), remote),
                description=item.get("descriptionPlain") or "",
                posted_date=item.get("publishedAt"),
            ))
        except (KeyError, ValueError) as exc:
            log.warning("skipping malformed ashby job for %s: %s", company, exc)
    return jobs
