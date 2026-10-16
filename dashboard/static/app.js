"use strict";
// All job text is untrusted. It is only ever put into the page with textContent / createTextNode,
// and URLs only become links if they are http or https.

const STATUSES = ["new", "shortlisted", "applied", "interview", "offer", "rejected", "withdrawn", "skipped"];
const PIPELINE = ["applied", "interview", "offer", "rejected", "withdrawn"];
const TOKEN = document.querySelector('meta[name="dashboard-token"]').content;
const panel = document.getElementById("panel");
const banner = document.getElementById("banner");

let data = null;
let tab = "ready";
const ui = {
  sort: { ready: ["score", -1], pipeline: ["applied_at", -1], all: ["score", -1], filtered: ["company", 1] },
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

async function save(job, body) {
  const res = await fetch("/api/applications/" + encodeURIComponent(job.id), {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Dashboard-Token": TOKEN },
    body: JSON.stringify(body),
  });
  if (res.status === 403) { say("Session expired. Reload this page."); throw new Error("forbidden"); }
  if (!res.ok) { say("Save failed (" + res.status + ")"); throw new Error("save"); }
  const entry = await res.json();
  job.status = entry.status; job.applied_at = entry.applied_at; job.app_notes = entry.notes;
  say("");
  recount();
}

// ---- derived numbers ----
function recount() {
  const rows = data.jobs, c = data.counts;
  const n = (...s) => rows.filter(r => s.includes(r.status)).length;
  c.ready = rows.filter(r => ["apply", "stretch"].includes(r.verdict) &&
    ["new", "shortlisted"].includes(r.status) && !r.orphaned).length;
  c.applied = n("applied"); c.interview = n("interview"); c.rejected = n("rejected"); c.skipped = n("skipped");
  renderStrip();
}

function renderStrip() {
  const c = data.counts;
  const stat = (n, l, small) => el("div", { class: "stat" + (small ? " small" : "") },
    el("div", { class: "n", text: n }), el("div", { class: "l", text: l }));
  const when = c.last_run ? new Date(c.last_run).toLocaleString() : "never";
  document.getElementById("strip").replaceChildren(
    stat(c.ready, "Ready to apply"), stat(c.applied, "Applied"), stat(c.interview, "Interviewing"),
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

const COLS = {
  company: text("company", "Company"), title: text("title", "Title"), location: text("location", "Location"),
  source: { key: "source", label: "Source", value: r => r.source || "", render: sourceCell },
  score: { key: "score", label: "Score", num: true, value: r => r.score, render: scoreCell },
  verdict: { key: "verdict", label: "Verdict", value: r => r.verdict, render: verdictChip },
  notes: { key: "notes", label: "Notes", sortable: false, value: r => r.notes.join("; "), render: notesCell },
  posted: { key: "posted_date", label: "Posted", value: r => (r.posted_date || "").slice(0, 10) },
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
  const rows = () => data.jobs.filter(r => ["apply", "stretch"].includes(r.verdict) &&
    ["new", "shortlisted"].includes(r.status) && !r.orphaned);
  const cols = [COLS.company, COLS.title, COLS.location, COLS.source, COLS.score, COLS.verdict, COLS.notes,
    COLS.posted, COLS.open, COLS.status, COLS.mark];
  return { toolbar: [el("span", { class: "count", id: "count" })],
    draw() { const r = rows(); document.getElementById("count").textContent = r.length + " jobs";
      swapHolder(buildTable("ready", cols, r, refresh)); } };
}

function renderPipeline() {
  const rows = () => data.jobs.filter(r => PIPELINE.includes(r.status));
  const cols = [COLS.company, COLS.title, COLS.status, COLS.applied, COLS.followUp, COLS.myNotes,
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
    COLS.status, COLS.notes, COLS.posted, COLS.open];
  const search = el("input", { type: "search", placeholder: "Search company, title, location", value: f.q,
    "aria-label": "Search" });
  const upd = () => draw();
  search.addEventListener("input", () => { f.q = search.value; upd(); });
  function draw() {
    const q = f.q.trim().toLowerCase();
    const rows = data.jobs.filter(r => (!f.verdict || r.verdict === f.verdict) && (!f.status || r.status === f.status)
      && (!f.source || r.source === f.source) && (!f.tier || r.tier === f.tier)
      && (!q || [r.company, r.title, r.location].join(" ").toLowerCase().includes(q)));
    document.getElementById("count").textContent = rows.length + " of " + data.jobs.length;
    swapHolder(buildTable("all", cols, rows, draw));
  }
  return { toolbar: [search,
    select(["apply", "stretch", "skip"], f.verdict, "All verdicts", v => { f.verdict = v; upd(); }),
    select(STATUSES, f.status, "All statuses", v => { f.status = v; upd(); }),
    select(uniq("source"), f.source, "All sources", v => { f.source = v; upd(); }),
    select(uniq("tier"), f.tier, "All tiers", v => { f.tier = v; upd(); }),
    el("span", { class: "count", id: "count" })], draw };
}

function renderFiltered() {
  const f = ui.filters.filtered;
  const category = r => String(r.reason || "").split(":")[0];
  const reasons = [...new Set(data.filtered_out.map(category))].sort();
  const cols = [text("company", "Company"), text("title", "Title"), text("location", "Location"),
    text("reason", "Reason")];
  const search = el("input", { type: "search", placeholder: "Search company, title, location", value: f.q,
    "aria-label": "Search" });
  search.addEventListener("input", () => { f.q = search.value; draw(); });
  function draw() {
    const q = f.q.trim().toLowerCase();
    const rows = data.filtered_out.filter(r => (!f.reason || category(r) === f.reason)
      && (!q || [r.company, r.title, r.location, r.reason].join(" ").toLowerCase().includes(q)));
    document.getElementById("count").textContent = rows.length + " of " + data.filtered_out.length;
    swapHolder(buildTable("filtered", cols, rows, draw));
  }
  return { toolbar: [search, select(reasons, f.reason, "All reasons", v => { f.reason = v; draw(); }),
    el("span", { class: "count", id: "count" })], draw };
}

function bars(title, items) {
  const max = Math.max(1, ...items.map(i => i[1]));
  return el("div", { class: "bars" }, el("h2", { text: title }), items.map(([label, n]) =>
    el("div", { class: "barrow" }, el("span", { text: label }),
      el("div", { class: "bartrack" }, el("div", { class: "bar", style: "width:" + (100 * n / max).toFixed(1) + "%" })),
      el("span", { class: "num", text: String(n) }))));
}

function renderRuns() {
  const sum = o => Object.values(o || {}).reduce((a, b) => a + b, 0);
  const runs = data.runs;
  return { toolbar: [], draw() {
    if (!runs.length) return swapHolder(el("div", { class: "empty", text: "No runs recorded yet." }));
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

const TABS = { ready: renderReady, pipeline: renderPipeline, all: renderAll, filtered: renderFiltered, runs: renderRuns };
let current = null;

function showTab(name) {
  tab = name;
  for (const b of document.querySelectorAll("#tabs button")) b.setAttribute("aria-selected", String(b.dataset.tab === name));
  current = TABS[name]();
  panel.replaceChildren(el("div", { class: "toolbar" }, current.toolbar), holder());
  current.draw();
}
function refresh() { if (current) current.draw(); }

document.getElementById("tabs").addEventListener("click", e => {
  const b = e.target.closest("button[data-tab]");
  if (b) showTab(b.dataset.tab);
});

loadState().then(() => {
  renderStrip();
  if (data.demo) document.getElementById("demo-badge").hidden = false;
  showTab(tab);
}).catch(() => say("Could not load data. Is the server running?"));
