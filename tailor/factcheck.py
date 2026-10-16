"""Fact check on the FINAL generated text (resume, cover note, outreach), not on model claims.

Every number, percentage, currency amount, date, tool name, company name and job title found in
the text must be backed by master_profile.json. An INR/USD equivalent pair counts as one fact.
Checks are heuristic by nature; the notes on each one say what it can and cannot see.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List

from tailor.facts import DATE_RE, Facts, extract_dates, extract_numbers, word_in

# Tool vocabulary: a hit is a violation unless the master text also mentions the tool.
KNOWN_TOOLS_LIST = [
    "jira", "confluence", "sql", "mysql", "postgresql", "python", "java", "javascript", "typescript",
    "react", "node.js", "kotlin", "swift", "tableau", "looker", "power bi", "metabase", "amplitude",
    "mixpanel", "segment", "google analytics", "ga4", "heap", "hotjar", "fullstory", "pendo",
    "optimizely", "launchdarkly", "notion", "linear", "asana", "trello", "monday.com", "airtable",
    "zapier", "slack", "github", "gitlab", "bitbucket", "sketch", "zeplin", "miro", "productboard",
    "postman", "swagger", "snowflake", "bigquery", "redshift", "databricks", "dbt", "airflow",
    "hubspot", "zendesk", "intercom", "freshdesk", "freshworks", "servicenow", "sap", "oracle",
    "workday", "netsuite", "stripe", "razorpay", "paypal", "aws", "gcp", "azure", "kubernetes",
    "docker", "terraform", "firebase", "twilio", "webengage", "clevertap", "braze", "appsflyer",
    "adjust", "branch.io", "excel", "google sheets", "salesforce", "moengage", "clarity", "clickup",
    "figma", "erpnext", "mojro", "whatsapp",
]
# Plain business acronyms that are not tools.
GENERIC_ACRONYMS = {"KPI", "KPIS", "OKR", "OKRS", "API", "APIS", "UX", "UI", "PRD", "PRDS", "B2B", "B2C", "B2B2C",
                    "SAAS", "D2C", "ARR", "CRM", "ROI", "MVP", "SLA", "GTM", "PM", "PMS", "AI", "ML", "IT",
                    "HR", "HRMS", "ERP", "NPS", "CSAT", "ETA", "USA", "US", "UK", "EU", "CTO", "CEO", "COO",
                    "IVR", "SMS", "QA", "AB", "CSPO", "ISB", "FAQ", "FAQS", "PDF", "ATS", "IST", "SEO", "SEA",
                    "P&L", "OTP", "UPI", "GST", "NA", "TBD", "ID", "OK"}
_TECH_TOKEN = re.compile(r"(?<![A-Za-z0-9])(?:[A-Z]{2,}[a-z]*\d*|[A-Z][a-z]+[A-Z][A-Za-z]*|[A-Za-z]+\.js|C\+\+|C#)(?![A-Za-z0-9])")
_COMPANY_SUFFIX = re.compile(
    r"\b([A-Z][\w'&.-]*(?:\s[A-Z][\w'&.-]*){0,3}\s(?:Pvt\.?|Ltd\.?|Inc\.?|LLC|LLP|Corp\.?|Corporation|Labs|"
    r"Technologies|Limited|Private|Systems|Solutions))", re.U)
_EMPLOYER_CUE = re.compile(
    r"\b(?i:at|with|for|joined|left|from|via)\s+((?:[A-Z][\w'&-]*)(?:\s[A-Z][\w'&-]*){0,2})")
_TITLE_RE = re.compile(
    r"\b((?:Senior|Sr\.?|Lead|Principal|Group|Staff|Associate|Assistant|Junior|Chief)?\s*"
    r"(?:Vice President|VP|AVP|Head|Director|Product|Program|Project|UI/UX|UX|UI|Design|Engineering)"
    r"(?:\s+(?:of|-)\s+|\s+)?(?:Product|Program|Project|Design|Engineering|Management|Strategy)?\s*"
    r"(?:Manager|Designer|Director|Lead|Owner|President|Head))\b")
_TITLE2_RE = re.compile(
    r"\b((?:(?:Senior|Sr\.?|Associate|Assistant|Executive)\s+)?(?:Vice President|VP|AVP|Director|Head)"
    r"(?:\s+of)?(?:\s+-)?\s+(?:Product|Design|Engineering|Program|Project)(?:\s+Management)?)\b")
NOT_EMPLOYERS = {"india", "bengaluru", "bangalore", "hyderabad", "us", "usa", "the", "a", "an", "my", "your", "our", "i"}
EM_DASH = "—"


@dataclass
class Context:
    """Things in the text that are allowed because they belong to the job, not the candidate."""
    company: str = ""
    title: str = ""
    jd_text: str = ""


def _in_jd_or_target(name: str, ctx: Context) -> bool:
    low = name.lower()
    return low in ctx.jd_text.lower() or low in ctx.company.lower() or ctx.company.lower() in low


def check_numbers(label: str, text: str, facts: Facts, allowed: tuple = None) -> List[str]:
    """Every number must be in master (or in `allowed` = (values, fulls) for per-bullet checks).
    A bare number is accepted on its value; a currency, percent or unit amount must match in full
    (so "₹90Cr" is rejected when master says "₹90L")."""
    values, fulls = allowed or (facts.values, facts.fulls)
    out = []
    for n in extract_numbers(text):
        has_marker = n.full != n.value
        ok = n.full in fulls if has_marker else n.value in values
        if not ok and has_marker and n.value in values and n.full.lstrip("₹$") == n.value:
            ok = True
        if not ok:
            out.append(f"{label}: number '{n.raw}' is not in master_profile.json")
    return out


def check_dates(label: str, text: str, facts: Facts) -> List[str]:
    return [f"{label}: date '{raw}' is not a date in master_profile.json"
            for m, y, raw in extract_dates(text) if (m, y) not in facts.dates]


def check_tools(label: str, text: str, facts: Facts, ctx: Context) -> List[str]:
    out = []
    for tool in KNOWN_TOOLS_LIST:
        if word_in(tool, text.lower()) and not word_in(tool, facts.blob):
            out.append(f"{label}: tool '{tool}' is not in master_profile.json")
    jd_tokens = {t for t in _TECH_TOKEN.findall(ctx.jd_text) if t.upper() not in GENERIC_ACRONYMS}
    for tok in sorted(jd_tokens):
        if word_in(tok, text) and not word_in(tok, facts.blob):
            out.append(f"{label}: term '{tok}' comes from the job description and is not in master_profile.json")
    return out


_SUFFIX_WORDS = r"\b(pvt|ltd|inc|llc|llp|corp|corporation|labs|technologies|limited|private|systems|solutions)\b\.?"


def check_companies(label: str, text: str, facts: Facts, ctx: Context) -> List[str]:
    out = []
    known = " | ".join(facts.companies)
    known_bases = {re.sub(_SUFFIX_WORDS, "", c).strip(" .,") for c in facts.companies}
    for m in _COMPANY_SUFFIX.finditer(text):
        name = m.group(1)
        base = re.sub(_SUFFIX_WORDS, "", name.lower()).strip(" .,")
        # a leading capitalised word from the sentence ("At Sid's Farm Pvt. Ltd.") must not hide a match
        if base and not any(kb and kb in base for kb in known_bases) and not _in_jd_or_target(base, ctx):
            out.append(f"{label}: company '{name}' is not in master_profile.json")
    for m in _EMPLOYER_CUE.finditer(text):
        name = m.group(1).strip()
        low = name.lower()
        words = low.split()
        if (low in NOT_EMPLOYERS or words[0] in NOT_EMPLOYERS or low in known or any(low in c for c in facts.companies)
                or _in_jd_or_target(name, ctx) or word_in(name, facts.blob) or _TITLE_RE.search(name)
                or DATE_RE.match(name) or words[0] in {"product", "senior", "january", "february", "march", "april",
                                                        "may", "june", "july", "august", "september", "october",
                                                        "november", "december"}):
            continue
        out.append(f"{label}: '{name}' looks like a company or employer that is not in master_profile.json")
    return out


def _norm_title(t: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\s-\s|\bof\b", " ", t.lower())).strip()


def check_titles(label: str, text: str, facts: Facts, ctx: Context) -> List[str]:
    """A title phrase must be one of the user's master titles or the target job's own title.
    Sees title-shaped phrases (X Manager, Director of Product, VP Product...); it cannot tell a
    claim ("I was a Senior Director") from a mention ("your Senior Product Manager role")."""
    out = []
    allowed = [_norm_title(t) for t in [*facts.titles, ctx.title] if t]
    for m in [*_TITLE_RE.finditer(text), *_TITLE2_RE.finditer(text)]:
        t = _norm_title(m.group(1))
        if len(t.split()) < 2:
            continue
        if not any(t in a for a in allowed) and not _in_jd_or_target(t, ctx):
            out.append(f"{label}: job title '{m.group(1).strip()}' is not a title in master_profile.json")
    return out


def check_text(texts: Dict[str, str], facts: Facts, ctx: Context) -> List[str]:
    """All checks over every generated text. Returns human-readable violations (empty = pass)."""
    out: List[str] = []
    for label, text in texts.items():
        if EM_DASH in text:
            out.append(f"{label}: contains an em dash")
        out += check_numbers(label, text, facts)
        out += check_dates(label, text, facts)
        out += check_tools(label, text, facts, ctx)
        out += check_companies(label, text, facts, ctx)
        out += check_titles(label, text, facts, ctx)
    return list(dict.fromkeys(out))
