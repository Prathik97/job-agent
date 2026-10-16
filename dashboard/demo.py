"""Synthetic sample data for `--demo`. Written to a temp directory in the real data layout, so
the same code paths run and the real data/ folder is never read or written."""
from __future__ import annotations

import json
import random
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dashboard.store import atomic_write

COMPANIES = ["Northwind Labs", "Example Corp", "Acme Cloud", "Globex Pay", "Initech Systems",
             "Umbrella Health", "Hooli Apps", "Stark Logistics", "Wayne Commerce", "Pied Piper"]
TITLES = ["Senior Product Manager", "Product Manager 2", "Lead Product Manager",
          "Principal Product Manager", "Staff Product Manager", "Group Product Manager",
          "Technical Program Manager", "Head of Product"]
LOCATIONS = ["Bengaluru, India", "Remote - India", "Bangalore", "Remote", "Bengaluru, Karnataka"]
SOURCES = ["greenhouse", "lever", "ashby", "jsearch", "adzuna"]
TIERS = ["A", "A", "A", "B", "C", "untiered"]


def make_demo_dir() -> Path:
    rng = random.Random(7)
    root = Path(tempfile.mkdtemp(prefix="job_agent_demo_"))
    clean = root / "clean"
    rows, filtered = [], []
    for i in range(48):
        score = rng.randint(30, 92)
        verdict = "apply" if score >= 70 else "stretch" if score >= 55 else "skip"
        source = rng.choice(SOURCES)
        partial = source in ("jsearch", "adzuna")
        notes = []
        if rng.random() < 0.3:
            notes.append("verify location")
        if partial:
            notes.append("partial JD, open the link and read it before applying")
            verdict = "stretch" if verdict == "apply" else verdict
        rows.append({
            "id": f"demo{i:04d}", "source": source, "title": rng.choice(TITLES),
            "company": f"{rng.choice(COMPANIES)} {i}", "location": rng.choice(LOCATIONS),
            "remote_type": "unknown", "url": f"https://example.com/jobs/{i}",
            "description": "Synthetic demo job.", "fetched_at": "2026-10-16T03:30:00+00:00",
            "posted_date": (date(2026, 10, 16) - timedelta(days=rng.randint(0, 30))).isoformat(),
            "jd_partial": partial, "also_listed_on": [],
            "screen": {"status": "scored", "tier": rng.choice(TIERS), "flags": [], "notes": notes,
                       "location_status": "confirmed", "stage1": None,
                       "stage2": {"score": score, "verdict": verdict, "role_family": "product",
                                  "reasons": ["demo"], "red_flags": [], "notes": notes}}})
    for i, reason in enumerate(["title_untiered"] * 6 + ["title_reject:Sales"] * 2 +
                               ["location_non_india_only"] * 3 + ["stage1: remote US only"] * 2):
        filtered.append({"id": f"filt{i:04d}", "company": f"{rng.choice(COMPANIES)} F{i}",
                         "title": rng.choice(["Software Engineer", "Account Executive", "Product Manager"]),
                         "location": rng.choice(["Austin, TX", "Remote - US", "London"]), "reason": reason})
    now = datetime.now(timezone.utc)
    runs = []
    for d in range(4, -1, -1):
        total = 1700 + rng.randint(0, 80)
        runs.append({"timestamp": (now - timedelta(days=d)).isoformat(timespec="seconds"),
                     "dry_run": False, "aborted": False,
                     "jobs_in": {"greenhouse": total - 400, "lever": 90, "ashby": 310 - 0, "jsearch": 0},
                     "jobs_in_total": total + 0, "after_rules": {"greenhouse": 5, "lever": 6},
                     "after_stage1": {"greenhouse": 5, "lever": 6}, "scored": {"greenhouse": 5, "lever": 6},
                     "stage1_calls": rng.randint(0, 6), "stage2_calls": rng.randint(0, 12),
                     "cost_usd": round(rng.uniform(0.02, 0.2), 4),
                     "verdicts": {"apply": 12, "stretch": 20, "skip": 16}})
    apps = {}
    now_s = now.isoformat(timespec="seconds")
    for jid, status, ago in [("demo0001", "applied", 10), ("demo0002", "applied", 2),
                             ("demo0003", "interview", 5), ("demo0004", "rejected", 20),
                             ("demo0005", "shortlisted", 0), ("demo0006", "skipped", 0)]:
        row = next(r for r in rows if r["id"] == jid)
        applied = (date.today() - timedelta(days=ago)).isoformat() if status in (
            "applied", "interview", "rejected") else None
        apps[jid] = {"status": status, "updated_at": now_s, "applied_at": applied,
                     "notes": "demo note" if status == "interview" else "",
                     "history": [{"at": now_s, "from": "new", "to": status}],
                     "snapshot": {"company": row["company"], "title": row["title"], "url": row["url"],
                                  "score": row["screen"]["stage2"]["score"],
                                  "verdict": row["screen"]["stage2"]["verdict"]}}
    apps["demo-gone"] = {"status": "applied", "updated_at": now_s,
                         "applied_at": (date.today() - timedelta(days=9)).isoformat(),
                         "notes": "posting removed", "history": [],
                         "snapshot": {"company": "Vanished Inc", "title": "Senior Product Manager",
                                      "url": "https://example.com/gone", "score": 81, "verdict": "apply"}}
    atomic_write(clean / "screened_jobs.json", rows)
    atomic_write(clean / "filtered_out.json", filtered)
    atomic_write(clean / "applications.json", apps)
    (clean / "run_stats.jsonl").write_text("".join(json.dumps(r) + "\n" for r in runs))
    return root
