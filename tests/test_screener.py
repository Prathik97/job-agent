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
        "tier_a": ["Senior Product Manager", "Sr. Product Manager", "Staff Product Manager", "SPM",
                   "Project Manager", "Senior Project Manager", "Program Manager",
                   "Technical Program Manager"],
        "tier_b": ["Principal Product Manager", "Group Product Manager"],
        "tier_c": ["VP Product", "Director of Product"],
    },
    "title_reject_keywords": ["Product Marketing", "Sales", "Account Manager", "Intern", "APM",
                              "Associate Product Manager"],
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
                "thresholds": {"shortlist_min_score": 70, "stretch_min_score": 55}},
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
    ("Technical Program Manager", False), ("Project Manager, Platform", False),  # no longer rejected
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
    assert rules.evaluate(job("Delivery Lead (Program)")).drop_reason == "title_untiered"


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
    ("Mumbai, India", "onsite", "bad"), ("San Francisco, Remote", "unknown", "non_india"),
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


GOOD_S2 = json.dumps({"role_family": "product", "scores": {"role_and_title_fit": 22, "domain_fit": 15, "seniority_fit": 16,
                                 "location_fit": 15, "company_and_growth": 7, "comp_signal": 5},
                      "reasons": ["fits"], "red_flags": []})


def test_extract_json_tolerates_fences():
    assert extract_json('```json\n{"decision": "keep"}\n```') == {"decision": "keep"}
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_stage1_valid_and_model_id(rules):
    from config import HAIKU
    client = FakeClient(['{"decision": "keep", "reason": "ok"}'])
    d = stage1.decide(client, job("Product Manager"), rules.evaluate(job("Product Manager")), Usage())
    assert d.keep is True and client.calls[0]["model"] == HAIKU


def test_stage1_prompt_includes_rule_years_and_flags(rules):
    j = job("Product Manager", desc="8+ years of experience")
    r = rules.evaluate(j)
    prompt = stage1.build_user(j, r)
    assert "years of experience required=8" in prompt and "untiered" in prompt


def test_invalid_json_retries_once_then_succeeds(rules):
    client = FakeClient(["garbage", '{"decision": "drop", "reason": "junior"}'])
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
    assert stage2.verdict_for(score, 70, 55) == verdict


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
    client = FakeClient([GOOD_S2, '{"decision": "keep", "reason": "ok"}', GOOD_S2])
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
    client = FakeClient([GOOD_S2, '{"decision": "keep", "reason": "ok"}', GOOD_S2])
    go(env, client)
    jobs = json.loads(env.jobs.read_text())
    jobs[0]["description"] = "Completely new description"
    env.jobs.write_text(json.dumps(jobs))
    client2 = FakeClient([GOOD_S2])
    go(env, client2)
    assert len(client2.calls) == 1


def test_stage1_drop_is_recorded_and_skips_stage2(env):
    client = FakeClient([GOOD_S2, '{"decision": "drop", "reason": "junior"}'])
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

    go(env, FakeClient([GOOD_S2, '{"decision": "keep", "reason": "ok"}', GOOD_S2]))
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


# ---------- tuning round: profile-driven stretch, PM-role pattern, blocklist, prompts, cache ----------

def test_stretch_min_comes_from_profile_not_config():
    import config
    assert not hasattr(config, "STRETCH_MIN_SCORE")
    ctx = stage2.build_context(PROFILE)
    assert (ctx.shortlist_min, ctx.stretch_min) == (70, 55)
    custom = json.loads(json.dumps(PROFILE))
    custom["scoring"]["thresholds"]["stretch_min_score"] = 40
    assert stage2.build_context(custom).stretch_min == 40
    broken = json.loads(json.dumps(PROFILE))
    del broken["scoring"]["thresholds"]["stretch_min_score"]
    with pytest.raises(KeyError):
        stage2.build_context(broken)


def test_program_and_project_manager_are_tier_a_not_rejected(rules):
    for t in ("Program Manager", "Technical Program Manager", "Senior Project Manager"):
        assert rules.title_reject(t) is None and rules.title_tier(t) == "A"
    assert rules.evaluate(job("Technical Program Manager")).status == "stage2"


def test_salesforce_product_manager_kept_and_reaches_stage1(rules):
    r = rules.evaluate(job("Product Manager, Salesforce Integrations"))
    assert r.status == "stage1" and r.tier == "untiered"


def test_plural_titles_match(rules):
    assert rules.evaluate(job("Products Manager")).status == "stage1"
    assert rules.title_tier("Senior Products Managers") == "A"
    assert phrase_re("Product").search("Billing Products") and not phrase_re("Product").search("Production")


@pytest.mark.parametrize("title", [
    "Product Manager", "Product Manager II", "Product Lead, Growth", "Lead Product, Payments",
    "Head of Products", "Director - Product", "VP of Product", "Vice President, Product",
    "Product Owner", "TPM", "Program Manager, Ops", "Project Manager",
])
def test_pm_role_pattern_accepts(title):
    from screener.rules import pm_role_kind
    assert pm_role_kind(title) is not None


