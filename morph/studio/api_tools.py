"""Agent tools in the Studio (beta): the toolbox's routes and roles, its live events on the room hubs, the agents a person
may start, and what a Studio start and a key save do for the runs that wait (RELEASE_CONTRACTS section 15).

    GET  /api/tools                   viewer  every catalog tool, on or off, its key (set or not), approval and warnings
    POST /api/tools/enable|disable    owner   {tool}
    GET  /api/runs                    viewer  ?project= (the tab's), ?status=, ?agent=: run views (status, what they wait
                                              for, cards, result; never message text), what this member may do (`can`)
                                              and whether runs can start now (`ready`)
    GET  /api/runs/answer             viewer  ?run=: a finished run's closing summary, for its card
    POST /api/runs/start              editor  {agent, rev?}: one of the agents registered here (Live prices: price_check)
    POST /api/runs/decide             editor  {run, decisions: {tool_use_id: allow_once | allow_always | skip | deny}}:
                                              editors may skip; only the owner allows or denies
    POST /api/runs/resume|cancel      editor  {run}

Events go to the run's room (every tab when a run has no project): run_started, credential_needed, tool_request,
tool_used, run_paused, run_resumed, run_done, run_failed; they never carry a key, a tool's answer or message text. A
Studio start turns runs it left working into interrupted pauses; POST /api/keys (server.py) resumes the runs that waited
for a key it saved. While "agent tools" are off (Keys, Privacy and network) no run starts or continues.
"""

from __future__ import annotations

import re
import threading
import traceback

from .routes import HTTPError, route
from .toolbox import Toolbox, ToolboxError
from .toolbox.runs import ENDED, RUN_ID

STATUSES = ("running", "paused", "done", "failed", "refused", "cancelled")
AGENTS: dict = {}  # name -> {"name", "title", "start": fn(studio, room, body, by) -> (view, already)}
_LOCK = threading.Lock()


def agent(name: str, *, title: str, start):
    """Let people start an agent from the Studio (POST /api/runs/start {agent: name})."""
    AGENTS[name] = {"name": name, "title": title, "start": start}


# ------------------------------------------------------------------ the Studio's toolbox
def attach(studio) -> Toolbox:
    """studio.toolbox, made once per Studio: its events go to the run's room, and runs a stopped Studio left working
    become interrupted pauses (Toolbox.recover)."""
    with _LOCK:
        tb = getattr(studio, "toolbox", None)
        if tb is None:
            tb = studio.toolbox = Toolbox(studio.home, emit=lambda ev: _emit(studio, ev))
            try:
                tb.recover()
            except Exception:  # noqa: BLE001  (a damaged run file never stops the Studio)
                traceback.print_exc()
        return tb


def toolbox(studio) -> Toolbox:
    tb = getattr(studio, "toolbox", None)
    return tb if tb is not None else attach(studio)


def _emit(studio, ev: dict):
    project = ev.get("project")
    if not project:
        studio.broadcast(ev)
        return
    try:
        room = studio.room(project)
    except (ValueError, LookupError):  # the project was deleted while its run worked
        return
    room.hub.send(ev)


def tools_on() -> bool:
    from .. import settings
    return settings.load()["agent_tools_enabled"]


