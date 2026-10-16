"""Everything the fact checker knows about the user, derived from master_profile.json only."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Set, Tuple

MONTHS = {m: i for i, m in enumerate(
    "january february march april may june july august september october november december".split(), 1)}
_MONTH_ABBR = {k[:3]: v for k, v in MONTHS.items()} | {"sept": 9}
_MONTH_RE = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
DATE_RE = re.compile(rf"\b({_MONTH_RE})\.?\s+(\d{{4}})\b", re.I)

WORD_NUMS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen twenty".split())}
WORD_NUMS.update({"thirty": 30, "forty": 40, "fifty": 50, "hundred": 100})
# "one" is too common in ordinary prose ("one of", "no one") to treat as a claim.
CHECKED_WORDS = {w: v for w, v in WORD_NUMS.items() if w not in ("zero", "one")}

NUM_RE = re.compile(
    r"(?P<cur>₹|\$|Rs\.?\s?|INR\s?|USD\s?)?"
    r"(?P<num>\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"(?P<unit>\s?(?:crores?|cr|lakhs?|l|m|k|x|%)(?![A-Za-z]))?", re.I)
_WORD_RE = re.compile(r"(?<![A-Za-z])(" + "|".join(CHECKED_WORDS) + r")(?![A-Za-z])", re.I)
_UNIT = {"crore": "cr", "crores": "cr", "lakh": "l", "lakhs": "l"}


@dataclass(frozen=True)
class Num:
    raw: str
    value: str   # "1000", "18.5"
    full: str    # currency + value + unit, e.g. "₹18.5l", "85%", "2x"


def _canon_value(s: str) -> str:
    s = s.replace(",", "")
    return s[:-2] if s.endswith(".0") else s


def extract_numbers(text: str) -> List[Num]:
    out = []
    for m in NUM_RE.finditer(text):
        cur = (m.group("cur") or "").strip().lower()
        cur = "₹" if cur in ("₹", "rs", "rs.", "inr") else "$" if cur in ("$", "usd") else ""
        unit = (m.group("unit") or "").strip().lower()
        unit = _UNIT.get(unit, unit)
        value = _canon_value(m.group("num"))
        out.append(Num(m.group(0).strip(), value, f"{cur}{value}{unit}"))
    for m in _WORD_RE.finditer(text):
        v = str(CHECKED_WORDS[m.group(1).lower()])
        out.append(Num(m.group(0), v, v))
    return out


def extract_dates(text: str) -> List[Tuple[int, int, str]]:
    out = []
    for m in DATE_RE.finditer(text):
        key = m.group(1).lower().rstrip(".")
        month = MONTHS.get(key) or _MONTH_ABBR.get(key[:3])
        out.append((month, int(m.group(2)), m.group(0)))
    return out


def _strings(obj) -> List[str]:
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [s for v in obj.values() for s in _strings(v)]
    if isinstance(obj, list):
        return [s for v in obj for s in _strings(v)]
    return []


def word_in(term: str, blob: str) -> bool:
    return re.search(r"(?<![A-Za-z0-9])" + re.escape(term.lower()) + r"(?![A-Za-z0-9])", blob) is not None


@dataclass
class Facts:
    master: dict
    blob: str = ""                      # all master text, lowercased
    values: Set[str] = field(default_factory=set)
    fulls: Set[str] = field(default_factory=set)
    dates: Set[Tuple[int, int]] = field(default_factory=set)
    companies: Set[str] = field(default_factory=set)
    titles: Set[str] = field(default_factory=set)
    tools: Set[str] = field(default_factory=set)
    equivalents: List[Dict[str, str]] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "Facts":
        return cls.from_master(json.loads(path.read_text()))

    @classmethod
    def from_master(cls, master: dict) -> "Facts":
        f = cls(master=master)
        f.equivalents = [e for e in master.get("equivalents", []) if e.get("inr") and e.get("usd")]
        texts = _strings(master)
        f.blob = "\n".join(texts).lower()
        f.values, f.fulls = f.numbers_allowed_by("\n".join(texts))
        f.dates = {(m, y) for m, y, _ in extract_dates("\n".join(texts))}
        exp = master.get("experience", [])
        f.companies = {e["company"].lower() for e in exp}
        f.titles = {e["title"].lower() for e in exp}
        f.tools = {t.lower() for t in master.get("skills", {}).get("tools", [])}
        return f

    def numbers_allowed_by(self, text: str) -> Tuple[Set[str], Set[str]]:
        """Values and full tokens that `text` supports. An INR/USD equivalent pair is one fact:
        if the text has one side, both sides' numbers are supported."""
        extra = []
        low = text.lower()
        for pair in self.equivalents:
            if pair["inr"].lower() in low or pair["usd"].lower() in low:
                extra += [pair["inr"], pair["usd"]]
        nums = extract_numbers(text + "\n" + "\n".join(extra))
        return {n.value for n in nums}, {n.full for n in nums}

    def localize(self, text: str, india: bool) -> str:
        """Show the currency form that fits the job: INR in India-based jobs, USD elsewhere."""
        for pair in self.equivalents:
            keep, drop = (pair["inr"], pair["usd"]) if india else (pair["usd"], pair["inr"])
            text = text.replace(drop, keep)
        return text

    def localized_master(self, india: bool) -> dict:
        return json.loads(self.localize(json.dumps(self.master, ensure_ascii=False), india))
