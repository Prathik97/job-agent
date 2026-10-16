"""Stage 3: prepare-application endpoints, the files route, resume_version, screening answers."""
import json
import logging
import os
import threading
from pathlib import Path

import pytest

from dashboard import serve
from dashboard.demo import make_demo_dir
from dashboard.service import DemoService, ServiceError
from dashboard.store import ApplicationsStore
from tailor.answers import QUESTIONS, load_answers, screening_answers_md
from tests.test_dashboard import call, good_headers, screened_row


class FakeService:
    def __init__(self, output_dir):
        self.output_dir, self.calls = output_dir, []

    def estimate(self, job_id, jd):
        self.calls.append(("estimate", job_id, jd))
        if job_id == "partial" and not jd:
            return {"requires_jd": True}
        return {"requires_jd": False, "model": "m", "est_cost": 0.05, "worst_cost": 0.1, "max_cost": 1.0}

    def run(self, job_id, jd):
        self.calls.append(("run", job_id, jd))
        if job_id == "partial" and not jd:
            raise ServiceError(400, "partial JD", needs_jd=True)
        if job_id == "bad":
            raise ServiceError(422, "failed the fact check", violations=["number '95%' is not in master"])
        folder = self.output_dir / f"Acme_{job_id}_20261016"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "resume.pdf").write_bytes(b"%PDF-1.4 fake")
        (folder / "cover_note.md").write_text("hello")
        return {"folder": folder.name, "files": ["cover_note.md", "resume.pdf"], "model": "m", "cost": 0.04}


@pytest.fixture
def env(tmp_path):
    clean, out = tmp_path / "clean", tmp_path / "output" / "resumes"
    clean.mkdir()
    out.mkdir(parents=True)
    (clean / "screened_jobs.json").write_text(json.dumps([screened_row("j1"), screened_row("partial")]))
    (tmp_path / "secret.txt").write_text("TOP SECRET")
    (tmp_path / "output" / "other.pdf").write_bytes(b"outside resumes")
    svc = FakeService(out)
    srv = serve.make_server(0, tmp_path, token="tok", output_dir=out, service=svc)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv, svc, tmp_path, out
    srv.shutdown()
    srv.server_close()


def gh(srv):
    return good_headers(srv, **{"X-Dashboard-Token": "tok"})


def get(srv, path, token="tok", host=None):
    h = {"X-Dashboard-Token": token} if token is not None else {}
    if host:
        h["Host"] = host
    return call(srv, "GET", path, headers=h)


# ---------- endpoints ----------

def test_tailor_endpoints_require_token_and_origin(env):
    srv, svc, _, _ = env
    for path in ("/api/tailor/estimate", "/api/tailor/run"):
        for h in ({k: v for k, v in gh(srv).items() if k != "X-Dashboard-Token"},
                  good_headers(srv, **{"X-Dashboard-Token": "nope"}),
                  good_headers(srv, Origin="http://evil.com")):
            assert call(srv, "POST", path, {"job_id": "j1"}, h)[0] == 403
    assert svc.calls == []


def test_estimate_run_and_prepared_state(env):
    srv, svc, _, out = env
    h = gh(srv)
    status, _, raw = call(srv, "POST", "/api/tailor/estimate", {"job_id": "j1"}, h)
    assert status == 200 and json.loads(raw)["est_cost"] == 0.05
    status, _, raw = call(srv, "POST", "/api/tailor/run", {"job_id": "j1"}, h)
    assert status == 200 and json.loads(raw)["files"] == ["cover_note.md", "resume.pdf"]
    state = json.loads(call(srv, "GET", "/api/state")[2])
    assert state["prepared"]["j1"] == {"folder": "Acme_j1_20261016", "files": ["cover_note.md", "resume.pdf"]}


def test_partial_jd_and_fact_check_errors_surface(env):
    srv, svc, _, out = env
    h = gh(srv)
    status, _, raw = call(srv, "POST", "/api/tailor/estimate", {"job_id": "partial"}, h)
    assert json.loads(raw) == {"requires_jd": True}
    status, _, raw = call(srv, "POST", "/api/tailor/run", {"job_id": "partial"}, h)
    assert status == 400 and json.loads(raw)["needs_jd"] is True
    status, _, raw = call(srv, "POST", "/api/tailor/run", {"job_id": "partial", "jd": "full text"}, h)
    assert status == 200
    status, _, raw = call(srv, "POST", "/api/tailor/run", {"job_id": "bad"}, h)
    assert status == 422 and "95%" in json.loads(raw)["violations"][0]


