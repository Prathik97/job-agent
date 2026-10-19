"""One command: scouts -> Screener -> stats.

Usage:  python run_pipeline.py [--dry-run] [--skip-scout] [--limit N] [--max-cost USD]
--dry-run reads the existing scout output, makes no network or API calls and writes nothing;
it prints the funnel by source and the estimated cost. A real run aborts before any Stage 1 or
Stage 2 call if the estimate is above --max-cost (default MAX_COST_PER_RUN in config.py).
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

import yaml
from dotenv import load_dotenv

from config import MAX_COST_PER_RUN
from scout import run_scouts
from scout.logsafe import configure_logging
from screener import run_screener
from screener.cache import atomic_write_json
from screener.rules import Rules
from screener.stats import build_filtered_out, build_stats_line, layer_counts

log = logging.getLogger("pipeline")
STAGES = ("jobs_in", "after_rules", "after_stage1", "scored")


def load_screened(path: Path) -> Dict[str, dict]:
    return {r["id"]: r for r in json.loads(path.read_text())} if path.exists() else {}


def log_source_funnel(layers: dict) -> None:
    log.info("FUNNEL BY SOURCE")
    log.info("  %-12s%8s%13s%14s%8s", "source", "in", "after rules", "after stage1", "scored")
    for s in layers["sources"]:
        log.info("  %-12s%8d%13d%14d%8d", s, *(layers[k][s] for k in STAGES))
    totals = [sum(layers[k].values()) for k in STAGES]
    log.info("  %-12s%8d%13d%14d%8d", "TOTAL", *totals)
    log.info("  verdicts: %s", ", ".join(f"{k} {v}" for k, v in layers["verdicts"].items()))


def record_failure(clean_dir: Path, exc: Exception, trigger: str) -> None:
    """A crashed run leaves one line in run_stats.jsonl (error type only, never keys or text)."""
    clean_dir.mkdir(parents=True, exist_ok=True)
    line = {"timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"), "dry_run": False,
            "aborted": False, "failed": True, "error": type(exc).__name__, "trigger": trigger}
    with (clean_dir / "run_stats.jsonl").open("a") as fh:
        fh.write(json.dumps(line) + "\n")


def run(*, companies: Path, profile: Path, jobs: Path, screened: Path, cache: Path,
        clean_dir: Path, usage: Path, dry_run: bool = False, skip_scout: bool = False,
        limit: Optional[int] = None, max_cost: float = MAX_COST_PER_RUN, client=None,
        trigger: str = "cli") -> int:
    """Returns a process exit code: 0 ok, 1 failed, 2 stopped by the cost cap. A failed run
    writes nothing over existing results: scout and screener output files are only replaced
    atomically at the end of their own step, and the failure is logged to run_stats.jsonl."""
    try:
        return _run(companies=companies, profile=profile, jobs=jobs, screened=screened, cache=cache,
                    clean_dir=clean_dir, usage=usage, dry_run=dry_run, skip_scout=skip_scout,
                    limit=limit, max_cost=max_cost, client=client, trigger=trigger)
    except Exception as exc:  # noqa: BLE001 - any crash must be recorded, not lost
        log.error("pipeline failed: %s", type(exc).__name__)
        if not dry_run:
            record_failure(clean_dir, exc, trigger)
        return 1


def _run(*, companies: Path, profile: Path, jobs: Path, screened: Path, cache: Path,
         clean_dir: Path, usage: Path, dry_run: bool, skip_scout: bool,
         limit: Optional[int], max_cost: float, client, trigger: str) -> int:
    started = datetime.now(timezone.utc)
    if not (dry_run or skip_scout):
        run_scouts.run(companies, jobs, profile, usage_path=usage, adzuna_weekly_only=(trigger == "scheduled"))
    elif not jobs.exists():
        log.error("no scout output at %s; run without --dry-run / --skip-scout first", jobs)
        return 1
    rules = Rules.from_profile(yaml.safe_load(profile.read_text()))
    all_jobs = run_screener.load_jobs(jobs)

    aborted = False
    try:
        funnel = run_screener.run(jobs, screened, profile, cache, dry_run=dry_run, limit=limit,
                                  max_cost=max_cost, client=client)
    except run_screener.SpendGuardError as exc:
        log.error("ABORTED: %s", exc)
        aborted, funnel = True, {}

    rows = load_screened(screened)
    layers = layer_counts(all_jobs, rules, rows)
    log_source_funnel(layers)
    if dry_run:
        log.info("dry run: estimated cost $%.3f, nothing written", funnel["est_cost_total"])
        return 0
    atomic_write_json(clean_dir / "filtered_out.json", build_filtered_out(all_jobs, rules, rows))
    stats = {**build_stats_line(layers, funnel, dry_run=False, aborted=aborted), "trigger": trigger,
             # `timestamp` is when the run finished; these two show when it really started, which
             # for a scheduled run on a sleeping Mac can be well after 8:30.
             "started_at": started.isoformat(timespec="seconds"),
             "duration_s": round((datetime.now(timezone.utc) - started).total_seconds())}
    with (clean_dir / "run_stats.jsonl").open("a") as fh:
        fh.write(json.dumps(stats) + "\n")
    if funnel.get("stopped_at_cost_cap"):
        log.warning("stopped at the cost cap; the remaining jobs are screened next run")
    if not aborted:
        log.info("cost this run: $%.3f (%d stage 1 calls, %d stage 2 calls)", stats["cost_usd"],
                 stats["stage1_calls"], stats["stage2_calls"])
    return 2 if aborted else 0


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--skip-scout", action="store_true", help="screen the existing scout output")
    p.add_argument("--limit", type=int, help="screen at most N jobs that survive the rules")
    p.add_argument("--max-cost", type=float, default=MAX_COST_PER_RUN,
                   help="abort if the estimated cost is above this (USD)")
    p.add_argument("--trigger", default="cli", help="who started this run (cli, scheduled, dashboard)")
    p.add_argument("--companies", type=Path, default=Path("companies.yaml"))
    p.add_argument("--profile", type=Path, default=Path("profile.yaml"))
    p.add_argument("--data", type=Path, default=Path("data"))
    a = p.parse_args()
    configure_logging()
    load_dotenv()
    clean = a.data / "clean"
    sys.exit(run(companies=a.companies, profile=a.profile, jobs=a.data / "raw/scout_jobs.json",
                 screened=clean / "screened_jobs.json", cache=clean / "screener_cache.json",
                 clean_dir=clean, usage=a.data / "raw/api_usage.json", dry_run=a.dry_run,
                 skip_scout=a.skip_scout, limit=a.limit, max_cost=a.max_cost, trigger=a.trigger))


if __name__ == "__main__":
    main()
