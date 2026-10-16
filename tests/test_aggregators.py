"""Stage 1 tests: title normalization, aggregator scouts, budgets, ATS-wins merge. No network."""
import json
from datetime import date
from types import SimpleNamespace

import pytest
import requests

from scout import adzuna, jsearch, run_scouts
from scout.aggregator import QuotaStop, api_get
from scout.normalize import locations_compatible, merge_sources, normalize_company
from scout.queries import Query, build_queries, select_queries
from scout.schema import Job
from scout.usage import UsageTracker
from screener import stage2
from screener.models import RuleResult
from screener.rules import Rules
from screener.titles import normalize_title
from tests.test_screener import PROFILE


# ---------- title normalization ----------

@pytest.mark.parametrize("raw,expected", [
    ("Product Manager II", "Product Manager 2"),
    ("Product Manager III", "Product Manager 3"),
    ("Sr. Product Manager", "Senior Product Manager"),
    ("Sr Product Manager", "Senior Product Manager"),
    ("Jr Product Manager", "Junior Product Manager"),
    ("Jr. PM IV", "Junior PM 4"),
    ("Product Manager - Series", "Product Manager - Series"),   # no false roman/Sr hits
])
def test_normalize_title(raw, expected):
    assert normalize_title(raw) == expected


def test_roman_numerals_match_tiers():
    profile = json.loads(json.dumps(PROFILE))
    profile["titles"]["tier_a"] += ["Product Manager 2", "Product Manager 3", "PM II"]
    rules = Rules.from_profile(profile)
    assert rules.title_tier("Product Manager II") == "A"
    assert rules.title_tier("Product Manager III") == "A"
    assert rules.title_tier("PM 2") == "A"          # profile says "PM II"
    assert rules.title_tier("Sr. Product Manager") == "A"
    assert rules.title_tier("Sr Product Manager") == "A"
    assert rules.title_tier("Principal Product Manager") == "B"


def test_junior_title_is_not_tiered_as_senior():
    rules = Rules.from_profile(PROFILE)
    assert rules.title_tier("Jr Product Manager") is None


# ---------- company / location matching ----------

def make(source, title="Product Manager 2", company="Meesho", location="Bengaluru", **kw):
    return Job.build(source=source, title=title, company=company, location=location,
                     url=f"https://x/{source}", **kw)


def test_company_normalization():
    assert normalize_company("Meesho Pvt Ltd") == normalize_company("Meesho")
    assert normalize_company("Acme Technologies India Pvt. Ltd.") == "acme"
    assert normalize_company("Labs") == "labs"   # never reduced to nothing


def test_ats_wins_and_notes_aggregator():
    out = merge_sources([make("jsearch", company="Meesho Pvt Ltd", title="Product Manager II"),
                         make("greenhouse")])
    assert [j.source for j in out] == ["greenhouse"]
    assert out[0].also_listed_on == ["jsearch"]


def test_same_title_in_two_cities_stays_two_jobs():
    out = merge_sources([make("greenhouse", location="San Francisco, CA, US"),
                         make("jsearch", location="Bengaluru, Karnataka, India")])
    assert sorted(j.source for j in out) == ["greenhouse", "jsearch"]
    out = merge_sources([make("greenhouse", location="Mumbai, India"),
                         make("adzuna", location="Bengaluru, India")])
    assert len(out) == 2


def test_unknown_or_plain_remote_location_still_matches():
    assert len(merge_sources([make("lever", location="Remote"), make("adzuna")])) == 1
    assert len(merge_sources([make("lever", location=""), make("adzuna")])) == 1
    assert len(merge_sources([make("ashby"), make("jsearch", location="")])) == 1


def test_no_fuzzy_matching():
    out = merge_sources([make("greenhouse", title="Senior Product Manager"),
                         make("jsearch", title="Senior Product Manager, Payments")])
    assert len(out) == 2


def test_same_city_alias_and_country_only():
    assert locations_compatible("Bangalore", "Bengaluru, Karnataka, India")
    assert locations_compatible("Remote - India", "Bengaluru, India")
    assert not locations_compatible("United States", "Bengaluru, India")


def test_same_source_duplicates_not_merged():
    assert len(merge_sources([make("greenhouse", location="Bengaluru"),
                              make("greenhouse", location="Bengaluru, India")])) == 2


def test_aggregator_listed_by_both_keeps_one():
    out = merge_sources([make("adzuna"), make("jsearch")])
    assert [(j.source, j.also_listed_on) for j in out] == [("jsearch", ["adzuna"])]


# ---------- queries and budget ----------

