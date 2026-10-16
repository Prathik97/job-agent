"""Tailor: build a per-job resume package from master_profile.json only.

    python -m tailor <job_id> [--jd-file full_jd.txt] [--max-cost USD] [--dry-run]

Writes output/resumes/<company>_<title>_<yyyymmdd>/ with resume.docx, resume.pdf, cover_note.md,
outreach.md, changes.md, jd_keywords.md (and screening_answers.md, jd_pasted.txt when relevant).
Nothing is written unless the final text passes the fact check.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

from config import MAX_COST_PER_RUN, OPUS, SONNET
from screener.llm import Usage, cost_usd, extract_json, make_client
from screener.text import est_tokens, truncate
from tailor import layout, render
from tailor.facts import Facts, word_in
from tailor.factcheck import Context, check_text
from tailor.plan import TailorPlan, achievements_by_id, structure_violations

log = logging.getLogger(__name__)
INDIA_PLACES = ("india", "bengaluru", "bangalore", "hyderabad", "mumbai", "delhi", "gurgaon", "gurugram",
                "noida", "pune", "chennai", "kolkata", "ahmedabad")
EST_OUT_TOKENS = 5500   # includes the model's thinking tokens
MAX_TOKENS = 5000
JD_MAX_CHARS = 8000

SYSTEM = """You tailor a resume for one job. You may use ONLY facts from the candidate's master profile, which is given as JSON.

Hard rules:
- Never invent or change a number, percentage, currency amount, tool, technology, company, job title, date, team size or education fact. Never combine numbers from different achievements.
- A tool or technology named in the job description may appear in your text only if it is in the profile's skills.tools or already in the profile text. Otherwise do not mention it.
- Choose headline_key from headline_variants (the headline text itself is filled in by code).
- Select and order achievements by relevance to the job using their tags and text. Keep every role, in the given order, with at least one bullet each. Use 3 to 6 bullets for the most recent roles and 1 to 3 for the earliest.
- A bullet's text is either the achievement text unchanged or lightly reworded to mirror the job's vocabulary while every claim stays true. If you reword, say why in reword_why. If unchanged, leave reword_why empty.
- summary: 2 or 3 short sentences made only from summary_facts.
- skills: choose and order items that appear in the profile's skills, grouped under the profile's category names.
- certifications: exact strings from the profile, only those relevant.
- jd_keywords: the 12 to 25 most important skills and keywords in the job description.
- cover_note: 120 to 150 words, plain first person, specific to this job and company, drawn from real profile facts, no generic flattery, no clichés like "I am excited" or "passionate".
- outreach: under 70 words, a short note the candidate sends to the hiring manager or recruiter, plain first person, specific, one concrete fact.
- Never use em dashes. Use commas, periods or hyphens.
Return ONLY a JSON object with these keys: headline_key, summary (list of strings), experience (list of {id, bullets: [{achievement_id, text, reword_why}]}), skills ({category: [items]}), certifications (list), jd_keywords (list), selection_rationale (one or two sentences), cover_note, outreach."""

USER = """Job: {title} at {company} ({location})

Job description:
{jd}

Candidate master profile (JSON):
{master}"""


class TailorError(Exception):
    pass


class PartialJDError(TailorError):
    """The job's description is truncated; the full JD must be pasted first."""


class CostGuardError(TailorError):
    pass


class FactCheckFailed(TailorError):
    def __init__(self, violations: List[str]):
        super().__init__("fact check failed: " + "; ".join(violations[:5]))
        self.violations = violations


@dataclass
class Prepared:
    job: dict
    jd: str
    jd_pasted: bool
    model: str
    india: bool
    facts: Facts
    master_view: dict
    system: str
    user: str
    est_cost: float          # one call
    score: Optional[int] = None


@dataclass
class Result:
    folder: Path
    model: str
    cost: float
    calls: int
    violations_fixed: List[str] = field(default_factory=list)


def is_india_job(location: str) -> bool:
    low = (location or "").lower()
    return any(re.search(rf"(?<![a-z]){p}(?![a-z])", low) for p in INDIA_PLACES)


def slug(text: str, limit: int = 40) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")[:limit] or "x"


def find_job(data_dir: Path, job_id: str) -> dict:
    try:
        rows = json.loads((data_dir / "clean" / "screened_jobs.json").read_text())
    except (OSError, ValueError) as exc:
        raise TailorError("no screened_jobs.json; run the pipeline first") from exc
    for row in rows:
        if row["id"] == job_id and row["screen"]["status"] == "scored":
            return row
    raise TailorError(f"job {job_id} is not a scored job")


