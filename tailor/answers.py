"""Screening answers from data/clean/application_answers.yaml (gitignored, personal).

A missing or skipped field is written as "unanswered"; nothing is ever guessed. This module
never logs, prints or returns values anywhere except the screening_answers.md text.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

import yaml

# (key, label, default used only when the key is absent from the file)
QUESTIONS = [
    ("notice_period", "Notice period", None),
    ("earliest_start_date", "Earliest start date", None),
    ("current_fixed_ctc", "Current fixed CTC", None),
    ("current_variable_ctc", "Current variable CTC", None),
    ("expected_ctc", "Expected CTC", None),
    ("willing_to_relocate", "Willing to relocate", None),
    ("work_authorization_india", "Work authorization for India", None),
    ("how_heard", "How did you hear about this role", "job board"),
    ("linkedin_url", "LinkedIn URL", None),
    ("portfolio_url", "Portfolio URL", None),
]


def load_answers(path: Path) -> Dict[str, Optional[str]]:
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        data = {}
    out: Dict[str, Optional[str]] = {}
    for key, _, default in QUESTIONS:
        raw = data.get(key, default) if isinstance(data, dict) else default
        if isinstance(raw, dict):               # {answer: ..., status: unanswered}
            raw = raw.get("answer") if raw.get("status") != "unanswered" else None
        text = None if raw is None else str(raw).strip()
        out[key] = text or None
    return out


def screening_answers_md(answers: Dict[str, Optional[str]]) -> str:
    lines = ["# Screening answers", "",
             "Copied from your application_answers.yaml. Anything you have not answered says unanswered.", ""]
    for key, label, _ in QUESTIONS:
        lines.append(f"- {label}: {answers.get(key) or 'unanswered'}")
    return "\n".join(lines) + "\n"
