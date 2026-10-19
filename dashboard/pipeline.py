"""What the dashboard's "Run pipeline now" button calls: scout then screen, in the background.

The estimate covers the screening of jobs already scouted; jobs the scouts newly find can add to
it, which is why every run also has a hard cap (max_cost). The run itself is the same
run_pipeline.run that "Run Job Agent.command" uses. Runs are manual; nothing runs on a schedule.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from config import MAX_COST_PER_RUN
from dashboard.service import ServiceError

log = logging.getLogger("dashboard")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class PipelineService:
    def __init__(self, data_dir: Path, companies: Path = Path("companies.yaml"),
                 profile: Path = Path("profile.yaml"), max_cost: float = MAX_COST_PER_RUN,
                 runner: Optional[Callable[..., int]] = None,
                 estimator: Optional[Callable[[], float]] = None):
        self.data_dir, self.companies, self.profile, self.max_cost = data_dir, companies, profile, max_cost
        self._runner, self._estimator = runner, estimator
        self._lock = threading.Lock()
        self._state = {"state": "idle", "started_at": None, "finished_at": None, "exit_code": None}

    # ---- estimate ----
    def _estimate_cost(self) -> float:
        if self._estimator:
            return self._estimator()
        from screener import run_screener
        clean = self.data_dir / "clean"
        jobs = self.data_dir / "raw" / "scout_jobs.json"
        if not jobs.exists():
            return 0.0
        funnel = run_screener.run(jobs, clean / "screened_jobs.json", self.profile,
                                  clean / "screener_cache.json", dry_run=True, max_cost=self.max_cost)
        return float(funnel["est_cost_total"])

    def estimate(self, body: dict) -> dict:
        est = round(self._estimate_cost(), 3)
        return {"est_cost": est, "max_cost": self.max_cost,
                "note": ("Estimate covers screening jobs already found. New jobs from the scouts can add "
                         f"to it; the run stops cleanly at ${self.max_cost:.2f}. Aggregator API calls "
                         "stay inside their plan budgets."),
                **self.status()}

    # ---- run ----
    def status(self) -> dict:
        with self._lock:
            return dict(self._state)

    def start(self, body: dict) -> dict:
        with self._lock:
            if self._state["state"] == "running":
                raise ServiceError(409, "a pipeline run is already in progress")
            self._state = {"state": "running", "started_at": now_iso(), "finished_at": None, "exit_code": None}
        threading.Thread(target=self._work, daemon=True).start()
        return self.status()

    def _work(self) -> None:
        code = 1
        try:
            code = self._execute()
        except Exception as exc:  # noqa: BLE001 - the worker thread must always report back
            log.error("pipeline thread failed: %s", type(exc).__name__)
        finally:
            with self._lock:
                self._state.update(state="done" if code in (0, 2) else "failed",
                                   finished_at=now_iso(), exit_code=code)

    def _execute(self) -> int:
        if self._runner:
            return self._runner(max_cost=self.max_cost)
        import run_pipeline
        clean = self.data_dir / "clean"
        return run_pipeline.run(
            companies=self.companies, profile=self.profile, jobs=self.data_dir / "raw" / "scout_jobs.json",
            screened=clean / "screened_jobs.json", cache=clean / "screener_cache.json", clean_dir=clean,
            usage=self.data_dir / "raw" / "api_usage.json", max_cost=self.max_cost, trigger="dashboard")


class DemoPipelineService(PipelineService):
    """Demo mode never scouts or calls a model."""

    def _estimate_cost(self) -> float:
        return 0.0

    def start(self, body: dict) -> dict:
        raise ServiceError(400, "Running the pipeline is disabled in demo mode")
