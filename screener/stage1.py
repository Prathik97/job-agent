"""Layer 2: Haiku keep/drop on location and seniority for jobs the rules could not settle."""
from __future__ import annotations

from scout.schema import Job
from config import HAIKU, STAGE1_MAX_CHARS
from screener.llm import Usage, ask_json
from screener.models import RuleResult, Stage1Decision
from screener.text import sha, truncate

VERSION = "s1v2"
MAX_OUT_TOKENS = 200

SYSTEM = """You are a fast first-pass filter for a job search. The candidate lives in India and will only \
consider roles that are based in or hybrid in Bengaluru, India, or fully remote and open to people \
living in India.

Decide ONLY on two things: location and role type.

LOCATION
- Answer "drop" only if the posting EXPLICITLY places the job outside India with no India option \
(for example every listed location is a non-India city, or it says "Remote - US" or "US only"), or \
the text EXPLICITLY requires work authorization or residency outside India.
- NEVER infer anything from where the company is headquartered or from what companies "typically" do.
- If the location is unknown, missing, "N/A", or unclear, answer "keep". A bare "Remote" is "unsure".

ROLE TYPE
- Answer "drop" only if the job is clearly not a product, program or project management role \
(for example engineering, design, sales, marketing, accounting, support, research).
- NEVER drop because of years of experience required, seniority, or level (junior, senior, staff, \
director and VP are all fine). A later stage judges seniority.

Answers: "keep", "drop", or "unsure". "unsure" is treated as keep, so use it whenever you are not \
certain. Reply with ONLY a JSON object: {"decision": "keep"|"drop"|"unsure", "reason": "<one short sentence>"}"""

USER_TEMPLATE = """Title: {title}
Company: {company}
Location: {location}
Remote type: {remote_type}
Rules found: title tier={tier}, years of experience required={years}, flags={flags}

Description:
{description}"""

PROMPT_HASH = sha(SYSTEM + USER_TEMPLATE + str(STAGE1_MAX_CHARS))


def build_user(job: Job, rule: RuleResult) -> str:
    years = rule.years_required if rule.years_required is not None else "not stated"
    return USER_TEMPLATE.format(
        title=job.title, company=job.company, location=job.location or "not stated",
        remote_type=job.remote_type, tier=rule.tier, years=years,
        flags=rule.flags or "none", description=truncate(job.description, STAGE1_MAX_CHARS))


def decide(client, job: Job, rule: RuleResult, usage: Usage) -> Stage1Decision:
    return ask_json(client, HAIKU, SYSTEM, build_user(job, rule),
                    Stage1Decision.model_validate, usage, MAX_OUT_TOKENS)
