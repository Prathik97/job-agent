"""Firecrawl scout: last resort for company career pages with no public ATS endpoint.

Usage:  python -m scout.firecrawl_scout [--dry-run] [--max-credits 40]

Flow per run (1 Firecrawl credit per page scraped, hard cap per run):
  1. Listing pages: robots.txt is checked in our own code before every call. Haiku extracts only
     title, location and link from each page's markdown.
  2. Your title and location rules (screener/rules.py) filter that list.
  3. Only jobs that pass get their full page fetched (one more credit each). Jobs whose full text
     was never fetched stay jd_partial.
Every page is cached 24h, so a re-run costs nothing. Output merges into firecrawl_jobs.json and
scout_jobs.json by id: a failed or empty response never replaces existing data.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional
from urllib.parse import urljoin, urlsplit

import requests
import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

from config import FIRECRAWL_CACHE_HOURS, FIRECRAWL_MAX_CREDITS_PER_RUN, HAIKU
from scout.http import CACHE_DIR, infer_remote_type, make_session, robots_allowed
from scout.run_scouts import load_previous
from scout.schema import Job, utcnow_iso
from scout.usage import USAGE_PATH, UsageTracker
from screener.cache import atomic_write_json
from screener.llm import ScreenError, Usage, ask_json, make_client
from screener.rules import Rules

log = logging.getLogger(__name__)
SOURCE = "firecrawl"
API = "https://api.firecrawl.dev/v1/scrape"
MAX_MARKDOWN_CHARS = 30000
MAX_JD_CHARS = 12000
FIRECRAWL_JOBS_PATH = Path("data/raw/firecrawl_jobs.json")
SCOUT_JOBS_PATH = Path("data/raw/scout_jobs.json")
# ATS-hosted tenant pages are a better listing than the marketing careers page.
TENANT_URLS = {"darwinbox": "https://{slug}.darwinbox.in/ms/candidate/careers",
               "trakstar": "https://{slug}.hire.trakstar.com/"}


class ListedJob(BaseModel):
    title: str
    location: str = ""
    url: str


class Listing(BaseModel):
    jobs: List[ListedJob] = Field(default_factory=list)


class CreditCap(Exception):
    """The per-run credit cap is reached; stop cleanly."""


class Budget:
    """Counts credits before each call (a failed call still costs) and records them."""

    def __init__(self, cap: int, tracker: UsageTracker):
        self.cap, self.used, self.tracker = cap, 0, tracker

    def spend(self) -> None:
        if self.used >= self.cap:
            raise CreditCap(f"credit cap of {self.cap} reached")
        self.used += 1
        self.tracker.record(SOURCE)


# ---- cache ----
def _cache_path(url: str, cache_dir: Path) -> Path:
    return cache_dir / f"firecrawl_{hashlib.sha256(url.encode()).hexdigest()[:20]}.json"


def cache_get(url: str, cache_dir: Path, now: datetime,
              hours: int = FIRECRAWL_CACHE_HOURS) -> Optional[dict]:
    path = _cache_path(url, cache_dir)
    try:
        entry = json.loads(path.read_text())
        fetched = datetime.fromisoformat(entry["fetched_at"])
    except (OSError, ValueError, KeyError):
        return None
    return entry if now - fetched < timedelta(hours=hours) else None


def cache_put(url: str, cache_dir: Path, entry: dict, now: datetime) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    _cache_path(url, cache_dir).write_text(json.dumps({**entry, "fetched_at": now.isoformat()}))


# ---- Firecrawl + extraction ----
def scrape(session: requests.Session, key: str, url: str, wait_ms: int = 0) -> Optional[str]:
    """Markdown of one page, or None on any failure or empty page. Never logs the key.
    wait_ms makes Firecrawl wait that long for JavaScript-rendered content (same credit cost)."""
    payload: dict = {"url": url, "formats": ["markdown"], "onlyMainContent": True}
    if wait_ms:
        payload["waitFor"] = wait_ms
    try:
        resp = session.post(API, json=payload,
                            headers={"Authorization": f"Bearer {key}"}, timeout=90)
        resp.raise_for_status()
        body = resp.json()
    except (requests.RequestException, ValueError) as exc:
        log.warning("firecrawl failed for %s: %s", url, type(exc).__name__)
        return None
    markdown = ((body.get("data") or {}).get("markdown") or "").strip() if body.get("success") else ""
    if not markdown:
        log.warning("firecrawl returned nothing for %s", url)
        return None
    return markdown


EXTRACT_SYSTEM = """You read the markdown of a company careers page and list its job postings.
Return ONLY JSON: {"jobs": [{"title": "...", "location": "...", "url": "..."}]}.
Include only individual job postings with their own link. Skip navigation, teams, blog and
social links. Use the link exactly as written. If a location is not shown, use "". If there are no
postings, return {"jobs": []}."""


def extract_listing(client, markdown: str, usage: Usage) -> List[ListedJob]:
    return ask_json(client, HAIKU, EXTRACT_SYSTEM, markdown[:MAX_MARKDOWN_CHARS],
                    lambda d: Listing(**d), usage, max_tokens=4000).jobs


# ---- pipeline ----
def eligible_pages(report: List[dict]) -> List[dict]:
    """Companies still needing Firecrawl whose page we may legitimately scrape: robots.txt allowed
    for the page, no bot-wall (HTTP error), and not blocked by the ATS endpoint's robots.txt."""
    pages = []
    for rec in report:
        note = rec.get("note") or ""
        if rec.get("endpoint_jobs") or rec.get("robots") != "allowed" or not rec.get("careers_url"):
            continue
        if "robots" in note or note.startswith("careers page returned HTTP") or "failed" in note:
            continue
        url = rec["careers_url"]
        if rec.get("ats") in TENANT_URLS and rec["company"] != "Darwinbox" and rec.get("slug"):
            url = TENANT_URLS[rec["ats"]].format(slug=rec["slug"])
        pages.append({"company": rec["company"], "url": url})
    return pages


