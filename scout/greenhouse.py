"""Greenhouse scout: public boards-api JSON endpoint."""
from __future__ import annotations

import logging
from typing import List, Optional

import requests

from scout.http import get_json, html_to_text, infer_remote_type
from scout.schema import Job

log = logging.getLogger(__name__)
SOURCE = "greenhouse"
URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"


def fetch_raw(session: requests.Session, slug: str) -> Optional[dict]:
    return get_json(session, URL.format(slug=slug), params={"content": "true"})


def parse(raw: dict, company: str) -> List[Job]:
    jobs = []
    for item in raw.get("jobs", []):
        try:
            location = (item.get("location") or {}).get("name", "")
            jobs.append(Job.build(
                source=SOURCE, title=item["title"], company=company, location=location,
                url=item["absolute_url"],
                remote_type=infer_remote_type(location, item["title"]),
                description=html_to_text(item.get("content", "")),
                posted_date=item.get("first_published") or item.get("updated_at"),
            ))
        except (KeyError, ValueError) as exc:
            log.warning("skipping malformed greenhouse job for %s: %s", company, exc)
    return jobs
