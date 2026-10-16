"""Build the dashboard's view of the data: scored jobs + application status + run stats."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlsplit

from dashboard.store import PIPELINE_STATUSES, ApplicationsStore

FOLLOW_UP_DAYS = 7
READY_VERDICTS = ("apply", "stretch")
READY_STATUSES = ("new", "shortlisted")


def safe_url(url: object) -> str:
    """Only http(s) URLs survive; anything else (javascript:, data:, ...) becomes ''."""
    if not isinstance(url, str):
        return ""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return ""
    return url.strip() if parts.scheme in ("http", "https") and parts.netloc else ""


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def read_jsonl(path: Path) -> List[dict]:
    rows = []
    try:
        for line in path.read_text().splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        pass
    return rows


def job_row(row: dict) -> Optional[dict]:
    """Flatten a screened_jobs.json record; None if the job was not scored."""
    screen = row.get("screen") or {}
    s2 = screen.get("stage2") or {}
    if screen.get("status") != "scored" or "score" not in s2:
        return None
    return {"id": row["id"], "company": row.get("company", ""), "title": row.get("title", ""),
            "location": row.get("location", ""), "source": row.get("source", ""),
            "url": safe_url(row.get("url")), "posted_date": row.get("posted_date"),
            "tier": screen.get("tier") or "", "score": s2["score"], "verdict": s2["verdict"],
            "notes": list(screen.get("notes") or []), "red_flags": list(s2.get("red_flags") or []),
            "also_listed_on": list(row.get("also_listed_on") or []), "orphaned": False}


def merge_application(job: dict, app: Optional[dict]) -> dict:
    app = app or {}
    return {**job, "status": app.get("status", "new"), "applied_at": app.get("applied_at"),
            "app_notes": app.get("notes", ""), "updated_at": app.get("updated_at")}


def orphan_row(job_id: str, app: dict) -> dict:
    """An application whose posting is gone from the feeds, rebuilt from its snapshot."""
    snap = app.get("snapshot") or {}
    return merge_application({
        "id": job_id, "company": snap.get("company", "(unknown)"), "title": snap.get("title", "(unknown)"),
        "location": "", "source": "", "url": safe_url(snap.get("url")), "posted_date": None,
        "tier": "", "score": snap.get("score"), "verdict": snap.get("verdict", ""),
        "notes": ["posting no longer in the feeds"], "red_flags": [], "also_listed_on": [],
        "orphaned": True}, app)


def follow_up_due(row: dict, today: date) -> bool:
    if row["status"] != "applied" or not row.get("applied_at"):
        return False
    try:
        return (today - date.fromisoformat(row["applied_at"])).days >= FOLLOW_UP_DAYS
    except ValueError:
        return False


def month_cost(runs: List[dict], today: date) -> float:
    prefix = today.strftime("%Y-%m")
    return round(sum(r.get("cost_usd", 0) for r in runs
                     if str(r.get("timestamp", "")).startswith(prefix) and not r.get("dry_run")), 4)


def build_state(data_dir: Path, store: ApplicationsStore, today: Optional[date] = None) -> dict:
    today = today or datetime.now(timezone.utc).date()
    clean = data_dir / "clean"
    apps = store.load()
    jobs: Dict[str, dict] = {}
    for raw in read_json(clean / "screened_jobs.json", []):
        job = job_row(raw)
        if job:
            jobs[job["id"]] = merge_application(job, apps.get(job["id"]))
    for job_id, app in apps.items():
        if job_id not in jobs and app.get("status") != "new":
            jobs[job_id] = orphan_row(job_id, app)
    rows = list(jobs.values())
    for row in rows:
        row["follow_up"] = follow_up_due(row, today)
    runs = read_jsonl(clean / "run_stats.jsonl")
    real_runs = [r for r in runs if not r.get("aborted")]

    def count(*statuses):
        return sum(1 for r in rows if r["status"] in statuses)

    counts = {
        "ready": sum(1 for r in rows if r["verdict"] in READY_VERDICTS and r["status"] in READY_STATUSES
                     and not r["orphaned"]),
        "applied": count("applied"), "interview": count("interview"),
        "rejected": count("rejected"), "skipped": count("skipped"),
        "total_scored": sum(1 for r in rows if not r["orphaned"]),
        "last_run": (real_runs[-1]["timestamp"] if real_runs else None),
        "month_cost": month_cost(runs, today),
    }
    return {"jobs": rows, "counts": counts, "runs": runs, "pipeline_statuses": list(PIPELINE_STATUSES),
            "filtered_out": [{**f, "company": str(f.get("company", ""))}
                             for f in read_json(clean / "filtered_out.json", [])],
            "today": today.isoformat()}


def snapshot_for(data_dir: Path, job_id: str) -> Optional[dict]:
    """Server-side lookup of a job's snapshot fields; the client is never trusted for these."""
    for raw in read_json(data_dir / "clean" / "screened_jobs.json", []):
        if raw.get("id") == job_id:
            job = job_row(raw)
            if job:
                return {k: job[k] for k in ("company", "title", "url", "score", "verdict")}
    return None


def filtered_snapshot_for(data_dir: Path, job_id: str) -> Optional[str]:
    """The drop reason recorded for a filtered-out job, or None if it is not in the list."""
    for f in read_json(data_dir / "clean" / "filtered_out.json", []):
        if f.get("id") == job_id:
            return str(f.get("reason", ""))
    return None