def test_only_one_tailor_run_at_a_time(env):
    srv, svc, _, _ = env
    assert srv.tailor_lock.acquire(blocking=False)
    try:
        assert call(srv, "POST", "/api/tailor/run", {"job_id": "j1"}, gh(srv))[0] == 409
    finally:
        srv.tailor_lock.release()


# ---------- files route ----------

def test_files_served_only_with_token_from_output_folder(env):
    srv, _, tmp, out = env
    call(srv, "POST", "/api/tailor/run", {"job_id": "j1"}, gh(srv))
    status, headers, raw = get(srv, "/files/Acme_j1_20261016/resume.pdf")
    assert status == 200 and raw == b"%PDF-1.4 fake" and headers["Content-Type"] == "application/pdf"
    assert get(srv, "/files/Acme_j1_20261016/resume.pdf", token=None)[0] == 403
    assert get(srv, "/files/Acme_j1_20261016/resume.pdf", token="wrong")[0] == 403
    assert get(srv, "/files/Acme_j1_20261016/resume.pdf", host="evil.com")[0] == 403


@pytest.mark.parametrize("path", [
    "/files/../secret.txt", "/files/..%2fsecret.txt", "/files/%2e%2e/%2e%2e/secret.txt",
    "/files/Acme_j1_20261016/../../other.pdf", "/files/Acme_j1_20261016/..%2f..%2fother.pdf",
    "/files/Acme_j1_20261016%2f..%2f..%2fother.pdf", "/files//etc/passwd", "/files/Acme_j1_20261016/",
    "/files/Acme_j1_20261016/.hidden.md", "/files/Acme_j1_20261016/resume.pdf%00.md",
    "/files/Acme_j1_20261016/a\\b.pdf", "/files/Acme_j1_20261016/x/y.pdf", "/files/",
])
def test_path_traversal_is_rejected(env, path):
    srv, _, _, out = env
    call(srv, "POST", "/api/tailor/run", {"job_id": "j1"}, gh(srv))
    status, _, raw = get(srv, path)
    assert status in (403, 404) and b"TOP SECRET" not in raw and b"outside resumes" not in raw


def test_symlink_escape_and_bad_extensions_are_rejected(env):
    srv, _, tmp, out = env
    call(srv, "POST", "/api/tailor/run", {"job_id": "j1"}, gh(srv))
    folder = out / "Acme_j1_20261016"
    os.symlink(tmp / "secret.txt", folder / "link.txt")
    os.symlink(tmp / "output", out / "linkdir")
    (folder / "run.py").write_text("print(1)")
    assert get(srv, "/files/Acme_j1_20261016/link.txt")[0] == 404
    assert get(srv, "/files/linkdir/other.pdf")[0] == 404
    assert get(srv, "/files/Acme_j1_20261016/run.py")[0] == 404


# ---------- resume_version ----------

def test_marking_applied_stores_the_prepared_resume_folder(env):
    srv, _, tmp, _ = env
    h = gh(srv)
    call(srv, "POST", "/api/tailor/run", {"job_id": "j1"}, h)
    status, _, raw = call(srv, "POST", "/api/applications/j1", {"mark_applied": True}, h)
    assert json.loads(raw)["resume_version"] == "Acme_j1_20261016"
    stored = json.loads((tmp / "clean" / "applications.json").read_text())
    assert stored["j1"]["resume_version"] == "Acme_j1_20261016"
    state = json.loads(call(srv, "GET", "/api/state")[2])
    assert next(j for j in state["jobs"] if j["id"] == "j1")["resume_version"] == "Acme_j1_20261016"


def test_no_resume_version_when_nothing_was_prepared(env):
    srv, _, _, _ = env
    _, _, raw = call(srv, "POST", "/api/applications/j1", {"mark_applied": True}, gh(srv))
    assert json.loads(raw)["resume_version"] is None


def test_resume_version_not_overwritten_by_later_prepare(tmp_path):
    store = ApplicationsStore(tmp_path / "a.json")
    store.update("j", status="applied", snapshot={}, resume_version="v1")
    assert store.update("j", status="interview", resume_version="v2")["resume_version"] == "v1"


# ---------- demo mode ----------

