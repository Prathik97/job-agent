"""Projects as facts, the AI/technical gate, and the word-form keyword match. The model is faked."""
import copy
import json
from datetime import date
from types import SimpleNamespace

import pytest
import yaml

from tailor import tailor as t
from tailor.facts import Facts
from tailor.factcheck import Context, check_text
from tailor.keywords import match_keyword
from tailor.plan import TailorPlan, structure_violations
from tailor.projects import jd_warrants_projects
from tests.test_tailor import MASTER, good_plan

PROJECTS = [
    {"id": "copilot", "name": "Example PRD Copilot", "description": "An AI co-pilot that drafts PRDs from rough notes.",
     "tools": ["Python", "Claude API"], "metrics": ["120 weekly users"], "link": "https://example.com/copilot",
     "status": "live", "tags": ["ai", "prd"], "unanswered": []},
    {"id": "checker", "name": "Example Score Checker", "description": "A public tool that scores a resume against a JD.",
     "tools": ["Vercel", "Next.js"], "metrics": None, "link": None, "status": "live", "tags": ["ats"],
     "unanswered": ["metrics", "link"]},
    {"id": "blank", "name": "Unfinished idea", "description": None, "tools": None, "metrics": None, "link": None,
     "status": None, "tags": None, "unanswered": ["description", "tools", "metrics", "link", "status", "tags"]},
]
M = {**copy.deepcopy(MASTER), "projects": PROJECTS}
M["skills"]["product"] = ["Roadmapping", "Prioritisation", "A/B testing", "LLM", "Stakeholder management"]
FACTS = Facts.from_master(M)
AI_JD = "Own the AI product roadmap. Work with LLMs, RAG pipelines and agents. Comfortable with APIs."
PLAIN_JD = "Own the roadmap for our subscription app. Work with design and sales on pricing."
CTX = Context(company="Acme Corp", title="Senior Product Manager", jd_text=AI_JD)


def plan_with(*projects, **over):
    return TailorPlan.model_validate(good_plan(projects=list(projects), **over))


def violations(plan):
    return structure_violations(plan, FACTS)


# ---------- projects as facts ----------

def test_project_fact_passes_the_checker():
    line = "An AI co-pilot that drafts PRDs from rough notes, used by 120 weekly users."
    plan = plan_with({"id": "copilot", "text": line, "reword_why": "adds the real usage number"})
    assert violations(plan) == []
    assert check_text({"resume": line + " Built with Claude API and Python."}, FACTS, CTX) == []
    # the description unchanged needs no reword_why
    assert violations(plan_with({"id": "copilot", "text": PROJECTS[0]["description"]})) == []


def test_invented_project_is_rejected():
    out = violations(plan_with({"id": "robo_trader", "text": "Built an automated trading bot."}))
    assert any("'robo_trader' is not a project in master_profile.json" in v for v in out)
    # a project the user left without a description cannot be used either
    assert any("'blank'" in v for v in violations(plan_with({"id": "blank", "text": "Something."})))


def test_invented_number_in_a_project_is_rejected():
    out = violations(plan_with({"id": "copilot", "text": "An AI co-pilot used by 5,000 users.", "reword_why": "x"}))
    assert any("'5,000'" in v for v in out)
    # another project's number is not borrowable either
    assert any("120" in v for v in violations(plan_with(
        {"id": "checker", "text": "A public tool used by 120 weekly users.", "reword_why": "x"})))


def test_project_tool_not_confirmed_is_rejected():
    # Vercel is confirmed for the checker only, Kubernetes is confirmed nowhere
    out = violations(plan_with({"id": "copilot", "text": "An AI co-pilot hosted on Vercel.", "reword_why": "x"}))
    assert any("tool 'vercel' is not one of this project's confirmed tools" in v for v in out)
    out = violations(plan_with({"id": "copilot", "text": "An AI co-pilot running on Kubernetes.", "reword_why": "x"}))
    assert any("tool 'kubernetes'" in v for v in out)
    assert any("kubernetes" in v for v in check_text({"r": "Runs on Kubernetes."}, FACTS, CTX))
    # a confirmed tool is fine on its own project
    assert violations(plan_with({"id": "checker", "text": "A public tool on Vercel that scores a resume against a JD.",
                                 "reword_why": "adds hosting"})) == []


def test_reworded_project_needs_a_reason_and_at_most_three_projects():
    assert any("reword_why is empty" in v for v in violations(plan_with({"id": "copilot", "text": "A PRD co-pilot."})))
    many = [{"id": "copilot", "text": PROJECTS[0]["description"]}] * 4
    assert any("at most 3" in v for v in violations(plan_with(*many)))


# ---------- the gate and the rendering ----------

@pytest.fixture
def env(tmp_path):
    data = tmp_path / "data" / "clean"
    data.mkdir(parents=True)

    def row(jid, desc):
        return {"id": jid, "source": "greenhouse", "title": "Senior Product Manager", "company": "Acme Corp",
                "location": "Bangalore, Karnataka", "url": "https://x", "description": desc, "jd_partial": False,
                "also_listed_on": [], "screen": {"status": "scored", "stage2": {"score": 72, "verdict": "apply"}}}
    (data / "screened_jobs.json").write_text(json.dumps([row("ai", AI_JD), row("plain", PLAIN_JD)]))
    (tmp_path / "master.json").write_text(json.dumps(M))
    (tmp_path / "profile.yaml").write_text(yaml.safe_dump({"scoring": {"thresholds": {"top_pick_min_score": 85}}}))
    return SimpleNamespace(root=tmp_path)


