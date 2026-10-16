"""Career-page scouts: Workday, SmartRecruiters, robots.txt gating, discovery. HTTP is mocked."""
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import requests

from scout import careers, http, run_scouts, smartrecruiters, workday
from scout.http import RobotsDisallowed, robots_allowed
from scout.schema import Job
from screener.freshness import hours_old_at_first_seen

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)

WD_LIST = {"total": 3, "jobPostings": [
    {"title": "Senior Product Manager", "externalPath": "/job/Bengaluru/Senior-PM_JR1",
     "locationsText": "Bengaluru", "postedOn": "Posted 3 Days Ago"},
    {"title": "Designer", "externalPath": "/job/Remote/Designer_JR2",
     "locationsText": "India Remote", "postedOn": "Posted Today"},
    {"title": "No path"},
]}
WD_DETAIL = {"jobPostingInfo": {"jobDescription": "<p>Own the <b>roadmap</b></p>", "location": "Bengaluru",
             "startDate": "2026-10-13", "externalUrl": "https://acme.wd3.myworkdayjobs.com/External/job/x",
             "remoteType": "Hybrid"}}
SR_PAGE = {"totalFound": 2, "content": [
    {"id": "1", "name": "Product Manager", "company": {"identifier": "Acme"},
     "releasedDate": "2026-10-15T22:34:23.778Z",
     "location": {"city": "Bengaluru", "country": "in", "fullLocation": "Bengaluru, India", "remote": False,
                  "hybrid": True}},
    {"id": "2", "name": "Remote Engineer", "company": {"identifier": "Acme"},
     "location": {"city": "Pune", "remote": True}},
]}
SR_DETAIL = {"postingUrl": "https://jobs.smartrecruiters.com/Acme/1-pm",
             "jobAd": {"sections": {"jobDescription": {"text": "<p>Build things</p>"}}}}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(http, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(http, "_robots", {})
    monkeypatch.setattr(http, "MIN_DELAY_SECONDS", 0)


# ---- Workday ----
def test_workday_slug_and_urls():
    assert workday.split_slug("acme.wd3.External") == ("acme", "wd3", "External")
    assert workday.base_url("acme.wd3.External") == \
        "https://acme.wd3.myworkdayjobs.com/wday/cxs/acme/External"


def test_workday_fetch_paginates_and_caches_details(monkeypatch):
    pages = []

    def fake_post(session, url, body, **kw):
        pages.append(body["offset"])
        assert kw.get("respect_robots") is True
        postings = WD_LIST["jobPostings"]
        return {"total": 3, "jobPostings": postings[body["offset"]:body["offset"] + 2]}
    monkeypatch.setattr(workday, "PAGE_SIZE", 2)
    monkeypatch.setattr(workday, "post_json", fake_post)
    detail_calls = []
    monkeypatch.setattr(workday, "get_json", lambda s, url, **k: detail_calls.append(url) or WD_DETAIL)
    raw = workday.fetch_raw(None, "acme.wd3.External")
    assert pages == [0, 2] and len(raw["postings"]) == 3
    assert len(detail_calls) == 2  # the posting with no path is skipped
    workday.fetch_raw(None, "acme.wd3.External")
    assert len(detail_calls) == 2  # second run reads the details cache, no new calls


def test_workday_parse_uses_detail_and_relative_dates():
    raw = {"slug": "acme.wd3.External", "postings": WD_LIST["jobPostings"],
           "details": {"/job/Bengaluru/Senior-PM_JR1": WD_DETAIL["jobPostingInfo"]}}
    jobs = workday.parse(raw, "Acme", now=NOW)
    assert len(jobs) == 2  # entry without externalPath is skipped
    pm, designer = jobs
    assert pm.posted_date == "2026-10-13" and pm.remote_type == "hybrid"
    assert pm.description == "Own the roadmap" and pm.url.endswith("/External/job/x")
    assert designer.posted_date == "2026-10-16"  # "Posted Today", no detail
    assert designer.url == "https://acme.wd3.myworkdayjobs.com/External/job/Remote/Designer_JR2"
    assert designer.remote_type == "remote"


def test_workday_relative_date_unknown():
    assert workday._relative_date("Posted 30+ Days Ago", NOW) == "2026-09-16"
    assert workday._relative_date("something else", NOW) is None


# ---- SmartRecruiters ----
def test_smartrecruiters_fetch_and_parse(monkeypatch):
    def fake_get(session, url, params=None, **kw):
        assert kw.get("respect_robots") is True
        if params:
            return SR_PAGE
        return SR_DETAIL if url.endswith("/1") else None  # posting 2 has no detail
    monkeypatch.setattr(smartrecruiters, "get_json", fake_get)
    raw = smartrecruiters.fetch_raw(None, "Acme")
    jobs = smartrecruiters.parse(raw, "Acme")
    assert [j.title for j in jobs] == ["Product Manager", "Remote Engineer"]
    assert jobs[0].posted_date == "2026-10-15T22:34:23.778Z"
    assert jobs[0].description == "Build things" and jobs[0].remote_type == "hybrid"
    assert jobs[0].location == "Bengaluru, India"
    assert jobs[1].remote_type == "remote" and jobs[1].posted_date is None
    assert jobs[1].url == "https://jobs.smartrecruiters.com/Acme/2"


# ---- robots.txt ----
def _session(status, text=""):
    calls = []

    def get(url, timeout=None):
        calls.append(url)
        return SimpleNamespace(status_code=status, text=text)
    return SimpleNamespace(get=get, request=get), calls


def test_robots_disallow_blocks_and_is_cached():
    session, calls = _session(200, "User-agent: *\nDisallow: /")
    assert robots_allowed(session, "https://x.test/jobs") is False
    assert robots_allowed(session, "https://x.test/other") is False
    assert calls == ["https://x.test/robots.txt"]  # fetched once per host


def test_robots_path_rules_and_missing_file():
    session, _ = _session(200, "User-agent: *\nDisallow: /private/")
    assert robots_allowed(session, "https://a.test/jobs") is True
    assert robots_allowed(session, "https://a.test/private/x") is False
    session, _ = _session(404)
    assert robots_allowed(session, "https://b.test/anything") is True
    session, _ = _session(503)
    assert robots_allowed(session, "https://c.test/anything") is False  # unreadable: be safe


def test_get_json_raises_before_fetching_when_robots_disallow():
    session, calls = _session(200, "User-agent: *\nDisallow: /")
    with pytest.raises(RobotsDisallowed):
        http.get_json(session, "https://x.test/api", respect_robots=True)
    assert calls == ["https://x.test/robots.txt"]


def test_careers_page_skipped_when_robots_disallows():
    session, calls = _session(200, "User-agent: *\nDisallow: /careers")
    rec = careers.check_company(session, "Acme", "https://acme.test/careers")
    assert rec["robots"] == "disallowed" and rec["ats"] is None
    assert calls == ["https://acme.test/robots.txt"]  # the careers page itself was never requested


# ---- discovery ----
@pytest.mark.parametrize("url,html,expected", [
    ("https://acme.test/careers", '<a href="https://job-boards.greenhouse.io/acmeco">', ("greenhouse", "acmeco")),
    ("https://acme.test/c", "see https://jobs.lever.co/acme-x/123", ("lever", "acme-x")),
    ("https://acme.test/c", 'href="https://careers.smartrecruiters.com/Acme/eng"', ("smartrecruiters", "Acme")),
    ("https://acme.test/c", "https://acme.wd3.myworkdayjobs.com/en-US/External", ("workday", "acme.wd3.External")),
    ("https://acme.test/c", "https://acme.freshteam.com/jobs", ("freshteam", "acme")),
    ("https://acme.test/c", "https://acme.darwinbox.in/ms/candidate/careers", ("darwinbox", "acme")),
    ("https://acme.test/c", "nothing here", (None, None)),
])
def test_detect_ats(url, html, expected):
    assert careers.detect_ats(url, html) == expected


def test_probe_reports_blocked_and_empty(monkeypatch):
    monkeypatch.setattr(careers, "get_json", lambda *a, **k: (_ for _ in ()).throw(RobotsDisallowed("x")))
    count, note = careers.probe_endpoint(None, "smartrecruiters", "Acme")
    assert count is None and "robots" in note
    monkeypatch.setattr(careers, "get_json", lambda *a, **k: [])
    assert careers.probe_endpoint(None, "lever", "acme") == (None, "endpoint answered but returned no jobs")
    monkeypatch.setattr(careers, "get_json", lambda *a, **k: {"jobs": [{"id": 1}]})
    assert careers.probe_endpoint(None, "greenhouse", "acme") == (1, "confirmed")
    assert careers.probe_endpoint(None, "darwinbox", "acme")[0] is None


def test_failed_discovery_fetch_keeps_earlier_report(tmp_path):
    report = tmp_path / "report.json"
    report.write_text(json.dumps([{"company": "Acme", "ats": "workday", "slug": "acme.wd3.X",
                                   "endpoint_jobs": 9, "note": "confirmed"}]))
    merged = careers.merge_report(report, [{"company": "Other", "ats": None}])
    assert {r["company"] for r in merged} == {"Acme", "Other"}  # Acme not dropped
    assert next(r for r in merged if r["company"] == "Acme")["endpoint_jobs"] == 9


def test_discovery_run_leaves_firecrawl_file_alone_without_resolved(tmp_path, monkeypatch):
    firecrawl = tmp_path / "fc.json"
    entries = [{"company": "Acme", "reason": "r"}, {"company": "Beta", "reason": "r"}]
    firecrawl.write_text(json.dumps(entries))
    pages = tmp_path / "pages.yaml"
    pages.write_text("Acme: https://acme.test/careers\n")
    monkeypatch.setattr(careers, "check_company", lambda s, c, u: {
        "company": c, "careers_url": u, "robots": "allowed", "ats": None, "slug": None,
        "endpoint_jobs": None, "note": "x"})
    report = careers.run(firecrawl, pages, tmp_path / "r.json", session=object())
    assert json.loads(firecrawl.read_text()) == entries
    assert {r["company"] for r in report} == {"Acme", "Beta"}
    careers.run(firecrawl, pages, tmp_path / "r.json", resolved={"Acme"}, session=object())
    left = json.loads(firecrawl.read_text())
    assert [e["company"] for e in left] == ["Beta"] and left[0]["discovery_note"] == "no careers URL on file"


# ---- merge, never overwrite; first_seen_at ----
def test_workday_failure_does_not_overwrite_and_first_seen_survives(tmp_path, monkeypatch):
    companies = tmp_path / "c.yaml"
    companies.write_text("workday:\n  - {slug: acme.wd3.External, name: Acme}\n")
    out = tmp_path / "out.json"
    monkeypatch.setattr("scout.run_scouts.cache_changed", lambda *a: True)
    raw = {"slug": "acme.wd3.External", "postings": WD_LIST["jobPostings"][:1],
           "details": {"/job/Bengaluru/Senior-PM_JR1": WD_DETAIL["jobPostingInfo"]}}
    monkeypatch.setattr(workday, "fetch_raw", lambda s, slug: raw)
    first = run_scouts.run(companies, out, aggregators=False)
    seen = json.loads(out.read_text())[0]["first_seen_at"]
    assert seen and len(first) == 1

    def boom(*a, **k):
        raise requests.ConnectionError("offline")
    monkeypatch.setattr(workday, "fetch_raw", boom)
    assert len(run_scouts.run(companies, out, aggregators=False)) == 1
    assert json.loads(out.read_text())[0]["first_seen_at"] == seen

    monkeypatch.setattr(workday, "fetch_raw", lambda s, slug: raw)  # recovers: one row, same first_seen
    run_scouts.run(companies, out, aggregators=False)
    rows = json.loads(out.read_text())
    assert len(rows) == 1 and rows[0]["first_seen_at"] == seen


def test_robots_blocked_company_keeps_previous_jobs(tmp_path, monkeypatch):
    companies = tmp_path / "c.yaml"
    companies.write_text("smartrecruiters:\n  - {slug: Acme, name: Acme}\n")
    out = tmp_path / "out.json"
    prev = Job.build(source="smartrecruiters", title="PM", company="Acme", location="Pune", url="https://x/1")
    out.write_text(json.dumps([prev.model_dump()]))
    monkeypatch.setattr(smartrecruiters, "get_json",
                        lambda *a, **k: (_ for _ in ()).throw(RobotsDisallowed("blocked")))
    result = run_scouts.run(companies, out, aggregators=False)
    assert list(result) == [prev.id] and json.loads(out.read_text())[0]["id"] == prev.id


def test_first_seen_defaults_to_fetched_at_and_hours_calc():
    job = Job.build(source="x", title="PM", company="Acme", location="", url="https://x/1",
                    fetched_at="2026-10-16T09:00:00+00:00")
    assert job.first_seen_at == "2026-10-16T09:00:00+00:00"
    assert hours_old_at_first_seen("2026-10-16T00:00:00+00:00", job.first_seen_at) == 9
    assert hours_old_at_first_seen("2026-10-16", job.first_seen_at) == 9  # date-only = midnight UTC
    assert hours_old_at_first_seen("2026-10-17", job.first_seen_at) == 0  # never negative
    assert hours_old_at_first_seen(None, job.first_seen_at) is None
    assert hours_old_at_first_seen("garbage", job.first_seen_at) is None


def test_dashboard_row_carries_hours_at_first_seen():
    from dashboard.data import job_row
    row = job_row({"id": "a", "company": "Acme", "title": "PM", "url": "https://x/1",
                   "posted_date": "2026-10-16T00:00:00+00:00", "first_seen_at": "2026-10-16T05:30:00+00:00",
                   "screen": {"status": "scored", "stage2": {"score": 80, "verdict": "apply"}}})
    assert row["hours_at_first_seen"] == 5 and row["first_seen_at"].startswith("2026-10-16T05")
