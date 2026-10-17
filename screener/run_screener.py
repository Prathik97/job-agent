"""Run the three screening layers.

Usage:  python -m screener.run_screener [--dry-run] [--limit N] [--retry-failed]
Input:  data/raw/scout_jobs.json    Output: data/clean/screened_jobs.json
Results are cached by job id + description hash, so reruns never re-spend API credit.
"""
from __future__ import annotations

import argparse
import json
import logging
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import anthropic
import yaml

from config import HAIKU, MAX_COST_PER_RUN, SONNET
from scout.schema import Job
from screener import stage1, stage2
from screener.freshness import posting_age_days
from screener.cache import Cache, atomic_write_json, cache_key
from screener.llm import ScreenError, Usage, cost_usd, make_client
from screener.models import RuleResult, Stage1Decision, Stage2Result
from screener.rules import Rules
from screener.text import est_tokens
from config import ASSUMED_STAGE1_KEEP_RATE

log = logging.getLogger(__name__)
TIER_ORDER = {"A": 0, "B": 1, "C": 2, "untiered": 3}
Candidate = Tuple[Job, RuleResult]


class SpendGuardError(RuntimeError):
    """Estimated cost is above the per-run limit; aborted before any API call."""


def load_jobs(path: Path) -> List[Job]:
    return [Job(**d) for d in json.loads(path.read_text())]


def key1(job: Job) -> str:
    return cache_key("stage1", HAIKU, stage1.VERSION, job.id, job.description, stage1.PROMPT_HASH)


def key2(job: Job, ctx: stage2.ScoringContext) -> str:
    return cache_key("stage2", SONNET, stage2.VERSION, job.id, job.description, ctx.hash)


def _usable(entry: Optional[dict], retry_failed: bool) -> bool:
    """A cache entry counts unless it is a recorded failure and the user asked to retry."""
    return entry is not None and not (entry.get("failed") and retry_failed)


def build_funnel(evaluated: List[Candidate], candidates: List[Candidate], cache: Cache,
                 ctx: stage2.ScoringContext, retry_failed: bool) -> dict:
    """Layer counts, cache hits and estimated API cost. Makes no API call."""
    drops = Counter(r.drop_category for _, r in evaluated if r.status == "drop")
    s1 = [(j, r) for j, r in candidates if r.status == "stage1"]
    direct = [(j, r) for j, r in candidates if r.status == "stage2"]
    s1_cached = [(j, r) for j, r in s1 if _usable(cache.get(key1(j)), retry_failed)]
    s1_calls = [(j, r) for j, r in s1 if (j, r) not in s1_cached]

    known_s2 = list(direct)
    for j, r in s1_cached:
        entry = cache.get(key1(j))
        if not entry.get("failed") and entry.get("keep"):
            known_s2.append((j, r))
    s2_calls_known = [(j, r) for j, r in known_s2 if not _usable(cache.get(key2(j, ctx)), retry_failed)]

    def s1_tokens(j, r):
        return est_tokens(stage1.SYSTEM + stage1.build_user(j, r)), 60

    cost1 = sum(cost_usd(HAIKU, *s1_tokens(j, r)) for j, r in s1_calls)
    cost2_known = sum(stage2.est_cost(ctx, j, r) for j, r in s2_calls_known)
    cost2_extra = ASSUMED_STAGE1_KEEP_RATE * sum(stage2.est_cost(ctx, j, r) for j, r in s1_calls)
    return {
        "total_jobs": len(evaluated),
        "dropped_by_rules": sum(drops.values()),
        "drop_reasons": dict(drops.most_common()),
        "survivors_after_rules": sum(1 for _, r in evaluated if r.status != "drop"),
        "processing_now": len(candidates),
        "to_stage1": len(s1), "stage1_cached": len(s1_cached), "stage1_calls": len(s1_calls),
        "direct_to_stage2": len(direct),
        "stage2_calls_known": len(s2_calls_known),
        "stage2_calls_expected_extra": round(ASSUMED_STAGE1_KEEP_RATE * len(s1_calls)),
        "est_cost_stage1": cost1, "est_cost_stage2": cost2_known + cost2_extra,
        "est_cost_total": cost1 + cost2_known + cost2_extra,
    }