def claude_ready() -> bool:
    import os

    from .. import settings
    env = os.environ
    return bool(env.get("ANTHROPIC_API_KEY") or env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_PROFILE")) \
        and settings.network_allowed()


def ready() -> dict:
    """Whether a run can start now, and why not: Claude (key and network lookups) and the two settings."""
    from .. import settings
    s = settings.load()
    return {"claude": claude_ready(), "network": settings.network_allowed(), "agent_tools": s["agent_tools_enabled"],
            "auto_price_check": s["auto_price_check"]}


def can(req) -> dict:
    """What this member may do with runs, keys and tools (a Studio without Teams has one person: everything)."""
    if not req.studio.team:
        return {"start": True, "skip": True, "allow": True, "keys": True, "tools": True}
    role = (req.member or {}).get("role")
    owner, editor = role == "owner", role in ("owner", "editor")
    return {"start": editor, "skip": editor, "allow": owner, "keys": owner, "tools": owner}


def keys_saved(req, changed) -> list:
    """POST /api/keys, after keys.save(): saving a curated read-only tool's key approves its first use, and runs that
    waited for one of these keys continue (unless agent tools are off: then they keep waiting). Returns their ids."""
    names = [n for n in changed or [] if isinstance(n, str) and n.startswith("MORPH_TOOL_")]
    if not names:
        return []
    try:
        return toolbox(req.studio).on_credentials_saved(names, by=_by(req), resume=tools_on())
    except Exception:  # noqa: BLE001  (the keys are saved; a run that can't resume yet says so on its card)
        traceback.print_exc()
        return []


def after_build(studio, room, rev: str):
    """After a finished build (server.py): Live prices by itself when "Check live prices after a build" is on (with agent
    tools, Claude and network lookups), and only when the build changed the purchased parts. Starts in the background,
    so the room never waits; returns that thread (tests) or None."""
    from .. import settings
    from . import agents
    s = settings.load()
    if not (s["auto_price_check"] and s["agent_tools_enabled"] and claude_ready()):
        return None
    try:
        parent = (room.p.load_results(rev) or {}).get("parent")
    except (OSError, ValueError):
        return None
    now = agents.bom_lines(room.p, rev)
    key = lambda lines: sorted((ln["item"], ln["qty"]) for ln in lines)  # noqa: E731
    if not now or (parent and key(agents.bom_lines(room.p, parent)) == key(now)):
        return None

    def go():
        try:
            agents.start_price_check(studio, room, {"rev": rev}, None, auto=True)
        except Exception:  # noqa: BLE001  (nothing started; the BOM tab's button still can)
            traceback.print_exc()

    t = threading.Thread(target=go, daemon=True, name="morph-auto-prices")
    t.start()
    return t


def _by(req):
    return (req.member or {}).get("id")


def _team_audit(req, event: str, **fields):
    """Teams' own security log (team Studios): tool switches and decisions, next to key and settings changes."""
    if req.studio.team:
        from .teams import audit
        audit.append(event, by=_by(req), home=req.studio.home, **fields)


def _run_id(req) -> str:
    run_id = (req.body or {}).get("run") if req.method == "POST" else req.arg("run")
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        raise HTTPError(400, 'Send {"run": "run_<16 hex digits>"}.')
    return run_id


def _refused(exc: ToolboxError):
    return HTTPError(exc.status, str(exc))


def _off():
    return HTTPError(409, 'Agent tools are off (Keys, Privacy and network: "Let agents use tools"). Turn them on to run '
                          "Live prices or continue a run.")


# ------------------------------------------------------------------ tools
@route("GET", "/api/tools", role="viewer")
def _tools(req):
    return 200, {**toolbox(req.studio).tools(), "can": can(req), "ready": ready()}


def _switch(req, on: bool):
    name = (req.body or {}).get("tool")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{2,47}", name):
        raise HTTPError(400, 'Send {"tool": "<tool name>"}.')
    try:
        out = toolbox(req.studio).set_enabled(name, on, by=_by(req))
    except ToolboxError as exc:
        raise _refused(exc) from None
    if out["changed"]:
        _team_audit(req, "tool_enabled" if on else "tool_disabled", tool=name)
    return 200, {"ok": True, **out}


@route("POST", "/api/tools/enable", role="owner")
def _tools_enable(req):
    return _switch(req, True)


@route("POST", "/api/tools/disable", role="owner")
def _tools_disable(req):
    return _switch(req, False)


# ------------------------------------------------------------------ runs
@route("GET", "/api/runs", role="viewer")
def _runs(req):
    status, name = req.arg("status"), req.arg("agent")
    if status is not None and status not in STATUSES:
        raise HTTPError(400, f"status is one of {', '.join(STATUSES)}.")
    if name is not None and not re.fullmatch(r"[a-z][a-z0-9_-]{1,31}", name):
        raise HTTPError(400, "agent is an agent's name, like price_check.")
    try:
        limit = max(1, min(100, int(req.arg("limit") or 20)))
    except ValueError:
        raise HTTPError(400, "limit is a number from 1 to 100.") from None
    views = toolbox(req.studio).runs(project=req.room.name, status=status, limit=limit, agent=name)
    return 200, {"runs": views, "can": can(req), "ready": ready(),
                 "agents": [{"name": a["name"], "title": a["title"]} for a in AGENTS.values()]}


@route("GET", "/api/runs/answer", role="viewer")
def _runs_answer(req):
    run_id, tb = _run_id(req), toolbox(req.studio)
    try:
        view = tb.get(run_id)
    except ToolboxError as exc:
        raise _refused(exc) from None
    if view["project"] != req.room.name:
        raise HTTPError(404, "There is no such run in this project.")
    return 200, {"run": run_id, "status": view["status"], "text": tb.answer(run_id) if view["status"] == "done" else None}


@route("POST", "/api/runs/start")
def _runs_start(req):
    body = req.body or {}
    a = AGENTS.get(body.get("agent")) if isinstance(body.get("agent"), str) else None
    if a is None:
        raise HTTPError(400, f"There is no agent called {str(body.get('agent'))[:40]}.")
    if not tools_on():
        raise _off()
    try:
        view, already = a["start"](req.studio, req.room, body, _by(req))
    except ToolboxError as exc:
        raise _refused(exc) from None
    except ValueError as exc:
        raise HTTPError(400, str(exc)) from None
    return (200 if already else 202), {"ok": True, "run": view, "already": bool(already)}


@route("POST", "/api/runs/decide")
def _runs_decide(req):
    run_id, decisions = _run_id(req), (req.body or {}).get("decisions")
    role = ((req.member or {}).get("role") or "viewer") if req.studio.team else "owner"
    if not tools_on():
        raise _off()
    try:
        view = toolbox(req.studio).decide(run_id, decisions, by=_by(req), role=role)
    except ToolboxError as exc:
        raise _refused(exc) from None
    _team_audit(req, "tool_decision", run=run_id, decisions=sorted(set(decisions.values())) if isinstance(decisions, dict) else [])
    return 200, {"ok": True, "run": view}


@route("POST", "/api/runs/resume")
def _runs_resume(req):
    run_id = _run_id(req)
    if not tools_on():
        raise _off()
    tb = toolbox(req.studio)
    try:
        view = tb.get(run_id)
        if view["status"] == "paused" and "interrupted" in view["waiting_on"] and not claude_ready():
            raise HTTPError(503, "Claude isn't available: add the Anthropic key in Keys and keep network lookups on, then "
                                 "continue the run.")
        view = tb.resume(run_id, by=_by(req))
    except ToolboxError as exc:
        raise _refused(exc) from None
    return 200, {"ok": True, "run": view}


@route("POST", "/api/runs/cancel")
def _runs_cancel(req):
    run_id = _run_id(req)
    try:
        view = toolbox(req.studio).cancel(run_id, by=_by(req))
    except ToolboxError as exc:
        raise _refused(exc) from None
    return 200, {"ok": True, "run": view, "ended": view["status"] in ENDED}


# ------------------------------------------------------------------ the agents a person may start
def _register_agents():
    from . import agents
    agent(agents.PRICE_AGENT, title="Live prices", start=agents.start_price_check)


_register_agents()
