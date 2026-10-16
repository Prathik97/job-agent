"""Screener tests. No network, no real profile.yaml, no real API: everything is mocked."""
import json
from types import SimpleNamespace

import pytest

from scout.schema import Job
from screener import run_screener, stage1, stage2
from screener.cache import Cache
from screener.llm import ScreenError, Usage, extract_json
from screener.rules import Rules, extract_years, phrase_re
from screener.text import truncate

PROFILE = {
    "candidate": {"years_experience": 7},
    "titles": {
        "tier_a": ["Senior Product Manager", "Sr. Product Manager", "Staff Product Manager", "SPM"],
        "tier_b": ["Principal Product Manager", "Group Product Manager"],
        "tier_c": ["VP Product", "Director of Product"],
    },
    "title_reject_keywords": ["Product Marketing", "Sales", "Account Manager", "Intern", "APM",
                              "Associate Product Manager", "Project Manager", "Program Manager"],
    "experience": {"min_required_in_jd": 5, "stretch_if_jd_requires_over": 9,
                   "hard_reject_if_jd_requires_over": 14},
    "location": {"onsite_or_hybrid_cities": ["Bengaluru", "Bangalore"], "remote_allowed": True,
                 "remote_must_be_india_eligible": True,
                 "reject_if_text_contains": ["US only", "work authorization required"]},
    "domains_ranked": ["B2B SaaS", "Payments and billing"],
    "deal_breakers": ["Commission only", "TODO: add your own"],
    "compensation": {"floor_lpa_inr": 35},
    "company_preferences": {"stage": ["Series B"]},
    "scoring": {"weights": {"role_and_title_fit": 25, "domain_fit": 20, "seniority_fit": 20,
                            "location_fit": 15, "company_and_growth": 10, "comp_signal": 10},
                "thresholds": {"shortlist_min_score": 70}},
}


def job(title="Senior Product Manager", location="Bengaluru", desc="Own the roadmap.",
        remote="unknown", jid=None) -> Job:
    j = Job.build(source="t", title=title, company="Acme", location=location,
                  url="https://x", description=desc, remote_type=remote)
    return j if jid is None else j.model_copy(update={"id": jid})


@pytest.fixture
def rules():
    return Rules.from_profile(PROFILE)


# ---------- layer 1: rules ----------

def test_salesforce_product_manager_never_rejected(rules):
    r = rules.evaluate(job("Product Manager, Salesforce Integrations"))
    assert r.status != "drop"
    assert rules.title_reject("Product Manager, Salesforce Integrations") is None


@pytest.mark.parametrize("title,rejected", [
    ("Sales Manager", True), ("Account Manager, EMEA", True),
    ("Product Manager, Sales Tools", False),      # conditional reject skipped: has "product"
    ("Senior Product Marketing Manager", True),   # always rejected
    ("Intern, Product", True), ("Internal Tools Product Manager", False),  # whole words only
    ("APM", True), ("Senior Product Manager", False),
    ("Technical Program Manager", True),
])
def test_reject_keywords_whole_word(rules, title, rejected):
    assert (rules.title_reject(title) is not None) is rejected


@pytest.mark.parametrize("title,tier", [
    ("Senior Product Manager, Payments", "A"), ("sr. product manager", "A"),
    ("Group Product Manager", "B"), ("Director of Product", "C"),
    ("Product Manager", None), ("Senior Software Engineer", None),
])
def test_title_tier(rules, title, tier):
    assert rules.title_tier(title) == tier


def test_untiered_routing(rules):
    plain = rules.evaluate(job("Product Manager"))           # allowed through, to stage 1
    assert plain.status == "stage1" and plain.tier == "untiered" and "untiered" in plain.flags
    assert rules.evaluate(job("Software Engineer")).drop_reason == "title_untiered"
    assert rules.evaluate(job("Product Lead, Growth")).status == "stage1"
    assert rules.evaluate(job("Delivery Lead (Program)")).status == "stage1"


def test_tiered_bengaluru_goes_straight_to_stage2(rules):
    r = rules.evaluate(job())
    assert (r.status, r.tier) == ("stage2", "A")


@pytest.mark.parametrize("text,expected", [
    ("5+ years of experience in product", 5),
    ("3-5 years of product management experience", 5),     # upper end of a range
    ("3 to 7 yrs experience", 7),
    ("minimum of seven years' experience", 7),
    ("Experience: 8+ years", 8),
    ("We were founded 12 years ago", None),                # not an experience requirement
    ("7+ years PM experience and 3+ years in B2B experience", 7),
    ("", None),
])
def test_extract_years(text, expected):
    assert extract_years(text) == expected


