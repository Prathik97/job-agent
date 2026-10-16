"""Score a JD tab: same path as the pipeline, rule warnings, extraction, add-to-pipeline, privacy.
The model is faked; nothing here calls a paid API."""
import json
import logging
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from config import HAIKU, SONNET
from dashboard import serve
from dashboard.jd import DemoJDScorer, JDScorer, jd_match
from dashboard.service import ServiceError
from scout.schema import Job
from screener import run_screener, stage2
from screener.cache import Cache
from screener.llm import Usage
from screener.rules import Rules
from tailor import tailor as t
from tests.test_dashboard import call, good_headers, screened_row
from tests.test_screener import PROFILE
from tests.test_tailor import MASTER, FakeClient as TailorFakeClient, good_plan, needs_font

MARKER = "ZQX-UNIQUE-PASTED-MARKER-7731"
JD = f"""Senior Product Manager, Payments
About Acme Cloud. We are hiring in Bengaluru (hybrid).
{MARKER}
You will own the roadmap and work with engineering. 6+ years of experience. Jira and Salesforce a plus."""
SCORES = {"role_and_title_fit": 20, "domain_fit": 14, "seniority_fit": 16, "location_fit": 12,
          "company_and_growth": 6, "comp_signal": 5}
EXTRACTED = {"title": "Senior Product Manager", "company": "Acme Cloud", "location": "Bengaluru",
             "keywords": ["roadmap", "Salesforce", "Kubernetes", "Jira"]}


def s2_reply(**over):
    return {"role_family": "product", "scores": SCORES, "reasons": ["r1", "r2", "r3", "r4"],
            "red_flags": ["no payments depth"], **over}


class Fake:
    """Answers by model: Haiku gets the extraction JSON, Sonnet the Stage 2 JSON."""

    def __init__(self, extract=None, stage2_replies=None):
        self.extract = EXTRACTED if extract is None else extract
        self.s2 = list(stage2_replies or [])
        self.calls = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.calls.append(kw["model"])
        if kw["model"] == HAIKU:
            text = json.dumps(self.extract)
        else:
            text = self.s2.pop(0) if self.s2 else json.dumps(s2_reply())
            text = text if isinstance(text, str) else json.dumps(text)
        return SimpleNamespace(content=[SimpleNamespace(text=text)],
                               usage=SimpleNamespace(input_tokens=2000, output_tokens=300))


@pytest.fixture
def env(tmp_path):
    (tmp_path / "data" / "clean").mkdir(parents=True)
    (tmp_path / "profile.yaml").write_text(yaml.safe_dump(PROFILE, sort_keys=False))
    (tmp_path / "master.json").write_text(json.dumps(MASTER))
    return SimpleNamespace(root=tmp_path, data=tmp_path / "data", out=tmp_path / "output" / "resumes")


def scorer(env, client=None, **kw):
    return JDScorer(env.data, env.out, env.root / "master.json", env.root / "profile.yaml",
                    client=client or Fake(), **kw)


def body(**over):
    return {"jd": JD, "company": "Acme Cloud", "title": "Senior Product Manager", "location": "Bengaluru", **over}


# ---------- same path as the pipeline ----------

def test_pasted_jd_scores_like_a_pipeline_job_and_repeats_exactly(env):
    svc, fake = scorer(env), None
    first = svc.score(body())
    calls_after_first = len(svc._client.calls)
    # The identical job through the pipeline's own screen() gets the same capped result.
    rules, ctx = Rules.from_profile(PROFILE), stage2.build_context(PROFILE)
    job = Job.build(source="greenhouse", title="Senior Product Manager", company="Acme Cloud",
                    location="Bengaluru", url="", description=JD.strip())
    out = run_screener.screen([(job, rules.evaluate(job))], Cache(env.root / "pipe_cache.json"), ctx,
                             Fake(), False, Usage())[job.id]["screen"]["stage2"]
    assert first["id"] == job.id
    assert (first["score"], first["verdict"], first["caps"]) == (out["score"], out["verdict"], out["notes"])
    assert {c["key"]: c["points"] for c in first["subscores"]} == out["subscores"]
    assert first["score"] == sum(SCORES.values()) and first["verdict"] == "apply"
    # Same text again: same score, served from the shared screener cache, no new model calls.
    second = svc.score(body())
    assert second["score"] == first["score"] and len(svc._client.calls) == calls_after_first
    # ... and the pipeline cache key for this job is the one that was filled.
    assert Cache(env.data / "clean" / "screener_cache.json").get(run_screener.key2(job, ctx)) is not None


