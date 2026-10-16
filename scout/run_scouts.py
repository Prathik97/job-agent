"""Run the ATS scouts over companies.yaml.

Usage:  python -m scout.run_scouts [--companies companies.yaml] [--out data/raw/scout_jobs.json]
Idempotent: output is keyed by job id, so re-running never creates duplicates.
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
import yaml
from dotenv import load_dotenv

from scout import adzuna, ashby, greenhouse, jsearch, lever
from scout.http import cache_changed, make_session
from scout.normalize import AGGREGATOR_SOURCES, merge_sources
from scout.queries import build_queries
from scout.schema import Job
from scout.usage import USAGE_PATH, UsageTracker
from screener.cache import atomic_write_json

log = logging.getLogger(__name__)
SCOUTS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby}


def load_companies(path: Path) -> List[Tuple[str, str, str]]:
    """Return (source, slug, display name). Entries are 'slug' or {slug, name}."""
    data = yaml.safe_load(path.read_text()) or {}
    out = []
    for source in SCOUTS:
        for entry in data.get(source) or []:
            if isinstance(entry, str):
                entry = {"slug": entry}
            slug = entry["slug"]
            out.append((source, slug, entry.get("name") or slug.replace("-", " ").title()))
    return out


def scout_company(session: requests.Session, source: str, slug: str, name: str) -> List[Job]:
    module = SCOUTS[source]
    raw = module.fetch_raw(session, slug)
    if raw is None:
        return []
    changed = cache_changed(source, slug, raw)
    jobs = module.parse(raw, name)
    log.info("%s/%s: %d jobs (%s)", source, slug, len(jobs), "changed" if changed else "unchanged")
    return jobs


def load_previous(out_path: Path) -> List[Job]:
    if not out_path.exists():
        return []
    try:
        return [Job(**d) for d in json.loads(out_path.read_text())]
    except ValueError:
        log.warning("previous %s unreadable; ignoring it", out_path)
        return []


def run_aggregators(profile_path: Optional[Path], tracker: UsageTracker) -> List[Job]:
    """Adzuna then JSearch. A missing key, spent budget or quota error skips that API only."""
    if profile_path is None or not profile_path.exists():
        log.warning("no profile at %s; skipping aggregators", profile_path)
        return []
    queries = build_queries(yaml.safe_load(profile_path.read_text()))
    jobs: List[Job] = []
    for module in (adzuna, jsearch):
        jobs.extend(module.scout(queries, tracker))
    return jobs


def run(companies_path: Path, out_path: Path, profile_path: Optional[Path] = Path("profile.yaml"),
        usage_path: Path = USAGE_PATH, aggregators: bool = True) -> Dict[str, Job]:
    session = make_session()
    found: Dict[str, Job] = {}
    failures = 0
    for source, slug, name in load_companies(companies_path):
        try:
            jobs = scout_company(session, source, slug, name)
        except (requests.RequestException, ValueError) as exc:
            log.error("%s/%s failed: %s", source, slug, exc)
            failures += 1
            continue
        for job in jobs:
            found.setdefault(job.id, job)
    previous = load_previous(out_path)
    prev_by_id = {j.id: j for j in previous}
    if failures:
        # A failed fetch must not wipe earlier results: keep previous jobs we did not re-find.
        for job in previous:
            if job.source not in AGGREGATOR_SOURCES:
                found.setdefault(job.id, job)
        log.warning("%d fetch(es) failed; merged with previous output", failures)
    # Aggregator results are a rolling window (queries rotate, budgets run out), so earlier
    # aggregator jobs are kept and fresh ones replace them by id.
    agg = {j.id: j for j in previous if j.source in AGGREGATOR_SOURCES}
    if aggregators:
        agg.update({j.id: j for j in run_aggregators(profile_path, UsageTracker(
            usage_path, {adzuna.SOURCE: adzuna.LIMITS, jsearch.SOURCE: jsearch.LIMITS}))})
    for job in found.values():  # keep "also listed on" notes found in earlier runs
        old = prev_by_id.get(job.id)
        if old:
            job.also_listed_on = list(dict.fromkeys([*job.also_listed_on, *old.also_listed_on]))
    merged = {j.id: j for j in merge_sources([*found.values(), *agg.values()])}
    if not merged and previous:
        log.warning("scouts returned nothing; keeping previous %s untouched", out_path)
        return {j.id: j for j in previous}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(out_path, [j.model_dump() for j in merged.values()])
    log.info("wrote %d unique jobs to %s", len(merged), out_path)
    return merged


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--companies", type=Path, default=Path("companies.yaml"))
    parser.add_argument("--out", type=Path, default=Path("data/raw/scout_jobs.json"))
    parser.add_argument("--profile", type=Path, default=Path("profile.yaml"))
    parser.add_argument("--no-aggregators", action="store_true", help="ATS scouts only")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    load_dotenv()
    run(args.companies, args.out, args.profile, aggregators=not args.no_aggregators)


if __name__ == "__main__":
    main()
