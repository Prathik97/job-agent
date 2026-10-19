"use strict";
// All job text is untrusted. It is only ever put into the page with textContent / createTextNode,
// and URLs only become links if they are http or https.

const REASONS = ["location", "seniority", "domain", "role type", "company", "compensation", "other"];
const STATUSES = ["new", "shortlisted", "applied", "interview", "offer", "rejected", "withdrawn", "skipped"];
const PIPELINE = ["applied", "interview", "offer", "rejected", "withdrawn"];
const TOKEN = document.querySelector('meta[name="dashboard-token"]').content;
const panel = document.getElementById("panel");
const banner = document.getElementById("banner");

let data = null;
let tab = "ready";
const ui = {
  sort: { ready: ["score", -1], pipeline: ["applied_at", -1], all: ["score", -1], filtered: ["company", 1] },
  sample: { on: false, ids: [] },
  showAllStretch: false,
  showFlagged: false,
  onlyNew: false,
  // Score a JD: held in memory only (never in localStorage), so a pasted JD does not outlive the page.
  jd: { jd: "", company: "", title: "", location: "", url: "", est: null, result: null, tailor: null, added: null, status: "" },
  filters: { all: { q: "", verdict: "", status: "", source: "", tier: "" }, filtered: { q: "", reason: "" } },
};

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k === "style") node.style.cssText = v;   // CSSOM, allowed by the CSP (no inline style attrs)
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

function safeHref(url) {
  try {
    const u = new URL(url);
    return u.protocol === "http:" || u.protocol === "https:" ? u.href : null;
  } catch (e) { return null; }
}

function openLink(url) {
  const href = safeHref(url);
  return href
    ? el("a", { class: "btn", href, target: "_blank", rel: "noopener noreferrer", text: "Open" })
    : el("span", { class: "chip", text: "no link" });
}

function say(msg) { banner.textContent = msg || ""; }

async function loadState() {
  const res = await fetch("/api/state");
  if (!res.ok) throw new Error("state " + res.status);
  data = await res.json();
}

async function post(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Dashboard-Token": TOKEN },
    body: JSON.stringify(body),
  });
  if (res.status === 403) { say("Session expired. Reload this page."); throw new Error("forbidden"); }
  if (!res.ok) { say("Save failed (" + res.status + ")"); throw new Error("save"); }
  say("");
  return res.json();
}

async function openFile(folder, name) {
  const res = await fetch("/files/" + encodeURIComponent(folder) + "/" + encodeURIComponent(name),
    { headers: { "X-Dashboard-Token": TOKEN } });
  if (!res.ok) { say("Could not open " + name + " (" + res.status + ")"); return; }
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  if (/\.(pdf|md|txt)$/.test(name)) {
    window.open(url, "_blank", "noopener");
  } else {
    const a = el("a", { href: url, download: name }); document.body.append(a); a.click(); a.remove();
  }
  setTimeout(() => URL.revokeObjectURL(url), 60000);
}

async function saveLabel(id, body) {
  const out = await post("/api/labels/" + encodeURIComponent(id), body);
  if (out.entry) data.labels[id] = out.entry; else delete data.labels[id];
}

async function save(job, body) {
  const entry = await post("/api/applications/" + encodeURIComponent(job.id), body);
  job.status = entry.status; job.applied_at = entry.applied_at; job.app_notes = entry.notes;
  job.resume_version = entry.resume_version || null;
  recount();
}

// ---- derived numbers ----
// Ready to apply: apply verdicts plus stretch at or above the score cutoff; capped role families
// never qualify. `showAllStretch` adds the lower-scoring stretch jobs (still never capped families).
const CHECK_NOTES = ["possibly stale", "verify location"];
const needsCheck = r => r.notes.some(n => CHECK_NOTES.some(c => n.toLowerCase().includes(c)));

function isReady(r, showAllStretch, showFlagged) {
  if (r.orphaned || !["new", "shortlisted"].includes(r.status)) return false;
  if (data.counts.capped_families.includes(r.role_family)) return false;
  if (r.verdict === "apply") return true;   // apply jobs stay visible and keep their notes
  if (r.verdict !== "stretch" || (needsCheck(r) && !showFlagged)) return false;
  return showAllStretch || (r.score || 0) >= data.counts.ready_stretch_min;
}

function recount() {
  const rows = data.jobs, c = data.counts;
  const n = (...s) => rows.filter(r => s.includes(r.status)).length;
  c.ready = rows.filter(r => isReady(r, false, false)).length;
  c.new_ready = rows.filter(r => r.is_new && isReady(r, false, false)).length;
  c.applied = n("applied"); c.interview = n("interview"); c.rejected = n("rejected"); c.skipped = n("skipped");
  renderStrip();
}

function renderStrip() {
  const c = data.counts;
  const stat = (n, l, small) => el("div", { class: "stat" + (small ? " small" : "") },
    el("div", { class: "n", text: n }), el("div", { class: "l", text: l }));
  const when = c.last_run ? new Date(c.last_run).toLocaleString() : "never";
  document.getElementById("strip").replaceChildren(
    stat(c.ready, "Ready to apply"), stat(c.new_ready, "New since last visit"), stat(c.applied, "Applied"), stat(c.interview, "Interviewing"),
    stat(c.rejected, "Rejected"), stat(c.skipped, "Skipped"), stat(c.total_scored, "Total scored"),
    stat(when, "Last run", true), stat("$" + c.month_cost.toFixed(2), "API cost this month", true));
}

