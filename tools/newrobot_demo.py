"""Standalone New robot demo: the real brief engine behind a mock Studio shell, for development and browser tests.

    python tools/newrobot_demo.py [--port 8800] [--home DIR]      ->  http://127.0.0.1:8800/

Serves tools/newrobot_demo.html at / and the Studio's static files at /static/, and answers POST/GET /api/brief with
morph.studio.api_brief (turns run inline; the events they emit come back in the reply as "_events" and the page replays
them like the SSE stream). The build itself is simulated by the page: no pipeline runs. Loopback only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
if REPO not in sys.path:
    sys.path.insert(0, REPO)
from morph.studio import api_brief  # noqa: E402

STATIC = os.path.join(REPO, "morph", "studio", "static")
TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".json": "application/json", ".svg": "image/svg+xml", ".png": "image/png"}


def make_handler(home: str):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, status, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _file(self, root, rel):
            root = os.path.realpath(root)
            p = os.path.realpath(os.path.join(root, rel))
            if os.path.commonpath([root, p]) != root or not os.path.isfile(p):
                return self._send(404, {"ok": False, "error": "not found"})
            with open(p, "rb") as fh:
                return self._send(200, fh.read(), TYPES.get(os.path.splitext(p)[1], "application/octet-stream"))

        def _req(self, body=None, query=None):
            return SimpleNamespace(method=self.command, path="/api/brief", body=body, query=query or {}, member=None, handler=self,
                                   studio=SimpleNamespace(home=home), room=SimpleNamespace(name="demo", hub=None))

        def do_GET(self):
            u = urlparse(self.path)
            if u.path in ("/", "/index.html"):
                return self._file(HERE, "newrobot_demo.html")
            if u.path.startswith("/static/"):
                return self._file(STATIC, u.path[len("/static/"):])
            if u.path == "/api/brief":
                st, out = api_brief.get(self._req(query=parse_qs(u.query)), home=home)
                return self._send(st, out)
            return self._send(404, {"ok": False, "error": "not found"})

        def do_POST(self):
            u = urlparse(self.path)
            n = int(self.headers.get("Content-Length") or 0)
            if n > 1 << 20:
                return self._send(413, {"ok": False, "error": "too large"})
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._send(400, {"ok": False, "error": "bad JSON"})
            if u.path == "/api/brief":
                events = []
                st, out = api_brief.post(self._req(body), events.append, home=home, sync=True,
                                         create_project=lambda pay: {"project": pay["name"]})
                return self._send(st, dict(out, _events=events))
            if u.path == "/api/params":
                return self._send(202, {"ok": True})
            return self._send(404, {"ok": False, "error": "not found"})

    return H


def serve(port: int = 8800, home: str | None = None) -> ThreadingHTTPServer:
    home = home or tempfile.mkdtemp(prefix="morph-newrobot-")
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(home))
    srv.daemon_threads = True
    return srv


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8800)
    ap.add_argument("--home", help="where brief drafts go (default: a temporary folder)")
    a = ap.parse_args()
    s = serve(a.port, a.home)
    print(f"New robot demo: http://127.0.0.1:{a.port}/  (Ctrl+C to stop)")
    try:
        s.serve_forever()
    except KeyboardInterrupt:
        pass
