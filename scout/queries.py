"""Build aggregator search queries from profile.yaml tier A and B titles.

Titles are reduced to role stems ("Senior Product Manager" and "Product Manager 2" both become
"product manager") so a handful of queries cover all tiers; the rules layer then assigns tiers.
Each stem is searched for Bengaluru and for remote India. A run only uses a few queries
(chosen by a rotating cursor) so the monthly free-plan budget lasts.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Tuple

from screener.titles import normalize_title

SENIORITY_WORDS = {"senior", "junior", "lead", "staff", "principal", "group", "associate"}
LOCATIONS = ("bengaluru", "remote_india")


@dataclass(frozen=True)
class Query:
    stem: str
    location: str  # "bengaluru" or "remote_india"


def role_stems(profile: dict) -> List[str]:
    titles = profile.get("titles", {})
    stems: List[str] = []
    for key in ("tier_a", "tier_b"):
        for title in titles.get(key, []):
            words = [w for w in re.sub(r"[^a-z0-9 ]", " ", normalize_title(title).lower()).split()
                     if w not in SENIORITY_WORDS and not w.isdigit() and w != "avp"]
            stem = " ".join(words)
            # one-word stems are too broad; abbreviations like "pm" and "spm" are one word
            if len(stem.split()) > 1 and stem not in stems:
                stems.append(stem)
    # A stem that contains another stem is already covered by it ("technical program manager"
    # is covered by "program manager").
    def covered(s: str) -> bool:
        return any(o != s and re.search(rf"\b{re.escape(o)}\b", s) for o in stems)
    return [s for s in stems if not covered(s)]


def build_queries(profile: dict) -> List[Query]:
    """Bengaluru and remote-India queries interleaved so any prefix covers both."""
    stems = role_stems(profile)
    return [Query(s, loc) for s in stems for loc in LOCATIONS]


def select_queries(queries: List[Query], count: int, cursor: int) -> Tuple[List[Query], int]:
    """Pick `count` queries starting at the cursor (wrapping). Returns (picked, new_cursor)."""
    if not queries or count <= 0:
        return [], cursor
    count = min(count, len(queries))
    start = cursor % len(queries)
    picked = [queries[(start + i) % len(queries)] for i in range(count)]
    return picked, (start + count) % len(queries)