// ---- generic table ----
function compare(a, b) {
  if (a === b) return 0;
  if (a === null || a === undefined || a === "") return 1;   // blanks last
  if (b === null || b === undefined || b === "") return -1;
  return typeof a === "number" && typeof b === "number" ? a - b : String(a).localeCompare(String(b));
}

function buildTable(key, cols, rows, onSort) {
  if (!rows.length) return el("div", { class: "empty", text: "Nothing here." });
  const [skey, sdir] = ui.sort[key] || [null, 1];
  const col = cols.find(c => c.key === skey);
  const sorted = col ? [...rows].sort((a, b) => sdir * compare(col.value(a), col.value(b))) : rows;
  const head = el("tr", {}, cols.map(c => {
    const arrow = c.key === skey ? (sdir > 0 ? " ▲" : " ▼") : "";
    return el("th", c.sortable === false ? {} : {
      class: "sortable", onclick: () => {
        ui.sort[key] = [c.key, c.key === skey ? -sdir : (c.num ? -1 : 1)]; onSort();
      } }, c.label + arrow);
  }));
  const body = sorted.map(r => el("tr", {}, cols.map(c =>
    el("td", { class: c.num ? "num" : "" }, c.render ? c.render(r) : c.value(r)))));
  return el("div", { class: "tablewrap" }, el("table", {}, el("thead", {}, head), el("tbody", {}, body)));
}

// ---- column pieces ----
const text = (key, label, extra) => ({ key, label, value: r => r[key] || "", ...extra });
const titleWithNew = r => [r.title || "", ...(r.is_new ? [el("span", { class: "chip new", text: "New" })] : [])];
const verdictChip = r => el("span", { class: "chip " + r.verdict, text: r.verdict || "" });
const notesCell = r => r.notes.map(n => el("span", { class: "note", text: n }));
const statusSelect = r => {
  const sel = el("select", { "aria-label": "Status" }, STATUSES.map(s => {
    const o = el("option", { value: s, text: s }); if (s === r.status) o.selected = true; return o;
  }));
  sel.addEventListener("change", async () => {
    try { await save(r, { status: sel.value }); refresh(); } catch (e) { sel.value = r.status; }
  });
  return sel;
};
const markApplied = r => el("button", { class: "btn primary", text: "Mark applied", onclick: async () => {
  try { await save(r, { mark_applied: true }); refresh(); } catch (e) { /* banner already shows it */ }
} });
const notesInput = r => {
  const input = el("input", { class: "notes", type: "text", value: r.app_notes || "", "aria-label": "Notes",
    maxlength: 5000 });
  input.addEventListener("change", () => save(r, { notes: input.value }).catch(() => {}));
  return input;
};
const sourceCell = r => [r.source || "—", ...r.also_listed_on.map(s => el("span", { class: "chip", text: "also " + s }))];
const scoreCell = r => (r.score === null || r.score === undefined) ? "" : String(r.score);

function reasonSelect(current, disabled) {
  const sel = el("select", { "aria-label": "Reason category", disabled }, [el("option", { value: "", text: "reason (optional)" }),
    ...REASONS.map(r => { const o = el("option", { value: r, text: r }); if (r === current) o.selected = true; return o; })]);
  return sel;
}

const labelCell = r => {
  const cur = data.labels[r.id] || {};
  const lab = el("select", { "aria-label": "My label" }, [["", "— my label —"], ["yes", "yes (I would apply)"],
    ["maybe", "maybe"], ["no", "no"]].map(([v, t]) => { const o = el("option", { value: v, text: t });
      if (v === (cur.label || "")) o.selected = true; return o; }));
  const reason = reasonSelect(cur.reason, !cur.label);
  const note = el("input", { type: "text", placeholder: "note (optional)", value: cur.note || "", maxlength: 5000,
    disabled: !cur.label, "aria-label": "Label note" });
  const commit = async () => {
    reason.disabled = note.disabled = !lab.value;
    try { await saveLabel(r.id, { label: lab.value || null, reason: reason.value, note: note.value }); }
    catch (e) { /* banner shows the error */ }
  };
  [lab, reason, note].forEach(c => c.addEventListener("change", commit));
  return el("div", { class: "labelbox" }, lab, reason, note);
};

const keptCell = r => {
  const box = el("div", { class: "labelbox" });
  const draw = () => {
    const now = data.labels[r.id];
    const mark = (text, value) => el("button", {
      class: "btn" + (now && now.label === value ? " primary" : ""), text,
      onclick: async () => {
        const same = now && now.label === value;
        try {
          await saveLabel(r.id, same ? { label: null } : { label: value, reason: value === "yes" && now ? now.reason || "" : "" });
          draw(); if (ui.onReviewed) ui.onReviewed();
        } catch (e) { /* banner shows the error */ }
      } });
    const reason = reasonSelect(now && now.reason, false);
    reason.addEventListener("change", async () => {
      try { await saveLabel(r.id, { label: "yes", reason: reason.value }); } catch (e) { /* banner */ }
    });
    box.replaceChildren(mark("Correctly dropped", "no"), mark("Should have been kept", "yes"),
      now && now.label === "yes" ? reason : "");
  };
  draw();
  return box;
};

function fileLinks(folder, files) {
  return el("div", { class: "files" }, el("div", { class: "l", text: "Saved in output/resumes/" + folder }),
    files.map(f => el("button", { class: "btn", text: f, onclick: () => openFile(folder, f) })));
}

