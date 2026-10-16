"""SmartRecruiters scout: public Posting API (list plus one detail call per new posting).

api.smartrecruiters.com/robots.txt currently disallows generic crawlers, so every call is gated
on robots.txt and the scout raises RobotsDisallowed until that changes.
"""
from __future__ import annotations

import logging
from typing import List, Optional

import requests

from scout.http import get_json, html_to_text, infer_remote_type, load_details, save_details
from scout.schema import Job

log = logging.getLogger(__name__)
SOURCE = "smartrecruiters"
URL = "https://api.smartrecruiters.com/v1/companies/{slug}/postings"
PAGE_SIZE = 100
MAX_JOBS = 1000
SECTIONS = ("companyDescription", "jobDescription", "qualifications", "additionalInformation")


def fetch_raw(session: requests.Session, slug: str) -> Optional[dict]:
    postings: List[dict] = []
    total = None
    while total is None or len(postings) < min(total, MAX_JOBS):
        page = get_json(session, URL.format(slug=slug),
                        params={"limit": PAGE_SIZE, "offset": len(postings)}, respect_robots=True)
        if page is None:
            return None
        batch = page.get("content") or []
        total = page.get("totalFound", len(postings) + len(batch))
        if not batch:
            break
        postings.extend(batch)
    details = load_details(SOURCE, slug)
    fresh = False
    for item in postings:
        if item["id"] in details:
            continue
        detail = get_json(session, f"{URL.format(slug=slug)}/{item['id']}", respect_robots=True)
        if detail:
            details[item["id"]] = {"postingUrl": detail.get("postingUrl"),
                                   "sections": (detail.get("jobAd") or {}).get("sections") or {}}
            fresh = True
    if fresh:
        save_details(SOURCE, slug, details)
    return {"slug": slug, "postings": postings,
            "details": {i["id"]: details[i["id"]] for i in postings if i["id"] in details}}


def _location(loc: dict) -> str:
    if loc.get("fullLocation"):
        return loc["fullLocation"]
    return ", ".join(p for p in (loc.get("city"), loc.get("region"), loc.get("country")) if p)


def parse(raw: dict, company: str) -> List[Job]:
    jobs = []
    for item in raw.get("postings", []):
        try:
            detail = (raw.get("details") or {}).get(item["id"]) or {}
            loc = item.get("location") or {}
            location = _location(loc)
            sections = detail.get("sections") or {}
            text = "\n".join(html_to_text((sections.get(k) or {}).get("text", ""))
                             for k in SECTIONS if sections.get(k))
            hint = "remote" if loc.get("remote") else "hybrid" if loc.get("hybrid") else ""
            jobs.append(Job.build(
                source=SOURCE, title=item["name"], company=company, location=location,
                url=detail.get("postingUrl") or
                f"https://jobs.smartrecruiters.com/{item['company']['identifier']}/{item['id']}",
                remote_type=infer_remote_type(location, hint),
                description=text, posted_date=item.get("releasedDate"),
            ))
        except (KeyError, ValueError) as exc:
            log.warning("skipping malformed smartrecruiters job for %s: %s", company, exc)
    return jobs
