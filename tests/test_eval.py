"""Labels and evaluation: store, metric math on a known fixture, endpoint security, demo."""
import json
import shutil
import threading

import pytest

from dashboard import serve
from dashboard.demo import make_demo_dir
from dashboard.labels import LabelStore
from dashboard.store import StoreError
from evaluate import __main__ as evaluate_cli
from evaluate.metrics import MIN_LABELS, aggregate_only, compute, drop_category, filtered_totals
from tests.test_dashboard import call, good_headers, screened_row


def scored(verdict, label, reason=None, note="", score=70):
    return {"kind": "scored", "label": label, "reason": reason, "note": note, "labelled_at": "t",
            "snapshot": {"score": score, "verdict": verdict}}


def dropped(reason):
    return {"kind": "filtered", "label": "yes", "reason": None, "note": "", "labelled_at": "t",
            "snapshot": {"drop_reason": reason}}


# ---------- label store ----------

@pytest.fixture
def store(tmp_path):
    return LabelStore(tmp_path / "clean" / "labels.json")


def test_label_roundtrip_and_clear(store):
    e = store.set("j1", kind="scored", label="yes", snapshot={"score": 80, "verdict": "apply"},
                  reason="location", note="n", now="t1")
    assert e["snapshot"] == {"score": 80, "verdict": "apply"} and store.load()["j1"]["reason"] == "location"
    assert store.set("j1", kind="scored", label=None, snapshot={}) is None
    assert store.load() == {}


def test_snapshot_survives_rescoring_and_note_edits_but_not_a_new_judgment(store):
    store.set("j1", kind="scored", label="no", snapshot={"score": 40, "verdict": "skip"}, now="t1")
    # job is re-scored (apply/90); editing only the reason/note keeps the original snapshot
    e = store.set("j1", kind="scored", label="no", snapshot={"score": 90, "verdict": "apply"},
                  reason="domain", note="later note", now="t2")
    assert e["snapshot"] == {"score": 40, "verdict": "skip"} and e["labelled_at"] == "t1"
    # changing the label value is a new judgment against the current agent output
    e = store.set("j1", kind="scored", label="yes", snapshot={"score": 90, "verdict": "apply"}, now="t3")
    assert e["snapshot"]["verdict"] == "apply" and e["labelled_at"] == "t3"


def test_label_validation(store):
    snap = {"score": 1, "verdict": "skip"}
    for kw in ({"label": "definitely"}, {"label": "yes", "reason": "vibes"},
               {"label": "yes", "note": "x" * 6000}, {"kind": "other", "label": "yes"}):
        with pytest.raises(StoreError):
            store.set("j1", **{"kind": "scored", "snapshot": snap, **kw})
    with pytest.raises(StoreError):
        store.set("j1", kind="filtered", label="no", snapshot={"drop_reason": "x"})


def test_label_write_is_atomic_and_corrupt_file_untouched(store):
    store.path.parent.mkdir(parents=True)
    store.path.write_text("{bad")
    with pytest.raises(StoreError):
        store.set("j1", kind="scored", label="yes", snapshot={"score": 1, "verdict": "apply"})
    assert store.path.read_text() == "{bad"
    store.path.unlink()
    store.set("j1", kind="scored", label="yes", snapshot={"score": 1, "verdict": "apply"})
    assert list(store.path.parent.glob("*.tmp")) == []


# ---------- metric math (known answers) ----------

def fixture_labels():
    """10 scored: apply->yes x3, apply->no x1, stretch->maybe x2, stretch->yes x1,
    skip->no x1, skip->yes x2.  Agreements: 3 + 2 + 1 = 6."""
    labels = {}
    spec = [("apply", "yes")] * 3 + [("apply", "no", "domain")] + [("stretch", "maybe")] * 2 + \
           [("stretch", "yes", "seniority")] + [("skip", "no")] + \
           [("skip", "yes", "location"), ("skip", "yes", "location")]
    for i, s in enumerate(spec):
        labels[f"j{i}"] = scored(s[0], s[1], s[2] if len(s) > 2 else None, note=f"n{i}")
    return labels


def test_confusion_errors_and_reason_counts():
    m = compute(fixture_labels())
    assert m["confusion"] == {"apply": {"yes": 3, "maybe": 0, "no": 1},
                              "stretch": {"yes": 1, "maybe": 2, "no": 0},
                              "skip": {"yes": 2, "maybe": 0, "no": 1}}
    assert m["errors"] == {"agent_skip_but_i_said_yes": 2, "agent_apply_but_i_said_no": 1}
    assert m["agreement"]["agree"] == 6 and m["agreement"]["total"] == 10
    assert m["disagreements_by_reason"] == {"location": 2, "domain": 1, "seniority": 1}
    assert len(m["disagreements"]) == 4


def test_no_headline_percentage_before_30_labels():
    assert compute(fixture_labels())["agreement"]["rate"] is None
    many = {f"k{i}": scored("apply", "yes" if i < 24 else "no") for i in range(MIN_LABELS)}
    m = compute(many)
    assert m["progress"] == {"labelled": 30, "target": 30} and m["agreement"]["rate"] == 0.8


def test_disagreement_without_reason_counted_as_none_given():
    m = compute({"a": scored("apply", "no")})
    assert m["disagreements_by_reason"] == {"none given": 1}