def test_weights_thresholds_and_caps_come_from_the_profile(env):
    out = scorer(env, Fake(stage2_replies=[s2_reply(role_family="physical_ops")])).score(body())
    assert out["thresholds"] == {"apply": 70, "stretch": 55}
    assert [c["max"] for c in out["subscores"]] == [25, 20, 20, 15, 10, 10]
    assert out["uncapped_verdict"] == "apply" and out["verdict"] == "stretch"
    assert any("physical_ops" in c for c in out["caps"])


def test_unknown_location_cap_shows_its_reason(env):
    out = scorer(env).score(body(location="Remote", jd="Own the roadmap. Fully remote."))
    assert out["verdict"] == "stretch" and "verify location" in out["caps"]


@pytest.mark.parametrize("fields,jd,code", [
    ({"title": "Sales Manager"}, JD, "title_reject"),
    ({}, JD + " Requires 18 years of experience.", "years_over_hard_limit"),
    ({"location": "Remote - US"}, "Own the roadmap. US only.", "location_reject"),
    ({"location": "London"}, "Own the roadmap in our office.", "location_not_eligible"),
])
def test_rule_drops_become_warnings_and_the_model_still_scores(env, fields, jd, code):
    fake = Fake()
    out = scorer(env, fake).score(body(jd=jd, **fields))
    assert [w["code"] for w in out["warnings"]] == [code] and out["warnings"][0]["text"]
    assert out["score"] == sum(SCORES.values()) and SONNET in fake.calls


# ---------- missing title or company ----------

def test_blank_title_company_location_are_extracted(env):
    fake = Fake()
    out = scorer(env, fake).score({"jd": JD})
    assert (out["title"], out["company"], out["location"]) == ("Senior Product Manager", "Acme Cloud", "Bengaluru")
    assert out["extracted"] == {"title": True, "company": True, "location": True}
    assert fake.calls.count(HAIKU) == 1


def test_typed_fields_win_over_extraction(env):
    out = scorer(env).score(body(title="Lead Product Manager"))
    assert out["title"] == "Lead Product Manager" and out["extracted"]["title"] is False


def test_extraction_that_finds_nothing_asks_for_the_fields(env):
    svc = scorer(env, Fake(extract={"title": "", "company": "", "location": "", "keywords": []}))
    with pytest.raises(ServiceError) as exc:
        svc.score({"jd": JD})
    assert exc.value.status == 422 and "title and company" in exc.value.message


# ---------- cost shown first, spend guard ----------

def test_estimate_then_cache_makes_the_next_one_free(env):
    svc = scorer(env)
    first = svc.estimate(body())
    assert first["est_cost"] > 0 and not first["cached"] and not first["over_limit"]
    svc.score(body())
    again = svc.estimate(body())
    assert again["cached"] and again["est_cost"] == 0.0


def test_spend_guard_blocks_before_any_model_call(env):
    fake = Fake()
    svc = scorer(env, fake, max_cost=0.0001)
    assert svc.estimate(body())["over_limit"]
    with pytest.raises(ServiceError) as exc:
        svc.score(body())
    assert exc.value.status == 400 and "limit" in exc.value.message and fake.calls == []


@pytest.mark.parametrize("bad", [{"jd": ""}, {"jd": "   "}, {"jd": 5}, {}, body(title=5), body(url="javascript:alert(1)"),
                                 body(company="x" * 300)])
def test_bad_input_is_rejected(env, bad):
    with pytest.raises(ServiceError) as exc:
        scorer(env).estimate(bad)
    assert exc.value.status == 400


# ---------- JD match ----------

