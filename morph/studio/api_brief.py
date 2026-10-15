"""HTTP handlers for the New robot brief: POST /api/brief and GET /api/brief (RELEASE_CONTRACTS section 2).

Written against the morph.studio.routes contract (W0b). The module registers both routes with the Studio's hooks when
the Studio loads its feature modules (studio_hooks below); register(route, ...) takes other hooks for tests and demos:

    from morph.studio import api_brief
    api_brief.register(route, create_project=...)   # or call api_brief.post / api_brief.get from your handlers

POST {text?, brief_id?, answers?: {slot: value}, action?: "defaults" | "build" | "cancel", name?, stop_on_issues?}
  text alone starts a brief; answers and/or text with brief_id continue it; "defaults" fills every open slot (on an
  out-of-scope brief it accepts the nearest robot); "build" returns what the project service needs; "cancel" drops it.
  "build" with stop_on_issues (New robot's Build now) on a brief nobody has confirmed yet fills the defaults and, when
  the preflight then has issues, answers 200 {ok, brief_id, brief, stopped: "preflight"} instead of building, so the
  person sees the issues on the confirm card first ("Build anyway" builds a confirmed brief as it is).
  Conversation turns run on a worker thread because SubQ and Claude can take seconds: the reply is 202
  {ok, brief_id, busy: true, brief}, the timeline streams as SSE `agent` events and the result as a `brief` event.
  GET /api/brief?id= re-reads a brief (after an SSE reconnect, or to poll).
"""

from __future__ import annotations

import json
import os
import threading
import time
import traceback

from . import agents
from . import brief as B
from . import capability, routes
from .rooms import RESUMED
from .security import holds_secret, looks_like_secret, secret_refused

ACTIONS = ("defaults", "build", "cancel", "nearest")
MAX_TEXT = 2000
MAX_IN_MEMORY = 200


class Store:
    """Briefs in memory, mirrored to <home>/.briefs/<id>.json drafts (deleted after 7 days)."""

    def __init__(self):
        self.mem, self.lock, self.busy, self.cleaned = {}, threading.Lock(), set(), set()

    def get(self, home: str, bid: str) -> B.Brief | None:
        with self.lock:
            b = self.mem.get((home, bid))
        return b if b is not None else (B.load_draft(home, bid) if home else None)

    def put(self, home: str, b: B.Brief):
        b.updated = time.time()
        with self.lock:
            self.mem[(home, b.id)] = b
            if len(self.mem) > MAX_IN_MEMORY:
                for k in sorted(self.mem, key=lambda k: self.mem[k].updated)[:len(self.mem) - MAX_IN_MEMORY]:
                    self.mem.pop(k, None)
            first = home not in self.cleaned
            self.cleaned.add(home)
        if home:
            try:
                if first:
                    B.cleanup_drafts(home)
                B.save_draft(b, home)
            except OSError:
                pass  # a read-only projects folder keeps the brief in memory only

    def drop(self, home: str, bid: str):
        with self.lock:
            self.mem.pop((home, bid), None)
        if home:
            B.delete_draft(home, bid)

    def claim(self, bid: str) -> bool:
        with self.lock:
            if bid in self.busy:
                return False
            self.busy.add(bid)
            return True

    def release(self, bid: str):
        with self.lock:
            self.busy.discard(bid)


STORE = Store()


def _err(status: int, msg: str):
    return status, {"ok": False, "error": msg}


def _home(req, home):
    return home or getattr(getattr(req, "studio", None), "home", None)


def _send_for(req):
    """Events go to the tab's room and carry its project (RELEASE_CONTRACTS section 3)."""
    room = getattr(req, "room", None)
    hub = getattr(room, "hub", None) or getattr(getattr(req, "studio", None), "hub", None)
    project = getattr(room, "name", None) or getattr(room, "project", None)

    def send(ev):
        if hub is not None:
            hub.send(dict(ev, project=ev.get("project") or project))
    return send


def event(b: B.Brief, busy: bool = False) -> dict:
    """The SSE `brief` event: {id, status, round, questions, slots, assumptions, scope, preflight, ...}. `project` is
    left for the room the conversation runs in (section 3); the project a build created is `built_project`."""
    d = b.to_dict()
    d["built_project"] = d.pop("project", None)
    return {"type": "brief", **d, "busy": busy}


