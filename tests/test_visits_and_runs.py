"""Stage 3: New badge, last_seen, Run pipeline now, cost cap, failed runs, launchd plist."""
import json
import plistlib
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import run_pipeline
from dashboard import data as dash_data
from dashboard import serve
from dashboard.pipeline import PipelineService
from dashboard.seen import SAME_VISIT_MINUTES, SeenStore
from dashboard.service import ServiceError
from dashboard.store import ApplicationsStore
from screener import run_screener
from screener.llm import Usage
from tests.test_dashboard import call, good_headers, screened_row
from tests.test_pipeline import go as pipeline_go
from tests.test_pipeline import paths  # noqa: F401  (fixture)
from tests.test_screener import GOOD_S2, FakeClient

T0 = datetime(2026, 10, 17, 9, 0, tzinfo=timezone.utc)


# ---------- last_seen ----------

def test_first_open_has_no_baseline_and_records_last_seen(tmp_path):
    seen = SeenStore(tmp_path / "seen.json")
    assert seen.open_visit(T0) is None
    assert seen.last_seen() == T0.isoformat(timespec="seconds") and seen.since() is None


def test_next_visit_baseline_is_previous_last_seen(tmp_path):
    seen = SeenStore(tmp_path / "seen.json")
    seen.open_visit(T0)
    later = T0 + timedelta(hours=20)
    assert seen.open_visit(later) == T0.isoformat(timespec="seconds")
    assert seen.last_seen() == later.isoformat(timespec="seconds")   # last_seen moved to now
    assert seen.since() == T0.isoformat(timespec="seconds")


def test_quick_reload_keeps_the_baseline(tmp_path):
    seen = SeenStore(tmp_path / "seen.json")
    seen.open_visit(T0)
    seen.open_visit(T0 + timedelta(hours=20))
    reload_at = T0 + timedelta(hours=20, minutes=SAME_VISIT_MINUTES - 1)
    assert seen.open_visit(reload_at) == T0.isoformat(timespec="seconds")   # same visit: badges stay
    assert seen.last_seen() == reload_at.isoformat(timespec="seconds")


def test_unreadable_seen_file_is_treated_as_first_visit(tmp_path):
    path = tmp_path / "seen.json"
    path.write_text("{not json")
    assert SeenStore(path).open_visit(T0) is None


# ---------- New badge ----------

def test_is_new_only_after_baseline():
    since = "2026-10-17T00:00:00+00:00"
    assert dash_data.is_new("2026-10-17T00:00:01+00:00", since) is True
    assert dash_data.is_new("2026-10-16T23:59:59+00:00", since) is False
    assert dash_data.is_new("2026-10-17T00:00:00+00:00", since) is False       # not strictly after
    assert dash_data.is_new("2026-10-17T05:30:00+05:30", since) is False       # offsets compared as instants
    assert dash_data.is_new(None, since) is False and dash_data.is_new("2026-10-18", None) is False


def _state(tmp_path, since):
    clean = tmp_path / "clean"
    clean.mkdir(exist_ok=True)
    rows = [screened_row("old", first_seen_at="2026-10-16T08:00:00+00:00"),
            screened_row("fresh", first_seen_at="2026-10-17T07:00:00+00:00"),
            screened_row("fresh_skip", first_seen_at="2026-10-17T07:00:00+00:00")]
    rows[2]["screen"]["stage2"]["verdict"] = "skip"
    (clean / "screened_jobs.json").write_text(json.dumps(rows))
    return dash_data.build_state(tmp_path, ApplicationsStore(clean / "applications.json"), since=since)


def test_new_badge_only_for_jobs_first_seen_after_last_seen(tmp_path):
    state = _state(tmp_path, "2026-10-17T00:00:00+00:00")
    new = {r["id"]: r["is_new"] for r in state["jobs"]}
    assert new == {"old": False, "fresh": True, "fresh_skip": True}
    assert state["counts"]["new_ready"] == 1          # the skip verdict is not in Ready to apply
    assert state["counts"]["since"] == "2026-10-17T00:00:00+00:00"


def test_no_baseline_means_nothing_is_new(tmp_path):
    state = _state(tmp_path, None)
    assert not any(r["is_new"] for r in state["jobs"]) and state["counts"]["new_ready"] == 0