def test_keyword_match_uses_the_same_logic_as_jd_keywords_md(env):
    plan = t.TailorPlan.model_validate(good_plan(jd_keywords=["roadmap", "Salesforce", "Kubernetes", "A/B testing"]))
    md = t.jd_keywords_md(plan, MASTER)
    items = jd_match(plan.jd_keywords, MASTER)
    for item in items:
        line = next(l for l in md.splitlines() if l.startswith(f"- {item['keyword']}:"))
        assert (": backed (" in line) is item["backed"]
    out = scorer(env).score(body())
    by_kw = {i["keyword"]: i for i in out["jd_match"]["items"]}
    assert by_kw["roadmap"]["backed"] and by_kw["Salesforce"]["backed"] and not by_kw["Kubernetes"]["backed"]
    assert out["jd_match"]["backed"] == 2 and len(out["reasons"]) == 3


def test_gaps_fall_back_to_weak_categories(env):
    out = scorer(env, Fake(stage2_replies=[s2_reply(red_flags=[], scores={**SCORES, "domain_fit": 4})])).score(body())
    assert out["gaps"] and out["gaps"][0].startswith("Weak domain fit: 4 of 20")


# ---------- server: token checks, add to pipeline, tailor ----------

@pytest.fixture
def srv(env):
    class FakeTailor:
        def __init__(self):
            self.calls = []

        def estimate(self, job_id, jd, job_record=None):
            self.calls.append(("estimate", job_id, jd, job_record))
            return {"requires_jd": False, "model": "m", "est_cost": 0.05, "worst_cost": 0.1, "max_cost": 1.0}

        def run(self, job_id, jd, job_record=None):
            self.calls.append(("run", job_id, jd, job_record))
            folder = env.out / "Acme-Cloud_Senior-Product-Manager_20261016"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "resume.pdf").write_bytes(b"%PDF-1.4 fake")
            return {"folder": folder.name, "files": ["resume.pdf"], "model": "m", "cost": 0.04}

    tailor = FakeTailor()
    svc = scorer(env, tailor_service=tailor)
    server = serve.make_server(0, env.data, token="tok", output_dir=env.out, jd_service=svc)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield SimpleNamespace(server=server, tailor=tailor, env=env)
    server.shutdown()
    server.server_close()


def post(srv, path, payload, **hdr):
    return call(srv.server, "POST", path, payload, good_headers(srv.server, **{"X-Dashboard-Token": "tok", **hdr}))


def jd_json(status_raw):
    status, _, raw = status_raw
    return status, json.loads(raw)


ROUTES = ["/api/jd/estimate", "/api/jd/score", "/api/jd/add", "/api/jd/tailor/estimate", "/api/jd/tailor/run"]


@pytest.mark.parametrize("path", ROUTES)
def test_jd_routes_need_token_origin_and_localhost_host(srv, path):
    s = srv.server
    assert call(s, "POST", path, body(), {"Origin": f"http://127.0.0.1:{s.port}"})[0] == 403          # no token
    assert call(s, "POST", path, body(), good_headers(s, **{"X-Dashboard-Token": "nope"}))[0] == 403
    assert call(s, "POST", path, body(), {"X-Dashboard-Token": "tok"})[0] == 403                       # no origin
    assert call(s, "POST", path, body(), good_headers(s, **{"X-Dashboard-Token": "tok", "Origin": "http://evil.test"}))[0] == 403
    assert call(s, "POST", path, body(), good_headers(s, **{"X-Dashboard-Token": "tok", "Host": "evil.test"}))[0] == 403
    assert call(s, "GET", path)[0] in (404, 405, 501)