def log_funnel(f: dict, limit: Optional[int]) -> None:
    log.info("FUNNEL")
    log.info("  jobs in                         %6d", f["total_jobs"])
    log.info("  dropped by rules                %6d", f["dropped_by_rules"])
    for reason, n in f["drop_reasons"].items():
        log.info("      %-28s%6d", reason, n)
    log.info("  survive rules                   %6d", f["survivors_after_rules"])
    if limit:
        log.info("  processed this run (--limit %d) %6d", limit, f["processing_now"])
    log.info("    straight to stage 2           %6d", f["direct_to_stage2"])
    log.info("    to stage 1 (Haiku)            %6d  (cached %d, API calls %d)",
             f["to_stage1"], f["stage1_cached"], f["stage1_calls"])
    log.info("  stage 2 (Sonnet) API calls      %6d known + ~%d expected from stage 1 (assumed %d%% keep)",
             f["stage2_calls_known"], f["stage2_calls_expected_extra"],
             int(ASSUMED_STAGE1_KEEP_RATE * 100))
    log.info("  est. cost  stage1 $%.3f  stage2 $%.3f  total $%.3f",
             f["est_cost_stage1"], f["est_cost_stage2"], f["est_cost_total"])


def _record(job: Job, rule: RuleResult, status: str, s1: Optional[dict], s2: Optional[dict]) -> dict:
    return {**job.model_dump(), "screen": {
        "status": status, "tier": rule.tier, "posting_age_days": posting_age_days(job.posted_date), "years_required": rule.years_required,
        "flags": rule.flags, "location_status": rule.location_status,
        "notes": (s2 or {}).get("notes", []), "stage1": s1, "stage2": s2,
        "screened_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}}


def _cached_or_call(cache: Cache, key: str, retry_failed: bool, call) -> Optional[dict]:
    """Return a cache entry, calling the API on a miss. None means an API error (not cached)."""
    entry = cache.get(key)
    if _usable(entry, retry_failed):
        return entry
    try:
        value = call()
    except ScreenError as exc:
        entry = {"failed": True, "error": str(exc)}
    except anthropic.AuthenticationError:
        raise
    except anthropic.APIError as exc:
        log.error("API error, will retry next run: %s", exc.__class__.__name__)
        return None
    else:
        entry = value.model_dump()
    cache.put(key, entry)
    return entry


def screen(candidates: List[Candidate], cache: Cache, ctx: stage2.ScoringContext, client,
           retry_failed: bool, usage: Usage, max_cost: Optional[float] = None) -> Dict[str, dict]:
    """Score candidates. With max_cost, stop cleanly once actual spend reaches it (the estimate
    can undershoot); jobs not reached are simply picked up by the next run."""
    out: Dict[str, dict] = {}
    for job, rule in candidates:
        if max_cost is not None and usage.cost() >= max_cost:
            usage.capped = True
            log.warning("spend reached the $%.2f cap; stopping with %d of %d jobs screened",
                        max_cost, len(out), len(candidates))
            break
        s1_entry = None
        if rule.status == "stage1":
            s1_entry = _cached_or_call(cache, key1(job), retry_failed,
                                       lambda j=job, r=rule: stage1.decide(client, j, r, usage))
            if s1_entry is None:
                continue
            if s1_entry.get("failed"):
                out[job.id] = _record(job, rule, "skipped_invalid_json", s1_entry, None)
                continue
            if not s1_entry["keep"]:
                out[job.id] = _record(job, rule, "dropped_stage1", s1_entry, None)
                continue
        s2_entry = _cached_or_call(cache, key2(job, ctx), retry_failed,
                                   lambda j=job, r=rule: stage2.score_job(client, j, r, ctx, usage))
        if s2_entry is None:
            continue
        if s2_entry.get("failed"):
            out[job.id] = _record(job, rule, "skipped_invalid_json", s1_entry, s2_entry)
        else:
            out[job.id] = _record(job, rule, "scored", s1_entry, stage2.apply_caps(
                s2_entry, rule, jd_partial=job.jd_partial, also_listed_on=tuple(job.also_listed_on),
                age_days=posting_age_days(job.posted_date), stale_note_days=ctx.stale_note_days,
                stale_skip_days=ctx.stale_skip_days))
    return out


def merge_output(out_path: Path, new: Dict[str, dict]) -> int:
    """Merge into the previous file (never clobber good results). Returns total rows."""
    merged: Dict[str, dict] = {}
    if out_path.exists():
        merged = {r["id"]: r for r in json.loads(out_path.read_text())}
    merged.update(new)
    atomic_write_json(out_path, list(merged.values()))
    return len(merged)


def run(jobs_path: Path, out_path: Path, profile_path: Path, cache_path: Path, *,
        dry_run: bool = False, limit: Optional[int] = None, retry_failed: bool = False,
        client=None, max_cost: float = MAX_COST_PER_RUN) -> dict:
    profile = yaml.safe_load(profile_path.read_text())
    rules, ctx = Rules.from_profile(profile), stage2.build_context(profile)
    cache = Cache(cache_path)
    evaluated = [(j, rules.evaluate(j)) for j in load_jobs(jobs_path)]
    candidates = sorted((c for c in evaluated if c[1].status != "drop"),
                        key=lambda c: (c[1].status != "stage2", TIER_ORDER[c[1].tier]))
    if limit:
        candidates = candidates[:limit]
    funnel = build_funnel(evaluated, candidates, cache, ctx, retry_failed)
    log_funnel(funnel, limit)
    if dry_run:
        log.info("dry run: no API calls made, nothing written")
        return funnel
    if funnel["est_cost_total"] > max_cost:
        raise SpendGuardError(
            f"estimated cost ${funnel['est_cost_total']:.2f} is above the limit ${max_cost:.2f}; "
            "no API call was made. Raise it with --max-cost or lower the load with --limit.")
    usage = Usage()
    results = screen(candidates, cache, ctx, client or make_client(), retry_failed, usage, max_cost)
    total = merge_output(out_path, results)
    funnel["actual_cost"] = usage.cost()
    funnel["stopped_at_cost_cap"] = usage.capped
    funnel["actual_stage1_calls"] = usage.calls.get(HAIKU, 0)
    funnel["actual_stage2_calls"] = usage.calls.get(SONNET, 0)
    funnel["scored"] = sum(1 for r in results.values() if r["screen"]["status"] == "scored")
    log.info("done: %d screened this run, %d rows in %s, actual API cost $%.3f",
             len(results), total, out_path, funnel["actual_cost"])
    return funnel


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true", help="funnel and cost estimate only")
    p.add_argument("--limit", type=int, help="process at most N jobs that survive the rules")
    p.add_argument("--retry-failed", action="store_true", help="retry jobs cached as invalid JSON")
    p.add_argument("--max-cost", type=float, default=MAX_COST_PER_RUN,
                   help="abort if the estimated cost is above this (USD)")
    p.add_argument("--jobs", type=Path, default=Path("data/raw/scout_jobs.json"))
    p.add_argument("--out", type=Path, default=Path("data/clean/screened_jobs.json"))
    p.add_argument("--profile", type=Path, default=Path("profile.yaml"))
    p.add_argument("--cache", type=Path, default=Path("data/clean/screener_cache.json"))
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    run(a.jobs, a.out, a.profile, a.cache, dry_run=a.dry_run, limit=a.limit,
        retry_failed=a.retry_failed, max_cost=a.max_cost)


if __name__ == "__main__":
    main()
