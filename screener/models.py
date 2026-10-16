"""Pydantic models for screener output. All LLM output is validated through these."""
from __future__ import annotations

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field, computed_field

Verdict = Literal["apply", "stretch", "skip"]
RoleFamily = Literal["product", "tech_program", "software_project", "physical_ops", "other"]
LocationStatus = Literal["confirmed", "unknown"]


class RuleResult(BaseModel):
    status: Literal["drop", "stage1", "stage2"]
    drop_reason: Optional[str] = None
    tier: Optional[str] = None          # "A", "B", "C" or "untiered"
    years_required: Optional[int] = None
    location_status: LocationStatus = "unknown"
    flags: List[str] = Field(default_factory=list)

    @property
    def drop_category(self) -> str:
        return (self.drop_reason or "").split(":")[0]


class Stage1Decision(BaseModel):
    decision: Literal["keep", "drop", "unsure"]
    reason: str = ""

    @computed_field  # type: ignore[misc]
    @property
    def keep(self) -> bool:
        """'unsure' counts as keep: only an explicit drop removes a job."""
        return self.decision != "drop"


class Stage2Raw(BaseModel):
    """What the model returns. The final score is computed in code, not trusted."""
    role_family: RoleFamily
    scores: Dict[str, float]
    reasons: List[str] = Field(default_factory=list)
    red_flags: List[str] = Field(default_factory=list)


class Stage2Result(BaseModel):
    score: int = Field(ge=0, le=100)
    subscores: Dict[str, int]
    role_family: RoleFamily
    reasons: List[str]
    red_flags: List[str]
    verdict: Verdict  # as scored; run_screener applies the caps (see stage2.apply_caps)
