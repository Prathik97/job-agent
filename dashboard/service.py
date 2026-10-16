"""What the dashboard's "Prepare application" button calls. The real service runs the Tailor;
the demo service writes clearly-marked placeholder files and never calls a model."""
from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional


class ServiceError(Exception):
    def __init__(self, status: int, message: str, **extra):
        super().__init__(message)
        self.status, self.message, self.extra = status, message, extra


def list_files(folder: Path) -> List[str]:
    try:
        return sorted(p.name for p in folder.iterdir() if p.is_file())
    except OSError:
        return []


class TailorService:
    """Runs the real Tailor (imported lazily so the dashboard itself stays standard library only)."""

    def __init__(self, data_dir: Path, output_dir: Path, master_path: Path, profile_path: Path):
        self.data_dir, self.output_dir = data_dir, output_dir
        self.master_path, self.profile_path = master_path, profile_path

    def _kw(self):
        return dict(data_dir=self.data_dir, master_path=self.master_path, profile_path=self.profile_path)

    def estimate(self, job_id: str, jd: Optional[str]) -> dict:
        from config import MAX_COST_PER_RUN
        from tailor import tailor as t
        try:
            prep = t.prepare(job_id, jd_override=jd, **self._kw())
        except t.PartialJDError:
            return {"requires_jd": True}
        except t.TailorError as exc:
            raise ServiceError(400, str(exc))
        worst = prep.est_cost + 1.3 * prep.est_cost
        return {"requires_jd": False, "model": prep.model, "est_cost": round(prep.est_cost, 3),
                "worst_cost": round(worst, 3), "max_cost": MAX_COST_PER_RUN}

    def run(self, job_id: str, jd: Optional[str]) -> dict:
        from tailor import tailor as t
        try:
            res = t.tailor_job(job_id, out_root=self.output_dir, jd_override=jd, **self._kw())
        except t.PartialJDError as exc:
            raise ServiceError(400, str(exc), needs_jd=True)
        except t.FactCheckFailed as exc:
            raise ServiceError(422, "The resume failed the fact check, so nothing was written.",
                               violations=exc.violations)
        except t.TailorError as exc:
            raise ServiceError(400, str(exc))
        return {"folder": res.folder.name, "files": list_files(res.folder), "model": res.model,
                "cost": round(res.cost, 4)}


_MIN_PDF = (b"%PDF-1.1\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
            b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 120]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
            b"4 0 obj<</Length 52>>stream\nBT /F1 14 Tf 20 60 Td (DEMO resume placeholder) Tj ET\nendstream endobj\n"
            b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
            b"trailer<</Root 1 0 R/Size 6>>\n%%EOF\n")


class DemoService:
    def __init__(self, data_dir: Path, output_dir: Path):
        self.data_dir, self.output_dir = data_dir, output_dir

    def _job(self, job_id: str) -> dict:
        try:
            rows = json.loads((self.data_dir / "clean" / "screened_jobs.json").read_text())
        except (OSError, ValueError):
            rows = []
        for r in rows:
            if r["id"] == job_id:
                return r
        raise ServiceError(404, "unknown job")

    def estimate(self, job_id: str, jd: Optional[str]) -> dict:
        job = self._job(job_id)
        if job.get("jd_partial") and not (jd or "").strip():
            return {"requires_jd": True}
        return {"requires_jd": False, "model": "demo (no model call)", "est_cost": 0.05, "worst_cost": 0.12,
                "max_cost": 1.0}

    def run(self, job_id: str, jd: Optional[str]) -> dict:
        job = self._job(job_id)
        if job.get("jd_partial") and not (jd or "").strip():
            raise ServiceError(400, "partial JD: paste the full job description first.", needs_jd=True)
        name = "".join(c if c.isalnum() else "-" for c in f"{job['company']}_{job['title']}")[:60] + "_demo"
        folder = self.output_dir / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "resume.pdf").write_bytes(_MIN_PDF)
        for fname in ("cover_note.md", "outreach.md", "changes.md", "jd_keywords.md", "screening_answers.md"):
            (folder / fname).write_text(f"DEMO placeholder for {fname}. No model was called.\n")
        return {"folder": name, "files": list_files(folder), "model": "demo", "cost": 0.0}