function openPrepare(job) {
  const existing = (data.prepared || {})[job.id];
  const status = el("div", { class: "modalstatus" });
  const out = el("div", {});
  const jd = el("textarea", { rows: 8, placeholder: "Paste the full job description here", "aria-label": "Full job description",
    maxlength: 200000 });
  const runBtn = el("button", { class: "btn primary", text: "Run tailor", disabled: true });
  const estBtn = el("button", { class: "btn", text: "Get cost estimate" });
  const close = () => overlay.remove();
  const body = () => ({ job_id: job.id, jd: jd.value.trim() || null });

  async function estimate() {
    status.textContent = "Estimating...";
    try {
      const r = await post("/api/tailor/estimate", body());
      if (r.requires_jd) { status.textContent = "This job's description is partial. Paste the full description, then get the estimate."; runBtn.disabled = true; return; }
      status.textContent = "Model: " + r.model + ". Estimated cost $" + r.est_cost.toFixed(3) + " (worst case $" + r.worst_cost.toFixed(3) + ", limit $" + r.max_cost.toFixed(2) + ").";
      runBtn.disabled = false;
    } catch (e) { status.textContent = "Estimate failed."; }
  }
  estBtn.addEventListener("click", estimate);
  runBtn.addEventListener("click", async () => {
    runBtn.disabled = true; status.textContent = "Tailoring... this takes up to a minute.";
    out.replaceChildren();
    const res = await fetch("/api/tailor/run", { method: "POST",
      headers: { "Content-Type": "application/json", "X-Dashboard-Token": TOKEN }, body: JSON.stringify(body()) });
    const r = await res.json();
    if (!res.ok) {
      status.textContent = r.error || "Tailor failed.";
      out.replaceChildren(...(r.violations || []).map(v => el("div", { class: "note", text: v })));
      runBtn.disabled = false; return;
    }
    data.prepared[job.id] = { folder: r.folder, files: r.files };
    status.textContent = "Done (" + r.model + ", $" + r.cost.toFixed(3) + ").";
    out.replaceChildren(fileLinks(r.folder, r.files));
    refresh();
  });
  if (existing) out.replaceChildren(fileLinks(existing.folder, existing.files));
  const overlay = el("div", { class: "overlay", onclick: ev => { if (ev.target === overlay) close(); } },
    el("div", { class: "modal", role: "dialog", "aria-label": "Prepare application" },
      el("h2", { text: job.company + ": " + job.title }),
      job.verdict ? el("div", { class: "l", text: "Score " + job.score + ", " + job.verdict }) : "",
      job.jd_partial ? el("div", { class: "note", text: "Partial JD from an aggregator. Paste the full description to continue." }) : "",
      job.jd_partial ? jd : el("div", {}, el("details", {}, el("summary", { text: "Replace the stored description (optional)" }), jd)),
      el("div", { class: "toolbar" }, estBtn, runBtn, el("button", { class: "btn", text: "Close", onclick: close })),
      status, out));
  document.body.append(overlay);
  if (!job.jd_partial) estimate();
}

const prepareCell = r => {
  const p = (data.prepared || {})[r.id];
  return el("button", { class: "btn" + (p ? "" : " primary"), text: p ? "Prepared: files" : "Prepare application",
    onclick: () => openPrepare(r) });
};

const COLS = {
  prepare: { key: "prepare", label: "Application", sortable: false, value: () => "", render: prepareCell },
  resume: { key: "resume_version", label: "Resume version", value: r => r.resume_version || "" },
  myLabel: { key: "my_label", label: "My label", sortable: false, value: r => (data.labels[r.id] || {}).label || "", render: labelCell },
  kept: { key: "kept", label: "Keep?", sortable: false, value: () => "", render: keptCell },
  company: text("company", "Company"), title: text("title", "Title"),
  titleNew: { key: "title", label: "Title", value: r => r.title || "", render: titleWithNew }, location: text("location", "Location"),
  source: { key: "source", label: "Source", value: r => r.source || "", render: sourceCell },
  score: { key: "score", label: "Score", num: true, value: r => r.score, render: scoreCell },
  verdict: { key: "verdict", label: "Verdict", value: r => r.verdict, render: verdictChip },
  notes: { key: "notes", label: "Notes", sortable: false, value: r => r.notes.join("; "), render: notesCell },
  posted: { key: "age_days", label: "Posted", num: true, value: r => r.age_days,
    render: r => {
      if (r.age_days === null || r.age_days === undefined) return el("span", { class: "chip", text: "unknown age" });
      const seen = r.hours_at_first_seen === null || r.hours_at_first_seen === undefined ? []
        : [el("span", { class: "note", text: "first seen " + r.hours_at_first_seen + "h after posting" })];
      return [r.age_days + " days ago", ...(r.age_days > 60 ? [el("span", { class: "note", text: "possibly stale" })] : []), ...seen];
    } },
  open: { key: "open", label: "", sortable: false, value: () => "", render: r => openLink(r.url) },
  status: { key: "status", label: "Status", value: r => r.status, render: statusSelect },
  mark: { key: "mark", label: "", sortable: false, value: () => "", render: markApplied },
  applied: { key: "applied_at", label: "Applied", value: r => r.applied_at || "" },
  tier: { key: "tier", label: "Tier", value: r => r.tier },
  myNotes: { key: "app_notes", label: "My notes", sortable: false, value: r => r.app_notes, render: notesInput },
  followUp: { key: "follow_up", label: "", value: r => (r.follow_up ? 1 : 0),
    render: r => r.follow_up ? el("span", { class: "chip followup", text: "follow up" }) : "" },
};

// ---- tabs ----
function holder() { return el("div", { id: "holder" }); }
function swapHolder(node) { document.getElementById("holder").replaceChildren(node); }