def test_queries_cover_both_locations_and_dedupe_stems():
    profile = {"titles": {"tier_a": ["Product Manager 2", "PM 2", "Senior Product Manager",
                                     "Technical Program Manager", "Program Manager"],
                          "tier_b": ["Principal Product Manager", "Head of Product"],
                          "tier_c": ["VP Product"]}}
    qs = build_queries(profile)
    assert {q.stem for q in qs} == {"product manager", "program manager", "head of product"}
    assert {q.location for q in qs} == {"bengaluru", "remote_india"}


def test_select_queries_rotates():
    qs = [Query(str(i), "bengaluru") for i in range(5)]
    first, cur = select_queries(qs, 2, 0)
    second, cur = select_queries(qs, 2, cur)
    third, _ = select_queries(qs, 2, cur)
    assert [q.stem for q in first + second + third] == ["0", "1", "2", "3", "4", "0"]


def test_usage_budget_and_month_reset(tmp_path):
    path = tmp_path / "u.json"
    t = UsageTracker(path, {"x": {"month": 2, "day": 1}}, today=date(2026, 10, 1))
    assert t.allowed("x")
    t.record("x")
    assert not t.allowed("x")                       # daily limit hit
    t = UsageTracker(path, {"x": {"month": 2, "day": 1}}, today=date(2026, 10, 2))
    assert t.allowed("x")
    t.record("x")
    assert not t.allowed("x")                       # monthly limit hit
    t = UsageTracker(path, {"x": {"month": 2, "day": 1}}, today=date(2026, 11, 1))
    assert t.allowed("x") and t.month_calls("x") == 0


# ---------- aggregator clients (mocked HTTP) ----------

class FakeSession:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, params, headers))
        status, payload = self.responses.pop(0)
        return SimpleNamespace(status_code=status, json=lambda: payload)


ADZUNA_RAW = {"results": [
    {"title": "<b>Senior</b> Product Manager", "company": {"display_name": "Meesho"},
     "location": {"display_name": "Bengaluru, Karnataka"}, "redirect_url": "https://adzuna/1",
     "description": "Own the roadmap …", "created": "2026-10-01T00:00:00Z"},
    {"title": "no url", "company": {"display_name": "X"}},
]}
JSEARCH_RAW = {"data": [
    {"job_title": "Senior Product Manager", "employer_name": "Meesho", "job_city": "Bengaluru",
     "job_state": "Karnataka", "job_country": "IN", "job_is_remote": False,
     "job_description": "Own the roadmap", "job_apply_link": "https://js/1",
     "job_posted_at_datetime_utc": "2026-10-02T00:00:00Z"},
    {"job_title": "Remote PM", "employer_name": "Acme", "job_is_remote": True,
     "job_apply_link": "https://js/2"},
]}
Q = [Query("product manager", "bengaluru"), Query("product manager", "remote_india")]


def test_adzuna_parse_and_partial_flag():
    jobs = adzuna.parse(ADZUNA_RAW)
    assert len(jobs) == 1 and jobs[0].jd_partial and jobs[0].source == "adzuna"
    assert jobs[0].title == "Senior Product Manager"


def test_jsearch_parse_and_remote():
    jobs = jsearch.parse(JSEARCH_RAW)
    assert [j.remote_type for j in jobs] == ["unknown", "remote"]
    assert jobs[1].location == "Remote" and all(j.jd_partial for j in jobs)


def test_missing_keys_skip_without_calls(monkeypatch, tmp_path):
    for k in ("ADZUNA_APP_ID", "ADZUNA_APP_KEY", "RAPIDAPI_KEY"):
        monkeypatch.delenv(k, raising=False)
    session = FakeSession([])
    t = UsageTracker(tmp_path / "u.json")
    assert adzuna.scout(Q, t, session) == [] and jsearch.scout(Q, t, session) == []
    assert session.calls == [] and not (tmp_path / "u.json").exists()


def test_adzuna_scout_counts_calls(monkeypatch, tmp_path):
    monkeypatch.setenv("ADZUNA_APP_ID", "id")
    monkeypatch.setenv("ADZUNA_APP_KEY", "key")
    session = FakeSession([(200, ADZUNA_RAW), (200, ADZUNA_RAW)])
    t = UsageTracker(tmp_path / "u.json")
    jobs = adzuna.scout(Q, t, session)
    assert len(jobs) == 1 and t.month_calls("adzuna") == 2
    assert session.calls[0][1]["where"] == "Bengaluru"


