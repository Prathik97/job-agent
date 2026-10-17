"""Dashboard tests: applications store, server security, HTML escaping. Local sockets only."""
import http.client
import json
import re
import threading
from datetime import date
from pathlib import Path

import pytest

from dashboard import data as dash_data
from dashboard import serve
from dashboard.demo import make_demo_dir
from dashboard.store import ApplicationsStore, StoreError, atomic_write

SNAP = {"company": "Acme", "title": "Senior PM", "url": "https://acme.test/1", "score": 80, "verdict": "apply"}


def screened_row(jid="j1", **kw):
    return {"id": jid, "source": "greenhouse", "title": "Senior PM", "company": "Acme",
            "location": "Bengaluru", "url": "https://acme.test/1", "posted_date": "2026-10-01",
            "also_listed_on": [], "screen": {"status": "scored", "tier": "A", "notes": ["verify location"],
            "stage2": {"score": 80, "verdict": "apply", "red_flags": []}}, **kw}


@pytest.fixture
def store(tmp_path):
    return ApplicationsStore(tmp_path / "clean" / "applications.json")


# ---------- applications store ----------

def test_status_history_and_snapshot_taken_on_first_leave_new(store):
    e = store.update("j1", status="shortlisted", snapshot=SNAP, now="t1")
    assert e["snapshot"] == SNAP and e["history"] == [{"at": "t1", "from": "new", "to": "shortlisted"}]
    e = store.update("j1", status="applied", snapshot={**SNAP, "score": 1}, now="t2", today=date(2026, 10, 16))
    assert e["snapshot"]["score"] == 80                      # snapshot not replaced later
    assert e["applied_at"] == "2026-10-16" and len(e["history"]) == 2
    assert store.load()["j1"]["status"] == "applied"


def test_mark_applied_stamps_today_once(store):
    store.update("j1", mark_applied=True, snapshot=SNAP, today=date(2026, 10, 1))
    e = store.update("j1", status="interview", today=date(2026, 10, 9))
    assert e["applied_at"] == "2026-10-01"
    e = store.update("j1", mark_applied=True, today=date(2026, 10, 20))
    assert e["applied_at"] == "2026-10-01"                   # not re-stamped


def test_notes_only_change_keeps_status_new_and_no_snapshot(store):
    e = store.update("j1", notes="call recruiter")
    assert e["status"] == "new" and e["snapshot"] is None and e["history"] == []


def test_no_change_writes_nothing(store):
    store.update("j1")
    assert not store.path.exists()


def test_invalid_input_rejected(store):
    for bad in ({"status": "hired"}, {"notes": "x" * 6000}, {"notes": 5}):
        with pytest.raises(StoreError):
            store.update("j1", **bad)
    with pytest.raises(StoreError):
        store.update("", status="applied")


def test_atomic_write_keeps_old_file_on_failure(tmp_path, monkeypatch):
    path = tmp_path / "a.json"
    atomic_write(path, {"ok": 1})
    class Boom:  # not JSON serializable -> fails mid-write
        pass
    with pytest.raises(TypeError):
        atomic_write(path, {"bad": Boom()})
    assert json.loads(path.read_text()) == {"ok": 1}
    assert list(tmp_path.glob("*.tmp")) == []


def test_corrupt_store_is_not_overwritten(store):
    store.path.parent.mkdir(parents=True)
    store.path.write_text("{not json")
    with pytest.raises(StoreError):
        store.update("j1", status="applied", snapshot=SNAP)
    assert store.path.read_text() == "{not json"


