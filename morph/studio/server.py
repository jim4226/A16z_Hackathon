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

import hmac
import html
import json
import mimetypes
import os
import queue
import secrets
import shutil
import socket
import threading
import time
import traceback
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlencode, urlparse

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
    """Which services are configured on this machine (keys come from the environment)."""
    env = os.environ.get
    return {"claude": {"chat": bool(env("ANTHROPIC_API_KEY"))},
            "keewano": {"send": bool(env("KEEWANO_API_KEY")), "query": bool(env("KEEWANO_PAT") and env("KEEWANO_PROJECT_ID"))},
            "darwin": {"search": True}}


def is_loopback(host: str) -> bool:
    return host in ("localhost", "::1") or host.startswith("127.")


def access_token(host: str, token: str | None = None) -> str | None:
    """Bring your own compute: a Studio that other computers can reach (any address but loopback) only
    serves browsers that opened its access link; `token` keeps the link the same across restarts.
    Local-only Studios need no token."""
    return None if is_loopback(host) else (token or secrets.token_urlsafe(12))


def lan_address() -> str | None:
    """The address other computers reach this one by, or None (no network)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("10.255.255.255", 1))  # UDP: nothing is sent, it only picks the outgoing interface
            ip = sock.getsockname()[0]
        return None if ip.startswith("127.") else ip
    except OSError:
        return None


def compute_info(remote: bool) -> dict:
    """Where the pipeline runs: shown in the header, so a laptop browser shows which machine does the work."""
    return {"host": socket.gethostname(), "cores": os.cpu_count() or 0, "remote": remote}


SIGN_IN = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Morph Studio: access code</title><style>
:root { color-scheme: dark; } body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: #08090c; color: #e9ecf2;
  font: 14px/1.5 Inter, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
main { width: min(440px, calc(100vw - 32px)); padding: 22px; border: 1px solid #262c38; border-radius: 14px; background: #0d0f14; }
h1 { font-size: 17px; margin: 0 0 6px; } p { color: #b9c0cd; margin: 0 0 14px; } .bad { color: #ff6b6b; }
form { display: flex; gap: 8px; } input { flex: 1; min-width: 0; height: 36px; border-radius: 8px; border: 1px solid #343b49; background: #08090c;
  color: #e9ecf2; padding: 0 10px; font: 13px ui-monospace, Menlo, Consolas, monospace; }
button { height: 36px; border: 0; border-radius: 8px; padding: 0 14px; background: #7091ff; color: #fff; font-weight: 600; cursor: pointer; }
</style></head><body><main><h1>Morph Studio on HOST</h1>
<p>This Studio runs on another computer. Open the link printed in the terminal where it was started, or paste its access code.</p>BAD
<form method="get" action="/"><input name="token" autocomplete="off" spellcheck="false" placeholder="Access code" aria-label="Access code" autofocus>
<button type="submit">Open</button></form></main></body></html>"""


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
        self.remote = False  # set by serve() when other computers may use this Studio

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
               "history": self.hub.history, "integrations": integrations(), "compute": compute_info(self.remote)}
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

    def reset(self, rebuild: bool = False) -> bool:
        """Demo reset: every project goes back to a clean build of its original design (see baseline());
        the revisions after it are deleted, like deleting the projects folder but keeping the first
        build. If that build is from older code, or `rebuild` is set (new API keys: the Keewano grip
        calibration only applies to a fresh build), it is rebuilt now (about 30 s per robot)."""
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
                if complete and (rebuild or (self._results(p, keep) or {}).get("pipeline_version") != pipeline_version()):
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