@pytest.mark.parametrize("title", [
    "Product Designer", "Product Analytics Lead", "Product Support Specialist",
    "Engineering Manager, Billing Products", "Account Executive Product - Tax",
    "Product Operations Specialist", "Production Manager",
])
def test_non_pm_titles_never_reach_stage1(rules, title):
    assert rules.evaluate(job(title)).status == "drop"


def test_blocklist_catches_weak_pattern_titles(rules):
    assert rules.evaluate(job("Lead Product Designer")).drop_reason == "title_blocklist"
    assert rules.evaluate(job("Lead Product Accountant")).drop_reason == "title_blocklist"
    # strong PM form is exempt from the blocklist
    assert rules.evaluate(job("Product Manager, Customer Support Platform")).status == "stage1"


@pytest.mark.parametrize("loc", ["N/A", "n/a", "", "   ", "TBD"])
def test_unknown_location_is_never_ineligible(rules, loc):
    assert rules.location_class(job(location=loc)) == "ambiguous"
    assert rules.evaluate(job("Senior Product Manager", location=loc)).status == "stage1"


def test_stage1_decision_unsure_counts_as_keep():
    from screener.models import Stage1Decision
    assert Stage1Decision(decision="unsure").keep is True
    assert Stage1Decision(decision="keep").keep is True
    assert Stage1Decision(decision="drop").keep is False
    assert Stage1Decision(decision="drop").model_dump()["keep"] is False
    with pytest.raises(ValueError):
        Stage1Decision.model_validate({"decision": "maybe"})


def test_stage1_prompt_rules():
    text = stage1.SYSTEM
    assert "NEVER drop because of years" in text
    assert "NEVER infer anything from where the company is headquartered" in text
    assert "unknown, missing" in text and "keep" in text
    assert "unsure" in text


def test_stage2_prompt_missing_years_not_a_red_flag_for_tiered_titles():
    system = stage2.build_context(PROFILE).system
    assert "is NOT a red flag" in system and "tier" in system


def test_cache_key_changes_with_prompt(monkeypatch, env):
    j = job(jid="z")
    k1 = run_screener.key1(j)
    monkeypatch.setattr(stage1, "PROMPT_HASH", "different-prompt")
    assert run_screener.key1(j) != k1
    ctx = stage2.build_context(PROFILE)
    k2 = run_screener.key2(j, ctx)
    changed = json.loads(json.dumps(PROFILE))
    changed["domains_ranked"] = ["Something else"]
    assert run_screener.key2(j, stage2.build_context(changed)) != k2


def test_prompt_change_causes_cache_miss_and_new_call(env, monkeypatch):
    go(env, FakeClient([GOOD_S2, '{"decision": "keep", "reason": "ok"}', GOOD_S2]))
    quiet = FakeClient([])
    go(env, quiet)
    assert quiet.calls == []                                   # same prompts: all cached
    monkeypatch.setattr(stage1, "PROMPT_HASH", "edited-stage1-prompt")
    client = FakeClient(['{"decision": "keep", "reason": "ok"}'])
    go(env, client)
    assert len(client.calls) == 1                              # only stage 1 re-asked
    assert client.calls[0]["model"].startswith("claude-haiku")


def test_stage1_unsure_flows_to_stage2(env):
    client = FakeClient([GOOD_S2, '{"decision": "unsure", "reason": "unclear"}', GOOD_S2])
    go(env, client)
    rows = {r["id"]: r for r in json.loads(env.out.read_text())}
    assert rows["b"]["screen"]["status"] == "scored"


# ---------- round 3: role_family, location_status, non-India drop, pricing ----------

def test_prices_match_official_page_and_cite_source():
    import config
    assert config.PRICES[config.HAIKU] == (1.00, 5.00)
    assert config.PRICES[config.SONNET] == (2.00, 10.00)
    assert config.PRICES[config.OPUS] == (4.00, 20.00)
    src = open(config.__file__).read()
    assert "platform.claude.com/docs/en/about-claude/pricing" in src and "2026-10-16" in src


def test_stage2_prompt_asks_for_role_family_and_no_title_credit_for_project_titles():
    system = stage2.build_context(PROFILE).system
    for fam in ("product", "tech_program", "software_project", "physical_ops", "other"):
        assert fam in system
    assert "role_family" in system
    assert "Project Manager and Program Manager titles, give NO seniority credit" in system


def test_role_family_validated():
    ctx = stage2.build_context(PROFILE)
    validate = stage2.make_validator(ctx)
    assert validate(json.loads(GOOD_S2)).role_family == "product"
    bad = json.loads(GOOD_S2)
    bad["role_family"] = "warehouse"
    with pytest.raises(ValueError):
        validate(bad)
    del bad["role_family"]
    with pytest.raises(ValueError):
        validate(bad)


def entry(verdict="apply", family="product"):
    return {"score": 80, "verdict": verdict, "role_family": family, "reasons": [], "red_flags": [],
            "subscores": {}}


