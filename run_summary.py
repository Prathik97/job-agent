"""Short summary of the latest pipeline run, for "Run Job Agent.command".

Usage:  python run_summary.py [exit_code]
Reads data/clean/run_stats.jsonl, data/raw/scout_jobs.json and data/raw/api_usage.json.
Prints counts and spend only: no job text, no keys.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from config import MAX_COST_PER_RUN
from dashboard.data import api_budget, read_json, read_jsonl
from screener.freshness import parse_moment

API_NAMES = {"jsearch": "JSearch", "adzuna": "Adzuna"}


def last_run(clean_dir: Path) -> Optional[dict]:
    runs = [r for r in read_jsonl(clean_dir / "run_stats.jsonl") if not r.get("dry_run")]
    return runs[-1] if runs else None


def count_new(jobs_path: Path, since: Optional[str]) -> int:
    """Jobs the scouts first saw during this run (first_seen_at at or after its start)."""
    start = parse_moment(since)
    if start is None:
        return 0
    return sum(1 for j in read_json(jobs_path, []) if (m := parse_moment(j.get("first_seen_at"))) and m >= start)


def summary_lines(data_dir: Path, exit_code: int = 0) -> List[str]:
    run = last_run(data_dir / "clean")
    if run is None:
        return ["No run recorded yet."]
    if run.get("failed"):
        return [f"The run FAILED ({run.get('error', 'unknown error')}). Nothing was overwritten.",
                "See the output above, then run again."]
    v = run.get("verdicts") or {}
    new = count_new(data_dir / "raw" / "scout_jobs.json", run.get("started_at")) if run.get("started_at") else "n/a"
    lines = [
        "Run summary",
        f"  New jobs found        {new}",
        f"  Scored                {sum((run.get('scored') or {}).values())}   (all scored jobs, not only new ones)",
        f"  Apply / Stretch       {v.get('apply', 0)} / {v.get('stretch', 0)}   (across all scored jobs)",
        f"  Cost this run         ${run.get('cost_usd', 0):.3f}   (cap $1.00 per run)",
    ]
    if run.get("aborted"):
        lines.append("  Stopped before screening: estimated cost was above the cap.")
    if run.get("stopped_at_cost_cap"):
        lines.append("  Stopped at the cost cap; the remaining jobs are screened next run.")
    budget = api_budget(data_dir)
    lines.append("  Calls left this month " + ", ".join(
        f"{API_NAMES[a]} {b['left']} of {b['limit']}" for a, b in budget.items()))
    return lines


def main() -> None:
    code = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    print("\n".join(summary_lines(Path("data"), code)))


if __name__ == "__main__":
    main()
