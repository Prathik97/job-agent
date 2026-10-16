"""Match a job-description keyword against master_profile.json, tolerant of word form only.

Counts as the same word: spelling variants (prioritisation / prioritization, behaviour / behavior),
plurals (LLM / LLMs), and a small synonym list. Everything else stays strict: the keyword's words
must appear next to each other, in order, in one master phrase. There is no prefix matching, so
"product" is not backed by "productivity".
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Iterator, List, Optional, Tuple

# Each group is one concept; any phrase in it matches any other. Keep this list small and literal.
SYNONYM_GROUPS: List[List[str]] = [
    ["roadmap", "roadmapping", "product roadmap", "product roadmapping"],
    ["a/b testing", "ab testing", "split testing"],
    ["llm", "large language model"],
    ["generative ai", "genai", "gen ai"],
    ["rag", "retrieval augmented generation"],
    ["prd", "product requirements document"],
    ["ux", "user experience"],
    ["ui", "user interface"],
    ["gtm", "go to market"],
    ["kpi", "key performance indicator"],
]

_WORD = re.compile(r"[A-Za-z0-9]+")


def _spelling(w: str) -> str:
    """British to American spelling, applied to both sides so the two always meet in the middle."""
    w = re.sub(r"isation(s?)$", r"ization\1", w)
    w = re.sub(r"is(e|es|ed|ing)$", lambda m: "iz" + m.group(1), w) if len(w) > 5 else w
    w = re.sub(r"([^aeiou])our(s?)$", r"\1or\2", w)
    w = re.sub(r"ogue(s?)$", r"og\1", w)
    return re.sub(r"([aeiou])ll(ing|ed|er|ers)$", r"\1l\2", w)


def _singular(w: str) -> str:
    if len(w) <= 3 or w.endswith(("ss", "us", "is")):
        return w
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith(("sses", "xes", "ches", "shes")):
        return w[:-2]
    return w[:-1] if w.endswith("s") else w


def canon(word: str) -> str:
    return _singular(_spelling(word.lower()))


def tokens(text: str) -> List[Tuple[str, int, int]]:
    """(canonical word, start, end) for every word in the text."""
    return [(canon(m.group()), m.start(), m.end()) for m in _WORD.finditer(text)]


def _words(phrase: str) -> List[str]:
    return [t[0] for t in tokens(phrase)]


_GROUP_OF: Dict[Tuple[str, ...], int] = {}
for _i, _group in enumerate(SYNONYM_GROUPS):
    for _p in _group:
        _GROUP_OF[tuple(_words(_p))] = _i


def variants(keyword: str) -> List[Tuple[Tuple[str, ...], str]]:
    """The keyword's own word sequence first, then each synonym, as (words, label)."""
    own = tuple(_words(keyword))
    out = [(own, "")]
    group = _GROUP_OF.get(own)
    if group is not None:
        out += [(tuple(_words(p)), p) for p in SYNONYM_GROUPS[group] if tuple(_words(p)) != own]
    return [(w, label) for w, label in out if w]


def find_phrase(words: Tuple[str, ...], text: str) -> Optional[str]:
    """The text as written in `text` where `words` appear contiguously, or None."""
    toks = tokens(text)
    n = len(words)
    for i in range(len(toks) - n + 1):
        if all(toks[i + j][0] == words[j] for j in range(n)):
            return text[toks[i][1]: toks[i + n - 1][2]]
    return None


@dataclass
class Match:
    where: str      # e.g. "skills.leadership", "tag on a3", "projects.pm_assistant.tools"
    matched: str    # the master text that matched, as written there
    how: str        # "exact", "word form" or "synonym 'roadmapping'"


def _sources(master: dict) -> Iterator[Tuple[str, str]]:
    """(where, text) in priority order: skills, achievement tags and text, summary, headlines, projects."""
    for cat, items in master.get("skills", {}).items():
        for item in items:
            yield f"skills.{cat}", item
    for e in master.get("experience", []):
        for a in e.get("achievements", []):
            for tag in a.get("tags", []):
                yield f"tag on {a['id']}", tag.replace("_", " ")
            yield f"text of {a['id']}", a["text"]
    for s in master.get("summary_facts", []):
        yield "summary_facts", s
    for h in master.get("headline_variants", {}).values():
        yield "headline_variants", h
    for p in master.get("projects", []):
        pid = p.get("id", "?")
        for tool in p.get("tools") or []:
            yield f"projects.{pid}.tools", tool
        for tag in p.get("tags") or []:
            yield f"projects.{pid}.tags", tag.replace("_", " ")
        for field in ("name", "description"):
            if isinstance(p.get(field), str):
                yield f"projects.{pid}.{field}", p[field]
        for metric in p.get("metrics") or []:
            yield f"projects.{pid}.metrics", metric


def match_keyword(keyword: str, master: dict) -> Optional[Match]:
    """The first master source that backs the keyword, with the phrase that matched, or None."""
    options = variants(keyword)
    if not options:
        return None
    for where, text in _sources(master):
        for words, label in options:
            hit = find_phrase(words, text)
            if hit is None:
                continue
            if label:
                how = f"synonym '{label}'"
            else:
                how = "exact" if hit.lower() == keyword.strip().lower() else "word form"
            return Match(where, hit if len(text) > 80 else text, how)
    return None