def _valid_answers(answers) -> tuple[dict | None, str]:
    if answers is None:
        return {}, ""
    if not isinstance(answers, dict) or len(answers) > len(B.SLOTS):
        return None, "answers must be an object of {slot: value}"
    if len(json.dumps(answers, default=str)) > 4000:
        return None, "answers are too long"
    for k, v in answers.items():
        if k not in B.SLOTS:
            return None, f"unknown slot {k!r}"
        if not (v is None or isinstance(v, (str, int, float)) or (k == "object" and isinstance(v, dict))):
            return None, f"bad value for {k}"
    return answers, ""


def post(req, send=None, *, home: str | None = None, open_project: dict | None = None, create_project=None,
         sync: bool = False, revive=None):
    """POST /api/brief. `send(ev)` gets the agent and brief events; `create_project(payload)` (optional) is the W0b
    project service hook for action "build" and returns {"project": name, ...}; `revive(project, brief_id)` (optional)
    says whether a brief's project still builds or has its first revision (else Build makes it again)."""
    home = _home(req, home)
    send = send or _send_for(req)
    body = getattr(req, "body", None)
    if not isinstance(body, dict):
        return _err(400, "expected a JSON object")
    text, bid, action, name = body.get("text"), body.get("brief_id"), body.get("action"), body.get("name")
    if text is not None and not isinstance(text, str):
        return _err(400, "text must be a string")
    text = (text or "").strip()
    if len(text) > MAX_TEXT:
        return _err(400, f"keep the description under {MAX_TEXT} characters")
    if action is not None and action not in ACTIONS:
        return _err(400, f"action must be one of {', '.join(ACTIONS[:3])}")
    if name is not None and (not isinstance(name, str) or not B.NAME_RE.match(name)):
        return _err(400, "project names use lower-case letters, digits and dashes (up to 40)")
    answers, why = _valid_answers(body.get("answers"))
    if answers is None:
        return _err(400, why)
    if looks_like_secret(text) or holds_secret(answers) or looks_like_secret(name):  # before anything is kept or sent
        return _err(400, secret_refused("in a robot description"))
    if bid is None:
        if action or answers:
            return _err(400, "start with a description of the robot")
        if not text:
            return _err(400, "describe the robot you need")
        b = B.Brief(id=B.new_id(), request=text)
        b.turns.append({"role": "user", "text": text, "ts": B._now_iso()})
        fresh = True
    else:
        if not isinstance(bid, str) or not B.ID_RE.match(bid):
            return _err(400, "bad brief_id")
        b = STORE.get(home, bid)
        if b is None:
            return _err(404, "no such brief (drafts are kept for 7 days)")
        fresh = False
    if action == "cancel":
        b.flags["cancelled"] = True        # a turn still running on this brief must not save it back
        STORE.drop(home, b.id)
        send({"type": "brief", "id": b.id, "status": "cancelled"})
        return 200, {"ok": True, "brief_id": b.id, "status": "cancelled"}
    if b.status in ("building", "built") and action != "build":
        return _err(409, "this brief is already building; start a new one for another robot")
    if not STORE.claim(b.id):
        return _err(409, "still working on the last answer")
    if action == "build":
        try:
            return _build(b, home, name, send, create_project, revive,
                          stop_on_issues=body.get("stop_on_issues") is True)
        finally:
            STORE.release(b.id)

    def work():
        try:
            agents.run_turn(b, text=text or None, answers=answers or None, action=action, fresh=fresh, on_event=send,
                            open_project=open_project, home=home)
        except Exception as exc:  # noqa: BLE001  run_turn never raises; this is the last line of defence
            traceback.print_exc()
            b.flags.setdefault("notes", []).append(f"Something went wrong reading that ({exc.__class__.__name__}); try again.")
        finally:
            live = not b.flags.get("cancelled")
            if live:
                STORE.put(home, b)
            STORE.release(b.id)
            if live:
                send(event(b))

    STORE.put(home, b)
    if sync:
        work()
        return 200, {"ok": True, "brief_id": b.id, "busy": False, "brief": b.to_dict()}
    threading.Thread(target=work, daemon=True, name=f"brief-{b.id}").start()
    return 202, {"ok": True, "brief_id": b.id, "busy": True, "brief": b.to_dict()}


