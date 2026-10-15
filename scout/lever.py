"""Lever scout: public postings JSON endpoint."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import List, Optional

import requests

from scout.http import get_json, infer_remote_type
from scout.schema import Job

log = logging.getLogger(__name__)
SOURCE = "lever"
URL = "https://api.lever.co/v0/postings/{slug}"


def fetch_raw(session: requests.Session, slug: str) -> Optional[list]:
    return get_json(session, URL.format(slug=slug), params={"mode": "json"})


def _ms_to_iso(ms) -> Optional[str]:
    if not isinstance(ms, (int, float)):
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat(timespec="seconds")


def parse(raw: list, company: str) -> List[Job]:
    jobs = []
    for item in raw or []:
        try:
            cats = item.get("categories") or {}
            location = cats.get("location") or ""
            remote = item.get("workplaceType") or ""
            jobs.append(Job.build(
                source=SOURCE, title=item["text"], company=company, location=location,
                url=item["hostedUrl"],
                remote_type=infer_remote_type(location, remote, cats.get("commitment", "")),
                description=item.get("descriptionPlain") or "",
                posted_date=_ms_to_iso(item.get("createdAt")),
            ))
        except (KeyError, ValueError) as exc:
            log.warning("skipping malformed lever job for %s: %s", company, exc)
    return jobs
