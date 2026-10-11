"""Morph Studio: chat with (or edit) the robot and watch everything downstream regenerate.

  python -m morph.cli studio egg-picker      -> http://localhost:8765

Stdlib only (ThreadingHTTPServer + server-sent events), so it runs anywhere the
CLI runs. Each chat message or parameter edit becomes design changes; the
whole-robot pipeline streams stage-by-stage progress; the page then shows the
new CAD, simulations, electronics (schematic, PCB, harness, firmware),
manufacturing plan, BOM/sourcing and assembly manual against the previous
revision.
"""

from __future__ import annotations

import json
import mimetypes
import os
import queue
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from ..robot.pipeline2 import LABELS, RobotProject, params_for
from ..spec import Design
from .intent import OBJECTS, interpret, object_changes

STATIC = os.path.join(os.path.dirname(__file__), "static")


class Hub:
    def __init__(self):
        self.clients, self.lock = [], threading.Lock()
        self.history = []

    def subscribe(self):
        q = queue.Queue()
        with self.lock:
            self.clients.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.clients:
                self.clients.remove(q)

    def send(self, event: dict):
        if event.get("type") in ("user", "assistant"):
            self.history.append(event)
        with self.lock:
            for q in self.clients:
                q.put(event)


def _url(base: str, rel: str | None) -> str | None:
    return f"{base}/{rel}" if rel else None


def rev_payload(p: RobotProject, rev: str) -> dict:
    res, d = p.load_results(rev), p.load_design(rev)
    base = f"/files/{rev}"
    cad = res.get("cad") or {}
    sims = {}
    for name, s in ((res.get("sims") or {}).get("scenarios") or {}).items():
        sims[name] = {"url": _url(f"{base}/sim", s.get("file")), "title": s.get("title") or name.replace("_", " "),
                      "metrics": s.get("metrics", {})}
    el = dict(res.get("electronics") or {})
    if el:
        el["base"] = f"{base}/electronics"
        el["plan"] = res.get("eplan")
    mfg = dict(res.get("manufacturing") or {})
    if mfg:
        mfg["base"] = f"{base}/mfg"
    man = dict(res.get("manual") or {})
    if man:
        man["json"] = _url(f"{base}/manual", man.get("json"))
        man["html"] = _url(f"{base}/manual", man.get("html"))
    grip = res.get("gripper") or {}
    return {
        "rev": rev, "label": res.get("label"), "parent": res.get("parent"), "changes": res.get("changes") or {},
        "provenance": res.get("provenance") or {}, "timings": res.get("timings_s") or {},
        "nodes": [list(x) for x in LABELS], "design": d.flat(), "params": params_for(d),
        "summary": res.get("summary") or {}, "errors": res.get("errors") or {},
        "robot": {"json": f"{base}/cad/robot.json", "glb": f"{base}/cad/robot.glb",
                  "urdf": _url(base, cad.get("urdf"))} if cad else None,
        "cad": cad, "sizing": res.get("sizing"), "sims": sims, "electronics": el or None,
        "manufacturing": mfg or None, "bom": res.get("bom"), "manual": man or None,
        "gripper": {"analysis": grip.get("analysis"), "sim": grip.get("sim"), "force": grip.get("force")},
    }


