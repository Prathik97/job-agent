"""Pydantic models for screener output. All LLM output is validated through these."""
from __future__ import annotations

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field

Verdict = Literal["apply", "stretch", "skip"]


class RuleResult(BaseModel):
    status: Literal["drop", "stage1", "stage2"]
    drop_reason: Optional[str] = None
    tier: Optional[str] = None          # "A", "B", "C" or "untiered"
    years_required: Optional[int] = None
    flags: List[str] = Field(default_factory=list)

    @property
    def drop_category(self) -> str:
        return (self.drop_reason or "").split(":")[0]


class Stage1Decision(BaseModel):
    keep: bool
    reason: str = ""


class Stage2Raw(BaseModel):
    """What the model returns. The final score is computed in code, not trusted."""
    scores: Dict[str, float]
    reasons: List[str] = Field(default_factory=list)
    red_flags: List[str] = Field(default_factory=list)


class Stage2Result(BaseModel):
    score: int = Field(ge=0, le=100)
    subscores: Dict[str, int]
    reasons: List[str]
    red_flags: List[str]
    verdict: Verdict