def rr(status="confirmed"):
    from screener.models import RuleResult
    return RuleResult(status="stage2", tier="A", location_status=status)


@pytest.mark.parametrize("family", ["physical_ops", "other"])
def test_apply_capped_to_stretch_for_non_pm_families(family):
    out = stage2.apply_caps(entry("apply", family), rr("confirmed"))
    assert out["verdict"] == "stretch" and out["uncapped_verdict"] == "apply"
    assert any(family in n for n in out["notes"]) and "verify location" not in out["notes"]


@pytest.mark.parametrize("family", ["product", "tech_program", "software_project"])
def test_pm_families_with_confirmed_location_stay_apply(family):
    out = stage2.apply_caps(entry("apply", family), rr("confirmed"))
    assert out["verdict"] == "apply" and out["notes"] == []


def test_unknown_location_caps_at_stretch_with_verify_note():
    out = stage2.apply_caps(entry("apply"), rr("unknown"))
    assert out["verdict"] == "stretch" and out["notes"] == ["verify location"]


def test_caps_never_raise_a_verdict():
    assert stage2.apply_caps(entry("skip", "other"), rr("unknown"))["verdict"] == "skip"
    assert stage2.apply_caps(entry("stretch"), rr("unknown"))["verdict"] == "stretch"


@pytest.mark.parametrize("loc,desc,expected", [
    ("Bengaluru", "", "confirmed"), ("Bangalore, Karnataka", "", "confirmed"),
    ("Remote - India", "", "confirmed"), ("Remote", "Our team in India is growing", "confirmed"),
    ("N/A", "Own the roadmap", "unknown"), ("Remote", "Work from anywhere", "unknown"),
    ("", "", "unknown"),
])
def test_location_status(rules, loc, desc, expected):
    assert rules.location_status(job(location=loc, desc=desc)) == expected


@pytest.mark.parametrize("loc", [
    "US-Remote", "Remote-US", "US Remote", "Remote in the US", "SF, NYC, Remote",
    "San Francisco, Seattle, New York, US-Remote", "Seattle, Washington, United States",
    "Remote, EMEA", "US-SF-HQ, US-SEA, US-CHI, US-NYC", "New York City, Toronto, Chicago, or Remote", "Toronto, Canada", "London or Dublin Remote",
])
def test_remote_with_only_non_india_places_is_dropped(rules, loc):
    r = rules.evaluate(job("Senior Product Manager", location=loc, remote="remote"))
    assert r.status == "drop" and r.drop_reason == "location_non_india_only"


@pytest.mark.parametrize("loc", [
    "Remote", "Remote - Atlantis", "San Francisco; Bengaluru", "Remote, India",
    "Remote - Germany or India", "N/A", "", "Remote - APAC",
])
def test_unknown_or_india_signal_locations_survive(rules, loc):
    r = rules.evaluate(job("Senior Product Manager", location=loc, remote="remote"))
    assert r.status != "drop", loc


def test_india_signal_in_title_prevents_non_india_drop(rules):
    r = rules.evaluate(job("Senior Product Manager (Bengaluru)", location="New York, Remote"))
    assert r.status != "drop"


def test_bare_remote_stays_unknown_not_dropped(rules):
    assert rules.location_class(job(location="Remote", remote="remote")) == "ambiguous"


def test_record_carries_notes_and_capped_verdict(env):
    """Unknown-location job via stage 1 'unsure' -> scored, capped to stretch, verify note."""
    jobs = json.loads(env.jobs.read_text())
    jobs[1]["location"] = "N/A"
    jobs[1]["description"] = "Own roadmap too"
    env.jobs.write_text(json.dumps(jobs))
    high = json.loads(GOOD_S2)
    high["scores"] = {"role_and_title_fit": 25, "domain_fit": 20, "seniority_fit": 20,
                      "location_fit": 15, "company_and_growth": 10, "comp_signal": 10}
    client = FakeClient([GOOD_S2, '{"decision": "unsure", "reason": "unknown"}', json.dumps(high)])
    go(env, client)
    rows = {r["id"]: r for r in json.loads(env.out.read_text())}
    b = rows["b"]["screen"]
    assert b["location_status"] == "unknown" and "verify location" in b["notes"]
    assert b["stage2"]["score"] == 100 and b["stage2"]["verdict"] == "stretch"
    assert b["stage2"]["uncapped_verdict"] == "apply"
    assert rows["a"]["screen"]["notes"] == []                 # confirmed Bengaluru, product


def test_physical_ops_job_capped_end_to_end(env):
    ops = json.loads(GOOD_S2)
    ops["role_family"] = "physical_ops"
    ops["scores"] = {"role_and_title_fit": 25, "domain_fit": 20, "seniority_fit": 20,
                     "location_fit": 15, "company_and_growth": 10, "comp_signal": 10}
    go(env, FakeClient([json.dumps(ops)]), limit=1)
    row = json.loads(env.out.read_text())[0]["screen"]["stage2"]
    assert row["verdict"] == "stretch" and row["role_family"] == "physical_ops"
