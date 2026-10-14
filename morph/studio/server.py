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

import base64
import errno
import hashlib
import hmac
import html
import importlib
import io
import json
import mimetypes
import os
import pkgutil
import queue
import re
import secrets
import selectors
import shutil
import socket
import stat
import threading
import time
import traceback
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlencode, urlparse

from ..pipeline import REV
from ..robot.pipeline2 import LABELS, PARAMS, RobotProject, params_for, pipeline_version
from ..spec import diff
from . import labels, projects, routes
from .intent import CHAT_FORBIDDEN, OBJECTS, interpret, object_changes
from .rooms import Hub, QueueFull, Room, UnknownProject  # noqa: F401  (Hub: older code imports it from here)
from .routes import HTTPError, route
from .security import (MAX_MESSAGE, BodyError, contained_path, content_length, design_values, json_object,
                       looks_like_secret, param_changes, project_name)

STATIC = os.path.join(os.path.dirname(__file__), "static")


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


def integrations() -> dict:
    """Which services are configured on this machine (keys come from the environment) and switched on in
    the settings: `configured` means the keys are there, the other flags mean Morph will actually call it."""
    from .. import settings
    from . import subq
    env, s, net = os.environ.get, settings.load(), settings.network_allowed()
    claude = bool(env("ANTHROPIC_API_KEY"))
    send, query = bool(env("KEEWANO_API_KEY")), bool(env("KEEWANO_PAT") and env("KEEWANO_PROJECT_ID"))
    sq = subq.status()
    return {"claude": {"configured": claude, "chat": claude and net},
            "keewano": {"configured": send and query, "send": send and net and settings.telemetry_enabled(),
                        "query": query and net, "calibration": query and net and s["calibration_enabled"]},
            "darwin": {"search": net and s["sourcing_enabled"]},
            "subq": {"configured": sq["configured"], "decide": subq.enabled(), "disabled": sq["disabled"]},
            "network": net}


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


def rev_payload(p: RobotProject, rev: str, project: str | None = None) -> dict:
    """A revision for the page. File URLs are /files/<project>/<rev>/... (/files/<rev>/... without a project: the
    Studio's start project)."""
    res, d = p.load_results(rev), p.load_design(rev)
    base = f"/files/{project}/{rev}" if project else f"/files/{rev}"
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
        "provenance": res.get("provenance") or {}, "stage_status": res.get("stage_status") or {},
        "timings": res.get("timings_s") or {},
        "nodes": [list(x) for x in LABELS], "design": d.flat(), "params": params_for(d),
        "summary": res.get("summary") or {}, "errors": res.get("errors") or {},
        "robot": {"json": f"{base}/cad/robot.json", "glb": f"{base}/cad/robot.glb",
                  "urdf": _url(base, cad.get("urdf"))} if cad else None,
        "cad": cad, "sizing": res.get("sizing"), "sims": sims, "electronics": el or None,
        "manufacturing": mfg or None, "bom": res.get("bom"), "manual": man or None,
        "gripper": {"analysis": grip.get("analysis"), "sim": grip.get("sim"), "force": grip.get("force")},
        "time_study": time_card, "sourcing": _sourcing(p, rev),
    }


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name) or default))
    except ValueError:
        return default


def _start_features(home: str):
    """What feature modules tidy when a Studio starts on `home`: the importers' home (imported robots live in
    home/.robots) and import sessions older than a day, then video work folders a crash left in .uploads."""
    try:
        from ..importers import common as imports, session as import_sessions
        imports.set_home(home)
        import_sessions.cleanup(home)
    except Exception:  # noqa: BLE001  (a feature's clean-up never stops the Studio)
        traceback.print_exc()
    try:
        from . import ffmpeg
        ffmpeg.cleanup(home, older_than_s=3600)
    except Exception:  # noqa: BLE001
        traceback.print_exc()


def _after_build(studio, room, rev):
    """A finished build: Live prices may start by itself (api_tools.after_build, when its setting is on)."""
    try:
        from . import api_tools
        api_tools.after_build(studio, room, rev)
    except Exception:  # noqa: BLE001  (an after-build agent never fails the build)
        traceback.print_exc()


def _start_tools(studio):
    """The agents' toolbox (api_tools.attach): runs a stopped Studio left working become interrupted pauses."""
    try:
        from . import api_tools
        api_tools.attach(studio)
    except Exception:  # noqa: BLE001  (agent tools never stop the Studio)
        traceback.print_exc()


class Refused(RuntimeError):
    """A project change the Studio won't make now (the project is building, or it is the start project)."""