@pytest.fixture
def live(tmp_path):
    clean = tmp_path / "clean"
    clean.mkdir()
    (clean / "screened_jobs.json").write_text(json.dumps([
        screened_row("old", first_seen_at="2020-01-01T00:00:00+00:00"),
        screened_row("fresh", first_seen_at="2099-01-01T00:00:00+00:00")]))
    pipeline = PipelineService(tmp_path, runner=lambda max_cost: 0, estimator=lambda: 0.12, max_cost=1.0)
    srv = serve.make_server(0, tmp_path, token="tok123", pipeline_service=pipeline)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


def test_opening_the_dashboard_updates_last_seen_and_sets_baseline(live, tmp_path):
    seen_file = tmp_path / "clean" / "dashboard_seen.json"
    assert not seen_file.exists()
    status, _, body = call(live, "GET", "/")
    assert status == 200 and b'name="dashboard-since" content=""' in body       # first visit: no baseline
    first = json.loads(seen_file.read_text())["last_seen"]
    # pretend the last visit was yesterday, then open again
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    seen_file.write_text(json.dumps({"last_seen": yesterday, "since": None}))
    _, _, body = call(live, "GET", "/")
    assert yesterday.encode() in body                                            # baseline = previous visit
    assert json.loads(seen_file.read_text())["last_seen"] != yesterday           # last_seen updated to now
    state = json.loads(call(live, "GET", "/api/state")[2])
    assert {r["id"]: r["is_new"] for r in state["jobs"]} == {"old": False, "fresh": True}
    assert json.loads(seen_file.read_text())["last_seen"] >= first               # /api/state does not move it


# ---------- Run pipeline now ----------

def test_pipeline_routes_need_the_token(live):
    for path in ("/api/pipeline/estimate", "/api/pipeline/run"):
        assert call(live, "POST", path, {}, {"Origin": f"http://127.0.0.1:{live.port}"})[0] == 403
        assert call(live, "POST", path, {}, good_headers(live, **{"X-Dashboard-Token": "wrong"}))[0] == 403


def test_estimate_shows_cost_and_cap_before_running(live):
    status, _, body = call(live, "POST", "/api/pipeline/estimate", {}, good_headers(live))
    out = json.loads(body)
    assert status == 200 and out["est_cost"] == 0.12 and out["max_cost"] == 1.0 and out["state"] == "idle"


def test_run_starts_in_background_and_reports_done(live):
    status, _, body = call(live, "POST", "/api/pipeline/run", {}, good_headers(live))
    assert status == 200 and json.loads(body)["state"] in ("running", "done")
    deadline = time.time() + 5
    while time.time() < deadline:
        st = json.loads(call(live, "GET", "/api/pipeline/status")[2])
        if st["state"] != "running":
            break
        time.sleep(0.05)
    assert st["state"] == "done" and st["exit_code"] == 0


def test_second_run_while_running_is_refused(tmp_path):
    gate = threading.Event()
    svc = PipelineService(tmp_path, runner=lambda max_cost: gate.wait(5) and 0, estimator=lambda: 0)
    svc.start({})
    with pytest.raises(ServiceError) as err:
        svc.start({})
    assert err.value.status == 409
    gate.set()


def test_runner_uses_the_cap_and_failure_is_reported(tmp_path):
    caps = []
    svc = PipelineService(tmp_path, max_cost=0.5, runner=lambda max_cost: caps.append(max_cost) or 1)
    svc.start({})
    for _ in range(100):
        if svc.status()["state"] != "running":
            break
        time.sleep(0.02)
    assert caps == [0.5] and svc.status()["state"] == "failed" and svc.status()["exit_code"] == 1


# ---------- cost cap and failed runs ----------

class ExpensiveClient(FakeClient):
    """Every reply reports a million input tokens, so the first call alone passes any small cap."""
    def _create(self, **kw):
        resp = super()._create(**kw)
        resp.usage.input_tokens = 1_000_000
        return resp


def test_run_that_hits_the_cap_mid_screen_stops_cleanly(paths):
    client = ExpensiveClient([GOOD_S2, GOOD_S2])
    est = run_screener.run(paths.jobs, paths.screened, paths.profile, paths.cache, dry_run=True)["est_cost_total"]
    assert pipeline_go(paths, client, max_cost=est) == 0
    assert len(client.calls) == 1                       # stopped after the first job, did not crash
    rows = json.loads(paths.screened.read_text())
    assert len(rows) == 1
    stats = json.loads((paths.clean / "run_stats.jsonl").read_text())
    assert stats["stopped_at_cost_cap"] is True
    # the next run picks up the job that was not reached
    assert pipeline_go(paths, FakeClient([GOOD_S2]), max_cost=5.0) == 0
    assert len(json.loads(paths.screened.read_text())) == 2


