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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urlsplit

from dashboard.data import build_state, snapshot_for
from dashboard.store import ApplicationsStore, StoreError

log = logging.getLogger("dashboard")
BIND_HOST = "127.0.0.1"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1"}
STATIC = Path(__file__).parent / "static"
STATIC_FILES = {"/app.css": ("app.css", "text/css; charset=utf-8"),
                "/app.js": ("app.js", "text/javascript; charset=utf-8")}
MAX_BODY = 64 * 1024
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
       "img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int, data_dir: Path, token: Optional[str] = None, demo: bool = False):
        self.data_dir = data_dir
        self.demo = demo
        self.token = token or secrets.token_urlsafe(32)
        self.store = ApplicationsStore(data_dir / "clean" / "applications.json")
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
        elif path == "/api/state":
            self._json(200, {**build_state(self.server.data_dir, self.server.store),
                             "demo": self.server.demo})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self._guard():
            return
        if not origin_ok(self.headers.get("Origin"), self.server.port):
            return self._json(403, {"error": "bad origin"})
        if not hmac.compare_digest(self.headers.get("X-Dashboard-Token", ""), self.server.token):
            return self._json(403, {"error": "bad token"})
        prefix = "/api/applications/"
        path = urlsplit(self.path).path
        if not path.startswith(prefix):
            return self._json(404, {"error": "not found"})
        body = self._read_body()
        if body is None:
            return
        job_id = path[len(prefix):]
        try:
            self._apply(job_id, body)
        except StoreError as exc:
            self._json(400, {"error": str(exc)})

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

    def _apply(self, job_id: str, body: dict) -> None:
        store = self.server.store
        known = store.load().get(job_id)
        snapshot = snapshot_for(self.server.data_dir, job_id)
        if snapshot is None and known is None:
            return self._json(404, {"error": "unknown job"})
        entry = store.update(job_id, status=body.get("status"), notes=body.get("notes"),
                             mark_applied=bool(body.get("mark_applied")), snapshot=snapshot)
        self._json(200, {"id": job_id, **entry})


def make_server(port: int, data_dir: Path, token: Optional[str] = None,
                demo: bool = False) -> DashboardServer:
    return DashboardServer(port, data_dir, token, demo)


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
    server = make_server(a.port, data_dir, demo=a.demo)
    log.info("Dashboard at http://127.0.0.1:%d  (Ctrl+C to stop)", server.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