def test_manual_job_appears_in_all_scored_and_can_be_marked_applied(srv):
    status, scored = jd_json(post(srv, "/api/jd/score", body()))
    assert status == 200 and scored["score"] == sum(SCORES.values())
    jid = scored["id"]
    state = json.loads(call(srv.server, "GET", "/api/state")[2])
    assert all(r["id"] != jid for r in state["jobs"])                       # scoring alone adds nothing
    status, added = jd_json(post(srv, "/api/jd/add", {"job_id": jid}))
    assert status == 200 and added == {"added": True, "already_in_pipeline": False, "source": "manual"}
    state = json.loads(call(srv.server, "GET", "/api/state")[2])
    row = next(r for r in state["jobs"] if r["id"] == jid)
    assert row["source"] == "manual" and row["score"] == scored["score"] and row["status"] == "new"
    assert state["counts"]["total_scored"] == 1
    status, applied = jd_json(post(srv, f"/api/applications/{jid}", {"mark_applied": True}))
    assert status == 200 and applied["status"] == "applied" and applied["applied_at"]
    assert json.loads(call(srv.server, "GET", "/api/state")[2])["counts"]["applied"] == 1


def test_add_is_idempotent_and_never_replaces_a_pipeline_row(srv):
    jid = jd_json(post(srv, "/api/jd/score", body()))[1]["id"]
    post(srv, "/api/jd/add", {"job_id": jid})
    status, again = jd_json(post(srv, "/api/jd/add", {"job_id": jid}))
    assert again["added"] is False and again["already_in_pipeline"] is True
    rows = json.loads((srv.env.data / "clean" / "screened_jobs.json").read_text())
    assert len(rows) == 1 and rows[0]["source"] == "manual"
    # a scored pipeline row with the same id is left alone
    row = dict(rows[0], source="greenhouse")
    (srv.env.data / "clean" / "screened_jobs.json").write_text(json.dumps([row]))
    assert jd_json(post(srv, "/api/jd/add", {"job_id": jid}))[1]["source"] == "greenhouse"
    assert json.loads((srv.env.data / "clean" / "screened_jobs.json").read_text())[0]["source"] == "greenhouse"


def test_add_never_overwrites_a_corrupt_file_and_needs_a_scored_draft(srv):
    jid = jd_json(post(srv, "/api/jd/score", body()))[1]["id"]
    path = srv.env.data / "clean" / "screened_jobs.json"
    path.write_text("{not json")
    assert post(srv, "/api/jd/add", {"job_id": jid})[0] == 500 and path.read_text() == "{not json"
    assert post(srv, "/api/jd/add", {"job_id": "never-scored"})[0] == 404
    assert post(srv, "/api/jd/tailor/run", {"job_id": "never-scored"})[0] == 404


def test_tailor_runs_on_the_pasted_text_and_links_the_files(srv):
    jid = jd_json(post(srv, "/api/jd/score", body()))[1]["id"]
    status, est = jd_json(post(srv, "/api/jd/tailor/estimate", {"job_id": jid}))
    assert status == 200 and est["est_cost"] == 0.05
    status, out = jd_json(post(srv, "/api/jd/tailor/run", {"job_id": jid}))
    assert status == 200 and out["files"] == ["resume.pdf"]
    kind, job_id, jd, record = srv.tailor.calls[-1]
    assert kind == "run" and job_id == jid and MARKER in jd
    assert record["screen"]["stage2"]["score"] == sum(SCORES.values())
    assert record["jd_partial"] is False and record["source"] == "manual"          # partial-JD rule never applies
    state = json.loads(call(srv.server, "GET", "/api/state")[2])
    assert state["prepared"][jid]["folder"] == out["folder"]
    status, _, raw = call(srv.server, "GET", f"/files/{out['folder']}/resume.pdf", None, {"X-Dashboard-Token": "tok"})
    assert status == 200 and raw.startswith(b"%PDF")