function renderReady() {
  const rows = () => data.jobs.filter(r => isReady(r, ui.showAllStretch, ui.showFlagged) && (!ui.onlyNew || r.is_new));
  const cols = [COLS.company, COLS.titleNew, COLS.location, COLS.source, COLS.score, COLS.verdict, COLS.notes,
    COLS.posted, COLS.open, COLS.prepare, COLS.status, COLS.mark];
  const toggle = el("label", { class: "toggle" },
    el("input", { type: "checkbox", checked: ui.showAllStretch, onchange: ev => { ui.showAllStretch = ev.target.checked; refresh(); } }),
    " Show all stretch (below " + data.counts.ready_stretch_min + " too)");
  const flagged = el("label", { class: "toggle" },
    el("input", { type: "checkbox", checked: ui.showFlagged, onchange: ev => { ui.showFlagged = ev.target.checked; refresh(); } }),
    " Show stale or unverified");
  const onlyNew = el("label", { class: "toggle" },
    el("input", { type: "checkbox", checked: ui.onlyNew, onchange: ev => { ui.onlyNew = ev.target.checked; refresh(); } }),
    " Show only new");
  return { toolbar: [toggle, flagged, onlyNew, el("span", { class: "count", id: "count" })],
    draw() { const r = rows(); document.getElementById("count").textContent = r.length + " jobs";
      swapHolder(buildTable("ready", cols, r, refresh)); } };
}

function renderPipeline() {
  const rows = () => data.jobs.filter(r => PIPELINE.includes(r.status));
  const cols = [COLS.company, COLS.title, COLS.status, COLS.applied, COLS.followUp, COLS.resume, COLS.myNotes,
    COLS.score, COLS.verdict, COLS.open];
  return { toolbar: [el("span", { class: "count", id: "count" })],
    draw() { const r = rows(); document.getElementById("count").textContent = r.length + " jobs";
      swapHolder(buildTable("pipeline", cols, r, refresh)); } };
}

function select(options, current, label, onChange) {
  const sel = el("select", { "aria-label": label }, [el("option", { value: "", text: label }),
    ...options.map(o => { const e = el("option", { value: o, text: o }); if (o === current) e.selected = true; return e; })]);
  sel.addEventListener("change", () => onChange(sel.value));
  return sel;
}

function renderAll() {
  const f = ui.filters.all;
  const uniq = k => [...new Set(data.jobs.map(r => r[k]).filter(Boolean))].sort();
  const cols = [COLS.company, COLS.title, COLS.location, COLS.source, COLS.tier, COLS.score, COLS.verdict,
    COLS.myLabel, COLS.status, COLS.notes, COLS.posted, COLS.open];
  const search = el("input", { type: "search", placeholder: "Search company, title, location", value: f.q,
    "aria-label": "Search" });
  const upd = () => draw();
  search.addEventListener("input", () => { f.q = search.value; upd(); });
  function draw() {
    const q = f.q.trim().toLowerCase();
    const rows = data.jobs.filter(r => !r.orphaned && (!f.verdict || r.verdict === f.verdict) && (!f.status || r.status === f.status)
      && (!f.source || r.source === f.source) && (!f.tier || r.tier === f.tier)
      && (!q || [r.company, r.title, r.location].join(" ").toLowerCase().includes(q)));
    document.getElementById("count").textContent = rows.length + " of " + data.counts.total_scored;
    swapHolder(buildTable("all", cols, rows, draw));
  }
  return { toolbar: [search,
    select(["apply", "stretch", "skip"], f.verdict, "All verdicts", v => { f.verdict = v; upd(); }),
    select(STATUSES, f.status, "All statuses", v => { f.status = v; upd(); }),
    select(uniq("source"), f.source, "All sources", v => { f.source = v; upd(); }),
    select(uniq("tier"), f.tier, "All tiers", v => { f.tier = v; upd(); }),
    el("span", { class: "count", id: "count" })], draw };
}

// ---- Score a JD ----
// The pasted text is sent only to this server's /api/jd/* routes (token required) and kept in memory here.
async function postJD(path, body) {
  const res = await fetch(path, { method: "POST",
    headers: { "Content-Type": "application/json", "X-Dashboard-Token": TOKEN }, body: JSON.stringify(body) });
  let json = {};
  try { json = await res.json(); } catch (e) { /* non-JSON error body */ }
  if (res.status === 403) say("Session expired. Reload this page.");
  return { ok: res.ok, status: res.status, json };
}

