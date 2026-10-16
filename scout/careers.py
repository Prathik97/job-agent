"""Careers-page discovery: for companies with no ATS board, find which ATS they use.

Usage:  python -m scout.careers [--firecrawl data/raw/needs_firecrawl.json]
                                [--pages careers_pages.yaml] [--report data/raw/careers_report.json]

For each company: check robots.txt, fetch the careers page, detect the ATS from the final URL and
the links in the page, then make ONE real call to that ATS's public endpoint to confirm it
returns jobs. Nothing is assumed. Companies that stay unresolved remain in needs_firecrawl.json.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
import yaml

from scout import ashby, greenhouse, lever, smartrecruiters, workday
from scout.http import (RobotsDisallowed, get_json, make_session, post_json, request_json,
                        robots_allowed)
from screener.cache import atomic_write_json

log = logging.getLogger(__name__)

# (ats, regex). The first capture group is the board slug (workday: tenant, wdN, site).
DETECTORS: List[Tuple[str, "re.Pattern[str]"]] = [(a, re.compile(p, re.I)) for a, p in [
    ("greenhouse", r"(?:job-)?boards\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_-]+)"),
    ("lever", r"jobs\.lever\.co/([a-z0-9_-]+)"),
    ("ashby", r"jobs\.ashbyhq\.com/([a-z0-9_.-]+)"),
    ("smartrecruiters", r"(?:careers|jobs)\.smartrecruiters\.com/([A-Za-z0-9_-]+)"),
    ("workday", r"([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)"),
    ("workable", r"apply\.workable\.com/([a-z0-9_-]+)"),
    ("recruitee", r"([a-z0-9-]+)\.recruitee\.com"),
    ("freshteam", r"([a-z0-9-]+)\.freshteam\.com"),
    ("darwinbox", r"([a-z0-9-]+)\.darwinbox\.(?:in|com)/ms/"),
    ("keka", r"([a-z0-9-]+)\.keka\.com/careers"),
    ("zoho_recruit", r"([a-z0-9-]+)\.zohorecruit\.(?:com|in)"),
    ("trakstar", r"([a-z0-9-]+)\.hire\.trakstar\.com"),
    ("turbohire", r"([a-z0-9-]+)\.turbohire\.co"),
    ("kula", r"careers\.kula\.ai/([a-z0-9_-]+)"),
]]


def detect_ats(final_url: str, html: str) -> Tuple[Optional[str], Optional[str]]:
    """(ats, slug) from the page URL, then links in the page. (None, None) if none matches."""
    for text in (final_url, html):
        for ats, pattern in DETECTORS:
            m = pattern.search(text or "")
            if m:
                slug = ".".join(m.groups()) if ats == "workday" else m.group(1)
                return ats, slug
    return None, None


def probe_endpoint(session: requests.Session, ats: str, slug: str) -> Tuple[Optional[int], str]:
    """One real call to the ATS's public endpoint. Returns (job count or None, note)."""
    try:
        if ats == "greenhouse":
            raw = get_json(session, greenhouse.URL.format(slug=slug), respect_robots=True)
            count = len(raw["jobs"]) if raw else None
        elif ats == "lever":
            raw = get_json(session, lever.URL.format(slug=slug), params={"mode": "json", "limit": 5},
                           respect_robots=True)
            count = len(raw) if raw is not None else None
        elif ats == "ashby":
            raw = get_json(session, ashby.URL.format(slug=slug), respect_robots=True)
            count = len(raw["jobs"]) if raw else None
        elif ats == "smartrecruiters":
            raw = get_json(session, smartrecruiters.URL.format(slug=slug), params={"limit": 1},
                           respect_robots=True)
            count = raw.get("totalFound") if raw else None
        elif ats == "workday":
            raw = post_json(session, f"{workday.base_url(slug)}/jobs", {
                "appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""},
                respect_robots=True)
            count = raw.get("total") if raw else None
        elif ats == "freshteam":
            url = f"https://{slug}.freshteam.com/hire/widgets/jobs.json"
            raw = request_json(session, "GET", url, respect_robots=True)
            count = len(raw["jobs"]) if raw else None
        else:
            return None, "no public JSON endpoint known or confirmed"
    except RobotsDisallowed as exc:
        return None, f"endpoint blocked by robots.txt ({exc})"
    except (requests.RequestException, ValueError, KeyError) as exc:
        return None, f"endpoint call failed: {type(exc).__name__}"
    if not count:
        return None, "endpoint answered but returned no jobs"
    return count, "confirmed"


def check_company(session: requests.Session, company: str, url: str) -> dict:
    rec: dict = {"company": company, "careers_url": url, "robots": "allowed", "ats": None,
                 "slug": None, "endpoint_jobs": None, "note": ""}
    try:
        if not robots_allowed(session, url):
            rec.update(robots="disallowed", note="careers page disallowed by robots.txt (or robots.txt unreadable); skipped")
            return rec
        page = session.get(url, timeout=20)
    except requests.RequestException as exc:
        rec.update(note=f"fetch failed: {type(exc).__name__}")
        return rec
    rec["careers_url"] = page.url
    if page.status_code != 200:
        rec["note"] = f"careers page returned HTTP {page.status_code}"
        return rec
    rec["ats"], rec["slug"] = detect_ats(page.url, page.text)
    if rec["ats"]:
        rec["endpoint_jobs"], rec["note"] = probe_endpoint(session, rec["ats"], rec["slug"])
    else:
        rec["note"] = "no known ATS found in page URL or links (custom or JS-rendered page)"
    return rec


def merge_report(path: Path, fresh: List[dict]) -> List[dict]:
    """Update the report by company; companies not re-checked keep their earlier entry."""
    old = {r["company"]: r for r in json.loads(path.read_text())} if path.exists() else {}
    for rec in fresh:
        old[rec["company"]] = rec
    return list(old.values())


def leftovers(firecrawl: List[dict], report: List[dict], resolved: set) -> List[dict]:
    """needs_firecrawl entries still unresolved, annotated with what discovery found."""
    by_co = {r["company"]: r for r in report}
    out = []
    for entry in firecrawl:
        name = entry["company"]
        if name in resolved:
            continue
        rec = by_co.get(name, {})
        out.append({**entry, "careers_url": rec.get("careers_url"), "ats_detected": rec.get("ats"),
                    "robots": rec.get("robots"), "discovery_note": rec.get("note")})
    return out


def run(firecrawl_path: Path, pages_path: Path, report_path: Path,
        resolved: Optional[set] = None, session: Optional[requests.Session] = None) -> List[dict]:
    session = session or make_session()
    pages: Dict[str, str] = yaml.safe_load(pages_path.read_text()) or {}
    firecrawl = json.loads(firecrawl_path.read_text())
    fresh = []
    for entry in firecrawl:
        url = pages.get(entry["company"])
        if not url:
            fresh.append({"company": entry["company"], "careers_url": None, "robots": "n/a",
                          "ats": None, "slug": None, "endpoint_jobs": None,
                          "note": "no careers URL on file"})
            continue
        fresh.append(check_company(session, entry["company"], url))
    report = merge_report(report_path, fresh)
    atomic_write_json(report_path, report)
    if resolved is not None:  # the caller names companies already added to companies.yaml
        atomic_write_json(firecrawl_path, leftovers(firecrawl, report, resolved))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--firecrawl", type=Path, default=Path("data/raw/needs_firecrawl.json"))
    parser.add_argument("--pages", type=Path, default=Path("careers_pages.yaml"))
    parser.add_argument("--report", type=Path, default=Path("data/raw/careers_report.json"))
    parser.add_argument("--resolved", nargs="*", default=None,
                        help="company names now in companies.yaml; removed from needs_firecrawl")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    report = run(args.firecrawl, args.pages, args.report,
                 set(args.resolved) if args.resolved is not None else None)
    for r in report:
        print(f"{r['company']:24} {str(r['ats']):16} jobs={r['endpoint_jobs']}  {r['note']}")


if __name__ == "__main__":
    main()