def test_years_flags_and_hard_reject(rules):
    low = rules.evaluate(job(desc="2-4 years of experience"))
    assert low.status == "stage2" and "below_range" in low.flags      # continues, not dropped
    assert "stretch_years" in rules.evaluate(job(desc="10+ years of experience")).flags
    hard = rules.evaluate(job(desc="15+ years of experience"))
    assert hard.status == "drop" and hard.drop_reason == "years_over_hard_limit"


def test_location_reject_phrase(rules):
    r = rules.evaluate(job(desc="This role is US only."))
    assert r.status == "drop" and r.drop_category == "location_reject"


@pytest.mark.parametrize("loc,remote,expected", [
    ("Bengaluru, Karnataka", "hybrid", "ok"), ("Remote - India", "remote", "ok"),
    ("Remote", "remote", "ambiguous"), ("N/A", "unknown", "ambiguous"),
    ("India", "unknown", "ambiguous"), ("London", "unknown", "bad"),
    ("Mumbai, India", "onsite", "bad"), ("San Francisco, Remote", "unknown", "ambiguous"),
])
def test_location_class(rules, loc, remote, expected):
    assert rules.location_class(job(location=loc, remote=remote)) == expected


def test_phrase_re_whole_word():
    assert phrase_re("Sales").search("Head of Sales") and not phrase_re("Sales").search("Salesforce")


def test_truncate_keeps_head_and_tail():
    text = "H" * 1000 + "M" * 5000 + "T" * 1000
    out = truncate(text, 100)
    assert out.startswith("H" * 70) and out.endswith("T" * 30) and "M" not in out
    assert truncate("short", 100) == "short"


# ---------- LLM layers (fake client) ----------

class FakeClient:
    """Returns queued replies; counts calls. Mimics client.messages.create."""
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        import inspect
        from anthropic.resources.messages import Messages
        allowed = set(inspect.signature(Messages.create).parameters)
        assert set(kw) <= allowed, f"not accepted by the installed SDK: {set(kw) - allowed}"
        self.calls.append(kw)
        text = self.replies.pop(0)
        return SimpleNamespace(content=[SimpleNamespace(text=text)],
                               usage=SimpleNamespace(input_tokens=1000, output_tokens=100))


GOOD_S2 = json.dumps({"scores": {"role_and_title_fit": 22, "domain_fit": 15, "seniority_fit": 16,
                                 "location_fit": 15, "company_and_growth": 7, "comp_signal": 5},
                      "reasons": ["fits"], "red_flags": []})


def test_extract_json_tolerates_fences():
    assert extract_json('```json\n{"keep": true}\n```') == {"keep": True}
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_stage1_valid_and_model_id(rules):
    from config import HAIKU
    client = FakeClient(['{"keep": true, "reason": "ok"}'])
    d = stage1.decide(client, job("Product Manager"), rules.evaluate(job("Product Manager")), Usage())
    assert d.keep is True and client.calls[0]["model"] == HAIKU


def test_stage1_prompt_includes_rule_years_and_flags(rules):
    j = job("Product Manager", desc="8+ years of experience")
    r = rules.evaluate(j)
    prompt = stage1.build_user(j, r)
    assert "years of experience required=8" in prompt and "untiered" in prompt


def test_invalid_json_retries_once_then_succeeds(rules):
    client = FakeClient(["garbage", '{"keep": false, "reason": "junior"}'])
    d = stage1.decide(client, job("Product Manager"), rules.evaluate(job("Product Manager")), Usage())
    assert d.keep is False and len(client.calls) == 2


def test_invalid_json_twice_raises(rules):
    client = FakeClient(["garbage", '{"keep": "maybe-not-bool-list"}'])
    with pytest.raises(ScreenError):
        stage1.decide(client, job("Product Manager"), rules.evaluate(job("Product Manager")), Usage())
    assert len(client.calls) == 2


def test_stage2_score_computed_in_code(rules):
    ctx = stage2.build_context(PROFILE)
    from config import SONNET
    client = FakeClient([GOOD_S2])
    res = stage2.score_job(client, job(), rules.evaluate(job()), ctx, Usage())
    assert res.score == 80 and res.verdict == "apply"
    assert client.calls[0]["model"] == SONNET


@pytest.mark.parametrize("score,verdict", [(85, "apply"), (70, "apply"), (60, "stretch"), (30, "skip")])
def test_verdict_thresholds(score, verdict):
    assert stage2.verdict_for(score, 70) == verdict


def test_stage2_rejects_out_of_range_or_missing_categories(rules):
    ctx = stage2.build_context(PROFILE)
    bad = json.loads(GOOD_S2)
    bad["scores"]["domain_fit"] = 99
    missing = json.loads(GOOD_S2)
    del missing["scores"]["comp_signal"]
    validate = stage2.make_validator(ctx)
    for data in (bad, missing):
        with pytest.raises(ValueError):
            validate(data)