def pick_model(score: Optional[int], profile: dict) -> str:
    top = profile.get("scoring", {}).get("thresholds", {}).get("top_pick_min_score", 85)
    return OPUS if score is not None and score >= top else SONNET


def prepare(job_id: str, *, data_dir: Path, master_path: Path, profile_path: Path,
            jd_override: Optional[str] = None, max_cost: float = MAX_COST_PER_RUN) -> Prepared:
    job = find_job(data_dir, job_id)
    if job.get("jd_partial") and not (jd_override or "").strip():
        raise PartialJDError("this job's description is partial (from an aggregator). Paste the full "
                             "job description and try again.")
    jd = (jd_override or job.get("description") or "").strip()
    facts = Facts.load(master_path)
    profile = yaml.safe_load(profile_path.read_text()) if profile_path.exists() else {}
    score = (job["screen"].get("stage2") or {}).get("score")
    model = pick_model(score, profile)
    india = is_india_job(job.get("location", ""))
    view = facts.localized_master(india)
    view.pop("identity", None)
    view.pop("guardrails", None)
    view.pop("equivalents", None)
    user = USER.format(title=job["title"], company=job["company"], location=job.get("location") or "not stated",
                       jd=truncate(jd, JD_MAX_CHARS), master=json.dumps(view, ensure_ascii=False, indent=1))
    est = cost_usd(model, est_tokens(SYSTEM + user), EST_OUT_TOKENS)
    return Prepared(job, jd, bool(jd_override), model, india, facts, view, SYSTEM, user, est, score)


def guard_cost(prep: Prepared, max_cost: float) -> None:
    """Worst case is the first call plus one retry (which also resends the first answer)."""
    in_tok = est_tokens(prep.system + prep.user)
    worst = prep.est_cost + cost_usd(prep.model, in_tok + EST_OUT_TOKENS + 300, EST_OUT_TOKENS)
    if worst > max_cost:
        raise CostGuardError(f"worst-case cost ${worst:.2f} is above the limit ${max_cost:.2f}; "
                             "raise it with --max-cost")


# ---------- building the documents from the plan ----------

def _title_case(key: str) -> str:
    return key.replace("_", " ").title().replace(" And ", " and ")


def build_sections(plan: TailorPlan, prep: Prepared) -> Tuple[str, str, List[Tuple[str, List[tuple]]]]:
    m = prep.master_view
    ident = prep.facts.master["identity"]
    contact = " | ".join(x for x in [ident.get("email"), ident.get("phone"), ident.get("linkedin"),
                                      ident.get("portfolio")] if x)
    ach = achievements_by_id(m)
    sections: Dict[str, List[tuple]] = {}
    sections["summary"] = [("para", m["headline_variants"][plan.headline_key])] + [("para", s) for s in plan.summary]
    exp_blocks: List[tuple] = []
    by_id = {e["id"]: e for e in m["experience"]}
    for role in plan.experience:
        e = by_id[role.id]
        exp_blocks.append(("role", f"{e['title']}, {e['company']}", f"{e['location']} | {e['start']} - {e['end']}"))
        exp_blocks += [("bullet", b.text.strip()) for b in role.bullets]
    sections["experience"] = exp_blocks
    sections["skills"] = [("para", f"{_title_case(cat)}: {', '.join(items)}") for cat, items in plan.skills.items() if items]
    ed = m["education"]
    sections["education"] = [("para", f"{ed['degree']}, {ed['institution']}"),
                             ("para", f"{ed['location']} | {ed['start']} - {ed['end']}")]
    sections["certifications"] = [("bullet", c) for c in plan.certifications]
    ordered = [(layout.HEADINGS[k], sections[k]) for k in layout.SECTION_ORDER if sections.get(k)]
    return ident["name"], contact, ordered


def resume_text(name: str, contact: str, sections) -> str:
    lines = [name, contact]
    for heading, blocks in sections:
        lines.append(heading)
        lines += [" ".join(b[1:]) for b in blocks]
    return "\n".join(lines)


