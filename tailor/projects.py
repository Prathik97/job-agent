"""Personal projects: when a JD warrants showing them, and what a project may claim.

Projects are facts in master_profile.json like any other, but they are never employment: the resume
shows them under their own "Personal Projects" heading, each labelled as a personal project, and only
when the job is AI or technical enough. The gate is code, not the model's opinion.
"""
from __future__ import annotations

from typing import List

from tailor.facts import word_in

MAX_PROJECTS = 3
STRONG = ["ai", "llm", "llms", "generative ai", "genai", "machine learning", "ml", "rag", "prompt", "prompts",
          "ai agents", "agentic", "evals", "nlp", "deep learning", "embeddings", "artificial intelligence"]
TECH = ["api", "apis", "sdk", "python", "sql", "developer", "developers", "technical", "architecture",
        "engineering", "infrastructure", "data pipeline", "data pipelines", "devops", "cloud", "backend",
        "platform", "integrations", "automation"]


def _hits(terms: List[str], text: str) -> int:
    return sum(1 for t in terms if word_in(t, text))


def jd_warrants_projects(title: str, jd: str) -> bool:
    """True for AI roles (2+ AI terms, or 1 plus 2 technical terms) and heavily technical roles (4+)."""
    text = f"{title}\n{jd}".lower()
    strong, tech = _hits(STRONG, text), _hits(TECH, text)
    return strong >= 2 or (strong >= 1 and tech >= 2) or tech >= 4


def offerable(master: dict) -> List[dict]:
    """Projects the Tailor may use: they need a name and a description the user actually gave."""
    return [p for p in master.get("projects", []) if p.get("name") and p.get("description")]


def facts_text(project: dict) -> str:
    """Everything a project's own bullet may draw numbers and tools from."""
    parts = [project.get("name"), project.get("description"), *(project.get("metrics") or []),
             *(project.get("tools") or [])]
    return "\n".join(str(x) for x in parts if x)
