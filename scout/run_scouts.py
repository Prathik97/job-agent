"""Run the ATS scouts over companies.yaml.

Usage:  python -m scout.run_scouts [--companies companies.yaml] [--out data/raw/scout_jobs.json]
Idempotent: output is keyed by job id, so re-running never creates duplicates.
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Dict, List, Tuple

import requests
import yaml

from scout import ashby, greenhouse, lever
from scout.http import cache_changed, make_session
from scout.schema import Job

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


def run(companies_path: Path, out_path: Path) -> Dict[str, Job]:
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
    if failures and out_path.exists():
        # A failed fetch must not wipe earlier results: keep previous jobs we did not re-find.
        for item in json.loads(out_path.read_text()):
            found.setdefault(item["id"], Job(**item))
        log.warning("%d fetch(es) failed; merged with previous output", failures)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps([j.model_dump() for j in found.values()], indent=2))
    log.info("wrote %d unique jobs to %s", len(found), out_path)
    return found


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--companies", type=Path, default=Path("companies.yaml"))
    parser.add_argument("--out", type=Path, default=Path("data/raw/scout_jobs.json"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    run(args.companies, args.out)


if __name__ == "__main__":
    main()