class Studio:
    """Every project the Studio serves, one Room each (rooms.py), created on first use. Requests name their project
    with ?project= (default: `start`, the project the Studio was started on), so switching projects in one tab
    moves nobody else. `builds` limits how many pipeline builds run at once across rooms (MORPH_MAX_BUILDS, 1).
    `team`: Morph Teams (morph studio --team), where members sign in and the role table decides what each may do;
    `allow_url_import`: importing from a link stays on even when other computers can use this Studio."""

    def __init__(self, project: RobotProject, home: str | None = None, max_builds: int | None = None, *,
                 team: bool = False, allow_url_import: bool = False):
        self.home = os.path.realpath(home or os.path.dirname(project.root))
        self.start = os.path.basename(project.root)
        self.remote = False  # set by serve() when other computers may use this Studio
        self.team, self.allow_url_import = bool(team), bool(allow_url_import)
        self.builds = threading.BoundedSemaphore(max_builds or _env_int("MORPH_MAX_BUILDS", 1))
        self.rooms = {self.start: Room(self.start, project)}
        self._rooms = threading.Lock()
        projects.clear_uploads(self.home)  # uploads an earlier run never finished with
        _start_features(self.home)
        self.toolbox = None
        _start_tools(self)

    @property
    def p(self) -> RobotProject:
        """The start project (rooms[start].p)."""
        return self.rooms[self.start].p

    @property
    def hub(self) -> Hub:
        return self.rooms[self.start].hub

    def _root(self, name) -> str | None:
        """The folder of an existing robot project called `name`, or None. Checked before anything is
        created: RobotProject() makes its folder, so it must never see a name that came from a request."""
        return projects.project_root(self.home, name)

    def room(self, name: str | None = None) -> Room:
        """The room of robot project `name` (default: the start project). ValueError for a name that can't be a
        project, UnknownProject (a LookupError) when there is no such robot project."""
        if name is None or name == "" or name == self.start:
            return self.rooms[self.start]
        project_name(name)
        room = self.rooms.get(name)
        if room is not None and os.path.isdir(room.p.root):
            return room
        root = self._root(name)
        if not root:
            raise UnknownProject(f"There is no robot project called {name}.")
        with self._rooms:
            room = self.rooms.get(name)
            if room is None or not os.path.isdir(room.p.root):
                room = self.rooms[name] = Room(name, root)
            return room

    def broadcast(self, event: dict):
        """An event for every open tab, whatever project it is on (a project was created or deleted)."""
        for room in list(self.rooms.values()):
            room.hub.send(event)

    def projects(self) -> list:
        """The project menu: projects.list_projects() plus `busy` (building or queued) and, for demo projects,
        `at_start` (no edits since the clean build Reset demo goes back to)."""
        out = projects.list_projects(self.home)
        for info in out:
            room = self.rooms.get(info["name"])
            info["busy"] = bool(room and room.jobs.busy())
            if info["demo"]:
                p = room.p if room else RobotProject(os.path.join(self.home, info["name"]))
                info["at_start"] = projects.finished(p)[-1:] == [baseline(p)]
        return out

    # -- projects: new, forked, deleted
    def create_project(self, name=None, *, source: str = "", design=None, need: str | None = None, origin: str = "default",
                       by: str | None = None, meta: dict | None = None, label: str = "first design", on_room=None):
        """A new robot project (projects.create) whose first build is queued in its room at once: tabs that open it
        see the build stream in. `on_room(room)` runs before the build is queued (a feature that relays the build
        subscribes there, so it misses no event). Returns (room, job). ValueError, FileExistsError as projects.create."""
        info = projects.create(self.home, name, source=source, design=design, need=need, origin=origin, by=by, meta=meta)
        room = self.room(info["name"])
        if on_room is not None:
            on_room(room)
        job = self.queue_build(room, info["design"], label=label, by=by)
        self.broadcast({"type": "project_created", "project": room.name, "origin": origin, **({"by": by} if by else {})})
        return room, job

    def fork_project(self, project: str, rev: str | None = None, name=None, *, by: str | None = None):
        """Fork `project` at `rev` (projects.fork): copied when its files are current, else built in the new room.
        Returns (room, job or None, info)."""
        info = projects.fork(self.home, project, rev, name, by=by)
        room = self.room(info["name"])
        src = info["meta"]["forked_from"]
        job = None if info["copied"] else self.queue_build(room, info["design"], label=f"fork of {project} {src['rev']}", by=by)
        self.broadcast({"type": "project_created", "project": room.name, "origin": "fork", **({"by": by} if by else {})})
        return room, job, info

    def delete_project(self, name: str) -> str:
        """Move a project to MORPH_HOME/.trash (projects.delete) and close its room; tabs on it are told. Refused while
        it builds or has changes waiting, and for the project the Studio started on."""
        if name == self.start:
            raise Refused(f"{name} is the project this Studio started on; start the Studio on another project "
                                  "to delete it.")
        root = self._root(name)
        if not root:
            raise UnknownProject(f"There is no robot project called {name}.")
        room = self.room(name)
        if room.jobs.busy() or not room.lock.acquire(blocking=False):
            raise Refused(f"{name} is building or has changes waiting: delete it when its queue is empty.")
        try:
            from . import jobs
            jobs.stop(self, name)  # its robustness study first (finished runs are kept, in the trash with the project)
            trash = projects.delete(self.home, name)
            with self._rooms:
                self.rooms.pop(name, None)
        finally:
            room.lock.release()
        self.broadcast({"type": "project_deleted", "project": name})
        room.hub.send({"type": "project_deleted", "project": name})
        return trash

    def state(self, room: Room | None = None) -> dict:
        room = room or self.room()
        revs = room.completed()
        out = {"project": room.name, "start": self.start, "projects": self.projects(), "revs": revs,
               "history": list(room.hub.history), "integrations": integrations(), "compute": compute_info(self.remote),
               "queue": room.jobs.snapshot(), "building": room.building()}
        if revs:
            out["current"] = rev_payload(room.p, revs[-1], room.name)
            parent = out["current"]["parent"]
            out["previous"] = rev_payload(room.p, parent, room.name) if parent in revs else None
        return out

    def artifact(self, rel: str) -> str | None:
        """/files/<project>/<rev>/... (or /files/<rev>/... for the start project): a file inside one of a robot
        project's revision folders, or None."""
        text = unquote(rel)
        head, _, rest = text.partition("/")
        start = self.rooms[self.start]
        if REV.fullmatch(head) and head in start.p.revs():
            return artifact_path(start.p, rel)
        if not head or not rest:
            return None
        try:
            room = self.room(head)
        except (ValueError, LookupError):
            return None
        if rest.split("/", 1)[0] not in room.p.revs():
            return None
        return contained_path(room.p.root, rest, decode=False)

    def _expand(self, changes: dict) -> dict:
        """Object presets fan out into dims/mass/friction/damage force + finger fit."""
        name = changes.get("object.name")
        if isinstance(name, str) and name in OBJECTS and len(changes) == 1:
            return object_changes(name)
        return changes

    @staticmethod
    def _tag(job) -> dict:
        return {"job": job.id, **({"by": job.by} if job.by else {})} if job is not None else {}

    def _say(self, room: Room, job, status: str, text: str, **extra):
        """An assistant message that ends a job without a new revision (noop, error)."""
        if job is not None:
            job.status = status
            if status == "error":
                job.error = text
        room.emit({"type": "assistant", "text": text, **extra, **self._tag(job)})

    def _conflict(self, room: Room, job, base: str, changes: dict) -> bool:
        """A queued change whose keys were changed by someone else after it was asked for (on job.base_rev) is not
        applied over them: the job is marked conflict and the room is asked to apply it on `base` or drop it."""
        if job is None or not job.base_rev or job.base_rev == base:
            return False
        since = None
        if job.base_rev in room.completed():
            try:
                since = set(diff(room.p.load_design(job.base_rev), room.p.load_design(base)))
            except (OSError, ValueError, KeyError, TypeError):
                since = None
        keys = sorted(set(changes) & since) if since is not None else sorted(changes)
        if not keys:
            return False
        job.status = "conflict"
        job.conflict = {"keys": keys, "base_rev": job.base_rev, "rev": base}
        why = (f"{', '.join(keys)} changed after {job.base_rev}, which this request was based on" if since is not None
               else f"{job.base_rev}, which this request was based on, no longer exists")
        room.emit({"type": "assistant", "text": f"Not applied yet: {why}. Apply it to {base} anyway, or drop it?",
                   "changes": changes, "conflict": {"job": job.id, **job.conflict}, **self._tag(job)})
        return True

    def apply(self, changes: dict, label: str, user_text: str | None = None, reply: str | None = None, source="",
              room: Room | None = None, job=None) -> str | None:
        """Apply `changes` to the room's latest revision and build it; returns the new revision. Runs in the caller's
        thread: the room's queue calls it for queued jobs (`job`), tests and scripts call it directly."""
        room = room or self.room()
        with room.lock:
            try:
                if user_text and job is None:
                    room.emit({"type": "user", "text": user_text})
                base = room.latest()
                if base is None:
                    return self._say(room, job, "error", "This project has no finished revision yet, so there is nothing "
                                                         "to change. Wait for its first build.")
                cur = room.p.load_design(base)
                changes = self._expand(changes)
                try:
                    design = cur.with_changes(changes)
                    design_values(design.flat(), projects.DEFAULTS, keys=changes)  # chat and models can ask for anything
                except (KeyError, ValueError) as exc:
                    return self._say(room, job, "error", f"Couldn't apply that: {exc}")
                if job is not None:
                    job.data.update(changes=changes, label=label, reply=reply, source=source)
                    if self._conflict(room, job, base, changes):
                        return None
                if design.to_dict() == cur.to_dict():
                    return self._say(room, job, "noop", f"Nothing to change: {base} already has that, so nothing was "
                                                        "regenerated.", noop=True, changes=changes)
                if reply:
                    room.emit({"type": "assistant", "text": reply, "changes": changes, "source": source, **self._tag(job)})
                t0 = time.time()
                with self.builds:
                    rev = room.p.run(design, parent=base, label=label[:48], verbose=False, on_event=room.emit,
                                     by=job.by if job is not None else None,
                                     proposed_by=job.data.get("proposed_by") if job is not None else None)
                room.p.log({"type": "tool_time", "cmd": "studio", "seconds": round(time.time() - t0, 2)})
                if job is not None:
                    job.result = rev
                room.emit({"type": "done", "current": rev_payload(room.p, rev, room.name),
                           "previous": rev_payload(room.p, base, room.name), "seconds": round(time.time() - t0, 1),
                           **self._tag(job)})
                _after_build(self, room, rev)
                return rev
            except Exception as exc:  # noqa: BLE001  (shown in the chat; the room keeps working)
                traceback.print_exc()
                self._say(room, job, "error", f"Error: {exc}")
                return None
            finally:
                room.live = None

    def _offer(self, room: Room, job, msg: str, cur) -> bool:
        """A message that asks for another robot (or to import one) is offered the New robot (or Import) dialog
        instead of becoming an edit of this one (agents.chat_offer decides). True when it was offered."""
        from . import agents
        info = {}
        try:
            offer = agents.chat_offer(msg, cur, {"name": room.name, "source": cur.arm.source or "generated"}, info=info)
        except Exception:  # noqa: BLE001  (routing never stops an edit)
            traceback.print_exc()
            offer = None
        if info.get("source"):  # SubQ answered: counts and timings in the ledger (Keewano, when its events are on)
            try:
                room.p.log({"type": "subq", "purpose": "chat_route", **info})
            except OSError:
                pass
        if not offer:
            return False
        name, source = room.name, offer["route"].get("source", "rules")
        text = (f"That reads like a new robot rather than a change to {name}, so {name} stays as it is."
                if offer["kind"] == "new_robot" else
                f"That sounds like bringing in an existing robot. Import takes its URDF, xacro or MuJoCo files, a zip, or "
                f"a GitHub or Hugging Face link; {name} stays as it is.")
        if source == "subq":  # SubQ made the call: say so, as every SubQ answer is labelled
            text += " (SubQ Snap (AI) read it that way.)"
        self._say(room, job, "noop", text, noop=True, offer={"kind": offer["kind"], "text": msg[:2000], "source": source})
        return True

    def chat(self, msg: str, room: Room | None = None, job=None, *, route: bool = True) -> str | None:
        """Interpret a chat message against the room's newest design and apply it. A queued job's user message was
        sent when it was queued; a direct call sends it here. With `route`, a message that asks for another robot is
        offered the New robot dialog instead (the tab's "Change it instead" sends it again with route=False)."""
        room = room or self.room()
        with room.lock:
            try:
                if job is None:
                    room.emit({"type": "user", "text": msg})
                base = room.latest()
                if base is None:
                    return self._say(room, job, "error", "This project has no finished revision yet, so there is "
                                                         "nothing to change. Wait for its first build.")
                cur = room.p.load_design(base)
                if route and self._offer(room, job, msg, cur):
                    return None
                target = ((self._results(room.p, base) or {}).get("summary") or {}).get("grip_target_n") or room.last_target
                it = interpret(msg, cur, target)  # this room's grip target in, the one after the message out
                room.last_target = it.get("last_target") or target
                it["changes"] = {k: v for k, v in (it.get("changes") or {}).items() if k not in CHAT_FORBIDDEN}
                if not it["changes"]:
                    return self._say(room, job, "noop", it["reply"], noop=True)
            except Exception as exc:  # noqa: BLE001
                traceback.print_exc()
                return self._say(room, job, "error", f"Error: {exc}")
            return self.apply(it["changes"], msg, reply=it["reply"], source=it["source"], room=room, job=job)

    def params(self, changes: dict, room: Room | None = None, job=None) -> str | None:
        desc = labels.describe(changes)  # "Build → Pilot", as the Parameters panel names them
        return self.apply(changes, f"edit: {desc}", reply=f"Parameter edit: {desc}.", room=room, job=job)

    def build(self, design, room: Room | None = None, label: str = "first design", job=None) -> str | None:
        """Build `design` as a revision with no parent: a new project's first revision."""
        room = room or self.room()
        with room.lock:
            try:
                t0 = time.time()
                with self.builds:
                    rev = room.p.run(design, parent=None, label=label[:48], verbose=False, on_event=room.emit,
                                     by=job.by if job is not None else None)
                room.p.log({"type": "tool_time", "cmd": "studio", "seconds": round(time.time() - t0, 2)})
                if job is not None:
                    job.result = rev
                room.emit({"type": "done", "current": rev_payload(room.p, rev, room.name), "previous": None,
                           "seconds": round(time.time() - t0, 1), **self._tag(job)})
                _after_build(self, room, rev)
                return rev
            except Exception as exc:  # noqa: BLE001
                traceback.print_exc()
                self._say(room, job, "error", f"Error: {exc}")
                return None
            finally:
                room.live = None

    @staticmethod
    def when_done(job, fn):
        """Run fn(job) on a background thread once `job` has ended, whatever its status: what a feature does after a
        build it queued (the brief's after-build agents, a change request's bookkeeping, an import's progress)."""
        def wait():
            job.wait()
            try:
                fn(job)
            except Exception:  # noqa: BLE001  (a feature's follow-up never breaks the room)
                traceback.print_exc()
        threading.Thread(target=wait, daemon=True, name=f"morph-after-{job.id}").start()

    # -- the queue: what /api/chat, /api/params and new projects use
    def queue_chat(self, room: Room, msg: str, *, base_rev: str | None = None, by: str | None = None, route: bool = True):
        """Queue a chat message; it is interpreted when its turn comes, against the newest design. The user's message
        is shown at once. `route=False`: an edit of this project even if it reads like a new robot. Raises QueueFull."""
        say = lambda job: room.hub.send({"type": "user", "text": msg, "job": job.id, **({"by": by} if by else {})})  # noqa: E731
        return room.jobs.submit("chat", lambda job: self.chat(msg, room=room, job=job, route=route), by=by, label=msg,
                                base_rev=base_rev or room.latest(), announce=say)

    def queue_changes(self, room: Room, changes: dict, *, label: str, reply: str | None = None, source: str = "",
                      user_text: str | None = None, base_rev: str | None = None, by: str | None = None, kind="change",
                      proposed_by: str | None = None):
        """Queue design changes (parameter edits, applied video findings, an imported object, a world, a Teams change
        request built by `by` for its author `proposed_by`, a resolved conflict). Raises QueueFull."""
        def say(job):
            if proposed_by:
                job.data["proposed_by"] = proposed_by
            if user_text:
                room.hub.send({"type": "user", "text": user_text, "job": job.id, **({"by": by} if by else {})})
        return room.jobs.submit(kind, lambda job: self.apply(changes, label, reply=reply, source=source, room=room, job=job),
                                by=by, label=label, base_rev=base_rev or room.latest(), announce=say)

    def queue_params(self, room: Room, changes: dict, *, base_rev: str | None = None, by: str | None = None):
        desc = labels.describe(changes)  # "Build → Pilot", as the Parameters panel names them
        return self.queue_changes(room, changes, label=f"edit: {desc}", reply=f"Parameter edit: {desc}.",
                                  base_rev=base_rev, by=by, kind="params")

    def queue_build(self, room: Room, design, *, label: str = "first design", by: str | None = None):
        """Queue a parentless build of `design` (a new project's first revision). Raises QueueFull."""
        return room.jobs.submit("build", lambda job: self.build(design, room=room, label=label, job=job), by=by,
                                label=label)

    def resolve(self, room: Room, job_id, action: str, by: str | None = None):
        """Answer a conflict: "apply" queues the same changes on the newest revision (checked again when they run),
        "drop" forgets them. Returns the new job (apply) or None (drop); LookupError when there is no such conflict."""
        job = room.jobs.get(job_id)
        if job is None or job.status != "conflict":
            raise LookupError("That change is no longer waiting for an answer.")
        if action == "drop":
            job.status = "dropped"
            room.jobs.publish(job)
            room.emit({"type": "assistant", "text": f"Dropped: {job.label[:80]}", "noop": True, **self._tag(job)})
            return None
        d = job.data
        new = self.queue_changes(room, d["changes"], label=d["label"], reply=d.get("reply"), source=d.get("source", ""),
                                 base_rev=room.latest(), by=by or job.by, kind=job.kind, proposed_by=d.get("proposed_by"))
        job.status = "resolved"
        room.jobs.publish(job)
        return new

    def source(self, rev: str | None = None, room: Room | None = None):
        """Darwin: shortlist agents for every purchased part and fab handoff of a revision, streamed."""
        from .. import darwin
        room = room or self.room()
        if not room.sourcing.acquire(blocking=False):
            return
        p, name = room.p, room.name
        try:
            revs = room.completed()
            rev = rev if rev in revs else (revs[-1] if revs else None)
            if rev is None:
                return
            room.hub.send({"type": "sourcing", "status": "start", "rev": rev, "project": name})
            prog = lambda n, tot, t: room.hub.send({"type": "sourcing", "status": "progress", "rev": rev, "project": name,  # noqa: E731
                                                    "done": n, "total": tot, **t})
            # one 15 s try per lookup, then the cached answer: the Studio never hangs on a slow Darwin
            darwin.source(p, rev, workers=6, on_progress=prog, timeout=15, retries=0)
            room.hub.send({"type": "sourcing", "status": "done", "rev": rev, "project": name, "sourcing": _sourcing(p, rev)})
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            room.hub.send({"type": "sourcing", "status": "error", "rev": rev, "project": name, "error": str(exc)})
        finally:
            room.sourcing.release()

    def reset(self, rebuild: bool = False) -> bool:
        """Demo reset: each demo project (meta.json demo: true, the launcher's egg-picker and so101) goes back to a
        clean build of its original design (see baseline()); the revisions after it are deleted, like deleting the
        project but keeping its first build. If that build is from older code, or `rebuild` is set (new API keys:
        the Keewano grip calibration only applies to a fresh build), it is rebuilt now (about 30 s per robot).
        Other projects are never touched. False when a demo project is still building."""
        names = [p["name"] for p in projects.list_projects(self.home) if p["demo"]]
        rooms = [self.room(n) for n in names]
        held = []
        try:
            for room in rooms:
                if room.jobs.busy() or not room.lock.acquire(blocking=False):
                    return False
                held.append(room)
            from . import jobs
            for room in rooms:
                jobs.stop(self, room.name)  # Reset demo stops the project's robustness study too (its runs are kept)
                p = room.p
                keep = baseline(p)
                if not keep:
                    continue
                revs = p.revs()
                complete = True
                for r in reversed(revs[revs.index(keep) + 1:]):  # newest first, so what is left stays contiguous
                    if not _remove_rev(p, r):
                        complete = False  # a file in it is open (Excel, a viewer): keep the rest intact
                        break
                if complete and (rebuild or (self._results(p, keep) or {}).get("pipeline_version")
                                 != pipeline_version(p.load_design(keep).arm.source)):
                    with self.builds:
                        keep = p.run(p.load_design(keep), parent=None, label="rebuilt with latest Morph", verbose=False)
                p.log({"type": "demo_reset", "rev": keep, "complete": complete})
                room.hub.clear_history()
            for room in rooms:
                room.hub.send({"type": "project", "project": room.name, "reset": True})
            return True
        finally:
            for room in held:
                room.lock.release()

    @staticmethod
    def _results(p, r):
        try:
            return p.load_results(r)
        except (OSError, ValueError):
            return None


