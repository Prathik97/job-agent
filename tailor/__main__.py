"""python -m tailor <job_id> [--jd-file FILE] [--max-cost USD] [--dry-run]"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

from config import MAX_COST_PER_RUN
from tailor import tailor as t


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("job_id")
    p.add_argument("--jd-file", type=Path, help="full job description (required for aggregator jobs)")
    p.add_argument("--max-cost", type=float, default=MAX_COST_PER_RUN)
    p.add_argument("--dry-run", action="store_true", help="print the cost estimate and stop")
    p.add_argument("--data", type=Path, default=Path("data"))
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    load_dotenv()
    jd = a.jd_file.read_text() if a.jd_file else None
    try:
        if a.dry_run:
            prep = t.prepare(a.job_id, data_dir=a.data, master_path=Path("master_profile.json"),
                             profile_path=Path("profile.yaml"), jd_override=jd)
            print(f"{prep.model}: estimated ${prep.est_cost:.3f} per call (worst case about ${2.3 * prep.est_cost:.3f})")
            return 0
        res = t.tailor_job(a.job_id, data_dir=a.data, jd_override=jd, max_cost=a.max_cost)
    except t.TailorError as exc:
        print(f"Not tailored: {exc}", file=sys.stderr)
        if isinstance(exc, t.FactCheckFailed):
            for v in exc.violations:
                print(f"  - {v}", file=sys.stderr)
        return 2
    print(f"Wrote {res.folder}  ({res.model}, ${res.cost:.3f}, {res.calls} call(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