def test_false_drop_rate_overall_and_per_reason():
    filtered = [{"reason": "title_untiered"}] * 6 + [{"reason": "title_reject:Sales"}] * 2 + \
               [{"reason": "stage1: US only"}] * 2
    labels = {"a": dropped("title_untiered"), "b": dropped("title_untiered"), "c": dropped("stage1: US only")}
    f = compute(labels, filtered_totals(filtered))["filtered"]
    assert f["flagged"] == 3 and f["total"] == 10 and f["false_drop_rate"] == 0.3
    assert f["by_reason"]["title_untiered"] == {"flagged": 2, "total": 6, "false_drop_rate": 0.3333}
    assert f["by_reason"]["stage1"]["false_drop_rate"] == 0.5
    assert f["by_reason"]["title_reject"]["false_drop_rate"] == 0.0
    assert drop_category("stage1: x") == "stage1"


def test_empty_labels_are_safe():
    m = compute({})
    assert m["agreement"] == {"agree": 0, "total": 0, "rate": None}
    assert m["filtered"]["false_drop_rate"] is None


def test_report_is_aggregate_only(tmp_path):
    clean = tmp_path / "clean"
    clean.mkdir()
    (clean / "screened_jobs.json").write_text(json.dumps([screened_row("j0", company="SecretCo")]))
    (clean / "filtered_out.json").write_text(json.dumps(
        [{"id": "f1", "company": "HiddenCo", "title": "Secret Title", "location": "x", "reason": "title_untiered"}]))
    labels = LabelStore(clean / "labels.json")
    labels.set("j0", kind="scored", label="no", snapshot={"score": 80, "verdict": "apply"},
               reason="company", note="friend works at SecretCo")
    labels.set("f1", kind="filtered", label="yes", snapshot={"drop_reason": "title_untiered"})
    report = evaluate_cli.run(tmp_path)
    text = (clean / "eval_report.json").read_text()
    for forbidden in ("SecretCo", "HiddenCo", "Secret Title", "friend works", "https://", "j0", "f1"):
        assert forbidden not in text
    assert report["errors"]["agent_apply_but_i_said_no"] == 1 and "disagreements" not in report
    assert "disagreements" not in aggregate_only(compute(labels.load()))


# ---------- endpoint ----------

@pytest.fixture
def server(tmp_path):
    clean = tmp_path / "clean"
    clean.mkdir()
    (clean / "screened_jobs.json").write_text(json.dumps([screened_row("j1")]))
    (clean / "filtered_out.json").write_text(json.dumps(
        [{"id": "f1", "company": "C", "title": "T", "location": "L", "reason": "title_untiered"}]))
    srv = serve.make_server(0, tmp_path, token="tok123")
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv
    srv.shutdown()
    srv.server_close()


def test_label_write_requires_token_and_origin(server):
    path = server.data_dir / "clean" / "labels.json"
    for h in ({k: v for k, v in good_headers(server).items() if k != "X-Dashboard-Token"},
              good_headers(server, **{"X-Dashboard-Token": "wrong"}),
              good_headers(server, Origin="http://evil.com")):
        assert call(server, "POST", "/api/labels/j1", {"label": "yes"}, h)[0] == 403
    assert not path.exists()


def test_label_write_uses_server_side_snapshot(server):
    h = good_headers(server)
    status, _, raw = call(server, "POST", "/api/labels/j1",
                          {"label": "no", "reason": "domain", "note": "x",
                           "snapshot": {"verdict": "forged", "score": 1}}, h)
    entry = json.loads(raw)["entry"]
    assert status == 200 and entry["snapshot"] == {"score": 80, "verdict": "apply"} and entry["kind"] == "scored"
    status, _, raw = call(server, "POST", "/api/labels/f1", {"label": "yes", "reason": "role type"}, h)
    assert json.loads(raw)["entry"]["snapshot"] == {"drop_reason": "title_untiered"}
    assert call(server, "POST", "/api/labels/f1", {"label": "no"}, h)[0] == 400
    assert call(server, "POST", "/api/labels/zzz", {"label": "yes"}, h)[0] == 404
    state = json.loads(call(server, "GET", "/api/state")[2])
    assert set(state["labels"]) == {"j1", "f1"} and state["eval"]["errors"]["agent_apply_but_i_said_no"] == 1
    assert call(server, "POST", "/api/labels/j1", {"label": None}, h)[0] == 200
    assert json.loads(call(server, "GET", "/api/eval")[2])["progress"]["labelled"] == 0
    assert "j1" not in json.loads(call(server, "GET", "/api/state")[2])["labels"]


# ---------- demo ----------

def test_demo_mode_has_synthetic_labels_and_eval():
    root = make_demo_dir()
    try:
        srv = serve.make_server(0, root, token="t", demo=True)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            state = json.loads(call(srv, "GET", "/api/state")[2])
        finally:
            srv.shutdown()
            srv.server_close()
        m = state["eval"]
        assert state["demo"] and len(state["labels"]) == 37
        assert m["progress"]["labelled"] == 34 and m["agreement"]["rate"] is not None
        assert m["filtered"]["flagged"] == 3
    finally:
        shutil.rmtree(root, ignore_errors=True)