SECURITY_HEADERS = (("X-Content-Type-Options", "nosniff"), ("X-Frame-Options", "DENY"), ("Referrer-Policy", "no-referrer"),
                    ("Cross-Origin-Resource-Policy", "same-origin"))
DEFAULT_CSP = "frame-ancestors 'none'"
# generated files (manual.html, schematic.svg, files from imported robots) never run script with the Studio's origin
ARTIFACT_CSP = "sandbox; default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; frame-ancestors 'none'"
SIGN_IN_CSP = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
CLIENT_HEADER = "X-Morph-Client"
DRAIN_MAX = 8 << 20  # a refused body up to this size is read and dropped, so the client still gets the answer
# Anyone who can reach a --remote Studio can open connections, so each one gets HEADER_TIMEOUT seconds in all for its
# request line and headers (a byte every few seconds no longer holds a thread for good), and at most MAX_CONNECTIONS
# are served at once, MAX_STREAMS of them event streams (so streams never take every slot); past a cap: 503 at once.
HEADER_TIMEOUT = 20.0
MAX_CONNECTIONS = 64
MAX_STREAMS = 48
LINGER = 2.0  # seconds a client turned away at the cap has to finish sending before its socket is closed
BUSY = (b'{"ok": false, "error": "The Studio is answering as many connections as it can. Try again in a few seconds."}')


