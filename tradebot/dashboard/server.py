"""A small, dependency-free web server for the dashboard.

  GET  /                  the dashboard page
  GET  /api/state         everything the page shows (token required)
  POST /api/approve       {"id": "<proposal id>"}  adopt a passed proposal
  POST /api/rollback      {"to": <version or null>}  go back a settings version

Every /api call needs the access token, sent as the X-Token header. The page
picks it up from the link the server prints (http://host:port/#token=...).
It listens on 127.0.0.1 by default; pass --host 0.0.0.0 to open it from a
phone on the same network. Approve and rollback change files under tuning/;
the running bot picks the new settings up the next time it starts.
"""

from __future__ import annotations

import hmac
import json
import logging
import secrets
import socket
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..improve.reviewer import approve
from ..improve.tuning import TuningError
from .state import DashboardState

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
MAX_BODY = 4096


def make_handler(state: DashboardState, token: str, demo: bool):
    class Handler(BaseHTTPRequestHandler):
        server_version = "tradebot-dashboard"

        def log_message(self, fmt, *args):  # quieter than the default
            log.debug("%s " + fmt, self.address_string(), *args)

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj, default=str).encode(), "application/json")

        def _authorized(self) -> bool:
            sent = self.headers.get("X-Token", "")
            return hmac.compare_digest(sent.encode(), token.encode())

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                html = (STATIC / "index.html").read_bytes()
                if demo:
                    html = html.replace(b"<html lang=\"en\">", b"<html lang=\"en\" data-force-demo=\"1\">", 1)
                return self._send(200, html, "text/html; charset=utf-8")
            if path == "/api/state":
                if not self._authorized():
                    return self._json(HTTPStatus.UNAUTHORIZED, {"error": "missing or wrong token"})
                try:
                    return self._json(200, state.build())
                except Exception as e:
                    log.exception("state build failed")
                    return self._json(500, {"error": f"{type(e).__name__}: {e}"})
            return self._json(404, {"error": "not found"})

        def do_POST(self):
            if not self._authorized():
                return self._json(HTTPStatus.UNAUTHORIZED, {"error": "missing or wrong token"})
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                return self._json(413, {"error": "body too large"})
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except json.JSONDecodeError:
                return self._json(400, {"error": "bad JSON"})
            _, store = state.config()
            try:
                if self.path == "/api/approve":
                    pid = str(body.get("id", ""))
                    if not pid or "/" in pid or "\\" in pid or pid.startswith("."):
                        return self._json(400, {"error": "bad proposal id"})
                    v = approve(store, pid)
                    log.info("dashboard: approved proposal %s as v%d", pid, v)
                    return self._json(200, {"ok": True, "version": v})
                if self.path == "/api/rollback":
                    to = body.get("to")
                    v = store.rollback(None if to is None else int(to))
                    log.info("dashboard: rolled back to v%d", v)
                    return self._json(200, {"ok": True, "version": v})
            except (TuningError, ValueError) as e:
                return self._json(409, {"error": str(e)})
            return self._json(404, {"error": "not found"})

    return Handler


def _lan_ip() -> str | None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return None


def serve(config_path: str | None, host: str = "127.0.0.1", port: int = 8765,
          demo: bool = False, token: str | None = None) -> None:
    token = token or secrets.token_urlsafe(18)
    state = DashboardState(config_path)
    httpd = ThreadingHTTPServer((host, port), make_handler(state, token, demo))
    shown = _lan_ip() if host in ("0.0.0.0", "") else host
    print(f"Dashboard{' (DEMO: simulated data)' if demo else ''}:")
    print(f"  http://{shown or host}:{port}/#token={token}")
    if host in ("0.0.0.0", ""):
        print("  Open that link on your phone (same Wi-Fi). Anyone with the link can view it.")
    print("Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
