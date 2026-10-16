"""The tailoring plan the model returns, and the deterministic checks on its structure."""
from __future__ import annotations

import re
from typing import Dict, List

from pydantic import BaseModel, Field

from tailor.facts import Facts

COVER_WORDS = (120, 150)
OUTREACH_MAX_WORDS = 69   # "under 70"


class BulletPlan(BaseModel):
    achievement_id: str
    text: str
    reword_why: str = ""          # required when text differs from the master wording


class RolePlan(BaseModel):
    id: str
    bullets: List[BulletPlan]


class TailorPlan(BaseModel):
    headline_key: str
    summary: List[str] = Field(min_length=1, max_length=3)
    experience: List[RolePlan]
    skills: Dict[str, List[str]]
    certifications: List[str] = Field(default_factory=list)
    jd_keywords: List[str] = Field(default_factory=list)
    selection_rationale: str = ""
    cover_note: str
    outreach: str


def words(text: str) -> int:
    return len(re.findall(r"\S+", text))


def achievements_by_id(master: dict) -> Dict[str, dict]:
    return {a["id"]: {**a, "role": e["id"]} for e in master["experience"] for a in e["achievements"]}


def structure_violations(plan: TailorPlan, facts: Facts) -> List[str]:
    """Selection and structure rules the code can verify exactly."""
    from tailor.factcheck import check_numbers   # local import: avoids a cycle at module load
    m, out = facts.master, []
    if plan.headline_key not in m["headline_variants"]:
        out.append(f"headline_key '{plan.headline_key}' is not one of {sorted(m['headline_variants'])}")
    ach = achievements_by_id(m)
    role_ids = [e["id"] for e in m["experience"]]
    if [r.id for r in plan.experience] != role_ids:
        out.append(f"experience must list every role once, in this order: {role_ids}")
    seen = set()
    for role in plan.experience:
        if not role.bullets:
            out.append(f"role {role.id} needs at least one bullet")
        for b in role.bullets:
            src = ach.get(b.achievement_id)
            if src is None or src["role"] != role.id:
                out.append(f"bullet '{b.achievement_id}' is not an achievement of role {role.id}")
                continue
            if b.achievement_id in seen:
                out.append(f"achievement '{b.achievement_id}' used twice")
            seen.add(b.achievement_id)
            allowed = facts.numbers_allowed_by(src["text"] + "\n" + "\n".join(src.get("metrics") or []))
            out += check_numbers(f"bullet {b.achievement_id}", b.text, facts, allowed)
            if b.text.strip() != src["text"].strip() and not b.reword_why.strip():
                out.append(f"bullet '{b.achievement_id}' is reworded but reword_why is empty")
    master_skills = {s.lower() for items in m["skills"].values() for s in items}
    for cat, items in plan.skills.items():
        for s in items:
            if s.lower() not in master_skills:
                out.append(f"skill '{s}' is not in master_profile.json skills")
    certs = set(m.get("certifications", []))
    out += [f"certification '{c}' is not in master_profile.json" for c in plan.certifications if c not in certs]
    cw, ow = words(plan.cover_note), words(plan.outreach)
    if not COVER_WORDS[0] <= cw <= COVER_WORDS[1]:
        out.append(f"cover_note is {cw} words; it must be {COVER_WORDS[0]} to {COVER_WORDS[1]}")
    if not 0 < ow <= OUTREACH_MAX_WORDS:
        out.append(f"outreach is {ow} words; it must be under 70")
    return out
