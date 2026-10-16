"""python -m evaluate [--data data]: compare the agent's verdicts with your labels.

Reads data/clean/labels.json and writes data/clean/eval_report.json (aggregate numbers only).
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from dashboard.labels import LabelStore
from dashboard.store import atomic_write
from evaluate.metrics import MIN_LABELS, aggregate_only, compute, filtered_totals


def _read(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return []


def run(data_dir: Path) -> dict:
    clean = data_dir / "clean"
    filtered = _read(clean / "filtered_out.json")
    metrics = compute(LabelStore(clean / "labels.json").load(), filtered_totals(filtered))
    report = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              **aggregate_only(metrics)}
    atomic_write(clean / "eval_report.json", report)
    titles = {r["id"]: f'{r.get("company", "")} | {r.get("title", "")}' for r in
              [*_read(clean / "screened_jobs.json"), *filtered]}
    print_report(metrics, titles)
    return report


def print_report(m: dict, titles: dict) -> None:
    p, a = m["progress"], m["agreement"]
    print(f"Labelled scored jobs: {p['labelled']} / {p['target']}")
    if a["rate"] is None:
        print(f"Agreement: not shown until {MIN_LABELS} labels ({a['agree']} of {a['total']} agree so far)")
    else:
        print(f"Agreement: {a['rate'] * 100:.1f}% ({a['agree']} of {a['total']})")
    print("\nConfusion (rows = agent, columns = you)\n  %-9s%6s%7s%5s" % ("", "yes", "maybe", "no"))
    for v, row in m["confusion"].items():
        print("  %-9s%6d%7d%5d" % (v, row["yes"], row["maybe"], row["no"]))
    e = m["errors"]
    print(f"\nMissed opportunities (agent skip, you said yes): {e['agent_skip_but_i_said_yes']}")
    print(f"Wasted time (agent apply, you said no):          {e['agent_apply_but_i_said_no']}")
    print("\nDisagreements by reason:", m["disagreements_by_reason"] or "none")
    for d in m["disagreements"]:
        print(f"  - {titles.get(d['id'], d['id'])}: agent {d['agent_verdict']}, you {d['my_label']}, "
              f"reason {d['reason']}" + (f", note: {d['note']}" if d["note"] else ""))
    f = m["filtered"]
    rate = "n/a" if f["false_drop_rate"] is None else f"{f['false_drop_rate'] * 100:.1f}%"
    print(f"\nFiltered out: you flagged {f['flagged']} of {f['total']} as wrongly dropped ({rate})")
    for cat, r in f["by_reason"].items():
        if r["flagged"]:
            print(f"  {cat}: {r['flagged']} of {r['total']}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data", type=Path, default=Path("data"))
    run(p.parse_args().data)


if __name__ == "__main__":
    main()