def test_demo_prepare_flow_writes_placeholders_only():
    root = make_demo_dir()
    out = root / "output" / "resumes"
    srv = serve.make_server(0, root, token="t", demo=True, output_dir=out, service=DemoService(root, out))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        rows = json.loads((root / "clean" / "screened_jobs.json").read_text())
        partial = next(r for r in rows if r["jd_partial"])
        full = next(r for r in rows if not r["jd_partial"])
        h = good_headers(srv, **{"X-Dashboard-Token": "t"})
        assert json.loads(call(srv, "POST", "/api/tailor/estimate", {"job_id": partial["id"]}, h)[2]) == {"requires_jd": True}
        assert call(srv, "POST", "/api/tailor/run", {"job_id": partial["id"]}, h)[0] == 400
        status, _, raw = call(srv, "POST", "/api/tailor/run", {"job_id": full["id"]}, h)
        assert status == 200 and "resume.pdf" in json.loads(raw)["files"]
        assert b"DEMO" in (out / json.loads(raw)["folder"] / "cover_note.md").read_bytes()
    finally:
        srv.shutdown()
        srv.server_close()
        import shutil
        shutil.rmtree(root, ignore_errors=True)


# ---------- screening answers ----------

SENTINELS = {"notice_period": "SENTINEL-NOTICE-61d", "expected_ctc": "SENTINEL-CTC-9999",
             "current_fixed_ctc": "SENTINEL-FIXED-1234"}


def test_answers_unanswered_default_and_never_guessed(tmp_path):
    path = tmp_path / "answers.yaml"
    path.write_text("notice_period: 30 days\nexpected_ctc:\n  status: unanswered\n")
    a = load_answers(path)
    assert a["notice_period"] == "30 days" and a["expected_ctc"] is None and a["how_heard"] == "job board"
    md = screening_answers_md(a)
    assert "Notice period: 30 days" in md and "Expected CTC: unanswered" in md
    assert "Current fixed CTC: unanswered" in md and "How did you hear about this role: job board" in md
    assert len(QUESTIONS) == 10
    assert load_answers(tmp_path / "missing.yaml")["notice_period"] is None


def test_answers_module_has_no_logging_or_printing():
    src = (Path(__file__).parent.parent / "tailor" / "answers.py").read_text()
    for banned in ("logging", "logger", "print(", "log."):
        assert banned not in src


from tests.test_tailor import MASTER, FakeClient, good_plan, needs_font  # noqa: E402


@needs_font
def test_answers_never_appear_in_logs_stats_or_other_files(tmp_path, caplog, capsys):
    import yaml
    from datetime import date
    from tailor import tailor as t
    clean = tmp_path / "data" / "clean"
    clean.mkdir(parents=True)
    row = {"id": "j", "source": "greenhouse", "title": "Senior Product Manager", "company": "Acme", "location": "Bangalore",
           "url": "https://x", "description": "Own the roadmap.", "jd_partial": False, "also_listed_on": [],
           "screen": {"status": "scored", "stage2": {"score": 70, "verdict": "apply"}}}
    (clean / "screened_jobs.json").write_text(json.dumps([row]))
    (clean / "application_answers.yaml").write_text(yaml.safe_dump(SENTINELS))
    (tmp_path / "m.json").write_text(json.dumps(MASTER))
    (tmp_path / "p.yaml").write_text("scoring: {thresholds: {top_pick_min_score: 85}}\n")
    with caplog.at_level(logging.DEBUG):
        res = t.tailor_job("j", data_dir=tmp_path / "data", master_path=tmp_path / "m.json", profile_path=tmp_path / "p.yaml",
                           out_root=tmp_path / "out", client=FakeClient([good_plan()]), today=date(2026, 10, 16))
    seen = capsys.readouterr()
    for value in SENTINELS.values():
        assert value not in caplog.text and value not in seen.out and value not in seen.err
        assert value in (res.folder / "screening_answers.md").read_text()
        for f in res.folder.iterdir():
            if f.suffix in (".md", ".txt") and f.name != "screening_answers.md":
                assert value not in f.read_text()
    # aggregate files written by the evaluate command never carry them either
    from evaluate import __main__ as ev
    report = ev.run(tmp_path / "data")
    assert not any(v in json.dumps(report) for v in SENTINELS.values())


def test_state_exposes_jd_partial_so_the_ui_asks_for_the_full_jd(env):
    srv, _, tmp, _ = env
    rows = json.loads((tmp / "clean" / "screened_jobs.json").read_text())
    rows[1]["jd_partial"] = True
    (tmp / "clean" / "screened_jobs.json").write_text(json.dumps(rows))
    jobs = {j["id"]: j for j in json.loads(call(srv, "GET", "/api/state")[2])["jobs"]}
    assert jobs["partial"]["jd_partial"] is True and jobs["j1"]["jd_partial"] is False
