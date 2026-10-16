"""Scout tests. All HTTP is mocked; nothing hits the network."""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from scout import ashby, greenhouse, lever, run_scouts
from scout.http import cache_changed, html_to_text, infer_remote_type
from scout.schema import Job, make_job_id

GREENHOUSE_RAW = {"jobs": [
    {"title": "Senior Product Manager", "absolute_url": "https://x.com/gh/1",
     "location": {"name": "Bengaluru, India"}, "first_published": "2026-09-01T10:00:00-04:00",
     "content": "&lt;p&gt;Lead &amp;amp; ship&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Own roadmap&lt;/li&gt;&lt;/ul&gt;"},
    {"title": "Broken job without url"},
]}
LEVER_RAW = [
    {"text": "Product Manager", "hostedUrl": "https://jobs.lever.co/acme/1",
     "categories": {"location": "Remote - India", "commitment": "Full-time"},
     "descriptionPlain": "Do things", "createdAt": 1788000000000, "workplaceType": "remote"},
    {"id": "no-url", "text": "Bad"},
]
ASHBY_RAW = {"jobs": [
    {"title": "Group PM", "jobUrl": "https://jobs.ashbyhq.com/acme/1", "location": "Bengaluru",
     "isRemote": False, "workplaceType": "Hybrid", "descriptionPlain": "Plain",
     "publishedAt": "2026-09-02T00:00:00.000+00:00"},
    {"title": "Hidden", "jobUrl": "https://jobs.ashbyhq.com/acme/2", "isListed": False},
]}


def test_job_id_stable_and_normalised():
    assert make_job_id("Acme", "PM", "Bengaluru") == make_job_id(" acme ", "pm", "BENGALURU")
    assert make_job_id("Acme", "PM", "Bengaluru") != make_job_id("Acme", "PM", "Delhi")


def test_greenhouse_parse():
    jobs = greenhouse.parse(GREENHOUSE_RAW, "Acme")
    assert len(jobs) == 1  # malformed entry skipped
    job = jobs[0]
    assert job.source == "greenhouse" and job.company == "Acme"
    assert job.location == "Bengaluru, India"
    assert "<" not in job.description and "Lead & ship" in job.description
    assert job.posted_date.startswith("2026-09-01")
    assert job.id == make_job_id("Acme", "Senior Product Manager", "Bengaluru, India")


def test_lever_parse():
    jobs = lever.parse(LEVER_RAW, "Acme")
    assert len(jobs) == 1
    assert jobs[0].remote_type == "remote"
    assert jobs[0].posted_date.endswith("+00:00")


def test_ashby_parse_skips_unlisted():
    jobs = ashby.parse(ASHBY_RAW, "Acme")
    assert [j.title for j in jobs] == ["Group PM"]
    assert jobs[0].remote_type == "hybrid"


def test_schema_fields():
    job = ashby.parse(ASHBY_RAW, "Acme")[0]
    assert set(job.model_dump()) == {"id", "source", "title", "company", "location",
                                     "remote_type", "url", "description", "posted_date",
                                     "fetched_at", "jd_partial", "also_listed_on"}


@pytest.mark.parametrize("loc,hint,expected", [
    ("Remote - India", "", "remote"), ("Bengaluru (Hybrid)", "", "hybrid"),
    ("Bengaluru", "OnSite", "onsite"), ("Bengaluru", "", "unknown"),
])
def test_infer_remote_type(loc, hint, expected):
    assert infer_remote_type(loc, hint) == expected


def test_html_to_text():
    assert html_to_text("&lt;p&gt;a&lt;/p&gt;&lt;p&gt;b&lt;/p&gt;") == "a\nb"


def test_cache_changed(tmp_path):
    assert cache_changed("x", "y", {"a": 1}, tmp_path) is True
    assert cache_changed("x", "y", {"a": 1}, tmp_path) is False
    assert cache_changed("x", "y", {"a": 2}, tmp_path) is True


def test_load_companies(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("greenhouse:\n  - stripe\nlever:\n  - {slug: my-co, name: My Co}\n")
    assert run_scouts.load_companies(p) == [("greenhouse", "stripe", "Stripe"),
                                            ("lever", "my-co", "My Co")]


def test_run_is_idempotent_and_survives_failures(tmp_path, monkeypatch):
    import requests
    monkeypatch.setattr("scout.http.CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr("scout.run_scouts.cache_changed",
                        lambda s, sl, raw: cache_changed(s, sl, raw, tmp_path / "cache"))
    companies = tmp_path / "c.yaml"
    companies.write_text("greenhouse: [a, b, gone]\nlever: [c]\nashby: [d]\n")
    out = tmp_path / "out.json"

    def fake_get_json(session, url, params=None, delay=0):
        if "/a/" in url or "/a" == url[-2:]:
            return GREENHOUSE_RAW
        if "boards/b/" in url:
            raise requests.ConnectionError("boom")
        if "boards/gone" in url:
            return None
        if "lever" in url:
            return LEVER_RAW
        return ASHBY_RAW

    for mod in (greenhouse, lever, ashby):
        monkeypatch.setattr(mod, "get_json", fake_get_json)
    first = run_scouts.run(companies, out, aggregators=False)
    second = run_scouts.run(companies, out, aggregators=False)
    assert len(first) == len(second) == 3
    assert len(json.loads(out.read_text())) == 3
    assert {j["source"] for j in json.loads(out.read_text())} == {"greenhouse", "lever", "ashby"}


def test_failed_fetch_does_not_wipe_previous_output(tmp_path, monkeypatch):
    import requests
    monkeypatch.setattr("scout.run_scouts.cache_changed", lambda *a: True)
    companies = tmp_path / "c.yaml"
    companies.write_text("ashby: [d]\n")
    out = tmp_path / "out.json"
    monkeypatch.setattr(ashby, "get_json", lambda *a, **k: ASHBY_RAW)
    run_scouts.run(companies, out, aggregators=False)

    def boom(*a, **k):
        raise requests.ConnectionError("offline")
    monkeypatch.setattr(ashby, "get_json", boom)
    assert len(run_scouts.run(companies, out, aggregators=False)) == 1
    assert len(json.loads(out.read_text())) == 1