class _Deadline(io.RawIOBase):
    """A connection's reads. While `until` (a time.monotonic() value) is set, each read waits only for what is left
    of it, so a client trickling its request line and headers a byte at a time is cut off when the time is up."""

    def __init__(self, raw, sock):
        super().__init__()
        self.raw, self.sock, self.until = raw, sock, None

    def readable(self):
        return True

    def readinto(self, b):
        if self.until is not None:
            left = self.until - time.monotonic()
            if left <= 0:
                raise TimeoutError("the request line and headers took too long")
            self.sock.settimeout(left)
        return self.raw.readinto(b)

    def close(self):
        if not self.closed:
            self.raw.close()
        super().close()


def _sha256(text: str) -> str:
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode() + "'"


def app_csp(page: str | None = None) -> str:
    """The Studio page's policy: scripts only from the Studio itself plus index.html's inline loader (by hash,
    worked out from the file so an edit can't silently break the page), three.js blob/data textures allowed, and
    blob: media so the video frame picker can play the clip the person picked (it never leaves the browser)."""
    if page is None:
        with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as fh:
            page = fh.read()
    scripts = [_sha256(s) for s in re.findall(r"<script>(.*?)</script>", page, re.S)]
    handlers = [_sha256(h) for h in re.findall(r"\son[a-z]+=\"([^\"]*)\"", page)]
    script_src = " ".join(["'self'", *scripts, *(["'unsafe-hashes'", *handlers] if handlers else [])])
    return (f"default-src 'self'; script-src {script_src}; style-src 'self' 'unsafe-inline'; "
            "font-src 'self' data:; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self' data: blob:; "
            "worker-src 'self' blob:; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


def artifact_path(p: RobotProject, rel: str) -> str | None:
    """/files/<rev>/...: a file inside one of `p`'s revision folders, or None."""
    rev = unquote(rel).split("/", 1)[0]
    return contained_path(p.root, rel) if rev in p.revs() else None


class Guesses:
    """Wrong access codes per address. After LIMIT different wrong codes within WINDOW seconds, that address's codes
    are not even compared until the window has passed, so a short MORPH_TOKEN can't be found by trying many. The same
    wrong code sent again (a tab left open while the Studio restarted with a new code) counts once."""

    LIMIT, WINDOW, ADDRESSES = 10, 900, 4096

    def __init__(self):
        self.lock, self.wrong = threading.Lock(), {}  # address -> {sha256 of a wrong code: when}

    def blocked(self, ip: str) -> bool:
        with self.lock:
            now, codes = time.monotonic(), self.wrong.get(ip) or {}
            for h in [h for h, t in codes.items() if now - t > self.WINDOW]:
                del codes[h]
            return len(codes) >= self.LIMIT

    def failed(self, ip: str, code: str):
        with self.lock:
            if ip not in self.wrong and len(self.wrong) >= self.ADDRESSES:
                self.wrong.pop(next(iter(self.wrong)))
            self.wrong.setdefault(ip, {})[hashlib.sha256(code.encode()).hexdigest()] = time.monotonic()


def make_handler(studio: Studio, allowed_hosts: set | None = None, token: str | None = None):
    """The request handler. A team Studio (studio.team) signs people in as members (session cookie or script token,
    api_team) instead of the access token, and the Teams role table checks every route."""
    guesses = Guesses()
    if studio.team:
        from . import api_team as team
    else:
        team = None

    class H(BaseHTTPRequestHandler):
        timeout = 30  # a client that stops sending or reading frees its thread; the event stream pings every 15 s
        sends_csp = True  # end_headers sends self.csp (Teams' pages set it)

        def setup(self):
            super().setup()
            self.rfile.close()  # the same socket, read through the header deadline
            self.clock = _Deadline(self.connection.makefile("rb", buffering=0), self.connection)
            self.rfile = io.BufferedReader(self.clock)

        def handle_one_request(self):
            self.clock.until = time.monotonic() + HEADER_TIMEOUT
            super().handle_one_request()

        def parse_request(self):
            try:
                return super().parse_request()
            finally:  # the headers are in: the body, the answer and an event stream get the usual timeout
                self.clock.until = None
                self.connection.settimeout(self.timeout)

        def log_message(self, *a):
            pass

        def send_response(self, *a, **k):
            self._responded = True
            super().send_response(*a, **k)

        def end_headers(self):
            for k, v in SECURITY_HEADERS:
                self.send_header(k, v)
            self.send_header("Content-Security-Policy", getattr(self, "csp", None) or DEFAULT_CSP)
            super().end_headers()

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

        def _code(self, code: str) -> bool | None:
            """True for the access code, False for a wrong one, None (not compared) when this address has tried too
            many different wrong codes lately."""
            ip = self.client_address[0]
            if guesses.blocked(ip):
                return None
            if hmac.compare_digest(code.encode(), token.encode()):
                return True
            guesses.failed(ip, code)
            return False

        def _signed_in(self) -> bool:
            """Remote Studios: the browser opened the access link (its cookie), or a script sends the token. Team
            Studios: a member's session cookie or script token (self._member)."""
            self._throttled, self._member = False, None
            if team is not None:
                self._member = team.authenticate(self, home=studio.home)
                return self._member is not None
            if not token:
                return True
            jar = SimpleCookie()
            try:
                jar.load(self.headers.get("Cookie") or "")
            except Exception:  # noqa: BLE001  (a malformed cookie header is just "not signed in")
                pass
            got = jar[self._cookie_name()].value if self._cookie_name() in jar else (self.headers.get("X-Morph-Token") or "")
            if not got:
                return False
            ok = self._code(got)
            self._throttled = ok is None
            return bool(ok)

        def _sign_in_page(self, bad=False):
            if team is not None:  # invites and sign-in links are the way in (a removed member's browser is told why)
                status, page, ctype, csp = team.signed_out_page(home=studio.home, handler=self)
                return self._send(status, page, ctype, csp=csp)
            note = ('<p class="bad">Too many wrong access codes from this computer. Wait 15 minutes, then try again.</p>'
                    if bad == "throttled" else
                    '<p class="bad">That access code did not match. Copy it again from the terminal.</p>' if bad else "")
            page = SIGN_IN.replace("HOST", html.escape(socket.gethostname())).replace("BAD", note)
            return self._send(429 if bad == "throttled" else 401, page.encode(), "text/html; charset=utf-8", csp=SIGN_IN_CSP)

        def _refuse_sign_in(self):
            if team is not None:
                return self._error(401, "Sign in first: open your invite link, or ask the team owner for a sign-in link.")
            if self._throttled:
                return self._error(429, "Too many wrong access codes from this computer. Wait 15 minutes, then try again.")
            return self._error(401, "open the access link printed where Morph Studio was started")

        def _access_link(self, u) -> bool:
            """GET /?token=...: a correct code signs this browser in (a cookie) and drops the code from the URL."""
            q = parse_qs(u.query)
            if not token or "token" not in q or u.path not in ("/", "/index.html"):
                return False
            ok = self._code(q["token"][0].strip())
            if not ok:
                self._sign_in_page(bad="throttled" if ok is None else True)
                return True
            rest = urlencode([(k, v) for k, vs in q.items() if k != "token" for v in vs])
            self.send_response(302)
            self.send_header("Location", "/" + (f"?{rest}" if rest else ""))
            self.send_header("Set-Cookie", f"{self._cookie_name()}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=2592000")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return True

        def _send(self, code, body: bytes, ctype="application/json", csp=None):
            self.csp = csp
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code, value):
            return self._send(code, json.dumps(value).encode())

        def _error(self, code, message):
            self._drain()
            return self._json(code, {"ok": False, "error": message})

        def _drain(self):
            """Read and drop a request body refused before it was read (too big, wrong type, not signed in), so the
            client gets the answer instead of a connection reset while it is still sending. Bounded: at most
            DRAIN_MAX bytes, two seconds, and a quarter of a second without data."""
            if self.command not in ("POST", "PUT", "DELETE") or getattr(self, "_read", False):
                return
            self._read = True
            try:
                n = content_length(self.headers, DRAIN_MAX)
            except BodyError:
                return
            deadline, old = time.monotonic() + 2.0, self.connection.gettimeout()
            try:
                while n > 0 and time.monotonic() < deadline:
                    self.connection.settimeout(max(0.01, min(0.25, deadline - time.monotonic())))
                    chunk = self.rfile.read1(min(n, 1 << 16))
                    if not chunk:
                        break
                    n -= len(chunk)
            except OSError:
                pass
            finally:
                try:
                    self.connection.settimeout(old)
                except OSError:
                    pass

        def _file(self, path, csp=None):
            ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
            if path.endswith((".js", ".mjs")):
                ctype = "text/javascript"
            elif path.endswith(".glb"):
                ctype = "model/gltf-binary"
            elif path.endswith(".woff2"):  # Python's own table only knows it from 3.14
                ctype = "font/woff2"
            elif path.endswith((".ino", ".nc", ".gbr", ".drl", ".csv", ".urdf", ".xml", ".dxf")):
                ctype = "text/plain; charset=utf-8"
            try:
                if not stat.S_ISREG(os.stat(path).st_mode):  # a folder, or a pipe or device that would never answer
                    raise IsADirectoryError(path)
                fh = open(path, "rb")
            except OSError:  # missing, not a plain file, or unreadable
                return self._error(404, "Not found.")
            with fh:
                self.csp = csp
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(os.fstat(fh.fileno()).st_size))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                shutil.copyfileobj(fh, self.wfile, 1 << 16)

        def _route(self, u):
            """Dispatch to the route table (routes.py): body size, role, JSON body, handler, answer."""
            r = routes.find(self.command, u.path)
            if r is None:
                return self._error(404, "Not found.")
            try:
                n = content_length(self.headers, r.max_body)  # from the header alone: nothing is read yet
                if n and not r.raw_body and self.headers.get_content_type() != "application/json":
                    raise BodyError(415, "Send JSON, with Content-Type: application/json.")
            except BodyError as exc:
                return self._error(exc.status, str(exc))
            req = routes.Request(self, studio, self.command, u.path, parse_qs(u.query), n)
            try:
                req.member = (team.role_hook(req, r.role, getattr(self, "_member", None)) if team is not None
                              else routes.authorize(req, r.role))
                if not r.raw_body and self.command != "GET":
                    self._read = True
                    try:
                        raw = self.rfile.read(n) if n else b""
                    except OSError:  # the client stopped sending for `timeout` seconds, or went away
                        raw = b""
                    if len(raw) != n:
                        raise BodyError(400, "The request body was cut off.")
                    req.body = json_object(raw)
                out = r.fn(req)
            except HTTPError as exc:
                return self._error(exc.status, exc.message)
            except BodyError as exc:
                return self._error(exc.status, str(exc))
            except Exception as exc:  # noqa: BLE001  (a bug in one route answers 500 instead of dropping the connection)
                traceback.print_exc()
                if getattr(self, "_responded", False):
                    return None
                return self._error(500, f"The Studio hit an error: {exc.__class__.__name__}: {exc}")
            if out is None:
                return None
            if len(out) == 2:
                return self._json(out[0], out[1])
            return self._send(out[0], out[1], out[2])

        def do_GET(self):
            self._read = False
            u = urlparse(self.path)
            if not self._trusted(post=False):
                return self._error(403, "This Studio only answers its own page.")
            if self._access_link(u):
                return None
            if team is not None and team.landing(self, home=studio.home):  # /?invite= and /?link= pages, before sign-in
                return None
            if not self._signed_in():
                if u.path in ("/", "/index.html"):
                    return self._sign_in_page(bad="throttled" if self._throttled else False)
                return self._refuse_sign_in()
            if u.path in ("/", "/index.html"):
                return self._file(os.path.join(STATIC, "index.html"), csp=app_csp())
            if u.path.startswith("/static/"):
                full = contained_path(STATIC, u.path[len("/static/"):])
                return self._file(full) if full else self._error(404, "Not found.")
            return self._route(u)

        def _change(self):
            """POST, PUT and DELETE: also need the X-Morph-Client header, which another site's page can't send
            without the Studio agreeing first (a CORS preflight it never answers)."""
            self._read = False
            u = urlparse(self.path)
            if not self._trusted(post=True):
                return self._error(403, "This Studio only answers its own page.")
            r = routes.find(self.command, u.path)
            public = team is not None and r is not None and r.role == "public"  # the join form: before sign-in
            if not self._signed_in() and not public:
                return self._refuse_sign_in()
            if not public and not self.headers.get(CLIENT_HEADER):
                return self._error(403, f"Send the header {CLIENT_HEADER}: 1 with every POST, PUT and DELETE "
                                        "(the Studio page does).")
            return self._route(u)

        do_POST = do_PUT = do_DELETE = _change

    return H


