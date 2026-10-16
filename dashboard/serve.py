"""Private local dashboard. Standard library only; binds to 127.0.0.1.

    python -m dashboard.serve [--port 8765] [--data data] [--demo]

Every request must carry a localhost Host header (blocks DNS rebinding). Writes also need a
localhost Origin and the random per-start token that is embedded in the served page.
"""
from __future__ import annotations

import argparse
import atexit
import hmac
import json
import logging
import secrets
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import unquote, urlsplit

from dashboard.data import build_state, filtered_snapshot_for, snapshot_for
from dashboard.labels import LabelStore
from dashboard.prepared import PreparedStore
from dashboard.service import ServiceError, list_files
from dashboard.store import ApplicationsStore, StoreError
from evaluate.metrics import compute, filtered_totals

log = logging.getLogger("dashboard")
BIND_HOST = "127.0.0.1"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}
STATIC = Path(__file__).parent / "static"
STATIC_FILES = {"/app.css": ("app.css", "text/css; charset=utf-8"),
                "/app.js": ("app.js", "text/javascript; charset=utf-8")}
MAX_BODY = 256 * 1024   # room for a pasted job description
FILE_TYPES = {".pdf": "application/pdf", ".md": "text/markdown; charset=utf-8",
              ".txt": "text/plain; charset=utf-8",
              ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
       "img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int, data_dir: Path, token: Optional[str] = None, demo: bool = False,
                 output_dir: Optional[Path] = None, service=None):
        self.data_dir = data_dir
        self.demo = demo
        self.output_dir = (output_dir or Path("output/resumes")).resolve()
        self.service = service
        self.tailor_lock = threading.Lock()   # one tailor run at a time: each one spends API credit
        self.prepared = PreparedStore(data_dir / "clean" / "prepared.json")
        self.token = token or secrets.token_urlsafe(32)
        self.store = ApplicationsStore(data_dir / "clean" / "applications.json")
        self.labels = LabelStore(data_dir / "clean" / "labels.json")
        super().__init__((BIND_HOST, port), Handler)

    @property
    def port(self) -> int:
        return self.server_address[1]


def host_ok(host_header: Optional[str], port: int) -> bool:
    """True for localhost / 127.0.0.1 / [::1] on this server's port."""
    if not host_header:
        return False
    try:
        parts = urlsplit("//" + host_header)
        name, host_port = parts.hostname, parts.port
    except ValueError:
        return False
    return (name or "").lower() in {h.strip("[]") for h in LOCAL_HOSTS} and (host_port or 80) == port


def origin_ok(origin: Optional[str], port: int) -> bool:
    try:
        parts = urlsplit(origin or "")
        return parts.scheme == "http" and host_ok(parts.netloc, port)
    except ValueError:
        return False