def trim_to_fit(plan: TailorPlan, prep: Prepared) -> Tuple[bytes, float, int, List[str], Tuple]:
    """Render; if the PDF is over the page limit at the smallest font, drop the least relevant
    bullet (last in the most crowded role) and try again. Returns the dropped achievement ids."""
    dropped: List[str] = []
    while True:
        name, contact, sections = build_sections(plan, prep)
        pdf, size, pages = render.fit_pdf(name, contact, sections)
        if pages <= layout.MAX_PAGES:
            return pdf, size, pages, dropped, (name, contact, sections)
        crowded = max((r for r in plan.experience if len(r.bullets) > 1), key=lambda r: len(r.bullets), default=None)
        if crowded is None:
            raise TailorError(f"the resume is {pages} pages even after trimming; limit is {layout.MAX_PAGES}")
        dropped.append(crowded.bullets.pop().achievement_id)


def _has(keyword: str, text: str) -> bool:
    """Whole-word match, plus a word-start match for keywords of 5+ letters so that
    'roadmap' is backed by 'Roadmapping' and 'feature' by 'features'."""
    k = keyword.lower().strip()
    if word_in(k, text):
        return True
    return len(k) >= 5 and re.search(r"(?<![a-z0-9])" + re.escape(k), text) is not None


def keyword_support(keyword: str, master: dict) -> str:
    """Where master_profile.json backs a JD keyword, or '' if it does not."""
    for cat, items in master["skills"].items():
        if any(_has(keyword, i.lower()) for i in items):
            return f"skills.{cat}"
    for e in master["experience"]:
        for a in e["achievements"]:
            if any(_has(keyword, t.replace("_", " ")) for t in a.get("tags", [])):
                return f"tag on {a['id']}"
            if _has(keyword, a["text"].lower()):
                return f"text of {a['id']}"
    if any(_has(keyword, s.lower()) for s in master.get("summary_facts", [])):
        return "summary_facts"
    if any(_has(keyword, h.lower()) for h in master["headline_variants"].values()):
        return "headline_variants"
    return ""


def jd_keywords_md(plan: TailorPlan, master: dict) -> str:
    lines = ["# JD keywords and whether master_profile.json backs them", ""]
    backed = [(k, keyword_support(k, master)) for k in plan.jd_keywords]
    for k, where in backed:
        lines.append(f"- {k}: backed ({where})" if where else f"- {k}: NOT backed, so it is not claimed")
    lines += ["", f"Backed: {sum(1 for _, w in backed if w)} of {len(backed)}."]
    return "\n".join(lines) + "\n"


def changes_md(plan: TailorPlan, prep: Prepared, dropped: List[str]) -> str:
    master = prep.facts.master
    ach = achievements_by_id(prep.master_view)
    cur = "INR" if prep.india else "USD"
    L = ["# Changes versus master_profile.json", "",
         f"Model: {prep.model}. Currency shown for the ARR figure: {cur} ("
         f"{'India-based job' if prep.india else 'job outside India'}).", "",
         f"Headline: variant '{plan.headline_key}' used verbatim.", ""]
    if plan.selection_rationale:
        L += [f"Why this selection: {plan.selection_rationale}", ""]
    L += ["Summary lines (from summary_facts):"] + [f"- {s}" for s in plan.summary] + [""]
    for role in plan.experience:
        e = next(x for x in master["experience"] if x["id"] == role.id)
        master_order = [a["id"] for a in e["achievements"]]
        chosen = [b.achievement_id for b in role.bullets]
        L.append(f"## {e['title']}, {e['company']}")
        L.append(f"- Selected, in resume order: {', '.join(chosen)}")
        skipped = [i for i in master_order if i not in chosen]
        L.append(f"- Not used: {', '.join(skipped) if skipped else 'none'}")
        in_master_order = [i for i in master_order if i in chosen]
        L.append("- Order versus master: " + ("same" if in_master_order == chosen else "reordered by relevance to the JD"))
        for b in role.bullets:
            if b.text.strip() != ach[b.achievement_id]["text"].strip():
                L += [f"- Reworded {b.achievement_id}: {b.reword_why}", f"  - Before: {ach[b.achievement_id]['text']}",
                      f"  - After: {b.text.strip()}"]
        L.append("")
    if dropped:
        L += ["Dropped to fit 2 pages: " + ", ".join(dropped), ""]
    L += ["Skills: chosen and ordered from master skills; none added.", "",
          "Unchanged and copied from master: name, company names, job titles, dates, education, locations, every number."]
    return "\n".join(L) + "\n"


# ---------- the model call ----------

