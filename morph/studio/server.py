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
import shutil
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from ..robot.pipeline2 import LABELS, RobotProject, params_for, pipeline_version
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


def _sourcing(p: RobotProject, rev: str) -> dict | None:
    """<rev>/sourcing_darwin.json, trimmed to what the BOM tab shows."""
    path = os.path.join(p.rev_dir(rev), "sourcing_darwin.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            rep = json.load(fh)
    except (OSError, ValueError):
        return None
    ag = lambda a: {"name": a.get("name"), "url": a.get("url"), "can_start": a.get("can_start"),
                    "description": (a.get("description") or "")[:160]}
    rows = [{"kind": r.get("kind"), "item": r.get("item"), "query": r.get("query"), "files": (r.get("files") or [])[:3],
             "n_files": len(r.get("files") or []), "gap": r.get("gap"), "cached": r.get("cached"),
             "error": (r.get("error") or "")[:120] or None, "n_agents": len(r.get("agents") or []),
             "picked": [ag(a) for a in r.get("picked") or []], "agents": [ag(a) for a in (r.get("agents") or [])[:4]]}
            for r in rep.get("rows") or []]
    return {"rev": rep.get("rev"), "rows": rows, "gaps": rep.get("gaps") or [], "note": rep.get("note"),
            "searched": time.strftime("%H:%M", time.localtime(os.path.getmtime(path)))}


def _design(p: RobotProject, rev: str) -> dict | None:
    try:
        return p.load_design(rev).to_dict()
    except (OSError, ValueError, KeyError):
        return None


def baseline(p: RobotProject) -> str | None:
    """The newest clean build of the project's ORIGINAL design (its first revision's): a revision with no
    parent whose design equals r001's. Later parentless revisions can carry an earlier take's edits
    (the start-up rebuild rebuilds the latest design), so they don't count."""
    revs = p.revs()
    if not revs:
        return None
    orig = _design(p, revs[0])
    clean = [r for r in revs if not (Studio._results(p, r) or {}).get("parent") and _design(p, r) == orig]
    return clean[-1] if clean else revs[0]


def _remove_rev(p: RobotProject, rev: str) -> bool:
    """Delete a revision folder, all or nothing: rename it out of the project first (Windows refuses
    while any file inside is open, and then nothing has been touched), then delete the renamed copy.
    revs() only lists names like r012, so a leftover "r012.deleted-..." folder is invisible."""
    d = p.rev_dir(rev)
    trash = f"{d}.deleted-{time.time_ns()}"
    try:
        os.rename(d, trash)
    except OSError:
        return False
    shutil.rmtree(trash, ignore_errors=True)
    return True


def is_robot_project(root: str) -> bool:
    try:
        with open(os.path.join(root, "meta.json"), encoding="utf-8") as fh:
            return json.load(fh).get("kind") == "robot"
    except (OSError, ValueError):  # a plain file (desktop.ini), a gripper project, anything else
        return False


def integrations() -> dict:
    """Which sponsor services are configured on this machine (keys come from the environment)."""
    env = os.environ.get
    return {"keewano": {"send": bool(env("KEEWANO_API_KEY")), "query": bool(env("KEEWANO_PAT") and env("KEEWANO_PROJECT_ID"))},
            "darwin": {"search": True}}


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
    try:
        from ..robot import timestudy2
        tbase = timestudy2.load_baseline()
        time_card = {"revision": timestudy2.revision(res, tbase), "project": timestudy2.project(p, tbase),
                     "placeholder": "PLACEHOLDER" in str(tbase.get("status", ""))}
    except Exception as exc:  # the time card must never break the payload
        time_card = {"error": str(exc)}
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
        "time_study": time_card, "sourcing": _sourcing(p, rev),
    }


