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
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..robot.pipeline2 import LABELS, RobotProject, params_for, pipeline_version
from ..spec import Design
from .intent import OBJECTS, interpret, object_changes
from .security import MAX_BODY, MAX_MESSAGE, LocalSession, contained_path, project_name

STATIC = os.path.join(os.path.dirname(__file__), "static")


class Hub:
    MAX_CLIENTS = 8
    MAX_EVENTS = 128

    def __init__(self):
        self.clients, self.lock = [], threading.Lock()
        self.history = []

    def subscribe(self):
        q = queue.Queue(maxsize=self.MAX_EVENTS)
        with self.lock:
            if len(self.clients) >= self.MAX_CLIENTS:
                return None
            self.clients.append(q)
        return q

    def unsubscribe(self, q):
        with self.lock:
            if q in self.clients:
                self.clients.remove(q)

    def send(self, event: dict):
        with self.lock:
            if event.get("type") in ("user", "assistant"):
                self.history.append(event)
                del self.history[:-200]
            for q in self.clients:
                try:
                    q.put_nowait(event)
                except queue.Full:
                    # A slow tab must not block the build or grow memory indefinitely.
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        pass
                    q.put_nowait({"type": "resync"})


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
    from .. import settings
    env = os.environ.get
    return {"keewano": {"send": bool(env("KEEWANO_API_KEY")) and settings.telemetry_enabled(),
                        "query": False, "calibration": "disabled for local beta"},
            "darwin": {"search": settings.sourcing_enabled()}}


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
    def __init__(self, project: RobotProject, home: str | None = None, demo_mode: bool = False):
        self.p, self.hub, self.busy = project, Hub(), threading.Lock()
        self.home = str(Path(home or os.path.dirname(project.root)).resolve())
        self.start = os.path.basename(project.root)
        self.sourcing = threading.Lock()
        self.demo_mode = demo_mode
        self.session = LocalSession()

    def projects(self) -> list:
        out = []
        for name in sorted(os.listdir(self.home)) if os.path.isdir(self.home) else []:
            if name.startswith("."):
                continue  # hidden transactional creation directories never enter the registry
            root = contained_path(self.home, name)
            if root is None:
                continue
            meta = os.path.join(root, "meta.json")
            if os.path.isfile(meta) and is_robot_project(root) and RobotProject(root).revs():
                try:
                    with open(meta, encoding="utf-8") as fh:
                        m = json.load(fh)
                except (OSError, ValueError):
                    continue
                if m.get("kind") == "robot":
                    rp = RobotProject(root)
                    out.append({"name": name, "source": m.get("source") or "", "need": m.get("need", ""),
                                "at_start": rp.latest() == baseline(rp)})
        return out

    def switch(self, name: str) -> bool:
        if not self.busy.acquire(blocking=False):
            return False
        try:
            # Resolve only names already in the registry. Project() creates its root on init.
            if self.sourcing.locked() or name not in {p["name"] for p in self.projects()}:
                return False
            root = contained_path(self.home, name)
            if root is None:
                return False
            self.p = RobotProject(root)
            self.hub.history.clear()
            self.hub.send({"type": "project", "project": name})
            return True
        finally:
            self.busy.release()

    def state(self):
        p = self.p
        rev = p.latest()
        out = {"project": os.path.basename(p.root), "projects": self.projects(), "revs": p.revs(),
               "history": list(self.hub.history), "integrations": integrations(),
               "demo_mode": self.demo_mode, "busy": self.busy.locked()}
        if rev:
            out["current"] = rev_payload(p, rev)
            parent = out["current"]["parent"]
            out["previous"] = rev_payload(p, parent) if parent else None
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
            self._apply(self.p, changes, label, user_text, reply, source)
        finally:
            self.busy.release()

    def _apply(self, p, changes, label, user_text=None, reply=None, source=""):
        if user_text:
            self.hub.send({"type": "user", "text": user_text})
        base = p.latest()
        if base is None:
            self.hub.send({"type": "assistant", "text": "Create a project first."})
            return
        cur = p.load_design(base)
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
        rev = p.run(design, parent=base, label=label[:48], verbose=False, on_event=self.hub.send)
        p.log({"type": "tool_time", "cmd": "studio", "seconds": round(time.time() - t0, 2)})
        self.hub.send({"type": "done", "current": rev_payload(p, rev),
                       "previous": rev_payload(p, base), "seconds": round(time.time() - t0, 1)})

    def chat(self, msg: str):
        if not self.busy.acquire(blocking=False):
            return False
        try:
            return self._chat(self.p, msg)
        finally:
            self.busy.release()

    def _chat(self, p, msg):
        if "sk-ant-" in msg.lower():
            self.hub.send({"type": "assistant", "text": "Enter API keys in Settings, never in chat. This message was not saved or sent."})
            return False
        base = p.latest()
        if base is None:
            self.hub.send({"type": "assistant", "text": "Create a project first."})
            return False
        cur = p.load_design(base)
        it = interpret(msg, cur, (p.load_results(base).get("summary") or {}).get("grip_target_n"))
        if not it["changes"]:
            self.hub.send({"type": "user", "text": msg})
            self.hub.send({"type": "assistant", "text": it["reply"], "noop": True})
            return
        self._apply(p, it["changes"], msg, user_text=msg, reply=it["reply"], source=it["source"])

    def submit(self, kind: str, value) -> bool:
        """Reserve one build slot BEFORE interpreting a prompt or spawning a worker."""
        if not self.busy.acquire(blocking=False):
            return False
        if self.sourcing.locked():
            self.busy.release()
            return False
        p = self.p  # immutable target, captured while project switches are excluded
        def work():
            try:
                if kind == "chat":
                    self._chat(p, value)
                elif kind == "params":
                    self._params(p, value)
                elif kind in ("create", "duplicate"):
                    self._create(p, value, duplicate=kind == "duplicate")
            except Exception:
                self.hub.send({"type": "assistant", "text": "The operation failed. Your existing revisions were preserved; retry or create a new project."})
                self.hub.send({"type": "job_error"})
            finally:
                completed_project = os.path.basename(self.p.root)
                self.busy.release()
                self.hub.send({"type": "job_idle", "project": completed_project, "kind": kind})
        try:
            threading.Thread(target=work, daemon=True).start()
        except Exception:
            self.busy.release()
            raise
        return True

    def _create(self, current, options, duplicate=False):
        name = project_name(options["name"])
        root = contained_path(self.home, name)
        if root is None or os.path.exists(root):
            raise ValueError("A project with that name already exists.")
        if duplicate:
            if not current.revs():
                raise ValueError("Choose an existing project to duplicate.")
            base = Path(current.root).resolve()
            # Never follow symlinks/junctions into another project or a credential directory.
            for walk_root, dirs, files in os.walk(base):
                for child in dirs + files:
                    item = Path(walk_root) / child
                    if item.is_symlink() or item.resolve() != item.absolute() or not item.resolve().is_relative_to(base):
                        raise ValueError("Projects containing linked files cannot be duplicated.")
        # Build/copy outside the registry, then publish atomically. Failed creation leaves
        # no half-project selected and the requested name is immediately available to retry.
        staging = contained_path(self.home, f".creating-{uuid.uuid4().hex}")
        try:
            if duplicate:
                shutil.copytree(current.root, staging)
            else:
                from ..cli import EGG_ROBOT
                source = "so101" if options.get("example") == "so101" else ""
                os.mkdir(staging)
                p = RobotProject(staging)
                with open(os.path.join(staging, "meta.json"), "w", encoding="utf-8") as fh:
                    json.dump({"kind": "robot", "source": source, "need": "Pick an egg and place it without cracking it."}, fh)
                design = Design().with_changes({**EGG_ROBOT, "name": name, "arm.source": source})
                p.run(design, label="first design", verbose=False, on_event=self.hub.send)
            if os.path.exists(root):
                raise ValueError("A project with that name already exists.")
            os.rename(staging, root)
        finally:
            if staging and os.path.isdir(staging) and Path(staging).resolve().parent == Path(self.home):
                shutil.rmtree(staging)
        self.p = RobotProject(root)
        self.hub.history.clear()
        self.hub.send({"type": "project", "project": name})

    def source(self, rev: str | None = None):
        """Darwin: shortlist agents for every purchased part and fab handoff of a revision, streamed."""
        if not self.sourcing.acquire(blocking=False):
            return
        p = self.p
        try:
            self._source(p, rev)
        finally:
            self.sourcing.release()

    def submit_source(self, rev=None):
        if not self.busy.acquire(blocking=False):
            return False
        try:
            if not self.sourcing.acquire(blocking=False):
                return False
            p = self.p
            def work():
                try:
                    self._source(p, rev)
                finally:
                    self.sourcing.release()
            try:
                threading.Thread(target=work, daemon=True).start()
            except Exception:
                self.sourcing.release()
                raise
            return True
        finally:
            self.busy.release()

    def _source(self, p, rev):
        from .. import darwin, settings
        if not settings.sourcing_enabled():
            self.hub.send({"type": "sourcing", "status": "error", "error": "Enable sourcing in Settings before contacting Darwin."})
            return
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
        except Exception:
            self.hub.send({"type": "sourcing", "status": "error", "rev": rev, "project": os.path.basename(p.root),
                           "error": "Sourcing failed. Check the network connection and try again."})

    def reset(self) -> bool:
        """Demo reset: every project goes back to a clean build of its original design (see baseline());
        the revisions after it are deleted, like deleting the projects folder but keeping the first
        build. If that build is from older code it is rebuilt now (about 30 s per robot)."""
        if not self.busy.acquire(blocking=False):
            return False
        try:
            if self.sourcing.locked():
                return False
            for name in sorted(os.listdir(self.home)):
                root = contained_path(self.home, name)
                if root is None or not is_robot_project(root):
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

    def _params(self, p, changes):
        desc = ", ".join(f"{k.split('.')[-1].replace('_', ' ')} → {v}" for k, v in changes.items())
        self._apply(p, changes, f"edit: {desc}", reply=f"Parameter edit: {desc}.")


