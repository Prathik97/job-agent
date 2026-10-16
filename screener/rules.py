"""Layer 1: pure-Python rules. No API calls.

Drops rejects, tags the title tier, extracts years-of-experience, flags location
problems, and routes each job to stage 1 (ambiguous) or straight to stage 2.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from scout.schema import Job
from screener.models import RuleResult
from screener.titles import normalize_title

# Reject keywords that only apply when the title has no "product" word, so
# "Product Manager, Salesforce Integrations" or "Product Manager, Sales Tools" survive.
CONDITIONAL_REJECTS = {"sales", "account manager"}

# Untiered titles only reach stage 1 if they look like a PM-type role. STRONG forms are
# unambiguous; WEAK forms ("product lead", "lead product ...") also match design/accounting
# titles, so the blocklist is applied to them.
PM_STRONG = [r"(?:product|program|project)s?\s+managers?", r"tpm"]
PM_WEAK = [r"product\s+leads?", r"lead\s+products?", r"head\s+of\s+products?",
           r"(?:director|vp|svp|avp|vice\s+president)\W+(?:of\W+)?products?",
           r"product\s+owners?"]
# Titles from other functions. Phrases are whole-word and plural-tolerant.
BLOCKLIST = ["engineering manager", "engineer", "designer", "sales", "account executive",
             "accountant", "accounting", "support specialist", "support operations",
             "support engineer", "product support", "recruiter", "marketing"]

_WORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen "
    "fourteen fifteen sixteen seventeen eighteen nineteen twenty".split())}
_NUM = r"(\d{1,2}|" + "|".join(_WORDS) + r")"
_YRS = r"\s*\+?\s*(?:years?|yrs?)\b"
_RANGE = re.compile(_NUM + r"\s*(?:-|–|—|to)\s*" + _NUM + _YRS, re.I)
_SINGLE = re.compile(_NUM + _YRS, re.I)
# Known non-India places. A location made up only of these (plus "remote"/"hybrid" filler)
# is dropped; anything unrecognised is left alone because we cannot be sure it is non-India.
NON_INDIA_PLACES = """
us, usa, u.s., u.s.a., united states, america, north america, americas, canada, uk, u.k.,
united kingdom, england, britain, ireland, germany, france, spain, italy, netherlands, belgium,
sweden, norway, denmark, finland, poland, portugal, switzerland, austria, europe, emea, eu, latam,
brazil, mexico, argentina, colombia, chile, singapore, japan, australia, new zealand, china,
hong kong, korea, israel, uae, dubai, philippines, indonesia, new york, nyc, ny, new-york,
san francisco, sf, bay area, sea, seattle, chicago, chi, atlanta, boston, austin, denver, miami,
dallas, houston, los angeles, la, washington, dc, portland, san jose, palo alto, mountain view,
south san francisco, toronto, vancouver, montreal, ottawa, dublin, london, paris, berlin, munich,
amsterdam, madrid, barcelona, lisbon, stockholm, copenhagen, zurich, warsaw, tokyo, sydney,
melbourne, auckland, mexico city, cdmx, sao paulo, buenos aires, bogota, santiago, tel aviv,
ca, wa, tx, il, ma, ga, co, fl, nj, va
"""
_FILLER = ("remote", "hybrid", "onsite", "on site", "hq", "headquarters", "office", "or", "and",
           "based", "in", "the", "of", "area", "metro", "region", "within", "multiple", "city")


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


_PLACES = sorted({_norm(p) for p in NON_INDIA_PLACES.replace("\n", " ").split(",") if p.strip()},
                 key=len, reverse=True)
_PLACE_RE = re.compile(r"(?<![a-z0-9])(?:" + "|".join(re.escape(p) for p in _PLACES) + r")(?![a-z0-9])")
_FILLER_RE = re.compile(r"(?<![a-z0-9])(?:" + "|".join(re.escape(_norm(f)) for f in _FILLER) + r")(?![a-z0-9])")


def names_only_non_india(location: str) -> bool:
    """True if the location names at least one known non-India place and nothing else
    (beyond words like 'remote'/'hybrid'). A bare 'Remote' or unknown place returns False."""
    low = _norm(location)
    if not _PLACE_RE.search(low):
        return False
    rest = _FILLER_RE.sub(" ", _PLACE_RE.sub(" ", low))
    return not re.search(r"[a-z0-9]", rest)


_PLACEHOLDER_LOCATIONS = {"n/a", "na", "none", "null", "unknown", "tbd", "-", "--"}
_REMOTE = re.compile(r"\b(remote|anywhere|worldwide|global|distributed)\b", re.I)


def phrase_re(phrase: str) -> re.Pattern:
    """Whole-word, case-insensitive phrase match. Spaces match any whitespace and each
    alphabetic word may be plural ("Product" also matches "Products")."""
    def word(w: str) -> str:
        return re.escape(w) + ("s?" if w[-1].isalpha() else "")
    body = r"\s+".join(word(p) for p in phrase.split())
    return re.compile(r"(?<![A-Za-z0-9])" + body + r"(?![A-Za-z0-9])", re.I)


def _raw_re(pattern: str) -> re.Pattern:
    return re.compile(r"(?<![A-Za-z0-9])(?:" + pattern + r")(?![A-Za-z0-9])", re.I)


_STRONG_RE = [_raw_re(p) for p in PM_STRONG]
_WEAK_RE = [_raw_re(p) for p in PM_WEAK]
_BLOCK_RE = [phrase_re(b) for b in BLOCKLIST]


def pm_role_kind(title: str) -> Optional[str]:
    """'strong', 'weak' or None: does the title look like a PM-type role?"""
    if any(r.search(title) for r in _STRONG_RE):
        return "strong"
    if any(r.search(title) for r in _WEAK_RE):
        return "weak"
    return None


def _to_int(token: str) -> int:
    token = token.lower()
    return _WORDS[token] if token in _WORDS else int(token)


def extract_years(text: str) -> Optional[int]:
    """Highest years-of-experience requirement in the text; a range counts its upper end.

    Only numbers near the word 'experience' count, so "founded 12 years ago" is ignored.
    """
    text = text or ""
    spans: List[Tuple[int, int]] = []
    values: List[int] = []

    def near_experience(start: int, end: int) -> bool:
        return "experience" in text[max(0, start - 60): end + 80].lower()

    for m in _RANGE.finditer(text):
        spans.append(m.span())
        if near_experience(*m.span()):
            values.append(max(_to_int(m.group(1)), _to_int(m.group(2))))
    for m in _SINGLE.finditer(text):
        if any(s <= m.start() < e for s, e in spans):
            continue
        if near_experience(*m.span()):
            values.append(_to_int(m.group(1)))
    values = [v for v in values if 0 < v <= 40]
    return max(values) if values else None


@dataclass
class Rules:
    tiers: Dict[str, List[re.Pattern]]
    rejects: List[Tuple[str, re.Pattern]]
    reject_phrases: List[Tuple[str, re.Pattern]]
    cities: List[str]
    remote_allowed: bool
    remote_must_be_india: bool
    min_years: int
    stretch_years: int
    hard_years: int

    @classmethod
    def from_profile(cls, profile: dict) -> "Rules":
        titles, exp, loc = profile["titles"], profile["experience"], profile["location"]
        return cls(
            tiers={t: [phrase_re(normalize_title(x)) for x in titles.get(f"tier_{t.lower()}", [])]
                   for t in "ABC"},
            rejects=[(k, phrase_re(normalize_title(k))) for k in profile.get("title_reject_keywords", [])],
            reject_phrases=[(k, phrase_re(k)) for k in loc.get("reject_if_text_contains", [])],
            cities=[c.lower() for c in loc.get("onsite_or_hybrid_cities", [])],
            remote_allowed=bool(loc.get("remote_allowed", True)),
            remote_must_be_india=bool(loc.get("remote_must_be_india_eligible", True)),
            min_years=exp["min_required_in_jd"],
            stretch_years=exp["stretch_if_jd_requires_over"],
            hard_years=exp["hard_reject_if_jd_requires_over"],
        )

    def title_reject(self, title: str) -> Optional[str]:
        title = normalize_title(title)
        has_product = bool(phrase_re("product").search(title))
        for kw, pat in self.rejects:
            if kw.lower() in CONDITIONAL_REJECTS and has_product:
                continue
            if pat.search(title):
                return kw
        return None

    def title_tier(self, title: str) -> Optional[str]:
        title = normalize_title(title)
        for tier in "ABC":
            if any(p.search(title) for p in self.tiers[tier]):
                return tier
        return None

    def location_class(self, job: Job) -> str:
        """'ok', 'ambiguous' (needs stage 1), 'non_india' or 'bad' (both dropped)."""
        loc = job.location or ""
        if loc.strip().lower() in _PLACEHOLDER_LOCATIONS:
            loc = ""  # unknown location: let stage 1 judge from the description
        low = loc.lower()
        if any(c in low for c in self.cities):
            return "ok"
        remote = job.remote_type == "remote" or bool(_REMOTE.search(loc)) or not loc.strip()
        if remote:
            if not self.remote_allowed:
                return "bad"
            if not self.remote_must_be_india or "india" in low:
                return "ok"
            if names_only_non_india(loc) and not self._india_signal(job.title):
                return "non_india"
            return "ambiguous"
        if low.strip() == "india":
            return "ambiguous"
        return "bad"

    def _india_signal(self, text: str) -> bool:
        low = (text or "").lower()
        return "india" in low or any(c in low for c in self.cities)

    def location_status(self, job: Job) -> str:
        """'confirmed' if the location or description names Bengaluru/Bangalore/India."""
        return "confirmed" if self._india_signal(f"{job.location}\n{job.description}") else "unknown"

    def evaluate(self, job: Job) -> RuleResult:
        def drop(reason: str, **kw) -> RuleResult:
            return RuleResult(status="drop", drop_reason=reason, **kw)

        title = normalize_title(job.title)
        kw = self.title_reject(title)
        if kw:
            return drop(f"title_reject:{kw}")

        tier = self.title_tier(title)
        if tier is None:
            kind = pm_role_kind(title)
            if kind is None:
                return drop("title_untiered")
            if kind == "weak" and any(b.search(title) for b in _BLOCK_RE):
                return drop("title_blocklist")
            tier = "untiered"

        text = f"{job.location}\n{job.description}"
        for phrase, pat in self.reject_phrases:
            if pat.search(text):
                return drop(f"location_reject:{phrase}", tier=tier)

        flags: List[str] = []
        years = extract_years(job.description)
        if years is not None:
            if years > self.hard_years:
                return drop("years_over_hard_limit", tier=tier, years_required=years)
            if years > self.stretch_years:
                flags.append("stretch_years")
            if years < self.min_years:
                flags.append("below_range")

        loc_class = self.location_class(job)
        if loc_class == "non_india":
            return drop("location_non_india_only", tier=tier, years_required=years)
        if loc_class == "bad":
            return drop("location_not_eligible", tier=tier, years_required=years)
        if loc_class == "ambiguous":
            flags.append("location_ambiguous")
        if tier == "untiered":
            flags.append("untiered")

        status = "stage1" if flags_need_stage1(flags) else "stage2"
        return RuleResult(status=status, tier=tier, years_required=years, flags=flags,
                          location_status=self.location_status(job))


def flags_need_stage1(flags: List[str]) -> bool:
    return "location_ambiguous" in flags or "untiered" in flags
