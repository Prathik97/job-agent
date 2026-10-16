"""Score a pasted job description (the dashboard's "Score a JD" tab).

The pasted text goes through the same path as a pipeline job: the Rules, then Stage 2 with the same
prompt, weights, thresholds, caps and cache keys. Stage 1 is skipped. A job the rules would drop is
still scored, with the reason shown as a warning. The text is never fetched from a URL, never logged,
and is stored only under data/ (the draft store, the screener cache) and output/ (the Tailor folder).

Imported lazily by dashboard.serve, so the dashboard itself stays standard library only.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml
from pydantic import BaseModel, Field

from config import HAIKU, MAX_COST_PER_RUN, STAGE2_MAX_CHARS
from dashboard.data import safe_url
from dashboard.service import ServiceError
from dashboard.store import atomic_write
from scout.schema import Job
from screener import run_screener, stage2
from screener.cache import Cache, cache_key
from screener.llm import ScreenError, Usage, ask_json, cost_usd, make_client
from screener.models import RuleResult, Stage2Result
from screener.rules import Rules, extract_years
from screener.text import est_tokens, sha, truncate

log = logging.getLogger(__name__)

MAX_JD_CHARS = 100_000
MAX_FIELD_CHARS = 200
MAX_DRAFTS = 25
EXTRACT_VERSION = "jx2"
EXTRACT_MAX_OUT = 600
MAX_KEYWORDS = 25
PLACEHOLDER_TITLE, PLACEHOLDER_COMPANY = "Senior Product Manager", "Example Company"

EXTRACT_SYSTEM = """You read one job posting and extract basics. Reply with ONLY a JSON object:
{"title": "<job title>", "company": "<hiring company>", "location": "<location as written, or empty>", \
"keywords": ["..."]}
Rules: use only what the posting says; if the title, company or location is not stated, use an empty \
string. keywords: the 12 to 25 most important skills, tools, domains and responsibilities named in the \
posting, each ONE atomic term of 1 to 3 words, as a candidate would write it on a resume (for example \
"roadmapping", "A/B testing", "RAG", "stakeholder management"). Never join several skills into one \
item and never add filler words. Most important first."""


class ExtractedJD(BaseModel):
    title: str = ""
    company: str = ""
    location: str = ""
    keywords: List[str] = Field(default_factory=list)


def clean_keywords(words: List[str]) -> List[str]:
    seen, out = set(), []
    for w in words:
        w = " ".join(str(w).split())
        if w and len(w) <= 60 and w.lower() not in seen:
            seen.add(w.lower())
            out.append(w)
    return out[:MAX_KEYWORDS]


def _validate_extract(data: dict) -> ExtractedJD:
    try:
        ext = ExtractedJD.model_validate(data)
    except ValueError:   # fixed message: pydantic's own error could quote the model's output
        raise ValueError("extraction did not match the schema") from None
    return ext.model_copy(update={"keywords": clean_keywords(ext.keywords)})


@dataclass
class JDInput:
    jd: str
    company: str = ""
    title: str = ""
    location: str = ""
    url: str = ""

    @classmethod
    def from_body(cls, body: dict) -> "JDInput":
        def field(name: str) -> str:
            value = body.get(name)
            if value is None:
                return ""
            if not isinstance(value, str):
                raise ServiceError(400, f"{name} must be text")
            value = " ".join(value.split())
            if len(value) > MAX_FIELD_CHARS:
                raise ServiceError(400, f"{name} is longer than {MAX_FIELD_CHARS} characters")
            return value

        jd = body.get("jd")
        if not isinstance(jd, str) or not jd.strip():
            raise ServiceError(400, "Paste the full job description first.")
        if len(jd) > MAX_JD_CHARS:
            raise ServiceError(400, f"The job description is longer than {MAX_JD_CHARS} characters.")
        url = field("url")
        if url and not safe_url(url):
            raise ServiceError(400, "The source URL must start with http:// or https://")
        return cls(jd=jd.strip(), company=field("company"), title=field("title"),
                   location=field("location"), url=url)


# ---------- rule results and plain-language explanations ----------

def scoring_rule(rules: Rules, job: Job, rule: RuleResult) -> RuleResult:
    """The RuleResult Stage 2 is shown. A job the rules would drop still needs a tier and a years
    figure for the prompt, so they are recomputed from the same functions the rules use."""
    if rule.status != "drop":
        return rule
    return RuleResult(status="stage2", tier=rule.tier or rules.title_tier(job.title) or "untiered",
                      years_required=rule.years_required if rule.years_required is not None
                      else extract_years(job.description),
                      location_status=rules.location_status(job))


def drop_text(rule: RuleResult, rules: Rules) -> str:
    cat, _, detail = (rule.drop_reason or "").partition(":")
    if cat == "title_reject":
        return f"Title contains the reject keyword '{detail}'."
    if cat == "title_untiered":
        return "Title is not one of your target titles and does not look like a product, program or project role."
    if cat == "title_blocklist":
        return "Title looks like another function (engineering, design, sales, marketing, support...)."
    if cat == "location_reject":
        return f"The text says '{detail}', which your location rules reject."
    if cat == "years_over_hard_limit":
        return f"The JD asks for {rule.years_required} years, above your hard limit of {rules.hard_years}."
    if cat == "location_non_india_only":
        return "The location names only places outside India."
    if cat == "location_not_eligible":
        return "The location is not Bengaluru and not fully remote."
    return f"The rules would drop this job ({rule.drop_reason})."


def flag_text(flag: str, rule: RuleResult, rules: Rules) -> str:
    return {
        "stretch_years": f"The JD asks for {rule.years_required} years, above {rules.stretch_years}: a stretch.",
        "below_range": f"The JD asks for {rule.years_required} years, below your usual range.",
        "location_ambiguous": "Location is unclear. The pipeline would run Stage 1 here; it is skipped for a pasted JD.",
        "untiered": "Title is not a listed tier. The pipeline would run Stage 1 here; it is skipped for a pasted JD.",
    }.get(flag, flag)


def label(key: str) -> str:
    return key.replace("_", " ").capitalize()


def top_gaps(entry: dict, weights: Dict[str, int], n: int = 3) -> List[str]:
    """The model's red flags first; if there are fewer than n, the weakest categories (under 60%)."""
    gaps = list(entry.get("red_flags") or [])[:n]
    for key in sorted(weights, key=lambda k: entry["subscores"][k] / weights[k]):
        if len(gaps) >= n:
            break
        if entry["subscores"][key] / weights[key] < 0.6:
            gaps.append(f"Weak {label(key).lower()}: {entry['subscores'][key]} of {weights[key]}")
    return gaps


