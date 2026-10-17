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


def hours_old_at_first_seen(posted_date: Optional[str], first_seen_at: Optional[str]) -> Optional[int]:
    """Whole hours between the posting and the agent first seeing it. A date-only posted_date
    counts as midnight UTC. None if either side is unknown; a first sighting before the posting
    (clock skew, timezone) counts as 0."""
    seen = parse_moment(first_seen_at)
    posted = parse_moment(posted_date)
    if seen is None or posted is None:
        return None
    return max(0, int((seen - posted).total_seconds() // 3600))


def parse_moment(value: Optional[str]) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        day = parse_posted_date(value)
        if day is None:
            return None
        moment = datetime(day.year, day.month, day.day)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
