"""Stage 2 tests: spend guard, dry run, aggregate-only stats, filtered-out list."""
import json
from types import SimpleNamespace

import pytest
import yaml

import run_pipeline
from scout.schema import Job
from screener import run_screener
from tests.test_screener import GOOD_S2, PROFILE, FakeClient, job


@pytest.fixture
def paths(tmp_path):
    jobs = [job("Senior Product Manager", "Bengaluru", "Own roadmap", jid="a").model_copy(
                update={"company": "SecretCo", "source": "greenhouse"}),
            job("Senior Product Manager", "Bengaluru", "Own other roadmap", jid="b").model_copy(
                update={"company": "OtherCo", "source": "jsearch", "jd_partial": True}),
            job("Software Engineer", "Bengaluru", "code", jid="c").model_copy(
                update={"company": "EngCo", "source": "lever"})]
    raw, clean = tmp_path / "raw", tmp_path / "clean"
    raw.mkdir()
    (raw / "scout_jobs.json").write_text(json.dumps([j.model_dump() for j in jobs]))
    profile = tmp_path / "profile.yaml"
    profile.write_text(yaml.safe_dump(PROFILE))
    return SimpleNamespace(
        tmp=tmp_path, clean=clean, profile=profile, jobs=raw / "scout_jobs.json",
        screened=clean / "screened_jobs.json", cache=clean / "screener_cache.json",
        usage=raw / "api_usage.json", companies=tmp_path / "c.yaml")


def go(p, client=None, **kw):
    return run_pipeline.run(companies=p.companies, profile=p.profile, jobs=p.jobs,
                            screened=p.screened, cache=p.cache, clean_dir=p.clean, usage=p.usage,
                            skip_scout=True, client=client, **kw)


def test_dry_run_prints_estimate_and_writes_nothing(paths, caplog):
    caplog.set_level("INFO", logger="pipeline")
    client = FakeClient([])
    assert go(paths, client, dry_run=True) == 0
    assert client.calls == [] and not paths.clean.exists()
    assert "estimated cost" in caplog.text and "FUNNEL BY SOURCE" in caplog.text


def test_spend_guard_aborts_before_any_call(paths):
    client = FakeClient([])
    assert go(paths, client, max_cost=0.0) == 2
    assert client.calls == []
    stats = json.loads((paths.clean / "run_stats.jsonl").read_text())
    assert stats["aborted"] is True and stats["cost_usd"] == 0


def test_spend_guard_in_screener_raises(paths):
    with pytest.raises(run_screener.SpendGuardError):
        run_screener.run(paths.jobs, paths.screened, paths.profile, paths.cache,
                         client=FakeClient([]), max_cost=0.0)


def test_max_cost_override_allows_run(paths):
    client = FakeClient([GOOD_S2, GOOD_S2])
    assert go(paths, client, max_cost=5.0) == 0
    assert len(client.calls) == 2


def test_stats_line_is_aggregate_only_and_counts_by_source(paths):
    assert go(paths, FakeClient([GOOD_S2, GOOD_S2])) == 0
    text = (paths.clean / "run_stats.jsonl").read_text()
    for secret in ("SecretCo", "OtherCo", "EngCo", "Senior Product Manager", "https://"):
        assert secret not in text
    stats = json.loads(text)
    assert stats["jobs_in"] == {"greenhouse": 1, "jsearch": 1, "lever": 1}
    assert stats["after_rules"] == {"greenhouse": 1, "jsearch": 1, "lever": 0}
    assert stats["scored"] == {"greenhouse": 1, "jsearch": 1, "lever": 0}
    assert stats["stage2_calls"] == 2 and stats["stage1_calls"] == 0 and stats["cost_usd"] > 0
    assert sum(stats["verdicts"].values()) == 2


def test_stats_appends_one_line_per_run(paths):
    go(paths, FakeClient([GOOD_S2, GOOD_S2]))
    go(paths, FakeClient([]))                       # all cached: no calls
    lines = (paths.clean / "run_stats.jsonl").read_text().splitlines()
    assert len(lines) == 2 and json.loads(lines[1])["cost_usd"] == 0


def test_filtered_out_lists_reasons_and_is_rewritten(paths):
    go(paths, FakeClient([GOOD_S2, GOOD_S2]))
    path = paths.clean / "filtered_out.json"
    rows = json.loads(path.read_text())
    assert [(r["company"], r["reason"]) for r in rows] == [("EngCo", "title_untiered")]
    assert set(rows[0]) == {"id", "company", "title", "location", "reason"}
    # rewritten from the full scout output: dropping the engineer job empties the list
    keep = [j for j in json.loads(paths.jobs.read_text()) if j["id"] != "c"]
    paths.jobs.write_text(json.dumps(keep))
    go(paths, FakeClient([]))
    assert json.loads(path.read_text()) == []


def test_stage1_drop_is_in_filtered_out(paths):
    jobs = json.loads(paths.jobs.read_text())
    jobs.append(job("Product Manager", "Remote", "maybe", jid="d").model_copy(
        update={"company": "AmbigCo", "source": "ashby"}).model_dump())
    paths.jobs.write_text(json.dumps(jobs))
    replies = [GOOD_S2, GOOD_S2, '{"decision": "drop", "reason": "US only in practice"}']
    go(paths, FakeClient(replies))
    rows = {r["company"]: r["reason"] for r in json.loads((paths.clean / "filtered_out.json").read_text())}
    assert rows["AmbigCo"] == "stage1: US only in practice"