def test_429_stops_that_api_only(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("RAPIDAPI_KEY", "secret-key")
    session = FakeSession([(200, JSEARCH_RAW), (429, {}), (200, JSEARCH_RAW)])
    t = UsageTracker(tmp_path / "u.json")
    qs = Q + [Query("program manager", "bengaluru")]
    jobs = jsearch.scout(qs, t, session)
    assert len(session.calls) == 2           # third query never sent
    assert len(jobs) == 2                    # first query's results kept
    assert "secret-key" not in caplog.text


def test_budget_exhausted_makes_no_call(monkeypatch, tmp_path):
    monkeypatch.setenv("RAPIDAPI_KEY", "k")
    t = UsageTracker(tmp_path / "u.json")
    t.data["jsearch"] = {"month": date.today().strftime("%Y-%m"), "month_calls": 100,
                         "day": date.today().isoformat(), "day_calls": 0}
    session = FakeSession([(200, JSEARCH_RAW)])
    assert jsearch.scout(Q, t, session) == [] and session.calls == []


def test_api_get_hides_url_and_key_in_errors():
    class Boom:
        def get(self, *a, **k):
            raise requests.ConnectionError("https://api?app_key=SECRET")
    with pytest.raises(ValueError) as e:
        api_get(Boom(), "adzuna", "https://api", {"app_key": "SECRET"})
    assert "SECRET" not in str(e.value)
    with pytest.raises(QuotaStop):
        api_get(FakeSession([(403, {})]), "jsearch", "u", {})


# ---------- pipeline merge behaviour ----------

def test_run_merges_aggregator_into_ats_and_is_idempotent(tmp_path, monkeypatch):
    companies, out = tmp_path / "c.yaml", tmp_path / "out.json"
    companies.write_text("greenhouse: [acme]\n")
    profile = tmp_path / "p.yaml"
    profile.write_text("titles: {tier_a: [Senior Product Manager]}\n")
    ats = make("greenhouse", title="Senior Product Manager", company="Acme")
    agg = make("jsearch", title="Sr. Product Manager", company="Acme Inc", jd_partial=True)
    only_agg = make("adzuna", title="Senior Product Manager", company="Other", jd_partial=True)
    monkeypatch.setattr(run_scouts, "scout_company", lambda *a: [ats])
    monkeypatch.setattr(jsearch, "scout", lambda *a, **k: [agg])
    monkeypatch.setattr(adzuna, "scout", lambda *a, **k: [only_agg])
    kw = dict(profile_path=profile, usage_path=tmp_path / "u.json")
    first = run_scouts.run(companies, out, **kw)
    second = run_scouts.run(companies, out, **kw)
    assert sorted(j.source for j in first.values()) == ["adzuna", "greenhouse"]
    assert len(first) == len(second) == 2
    gh = next(j for j in second.values() if j.source == "greenhouse")
    assert gh.also_listed_on == ["jsearch"]


def test_empty_aggregator_result_keeps_previous_jobs(tmp_path, monkeypatch):
    companies, out = tmp_path / "c.yaml", tmp_path / "out.json"
    companies.write_text("greenhouse: [acme]\n")
    profile = tmp_path / "p.yaml"
    profile.write_text("titles: {tier_a: [Senior Product Manager]}\n")
    monkeypatch.setattr(run_scouts, "scout_company", lambda *a: [])
    monkeypatch.setattr(adzuna, "scout", lambda *a, **k: [])
    monkeypatch.setattr(jsearch, "scout", lambda *a, **k: [make("jsearch", company="Z")])
    kw = dict(profile_path=profile, usage_path=tmp_path / "u.json")
    assert len(run_scouts.run(companies, out, **kw)) == 1
    monkeypatch.setattr(jsearch, "scout", lambda *a, **k: [])
    assert len(run_scouts.run(companies, out, **kw)) == 1
    assert len(json.loads(out.read_text())) == 1


# ---------- stage 2 caps ----------

def entry(verdict="apply"):
    return {"verdict": verdict, "role_family": "product"}


def test_partial_jd_caps_at_stretch_with_note():
    rule = RuleResult(status="stage2", tier="A", location_status="confirmed")
    out = stage2.apply_caps(entry(), rule, jd_partial=True)
    assert out["verdict"] == "stretch"
    assert stage2.PARTIAL_JD_NOTE in out["notes"]
    assert stage2.apply_caps(entry(), rule)["verdict"] == "apply"
    assert stage2.apply_caps(entry("skip"), rule, jd_partial=True)["verdict"] == "skip"


def test_ats_job_notes_aggregator():
    rule = RuleResult(status="stage2", tier="A", location_status="confirmed")
    out = stage2.apply_caps(entry(), rule, also_listed_on=("jsearch",))
    assert out["verdict"] == "apply" and "also listed on jsearch" in out["notes"]