# ------------------------------------------------------------------ routes (feature routes: morph/studio/api_*.py)

def _by(req) -> str | None:
    return (req.member or {}).get("id")


def _audit(req, event: str, **fields):
    """Teams' hash-chained security log (team Studios only): who changed keys or settings, exported, reset or deleted."""
    if req.studio.team:
        from .teams import audit
        audit.append(event, by=_by(req), home=req.studio.home, **fields)


def _queued(room: Room, job) -> dict:
    """A job as the queue shows it (with its position) right after it was queued."""
    return next((j for j in room.jobs.snapshot() if j["id"] == job.id), job.info())


def _base_rev(req, room: Room) -> str | None:
    """The revision the tab was showing when the person asked (body "base_rev"), if it still exists."""
    b = (req.body or {}).get("base_rev")
    return b if isinstance(b, str) and b in room.completed() else None


@route("GET", "/api/state", role="viewer")
def _api_state(req):
    return 200, req.studio.state(req.room)


@route("GET", "/api/rev", role="viewer")
def _api_rev(req):
    room, rev = req.room, req.arg("rev")
    if rev not in room.completed():
        raise HTTPError(404, "unknown rev")
    return 200, rev_payload(room.p, rev, room.name)


@route("GET", "/api/events", role="viewer")
def _api_events(req):
    room, h = req.room, req.handler
    if not getattr(h.server, "open_stream", lambda: True)():
        raise HTTPError(503, "There are as many open event streams as the Studio serves; the page tries again.")
    sel = selectors.DefaultSelector()
    q = room.hub.subscribe(_by(req))  # before the answer: a page that loads its state once the stream is open misses nothing
    try:
        h.send_response(200)
        h.send_header("Content-Type", "text/event-stream")
        h.send_header("Cache-Control", "no-store")
        h.end_headers()
        sel.register(h.connection, selectors.EVENT_READ)
        _presence(req, room, True)
        alive = _member_check(req)  # Teams: a removed (or signed-out-everywhere) member's open stream ends
        try:
            last = time.monotonic()
            while True:
                try:
                    data = f"data: {json.dumps(q.get(timeout=1.0))}\n\n".encode()
                except queue.Empty:
                    if _hung_up(sel, h.connection):  # the tab went away: its slot is free now, not after a failed ping
                        break
                    data = b": ping\n\n" if time.monotonic() - last >= 15 else b""
                if alive is not None and not alive():
                    h.wfile.write(alive.goodbye(room.name))
                    h.wfile.flush()
                    break
                if not data:
                    continue
                h.wfile.write(data)
                h.wfile.flush()
                last = time.monotonic()
        except OSError:  # tab closed (Windows: 10053) or stopped reading for `timeout` seconds
            pass
        finally:
            _presence(req, room, False)
    finally:
        room.hub.unsubscribe(q)
        sel.close()
        getattr(h.server, "close_stream", lambda: None)()
    return None