@needs_font
def test_real_tailor_accepts_a_job_record_and_stores_the_pasted_jd_in_output(env):
    """tailor_job with a record that is not in screened_jobs.json: same fact check, same folder format."""
    record = {"id": "m1", "source": "manual", "title": "Senior Product Manager", "company": "Acme Corp",
              "location": "Bangalore, Karnataka", "url": "", "description": "Own the roadmap. Salesforce a plus.",
              "jd_partial": False, "also_listed_on": [], "screen": {"status": "scored", "stage2": {"score": 72, "verdict": "apply"}}}
    (env.root / "master.json").write_text(json.dumps(MASTER))
    kw = dict(data_dir=env.data, master_path=env.root / "master.json", profile_path=env.root / "profile.yaml")
    with pytest.raises(t.TailorError):
        t.prepare("m1", **kw)                                                      # unknown without the record
    prep = t.prepare("m1", job_record=record, jd_override=record["description"], **kw)
    assert prep.job["company"] == "Acme Corp" and prep.jd_pasted and prep.india
    res = t.tailor_job("m1", out_root=env.out, job_record=record, jd_override=record["description"],
                       client=TailorFakeClient([good_plan()]), **kw)
    assert sorted(p.name for p in res.folder.iterdir()) == sorted(
        ["resume.docx", "resume.pdf", "cover_note.md", "outreach.md", "changes.md", "jd_keywords.md",
         "screening_answers.md", "jd_pasted.txt"])


# ---------- privacy: the pasted text lives only in data/ and output/ ----------

def test_pasted_text_never_appears_in_logs_stats_or_the_eval_report(srv, caplog, capsys):
    env = srv.env
    (env.data / "clean" / "run_stats.jsonl").write_text(json.dumps({"timestamp": "2026-10-16T00:00:00+00:00", "cost_usd": 0}) + "\n")
    # first Stage 2 reply is schema-invalid and quotes the marker, to exercise the retry-warning path
    bad = json.dumps({"role_family": MARKER, "scores": {"x": 1}, "reasons": [MARKER]})
    srv.server.jd_service._client = Fake(stage2_replies=[bad])
    with caplog.at_level(logging.DEBUG):
        status, scored = jd_json(post(srv, "/api/jd/score", body()))
        jid = scored["id"]
        post(srv, "/api/jd/add", {"job_id": jid})
        post(srv, "/api/jd/tailor/estimate", {"job_id": jid})
        post(srv, "/api/jd/tailor/run", {"job_id": jid})
        post(srv, "/api/jd/score", {"jd": ""})                                    # an error path too
        state = call(srv.server, "GET", "/api/state")[2].decode()
        ev = call(srv.server, "GET", "/api/eval")[2].decode()
    assert status == 200 and any("invalid JSON" in r.getMessage() for r in caplog.records)   # the retry did happen
    assert MARKER not in caplog.text
    captured = capsys.readouterr()
    assert MARKER not in captured.out and MARKER not in captured.err
    assert MARKER not in state and MARKER not in ev                               # state rows carry no description
    assert MARKER not in (env.data / "clean" / "run_stats.jsonl").read_text()
    assert MARKER not in json.dumps(scored)                                       # the response never echoes the text
    where = {p.name for p in env.root.rglob("*") if p.is_file() and MARKER in p.read_text(errors="ignore")}
    assert where <= {"jd_drafts.json", "screened_jobs.json"}                       # data/clean only
    assert all("data" in str(p) or "output" in str(p) for p in env.root.rglob("*")
               if p.is_file() and MARKER in p.read_text(errors="ignore"))


def test_the_scorer_module_has_no_prints_and_logs_no_text():
    src = Path("dashboard/jd.py").read_text()
    assert "print(" not in src
    logged = [ln for ln in src.splitlines() if "log." in ln]
    assert len(logged) == 1 and "description" not in logged[0] and "inp.jd" not in logged[0]


# ---------- demo mode ----------

def test_demo_scorer_runs_end_to_end_without_a_model(tmp_path):
    from dashboard.demo import make_demo_dir
    root = make_demo_dir()
    out = root / "output" / "resumes"
    svc = DemoJDScorer(root, out, Path(__file__).resolve().parent.parent)
    first = svc.score({"jd": JD})
    assert first["demo"] is True and first["cost"] == 0 and first["jd_match"]["available"]
    assert svc.score({"jd": JD})["score"] == first["score"]                     # deterministic
    assert svc.estimate({"jd": JD})["est_cost"] == 0.0
    assert svc.tailor_run({"job_id": first["id"]})["model"] == "demo"
    assert svc.add({"job_id": first["id"]})["added"] is True