def make_handler(studio: Studio, allowed_hosts: set | None = None, token: str | None = None):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _trusted(self, post: bool) -> bool:
            """Only this computer's Studio page may use the API. The Host check stops DNS rebinding
            (a web page that resolves its own name to 127.0.0.1); the Origin check stops another
            site's page from POSTing to the Studio (changing keys, resetting the demo)."""
            host = (self.headers.get("Host") or "").lower()
            if allowed_hosts is not None and host not in allowed_hosts:
                return False
            if post:
                origin = self.headers.get("Origin")
                if origin and origin.lower() not in (f"http://{host}", f"https://{host}"):
                    return False
            return True

        def _cookie_name(self) -> str:
            return f"morph_access_{self.server.server_address[1]}"  # cookies ignore the port

        def _signed_in(self) -> bool:
            """Remote Studios: the browser opened the access link (its cookie), or a script sends the token."""
            if not token:
                return True
            jar = SimpleCookie()
            try:
                jar.load(self.headers.get("Cookie") or "")
            except Exception:  # noqa: BLE001  (a malformed cookie header is just "not signed in")
                pass
            got = jar[self._cookie_name()].value if self._cookie_name() in jar else (self.headers.get("X-Morph-Token") or "")
            return hmac.compare_digest(got.encode(), token.encode())

        def _sign_in_page(self, bad=False):
            page = SIGN_IN.replace("HOST", html.escape(socket.gethostname())).replace(
                "BAD", '<p class="bad">That access code did not match. Copy it again from the terminal.</p>' if bad else "")
            return self._send(401, page.encode(), "text/html; charset=utf-8")

        def _access_link(self, u) -> bool:
            """GET /?token=...: a correct code signs this browser in (a cookie) and drops the code from the URL."""
            q = parse_qs(u.query)
            if not token or "token" not in q or u.path not in ("/", "/index.html"):
                return False
            if not hmac.compare_digest(q["token"][0].strip().encode(), token.encode()):
                self._sign_in_page(bad=True)
                return True
            rest = urlencode([(k, v) for k, vs in q.items() if k != "token" for v in vs])
            self.send_response(302)
            self.send_header("Location", "/" + (f"?{rest}" if rest else ""))
            self.send_header("Set-Cookie", f"{self._cookie_name()}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return True

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
            if not self._trusted(post=False):
                return self._send(403, b"forbidden", "text/plain")
            if self._access_link(u):
                return None
            if not self._signed_in():
                if u.path in ("/", "/index.html"):
                    return self._sign_in_page()
                return self._send(401, b'{"ok":false,"error":"open the access link printed where Morph Studio was started"}')
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
            if u.path == "/api/keys":
                from .. import keys
                return self._send(200, json.dumps({"keys": keys.status(), "integrations": integrations(),
                                                   "file": os.path.basename(keys.ENV_PATH),
                                                   "compute": compute_info(studio.remote)}).encode())
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
            if not self._trusted(post=True):
                return self._send(403, b"forbidden", "text/plain")
            if not self._signed_in():
                return self._send(401, b'{"ok":false,"error":"open the access link printed where Morph Studio was started"}')
            n = int(self.headers.get("Content-Length", 0))
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                body = None
            if not isinstance(body, dict):
                return self._send(400, b'{"ok":false,"error":"expected a JSON object"}')
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
                ok = studio.reset(rebuild=bool(body.get("rebuild")))
                return self._send(200 if ok else 409, json.dumps({"ok": ok}).encode())
            if u.path == "/api/keys":
                from .. import keewano, keys
                try:
                    changed = keys.save(body.get("values") or {})
                except ValueError as exc:
                    return self._send(400, json.dumps({"ok": False, "error": str(exc)}).encode())
                if any(k.startswith("KEEWANO") for k in changed):
                    keewano._ratio_cache.clear()  # re-query the calibration with the new project
                return self._send(200, json.dumps({"ok": True, "changed": changed, "keys": keys.status(),
                                                   "integrations": integrations()}).encode())
            if u.path == "/api/keys/check":  # POST: it calls the services with the person's keys
                from .. import keys
                services = body.get("services")
                return self._send(200, json.dumps({"checks": keys.check(services if isinstance(services, list) else None)}).encode())
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


def serve(project: RobotProject, host="127.0.0.1", port=8765, home: str | None = None, token: str | None = None):
    if not project.revs():
        raise SystemExit(f"{project.root} has no revisions yet: run `python -m morph.cli robot new {os.path.basename(project.root)}` first")
    studio = Studio(project, home)
    token = access_token(host, token)
    studio.remote = bool(token)
    # local Studio: requests must name this server (127.0.0.1/localhost); remote Studio: the access token decides
    allowed = None if token else {f"{h}:{port}" for h in ("127.0.0.1", "localhost", host)}
    try:
        httpd = _Server((host, port), make_handler(studio, allowed, token))
    except OSError as exc:
        raise SystemExit(f"Port {port} is busy ({exc}): Morph Studio is probably already running. "
                         "Close its CMD window, then start again.")
    httpd.daemon_threads = True
    if token:
        where = lan_address() if host in ("0.0.0.0", "") else host
        print(f"Morph Studio is running on {socket.gethostname()} ({os.cpu_count()} CPU cores) for other computers too.")
        print("Open this link in a browser on any computer that can reach this one (it signs that browser in):")
        for h in [x for x in (where,) if x]:
            print(f"    http://{h}:{port}/?token={token}")
        print(f"    http://127.0.0.1:{port}/?token={token}   (on this computer)")
        print(f"Access code: {token}   Project {project.root}; Ctrl+C to stop.")
    else:
        print(f"Morph Studio: http://{host}:{port}  (project {project.root}; Ctrl+C to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
