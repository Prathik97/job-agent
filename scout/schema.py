"""Unified job schema shared by every scout and the normalizer."""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, model_validator

RemoteType = Literal["remote", "hybrid", "onsite", "unknown"]


def make_job_id(company: str, title: str, location: str) -> str:
    """Stable id: hash of company + title + location (case/space insensitive)."""
    key = "|".join(" ".join(p.lower().split()) for p in (company, title, location))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Job(BaseModel):
    id: str
    source: str
    title: str
    company: str
    location: str = ""
    remote_type: RemoteType = "unknown"
    url: str
    description: str = ""
    posted_date: Optional[str] = None
    fetched_at: str = Field(default_factory=utcnow_iso)
    first_seen_at: Optional[str] = None  # when the agent first saw this job; carried across runs
    jd_partial: bool = False  # aggregator descriptions are often truncated
    also_listed_on: List[str] = Field(default_factory=list)  # other sources that listed this job

    @model_validator(mode="after")
    def _default_first_seen(self) -> "Job":
        if not self.first_seen_at:
            self.first_seen_at = self.fetched_at
        return self

    @classmethod
    def build(cls, *, source: str, title: str, company: str, location: str,
              url: str, **kwargs) -> "Job":
        return cls(id=make_job_id(company, title, location), source=source,
                   title=title.strip(), company=company, location=location.strip(),
                   url=url, **kwargs)