def make_handler(studio: Studio):
    class H(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(30)

        def log_message(self, *a):
            pass  # Request URLs and credential setup bodies never enter access logs.

        def _headers(self, code, ctype, length=None, cookie=False, artifact=False):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            if length is not None:
                self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "SAMEORIGIN" if artifact else "DENY")
            if artifact:
                # User/generated HTML and SVG must not execute with the settings origin.
                self.send_header("Content-Security-Policy", "sandbox; default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'")
            if cookie:
                self.send_header("Set-Cookie", studio.session.cookie())
            self.end_headers()

        def _send(self, code, body: bytes, ctype="application/json", cookie=False, artifact=False):
            self._headers(code, ctype, len(body), cookie, artifact)
            self.wfile.write(body)

        def _json(self, code, value):
            return self._send(code, json.dumps(value).encode("utf-8"))

        def _error(self, code, message):
            self._discard_rejected_body()
            return self._json(code, {"ok": False, "error": message})

        def _discard_rejected_body(self):
            """Avoid a Windows TCP reset when refusing a small POST before reading it.

            Only an unambiguous, already bounded Content-Length may be discarded.
            A strict wall-clock deadline also bounds slow or incomplete senders;
            this never parses unauthenticated data or waits for a large upload.
            """
            if self.command != "POST" or getattr(self, "_body_consumed", False):
                return
            self._body_consumed = True
            if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
                return
            try:
                remaining = int(self.headers.get("Content-Length", ""))
            except ValueError:
                return
            if not 0 < remaining <= MAX_BODY:
                return
            timeout = self.connection.gettimeout()
            deadline = time.monotonic() + 0.25
            try:
                while remaining:
                    wait = deadline - time.monotonic()
                    if wait <= 0:
                        break
                    self.connection.settimeout(wait)
                    chunk = self.rfile.read1(min(4096, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            except (OSError, TimeoutError):
                pass
            finally:
                self.connection.settimeout(timeout)

        def _guard(self, bootstrap=False):
            if not studio.session.valid_request(self.headers, self.server.server_address[1]):
                self._error(403, "This service accepts requests only from its local Studio window.")
                return False
            if not bootstrap and not studio.session.authenticated(self.headers):
                self._error(401, "Open the local Studio home page to start this session.")
                return False
            return True

        def _file(self, path, cookie=False, artifact=False):
            if not os.path.isfile(path):
                return self._error(404, "File not found.")
            ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
            if path.endswith((".js", ".mjs")):
                ctype = "text/javascript"
            elif path.endswith(".glb"):
                ctype = "model/gltf-binary"
            elif path.endswith((".ino", ".nc", ".gbr", ".drl", ".csv", ".urdf", ".xml", ".dxf")):
                ctype = "text/plain; charset=utf-8"
            try:
                with open(path, "rb") as fh:
                    self._headers(200, ctype, os.fstat(fh.fileno()).st_size, cookie, artifact)
                    shutil.copyfileobj(fh, self.wfile, length=64 * 1024)
            except FileNotFoundError:
                self._error(404, "File not found.")

        def _safe(self, root, rel):
            return contained_path(root, rel)

        def _export(self, rev):
            from .export import ExportError, build_export
            if not studio.busy.acquire(blocking=False):
                return self._error(409, "Wait for the current operation before exporting.")
            archive = None
            try:
                if studio.sourcing.locked():
                    return self._error(409, "Wait for sourcing to finish before exporting.")
                p = studio.p  # Capture after taking the same lock used by project switching.
                if not isinstance(rev, str) or rev not in p.revs():
                    return self._error(404, "Unknown revision.")
                archive, filename = build_export(p, rev)
            except ExportError as exc:
                return self._error(400, str(exc))
            except Exception:
                return self._error(503, "The revision could not be exported. Retry when all artifacts are available.")
            finally:
                # The ZIP is immutable now. A slow download must not hold the generation slot.
                studio.busy.release()
            try:
                archive.seek(0, os.SEEK_END)
                size = archive.tell()
                archive.seek(0)
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.send_header("Content-Length", str(size))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                shutil.copyfileobj(archive, self.wfile, length=64 * 1024)
            finally:
                archive.close()

        def do_GET(self):
            u = urlparse(self.path)
            bootstrap = u.path in ("/", "/index.html")
            if not self._guard(bootstrap=bootstrap):
                return
            if bootstrap:
                return self._file(os.path.join(STATIC, "index.html"), cookie=True)
            if u.path.startswith("/static/"):
                full = self._safe(STATIC, u.path[len("/static/"):])
                return self._file(full) if full else self._error(403, "Invalid file path.")
            p = studio.p
            if u.path.startswith("/files/"):
                rel = u.path[len("/files/"):]
                pieces = rel.split("/")
                if len(pieces) < 2 or pieces[0] not in p.revs() or any(piece.startswith(".") for piece in pieces):
                    return self._error(403, "Only revision artifacts can be downloaded.")
                full = self._safe(p.root, rel)
                return self._file(full, artifact=True) if full else self._error(403, "Invalid file path.")
            if u.path == "/api/state":
                return self._json(200, studio.state())
            if u.path == "/api/provider":
                from .. import providers
                return self._json(200, providers.status())
            if u.path == "/api/export":
                return self._export(parse_qs(u.query).get("rev", [None])[0])
            if u.path == "/api/rev":
                rev = parse_qs(u.query).get("rev", [None])[0]
                if rev not in p.revs():
                    return self._error(404, "Unknown revision.")
                return self._json(200, rev_payload(p, rev))
            if u.path == "/api/events":
                q = studio.hub.subscribe()
                if q is None:
                    return self._error(429, "Too many open Studio tabs. Close an unused tab.")
                self._headers(200, "text/event-stream")
                try:
                    while True:
                        try:
                            ev = q.get(timeout=15)
                            self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
                        except queue.Empty:
                            self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                except (OSError, TimeoutError):
                    pass
                finally:
                    studio.hub.unsubscribe(q)
                return
            self._error(404, "Not found.")

        def _body(self):
            if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
                self._error(400, "A single Content-Length is required.")
                return None
            if self.headers.get_content_type() != "application/json":
                self._error(415, "Send application/json.")
                return None
            try:
                n = int(self.headers.get("Content-Length", ""))
            except ValueError:
                n = -1
            if n < 0 or n > MAX_BODY:
                self._error(413, "Request body exceeds the limit.")
                return None
            try:
                self._body_consumed = True
                raw = self.rfile.read(n)
                if len(raw) != n:
                    raise ValueError()
                def invalid_constant(value):
                    raise ValueError("Invalid JSON constant")
                body = json.loads(raw.decode("utf-8"), parse_constant=invalid_constant)
                if not isinstance(body, dict):
                    raise ValueError()
                return body
            except (ValueError, UnicodeError, RecursionError):
                self._error(400, "Send a valid JSON object.")
                return None

        def _provider(self, action, body):
            from .. import providers
            if not studio.busy.acquire(blocking=False):
                return self._error(409, "Wait for the current operation before changing settings.")
            try:
                if studio.sourcing.locked():
                    return self._error(409, "Wait for sourcing to finish before changing settings.")
                if action in ("test", "save"):
                    for field in ("key", "model", "storage"):
                        limit = 4096 if field == "key" else 128
                        if field in body and (not isinstance(body[field], str) or len(body[field]) > limit):
                            return self._error(400, "Invalid provider settings.")
                if action == "test":
                    result = providers.test_connection(key=body.get("key"), model=body.get("model"))
                elif action == "save":
                    result = providers.save_key(body.get("key", ""), storage=body.get("storage", "system"), model=body.get("model"))
                elif action == "remove":
                    result = providers.remove_key()
                elif action == "preferences":
                    allowed = {"network_enabled", "sourcing_enabled", "telemetry_enabled", "model", "onboarding_complete"}
                    if set(body) - allowed:
                        return self._error(400, "Unknown preference.")
                    result = providers.update_preferences(body)
                else:
                    return self._error(404, "Unknown provider operation.")
                return self._json(200, result)
            except providers.ProviderError as exc:
                return self._error(400, str(exc))
            except Exception:
                return self._error(503, "Provider settings are temporarily unavailable.")
            finally:
                studio.busy.release()

        def do_POST(self):
            self._body_consumed = False
            if not self._guard():
                return
            body = self._body()
            if body is None:
                return
            path = urlparse(self.path).path
            if path.startswith("/api/provider/"):
                return self._provider(path.rsplit("/", 1)[1], body)
            if path == "/api/chat":
                msg = body.get("message")
                if not isinstance(msg, str) or not msg.strip() or len(msg) > MAX_MESSAGE:
                    return self._error(400, "Enter a message of at most 8,000 characters.")
                if "sk-ant-" in msg.lower():
                    return self._error(400, "Enter API keys in Settings. This message was not saved or sent.")
                ok = studio.submit("chat", msg.strip())
            elif path == "/api/params":
                changes = body.get("changes")
                if not isinstance(changes, dict) or not changes or len(changes) > 32 or any(
                    not isinstance(k, str) or len(k) > 128 or not isinstance(v, (str, int, float, bool)) or
                    (isinstance(v, str) and len(v) > 1024) for k, v in changes.items()
                ):
                    return self._error(400, "Invalid parameter changes.")
                if any(isinstance(v, str) and "sk-ant-" in v.lower() for v in changes.values()):
                    return self._error(400, "Enter API keys only in Settings.")
                ok = studio.submit("params", changes)
            elif path == "/api/project":
                name = body.get("name")
                if not isinstance(name, str):
                    return self._error(400, "Invalid project name.")
                ok = studio.switch(name)
                return self._json(200 if ok else 409, {"ok": ok})
            elif path in ("/api/projects/create", "/api/projects/duplicate"):
                try:
                    name = project_name(body.get("name"))
                except ValueError as exc:
                    return self._error(400, str(exc))
                root = contained_path(studio.home, name)
                if root is None or os.path.exists(root):
                    return self._error(409, "A project with that name already exists.")
                if body.get("example", "egg-picker") not in ("egg-picker", "so101"):
                    return self._error(400, "Choose egg-picker or so101.")
                if path.endswith("duplicate") and not studio.p.revs():
                    return self._error(409, "Choose an existing project to duplicate.")
                ok = studio.submit(path.rsplit("/", 1)[1], body)
            elif path == "/api/source":
                from .. import settings
                if not settings.sourcing_enabled():
                    return self._error(403, "Enable sourcing in Settings before contacting Darwin.")
                rev = body.get("rev")
                if rev is not None and (not isinstance(rev, str) or rev not in studio.p.revs()):
                    return self._error(400, "Unknown revision.")
                ok = studio.submit_source(rev)
            elif path == "/api/reset":
                if not studio.demo_mode:
                    return self._error(403, "Reset is available only in explicitly enabled demo mode. Duplicate a project instead.")
                ok = studio.reset()
                return self._json(200 if ok else 409, {"ok": ok})
            else:
                return self._error(404, "Not found.")
            return self._json(202 if ok else 409, {"ok": ok, **({} if ok else {"error": "Another operation is still running."})})

    return H


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"
    request_queue_size = 16

    def __init__(self, *args, **kwargs):
        self._slots = threading.BoundedSemaphore(24)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            try:
                request.settimeout(1)
                request.sendall(b"HTTP/1.0 503 Busy\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request, client_address):
        import sys
        if isinstance(sys.exc_info()[1], (OSError, TimeoutError)):
            return
        # Do not print exception contents: provider failures may contain sensitive request data.
        print("A local Studio request failed.")


def serve(project: RobotProject, host="127.0.0.1", port=8765, home: str | None = None, demo_mode=False):
    if host not in ("127.0.0.1", "localhost"):
        raise SystemExit("Studio is a local app; host must be 127.0.0.1 or localhost.")
    studio = Studio(project, home, demo_mode=demo_mode)
    try:
        httpd = _Server((host, port), make_handler(studio))
    except OSError as exc:
        raise SystemExit(f"Port {port} is busy ({exc}): R Cubed Studio may already be running.")
    print(f"R Cubed Studio: http://{host}:{httpd.server_address[1]}  (project {project.root}; Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