class Handler(BaseHTTPRequestHandler):
    server: DashboardServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # no request lines (they could carry job ids) on stderr
        log.debug(fmt, *args)

    # ---- helpers ----
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Connection", "close")  # early rejections leave request bodies unread
        self.end_headers()
        self.close_connection = True
        self.wfile.write(body)

    def _json(self, code: int, payload: object) -> None:
        self._send(code, json.dumps(payload).encode(), "application/json")

    def _guard(self) -> bool:
        if not host_ok(self.headers.get("Host"), self.server.port):
            self._json(403, {"error": "bad host"})
            return False
        return True

    # ---- routes ----
    def do_GET(self) -> None:
        if not self._guard():
            return
        path = urlsplit(self.path).path
        if path == "/":
            html = (STATIC / "index.html").read_text().replace("__TOKEN__", self.server.token)
            self._send(200, html.encode(), "text/html; charset=utf-8")
        elif path in STATIC_FILES:
            name, ctype = STATIC_FILES[path]
            self._send(200, (STATIC / name).read_bytes(), ctype)
        elif path.startswith("/files/"):
            self._serve_file(path)
        elif path == "/api/eval":
            self._json(200, self._eval())
        elif path == "/api/state":
            self._json(200, {**build_state(self.server.data_dir, self.server.store),
                             "demo": self.server.demo, "labels": self.server.labels.load(),
                             "eval": self._eval(), "prepared": self._prepared()})
        else:
            self._json(404, {"error": "not found"})

    def _prepared(self) -> dict:
        out = {}
        for job_id, folder in self.server.prepared.load().items():
            files = list_files(self.server.output_dir / folder) if self._safe_component(folder) else []
            if files:
                out[job_id] = {"folder": folder, "files": files}
        return out

    @staticmethod
    def _safe_component(part: str) -> bool:
        return bool(part) and part not in (".", "..") and not part.startswith(".") and not any(
            c in part for c in "/\\\x00")

    def _token_ok(self) -> bool:
        return hmac.compare_digest(self.headers.get("X-Dashboard-Token", ""), self.server.token)

    def _serve_file(self, raw_path: str) -> None:
        """Files come only from output/resumes/<folder>/<file>, to localhost with the token."""
        if not self._token_ok():
            return self._json(403, {"error": "bad token"})
        parts = unquote(raw_path[len("/files/"):]).split("/")
        if len(parts) != 2 or not all(self._safe_component(p) for p in parts):
            return self._json(404, {"error": "not found"})
        root = self.server.output_dir
        target = (root / parts[0] / parts[1]).resolve()
        ctype = FILE_TYPES.get(target.suffix.lower())
        if ctype is None or not target.is_file() or root not in target.parents:
            return self._json(404, {"error": "not found"})
        self._send(200, target.read_bytes(), ctype)

    def _tailor(self, action: str, body: dict) -> None:
        svc, job_id = self.server.service, str(body.get("job_id") or "")
        jd = body.get("jd") if isinstance(body.get("jd"), str) else None
        if svc is None or not job_id:
            return self._json(400, {"error": "tailoring is not available"})
        if action == "estimate":
            return self._json(200, svc.estimate(job_id, jd))
        if not self.server.tailor_lock.acquire(blocking=False):
            return self._json(409, {"error": "a tailor run is already in progress"})
        try:
            out = svc.run(job_id, jd)
        finally:
            self.server.tailor_lock.release()
        self.server.prepared.set(job_id, out["folder"])
        self._json(200, out)

    def _eval(self) -> dict:
        state = build_state(self.server.data_dir, self.server.store)
        return compute(self.server.labels.load(), filtered_totals(state["filtered_out"]))

    def do_POST(self) -> None:
        if not self._guard():
            return
        if not origin_ok(self.headers.get("Origin"), self.server.port):
            return self._json(403, {"error": "bad origin"})
        if not hmac.compare_digest(self.headers.get("X-Dashboard-Token", ""), self.server.token):
            return self._json(403, {"error": "bad token"})
        path = urlsplit(self.path).path
        tailor_actions = {"/api/tailor/estimate": "estimate", "/api/tailor/run": "run"}
        prefix = "/api/labels/" if path.startswith("/api/labels/") else "/api/applications/"
        if path not in tailor_actions and not path.startswith(prefix):
            return self._json(404, {"error": "not found"})
        body = self._read_body()
        if body is None:
            return
        try:
            if path in tailor_actions:
                self._tailor(tailor_actions[path], body)
            else:
                (self._label if prefix == "/api/labels/" else self._apply)(path[len(prefix):], body)
        except StoreError as exc:
            self._json(400, {"error": str(exc)})
        except ServiceError as exc:
            self._json(exc.status, {"error": exc.message, **exc.extra})

    def _read_body(self) -> Optional[dict]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if not 0 < length <= MAX_BODY:
            self._json(400, {"error": "bad body length"})
            return None
        try:
            body = json.loads(self.rfile.read(length))
        except ValueError:
            body = None
        if not isinstance(body, dict):
            self._json(400, {"error": "body must be a JSON object"})
            return None
        return body

    def _label(self, job_id: str, body: dict) -> None:
        """Agent-side snapshot always comes from the server's own files, never from the client."""
        scored = snapshot_for(self.server.data_dir, job_id)
        if scored is not None:
            kind, snap = "scored", {"score": scored["score"], "verdict": scored["verdict"]}
        else:
            reason = filtered_snapshot_for(self.server.data_dir, job_id)
            if reason is None:
                return self._json(404, {"error": "unknown job"})
            kind, snap = "filtered", {"drop_reason": reason}
        entry = self.server.labels.set(job_id, kind=kind, label=body.get("label"), snapshot=snap,
                                       reason=body.get("reason"), note=body.get("note") or "")
        self._json(200, {"id": job_id, "entry": entry})

    def _apply(self, job_id: str, body: dict) -> None:
        store = self.server.store
        known = store.load().get(job_id)
        snapshot = snapshot_for(self.server.data_dir, job_id)
        if snapshot is None and known is None:
            return self._json(404, {"error": "unknown job"})
        prepared = self.server.prepared.load().get(job_id)
        entry = store.update(job_id, status=body.get("status"), notes=body.get("notes"),
                             mark_applied=bool(body.get("mark_applied")), snapshot=snapshot,
                             resume_version=prepared)
        self._json(200, {"id": job_id, **entry})


def make_server(port: int, data_dir: Path, token: Optional[str] = None, demo: bool = False,
                output_dir: Optional[Path] = None, service=None) -> DashboardServer:
    return DashboardServer(port, data_dir, token, demo, output_dir, service)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--data", type=Path, default=Path("data"))
    p.add_argument("--demo", action="store_true", help="synthetic sample data in a temp folder")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    data_dir = a.data
    if a.demo:
        from dashboard.demo import make_demo_dir
        data_dir = make_demo_dir()
        atexit.register(shutil.rmtree, data_dir, True)
        log.info("DEMO MODE: synthetic data, real data/ is not touched")
    if a.demo:
        from dashboard.service import DemoService
        out_dir = data_dir / "output" / "resumes"
        service = DemoService(data_dir, out_dir)
    else:
        from dashboard.service import TailorService
        out_dir = Path("output/resumes")
        service = TailorService(data_dir, out_dir, Path("master_profile.json"), Path("profile.yaml"))
    server = make_server(a.port, data_dir, demo=a.demo, output_dir=out_dir, service=service)
    log.info("Dashboard at http://127.0.0.1:%d  (Ctrl+C to stop)", server.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
