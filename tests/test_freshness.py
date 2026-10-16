"""Posting freshness caps and the stricter Ready to apply rule."""
import json
from datetime import date
from pathlib import Path

import pytest
import yaml

from config import CAPPED_ROLE_FAMILIES, READY_STRETCH_MIN_SCORE
from dashboard.data import build_state, is_ready
from dashboard.store import ApplicationsStore
from screener import run_screener, stage2
from screener.freshness import parse_posted_date, posting_age_days
from screener.models import RuleResult
from tests.test_screener import GOOD_S2, PROFILE, FakeClient, job

TODAY = date(2026, 10, 16)
RULE = RuleResult(status="stage2", tier="A", location_status="confirmed")


# ---------- age ----------

@pytest.mark.parametrize("posted,age", [
    ("2026-10-06T10:00:00+00:00", 10), ("2026-10-06", 10), ("2026-10-06T10:00:00Z", 10),
    ("2026-07-20T10:48:43-04:00", 88), ("2026-10-20", 0),            # future date counts as 0
    (None, None), ("", None), ("last week", None), ("2026-13-45", None), (12345, None),
])
def test_posting_age_days(posted, age):
    assert posting_age_days(posted, TODAY) == age


def test_parse_posted_date_date_prefix_fallback():
    assert parse_posted_date("2026-10-06 garbage") == date(2026, 10, 6)


# ---------- caps ----------

def capped(verdict="apply", age=None, **kw):
    return stage2.apply_caps({"verdict": verdict, "role_family": "product"}, RULE, age_days=age, **kw)


def test_fresh_and_unknown_age_are_not_penalised():
    for age in (None, 0, 60):                      # exactly 60 is not "older than 60"
        out = capped(age=age)
        assert out["verdict"] == "apply" and not any("stale" in n for n in out["notes"])


def test_older_than_60_days_gets_note_and_stretch_cap():
    out = capped("apply", 61)
    assert out["verdict"] == "stretch" and out["uncapped_verdict"] == "apply"
    assert any("possibly stale, check the listing is still open" in n for n in out["notes"])
    assert capped("stretch", 61)["verdict"] == "stretch"
    assert capped("skip", 61)["verdict"] == "skip"


def test_older_than_120_days_is_capped_at_skip_with_reason():
    out = capped("apply", 121)
    assert out["verdict"] == "skip"
    assert any(n.startswith("stale posting") for n in out["notes"])
    assert capped("apply", 120)["verdict"] == "stretch"       # 120 itself is "older than 60" only


def test_thresholds_come_from_profile_with_defaults():
    ctx = stage2.build_context({**PROFILE, "freshness": {"stale_note_after_days": 30, "stale_skip_after_days": 90}})
    assert (ctx.stale_note_days, ctx.stale_skip_days) == (30, 90)
    default = stage2.build_context(PROFILE)
    assert (default.stale_note_days, default.stale_skip_days) == (60, 120)
    shipped = yaml.safe_load((Path(__file__).parent.parent / "profile.example.yaml").read_text())
    assert shipped["freshness"] == {"stale_note_after_days": 60, "stale_skip_after_days": 120}


def test_screener_applies_age_and_rescoring_from_cache_makes_no_calls(tmp_path):
    old = job("Senior Product Manager", "Bengaluru", "Own roadmap", jid="old").model_copy(
        update={"posted_date": "2026-01-01T00:00:00+00:00"})
    fresh = job("Senior Product Manager", "Bengaluru", "Own other roadmap", jid="new").model_copy(
        update={"posted_date": None})
    jobs, out, prof, cache = (tmp_path / n for n in ("j.json", "o.json", "p.yaml", "c.json"))
    jobs.write_text(json.dumps([old.model_dump(), fresh.model_dump()]))
    prof.write_text(yaml.safe_dump(PROFILE))
    run_screener.run(jobs, out, prof, cache, client=FakeClient([GOOD_S2, GOOD_S2]))
    rows = {r["id"]: r for r in json.loads(out.read_text())}
    assert rows["old"]["screen"]["stage2"]["verdict"] == "skip"
    assert rows["old"]["screen"]["posting_age_days"] > 120
    assert rows["new"]["screen"]["stage2"]["verdict"] == "apply" and rows["new"]["screen"]["posting_age_days"] is None
    # change only the thresholds: the cache still hits, so no model call, and the caps change
    PROFILE2 = {**PROFILE, "freshness": {"stale_note_after_days": 1000, "stale_skip_after_days": 2000}}
    prof.write_text(yaml.safe_dump(PROFILE2))
    client = FakeClient([])
    run_screener.run(jobs, out, prof, cache, client=client)
    assert client.calls == []
    assert {r["id"]: r for r in json.loads(out.read_text())}["old"]["screen"]["stage2"]["verdict"] == "apply"


# ---------- Ready to apply ----------

def row(verdict, score, family="product", status="new", orphaned=False):
    return {"verdict": verdict, "score": score, "role_family": family, "status": status, "orphaned": orphaned}


def test_ready_rule():
    assert READY_STRETCH_MIN_SCORE == 65 and CAPPED_ROLE_FAMILIES == ("physical_ops", "other")
    assert is_ready(row("apply", 72))
    assert is_ready(row("stretch", 65)) and not is_ready(row("stretch", 64))
    assert not is_ready(row("skip", 90))
    assert is_ready(row("apply", 80, status="shortlisted"))
    assert not is_ready(row("apply", 80, status="applied"))
    assert not is_ready(row("apply", 80, orphaned=True))


@pytest.mark.parametrize("family", ["physical_ops", "other"])
def test_capped_role_families_never_ready_whatever_the_score(family):
    assert not is_ready(row("apply", 99, family=family))
    assert not is_ready(row("stretch", 99, family=family))


def test_state_carries_age_family_and_ready_count(tmp_path):
    clean = tmp_path / "clean"
    clean.mkdir()

    def scored(jid, verdict, score, family="product", posted=None):
        return {"id": jid, "source": "lever", "title": "T", "company": jid, "location": "L", "url": "https://x",
                "posted_date": posted, "also_listed_on": [],
                "screen": {"status": "scored", "tier": "A", "notes": [],
                           "stage2": {"score": score, "verdict": verdict, "role_family": family, "red_flags": []}}}
    rows = [scored("a", "apply", 72, posted="2026-10-06"), scored("s70", "stretch", 70), scored("s60", "stretch", 60),
            scored("cap", "stretch", 80, family="physical_ops"), scored("sk", "skip", 40)]
    (clean / "screened_jobs.json").write_text(json.dumps(rows))
    state = build_state(tmp_path, ApplicationsStore(clean / "applications.json"), today=TODAY)
    by_id = {j["id"]: j for j in state["jobs"]}
    assert state["counts"]["ready"] == 2                      # apply 72 and stretch 70 only
    assert by_id["a"]["age_days"] == 10 and by_id["s70"]["age_days"] is None
    assert by_id["cap"]["role_family"] == "physical_ops"
    assert state["counts"]["ready_stretch_min"] == 65 and "other" in state["counts"]["capped_families"]
    assert state["counts"]["total_scored"] == 5               # everything stays in All scored


def test_frontend_has_toggle_age_labels_and_unknown_age():
    js = (Path(__file__).parent.parent / "dashboard" / "static" / "app.js").read_text()
    assert "Show all stretch" in js and "days ago" in js and "unknown age" in js
    assert "capped_families" in js
