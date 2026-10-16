"""Title normalization applied to job titles and profile title phrases before matching."""
from __future__ import annotations

import re

_ROMAN = {"ii": "2", "iii": "3", "iv": "4"}
# Standalone roman numerals only. A bare "I" is left alone: it is also a pronoun and
# part of names, and "Product Manager I" is rare.
_ROMAN_RE = re.compile(r"(?<![A-Za-z0-9])(ii|iii|iv)(?![A-Za-z0-9])", re.I)
_SR_RE = re.compile(r"(?<![A-Za-z0-9])sr\b\.?", re.I)
_JR_RE = re.compile(r"(?<![A-Za-z0-9])jr\b\.?", re.I)


def normalize_title(title: str) -> str:
    """'Sr. PM III' -> 'Senior PM 3'; 'Jr Product Manager II' -> 'Junior Product Manager 2'."""
    text = _ROMAN_RE.sub(lambda m: _ROMAN[m.group(1).lower()], title or "")
    text = _SR_RE.sub("Senior", text)
    text = _JR_RE.sub("Junior", text)
    return re.sub(r"\s+", " ", text).strip()
