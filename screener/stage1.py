"""Layer 2: Haiku keep/drop on location and seniority for jobs the rules could not settle."""
from __future__ import annotations

from scout.schema import Job
from config import HAIKU, STAGE1_MAX_CHARS
from screener.llm import Usage, ask_json
from screener.models import RuleResult, Stage1Decision
from screener.text import truncate

VERSION = "s1v1"
MAX_OUT_TOKENS = 200

SYSTEM = """You are a fast first-pass filter for a job search. The candidate is a senior product manager \
(about 7 years of experience) who will only consider roles that are either (a) based in or hybrid in \
Bengaluru, India, or (b) fully remote AND open to people living in India.

Decide KEEP or DROP for the job. Judge only two things:
1. Location: drop if the job is tied to a country/region that excludes India-based hires, or requires \
work authorization or residency outside India. Keep if India-eligible or truly unclear.
2. Role and seniority: drop if it is not really a product management role (for example project or \
program management, sales, marketing, engineering) or is clearly junior (intern, associate/APM) or \
far too senior. A plain "Product Manager" title is fine unless the text shows it is junior.

When in doubt, KEEP. A later, more careful stage does the real scoring.
Reply with ONLY a JSON object: {"keep": true|false, "reason": "<one short sentence>"}"""


def build_user(job: Job, rule: RuleResult) -> str:
    years = rule.years_required if rule.years_required is not None else "not stated"
    return (
        f"Title: {job.title}\nCompany: {job.company}\nLocation: {job.location or 'not stated'}\n"
        f"Remote type: {job.remote_type}\n"
        f"Rules found: title tier={rule.tier}, years of experience required={years}, "
        f"flags={rule.flags or 'none'}\n\n"
        f"Description:\n{truncate(job.description, STAGE1_MAX_CHARS)}"
    )


def decide(client, job: Job, rule: RuleResult, usage: Usage) -> Stage1Decision:
    return ask_json(client, HAIKU, SYSTEM, build_user(job, rule),
                    Stage1Decision.model_validate, usage, MAX_OUT_TOKENS)