def jd_match(keywords: List[str], master: dict) -> List[dict]:
    """Each keyword marked backed or not by master_profile.json, with the same logic as jd_keywords.md."""
    from tailor.tailor import keyword_support
    return [{"keyword": k, "where": (where := keyword_support(k, master)), "backed": bool(where)}
            for k in keywords]


# ---------- drafts: what "Tailor resume" and "Add to pipeline" act on ----------

class DraftStore:
    """The last few scored pasted JDs (screened_jobs.json-shaped rows), so the actions never trust
    the browser for job data. Lives in data/clean/, which is gitignored."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def _load(self) -> Dict[str, dict]:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def get(self, job_id: str) -> Optional[dict]:
        return (self._load().get(job_id) or {}).get("record")

    def put(self, job_id: str, record: dict, saved_at: str) -> None:
        with self._lock:
            data = self._load()
            data.pop(job_id, None)
            data[job_id] = {"record": record, "saved_at": saved_at}
            while len(data) > MAX_DRAFTS:
                data.pop(next(iter(data)))
            atomic_write(self.path, data)


# ---------- the service ----------

class JDScorer:
    demo = False

    def __init__(self, data_dir: Path, output_dir: Path, master_path: Path, profile_path: Path,
                 tailor_service=None, client=None, max_cost: float = MAX_COST_PER_RUN):
        from dashboard.service import TailorService
        self.data_dir, self.output_dir = data_dir, output_dir
        self.master_path, self.profile_path = master_path, profile_path
        self.tailor_service = tailor_service or TailorService(data_dir, output_dir, master_path, profile_path)
        self.drafts = DraftStore(data_dir / "clean" / "jd_drafts.json")
        self.screened_path = data_dir / "clean" / "screened_jobs.json"
        self._client, self.max_cost = client, max_cost
        self._write_lock = threading.Lock()

    # -- setup --
    def _rules_ctx(self) -> Tuple[Rules, stage2.ScoringContext]:
        try:
            profile = yaml.safe_load(self.profile_path.read_text())
            return Rules.from_profile(profile), stage2.build_context(profile)
        except (OSError, yaml.YAMLError, KeyError, TypeError) as exc:
            raise ServiceError(400, f"Could not read {self.profile_path.name}: {exc.__class__.__name__}")

    def _cache(self) -> Cache:
        return Cache(self.data_dir / "clean" / "screener_cache.json")

    def _master(self) -> Optional[dict]:
        from tailor.facts import Facts
        try:
            return Facts.load(self.master_path).master
        except (OSError, ValueError, KeyError):
            return None

    def _model_client(self):
        if self._client is None:
            self._client = make_client()
        return self._client

    # -- model calls (overridden by the demo service) --
    def _extract_cost(self, jd: str) -> float:
        return cost_usd(HAIKU, est_tokens(EXTRACT_SYSTEM + truncate(jd, STAGE2_MAX_CHARS)), EXTRACT_MAX_OUT)

    def _extract_call(self, jd: str, usage: Usage) -> ExtractedJD:
        return ask_json(self._model_client(), HAIKU, EXTRACT_SYSTEM, truncate(jd, STAGE2_MAX_CHARS),
                        _validate_extract, usage, EXTRACT_MAX_OUT)

    def _score_cost(self, ctx, job: Job, rule: RuleResult) -> float:
        return stage2.est_cost(ctx, job, rule)

    def _score_call(self, job: Job, rule: RuleResult, ctx, usage: Usage):
        return stage2.score_job(self._model_client(), job, rule, ctx, usage, quiet=True)

    # -- helpers --
    @staticmethod
    def _extract_key(jd: str) -> str:
        return cache_key("jdextract", HAIKU, EXTRACT_VERSION, "", jd, sha(EXTRACT_SYSTEM))

    def _cached_extraction(self, cache: Cache, jd: str) -> Optional[ExtractedJD]:
        entry = cache.get(self._extract_key(jd))
        return ExtractedJD.model_validate(entry) if entry and not entry.get("failed") else None

    @staticmethod
    def _fields(inp: JDInput, ext: Optional[ExtractedJD]) -> Tuple[str, str, str]:
        return (inp.title or (ext.title if ext else ""), inp.company or (ext.company if ext else ""),
                inp.location or (ext.location if ext else ""))

    @staticmethod
    def _job(inp: JDInput, title: str, company: str, location: str) -> Job:
        return Job.build(source="manual", title=title, company=company, location=location,
                         url=safe_url(inp.url), description=inp.jd)

    # -- API --
    def estimate(self, body: dict) -> dict:
        inp = JDInput.from_body(body)
        rules, ctx = self._rules_ctx()
        cache = self._cache()
        ext = self._cached_extraction(cache, inp.jd)
        title, company, location = self._fields(inp, ext)
        job = self._job(inp, title or PLACEHOLDER_TITLE, company or PLACEHOLDER_COMPANY, location)
        rule = scoring_rule(rules, job, rules.evaluate(job))
        # The id (and so the Stage 2 cache entry) is only known once every field is resolved.
        known = bool(title and company and (inp.location or ext is not None))
        score_cached = known and cache.get(run_screener.key2(job, ctx)) is not None
        extract_cost = 0.0 if ext is not None else self._extract_cost(inp.jd)
        score_cost = 0.0 if score_cached else self._score_cost(ctx, job, rule)
        total = extract_cost + score_cost
        return {"est_cost": round(total, 4), "extract_cost": round(extract_cost, 4),
                "score_cost": round(score_cost, 4), "cached": total == 0.0,
                "max_cost": self.max_cost, "over_limit": total > self.max_cost,
                "truncated": len(inp.jd) > STAGE2_MAX_CHARS, "limit_chars": STAGE2_MAX_CHARS}

    def score(self, body: dict) -> dict:
        est = self.estimate(body)
        if est["over_limit"]:
            raise ServiceError(400, f"Estimated cost ${est['est_cost']:.2f} is above the limit "
                                    f"${est['max_cost']:.2f}. Nothing was sent to the model.")
        inp = JDInput.from_body(body)
        rules, ctx = self._rules_ctx()
        cache, usage = self._cache(), Usage()
        ext = self._extract(cache, inp.jd, usage)
        title, company, location = self._fields(inp, ext)
        if not title or not company:
            missing = " and ".join(n for n, v in (("title", title), ("company", company)) if not v)
            raise ServiceError(422, f"Could not find the {missing} in the JD. Enter it in the form and score again.")
        job = self._job(inp, title, company, location)
        rule = rules.evaluate(job)
        srule = scoring_rule(rules, job, rule)
        entry = run_screener._cached_or_call(cache, run_screener.key2(job, ctx), True,
                                             lambda: self._score_call(job, srule, ctx, usage))
        if entry is None:
            raise ServiceError(502, "The model API returned an error. Nothing was saved; try again.")
        if entry.get("failed"):
            raise ServiceError(422, "The model returned invalid JSON twice, so there is no score. Try again.")
        capped = stage2.apply_caps(entry, srule, jd_partial=False, also_listed_on=(), age_days=None,
                                   stale_note_days=ctx.stale_note_days, stale_skip_days=ctx.stale_skip_days)
        record = run_screener._record(job, srule, "scored", None, capped)
        self.drafts.put(job.id, record, record["screen"]["screened_at"])
        log.info("scored a pasted JD: id=%s score=%d cost=$%.4f", job.id, capped["score"], usage.cost())
        return self._result(job, inp, ext, rule, srule, rules, ctx, capped, usage, est)

    def _extract(self, cache: Cache, jd: str, usage: Usage) -> Optional[ExtractedJD]:
        entry = run_screener._cached_or_call(cache, self._extract_key(jd), True,
                                             lambda: self._extract_call(jd, usage))
        if entry is None:
            raise ServiceError(502, "The model API returned an error. Nothing was saved; try again.")
        return None if entry.get("failed") else ExtractedJD.model_validate(entry)

    def _result(self, job: Job, inp: JDInput, ext: Optional[ExtractedJD], rule: RuleResult,
                srule: RuleResult, rules: Rules, ctx, capped: dict, usage: Usage, est: dict) -> dict:
        master = self._master()
        if ext is None:
            match = {"available": False, "reason": "Keyword extraction failed; score again to retry."}
        elif master is None:
            match = {"available": False, "reason": f"{self.master_path.name} not found."}
        else:
            items = jd_match(ext.keywords, master)
            match = {"available": True, "items": items, "backed": sum(1 for i in items if i["backed"])}
        warnings = [{"code": rule.drop_category, "text": drop_text(rule, rules)}] if rule.status == "drop" else []
        info = [] if rule.status == "drop" else [flag_text(f, rule, rules) for f in rule.flags]
        return {
            "id": job.id, "company": job.company, "title": job.title, "location": job.location,
            "extracted": {"title": not inp.title, "company": not inp.company,
                          "location": not inp.location and bool(job.location)},
            "score": capped["score"], "verdict": capped["verdict"], "uncapped_verdict": capped["uncapped_verdict"],
            "role_family": capped["role_family"], "tier": srule.tier, "years_required": srule.years_required,
            "subscores": [{"key": k, "label": label(k), "points": capped["subscores"][k], "max": w}
                          for k, w in ctx.weights.items()],
            "thresholds": {"apply": ctx.shortlist_min, "stretch": ctx.stretch_min},
            "caps": list(capped["notes"]), "warnings": warnings, "info": info,
            "reasons": list(capped["reasons"])[:3], "gaps": top_gaps(capped, ctx.weights),
            "jd_match": match, "cost": round(usage.cost(), 4), "truncated": est["truncated"],
            "limit_chars": est["limit_chars"], "demo": self.demo,
        }

    # -- actions on a scored draft --
    def _draft(self, job_id: object) -> dict:
        record = self.drafts.get(job_id) if isinstance(job_id, str) else None
        if record is None:
            raise ServiceError(404, "Score this JD first (the scored draft was not found).")
        return record

    def tailor_estimate(self, body: dict) -> dict:
        record = self._draft(body.get("job_id"))
        return self.tailor_service.estimate(record["id"], record["description"], record)

    def tailor_run(self, body: dict) -> dict:
        record = self._draft(body.get("job_id"))
        return self.tailor_service.run(record["id"], record["description"], record)

    def add(self, body: dict) -> dict:
        """Save the scored job to screened_jobs.json with source "manual". Idempotent, and never
        replaces a scored pipeline row or a corrupt file."""
        record = self._draft(body.get("job_id"))
        with self._write_lock:
            try:
                existing = {r["id"]: r for r in json.loads(self.screened_path.read_text())} \
                    if self.screened_path.exists() else {}
            except (ValueError, KeyError, TypeError):
                raise ServiceError(500, f"{self.screened_path.name} is not valid JSON; not overwriting it.")
            current = existing.get(record["id"])
            if current is not None and (current.get("screen") or {}).get("status") == "scored":
                return {"added": False, "already_in_pipeline": True, "source": current.get("source", "")}
            run_screener.merge_output(self.screened_path, {record["id"]: record})
        return {"added": True, "already_in_pipeline": False, "source": "manual"}


# ---------- demo mode ----------

DEMO_VOCAB = ["Roadmapping", "Prioritisation", "A/B testing", "PRDs", "Stakeholder management", "Jira", "Figma",
              "Mixpanel", "Funnel analysis", "Retention", "Kubernetes", "Machine learning", "GraphQL", "Salesforce",
              "Payments", "B2B SaaS", "Data pipelines"]


class DemoJDScorer(JDScorer):
    """Same code path with the model calls replaced by deterministic stand-ins (no API, $0). The rules
    and the keyword check are the real ones, run against profile.example.yaml and
    master_profile.example.json, so the real data and the real profile are never read."""
    demo = True

    def __init__(self, data_dir: Path, output_dir: Path, repo_root: Path):
        from dashboard.service import DemoService
        super().__init__(data_dir, output_dir, repo_root / "master_profile.example.json",
                         repo_root / "profile.example.yaml", tailor_service=DemoService(data_dir, output_dir))

    def _extract_cost(self, jd: str) -> float:
        return 0.0

    def _score_cost(self, ctx, job: Job, rule: RuleResult) -> float:
        return 0.0

    def _extract_call(self, jd: str, usage: Usage) -> ExtractedJD:
        lines = [ln.strip() for ln in jd.splitlines() if ln.strip()]
        company = re.search(r"\b(?:at|About|join)\s+([A-Z][\w&-]*(?:[ \t]+[A-Z][\w&-]*){0,2})", jd)
        place = re.search(r"\b(Bengaluru|Bangalore|Remote[^\n,.;]*|Hyderabad|Mumbai|Delhi)", jd)
        return ExtractedJD(title=lines[0][:80] if lines else "", company=company.group(1) if company else "",
                           location=place.group(1).strip() if place else "",
                           keywords=clean_keywords([w for w in DEMO_VOCAB if w.lower() in jd.lower()]))

    def _score_call(self, job: Job, rule: RuleResult, ctx, usage: Usage) -> Stage2Result:
        bits = int(sha(job.description), 16)
        sub = {k: round(w * (0.55 + ((bits >> (5 * i)) % 40) / 100)) for i, (k, w) in enumerate(ctx.weights.items())}
        total = sum(sub.values())
        return Stage2Result(
            score=total, subscores=sub, role_family="product", verdict=stage2.verdict_for(total, ctx.shortlist_min, ctx.stretch_min),
            reasons=["DEMO, no model was called: title matches a target tier", "DEMO: scope looks like owned roadmap work",
                     "DEMO: stand-in text, scores are pseudo-random from the pasted text"],
            red_flags=["DEMO: sample red flag, real runs list real gaps from the JD"])
