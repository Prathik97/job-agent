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
    t.data["jsearch"] = {"month": date.today().strftime("%Y-%m"), "month_calls": jsearch.LIMITS["month"],
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


@pytest.mark.parametrize("a,b", [
    ("Bangalore", "Bengaluru"), ("Gurgaon", "Gurugram"), ("Gurugram, Haryana, India", "Gurgaon"),
    ("Bengaluru, Karnataka, India", "Bangalore"), ("Bangalore", "Bengaluru, Karnataka, India"),
])
def test_city_aliases_are_compatible(a, b):
    assert locations_compatible(a, b)


def test_alias_jobs_merge_but_other_cities_do_not():
    assert len(merge_sources([make("greenhouse", location="Gurgaon"),
                              make("jsearch", location="Gurugram, Haryana, India")])) == 1
    assert len(merge_sources([make("greenhouse", location="Gurgaon"),
                              make("jsearch", location="Bengaluru, India")])) == 2


def test_jsearch_parses_search_v2_shape_and_uses_v2_endpoint():
    v2 = {"status": "OK", "data": {"jobs": JSEARCH_RAW["data"], "cursor": "x"}}
    assert [j.title for j in jsearch.parse(v2)] == ["Senior Product Manager", "Remote PM"]
    assert jsearch.URL.endswith("/search-v2")  # /search returns 404 "Endpoint does not exist"


def test_last_call_of_the_month_is_used_but_never_exceeded(monkeypatch, tmp_path):
    monkeypatch.setenv("RAPIDAPI_KEY", "k")
    monkeypatch.setattr(jsearch, "LIMITS", {"month": 100})
    t = UsageTracker(tmp_path / "u.json")
    t.data["jsearch"] = {"month": date.today().strftime("%Y-%m"), "month_calls": 99,
                         "day": date.today().isoformat(), "day_calls": 0}
    session = FakeSession([(200, JSEARCH_RAW), (200, JSEARCH_RAW)])
    jsearch.scout(Q, t, session)                       # two queries wanted, one call left
    assert len(session.calls) == 1 and t.month_calls("jsearch") == 100
    assert jsearch.scout(Q, t, FakeSession([(200, JSEARCH_RAW)])) == []   # nothing beyond the quota


def test_real_plan_budgets_and_comments():
    import config
    from pathlib import Path
    assert (config.JSEARCH_DAILY_CALLS, config.JSEARCH_MONTHLY_CALLS, config.JSEARCH_MAX_CALLS_PER_RUN) == (5, 180, 4)
    assert (config.ADZUNA_MAX_CALLS_PER_RUN, config.ADZUNA_MONTHLY_CALLS) == (5, 20)
    assert config.ADZUNA_MIN_SECONDS_BETWEEN_CALLS == 12 and config.ADZUNA_SCHEDULED_WEEKDAY == 0
    assert config.TIMEZONE == "Asia/Kolkata"
    assert jsearch.LIMITS == {"month": 180, "day": 5} and adzuna.LIMITS == {"month": 20}
    text = Path(config.__file__).read_text()
    assert "PROVISIONAL" not in text and "TODO" not in text
    assert "200 per month" in text and "25 per month" in text and "5 per minute" in text   # real plan limits noted


def test_jsearch_daily_limit_stops_before_exceeding(monkeypatch, tmp_path):
    monkeypatch.setenv("RAPIDAPI_KEY", "k")
    t = UsageTracker(tmp_path / "u.json")
    t.data["jsearch"] = {"month": t.today.strftime("%Y-%m"), "month_calls": 4,
                         "day": t.today.isoformat(), "day_calls": 4}   # 1 of the 5 daily calls left
    session = FakeSession([(200, JSEARCH_RAW), (200, JSEARCH_RAW)])
    jsearch.scout(Q, t, session)
    assert len(session.calls) == 1 and t.data["jsearch"]["day_calls"] == 5
    assert jsearch.scout(Q, t, FakeSession([(200, JSEARCH_RAW)])) == []


def test_adzuna_stops_at_20_calls_a_month_not_25(monkeypatch, tmp_path):
    monkeypatch.setenv("ADZUNA_APP_ID", "id")
    monkeypatch.setenv("ADZUNA_APP_KEY", "key")
    t = UsageTracker(tmp_path / "u.json")
    t.data["adzuna"] = {"month": t.today.strftime("%Y-%m"), "month_calls": 19,
                        "day": t.today.isoformat(), "day_calls": 0}
    session = FakeSession([(200, ADZUNA_RAW), (200, ADZUNA_RAW)])
    adzuna.scout(Q, t, session)
    assert len(session.calls) == 1 and t.month_calls("adzuna") == 20    # 5 spares of the plan's 25 stay unused
    assert adzuna.scout(Q, t, FakeSession([(200, ADZUNA_RAW)])) == []


def test_adzuna_per_run_cap_is_five(monkeypatch, tmp_path):
    monkeypatch.setenv("ADZUNA_APP_ID", "id")
    monkeypatch.setenv("ADZUNA_APP_KEY", "key")
    many = [Query(f"stem{i}", "bengaluru") for i in range(12)]
    session = FakeSession([(200, ADZUNA_RAW)] * 12)
    adzuna.scout(many, UsageTracker(tmp_path / "u.json"), session)
    assert len(session.calls) == 5


# ---------- Adzuna weekly schedule ----------

MONDAY, TUESDAY = date(2026, 10, 19), date(2026, 10, 20)


@pytest.fixture
def agg(monkeypatch, tmp_path):
    from scout import run_scouts
    called = []
    monkeypatch.setattr(run_scouts, "build_queries", lambda profile: Q)
    monkeypatch.setattr(run_scouts.adzuna, "scout", lambda q, t: called.append("adzuna") or [])
    monkeypatch.setattr(run_scouts.jsearch, "scout", lambda q, t: called.append("jsearch") or [])
    profile = tmp_path / "profile.yaml"
    profile.write_text("{}")
    return run_scouts, profile, called


def test_scheduled_run_skips_adzuna_on_non_mondays_and_says_so(agg, tmp_path, caplog):
    run_scouts, profile, called = agg
    caplog.set_level("INFO")
    run_scouts.run_aggregators(profile, UsageTracker(tmp_path / "u.json"), True, TUESDAY)
    assert called == ["jsearch"] and "Adzuna skipped, weekly schedule" in caplog.text


def test_scheduled_run_uses_adzuna_on_monday(agg, tmp_path):
    run_scouts, profile, called = agg
    run_scouts.run_aggregators(profile, UsageTracker(tmp_path / "u.json"), True, MONDAY)
    assert called == ["adzuna", "jsearch"]


def test_manual_run_may_use_adzuna_any_day(agg, tmp_path):
    run_scouts, profile, called = agg
    run_scouts.run_aggregators(profile, UsageTracker(tmp_path / "u.json"), False, TUESDAY)
    assert called == ["adzuna", "jsearch"]


def test_only_the_scheduled_trigger_restricts_adzuna(monkeypatch, tmp_path):
    import run_pipeline
    seen = {}

    def fake_run(*a, **kw):
        seen["weekly"] = kw["adzuna_weekly_only"]
        raise RuntimeError("stop here")
    monkeypatch.setattr(run_pipeline.run_scouts, "run", fake_run)
    for trigger, expected in (("scheduled", True), ("dashboard", False), ("cli", False)):
        run_pipeline.run(companies=tmp_path / "c.yaml", profile=tmp_path / "p.yaml", jobs=tmp_path / "j.json",
                         screened=tmp_path / "s.json", cache=tmp_path / "c.json", clean_dir=tmp_path / "clean",
                         usage=tmp_path / "u.json", trigger=trigger)
        assert seen["weekly"] is expected


# ---------- pacing ----------

def test_pace_waits_for_the_remaining_interval(monkeypatch):
    from scout import aggregator
    now, slept = [100.0], []
    monkeypatch.setattr(aggregator, "monotonic", lambda: now[0])
    monkeypatch.setattr(aggregator, "sleep", lambda s: (slept.append(s), now.__setitem__(0, now[0] + s)))
    aggregator._last_call.clear()
    aggregator.pace("x", 12)                 # first call: no wait
    now[0] += 5
    aggregator.pace("x", 12)                 # 5 s later: wait the other 7 s
    now[0] += 20
    aggregator.pace("x", 12)                 # long enough ago: no wait
    assert slept == [7.0]


def test_adzuna_calls_are_at_least_12_seconds_apart(monkeypatch, tmp_path):
    from scout import aggregator
    monkeypatch.setenv("ADZUNA_APP_ID", "id")
    monkeypatch.setenv("ADZUNA_APP_KEY", "key")
    now, slept = [0.0], []
    monkeypatch.setattr(aggregator, "monotonic", lambda: now[0])
    monkeypatch.setattr(aggregator, "sleep", lambda s: (slept.append(s), now.__setitem__(0, now[0] + s)))
    aggregator._last_call.clear()
    adzuna.scout([Query(f"s{i}", "bengaluru") for i in range(4)], UsageTracker(tmp_path / "u.json"),
                 FakeSession([(200, ADZUNA_RAW)] * 4))
    assert slept == [12.0, 12.0, 12.0]       # 4 calls, 3 gaps


# ---------- calendar month in IST ----------

def test_budget_month_follows_ist_not_utc():
    from datetime import datetime, timezone
    from scout.usage import today_local
    assert today_local(datetime(2026, 10, 31, 19, 0, tzinfo=timezone.utc)) == date(2026, 11, 1)   # 00:30 IST
    assert today_local(datetime(2026, 10, 31, 17, 0, tzinfo=timezone.utc)) == date(2026, 10, 31)  # 22:30 IST


def test_monthly_counter_resets_on_the_first_of_the_month(tmp_path):
    path = tmp_path / "u.json"
    t = UsageTracker(path, {"adzuna": {"month": 20}}, today=date(2026, 10, 31))
    for _ in range(20):
        t.record("adzuna")
    assert not t.allowed("adzuna") and t.month_left("adzuna") == 0
    t = UsageTracker(path, {"adzuna": {"month": 20}}, today=date(2026, 11, 1))
    assert t.allowed("adzuna") and t.month_left("adzuna") == 20 and t.month_calls("adzuna") == 0


def test_dashboard_shows_calls_left_this_month(tmp_path):
    import json
    from pathlib import Path
    from dashboard.data import api_budget
    raw = tmp_path / "raw"
    raw.mkdir()
    (raw / "api_usage.json").write_text(json.dumps({
        "jsearch": {"month": "2026-10", "month_calls": 68, "day": "2026-10-17", "day_calls": 0},
        "adzuna": {"month": "2026-09", "month_calls": 20, "day": "2026-09-30", "day_calls": 0}}))
    out = api_budget(tmp_path, today=date(2026, 10, 20))
    assert out["jsearch"] == {"left": 112, "limit": 180}      # "JSearch 112 of 180 left this month"
    assert out["adzuna"] == {"left": 20, "limit": 20}         # last month's usage does not carry over
    assert "left this month" in (Path(__file__).resolve().parent.parent / "dashboard/static/app.js").read_text()


def test_402_on_adzuna_stops_adzuna_but_jsearch_still_runs(monkeypatch, tmp_path):
    monkeypatch.setenv("ADZUNA_APP_ID", "id")
    monkeypatch.setenv("ADZUNA_APP_KEY", "key")
    monkeypatch.setenv("RAPIDAPI_KEY", "k")
    t = UsageTracker(tmp_path / "u.json")
    adz_session = FakeSession([(402, {}), (200, ADZUNA_RAW)])
    assert adzuna.scout(Q, t, adz_session) == [] and len(adz_session.calls) == 1   # stopped after the 402
    js_session = FakeSession([(200, JSEARCH_RAW), (200, JSEARCH_RAW)])
    assert len(jsearch.scout(Q, t, js_session)) == 2                               # unaffected