def test_concurrent_updates_all_land(store):
    threads = [threading.Thread(target=store.update, args=(f"j{i}",), kwargs={"status": "skipped", "snapshot": SNAP})
               for i in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(store.load()) == 20


def test_rerun_does_not_clobber_statuses_and_orphans_are_listed(tmp_path):
    clean = tmp_path / "clean"
    clean.mkdir()
    store = ApplicationsStore(clean / "applications.json")
    (clean / "screened_jobs.json").write_text(json.dumps([screened_row("j1"), screened_row("j2")]))
    store.update("j1", status="applied", snapshot=SNAP, today=date(2026, 10, 1))
    store.update("j2", status="applied", snapshot={**SNAP, "company": "Gone Co"})
    before = (clean / "applications.json").read_text()
    # a pipeline re-run rewrites screened_jobs.json; j1 changes score, j2 disappears from the feeds
    (clean / "screened_jobs.json").write_text(json.dumps([screened_row("j1"), screened_row("j3")]))
    assert (clean / "applications.json").read_text() == before
    state = dash_data.build_state(tmp_path, store, today=date(2026, 10, 16))
    by_id = {r["id"]: r for r in state["jobs"]}
    assert by_id["j1"]["status"] == "applied" and by_id["j1"]["follow_up"] is True   # 15 days
    assert by_id["j2"]["orphaned"] and by_id["j2"]["company"] == "Gone Co"
    assert by_id["j3"]["status"] == "new"
    assert state["counts"]["applied"] == 2 and state["counts"]["ready"] == 1
    assert state["counts"]["total_scored"] == 2                                       # orphan not counted


def test_ready_excludes_skip_and_non_new(tmp_path):
    clean = tmp_path / "clean"
    clean.mkdir()
    skip = screened_row("s")
    skip["screen"]["stage2"]["verdict"] = "skip"
    (clean / "screened_jobs.json").write_text(json.dumps([screened_row("a"), skip, screened_row("b")]))
    store = ApplicationsStore(clean / "applications.json")
    store.update("b", status="skipped", snapshot=SNAP)
    assert dash_data.build_state(tmp_path, store)["counts"]["ready"] == 1


# ---------- server ----------

@pytest.fixture
def server(tmp_path):
    clean = tmp_path / "clean"
    clean.mkdir()
    (clean / "screened_jobs.json").write_text(json.dumps([screened_row("j1")]))
    srv = serve.make_server(0, tmp_path, token="tok123")
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def call(srv, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    h = {"Host": f"127.0.0.1:{srv.port}", **(headers or {})}
    payload = json.dumps(body) if body is not None else None
    if payload:
        h.setdefault("Content-Type", "application/json")
    conn.request(method, path, body=payload, headers=h)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    return resp.status, dict(resp.getheaders()), raw


def good_headers(srv, **over):
    return {"Origin": f"http://127.0.0.1:{srv.port}", "X-Dashboard-Token": "tok123", **over}


def test_binds_to_loopback_only(server):
    assert server.server_address[0] == "127.0.0.1"


def test_page_embeds_token_and_state_is_served(server):
    status, headers, raw = call(server, "GET", "/")
    assert status == 200 and b'content="tok123"' in raw and b"__TOKEN__" not in raw
    assert "default-src 'none'" in headers["Content-Security-Policy"]
    status, _, raw = call(server, "GET", "/api/state")
    assert status == 200 and json.loads(raw)["counts"]["ready"] == 1


@pytest.mark.parametrize("host", ["evil.com", "evil.com:8765", "127.0.0.1.evil.com", "", "localhost:1"])
def test_bad_host_rejected_even_on_get(server, host):
    status, _, _ = call(server, "GET", "/api/state", headers={"Host": host})
    assert status == 403


def test_localhost_host_accepted(server):
    status, _, _ = call(server, "GET", "/api/state", headers={"Host": f"localhost:{server.port}"})
    assert status == 200


@pytest.mark.parametrize("origin", ["http://evil.com", "https://127.0.0.1", "null", "", "http://localhost.evil.com"])
def test_bad_or_missing_origin_rejected_on_write(server, origin):
    h = good_headers(server)
    h["Origin"] = origin
    status, _, _ = call(server, "POST", "/api/applications/j1", {"status": "applied"}, h)
    assert status == 403
    assert not (server.data_dir / "clean" / "applications.json").exists()


def test_token_required_on_write(server):
    for tok in (None, "", "wrong"):
        h = good_headers(server)
        h.pop("X-Dashboard-Token")
        if tok is not None:
            h["X-Dashboard-Token"] = tok
        status, _, _ = call(server, "POST", "/api/applications/j1", {"status": "applied"}, h)
        assert status == 403
    assert not (server.data_dir / "clean" / "applications.json").exists()


def test_valid_write_updates_store_with_server_side_snapshot(server):
    status, _, raw = call(server, "POST", "/api/applications/j1",
                          {"status": "shortlisted", "snapshot": {"company": "Forged"}}, good_headers(server))
    assert status == 200 and json.loads(raw)["snapshot"]["company"] == "Acme"
    status, _, raw = call(server, "POST", "/api/applications/j1", {"mark_applied": True}, good_headers(server))
    assert json.loads(raw)["status"] == "applied" and json.loads(raw)["applied_at"] == date.today().isoformat()


def test_write_errors(server):
    h = good_headers(server)
    assert call(server, "POST", "/api/applications/j1", {"status": "hired"}, h)[0] == 400
    assert call(server, "POST", "/api/applications/nope", {"status": "applied"}, h)[0] == 404
    assert call(server, "POST", "/api/other", {"x": 1}, h)[0] == 404
    assert call(server, "POST", "/api/applications/j1", ["not", "an", "object"], h)[0] == 400


# ---------- escaping ----------

STATIC = Path(serve.__file__).parent / "static"
HOSTILE = '<img src=x onerror=alert(1)>'


def test_frontend_never_uses_html_sinks():
    js = (STATIC / "app.js").read_text()
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert sink not in js
    assert "textContent" in js and 'rel: "noopener noreferrer"' in js
    assert "http:" in js and "https:" in js                  # safeHref allow-list


def test_no_external_resources_in_frontend():
    for name in ("index.html", "app.js", "app.css"):
        text = (STATIC / name).read_text()
        assert not re.findall(r"""(?:src|href)=["']https?://|@import|url\(https?:""", text)
        assert "XMLHttpRequest" not in text and "WebSocket" not in text
    js = (STATIC / "app.js").read_text()
    allowed = {'"/api/state"', '"/api/eval"', "path", '"/api/tailor/run"', '"/api/pipeline/status"',
               '"/files/" + encodeURIComponent(folder'}
    assert set(re.findall(r'fetch\(([^,)]+)', js)) <= allowed
    assert all(p in ('"/api/applications/" + encodeURIComponent(job.id)', '"/api/labels/" + encodeURIComponent(id)')
               for p in re.findall(r'post\(("/api/[^,]+\))', js))


@pytest.mark.parametrize("url,ok", [("https://a.com/x", True), ("http://a.com", True), ("javascript:alert(1)", False),
                                    ("data:text/html,<script>", False), ("//evil.com", False), ("", False),
                                    (None, False), ("  https://a.com  ", True)])
def test_safe_url_allows_only_http_and_https(url, ok):
    assert bool(dash_data.safe_url(url)) is ok


def test_hostile_job_text_is_data_not_markup(tmp_path):
    clean = tmp_path / "clean"
    clean.mkdir()
    row = screened_row("h1", title=HOSTILE, company=HOSTILE, url="javascript:alert(1)")
    (clean / "screened_jobs.json").write_text(json.dumps([row]))
    srv = serve.make_server(0, tmp_path, token="t")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        status, headers, raw = call(srv, "GET", "/api/state")
        assert headers["Content-Type"] == "application/json" and headers["X-Content-Type-Options"] == "nosniff"
        job = json.loads(raw)["jobs"][0]
        assert job["title"] == HOSTILE and job["url"] == ""      # kept as text; unsafe link dropped
        assert HOSTILE.encode() not in call(srv, "GET", "/")[2]  # never templated into the page
    finally:
        srv.shutdown()
        srv.server_close()


def test_demo_data_is_synthetic_and_loads():
    root = make_demo_dir()
    try:
        state = dash_data.build_state(root, ApplicationsStore(root / "clean" / "applications.json"))
        assert state["counts"]["total_scored"] == 48 and state["counts"]["ready"] > 0
        assert any(r["orphaned"] for r in state["jobs"])
        assert all("example.com" in r["url"] for r in state["jobs"])
    finally:
        import shutil
        shutil.rmtree(root, ignore_errors=True)