class Studio:
    def __init__(self, project: RobotProject):
        self.p, self.hub, self.busy = project, Hub(), threading.Lock()

    def state(self):
        rev = self.p.latest()
        out = {"project": os.path.basename(self.p.root), "revs": self.p.revs(), "history": self.hub.history}
        if rev:
            out["current"] = rev_payload(self.p, rev)
            parent = out["current"]["parent"]
            out["previous"] = rev_payload(self.p, parent) if parent else None
        return out

    def _expand(self, changes: dict) -> dict:
        """Object presets fan out into dims/mass/friction/damage force + finger fit."""
        name = changes.get("object.name")
        if isinstance(name, str) and name in OBJECTS and len(changes) == 1:
            return object_changes(name)
        return changes

    def apply(self, changes: dict, label: str, user_text: str | None = None, reply: str | None = None, source=""):
        if not self.busy.acquire(blocking=False):
            self.hub.send({"type": "assistant", "text": "Still regenerating the last change; one moment."})
            return
        try:
            if user_text:
                self.hub.send({"type": "user", "text": user_text})
            base = self.p.latest()
            cur = self.p.load_design(base)
            changes = self._expand(changes)
            try:
                design = cur.with_changes(changes)
            except (KeyError, ValueError) as exc:
                self.hub.send({"type": "assistant", "text": f"Couldn't apply that: {exc}"})
                return
            if reply:
                self.hub.send({"type": "assistant", "text": reply, "changes": changes, "source": source})
            t0 = time.time()
            rev = self.p.run(design, parent=base, label=label[:48], verbose=False, on_event=self.hub.send)
            self.p.log({"type": "tool_time", "cmd": "studio", "seconds": round(time.time() - t0, 2)})
            self.hub.send({"type": "done", "current": rev_payload(self.p, rev),
                           "previous": rev_payload(self.p, base), "seconds": round(time.time() - t0, 1)})
        except Exception as exc:
            traceback.print_exc()
            self.hub.send({"type": "assistant", "text": f"Error: {exc}"})
        finally:
            self.busy.release()

    def chat(self, msg: str):
        base = self.p.latest()
        cur = self.p.load_design(base)
        it = interpret(msg, cur, (self.p.load_results(base).get("summary") or {}).get("grip_target_n"))
        if not it["changes"]:
            self.hub.send({"type": "user", "text": msg})
            self.hub.send({"type": "assistant", "text": it["reply"]})
            return
        self.apply(it["changes"], msg, user_text=msg, reply=it["reply"], source=it["source"])

    def params(self, changes: dict):
        desc = ", ".join(f"{k.split('.')[-1].replace('_', ' ')} → {v}" for k, v in changes.items())
        self.apply(changes, f"edit: {desc}", reply=f"Parameter edit: {desc}.")


def make_handler(studio: Studio):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body: bytes, ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _file(self, path):
            if not os.path.isfile(path):
                return self._send(404, b"not found", "text/plain")
            ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
            if path.endswith((".js", ".mjs")):
                ctype = "text/javascript"
            elif path.endswith(".glb"):
                ctype = "model/gltf-binary"
            elif path.endswith((".ino", ".nc", ".gbr", ".drl", ".csv", ".urdf", ".xml", ".dxf")):
                ctype = "text/plain; charset=utf-8"
            with open(path, "rb") as fh:
                self._send(200, fh.read(), ctype)

        def _safe(self, root, rel):
            full = os.path.normpath(os.path.join(root, unquote(rel)))
            return full if full.startswith(os.path.normpath(root)) else None

        def do_GET(self):
            u = urlparse(self.path)
            if u.path in ("/", "/index.html"):
                return self._file(os.path.join(STATIC, "index.html"))
            if u.path.startswith("/static/"):
                full = self._safe(STATIC, u.path[len("/static/"):])
                return self._file(full) if full else self._send(403, b"", "text/plain")
            if u.path.startswith("/files/"):
                full = self._safe(studio.p.root, u.path[len("/files/"):])
                return self._file(full) if full else self._send(403, b"", "text/plain")
            if u.path == "/api/state":
                return self._send(200, json.dumps(studio.state()).encode())
            if u.path == "/api/rev":
                rev = parse_qs(u.query).get("rev", [None])[0]
                if rev not in studio.p.revs():
                    return self._send(404, b'{"error":"unknown rev"}')
                return self._send(200, json.dumps(rev_payload(studio.p, rev)).encode())
            if u.path == "/api/events":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                q = studio.hub.subscribe()
                try:
                    while True:
                        try:
                            ev = q.get(timeout=15)
                            self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
                        except queue.Empty:
                            self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    studio.hub.unsubscribe(q)
                return
            self._send(404, b"not found", "text/plain")

        def do_POST(self):
            u = urlparse(self.path)
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n) or b"{}")
            if u.path == "/api/chat":
                msg = (body.get("message") or "").strip()
                if msg:
                    threading.Thread(target=studio.chat, args=(msg,), daemon=True).start()
                return self._send(202, b'{"ok":true}')
            if u.path == "/api/params":
                changes = body.get("changes") or {}
                if changes:
                    threading.Thread(target=studio.params, args=(changes,), daemon=True).start()
                return self._send(202, b'{"ok":true}')
            self._send(404, b"not found", "text/plain")

    return H


def serve(project: RobotProject, host="127.0.0.1", port=8765):
    if not project.revs():
        raise SystemExit(f"{project.root} has no revisions yet: run `python -m morph.cli robot new {os.path.basename(project.root)}` first")
    studio = Studio(project)
    httpd = ThreadingHTTPServer((host, port), make_handler(studio))
    httpd.daemon_threads = True
    print(f"Morph Studio: http://{host}:{port}  (project {project.root}; Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