def _build(b: B.Brief, home, name, send, create_project, revive=None, stop_on_issues: bool = False):
    if b.status == "building" and b.project and create_project is not None:  # asked again (a second click, a retry):
        if revive is None or revive(b.project, b.id):                       # the project it is building, never a second
            return 200, {"ok": True, "brief_id": b.id, "build": B.build_payload(b, None, home), "project": b.project,
                         "already": True}
        b.status, b.project = "ready", None  # its project is gone, or never got its first build (a stop): build anew
    unseen = b.status in ("asking", "out_of_scope")
    if unseen:
        agents.run_turn(b, action="build", on_event=send, home=home)   # code only: defaults (or the nearest robot), preflight
    if b.status not in ("ready", "building"):
        STORE.put(home, b)
        send(event(b))
        return _err(409, "this request can't be built" if b.status == "refused" else "the brief isn't ready to build")
    if unseen and stop_on_issues and b.status == "ready" and (b.preflight or {}).get("issues"):
        STORE.put(home, b)                 # the confirm card shows the issues; Build anyway builds it as it is
        send(event(b))
        return 200, {"ok": True, "brief_id": b.id, "busy": False, "brief": b.to_dict(), "stopped": "preflight"}
    try:
        payload = B.build_payload(b, name, home)
    except ValueError as exc:
        return _err(409, str(exc))
    b.status, b.name = "building", payload["name"]
    out = {"ok": True, "brief_id": b.id, "build": payload}
    if create_project is not None:
        try:
            res = create_project(payload) or {}
            out["project"] = res.get("project", payload["name"])
            b.project = out["project"]
        except Exception as exc:  # noqa: BLE001  the project service reports its own errors
            b.status = "ready"
            STORE.put(home, b)
            send(event(b))
            return _err(409, f"couldn't create the project: {exc}")
    STORE.put(home, b)
    send(event(b))
    return 200, out


def get(req, *, home: str | None = None):
    """GET /api/brief?id=<brief id>."""
    home = _home(req, home)
    q = getattr(req, "query", None) or {}
    bid = (q.get("id") or [None])[0] if isinstance(q.get("id"), list) else q.get("id")
    if not isinstance(bid, str) or not B.ID_RE.match(bid):
        return _err(400, "bad id")
    b = STORE.get(home, bid)
    if b is None:
        return _err(404, "no such brief")
    return 200, {"ok": True, "brief": b.to_dict(), "busy": bid in STORE.busy}


def on_built(home: str, brief_id: str, project: str, rev: str | None = None, project_root: str | None = None) -> B.Brief | None:
    """Integration calls this when the first revision exists: status built, <project>/brief.json, draft deleted."""
    b = STORE.get(home, brief_id)
    if b is None:
        return None
    B.mark_built(b, project, rev)
    if project_root:
        try:
            B.write_final(b, project_root)
        except OSError:
            pass
    STORE.drop(home, brief_id)
    return b


def register(route, *, home_for=None, send_for=None, create_project=None, create_project_for=None, open_project_for=None,
             revive_for=None):
    """Register both routes with the W0b router decorator: route(method, path, role=...). The project service hook
    for action "build" is `create_project(payload)`, or `create_project_for(req)` returning one for this request."""
    @route("POST", "/api/brief", role="editor")
    def _post(req):
        return post(req, (send_for or _send_for)(req), home=home_for(req) if home_for else None,
                    open_project=open_project_for(req) if open_project_for else None,
                    create_project=create_project_for(req) if create_project_for else create_project,
                    revive=revive_for(req) if revive_for else None)

    @route("GET", "/api/brief", role="viewer")
    def _get(req):
        return get(req, home=home_for(req) if home_for else None)

    return _post, _get


# ------------------------------------------------------------------ the Studio's hooks (contracts section 2)
def _studio_home(req):
    return getattr(getattr(req, "studio", None), "home", None)


def _open_project(req):
    """The project this tab is on, for routing a typed line (a change to it, or a new robot)."""
    room = getattr(req, "room", None)
    return {"name": room.name} if room is not None else None


