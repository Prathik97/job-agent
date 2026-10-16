"""Cross-source matching: when an aggregator lists a job an ATS scout already found, keep the
ATS version and note the aggregator. Matching is exact on normalized company and title plus a
compatible location. No fuzzy matching."""
from __future__ import annotations

import re
from typing import Dict, List, Tuple

from scout.schema import Job
from screener.titles import normalize_title

ATS_SOURCES = ("greenhouse", "lever", "ashby")
AGGREGATOR_SOURCES = ("jsearch", "adzuna")
SOURCE_RANK = {**{s: 0 for s in ATS_SOURCES}, "jsearch": 1, "adzuna": 2}

COMPANY_SUFFIXES = {"inc", "pvt", "ltd", "technologies", "labs", "india", "private", "limited"}
_PLACEHOLDERS = {"", "n a", "na", "none", "null", "unknown", "tbd"}
_LOCATION_FILLER = {"remote", "hybrid", "onsite", "on", "site", "or", "and", "anywhere", "wfh"}
CITY_ALIASES = {"bangalore": "bengaluru", "bengaluru": "bengaluru", "gurgaon": "gurugram",
                "bombay": "mumbai", "new delhi": "delhi", "nyc": "new york"}
# Region tokens (countries, states) are not cities: two "Bengaluru, India" / "Mumbai, India"
# jobs share a country but are different jobs.
REGIONS = {"india", "usa", "us", "united states", "uk", "united kingdom", "canada", "germany",
           "ireland", "singapore", "australia", "karnataka", "maharashtra", "telangana",
           "tamil nadu", "haryana", "uttar pradesh", "kerala", "gujarat", "west bengal",
           "ca", "ny", "tx", "wa", "ma", "il"}


def _words(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def normalize_company(name: str) -> str:
    """'Meesho Pvt Ltd' -> 'meesho'. Only trailing suffix words are stripped."""
    tokens = _words(name).split()
    while len(tokens) > 1 and tokens[-1] in COMPANY_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def title_key(title: str) -> str:
    return _words(normalize_title(title))


def _location_tokens(location: str) -> Tuple[set, set]:
    """(cities, regions) named in a location string."""
    cities, regions = set(), set()
    for part in re.split(r"[,;/|()]|\s-\s|\bor\b", location or "", flags=re.I):
        tok = " ".join(w for w in _words(part).split() if w not in _LOCATION_FILLER)
        if tok in _PLACEHOLDERS:
            continue
        tok = CITY_ALIASES.get(tok, tok)
        (regions if tok in REGIONS else cities).add(tok)
    return cities, regions


def locations_compatible(a: str, b: str) -> bool:
    """Same city, or same country/region when a side names no city. Unknown or plain
    "Remote" on either side is compatible with anything."""
    ca, ra = _location_tokens(a)
    cb, rb = _location_tokens(b)
    if not (ca | ra) or not (cb | rb):
        return True
    if ca and cb:
        return bool(ca & cb)
    # at most one side names a city: compare regions; a side with no region is unknown
    if not ra or not rb:
        return True
    return bool(ra & rb)


def merge_sources(jobs: List[Job]) -> List[Job]:
    """Collapse the same job listed by several sources. Highest-priority source wins
    (ATS, then jsearch, then adzuna); the others are recorded in `also_listed_on`.
    Jobs from the same source are never merged with each other."""
    kept: Dict[Tuple[str, str], List[Job]] = {}
    out: List[Job] = []
    for job in sorted(jobs, key=lambda j: SOURCE_RANK.get(j.source, 0)):
        key = (normalize_company(job.company), title_key(job.title))
        match = next((k for k in kept.get(key, [])
                      if SOURCE_RANK.get(k.source, 0) < SOURCE_RANK.get(job.source, 0)
                      and locations_compatible(k.location, job.location)), None)
        if match is not None:
            if job.source not in match.also_listed_on:
                match.also_listed_on.append(job.source)
            for s in job.also_listed_on:
                if s not in match.also_listed_on and s != match.source:
                    match.also_listed_on.append(s)
            continue
        kept.setdefault(key, []).append(job)
        out.append(job)
    return out
