"""Layer 3: Sonnet scores each survivor out of 100 using the weights in profile.yaml.

The model returns per-category points; the total and the verdict are computed in code so
they stay deterministic and consistent with profile.yaml.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Dict, Optional

from scout.schema import Job
from config import (CAPPED_ROLE_FAMILIES, SONNET, STAGE2_MAX_CHARS, STALE_NOTE_AFTER_DAYS,
                    STALE_SKIP_AFTER_DAYS)
from screener.llm import Usage, ask_json, cost_usd
from screener.models import RuleResult, Stage2Raw, Stage2Result
from screener.text import est_tokens, sha, truncate

VERSION = "s2v3"
MAX_OUT_TOKENS = 700
EST_OUT_TOKENS = 350   # typical answer length, for cost estimates only

RUBRIC_HELP = {
    "role_and_title_fit": "how closely the role and title match the target titles",
    "domain_fit": "how well the company's domain matches the ranked target domains",
    "seniority_fit": "fit between the JD's seniority and years required and the candidate's experience",
    "location_fit": "fit with the location rules (Bengaluru onsite/hybrid, or remote open to India)",
    "company_and_growth": "company stage, quality and growth signal versus preferred stages",
    "comp_signal": "any compensation signal versus the floor; use about half the points if unstated",
}


@dataclass
class ScoringContext:
    weights: Dict[str, int]
    shortlist_min: int
    stretch_min: int
    system: str
    hash: str  # covers the system prompt, user template and truncation limit
    stale_note_days: int = STALE_NOTE_AFTER_DAYS
    stale_skip_days: int = STALE_SKIP_AFTER_DAYS


def build_context(profile: dict) -> ScoringContext:
    weights = dict(profile["scoring"]["weights"])
    thresholds = profile["scoring"]["thresholds"]
    shortlist_min, stretch_min = thresholds["shortlist_min_score"], thresholds["stretch_min_score"]
    summary = {
        "years_experience": profile["candidate"]["years_experience"],
        "target_titles": profile["titles"],
        "domains_ranked": profile["domains_ranked"],
        "location_rules": {k: profile["location"][k] for k in (
            "onsite_or_hybrid_cities", "remote_allowed", "remote_must_be_india_eligible")},
        "experience_rules": profile["experience"],
        "compensation_floor_lpa_inr": profile["compensation"]["floor_lpa_inr"],
        "preferred_company_stages": profile["company_preferences"]["stage"],
        "deal_breakers": [d for d in profile["deal_breakers"] if not d.startswith("TODO")],
    }
    rubric = "\n".join(
        f"- {k} (0 to {w}): {RUBRIC_HELP.get(k, k.replace('_', ' '))}" for k, w in weights.items())
    keys = ", ".join(f'"{k}": <int>' for k in weights)
    system = (
        "You score job postings for a senior product manager. Candidate and preferences:\n"
        f"{json.dumps(summary, indent=2)}\n\nScore the job in these categories, using whole "
        f"numbers within each range:\n{rubric}\n\nBe calibrated and strict; a perfect match is rare. "
        "Base everything on the posting text. List 2 to 5 concrete reasons and any red flags "
        "(for example location limits, junior or too-senior scope, sales-in-disguise).\n"
        "Seniority: for product-manager titles with a tier of A, B or C, missing years of experience is "
        "NOT a red flag; use the tier to judge seniority_fit (A and B are good fits, C is a stretch "
        "upward). For Project Manager and Program Manager titles, give NO seniority credit for the "
        "title tier alone; judge seniority_fit only from the JD (years, scope, team size, ownership).\n"
        "Also classify role_family as exactly one of: product (product management), tech_program "
        "(technical program management for software or technology), software_project (project "
        "management of software or IT delivery), physical_ops (physical infrastructure, warehouses, "
        "construction, facilities, hardware or logistics operations), other (anything else).\n"
        'Reply with ONLY a JSON object: {"role_family": "<one of the five>", "scores": {' + keys + '}, "reasons": ["..."], '
        '"red_flags": ["..."]}'
    )
    fresh = profile.get("freshness") or {}
    return ScoringContext(weights, shortlist_min, stretch_min, system,
                          sha(system + USER_TEMPLATE + str(STAGE2_MAX_CHARS)),
                          int(fresh.get("stale_note_after_days", STALE_NOTE_AFTER_DAYS)),
                          int(fresh.get("stale_skip_after_days", STALE_SKIP_AFTER_DAYS)))


USER_TEMPLATE = """Title: {title}
Company: {company}
Location: {location}
Remote type: {remote_type}
URL: {url}
Rules found: title tier={tier}, years required={years}, flags={flags}