def test_stage2_prompt_has_no_candidate_name():
    assert "Prathik" not in stage2.build_context(PROFILE).system


# ---------- pipeline: caching, limit, dry-run, idempotence ----------

@pytest.fixture
def env(tmp_path):
    import yaml
    jobs = [job("Senior Product Manager", "Bengaluru", "Own roadmap", jid="a"),
            job("Product Manager", "Bengaluru", "Own roadmap too", jid="b"),
            job("Software Engineer", "Bengaluru", "code", jid="c")]
    paths = SimpleNamespace(
        jobs=tmp_path / "jobs.json", out=tmp_path / "out.json",
        profile=tmp_path / "profile.yaml", cache=tmp_path / "cache.json")
    paths.jobs.write_text(json.dumps([j.model_dump() for j in jobs]))
    paths.profile.write_text(yaml.safe_dump(PROFILE))
    return paths


def go(env, client=None, **kw):
    return run_screener.run(env.jobs, env.out, env.profile, env.cache, client=client, **kw)


def test_dry_run_makes_no_calls_and_writes_nothing(env):
    client = FakeClient([])
    funnel = go(env, client, dry_run=True)
    assert client.calls == [] and not env.out.exists() and not env.cache.exists()
    assert funnel["dropped_by_rules"] == 1 and funnel["direct_to_stage2"] == 1
    assert funnel["to_stage1"] == 1 and funnel["est_cost_total"] > 0


def test_full_run_then_rerun_spends_nothing(env):
    client = FakeClient([GOOD_S2, '{"keep": true, "reason": "ok"}', GOOD_S2])
    go(env, client)
    rows = json.loads(env.out.read_text())
    assert {r["id"] for r in rows} == {"a", "b"}
    assert all(r["screen"]["status"] == "scored" for r in rows)
    n_calls = len(client.calls)

    client2 = FakeClient([])              # any call would raise IndexError
    go(env, client2)
    assert client2.calls == [] and n_calls == 3
    assert len(json.loads(env.out.read_text())) == 2          # no duplicates


def test_description_change_busts_cache(env):
    client = FakeClient([GOOD_S2, '{"keep": true, "reason": "ok"}', GOOD_S2])
    go(env, client)
    jobs = json.loads(env.jobs.read_text())
    jobs[0]["description"] = "Completely new description"
    env.jobs.write_text(json.dumps(jobs))
    client2 = FakeClient([GOOD_S2])
    go(env, client2)
    assert len(client2.calls) == 1


def test_stage1_drop_is_recorded_and_skips_stage2(env):
    client = FakeClient([GOOD_S2, '{"keep": false, "reason": "junior"}'])
    go(env, client)
    rows = {r["id"]: r for r in json.loads(env.out.read_text())}
    assert rows["b"]["screen"]["status"] == "dropped_stage1"
    assert rows["a"]["screen"]["status"] == "scored"
    assert len(client.calls) == 2


def test_invalid_json_twice_is_skipped_logged_and_cached(env):
    go(env, FakeClient([GOOD_S2]), limit=1)                   # job "a" scored and cached
    go(env, FakeClient(["bad", "bad"]))                       # job "b": stage 1 invalid twice
    rows = {r["id"]: r for r in json.loads(env.out.read_text())}
    assert rows["b"]["screen"]["status"] == "skipped_invalid_json"
    assert rows["a"]["screen"]["status"] == "scored"          # earlier good result kept
    client3 = FakeClient([])
    go(env, client3)                                          # failure is cached: no re-spend
    assert client3.calls == []


def test_limit_caps_processed_jobs(env):
    client = FakeClient([GOOD_S2])
    funnel = go(env, client, limit=1)
    assert funnel["processing_now"] == 1 and len(client.calls) == 1


def test_api_error_not_cached_and_does_not_clobber_output(env):
    import anthropic
    import httpx

    class Boom:
        messages = SimpleNamespace(create=lambda **kw: (_ for _ in ()).throw(
            anthropic.APIConnectionError(request=httpx.Request("POST", "https://x"))))

    go(env, FakeClient([GOOD_S2, '{"keep": true, "reason": "ok"}', GOOD_S2]))
    before = env.out.read_text()
    jobs = json.loads(env.jobs.read_text())
    jobs[0]["description"] = "changed so cache misses"
    env.jobs.write_text(json.dumps(jobs))
    go(env, Boom())
    assert env.out.read_text() == before          # failed fetch did not clobber good output


def test_cache_corrupt_file_starts_empty(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("{not json")
    assert Cache(p).data == {}