function renderJD() {
  const s = ui.jd;
  const jdBox = el("textarea", { class: "jdbox", rows: 16, maxlength: 100000, "aria-label": "Full job description",
    placeholder: "Paste the full job description here (required)" });
  jdBox.value = s.jd;
  const field = (key, label, ph) => {
    const input = el("input", { type: "text", maxlength: 200, placeholder: ph, "aria-label": label, value: s[key] });
    input.addEventListener("input", () => { s[key] = input.value; invalidate(); });
    return el("label", { class: "jdfield" }, el("span", { class: "l", text: label }), input);
  };
  const estBtn = el("button", { class: "btn", text: "Get cost estimate" });
  const scoreBtn = el("button", { class: "btn primary", text: "Score this JD", disabled: true });
  const clearBtn = el("button", { class: "btn", text: "Clear" });
  const status = el("div", { class: "modalstatus", role: "status" });
  const results = el("div", { class: "jdresults" });

  const body = () => ({ jd: s.jd, company: s.company.trim(), title: s.title.trim(), location: s.location.trim(), url: s.url.trim() });
  function invalidate() { s.est = null; scoreBtn.disabled = true; status.textContent = s.jd.trim() ? "Get the cost estimate, then score." : ""; }
  jdBox.addEventListener("input", () => { s.jd = jdBox.value; invalidate(); });

  function showEstimate() {
    const e = s.est;
    if (!e) return;
    const parts = e.cached ? "Cached from an earlier run: no model call, $0.00."
      : "Estimated cost $" + e.est_cost.toFixed(3) + " (details $" + e.extract_cost.toFixed(3) + ", scoring $" + e.score_cost.toFixed(3) +
        "), limit $" + e.max_cost.toFixed(2) + ".";
    status.textContent = e.over_limit ? parts + " Above the limit, so scoring is blocked." : parts;
    scoreBtn.disabled = e.over_limit;
  }

  estBtn.addEventListener("click", async () => {
    if (!s.jd.trim()) { status.textContent = "Paste the job description first."; return; }
    status.textContent = "Estimating...";
    const r = await postJD("/api/jd/estimate", body());
    if (!r.ok) { status.textContent = r.json.error || "Estimate failed."; return; }
    s.est = r.json; showEstimate();
  });
  scoreBtn.addEventListener("click", async () => {
    scoreBtn.disabled = estBtn.disabled = true; status.textContent = "Scoring... this takes a few seconds.";
    const r = await postJD("/api/jd/score", body());
    estBtn.disabled = false;
    if (!r.ok) { status.textContent = r.json.error || "Scoring failed."; scoreBtn.disabled = !s.est || s.est.over_limit; return; }
    s.result = r.json; s.tailor = null; s.added = null; s.est = null;
    status.textContent = "Done. Cost of this run $" + r.json.cost.toFixed(3) + (r.json.demo ? " (demo, no model called)." : ".");
    drawResult();
  });
  clearBtn.addEventListener("click", () => {
    Object.assign(s, { jd: "", company: "", title: "", location: "", url: "", est: null, result: null, tailor: null, added: null });
    showTab("jd");
  });

  function drawResult() {
    const r = s.result;
    if (!r) { results.replaceChildren(); return; }
    const pctOf = (n, m) => (100 * n / Math.max(1, m)).toFixed(1) + "%";
    const list = (title, items, empty) => el("div", { class: "jdcol" }, el("h2", { text: title }),
      items.length ? el("ul", { class: "plain" }, items.map(t => el("li", { text: t }))) : el("div", { class: "l", text: empty }));
    const pulled = [r.extracted.title ? "title" : "", r.extracted.company ? "company" : "", r.extracted.location ? "location" : ""].filter(Boolean);
    const lowered = r.uncapped_verdict !== r.verdict;
    const m = r.jd_match;

    const header = el("div", { class: "section" },
      el("h2", { text: r.company + " — " + r.title }),
      el("div", { class: "l", text: [r.location || "location not stated", "tier " + r.tier,
        r.years_required ? r.years_required + " years asked" : "years not stated", "family " + r.role_family].join(" · ") }),
      pulled.length ? el("div", { class: "l", text: "Taken from the JD by the model: " + pulled.join(", ") + ". Check them." }) : "",
      r.demo ? el("div", { class: "note", text: "DEMO: no model was called. The score and keywords are stand-ins; the rules and the keyword check are real." }) : "");
    const warns = r.warnings.map(w => el("div", { class: "warnbox" },
      el("strong", { text: "The pipeline's rules would drop this job. " }), w.text, " It was scored anyway."));
    const score = el("div", { class: "section scorecard" },
      el("div", {}, el("div", { class: "headline", text: String(r.score) }), el("div", { class: "l", text: "out of 100" })),
      el("div", {}, el("span", { class: "chip " + r.verdict, text: r.verdict }),
        el("div", { class: "l", text: lowered ? "Model verdict was " + r.uncapped_verdict + "; lowered by the caps below."
          : "apply at " + r.thresholds.apply + "+, stretch at " + r.thresholds.stretch + "+" })));
    const bars = el("div", { class: "bars" }, el("h2", { text: "Score by category" }), r.subscores.map(c =>
      el("div", { class: "barrow" }, el("span", { text: c.label }),
        el("div", { class: "bartrack" }, el("div", { class: "bar", style: "width:" + pctOf(c.points, c.max) })),
        el("span", { class: "num", text: c.points + " / " + c.max }))));
    const caps = el("div", { class: "section" }, el("h2", { text: "Caps applied" }),
      r.caps.length ? r.caps.map(c => el("span", { class: "note", text: c })) : el("div", { class: "l", text: "None." }));
    const info = r.info.length ? el("div", { class: "section" }, r.info.map(t => el("span", { class: "note", text: t }))) : "";
    const trunc = r.truncated ? el("div", { class: "note", text: "This JD is over " + r.limit_chars + " characters, so the middle is trimmed before scoring, as in the pipeline." }) : "";

    let match;
    if (!m.available) {
      match = el("div", { class: "l", text: m.reason });
    } else {
      const rows = m.items.map(i => el("tr", {}, el("td", { text: i.keyword }),
        el("td", {}, el("span", { class: "chip " + (i.backed ? "backed" : "notbacked"), text: i.backed ? "backed" : "not backed" })),
        el("td", { class: "l", text: i.backed ? "\u201c" + i.matched + "\u201d in " + i.where + " (" + i.how + ")" : "not claimed on a resume" })));
      match = el("div", {}, el("div", { class: "l", text: "Backed " + m.backed + " of " + m.items.length + " by master_profile.json (same check as jd_keywords.md)." }),
        m.items.length ? el("div", { class: "tablewrap" }, el("table", {}, el("thead", {}, el("tr", {},
          ["Keyword or skill", "Status", "Where"].map(h => el("th", { text: h })))), el("tbody", {}, rows))) : "");
    }

    // actions
    const actStatus = el("div", { class: "modalstatus", role: "status" });
    const actOut = el("div", {});
    const tailorBtn = el("button", { class: "btn primary", text: "Tailor resume" });
    const addBtn = el("button", { class: "btn", text: "Add to pipeline" });
    const runBtn = el("button", { class: "btn primary", text: "Run tailor", hidden: true });
    const showFiles = t => actOut.replaceChildren(fileLinks(t.folder, t.files));
    if (s.tailor) showFiles(s.tailor);
    if (s.added) actStatus.textContent = s.added;
    tailorBtn.addEventListener("click", async () => {
      actStatus.textContent = "Estimating...";
      const e = await postJD("/api/jd/tailor/estimate", { job_id: r.id });
      if (!e.ok) { actStatus.textContent = e.json.error || "Estimate failed."; return; }
      actStatus.textContent = "Model: " + e.json.model + ". Estimated cost $" + e.json.est_cost.toFixed(3) + " (worst case $" +
        e.json.worst_cost.toFixed(3) + ", limit $" + e.json.max_cost.toFixed(2) + "). Click Run tailor to spend it.";
      runBtn.hidden = false;
    });
    runBtn.addEventListener("click", async () => {
      runBtn.disabled = true; actStatus.textContent = "Tailoring... this takes up to a minute."; actOut.replaceChildren();
      const t = await postJD("/api/jd/tailor/run", { job_id: r.id });
      runBtn.disabled = false;
      if (!t.ok) { actStatus.textContent = t.json.error || "Tailor failed.";
        actOut.replaceChildren(...(t.json.violations || []).map(v => el("div", { class: "note", text: v }))); return; }
      s.tailor = t.json; data.prepared[r.id] = { folder: t.json.folder, files: t.json.files };
      actStatus.textContent = "Done (" + t.json.model + ", $" + t.json.cost.toFixed(3) + "). The fact check passed.";
      runBtn.hidden = true; showFiles(t.json);
    });
    addBtn.addEventListener("click", async () => {
      const a = await postJD("/api/jd/add", { job_id: r.id });
      if (!a.ok) { actStatus.textContent = a.json.error || "Could not add."; return; }
      s.added = a.json.added ? "Added to All scored with source manual. Mark it applied from there or from Ready to apply."
        : "Already in the pipeline (source " + (a.json.source || "unknown") + "); nothing changed.";
      actStatus.textContent = s.added;
      await loadState(); renderStrip();
    });
    const actions = el("div", { class: "section" }, el("h2", { text: "Actions" }),
      el("div", { class: "toolbar" }, tailorBtn, runBtn, addBtn), actStatus, actOut);

    results.replaceChildren(header, ...warns, score, bars, caps, info, trunc,
      el("div", { class: "jdcols section" }, list("Why it fits (top 3)", r.reasons, "No reasons returned."),
        list("Gaps (top 3)", r.gaps, "No gaps found.")),
      el("h2", { text: "JD match" }), match, actions);
  }

  const inputs = el("div", { class: "jdgrid" }, field("company", "Company (optional)", "taken from the JD if blank"),
    field("title", "Title (optional)", "taken from the JD if blank"), field("location", "Location (optional)", "e.g. Bengaluru"),
    field("url", "Source URL (optional, not fetched)", "https://..."));
  return { toolbar: [estBtn, scoreBtn, clearBtn, el("span", { class: "count", text: "Nothing is fetched. The text stays on this computer." })],
    draw() { swapHolder(el("div", { class: "jdform" }, jdBox, inputs, status, results)); showEstimate(); drawResult(); } };
}

