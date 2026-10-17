"""Per-run funnel numbers and the filtered-out list. Pure functions over files already on disk.

run_stats.jsonl lines are aggregate-only: counts and cost, never company names, titles or URLs.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Dict, List, Optional

from scout.schema import Job
from screener.rules import Rules, pm_role_kind

VERDICTS = ("apply", "stretch", "skip")


def _passed_stage1(row: Optional[dict]) -> bool:
    if row is None:
        return True  # not screened yet (limit, API error): still in the funnel
    screen = row["screen"]
    if screen["status"] == "dropped_stage1":
        return False
    return not (screen["status"] == "skipped_invalid_json" and screen.get("stage2") is None)


def _by_source(counter: Counter, sources: List[str]) -> Dict[str, int]:
    return {s: counter.get(s, 0) for s in sources}


def layer_counts(jobs: List[Job], rules: Rules, screened: Dict[str, dict]) -> dict:
    """Jobs in, after rules, after stage 1, scored: per source, plus verdict totals.
    Only jobs in the current scout output count, not older rows kept in screened_jobs.json."""
    sources = sorted({j.source for j in jobs})
    n_in, n_rules, n_s1, n_scored = Counter(), Counter(), Counter(), Counter()
    verdicts: Counter = Counter()
    drop_reasons: Counter = Counter()
    for job in jobs:
        n_in[job.source] += 1
        rule = rules.evaluate(job)
        if rule.status == "drop":
            drop_reasons[rule.drop_category] += 1
            continue
        n_rules[job.source] += 1
        row = screened.get(job.id)
        if not _passed_stage1(row):
            continue
        n_s1[job.source] += 1
        if row and row["screen"]["status"] == "scored":
            n_scored[job.source] += 1
            verdicts[row["screen"]["stage2"]["verdict"]] += 1
    return {"sources": sources, "jobs_in": _by_source(n_in, sources),
            "after_rules": _by_source(n_rules, sources), "after_stage1": _by_source(n_s1, sources),
            "scored": _by_source(n_scored, sources),
            "verdicts": {v: verdicts.get(v, 0) for v in VERDICTS},
            "rule_drop_reasons": dict(drop_reasons.most_common())}


def build_stats_line(layers: dict, funnel: dict, *, dry_run: bool, aborted: bool = False) -> dict:
    """One aggregate-only record for run_stats.jsonl."""
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dry_run": dry_run, "aborted": aborted,
        "jobs_in": layers["jobs_in"], "jobs_in_total": sum(layers["jobs_in"].values()),
        "after_rules": layers["after_rules"], "after_stage1": layers["after_stage1"],
        "scored": layers["scored"], "rule_drop_reasons": layers["rule_drop_reasons"],
        "stage1_calls": funnel.get("actual_stage1_calls", 0),
        "stage2_calls": funnel.get("actual_stage2_calls", 0),
        "cost_usd": round(funnel.get("actual_cost", 0.0), 4),
        "est_cost_usd": round(funnel.get("est_cost_total", 0.0), 4),
        "verdicts": layers["verdicts"],
        "stopped_at_cost_cap": bool(funnel.get("stopped_at_cost_cap")),
    }


def build_filtered_out(jobs: List[Job], rules: Rules, screened: Dict[str, dict]) -> List[dict]:
    """Every job the pipeline dropped, with the reason, rebuilt from the full scout output."""
    out = []
    for job in jobs:
        rule = rules.evaluate(job)
        reason = rule.drop_reason if rule.status == "drop" else None
        row = screened.get(job.id)
        if reason is None and row is not None:
            screen = row["screen"]
            if screen["status"] == "dropped_stage1":
                reason = "stage1: " + ((screen.get("stage1") or {}).get("reason") or "dropped")
            elif screen["status"] == "skipped_invalid_json":
                reason = "invalid_json"
        if reason:
            # eligible for the false-drop review sample: PM-like title or an India location
            eligible = pm_role_kind(job.title) is not None or rules._india_signal(job.location)
            out.append({"id": job.id, "company": job.company, "title": job.title,
                        "location": job.location, "reason": reason, "review_eligible": eligible})
    return out