def prep(env, job_id):
    return t.prepare(job_id, data_dir=env.root / "data", master_path=env.root / "master.json",
                     profile_path=env.root / "profile.yaml")


def test_gate_needs_an_ai_or_technical_jd():
    assert jd_warrants_projects("Product Manager", AI_JD)
    assert not jd_warrants_projects("Senior Product Manager", PLAIN_JD)
    assert not jd_warrants_projects("Senior Product Manager", "Work with engineering and data on our platform.")
    assert jd_warrants_projects("Technical PM", "Own APIs, SDK, platform, cloud and backend integrations.")


def test_projects_reach_the_model_only_for_ai_jds_and_only_answered_ones(env):
    ai, plain = prep(env, "ai"), prep(env, "plain")
    assert ai.projects_ok and [p["id"] for p in ai.master_view["projects"]] == ["copilot", "checker"]
    assert "Example PRD Copilot" in ai.user and "Unfinished idea" not in ai.user
    assert not plain.projects_ok and "projects" not in plain.master_view and "Example PRD Copilot" not in plain.user


def test_model_chosen_projects_are_dropped_when_the_gate_is_off(env):
    p = prep(env, "plain")
    plan, v = t.evaluate(json.dumps(good_plan(projects=[{"id": "copilot", "text": PROJECTS[0]["description"]}])), p)
    assert plan.projects == []
    assert "Personal Projects" not in "\n".join(h for h, _ in t.build_sections(plan, p)[2])


def test_projects_render_as_personal_projects_never_as_employment(env):
    p = prep(env, "ai")
    plan = plan_with({"id": "copilot", "text": PROJECTS[0]["description"]},
                     {"id": "checker", "text": PROJECTS[1]["description"]})
    _, _, sections = t.build_sections(plan, p)
    by_head = {h: blocks for h, blocks in sections}
    assert "PERSONAL PROJECTS" in by_head
    roles = [(b[1], b[2]) for b in by_head["PERSONAL PROJECTS"] if b[0] == "role"]
    assert roles == [("Example PRD Copilot", "Personal project"), ("Example Score Checker", "Personal project")]
    assert "Example PRD Copilot" not in json.dumps(by_head["WORK EXPERIENCE"])
    heads = [h for h, _ in sections]
    assert heads.index("PERSONAL PROJECTS") == heads.index("WORK EXPERIENCE") + 1
    assert "Personal projects shown" in t.changes_md(plan, p, [])


# ---------- keyword match: word form only ----------

@pytest.mark.parametrize("keyword,where,how", [
    ("product roadmap", "skills.product", "synonym 'roadmapping'"),     # the synonym list
    ("roadmaps", "skills.product", "synonym 'roadmapping'"),
    ("prioritization", "skills.product", "word form"),                  # spelling variant
    ("A/B testing", "skills.product", "exact"),
    ("LLMs", "skills.product", "word form"),                            # plural
    ("large language model", "skills.product", "synonym 'llm'"),
    ("stakeholder managements", "skills.product", "word form"),
])
def test_word_forms_and_synonyms_match_and_show_the_master_phrase(keyword, where, how):
    m = match_keyword(keyword, M)
    assert m is not None and m.where == where and m.how == how
    assert m.matched in {"Roadmapping", "Prioritisation", "A/B testing", "LLM", "Stakeholder management"}


def test_reverse_direction_also_matches():
    master = {**M, "skills": {"x": ["Prioritization", "Product roadmap"]}}
    assert match_keyword("prioritisation", master).matched == "Prioritization"
    hit = match_keyword("roadmapping", master)
    assert hit.matched == "Product roadmap" and hit.how.startswith("synonym")


@pytest.mark.parametrize("keyword", ["Kubernetes", "product", "road", "generative AI", "wealth-tech", "product strategy"])
def test_unrelated_or_partial_keywords_stay_not_backed(keyword):
    master = {"skills": {"x": ["Roadmapping", "Productivity tools", "Prioritisation"]}, "experience": []}
    assert match_keyword(keyword, master) is None      # "product" is not backed by "productivity"


def test_project_text_tools_and_tags_back_keywords():
    assert match_keyword("Claude API", M).where == "projects.copilot.tools"
    assert match_keyword("PRD", M).where in ("projects.copilot.tags", "projects.copilot.description")
    assert match_keyword("ATS", M).where == "projects.checker.tags"


def test_jd_keywords_md_names_the_matching_master_phrase():
    plan = TailorPlan.model_validate(good_plan(jd_keywords=["product roadmap", "Kubernetes"]))
    md = t.jd_keywords_md(plan, M)
    assert "- product roadmap: backed (matched 'Roadmapping' in skills.product, synonym 'roadmapping')" in md
    assert "- Kubernetes: NOT backed" in md and "Backed: 1 of 2." in md