const SAMPLE_SIZE = 50;

function drawSample() {
  const pool = data.filtered_out.filter(r => r.review_eligible && !data.labels[r.id]);
  const rnd = new Uint32Array(pool.length);
  crypto.getRandomValues(rnd);
  for (let i = pool.length - 1; i > 0; i--) {
    const j = rnd[i] % (i + 1);
    [pool[i], pool[j]] = [pool[j], pool[i]];
  }
  ui.sample.ids = pool.slice(0, SAMPLE_SIZE).map(r => r.id);
}

function renderFiltered() {
  const f = ui.filters.filtered, sample = ui.sample;
  const category = r => String(r.reason || "").split(":")[0];
  const reasons = [...new Set(data.filtered_out.map(category))].sort();
  const cols = [text("company", "Company"), text("title", "Title"), text("location", "Location"),
    text("reason", "Reason"), COLS.kept];
  const search = el("input", { type: "search", placeholder: "Search company, title, location", value: f.q,
    "aria-label": "Search" });
  search.addEventListener("input", () => { f.q = search.value; draw(); });
  const toggle = el("button", { class: "btn" + (sample.on ? " primary" : ""),
    text: sample.on ? "Leave review sample" : "Review sample", onclick: () => {
      sample.on = !sample.on; if (sample.on && !sample.ids.length) drawSample(); showTab("filtered"); } });
  const redraw = el("button", { class: "btn", text: "Draw new sample", hidden: !sample.on,
    onclick: () => { drawSample(); showTab("filtered"); } });
  function draw() {
    const q = f.q.trim().toLowerCase();
    let rows;
    if (sample.on) {
      const byId = new Map(data.filtered_out.map(r => [r.id, r]));
      rows = sample.ids.map(id => byId.get(id)).filter(Boolean);
      const done = rows.filter(r => data.labels[r.id]).length;
      const eligible = data.filtered_out.filter(r => r.review_eligible).length;
      document.getElementById("count").textContent = done + " of " + rows.length +
        " reviewed (sample drawn from " + eligible + " PM-title or India-location drops)";
    } else {
      rows = data.filtered_out.filter(r => (!f.reason || category(r) === f.reason)
        && (!q || [r.company, r.title, r.location, r.reason].join(" ").toLowerCase().includes(q)));
      document.getElementById("count").textContent = rows.length + " of " + data.filtered_out.length;
    }
    swapHolder(rows.length || !sample.on ? buildTable("filtered", cols, rows, draw)
      : el("div", { class: "empty", text: "No unreviewed eligible jobs left to sample." }));
  }
  ui.onReviewed = sample.on ? draw : null;
  return { toolbar: [toggle, redraw, ...(sample.on ? [] : [search,
    select(reasons, f.reason, "All reasons", v => { f.reason = v; draw(); })]),
    el("span", { class: "count", id: "count" })], draw };
}