def _presence(req, room: Room, opened: bool):
    """Teams: who has the project open (each member's open tabs), told to the room's tabs."""
    if req.studio.team and req.member:
        from . import api_team
        try:
            api_team.stream(room.name, req.member, opened, send=room.hub.send, home=req.studio.home)
        except Exception:  # noqa: BLE001  (presence never breaks the event stream)
            traceback.print_exc()


def _member_check(req):
    """Teams: a check, rate-limited, that the stream's member is still signed in (api_team.StreamCheck); None otherwise."""
    if not (req.studio.team and req.member):
        return None
    from . import api_team
    return api_team.stream_check(req.handler, req.member, home=req.studio.home)


def _hung_up(sel, sock) -> bool:
    """True once an event stream's client has closed the connection (a tab never sends anything after its request,
    so a readable socket that reads as empty means it is gone)."""
    try:
        return bool(sel.select(0)) and sock.recv(1, socket.MSG_PEEK) == b""
    except (BlockingIOError, InterruptedError):
        return False
    except (OSError, ValueError):
        return True


@route("GET", "/files/*", role="viewer")
def _files(req):
    full = req.studio.artifact(req.path[len("/files/"):])
    if not full:
        raise HTTPError(404, "Not found.")
    if req.studio.team:  # order packages of a revision nobody released yet follow the team's policy (and are audited)
        from . import api_team
        project, _, inner = os.path.relpath(full, req.studio.home).replace(os.sep, "/").partition("/")
        res = api_team.check_download(req.member, os.path.join(req.studio.home, project), inner, home=req.studio.home)
        if not res.get("allowed"):
            raise HTTPError(403, res.get("reason") or "That file needs a released revision.")
    return req.handler._file(full, csp=ARTIFACT_CSP)


@route("GET", "/api/jobs", role="viewer")
def _api_jobs(req):
    return 200, {"jobs": req.room.jobs.snapshot()}


@route("POST", "/api/chat")
def _api_chat(req):
    msg = req.body.get("message")
    if not isinstance(msg, str) or not msg.strip():
        raise HTTPError(400, "Type a message first.")
    if len(msg) > MAX_MESSAGE:
        raise HTTPError(400, f"That message is too long: {MAX_MESSAGE:,} characters at most.")
    if looks_like_secret(msg):
        raise HTTPError(400, "That looks like an API key. Paste keys in the Keys dialog, never in chat; this message "
                             "was not sent or saved.")
    room = req.room
    try:  # {edit: true}: the person chose "Change <project> instead" for a message that read like a new robot
        job = req.studio.queue_chat(room, msg.strip(), base_rev=_base_rev(req, room), by=_by(req),
                                    route=req.body.get("edit") is not True)
    except QueueFull as exc:
        raise HTTPError(429, str(exc)) from None
    return 202, {"ok": True, "job": _queued(room, job)}


@route("POST", "/api/params")
def _api_params(req):
    try:
        changes = param_changes(req.body.get("changes"), PARAMS)
    except ValueError as exc:
        raise HTTPError(400, str(exc)) from None
    room = req.room
    try:
        job = req.studio.queue_params(room, changes, base_rev=_base_rev(req, room), by=_by(req))
    except QueueFull as exc:
        raise HTTPError(429, str(exc)) from None
    return 202, {"ok": True, "job": _queued(room, job)}


