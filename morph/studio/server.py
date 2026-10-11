"""Morph Studio: chat with the design and watch everything downstream regenerate.

  python -m morph.cli studio raw-egg        -> http://localhost:8765

Stdlib only (ThreadingHTTPServer + server-sent events), so it runs anywhere
the CLI runs. Each chat message becomes parameter changes; the pipeline
streams node-by-node progress; the page animates the new CAD, board, fab
files, firmware and metrics against the previous revision.
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

from .. import agent
from ..design import build_profile
from ..mechanism import object_center_v
from ..pipeline import Project
from ..spec import Design
from .intent import interpret

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


def rev_payload(p: Project, rev: str) -> dict:
    res, d = p.load_results(rev), p.load_design(rev)
    geom = build_profile(d)
    base = f"/files/{rev}"
    cad_files = [f"{base}/cad/finger_{n}.stl" for n in geom.parts] if geom.parts else [f"{base}/cad/finger.stl"]
    e = res.get("electronics") or {}
    fw_text = ""
    if e.get("firmware"):
        with open(os.path.join(p.rev_dir(rev), e["firmware"])) as fh:
            fw_text = fh.read()
    fab = res.get("fab") or {}
    gif = os.path.join(p.rev_dir(rev), "sim", "grasp.gif")
    mech = res.get("mech") or {}
    mech_parts = [{"name": f[:-4], "url": f"{base}/mech/{f}"} for f in mech.get("files", [])]
    return {
        "rev": rev, "label": res.get("label"), "parent": res.get("parent"), "changes": res.get("changes"),
        "provenance": res.get("provenance"), "timings": res.get("timings_s"),
        "design": d.flat(), "summary": res["summary"],
        "geom": {"parts": cad_files, "center_v": round(object_center_v(d, geom), 3), "pad_center_u": geom.pad_center_u,
                 "R": d.object.diameter_mm / 2, "shape": d.object.shape, "obj_len": d.object.length_mm,
                 "width": d.finger.width_mm, "object": d.object.name, "sensor": geom.sensor,
                 "profile": res["cad"]["profile"]},
        "electronics": {k: e.get(k) for k in ("mcu", "servo", "sensor", "target_force_n", "window_n", "servo_stall_force_n",
                                              "limit_n", "open_loop_crushes", "object_limit_n", "wiring", "parts",
                                              "sensing", "issues", "warnings", "board")},
        "board_url": f"{base}/electronics/board/board.json" if e.get("board") else None,
        "firmware_url": f"{base}/{e['firmware']}" if e.get("firmware") else None, "firmware": fw_text,
        "fab": {"process": fab.get("process"), "minutes": fab.get("minutes"), "cost": fab.get("total_cost"),
                "files": [f"{base}/fab/{f}" for f in fab.get("files", [])], "dfm": fab.get("dfm_warnings", [])},
        "mech": {k: mech.get(k) for k in ("servo", "teeth", "pitch_radius_mm", "travel_mm", "opening_mm", "servo_open_deg",
                                          "servo_closed_deg", "servo_sweep_deg", "envelope_mm", "issues", "notes")},
        "mech_parts": mech_parts,
        "bom": res.get("bom"), "gif": f"{base}/sim/grasp.gif" if os.path.exists(gif) else None,
    }


class Studio:
    def __init__(self, project: Project):
        self.p, self.hub, self.busy = project, Hub(), threading.Lock()

    def state(self):
        rev = self.p.latest()
        out = {"project": os.path.basename(self.p.root), "revs": self.p.revs(), "history": self.hub.history}
        if rev:
            out["current"] = rev_payload(self.p, rev)
            parent = out["current"]["parent"]
            out["previous"] = rev_payload(self.p, parent) if parent else None
        return out

    def chat(self, msg: str):
        if not self.busy.acquire(blocking=False):
            self.hub.send({"type": "assistant", "text": "Still regenerating the last change; one moment."})
            return
        try:
            self.hub.send({"type": "user", "text": msg})
            base = self.p.latest()
            if base is None or msg.lower().startswith("new:"):
                design, need, src = agent.propose(msg.split(":", 1)[-1])
                self.hub.send({"type": "assistant", "text": f"New design for {need.object_name}. {need.rationale}"})
                parent, label = None, "new design"
            else:
                cur = self.p.load_design(base)
                last_target = self.p.load_results(base)["summary"].get("target_force_n")
                it = interpret(msg, cur, last_target)
                if not it["changes"]:
                    self.hub.send({"type": "assistant", "text": it["reply"]})
                    return
                try:
                    design = cur.with_changes(it["changes"])
                except (KeyError, ValueError) as exc:
                    self.hub.send({"type": "assistant", "text": f"Couldn't apply that: {exc}"})
                    return
                self.hub.send({"type": "assistant", "text": it["reply"], "changes": it["changes"], "source": it["source"]})
                parent, label = base, msg[:48]
            t0 = time.time()
            rev = self.p.run(design, parent=parent, label=label, verbose=False, on_event=self.hub.send)
            self.p.log({"type": "tool_time", "cmd": "studio", "seconds": round(time.time() - t0, 2)})
            payload = rev_payload(self.p, rev)
            self.hub.send({"type": "done", "current": payload,
                           "previous": rev_payload(self.p, parent) if parent else None,
                           "seconds": round(time.time() - t0, 1)})
            prov = payload["provenance"] or {}
            if prov.get("sim") == "computed" or prov.get("cad") == "computed" or not payload["gif"]:
                threading.Thread(target=self._gif, args=(rev,), daemon=True).start()
        except Exception as exc:
            traceback.print_exc()
            self.hub.send({"type": "assistant", "text": f"Error: {exc}"})
        finally:
            self.busy.release()

    def _gif(self, rev):
        from .. import sim

        d = self.p.load_design(rev)
        out = os.path.join(self.p.rev_dir(rev), "sim", "grasp.gif")
        if sim.render_gif(d, build_profile(d), out):
            self.hub.send({"type": "gif", "rev": rev, "url": f"/files/{rev}/sim/grasp.gif"})


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
            with open(path, "rb") as fh:
                self._send(200, fh.read(), ctype)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path in ("/", "/index.html"):
                return self._file(os.path.join(STATIC, "index.html"))
            if u.path.startswith("/static/"):
                rel = os.path.normpath(unquote(u.path[len("/static/"):]))
                if rel.startswith(".."):
                    return self._send(403, b"", "text/plain")
                return self._file(os.path.join(STATIC, rel))
            if u.path.startswith("/files/"):
                rel = os.path.normpath(unquote(u.path[len("/files/"):]))
                if rel.startswith(".."):
                    return self._send(403, b"", "text/plain")
                return self._file(os.path.join(studio.p.root, rel))
            if u.path == "/api/state":
                return self._send(200, json.dumps(studio.state()).encode())
            if u.path == "/api/rev":
                rev = parse_qs(u.query).get("rev", [None])[0]
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
            self._send(404, b"not found", "text/plain")

    return H


def serve(project: Project, host="127.0.0.1", port=8765):
    studio = Studio(project)
    httpd = ThreadingHTTPServer((host, port), make_handler(studio))
    httpd.daemon_threads = True
    print(f"Morph Studio: http://{host}:{port}  (project {project.root}; Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
