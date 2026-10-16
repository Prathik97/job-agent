"""Firecrawl scout tests. Firecrawl and Anthropic are mocked; nothing hits the network."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from scout import firecrawl_scout as fc
from scout import http
from scout.schema import Job

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)
PROFILE = """
titles:
  tier_a: ["Senior Product Manager"]
  tier_b: ["Product Manager"]
  tier_c: []
title_reject_keywords: ["engineer", "designer"]
experience: {min_required_in_jd: 4, stretch_if_jd_requires_over: 9, hard_reject_if_jd_requires_over: 14}
location:
  onsite_or_hybrid_cities: ["Bengaluru"]
  remote_allowed: true
  remote_must_be_india_eligible: true
  reject_if_text_contains: ["US only"]
"""
LISTING = {"jobs": [
    {"title": "Senior Product Manager", "location": "Bengaluru", "url": "/jobs/pm-1"},
    {"title": "Product Designer", "location": "Bengaluru", "url": "/jobs/design-1"},
    {"title": "Product Manager", "location": "Bengaluru", "url": "javascript:void(0)"},
]}


class FakeSession:
    """post = Firecrawl, get = robots.txt."""

    def __init__(self, robots="User-agent: *\nAllow: /", markdown="# page"):
        self.robots, self.markdown, self.posts = robots, markdown, []

    def get(self, url, timeout=None):
        return SimpleNamespace(status_code=200, text=self.robots)

    def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append(json["url"])
        md = self.markdown(json["url"]) if callable(self.markdown) else self.markdown
        return SimpleNamespace(raise_for_status=lambda: None,
                               json=lambda: {"success": bool(md), "data": {"markdown": md}})


class FakeClient:
    def __init__(self, payload):
        self.calls = 0
        self.messages = SimpleNamespace(create=self._create)
        self.payload = payload

    def _create(self, **kw):
        self.calls += 1
        return SimpleNamespace(content=[SimpleNamespace(text=json.dumps(self.payload))],
                               usage=SimpleNamespace(input_tokens=100, output_tokens=50))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "test-key")
    monkeypatch.setattr(http, "_robots", {})
    monkeypatch.setattr(http, "MIN_DELAY_SECONDS", 0)
    report = tmp_path / "report.json"
    report.write_text(json.dumps([{"company": "Acme", "careers_url": "https://acme.test/careers",
                                   "robots": "allowed", "ats": None, "slug": None,
                                   "endpoint_jobs": None, "note": "no known ATS"}]))
    profile = tmp_path / "profile.yaml"
    profile.write_text(PROFILE)
    return SimpleNamespace(tmp=tmp_path, report=report, profile=profile)


def go(env, session, client=None, **kw):
    t = env.tmp
    return fc.run(env.report, env.profile, cache_dir=t / "cache", usage_path=t / "usage.json",
                  jobs_path=t / "fc_jobs.json", scout_path=t / "scout_jobs.json", session=session,
                  client=client or FakeClient(LISTING), now=NOW, **kw)


def md_for(url):
    return "Own the roadmap in Bengaluru" if "pm-1" in url else "# careers listing"


def test_eligible_pages_skips_blocked_sites():
    base = {"careers_url": "https://x.test", "robots": "allowed", "endpoint_jobs": None, "note": ""}
    report = [
        {**base, "company": "Ok"},
        {**base, "company": "RobotsPage", "robots": "disallowed"},
        {**base, "company": "BotWall", "note": "careers page returned HTTP 403"},
        {**base, "company": "EndpointBlocked", "note": "endpoint blocked by robots.txt (x)"},
        {**base, "company": "Resolved", "endpoint_jobs": 5},
        {**base, "company": "Tenant", "ats": "darwinbox", "slug": "tenant"},
    ]
    pages = fc.eligible_pages(report)
    assert [p["company"] for p in pages] == ["Ok", "Tenant"]
    assert pages[1]["url"] == "https://tenant.darwinbox.in/ms/candidate/careers"


def test_listing_rules_then_full_page_only_for_passers(env):
    session = FakeSession(markdown=md_for)
    stats = go(env, session)
    # 1 listing page + 1 full page (the designer is dropped by title rules, the bad link ignored)
    assert stats["credits_used"] == 2
    assert session.posts == ["https://acme.test/careers", "https://acme.test/jobs/pm-1"]
    assert stats["companies"]["Acme"] == {"listed": 3, "passed_rules": 1, "full_jd": 1}
    saved = json.loads((env.tmp / "scout_jobs.json").read_text())
    assert len(saved) == 1 and saved[0]["source"] == "firecrawl"
    assert saved[0]["jd_partial"] is False and "roadmap" in saved[0]["description"]
    assert saved[0]["first_seen_at"]
    usage = json.loads((env.tmp / "usage.json").read_text())
    assert usage["firecrawl"]["month_calls"] == 2  # credits logged


def test_rerun_within_24h_costs_nothing(env):
    go(env, FakeSession(markdown=md_for))
    session, client = FakeSession(markdown=md_for), FakeClient(LISTING)
    stats = go(env, session, client)
    assert stats["credits_used"] == 0 and session.posts == [] and client.calls == 0
    assert len(json.loads((env.tmp / "scout_jobs.json").read_text())) == 1  # no duplicates


def test_cache_expires_after_24h(tmp_path):
    fc.cache_put("https://x.test", tmp_path, {"markdown": "m"}, NOW)
    assert fc.cache_get("https://x.test", tmp_path, NOW + timedelta(hours=23)) is not None
    assert fc.cache_get("https://x.test", tmp_path, NOW + timedelta(hours=25)) is None


def test_credit_cap_stops_cleanly_and_marks_partial(env):
    session = FakeSession(markdown=md_for)
    stats = go(env, session, max_credits=1)
    assert stats["capped"] is True and stats["credits_used"] == 1
    saved = json.loads((env.tmp / "scout_jobs.json").read_text())
    assert len(saved) == 1 and saved[0]["jd_partial"] is True and saved[0]["description"] == ""


def test_robots_disallowed_page_never_sent_to_firecrawl(env):
    session = FakeSession(robots="User-agent: *\nDisallow: /")
    stats = go(env, session)
    assert session.posts == [] and stats["credits_used"] == 0


def test_robots_disallowed_job_page_stays_partial(env):
    session = FakeSession(robots="User-agent: *\nDisallow: /jobs/", markdown=md_for)
    stats = go(env, session)
    assert session.posts == ["https://acme.test/careers"]
    assert json.loads((env.tmp / "scout_jobs.json").read_text())[0]["jd_partial"] is True
    assert stats["credits_used"] == 1


def test_failed_or_empty_firecrawl_never_replaces_existing(env):
    go(env, FakeSession(markdown=md_for))
    before = (env.tmp / "scout_jobs.json").read_text()
    for path in (env.tmp / "cache").glob("firecrawl_*.json"):
        path.unlink()  # force live calls again

    class Down(FakeSession):
        def post(self, *a, **k):
            raise requests.ConnectionError("down")
    stats = go(env, Down())
    assert (env.tmp / "scout_jobs.json").read_text() == before
    empty = go(env, FakeSession(markdown=""))
    assert (env.tmp / "scout_jobs.json").read_text() == before
    assert stats["credits_used"] == 1 and empty["credits_used"] == 1  # a failed call still costs


def test_merge_keeps_first_seen_and_full_text(tmp_path):
    path = tmp_path / "jobs.json"
    old = Job.build(source="firecrawl", title="PM", company="Acme", location="Bengaluru",
                    url="https://x/1", description="full text", jd_partial=False,
                    first_seen_at="2026-10-01T00:00:00+00:00")
    other = Job.build(source="greenhouse", title="Other", company="Zed", location="", url="https://z/1")
    path.write_text(json.dumps([old.model_dump(), other.model_dump()]))
    new = Job.build(source="firecrawl", title="PM", company="Acme", location="Bengaluru",
                    url="https://x/1", jd_partial=True)
    assert fc.merge_into(path, []) == 0  # empty result: file untouched
    fc.merge_into(path, [new])
    rows = {r["title"]: r for r in json.loads(path.read_text())}
    assert len(rows) == 2 and rows["Other"]["source"] == "greenhouse"
    assert rows["PM"]["first_seen_at"] == "2026-10-01T00:00:00+00:00"
    assert rows["PM"]["description"] == "full text" and rows["PM"]["jd_partial"] is False


def test_dry_run_spends_nothing(env):
    stats = go(env, FakeSession(), dry_run=True)
    assert stats["planned_pages"] and stats["credits_used"] == 0