function bars(title, items) {
  const max = Math.max(1, ...items.map(i => i[1]));
  return el("div", { class: "bars" }, el("h2", { text: title }), items.map(([label, n]) =>
    el("div", { class: "barrow" }, el("span", { text: label }),
      el("div", { class: "bartrack" }, el("div", { class: "bar", style: "width:" + (100 * n / max).toFixed(1) + "%" })),
      el("span", { class: "num", text: String(n) }))));
}

function budgetLine() {
  const names = { jsearch: "JSearch", adzuna: "Adzuna" };
  const parts = Object.entries(data.api_budget || {}).map(([api, b]) => names[api] + " " + b.left + " of " + b.limit + " left this month");
  return el("div", { class: "note", id: "api-budget", text: parts.join(" · ") + " · Runs are manual: use Run pipeline now, or double-click Run Job Agent.command." });
}

function renderRuns() {
  const sum = o => Object.values(o || {}).reduce((a, b) => a + b, 0);
  const runs = data.runs;
  return { toolbar: [budgetLine()], draw() {
    if (!runs.length) return swapHolder(el("div", { class: "empty", text: "No runs recorded yet. Runs are manual: use Run pipeline now (top right)." }));
    const last = [...runs].reverse().find(r => !r.aborted && !r.dry_run) || runs[runs.length - 1];
    const v = last.verdicts || {};
    const table = el("div", { class: "tablewrap" }, el("table", {},
      el("thead", {}, el("tr", {}, ["When", "Jobs in", "After rules", "After stage 1", "Scored", "Stage 1 calls",
        "Stage 2 calls", "Cost", "Apply", "Stretch", "Skip", ""].map(h => el("th", { text: h })))),
      el("tbody", {}, [...runs].reverse().map(r => el("tr", {},
        [new Date(r.timestamp).toLocaleString(), r.jobs_in_total ?? sum(r.jobs_in), sum(r.after_rules),
          sum(r.after_stage1), sum(r.scored), r.stage1_calls, r.stage2_calls, "$" + (r.cost_usd || 0).toFixed(3),
          (r.verdicts || {}).apply, (r.verdicts || {}).stretch, (r.verdicts || {}).skip,
          r.aborted ? "aborted by spend guard" : r.dry_run ? "dry run" : ""].map((c, i) =>
          el("td", { class: i && i < 11 ? "num" : "" }, c === undefined ? "" : c)))))));
    swapHolder(el("div", {},
      bars("Latest run: " + new Date(last.timestamp).toLocaleString(), [
        ["Jobs in", last.jobs_in_total ?? sum(last.jobs_in)], ["After rules", sum(last.after_rules)],
        ["After stage 1", sum(last.after_stage1)], ["Scored", sum(last.scored)]]),
      bars("Verdicts (latest run)", [["Apply", v.apply || 0], ["Stretch", v.stretch || 0], ["Skip", v.skip || 0]]),
      bars("Jobs in by source", Object.entries(last.jobs_in || {})),
      el("h2", { text: "Past runs" }), table));
  } };
}

function statCard(n, label, bad) {
  return el("div", { class: "stat" + (bad ? " bad" : "") }, el("div", { class: "n", text: String(n) }),
    el("div", { class: "l", text: label }));
}
const pct = x => (x === null || x === undefined) ? "n/a" : (100 * x).toFixed(1) + "%";