def _cached_empty(url: str, cache_dir: Path, now: datetime) -> bool:
    cached = cache_get(url, cache_dir, now)
    return bool(cached) and cached.get("jobs") == []


def _listing_for(page: dict, fetch: Callable[[str], Optional[str]], client, usage: Usage,
                 cache_dir: Path, now: datetime, session,
                 retry_empty: bool = False) -> Optional[List[ListedJob]]:
    cached = cache_get(page["url"], cache_dir, now)
    if retry_empty and cached and cached.get("jobs") == []:
        cached = None
    if cached and cached.get("jobs") is not None:
        return [ListedJob(**j) for j in cached["jobs"]]
    markdown = cached["markdown"] if cached else None
    if markdown is None:
        if not robots_allowed(session, page["url"]):
            log.info("%s: robots.txt disallows %s; skipped", page["company"], page["url"])
            return None
        markdown = fetch(page["url"])
        if markdown is None:
            return None
        cache_put(page["url"], cache_dir, {"markdown": markdown}, now)
    try:
        jobs = extract_listing(client, markdown, usage)
    except ScreenError as exc:
        log.warning("%s: could not extract listing: %s", page["company"], exc)
        return None
    cache_put(page["url"], cache_dir, {"markdown": markdown, "jobs": [j.model_dump() for j in jobs]}, now)
    return jobs


def _build_job(company: str, listed: ListedJob, page_url: str) -> Optional[Job]:
    url = urljoin(page_url, listed.url.strip())
    if urlsplit(url).scheme not in ("http", "https") or not listed.title.strip():
        return None
    return Job.build(source=SOURCE, title=listed.title, company=company, location=listed.location,
                     url=url, remote_type=infer_remote_type(listed.location, listed.title),
                     jd_partial=True)


def _full_text(job: Job, fetch: Callable[[str], Optional[str]], cache_dir: Path, now: datetime,
               session) -> Optional[str]:
    cached = cache_get(job.url, cache_dir, now)
    if cached:
        return cached["markdown"]
    if not robots_allowed(session, job.url):
        log.info("robots.txt disallows %s; keeping %s partial", job.url, job.title)
        return None
    markdown = fetch(job.url)
    if markdown:
        cache_put(job.url, cache_dir, {"markdown": markdown}, now)
    return markdown