def _studio_create(req):
    """W0b's project service for action "build": the brief's changes clamped to the capability envelope first (code
    decides; projects.create also refuses anything past the design limits), a new project with origin "brief" and the
    brief's id in meta.json, its first build queued in its own room, and project_created to every tab, the originating
    one included. When that build ends the brief is marked built and the after-build agents run (_first_build)."""
    studio = getattr(req, "studio", None)
    if studio is None or not callable(getattr(studio, "create_project", None)):
        return None
    by = (getattr(req, "member", None) or {}).get("id")

    def create(pay: dict) -> dict:
        from ..spec import Design

        changes, notes = capability.clamp(pay["changes"], allow=("name", "arm.source", "arm.actuator_overrides"))
        meta = pay.get("meta") or {}
        room, job = studio.create_project(pay["name"], design=Design().with_changes(changes), need=meta.get("need"),
                                          origin="brief", by=by, meta={"brief_id": meta.get("brief_id")})
        studio.when_done(job, lambda j: _first_build(studio, room, j, meta.get("brief_id")))
        return {"project": room.name, "job": job.id, "clamped": notes}
    return create


def _studio_revive(req):
    """The Studio's answer to "is this brief's project still on its way?" for a brief that reads "building": True while
    the project builds or has a finished revision. False when it is gone, or when it is empty and idle (a stop cut its
    first build off and nothing will build it): this brief's own empty project then goes to .trash first, so Build makes
    it again under the same name."""
    studio = getattr(req, "studio", None)
    if studio is None or not callable(getattr(studio, "room", None)):
        return None

    def revive(project: str, brief_id: str) -> bool:
        try:
            room = studio.room(project)
        except (ValueError, LookupError):
            return False
        if room.jobs.busy() or room.completed():
            return True
        from . import projects
        if projects.read_meta(room.p.root).get("brief_id") == brief_id and room.name != studio.start:
            try:
                studio.delete_project(project)
            except Exception:  # noqa: BLE001  (it stays; the new project gets the next free name)
                traceback.print_exc()
        return False
    return revive


def _resumed(studio, room, job, meta):
    """A brief's first build that the Studio queued again when it started (a stop had cut it off): the brief is marked
    built (or ready to build again) when it ends, as after the first try (_first_build)."""
    bid = (meta or {}).get("brief_id")
    if isinstance(bid, str) and B.ID_RE.match(bid):
        studio.when_done(job, lambda j: _first_build(studio, room, j, bid))


def _claim(brief_id: str) -> bool:
    """Wait for the request that created the project to finish with the brief (it saves it last), then hold it."""
    for _ in range(1200):
        if STORE.claim(brief_id):
            return True
        time.sleep(0.05)
    return False


def _first_build(studio, room, job, brief_id):
    """After the new project's first build: brief.json written and the draft deleted (on_built), then the agents
    timeline's last steps in the new room: Darwin sourcing when sourcing is on, Keewano when its events are on."""
    from .. import keewano, settings

    if not job.result:
        room.hub.send({"type": "agent", "role": "pipeline", "provider": "code", "status": "error", "ms": None,
                       "brief": brief_id, "text": f"The first build failed: {str(job.error or job.status)[:200]}"})
    if not brief_id or not _claim(brief_id):
        return
    try:
        if not job.result:  # the brief can be built again (a new project); the failed one stays for a look
            b = STORE.get(studio.home, brief_id)
            if b is not None and b.status == "building" and b.project == room.name:
                b.status, b.project = "ready", None
                STORE.put(studio.home, b)
            return
        b = on_built(studio.home, brief_id, room.name, job.result, room.p.root)
    finally:
        STORE.release(brief_id)
    if b is None:
        return
    try:
        summary = room.p.load_results(job.result).get("summary") or {}
    except (OSError, ValueError):
        summary = None
    source = (lambda rev: studio.source(rev, room)) if settings.network_allowed() and settings.sourcing_enabled() else None
    send = (lambda project, events: keewano.send(project, events)) if (
        settings.telemetry_enabled() and os.environ.get("KEEWANO_API_KEY")) else None
    agents.after_build(b, room.name, job.result, summary, source=source, keewano_send=send, on_event=room.hub.send,
                       no_source="Darwin lookups are off in Keys, Privacy and network; the BOM tab shows saved answers")


register(routes.route, home_for=_studio_home, create_project_for=_studio_create, open_project_for=_open_project,
         revive_for=_studio_revive)
RESUMED.append(_resumed)