function renderEval() {
  return { toolbar: [], async draw() {
    try { data.eval = await (await fetch("/api/eval")).json(); } catch (e) { say("Could not load eval."); return; }
    const m = data.eval, p = m.progress, a = m.agreement;
    const names = new Map([...data.jobs, ...data.filtered_out].map(j => [j.id, j.company + " — " + j.title]));
    const headline = a.rate === null
      ? el("div", {}, el("div", { class: "headline", text: "Label " + (p.target - p.labelled) + " more" }),
          el("div", { class: "l", text: "Agreement is shown once you have labelled " + p.target + " scored jobs." }))
      : el("div", {}, el("div", { class: "headline", text: pct(a.rate) }),
          el("div", { class: "l", text: "exact agreement (" + a.agree + " of " + a.total + ")" }));
    const prog = el("div", { class: "section" }, headline,
      el("div", { class: "progress" }, el("div", { style: "width:" + Math.min(100, 100 * p.labelled / p.target).toFixed(1) + "%" })),
      el("div", { class: "l", text: p.labelled + " / " + p.target + " scored jobs labelled" }));
    const cards = el("div", { class: "cards" },
      statCard(m.errors.agent_skip_but_i_said_yes, "Agent skip but I said yes (missed opportunities, worse)", true),
      statCard(m.errors.agent_apply_but_i_said_no, "Agent apply but I said no (wasted time)"));
    const matrix = el("div", { class: "tablewrap section" }, el("table", { class: "matrix" },
      el("thead", {}, el("tr", {}, ["Agent ↓ / You →", "yes", "maybe", "no"].map(h => el("th", { text: h })))),
      el("tbody", {}, Object.entries(m.confusion).map(([v, row]) =>
        el("tr", {}, el("td", { text: v }), el("td", { text: row.yes }), el("td", { text: row.maybe }), el("td", { text: row.no }))))));
    const reasons = Object.entries(m.disagreements_by_reason);
    const reasonBars = reasons.length ? bars("Disagreements by reason category", reasons)
      : el("div", { class: "empty", text: "No disagreements yet." });
    const dis = m.disagreements.length ? el("div", { class: "tablewrap section" }, el("table", {},
      el("thead", {}, el("tr", {}, ["Job", "Agent", "Score", "You", "Reason", "Note"].map(h => el("th", { text: h })))),
      el("tbody", {}, m.disagreements.map(d => el("tr", {}, el("td", { text: names.get(d.id) || d.id }),
        el("td", { text: d.agent_verdict }), el("td", { class: "num", text: d.score ?? "" }),
        el("td", { text: d.my_label }), el("td", { text: d.reason }), el("td", { text: d.note })))))) : "";
    const f = m.filtered;
    const fRows = Object.entries(f.by_reason);
    const fHead = f.false_drop_rate === null
      ? el("div", {}, el("div", { class: "headline", text: "Not enough reviewed" }),
          el("div", { class: "l", text: f.reviewed + " of " + f.min_reviewed + " reviewed so far (" + f.flagged + " flagged). Use Review sample on the Filtered out tab." }))
      : el("div", {}, el("div", { class: "headline", text: pct(f.false_drop_rate) }),
          el("div", { class: "l", text: "false drop rate: " + f.flagged + " flagged of " + f.reviewed + " reviewed" }));
    const filt = el("div", { class: "section" }, el("h2", { text: "False drops (filtered-out jobs)" }), fHead,
      el("div", { class: "l", text: "n reviewed = " + f.reviewed + ". Rate is flagged divided by reviewed, over the sampled PM-title or India-location drops." }),
      fRows.length ? el("div", { class: "tablewrap" }, el("table", {},
        el("thead", {}, el("tr", {}, ["Drop reason", "Flagged", "Reviewed", "False drop rate"].map(h => el("th", { text: h })))),
        el("tbody", {}, fRows.map(([c, r]) => el("tr", {}, el("td", { text: c }), el("td", { class: "num", text: r.flagged }),
          el("td", { class: "num", text: r.reviewed }), el("td", { class: "num", text: pct(r.false_drop_rate) })))))) : "");
    swapHolder(el("div", {}, prog, cards, el("h2", { text: "Confusion matrix" }), matrix, reasonBars,
      el("h2", { text: "Disagreements" }), dis, filt));
  } };
}

const TABS = { eval: renderEval, ready: renderReady, pipeline: renderPipeline, all: renderAll, jd: renderJD, filtered: renderFiltered, runs: renderRuns };
let current = null;

function showTab(name) {
  tab = name;
  for (const b of document.querySelectorAll("#tabs button")) b.setAttribute("aria-selected", String(b.dataset.tab === name));
  current = TABS[name]();
  panel.replaceChildren(el("div", { class: "toolbar" }, current.toolbar), holder());
  current.draw();
}
function refresh() { if (current) current.draw(); }

// ---- Run pipeline now: estimate first, confirm, run in the background, poll until done ----
const runBtn = document.getElementById("run-pipeline");
const runStatus = document.getElementById("run-status");
async function pipelinePost(path) {
  const res = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json", "X-Dashboard-Token": TOKEN },
    body: "{}" });
  let json = {};
  try { json = await res.json(); } catch (e) { /* non-JSON error body */ }
  return { ok: res.ok, json };
}
async function pollPipeline() {
  const res = await fetch("/api/pipeline/status", { headers: { "X-Dashboard-Token": TOKEN } });
  const st = await res.json();
  if (st.state === "running") { setTimeout(pollPipeline, 3000); return; }
  runBtn.disabled = false;
  runStatus.textContent = st.state === "done" ? (st.exit_code === 2 ? "Run stopped at the cost cap." : "Run finished.")
    : "Run failed; see data/clean/run_stats.jsonl.";
  await loadState(); renderStrip(); refresh();
}
runBtn.addEventListener("click", async () => {
  runBtn.disabled = true; runStatus.textContent = "Estimating...";
  const est = await pipelinePost("/api/pipeline/estimate");
  if (!est.ok) { runStatus.textContent = est.json.error || "Could not estimate."; runBtn.disabled = false; return; }
  const ok = window.confirm("Run scout and screen now?\n\nEstimated screening cost: $" + est.json.est_cost.toFixed(2)
    + "\nHard cap for this run: $" + est.json.max_cost.toFixed(2) + "\n\n" + est.json.note);
  if (!ok) { runStatus.textContent = ""; runBtn.disabled = false; return; }
  const run = await pipelinePost("/api/pipeline/run");
  if (!run.ok) { runStatus.textContent = run.json.error || "Could not start."; runBtn.disabled = false; return; }
  runStatus.textContent = "Running scout and screen. This takes a few minutes."; pollPipeline();
});

document.getElementById("tabs").addEventListener("click", e => {
  const b = e.target.closest("button[data-tab]");
  if (b) showTab(b.dataset.tab);
});

loadState().then(() => {
  renderStrip();
  if (data.demo) document.getElementById("demo-badge").hidden = false;
  showTab(tab);
}).catch(() => say("Could not load data. Is the server running?"));