@route("POST", "/api/jobs/cancel")
def _api_jobs_cancel(req):
    job = req.room.jobs.cancel(req.body.get("job"))
    if job is None:
        raise HTTPError(409, "That change has already started, or is not in the queue.")
    return 200, {"ok": True, "job": job.info()}


@route("POST", "/api/jobs/resolve")
def _api_jobs_resolve(req):
    action = req.body.get("action")
    if action not in ("apply", "drop"):
        raise HTTPError(400, 'Send {"job": id, "action": "apply" | "drop"}.')
    room = req.room
    try:
        job = req.studio.resolve(room, req.body.get("job"), action, by=_by(req))
    except LookupError as exc:
        raise HTTPError(409, str(exc)) from None
    except QueueFull as exc:
        raise HTTPError(429, str(exc)) from None
    return 202, {"ok": True, "job": _queued(room, job) if job else None}


@route("POST", "/api/source")
def _api_source(req):
    rev, room = req.body.get("rev"), req.room
    if rev is not None and (not isinstance(rev, str) or rev not in room.completed()):
        raise HTTPError(400, "unknown rev")
    threading.Thread(target=req.studio.source, args=(rev, room), daemon=True).start()
    return 202, {"ok": True}


@route("POST", "/api/project", role="viewer")
def _api_project(req):
    """Older pages switched every tab here. A tab now chooses its project with ?project= and this only checks the
    name, so it moves nobody."""
    name = req.body.get("name")
    try:
        project_name(name)
    except ValueError as exc:
        raise HTTPError(400, str(exc)) from None
    if not req.studio._root(name) and name != req.studio.start:
        raise HTTPError(404, f"There is no robot project called {name}.")
    return 200, {"ok": True, "project": name}


@route("GET", "/api/projects", role="viewer")
def _api_projects(req):
    return 200, {"projects": req.studio.projects(), "start": req.studio.start}


def _text(body: dict, key: str, limit: int) -> str | None:
    value = body.get(key)
    if value is not None and (not isinstance(value, str) or len(value) > limit):
        raise HTTPError(400, f"{key} must be text of at most {limit} characters.")
    return value


@route("POST", "/api/projects")
def _api_projects_create(req):
    """{name?, from: "default" | "so101" | "design", design?, need?, brief_id?}: a new project, its first build queued."""
    body, kind = req.body, req.body.get("from", "default")
    if kind not in ("default", "so101", "design"):
        raise HTTPError(400, 'from must be "default", "so101" or "design".')
    name, need, brief = _text(body, "name", 80), _text(body, "need", 2000), _text(body, "brief_id", 64)
    if brief is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", brief):
        raise HTTPError(400, "brief_id is not a brief id.")
    design = None
    try:
        if kind == "design":
            design = projects.as_design(body.get("design"))
            projects.check_design(design)
        room, job = req.studio.create_project(name, source="so101" if kind == "so101" else "", design=design, need=need,
                                              origin="brief" if brief else "default", by=_by(req),
                                              meta={"brief_id": brief} if brief else None)
    except FileExistsError:
        raise HTTPError(409, "A project with that name already exists.") from None
    except ValueError as exc:
        raise HTTPError(400, str(exc)) from None
    return 202, {"ok": True, "project": room.name, "job": _queued(room, job)}


@route("POST", "/api/projects/fork")
def _api_projects_fork(req):
    """{project, rev?, name?}: a copy of a project's revision as a new project (copied, or built when the files are
    from an older Morph)."""
    project, rev, name = req.body.get("project"), _text(req.body, "rev", 16), _text(req.body, "name", 80)
    try:
        project_name(project)
        room, job, info = req.studio.fork_project(project, rev, name, by=_by(req))
    except ValueError as exc:
        raise HTTPError(400, str(exc)) from None
    except LookupError as exc:
        raise HTTPError(404, str(exc)) from None
    return (202 if job else 201), {"ok": True, "project": room.name, "copied": info["copied"], "rev": info["rev"],
                                   "job": _queued(room, job) if job else None}


@route("DELETE", "/api/projects", role="owner")
def _api_projects_delete(req):
    """?project=: move that project to MORPH_HOME/.trash/ (nothing is erased)."""
    name = req.arg("project")
    try:
        project_name(name)
        trash = req.studio.delete_project(name)
    except ValueError as exc:
        raise HTTPError(400, str(exc)) from None
    except LookupError as exc:
        raise HTTPError(404, str(exc)) from None
    except Refused as exc:
        raise HTTPError(409, str(exc)) from None
    except OSError as exc:
        raise HTTPError(409, f"Couldn't move {name} to the trash ({exc.strerror or exc}); close any file of it that is "
                             "open, then try again.") from None
    _audit(req, "project_deleted", project=name)
    return 200, {"ok": True, "project": name, "trash": os.path.relpath(trash, req.studio.home).replace(os.sep, "/")}


@route("POST", "/api/reset", role="owner")
def _api_reset(req):
    ok = req.studio.reset(rebuild=req.body.get("rebuild") is True)
    if ok:
        _audit(req, "reset", rebuild=req.body.get("rebuild") is True)
    return (200 if ok else 409), {"ok": ok, "project": req.studio.start}


@route("GET", "/api/export", role="viewer")
def _api_export(req):
    from . import export
    room, rev, h = req.room, req.arg("rev"), req.handler
    full = req.arg("full", "") in ("1", "true", "yes")
    keep, extra, drafts = None, None, set()
    try:
        if req.studio.team:  # the team's download policy for order files, and its record of the revision (team.json)
            from . import api_team
            from .teams.collab import CollabError
            try:
                keep, extra, drafts = api_team.export_parts(req.member, room.p.root, rev, full, home=req.studio.home)
            except CollabError as exc:  # no such finished revision: the export says so in its own words, else the team's
                export.check(room.p, rev, full=full)  # reason (nothing goes out without the team's record)
                raise HTTPError(exc.status, str(exc)) from None
        if "check" in req.query:  # the button asks first, so a refusal is a message rather than a broken download
            export.check(room.p, rev, full=full, keep=keep)
            return 200, {"ok": True}
        archive, filename = export.build_export(room.p, rev, full=full, keep=keep, extra=extra)
    except export.ExportError as exc:
        raise HTTPError(exc.status, str(exc)) from None
    _audit(req, "export", project=room.name, rev=rev or "", full=full,
           **({"draft_order_files": sorted(drafts)[:20]} if drafts else {}))
    with archive:
        size = archive.seek(0, os.SEEK_END)
        archive.seek(0)
        h.send_response(200)
        h.send_header("Content-Type", "application/zip")
        h.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        h.send_header("Content-Length", str(size))
        h.send_header("Cache-Control", "no-store")
        h.end_headers()
        shutil.copyfileobj(archive, h.wfile, 1 << 16)
    return None


def _keys_state(studio: Studio) -> dict:
    from .. import keys, settings
    return {"keys": keys.status(home=studio.home), "integrations": integrations(), "settings": settings.load(),
            "file": os.path.basename(keys.ENV_PATH), "compute": compute_info(studio.remote)}


@route("GET", "/api/keys", role="owner")
def _api_keys(req):
    return 200, _keys_state(req.studio)


