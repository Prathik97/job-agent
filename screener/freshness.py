"""Posting age from a job's posted_date. Unknown or unparseable dates give None (never penalised)."""
from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Optional

_DATE_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}")


def parse_posted_date(value: Optional[str]) -> Optional[date]:
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    if _DATE_PREFIX.match(text):
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None
    return None


def posting_age_days(posted_date: Optional[str], today: Optional[date] = None) -> Optional[int]:
    """Whole days since the posting date; a future date counts as 0. None if unknown."""
    posted = parse_posted_date(posted_date)
    if posted is None:
        return None
    today = today or datetime.now(timezone.utc).date()
    return max(0, (today - posted).days)