def _ask(client, prep: Prepared, usage: Usage, messages: list) -> str:
    resp = client.messages.create(model=prep.model, max_tokens=MAX_TOKENS, system=prep.system, messages=messages)
    usage.add(prep.model, resp)
    return "".join(b.text for b in resp.content if getattr(b, "type", "text") == "text")


def evaluate(text: str, prep: Prepared) -> Tuple[Optional[TailorPlan], List[str]]:
    """Parse the answer and run every check on the final text. Returns (plan, violations)."""
    try:
        plan = TailorPlan.model_validate(extract_json(text))
    except ValueError as exc:
        return None, [f"the answer was not valid JSON for the schema: {str(exc)[:200]}"]
    violations = structure_violations(plan, prep.facts)
    try:
        name, contact, sections = build_sections(plan, prep)
    except KeyError as exc:
        return None, violations + [f"unknown id in plan: {exc}"]
    ctx = Context(company=prep.job["company"], title=prep.job["title"], jd_text=prep.jd)
    texts = {"resume": resume_text(name, contact, sections), "cover_note": plan.cover_note, "outreach": plan.outreach}
    return plan, list(dict.fromkeys(violations + check_text(texts, prep.facts, ctx)))


def generate(client, prep: Prepared, usage: Usage) -> Tuple[TailorPlan, List[str]]:
    """One attempt plus one retry with the violations listed. Raises FactCheckFailed."""
    messages = [{"role": "user", "content": prep.user}]
    answer = _ask(client, prep, usage, messages)
    plan, violations = evaluate(answer, prep)
    if not violations:
        return plan, []
    log.warning("tailor: %d violation(s), retrying once", len(violations))
    messages += [{"role": "assistant", "content": answer},
                 {"role": "user", "content": "Your answer failed these checks:\n" + "\n".join(f"- {v}" for v in violations)
                  + "\nFix every one without adding any new fact, and return the complete JSON object again."}]
    plan, again = evaluate(_ask(client, prep, usage, messages), prep)
    if again:
        raise FactCheckFailed(again)
    return plan, violations


# ---------- the public entry point ----------

def write_package(plan: TailorPlan, prep: Prepared, out_root: Path, today: date,
                  extra_files: Optional[Dict[str, str]] = None) -> Tuple[Path, List[str]]:
    pdf, size, pages, dropped, (name, contact, sections) = trim_to_fit(plan, prep)
    if pages > layout.MAX_PAGES:
        raise TailorError("resume is over the page limit")
    if dropped:  # trimmed bullets change the text: re-run the fact check is unnecessary (subset), keep the list
        log.info("dropped bullets to fit: %s", dropped)
    docx = render.build_docx(name, contact, sections, size)
    folder = out_root / f"{slug(prep.job['company'])}_{slug(prep.job['title'])}_{today:%Y%m%d}"
    out_root.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".tmp-", dir=out_root))
    try:
        files = {"cover_note.md": plan.cover_note.strip() + "\n", "outreach.md": plan.outreach.strip() + "\n",
                 "changes.md": changes_md(plan, prep, dropped), "jd_keywords.md": jd_keywords_md(plan, prep.facts.master),
                 **(extra_files or {})}
        for fname, content in files.items():
            (tmp / fname).write_text(content)
        (tmp / "resume.pdf").write_bytes(pdf)
        (tmp / "resume.docx").write_bytes(docx)
        if prep.jd_pasted:
            (tmp / "jd_pasted.txt").write_text(prep.jd)
        if folder.exists():
            shutil.rmtree(folder)
        os.replace(tmp, folder)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return folder, dropped


def tailor_job(job_id: str, *, data_dir: Path = Path("data"), master_path: Path = Path("master_profile.json"),
               profile_path: Path = Path("profile.yaml"), out_root: Path = Path("output/resumes"),
               jd_override: Optional[str] = None, max_cost: float = MAX_COST_PER_RUN, client=None,
               today: Optional[date] = None, extra_files: Optional[Dict[str, str]] = None) -> Result:
    prep = prepare(job_id, data_dir=data_dir, master_path=master_path, profile_path=profile_path,
                   jd_override=jd_override, max_cost=max_cost)
    log.info("estimated cost for one call: $%.3f (%s)", prep.est_cost, prep.model)
    guard_cost(prep, max_cost)
    usage = Usage()
    plan, fixed = generate(client or make_client(), prep, usage)
    folder, _ = write_package(plan, prep, out_root, today or date.today(), extra_files)
    return Result(folder, prep.model, usage.cost(), sum(usage.calls.values()), fixed)