@route("POST", "/api/keys", role="owner")
def _api_keys_save(req):
    from .. import keewano, keys
    values = req.body.get("values") or {}
    if not isinstance(values, dict):
        raise HTTPError(400, "Send the keys as {\"values\": {\"NAME\": \"key\"}}.")
    try:
        changed = keys.save(values, home=req.studio.home)
    except ValueError as exc:
        raise HTTPError(400, str(exc)) from None
    except OSError as exc:
        raise HTTPError(500, f"Couldn't save the keys: Morph can't write {os.path.basename(keys.ENV_PATH)} here "
                             f"({exc.strerror or exc.__class__.__name__}). Set them as environment variables instead, "
                             "or move Morph to a folder you can write to.") from None
    _audit(req, "keys_changed", names=sorted(changed))  # names only, never a key
    if any(k.startswith("KEEWANO") for k in changed):
        keewano._ratio_cache.clear()  # re-query the calibration with the new project
    if "SUBQ_API_KEY" in changed:
        from . import subq
        subq.reset()  # a new key gets a fresh start (a refused or out-of-credit key switched SubQ off)
    from . import api_tools
    resumed = api_tools.keys_saved(req, changed)  # agent runs that waited for one of these keys continue
    return 200, {"ok": True, "changed": changed, "resumed": resumed, **_keys_state(req.studio)}


@route("POST", "/api/keys/check", role="owner")
def _api_keys_check(req):  # POST: it calls the services with the person's keys
    from .. import keys
    services = req.body.get("services")
    return 200, {"checks": keys.check(services if isinstance(services, list) else None)}


@route("GET", "/api/settings", role="viewer")
def _api_settings(req):
    from .. import settings
    return 200, {"settings": settings.load(), "integrations": integrations()}


@route("POST", "/api/settings", role="owner")
def _api_settings_save(req):
    from .. import keewano, settings
    try:
        values = settings.save(req.body.get("values"))
    except ValueError as exc:
        raise HTTPError(400, str(exc)) from None
    except OSError as exc:
        raise HTTPError(500, f"Couldn't save the settings ({exc.strerror or exc.__class__.__name__}).") from None
    _audit(req, "settings_changed", values=values)
    keewano._ratio_cache.clear()
    return 200, {"ok": True, "settings": values, "integrations": integrations()}


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second Studio bind the same port while this one still listens
    allow_reuse_address = os.name != "nt"
    max_connections, max_streams = MAX_CONNECTIONS, MAX_STREAMS
    request_queue_size = MAX_CONNECTIONS  # a burst waits in the listen queue for its answer (or 503), not in SYN retries

    def __init__(self, *args, **kwargs):
        self.slots, self.connections, self.streams = threading.Lock(), 0, 0
        self.turned_away, self.closer = queue.SimpleQueue(), None
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        """A thread per connection, up to max_connections at once; past that, a 503 straight from here, without
        reading the request, so idle or slow clients can never take every thread."""
        with self.slots:
            ok = self.connections < self.max_connections
            self.connections += ok
        if not ok:
            return self._busy(request)
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release()

    def _release(self):
        with self.slots:
            self.connections -= 1

    def open_stream(self) -> bool:
        with self.slots:
            ok = self.streams < self.max_streams
            self.streams += ok
            return ok

    def close_stream(self):
        with self.slots:
            self.streams -= 1

    def _busy(self, request):
        """The 503, sent before the client has necessarily said anything. Its request may still be on the way, and
        closing a socket that then receives data resets the connection (Windows throws away an answer the client
        hasn't read yet), so the socket goes to the closer thread, which reads what the client sends until it
        closes or LINGER seconds pass. The accept loop never waits on a turned-away client."""
        try:
            request.settimeout(0.5)
            request.sendall(b"HTTP/1.0 503 Service Unavailable\r\nContent-Type: application/json\r\nCache-Control: no-store\r\n"
                            b"X-Content-Type-Options: nosniff\r\nRetry-After: 5\r\nConnection: close\r\n"
                            b"Content-Length: %d\r\n\r\n%s" % (len(BUSY), BUSY))
            request.shutdown(socket.SHUT_WR)
            request.setblocking(False)
        except OSError:
            return self.shutdown_request(request)
        with self.slots:
            if self.closer is None:
                self.closer = threading.Thread(target=self._linger, name="morph-closer", daemon=True)
                self.closer.start()
        self.turned_away.put((request, time.monotonic() + LINGER))

    def _linger(self):
        sel, due = selectors.DefaultSelector(), {}
        while True:
            try:
                while True:  # wait for a socket only while none lingers
                    s, until = self.turned_away.get(block=not due)
                    try:
                        if len(due) >= self.max_connections:  # a flood: no room to wait for this one
                            raise ValueError
                        sel.register(s, selectors.EVENT_READ)
                        due[s] = until
                    except (ValueError, OSError):
                        self.close_request(s)
            except queue.Empty:
                pass
            for key, _ in sel.select(timeout=0.05):
                try:
                    if key.fileobj.recv(65536):
                        continue
                except OSError:
                    pass
                due[key.fileobj] = 0  # the client closed (or the connection broke): nothing left to wait for
            now = time.monotonic()
            for s in [s for s, until in due.items() if until <= now]:
                sel.unregister(s)
                del due[s]
                self.close_request(s)

    def handle_error(self, request, client_address):
        # a browser tab closing mid-response is normal; don't print a traceback for it
        import sys
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)


def serve(project: RobotProject, host="127.0.0.1", port=8765, home: str | None = None, token: str | None = None, *,
          team: bool = False, allow_url_import: bool = False, link: str | None = None):
    """Run the Studio. `team`: Morph Teams (members sign in with invite or sign-in links; MORPH_TOKEN plays no part),
    `link` the owner's sign-in link to print; `allow_url_import`: imports from a link stay on for other computers."""
    if not project.revs():
        raise SystemExit(f"{project.root} has no revisions yet: run `python -m morph.cli robot new {os.path.basename(project.root)}` first")
    studio = Studio(project, home, team=team, allow_url_import=allow_url_import)
    token = None if team else access_token(host, token)
    studio.remote = bool(token) or (team and not is_loopback(host))
    if team:
        from . import api_team
        api_team.configure(home=studio.home, secure_cookies=False, enabled=True)
    from . import capability
    capability.warm(block=False)  # the brief's envelope from the generator, in the background
    # local Studio: requests must name this server (127.0.0.1/localhost); remote Studio: the access token decides; a team
    # Studio: the member's session decides, so a tunnel's or the LAN's address works too
    allowed = None if token or team else {f"{h}:{port}" for h in ("127.0.0.1", "localhost", host)}
    try:
        httpd = _Server((host, port), make_handler(studio, allowed, token))
    except OSError as exc:
        if exc.errno in (errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", errno.EADDRINUSE)):
            raise SystemExit(f"Port {port} is busy ({exc}): Morph Studio is probably already running. Stop it "
                             "(Ctrl+C in its terminal, or close that window), then start again.") from None
        raise SystemExit(f"Morph Studio couldn't listen on {host}:{port} ({exc}).") from None
    httpd.daemon_threads = True
    if team:
        where = (lan_address() or socket.gethostname()) if host in ("0.0.0.0", "") else host
        print(f"Morph Studio (Teams preview) is running on {socket.gethostname()} at http://{where}:{port}"
              + ("" if is_loopback(host) else " for other computers too") + ".")
        if link:
            print("Open this one-time link to sign in as the owner (it works once, within 15 minutes):")
            print(f"    {link}")
        print("Invite teammates from Share in the Studio, or with `morph team invite`. Ctrl+C to stop.")
    elif token:
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
    finally:
        from . import jobs
        jobs.shutdown(studio)  # robustness studies stop and keep their runs; the same settings carry on next time


FEATURE_ERRORS = {}  # feature module -> why it didn't load (its routes are off; the rest of the Studio works)


def _load_feature_routes():
    """Feature modules (morph/studio/api_<feature>.py) register their routes when imported."""
    for m in sorted(pkgutil.iter_modules([os.path.dirname(__file__)]), key=lambda m: m.name):
        if m.name.startswith("api_"):
            try:
                importlib.import_module(f"{__package__}.{m.name}")
            except Exception as exc:  # noqa: BLE001
                traceback.print_exc()
                FEATURE_ERRORS[m.name] = f"{exc.__class__.__name__}: {exc}"


_load_feature_routes()