Description:
{description}"""


def build_user(job: Job, rule: RuleResult) -> str:
    years = rule.years_required if rule.years_required is not None else "not stated"
    return USER_TEMPLATE.format(
        title=job.title, company=job.company, location=job.location or "not stated",
        remote_type=job.remote_type, url=job.url, tier=rule.tier, years=years,
        flags=rule.flags or "none", description=truncate(job.description, STAGE2_MAX_CHARS))


def verdict_for(score: int, shortlist_min: int, stretch_min: int) -> str:
    if score >= shortlist_min:
        return "apply"
    return "stretch" if score >= stretch_min else "skip"


def make_validator(ctx: ScoringContext):
    def validate(data: dict) -> Stage2Result:
        raw = Stage2Raw.model_validate(data)
        if set(raw.scores) != set(ctx.weights):
            raise ValueError(f"score keys {sorted(raw.scores)} != {sorted(ctx.weights)}")
        sub: Dict[str, int] = {}
        for key, weight in ctx.weights.items():
            val = raw.scores[key]
            if not 0 <= val <= weight:
                raise ValueError(f"{key}={val} outside 0..{weight}")
            sub[key] = round(val)
        total = sum(sub.values())
        return Stage2Result(score=total, subscores=sub, role_family=raw.role_family, reasons=raw.reasons,
                            red_flags=raw.red_flags,
                            verdict=verdict_for(total, ctx.shortlist_min, ctx.stretch_min))
    return validate


def est_cost(ctx: ScoringContext, job: Job, rule: RuleResult) -> float:
    """Estimated USD for one Stage 2 call. The pipeline funnel and the dashboard both use it."""
    return cost_usd(SONNET, est_tokens(ctx.system + build_user(job, rule)), EST_OUT_TOKENS)


def score_job(client, job: Job, rule: RuleResult, ctx: ScoringContext, usage: Usage,
              quiet: bool = False) -> Stage2Result:
    """`quiet` swaps pydantic's detailed error (which can quote model output) for a fixed message,
    so a retry warning in the logs never carries any posting text."""
    validate = make_validator(ctx)
    if quiet:
        inner = validate

        def validate(data: dict) -> Stage2Result:
            try:
                return inner(data)
            except ValueError:
                raise ValueError("model output did not match the schema") from None
    return ask_json(client, SONNET, ctx.system, build_user(job, rule), validate, usage, MAX_OUT_TOKENS)


CAPPED_FAMILIES = CAPPED_ROLE_FAMILIES


PARTIAL_JD_NOTE = "partial JD, open the link and read it before applying"


STALE_NOTE = "possibly stale, check the listing is still open"
STALE_REASON = "stale posting"


def apply_caps(entry: dict, rule: RuleResult, jd_partial: bool = False,
               also_listed_on: tuple = (), age_days: Optional[int] = None,
               stale_note_days: int = STALE_NOTE_AFTER_DAYS,
               stale_skip_days: int = STALE_SKIP_AFTER_DAYS) -> dict:
    """Final verdict from a cached (uncapped) stage 2 entry plus rule facts.

    Caps are applied here, not before caching, so changing a rule never needs a re-score.
    Returns a copy with `verdict` (capped), `uncapped_verdict` and `notes`.
    """
    out = dict(entry)
    out["uncapped_verdict"] = entry["verdict"]
    notes = []
    family = entry.get("role_family")
    if family in CAPPED_FAMILIES:
        notes.append(f"verdict capped at stretch: role_family={family}")
        if out["verdict"] == "apply":
            out["verdict"] = "stretch"
    if rule.location_status != "confirmed":
        notes.append("verify location")
        if out["verdict"] == "apply":
            out["verdict"] = "stretch"
    if jd_partial:
        notes.append(PARTIAL_JD_NOTE)
        if out["verdict"] == "apply":
            out["verdict"] = "stretch"
    if age_days is not None:   # unknown age is never penalised
        if age_days > stale_skip_days:
            notes.append(f"{STALE_REASON}: posted {age_days} days ago")
            out["verdict"] = "skip"
        elif age_days > stale_note_days:
            notes.append(f"{STALE_NOTE} (posted {age_days} days ago)")
            if out["verdict"] == "apply":
                out["verdict"] = "stretch"
    if also_listed_on:
        notes.append("also listed on " + ", ".join(also_listed_on))
    out["notes"] = notes
    return out
