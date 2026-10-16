"""Layer 3: Sonnet scores each survivor out of 100 using the weights in profile.yaml.

The model returns per-category points; the total and the verdict are computed in code so
they stay deterministic and consistent with profile.yaml.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Dict

from scout.schema import Job
from config import SONNET, STAGE2_MAX_CHARS
from screener.llm import Usage, ask_json
from screener.models import RuleResult, Stage2Raw, Stage2Result
from screener.text import sha, truncate

VERSION = "s2v2"
MAX_OUT_TOKENS = 700

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
        "Seniority: if the posting states no years of experience, do NOT list that as a red flag when "
        "the rules found a title tier of A, B or C; use the title tier to judge seniority_fit "
        "(A and B are good fits, C is a stretch upward). Only mention missing years for untiered titles.\n"
        'Reply with ONLY a JSON object: {"scores": {' + keys + '}, "reasons": ["..."], '
        '"red_flags": ["..."]}'
    )
    return ScoringContext(weights, shortlist_min, stretch_min, system,
                          sha(system + USER_TEMPLATE + str(STAGE2_MAX_CHARS)))


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
        return Stage2Result(score=total, subscores=sub, reasons=raw.reasons,
                            red_flags=raw.red_flags,
                            verdict=verdict_for(total, ctx.shortlist_min, ctx.stretch_min))
    return validate


def score_job(client, job: Job, rule: RuleResult, ctx: ScoringContext, usage: Usage) -> Stage2Result:
    return ask_json(client, SONNET, ctx.system, build_user(job, rule),
                    make_validator(ctx), usage, MAX_OUT_TOKENS)
