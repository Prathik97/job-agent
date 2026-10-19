"""Manual-only operation: the double-click command, the run summary, and the wording."""
import json
import os
import stat
from datetime import date
from pathlib import Path

import run_summary

ROOT = Path(__file__).resolve().parent.parent


def make_data(tmp_path, *, failed=False, started="2026-10-20T03:00:00+00:00"):
    clean, raw = tmp_path / "clean", tmp_path / "raw"
    clean.mkdir()
    raw.mkdir()
    run = ({"timestamp": "2026-10-20T03:05:00+00:00", "failed": True, "error": "RuntimeError"} if failed else
           {"timestamp": "2026-10-20T03:05:00+00:00", "started_at": started, "dry_run": False, "aborted": False,
            "scored": {"adzuna": 5, "lever": 2}, "verdicts": {"apply": 3, "stretch": 9, "skip": 4},
            "cost_usd": 0.1234, "stopped_at_cost_cap": False})
    (clean / "run_stats.jsonl").write_text(json.dumps({"timestamp": "2026-10-19T03:00:00+00:00", "dry_run": True}) + "\n"
                                           + json.dumps(run) + "\n")
    (raw / "scout_jobs.json").write_text(json.dumps([
        {"first_seen_at": "2026-10-19T10:00:00+00:00"}, {"first_seen_at": "2026-10-20T03:01:00+00:00"},
        {"first_seen_at": "2026-10-20T03:04:00+00:00"}]))
    month = date.today().strftime("%Y-%m")
    (raw / "api_usage.json").write_text(json.dumps({
        "jsearch": {"month": month, "month_calls": 68, "day": "2000-01-01", "day_calls": 0},
        "adzuna": {"month": month, "month_calls": 5, "day": "2000-01-01", "day_calls": 0}}))
    return tmp_path


def test_summary_has_every_requested_figure(tmp_path):
    text = "\n".join(run_summary.summary_lines(make_data(tmp_path)))
    assert "New jobs found        2" in text            # only jobs first seen since this run started
    assert "Scored                7" in text and "Apply / Stretch       3 / 9" in text
    assert "$0.123" in text and "cap $1.00" in text
    assert "JSearch 112 of 180" in text and "Adzuna 15 of 20" in text


def test_summary_of_a_failed_run_says_nothing_was_overwritten(tmp_path):
    text = "\n".join(run_summary.summary_lines(make_data(tmp_path, failed=True)))
    assert "FAILED" in text and "RuntimeError" in text and "Nothing was overwritten" in text


def test_summary_with_no_runs_or_no_start_time(tmp_path):
    assert run_summary.summary_lines(tmp_path) == ["No run recorded yet."]
    d = tmp_path / "d"
    d.mkdir()
    data = make_data(d)
    stats = data / "clean" / "run_stats.jsonl"
    stats.write_text(stats.read_text().replace('"started_at": "2026-10-20T03:00:00+00:00", ', ""))
    assert "New jobs found        n/a" in "\n".join(run_summary.summary_lines(data))


def test_double_click_command_is_executable_and_keeps_the_safeguards():
    path = ROOT / "Run Job Agent.command"
    assert path.exists() and os.stat(path).st_mode & stat.S_IXUSR
    body = path.read_text()
    assert "source .venv/bin/activate" in body and "run_pipeline.py --trigger manual" in body
    assert "run_summary.py" in body and "dashboard.serve" in body and 'open "http://127.0.0.1' in body
    assert "--max-cost" not in body                       # the config.py cap ($1.00) is not overridden
    assert "--skip-scout" not in body and "--dry-run" not in body   # one full real run
    assert body.index("run_pipeline.py") < body.index("run_summary.py") < body.index("dashboard.serve")


def test_manual_trigger_is_not_restricted_to_mondays(monkeypatch, tmp_path):
    import run_pipeline
    seen = {}

    def fake_run(*a, **kw):
        seen["weekly"] = kw["adzuna_weekly_only"]
        raise RuntimeError("stop here")
    monkeypatch.setattr(run_pipeline.run_scouts, "run", fake_run)
    run_pipeline.run(companies=tmp_path / "c.yaml", profile=tmp_path / "p.yaml", jobs=tmp_path / "j.json",
                     screened=tmp_path / "s.json", cache=tmp_path / "c.json", clean_dir=tmp_path / "clean",
                     usage=tmp_path / "u.json", trigger="manual")
    assert seen["weekly"] is False      # Adzuna may run any day the monthly budget allows


def test_nothing_is_installed_or_tracked_as_automatic():
    import subprocess
    tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True).stdout
    assert "jobagent.daily.plist\n" not in tracked          # only the .template is committed
    readme = (ROOT / "scheduler" / "README.md").read_text()
    assert "OFF by default" in readme and "not installed" in readme and "optional" in readme.lower()
    assert not (Path.home() / "Library/LaunchAgents/com.prathikkumar.jobagent.daily.plist").exists()


def test_wording_says_manual_with_no_8_30_schedule():
    readme = (ROOT / "README.md").read_text()
    js = (ROOT / "dashboard/static/app.js").read_text()
    assert "Running it (manual)" in readme and "Run Job Agent.command" in readme
    for text in (readme, js, (ROOT / "dashboard/static/index.html").read_text()):
        assert "8:30" not in text and "daily digest" not in text.lower()
    assert "Runs are manual" in js
