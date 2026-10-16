"""Layer 3: Sonnet scores each survivor out of 100 using the weights in profile.yaml.

The model returns per-category points; the total and the verdict are computed in code so
they stay deterministic and consistent with profile.yaml.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Dict

from scout.schema import Job
from config import SONNET, STAGE2_MAX_CHARS, STRETCH_MIN_SCORE
from screener.llm import Usage, ask_json
from screener.models import RuleResult, Stage2Raw, Stage2Result
from screener.text import sha, truncate

VERSION = "s2v1"
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
    system: str
    hash: str


def build_context(profile: dict) -> ScoringContext:
    weights = dict(profile["scoring"]["weights"])
    shortlist_min = profile["scoring"]["thresholds"]["shortlist_min_score"]
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
        'Reply with ONLY a JSON object: {"scores": {' + keys + '}, "reasons": ["..."], '
        '"red_flags": ["..."]}'
    )
    return ScoringContext(weights, shortlist_min, system, sha(system))


def build_user(job: Job, rule: RuleResult) -> str:
    years = rule.years_required if rule.years_required is not None else "not stated"
    return (
        f"Title: {job.title}\nCompany: {job.company}\nLocation: {job.location or 'not stated'}\n"
        f"Remote type: {job.remote_type}\nURL: {job.url}\n"
        f"Rules found: title tier={rule.tier}, years required={years}, flags={rule.flags or 'none'}\n\n"
        f"Description:\n{truncate(job.description, STAGE2_MAX_CHARS)}"
    )


def verdict_for(score: int, shortlist_min: int) -> str:
    if score >= shortlist_min:
        return "apply"
    return "stretch" if score >= STRETCH_MIN_SCORE else "skip"


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
                            verdict=verdict_for(total, ctx.shortlist_min))
    return validate


def score_job(client, job: Job, rule: RuleResult, ctx: ScoringContext, usage: Usage) -> Stage2Result:
    return ask_json(client, SONNET, ctx.system, build_user(job, rule),
                    make_validator(ctx), usage, MAX_OUT_TOKENS)