def test_screen_with_cap_marks_usage_capped():
    usage = Usage()
    usage.by_model["claude-sonnet-5-5"] = [10_000_000, 0]
    assert usage.cost() > 1 and usage.capped is False
    out = run_screener.screen([(object(), object())], cache=None, ctx=None, client=None,
                              retry_failed=False, usage=usage, max_cost=1.0)
    assert out == {} and usage.capped is True           # stopped before touching the first job


def test_failed_run_does_not_overwrite_existing_results(paths):
    assert pipeline_go(paths, FakeClient([GOOD_S2, GOOD_S2])) == 0
    before = paths.screened.read_text()
    jobs = json.loads(paths.jobs.read_text())
    extra = dict(jobs[0], id="zzz", title="Senior Product Manager", company="NewCo")
    paths.jobs.write_text(json.dumps([*jobs, extra]))   # needs one new API call

    class Boom(FakeClient):
        def _create(self, **kw):
            raise RuntimeError("model down")
    assert pipeline_go(paths, Boom([])) == 1
    assert paths.screened.read_text() == before        # untouched
    last = json.loads((paths.clean / "run_stats.jsonl").read_text().splitlines()[-1])
    assert last["failed"] is True and last["error"] == "RuntimeError"
    assert "model down" not in json.dumps(last)         # only the error type is logged


def test_scheduled_trigger_is_recorded(paths):
    assert pipeline_go(paths, FakeClient([GOOD_S2, GOOD_S2]), trigger="scheduled") == 0
    assert json.loads((paths.clean / "run_stats.jsonl").read_text())["trigger"] == "scheduled"


# ---------- launchd plist template and key redaction ----------

SCHED = Path(__file__).resolve().parent.parent / "scheduler"
TEMPLATE = SCHED / "com.prathikkumar.jobagent.daily.plist.template"


def test_plist_template_has_placeholder_paths_and_logs_to_scheduler_log():
    text = TEMPLATE.read_text()
    assert "/Users/" not in text and "__PROJECT_DIR__" in text      # no personal path is committed
    d = plistlib.loads(text.replace("__PROJECT_DIR__", "/x/job_agent").encode())
    assert d["StartCalendarInterval"] == {"Hour": 8, "Minute": 30} and d["RunAtLoad"] is False
    assert d["ProgramArguments"][1] == "/x/job_agent/scheduler/run_daily.sh"
    assert d["StandardOutPath"] == d["StandardErrorPath"] == "/x/job_agent/data/clean/scheduler.log"
    body = (SCHED / "run_daily.sh").read_text()
    assert "run_pipeline.py" in body and "--trigger scheduled" in body and "--max-cost" not in body
    assert "/dev/null" not in body


def test_generated_plist_and_logs_are_gitignored_and_real_plist_not_tracked():
    import subprocess
    root = SCHED.parent
    ignored = subprocess.run(["git", "check-ignore", "scheduler/com.prathikkumar.jobagent.daily.plist",
                              "data/clean/scheduler.log"], cwd=root, capture_output=True, text=True).stdout.split()
    assert set(ignored) == {"scheduler/com.prathikkumar.jobagent.daily.plist", "data/clean/scheduler.log"}
    tracked = subprocess.run(["git", "ls-files", "scheduler"], cwd=root, capture_output=True, text=True).stdout
    assert "jobagent.daily.plist\n" not in tracked and "jobagent.daily.plist.template" in tracked


def test_log_filter_redacts_keys_in_urls():
    import logging
    from scout.logsafe import RedactFilter, redact
    url = "/v1/api/jobs/in/search/1?app_id=ID123&app_key=SECRETKEY&what=product+manager"
    assert "SECRETKEY" not in redact(url) and "ID123" not in redact(url) and "what=product+manager" in redact(url)
    rec = logging.LogRecord("x", logging.WARNING, "f", 1, "Retrying after error: %s", (url,), None)
    RedactFilter().filter(rec)
    assert "SECRETKEY" not in rec.getMessage() and "app_key=***" in rec.getMessage()