def merge_into(path: Path, fresh: List[Job]) -> int:
    """Upsert by id into a job file. Nothing fresh means the file is left untouched; a job that
    already has its full text is not downgraded to partial; first_seen_at is carried over."""
    if not fresh:
        return 0
    existing = {j.id: j for j in load_previous(path)}
    for job in fresh:
        old = existing.get(job.id)
        if old:
            job.first_seen_at = old.first_seen_at
            if job.jd_partial and not old.jd_partial:
                job.description, job.jd_partial = old.description, False
        existing[job.id] = job
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, [j.model_dump() for j in existing.values()])
    return len(fresh)


def run(report_path: Path, profile_path: Path, *, max_credits: int = FIRECRAWL_MAX_CREDITS_PER_RUN,
        dry_run: bool = False, wait_ms: int = 0, retry_empty: bool = False, cache_dir: Path = CACHE_DIR, usage_path: Path = USAGE_PATH,
        jobs_path: Path = FIRECRAWL_JOBS_PATH, scout_path: Path = SCOUT_JOBS_PATH,
        session: Optional[requests.Session] = None, client=None,
        now: Optional[datetime] = None) -> dict:
    now = now or datetime.now(timezone.utc)
    session = session or make_session(retry_statuses=())
    pages = eligible_pages(json.loads(report_path.read_text()))
    if retry_empty:  # only pages whose cached listing came back empty, and ignore that cache
        pages = [p for p in pages if _cached_empty(p["url"], cache_dir, now)]
    stats: dict = {"pages": len(pages), "credits_used": 0, "companies": {}, "capped": False}
    if dry_run:
        stats["planned_pages"] = pages
        return stats
    key = os.getenv("FIRECRAWL_API_KEY", "")
    if not key:
        raise RuntimeError("FIRECRAWL_API_KEY missing. Add it to .env.")
    rules = Rules.from_profile(yaml.safe_load(profile_path.read_text()))
    client = client or make_client()
    usage = Usage()
    budget = Budget(max_credits, UsageTracker(usage_path))

    def fetch(url: str) -> Optional[str]:
        budget.spend()
        return scrape(session, key, url, wait_ms)

    passed: List[Job] = []
    try:
        for page in pages:
            co = stats["companies"].setdefault(page["company"], {
                "listed": 0, "passed_rules": 0, "full_jd": 0})
            listed = _listing_for(page, fetch, client, usage, cache_dir, now, session, retry_empty)
            for item in listed or []:
                co["listed"] += 1
                job = _build_job(page["company"], item, page["url"])
                if job and rules.evaluate(job).status != "drop":
                    co["passed_rules"] += 1
                    passed.append(job)
        for job in passed:
            text = _full_text(job, fetch, cache_dir, now, session)
            if text:
                job.description, job.jd_partial = text[:MAX_JD_CHARS], False
                stats["companies"][job.company]["full_jd"] += 1
    except CreditCap as exc:
        stats["capped"] = True
        log.warning("%s; stopping cleanly, remaining jobs stay jd_partial", exc)
    # a full text can reveal a location/years rejection the title alone could not
    kept = [j for j in passed if j.jd_partial or rules.evaluate(j).status != "drop"]
    stats["dropped_after_full_jd"] = len(passed) - len(kept)
    stats["credits_used"] = budget.used
    stats["haiku_cost_usd"] = round(usage.cost(), 4)
    stats["jobs"] = [j.id for j in kept]
    merge_into(jobs_path, kept)
    merge_into(scout_path, kept)
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=Path("data/raw/careers_report.json"))
    parser.add_argument("--profile", type=Path, default=Path("profile.yaml"))
    parser.add_argument("--max-credits", type=int, default=FIRECRAWL_MAX_CREDITS_PER_RUN)
    parser.add_argument("--wait-ms", type=int, default=0, help="Firecrawl waitFor for JS pages")
    parser.add_argument("--retry-empty", action="store_true",
                        help="re-scrape only pages whose cached listing was empty")
    parser.add_argument("--dry-run", action="store_true", help="list pages, spend nothing")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    load_dotenv()
    stats = run(args.report, args.profile, max_credits=args.max_credits, dry_run=args.dry_run,
                wait_ms=args.wait_ms, retry_empty=args.retry_empty)
    print(json.dumps({k: v for k, v in stats.items() if k != "jobs"}, indent=1, default=str))


if __name__ == "__main__":
    main()
