"""Agreement between the agent's verdicts and the user's own labels. Standard library only."""
from __future__ import annotations

from collections import Counter
from typing import Dict, Iterable, Optional

VERDICT_TO_LABEL = {"apply": "yes", "stretch": "maybe", "skip": "no"}
VERDICTS = ("apply", "stretch", "skip")
LABELS = ("yes", "maybe", "no")
MIN_LABELS = 30


def drop_category(reason: str) -> str:
    """'title_reject:Sales' -> 'title_reject'; 'stage1: remote US only' -> 'stage1'."""
    return (reason or "unknown").split(":")[0].strip() or "unknown"


def _rate(num: int, den: int) -> Optional[float]:
    return round(num / den, 4) if den else None


def filtered_totals(filtered_out: Iterable[dict]) -> Dict[str, int]:
    return dict(Counter(drop_category(f.get("reason", "")) for f in filtered_out))


def compute(labels: Dict[str, dict], totals: Optional[Dict[str, int]] = None) -> dict:
    """Full metrics, including the per-job disagreement list (ids, reasons, notes).
    `labels` are the stored entries; the agent side comes from each label's snapshot, so
    later re-scoring never changes history. `totals` = filtered-out counts per drop category."""
    totals = totals or {}
    scored = [(jid, l) for jid, l in labels.items() if l.get("kind") == "scored"]
    flagged = [(jid, l) for jid, l in labels.items() if l.get("kind") == "filtered"]
    confusion = {v: {k: 0 for k in LABELS} for v in VERDICTS}
    disagreements, reasons = [], Counter()
    agree = 0
    for jid, l in scored:
        verdict, mine = l["snapshot"]["verdict"], l["label"]
        confusion[verdict][mine] += 1
        if VERDICT_TO_LABEL[verdict] == mine:
            agree += 1
        else:
            reason = l.get("reason") or "none given"
            reasons[reason] += 1
            disagreements.append({"id": jid, "agent_verdict": verdict, "my_label": mine,
                                  "score": l["snapshot"].get("score"), "reason": reason,
                                  "note": l.get("note", "")})
    n = len(scored)
    flagged_by = Counter(drop_category(l["snapshot"].get("drop_reason", "")) for _, l in flagged)
    total_filtered = sum(totals.values())
    by_reason = {c: {"flagged": flagged_by.get(c, 0), "total": totals.get(c, 0),
                     "false_drop_rate": _rate(flagged_by.get(c, 0), totals.get(c, 0))}
                 for c in sorted(set(totals) | set(flagged_by))}
    return {
        "progress": {"labelled": n, "target": MIN_LABELS},
        "agreement": {"agree": agree, "total": n,
                      "rate": _rate(agree, n) if n >= MIN_LABELS else None},  # no headline < 30
        "confusion": confusion,
        "errors": {"agent_skip_but_i_said_yes": confusion["skip"]["yes"],
                   "agent_apply_but_i_said_no": confusion["apply"]["no"]},
        "disagreements_by_reason": dict(reasons.most_common()),
        "disagreements": disagreements,
        "filtered": {"flagged": len(flagged), "total": total_filtered,
                     "false_drop_rate": _rate(len(flagged), total_filtered), "by_reason": by_reason},
    }


def aggregate_only(metrics: dict) -> dict:
    """The eval_report.json payload: numbers only, no ids, notes, titles, companies or URLs."""
    return {k: v for k, v in metrics.items() if k != "disagreements"}