class Studio:
    def __init__(self, project: RobotProject, home: str | None = None):
        self.p, self.hub, self.busy = project, Hub(), threading.Lock()
        self.home = home or os.path.dirname(project.root)
        self.start = os.path.basename(project.root)
        self.sourcing = threading.Lock()

    def projects(self) -> list:
        out = []
        for name in sorted(os.listdir(self.home)) if os.path.isdir(self.home) else []:
            root = os.path.join(self.home, name)
            meta = os.path.join(root, "meta.json")
            if os.path.isfile(meta) and is_robot_project(root) and RobotProject(root).revs():
                m = json.load(open(meta, encoding="utf-8"))
                if m.get("kind") == "robot":
                    rp = RobotProject(root)
                    out.append({"name": name, "source": m.get("source") or "", "need": m.get("need", ""),
                                "at_start": rp.latest() == baseline(rp)})
        return out

    def switch(self, name: str) -> bool:
        root = os.path.join(self.home, name)
        if not RobotProject(root).revs() or self.busy.locked():
            return False
        self.p = RobotProject(root)
        self.hub.history.clear()
        self.hub.send({"type": "project", "project": name})
        return True

    def state(self):
        rev = self.p.latest()
        out = {"project": os.path.basename(self.p.root), "projects": self.projects(), "revs": self.p.revs(),
               "history": self.hub.history, "integrations": integrations()}
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
            if design.to_dict() == cur.to_dict():
                self.hub.send({"type": "assistant", "noop": True, "changes": changes,
                               "text": f"Nothing to change: {base} already has that, so nothing was regenerated."})
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
            self.hub.send({"type": "assistant", "text": it["reply"], "noop": True})
            return
        self.apply(it["changes"], msg, user_text=msg, reply=it["reply"], source=it["source"])

    def source(self, rev: str | None = None):
        """Darwin: shortlist agents for every purchased part and fab handoff of a revision, streamed."""
        from .. import darwin
        if not self.sourcing.acquire(blocking=False):
            return
        p = self.p
        rev = rev if rev in p.revs() else p.latest()
        try:
            self.hub.send({"type": "sourcing", "status": "start", "rev": rev, "project": os.path.basename(p.root)})
            name = os.path.basename(p.root)
            prog = lambda n, tot, t: self.hub.send({"type": "sourcing", "status": "progress", "rev": rev, "project": name,
                                                     "done": n, "total": tot, **t})
            # one 15 s try per lookup, then the cached answer: the Studio never hangs on a slow Darwin
            darwin.source(p, rev, workers=6, on_progress=prog, timeout=15, retries=0)
            self.hub.send({"type": "sourcing", "status": "done", "rev": rev, "project": os.path.basename(p.root),
                           "sourcing": _sourcing(p, rev)})
        except Exception as exc:
            traceback.print_exc()
            self.hub.send({"type": "sourcing", "status": "error", "rev": rev, "project": os.path.basename(p.root), "error": str(exc)})
        finally:
            self.sourcing.release()

    def reset(self) -> bool:
        """Demo reset: every project goes back to a clean build of its original design (see baseline());
        the revisions after it are deleted, like deleting the projects folder but keeping the first
        build. If that build is from older code it is rebuilt now (about 30 s per robot)."""
        if not self.busy.acquire(blocking=False):
            return False
        try:
            for name in sorted(os.listdir(self.home)):
                root = os.path.join(self.home, name)
                if not is_robot_project(root):
                    continue
                p = RobotProject(root)
                keep = baseline(p)
                if not keep:
                    continue
                revs = p.revs()
                complete = True
                for r in reversed(revs[revs.index(keep) + 1:]):  # newest first, so what is left stays contiguous
                    if not _remove_rev(p, r):
                        complete = False  # a file in it is open (Excel, a viewer): keep the rest intact
                        break
                if complete and (self._results(p, keep) or {}).get("pipeline_version") != pipeline_version():
                    keep = p.run(p.load_design(keep), parent=None, label="rebuilt with latest Morph", verbose=False)
                p.log({"type": "demo_reset", "rev": keep, "complete": complete})
            self.p = RobotProject(os.path.join(self.home, self.start))
            self.hub.history.clear()
            self.hub.send({"type": "project", "project": self.start})
            return True
        finally:
            self.busy.release()

    @staticmethod
    def _results(p, r):
        try:
            return p.load_results(r)
        except (OSError, ValueError):
            return None

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
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):  # tab closed (Windows: 10053)
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
            if u.path == "/api/project":
                ok = studio.switch(str(body.get("name", "")))
                return self._send(200 if ok else 409, json.dumps({"ok": ok}).encode())
            if u.path == "/api/source":
                threading.Thread(target=studio.source, args=(body.get("rev"),), daemon=True).start()
                return self._send(202, b'{"ok":true}')
            if u.path == "/api/reset":
                ok = studio.reset()
                return self._send(200 if ok else 409, json.dumps({"ok": ok}).encode())
            if u.path == "/api/params":
                changes = body.get("changes") or {}
                if changes:
                    threading.Thread(target=studio.params, args=(changes,), daemon=True).start()
                return self._send(202, b'{"ok":true}')
            self._send(404, b"not found", "text/plain")

    return H


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second Studio bind the same port while this one still listens
    allow_reuse_address = os.name != "nt"

    def handle_error(self, request, client_address):
        # a browser tab closing mid-response is normal; don't print a traceback for it
        import sys
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)


def serve(project: RobotProject, host="127.0.0.1", port=8765, home: str | None = None):
    if not project.revs():
        raise SystemExit(f"{project.root} has no revisions yet: run `python -m morph.cli robot new {os.path.basename(project.root)}` first")
    studio = Studio(project, home)
    try:
        httpd = _Server((host, port), make_handler(studio))
    except OSError as exc:
        raise SystemExit(f"Port {port} is busy ({exc}): Morph Studio is probably already running. "
                         "Close its CMD window, then start again.")
    httpd.daemon_threads = True
    print(f"Morph Studio: http://{host}:{port}  (project {project.root}; Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
