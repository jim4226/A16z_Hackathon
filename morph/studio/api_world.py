"""World mode routes (contracts section 2). The handlers take a routes.Request and return (status, body) or None
when they streamed the answer themselves:

    GET  /api/world?project=                 the project's world, the design's active world, starters, honesty notes
    GET  /api/world?project=&file=room.glb   the uploaded room itself (streamed)
    POST /api/world {starter} | {world} | {off: true}
                                             save world/world.json and queue a revision that reruns the simulation
    PUT  /api/world/upload?name=             a room scan (.glb, raw body, at most 300 MB): checked, kept as room.glb
    POST /api/world/obstacles/auto {world?}  proposed obstacle boxes (and mount surface) from the room scan
    POST /api/world/inspect {views, ai?, subq?, rev?}
                                             exact checks (with a room scan, the simulated path against the scan
                                             itself: a warning only) + the optional AI review (Claude, SubQ) of 6 views

register() adds them to morph.studio.routes; the module calls it when the Studio loads its feature modules. Live
progress goes out as `world` events and the agents timeline as `agent` events (contracts section 3).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
import time
from collections import OrderedDict

from . import worlds
from .worlds import WorldError

INSPECT_MAX_BODY = 16 << 20


def _err(status: int, message: str):
    return status, {"ok": False, "error": message}


def _send(room, event: dict):
    try:
        room.hub.send(event)
    except Exception:  # noqa: BLE001  (an event that can't be sent must not fail the request)
        pass


def _by(req) -> str | None:
    m = getattr(req, "member", None)
    return m.get("id") if isinstance(m, dict) else None


def _design(room, rev):
    try:
        return room.p.load_design(rev) if rev else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _json(path: str, limit: int = 64 << 20):
    try:
        if os.path.getsize(path) > limit:
            return None
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _log(room, entry: dict):
    """A world entry in the project's ledger (Keewano gets its categories and counts when its events are on)."""
    try:
        room.p.log(entry)
    except Exception:  # noqa: BLE001  (a ledger that can't be written never fails the request)
        pass


def _rev(room, body_rev=None) -> str | None:
    revs = room.completed() if hasattr(room, "completed") else room.p.revs()
    if body_rev is not None:
        return body_rev if isinstance(body_rev, str) and body_rev in revs else None
    return revs[-1] if revs else None


def _asset_url(room, w: dict | None, sha: str = "") -> str | None:
    """Where the page loads the room scan: its hash in the address, so a new upload is never the old file again."""
    if not (w is None or w.get("asset")):
        return None
    v = sha or (w or {}).get("asset_sha") or ""
    return f"/api/world?project={room.name}&file={worlds.ASSET}" + (f"&v={v}" if v else "")


def _ai_ready() -> dict:
    """Which optional reviewers would answer now: Claude (a key and network lookups on) and SubQ."""
    try:
        from . import subq
        sq = bool(subq.enabled())
    except Exception:  # noqa: BLE001  (no SubQ client: it is simply off)
        sq = False
    return {"claude": bool(worlds._claude_ready()), "subq": sq}


AI_OFF = ("AI review is off: it needs an Anthropic key (Keys, top right) and network lookups on. This is the measured "
          "review; no pictures were sent.")


# ---------------------------------------------------------------------- GET /api/world

def get_world(req):
    room = req.room
    root = room.p.root
    want = req.arg("file")
    if want is not None:
        path = worlds.asset_path(root)
        if want != worlds.ASSET or not os.path.isfile(path):
            return _err(404, "This project has no uploaded room.")
        return req.handler._file(path)
    try:
        w, problem = worlds.load(root), None
    except WorldError as exc:
        w, problem = None, str(exc)
    rev = _rev(room)
    d = _design(room, rev)
    sim = None
    if rev:
        res = _json(os.path.join(room.p.rev_dir(rev), "results.json")) or {}
        pp = (((res.get("sims") or {}).get("scenarios") or {}).get("pick_place") or {}).get("metrics") or {}
        sim = {"rev": rev, "world": pp.get("world") or "", "world_sha": pp.get("world_sha") or "",
               "min_clearance_mm": pp.get("min_clearance_mm"), "min_clearance_at": pp.get("min_clearance_at"),
               "clearance_ok": pp.get("clearance_ok"), "world_contacts": pp.get("world_contacts") or [],
               "success": pp.get("success"), "failure": pp.get("failure"), "arc": pp.get("arc"),
               "detours": pp.get("detours"), "path": pp.get("path"), "blocked": pp.get("blocked")}
    return 200, {"ok": True, "world": w, "sha": worlds.sha(w) if w else None, "problem": problem,
                 "active": {"id": d.world.id, "sha": d.world.sha} if d else {"id": "", "sha": ""}, "sim": sim,
                 "asset_url": _asset_url(room, w) if w else None, "ai": _ai_ready(),
                 "starters": worlds.starters(), "views": [{"name": v, "title": worlds.VIEW_TITLES[v]} for v in worlds.VIEWS],
                 "notes": worlds.NOTES}


# ---------------------------------------------------------------------- POST /api/world

def save_world(req):
    """Save the world and queue the revision that simulates in it ({off: true} turns world mode off)."""
    room = req.room
    body = req.body or {}
    rev = _rev(room)
    d = _design(room, rev)
    if d is None:
        return _err(409, "This project has no finished revision yet. Wait for its first build.")
    if body.get("off"):
        if not d.world.id:
            return 200, {"ok": True, "unchanged": True, "world": None}
        changes, label, reply, w, h = {"world.id": "", "world.sha": ""}, "world: off", \
            "World mode off: the simulation runs on the plain table again.", None, ""
    else:
        try:
            if isinstance(body.get("starter"), str):
                raw = worlds.starter(body["starter"])
            elif isinstance(body.get("world"), dict):
                raw = body["world"]
            else:
                return _err(400, "Send {starter: desk | counter | workbench}, {world: {...}} or {off: true}.")
            w, h = worlds.save(room.p.root, raw)
        except WorldError as exc:
            return _err(400, str(exc))
        if d.world.id == w["id"] and d.world.sha == h:
            _send(room, {"type": "world", "stage": "save", "status": "done", "text": f"{w['name']} is already simulated."})
            return 200, {"ok": True, "unchanged": True, "world": w, "sha": h}
        changes = {"world.id": w["id"], "world.sha": h}
        label = f"world: {w['name']}"
        reply = f"World mode: simulating in {w['name']} (beta). Only the simulation reruns."
    try:
        job = req.studio.queue_changes(room, changes, label=label, reply=reply, source="user", base_rev=rev, by=_by(req),
                                       kind="world")
    except RuntimeError as exc:  # rooms.QueueFull
        return _err(429, str(exc))
    _send(room, {"type": "world", "stage": "save", "status": "done",
                 "text": f"Saved {w['name']}; the simulation reruns in it." if w else "World mode off."})
    _log(room, worlds.ledger_entry(w))
    return 202, {"ok": True, "world": w, "sha": h, "job": job.info() if hasattr(job, "info") else None}


# ---------------------------------------------------------------------- PUT /api/world/upload

def _sweep(folder: str, max_age_s: float = 3600.0):
    """Remove upload leftovers (cut-off transfers) older than an hour."""
    try:
        for fn in os.listdir(folder):
            p = os.path.join(folder, fn)
            if fn.startswith(".upload-") and time.time() - os.path.getmtime(p) > max_age_s:
                os.remove(p)
    except OSError:
        pass


def upload_world(req):
    """Stream a .glb room scan to disk (never into memory), check its header, and keep it as world/room.glb.
    Answers a draft world (units guessed from its size) for the page to place the robot in."""
    room = req.room
    if not req.length:
        return _err(400, "Send the .glb file as the request body.")
    if req.length > worlds.MAX_GLB_BYTES:
        return _err(413, "The room file is larger than 300 MB.")
    folder = worlds.world_dir(room.p.root)
    os.makedirs(folder, exist_ok=True)
    _sweep(folder)
    tmp = os.path.join(folder, f".upload-{secrets.token_hex(8)}.glb")
    _send(room, {"type": "world", "stage": "upload", "status": "start", "text": "Receiving the room file"})
    try:
        n = req.save(tmp)
        info = worlds.check_glb(tmp)
        info["sha"] = worlds.glb_sha(tmp)
        os.replace(tmp, worlds.asset_path(room.p.root))
    except WorldError as exc:
        _send(room, {"type": "world", "stage": "upload", "status": "error", "text": str(exc)})
        return _err(400, str(exc))
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    name = re.split(r"[\\/]", str(req.arg("name") or ""))[-1]      # a label only: the file is always room.glb
    draft = worlds.draft_from_glb(info, name)
    draft["asset_sha"] = info["sha"]
    _send(room, {"type": "world", "stage": "upload", "status": "done",
                 "text": f"Room received: {info['faces']:,} triangles. Click a level surface to place the robot."})
    return 200, {"ok": True, "bytes": n, "faces": info["faces"], "bounds_m": info["bounds_m"], "warnings": info["warnings"],
                 "draft": draft, "asset_url": _asset_url(room, None, info["sha"])}


# ---------------------------------------------------------------------- POST /api/world/obstacles/auto

def auto_obstacles(req):
    """Boxes proposed from the room scan around the robot. Nothing is saved: the page shows them for the user to
    keep or drop, then saves the world."""
    room = req.room
    body = req.body or {}
    path = worlds.asset_path(room.p.root)
    if not os.path.isfile(path):
        return _err(400, "Upload a room (.glb) first; the starter worlds already have their boxes.")
    raw = body.get("world") if isinstance(body.get("world"), dict) else None
    try:
        if raw is None:
            w = worlds.load(room.p.root)
            if w is None:
                return _err(400, "Place the robot in the room first.")
        else:
            raw = dict(raw)
            m = raw.get("mount") if isinstance(raw.get("mount"), dict) else None
            if not isinstance(raw.get("surface"), dict) and m and isinstance(m.get("position"), list) and len(m["position"]) == 3:
                x, y, z = m["position"]                    # not confirmed yet: a provisional surface under the mount
                raw["surface"] = {"label": "Mount surface", "center": [x, y, z - 15.0], "half": [300.0, 300.0, 15.0],
                                  "yaw_deg": m.get("yaw_deg", 0.0)}
            raw["asset"] = worlds.ASSET
            w = worlds.validate(raw)
        pitch = body.get("pitch_mm", 20.0)
        pitch = float(pitch) if isinstance(pitch, (int, float)) and not isinstance(pitch, bool) and 10 <= pitch <= 50 else 20.0
    except WorldError as exc:
        return _err(400, str(exc))
    _send(room, {"type": "world", "stage": "obstacles", "status": "start", "text": "Finding obstacles in the scan"})
    _send(room, {"type": "agent", "role": "feasibility", "provider": "code", "status": "start", "ms": 0,
                 "text": "Boxing the room scan around the robot"})
    t0 = time.time()
    try:
        out = worlds.auto_obstacles(path, w, pitch_mm=pitch, time_limit_s=20.0)
    except WorldError as exc:
        _send(room, {"type": "world", "stage": "obstacles", "status": "error", "text": str(exc)})
        return _err(400, str(exc))
    except (MemoryError, ValueError, IndexError) as exc:
        _send(room, {"type": "world", "stage": "obstacles", "status": "error", "text": "The scan couldn't be read."})
        return _err(400, f"The room scan couldn't be read ({exc.__class__.__name__}).")
    ms = int((time.time() - t0) * 1000)
    n = len(out["obstacles"])
    text = f"{n} box{'es' if n != 1 else ''} proposed" + (" (stopped early: the scan is large)" if out["stats"]["truncated"] else "")
    _send(room, {"type": "agent", "role": "feasibility", "provider": "code", "status": "done", "ms": ms, "text": text})
    _send(room, {"type": "world", "stage": "obstacles", "status": "done", "text": text,
                 "result": {"boxes": n, "truncated": out["stats"]["truncated"]}})
    return 200, {"ok": True, **out}


# ---------------------------------------------------------------------- POST /api/world/inspect

def _inputs(room, rev):
    rd = room.p.rev_dir(rev)
    results = _json(os.path.join(rd, "results.json")) or {}
    sims = {k: _json(os.path.join(rd, "sim", f"sim_{k}.json")) for k in ("pick_place", "workspace")}
    robot = _json(os.path.join(rd, "cad", "robot.json"))
    return results, {k: v for k, v in sims.items() if v}, robot


_SCANS: OrderedDict = OrderedDict()      # (project, revision, world, room file) -> scan_clearance(): the last eight
_SCANS_LOCK = threading.Lock()


def _scan(room, rev, w: dict, sims: dict) -> dict | None:
    """The revision's simulated path against the room scan (worlds.scan_clearance), or None when the world has no
    scan. A revision's simulation never changes, so the answer is kept (in memory) for the same room file."""
    path = worlds.asset_path(room.p.root)
    if not w.get("asset") or not os.path.isfile(path):
        return None
    skip = lambda why: {"skipped": why, "note": worlds.SCAN_NOTE}  # noqa: E731
    doc = sims.get("pick_place")
    if not doc or not doc.get("frames"):
        return skip("There's no simulation to measure against the scan yet.")
    w_sha = worlds.sha(w)
    if (doc.get("world") or {}).get("sha") != w_sha:
        return skip("The simulation didn't run in this room as it is now: Save and simulate, then check again.")
    try:
        st = os.stat(path)
        with open(os.path.join(room.p.rev_dir(rev), "sim", "robot.xml"), encoding="utf-8") as fh:
            xml = fh.read()
        ss = os.stat(os.path.join(room.p.rev_dir(rev), "sim", "sim_pick_place.json"))
    except OSError:
        return skip("This revision's collision model (sim/robot.xml) is missing.")
    # Reset demo reuses revision names: the revision's own simulation and model are part of the key, not just its name
    key = (room.p.root, rev, w_sha, st.st_size, st.st_mtime_ns, ss.st_size, ss.st_mtime_ns,
           hashlib.sha256(xml.encode("utf-8")).hexdigest())
    with _SCANS_LOCK:
        if key in _SCANS:
            _SCANS.move_to_end(key)
            return _SCANS[key]
    _send(room, {"type": "world", "stage": "scan", "status": "start", "text": "Measuring the path against the room scan"})
    t0 = time.time()
    try:
        res = worlds.scan_clearance(path, w, doc, xml)
    except Exception as exc:  # noqa: BLE001  (the scan is a warning: it never takes the rest of the check down)
        res = skip(f"The path couldn't be measured against the scan ({exc.__class__.__name__}).")
    ms = int((time.time() - t0) * 1000)
    for g in res.get("regions") or []:          # a trimmed box's place names the revision whose path it was fitted to
        if isinstance(g.get("covers"), dict) and re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", rev):
            g["covers"]["rev"] = rev
    mc, n = res.get("min_clearance_mm"), len(res.get("regions") or [])
    cut = bool((res.get("stats") or {}).get("truncated"))
    text = (res["skipped"] if res.get("skipped") else
            (f"Closest to the scan: {'at most ' if cut else ''}{mc:.1f} mm" if mc is not None else
             "Not all measured in time" if cut else f"Nothing within {worlds.SCAN_CUT_MM:.0f} mm of the scan")
            + (f", {n} near miss{'es' if n != 1 else ''}" if n else "") + (" (cut short)" if cut and mc is not None else ""))
    _send(room, {"type": "agent", "role": "feasibility", "provider": "code", "status": "skipped" if res.get("skipped") else "done",
                 "ms": ms, "text": text})
    _send(room, {"type": "world", "stage": "scan", "status": "done", "text": text})
    if (res.get("stats") or {}).get("fit_timeout") or (res.get("stats") or {}).get("truncated"):
        return res                              # cut short (a busy computer): Check placement again measures again
    with _SCANS_LOCK:
        _SCANS[key] = res
        while len(_SCANS) > 8:
            _SCANS.popitem(last=False)
    return res


def inspect_world(req):
    """The world check: exact pass / fail from geometry and the simulation, then (when asked, and only then) Claude's
    review of the rendered views and SubQ's yes/no answers. The verdict is always the measured one."""
    room = req.room
    body = req.body or {}
    try:
        w = worlds.load(room.p.root)
    except WorldError as exc:
        return _err(400, str(exc))
    if w is None:
        return _err(400, "Save a world first: choose a starter or upload a room.")
    rev = _rev(room, body.get("rev"))
    if rev is None:
        return _err(404, "That revision doesn't exist or isn't finished.")
    try:
        views = worlds.clean_views(body.get("views") or [])
    except WorldError as exc:
        return _err(400, str(exc))
    ai, use_subq = body.get("ai", True) is not False, bool(body.get("subq"))
    results, sims, robot = _inputs(room, rev)
    d = _design(room, rev)
    _send(room, {"type": "world", "stage": "inspect", "status": "start", "text": "Checking the placement"})
    t0 = time.time()
    scan = _scan(room, rev, w, sims)
    res = worlds.inspect(w, results, views, sims=sims, robot=robot, design=d, ai=False, scan=scan)
    ms = int((time.time() - t0) * 1000)
    _send(room, {"type": "agent", "role": "feasibility", "provider": "code", "status": "done", "ms": ms,
                 "text": f"Measured verdict: {res['verdict'].replace('_', ' ')}"})
    if ai and not worlds._claude_ready():      # asked for, but there is no key (or lookups are off): say so
        res["review"]["skipped"] = AI_OFF
        _send(room, {"type": "agent", "role": "reviewer", "provider": "claude", "status": "skipped", "ms": 0, "text": AI_OFF})
    elif ai and not views:
        res["review"]["skipped"] = "The AI review needs the six rendered views; none came with this check."
    elif ai:
        _send(room, {"type": "agent", "role": "reviewer", "provider": "claude", "status": "start", "ms": 0,
                     "text": f"Reviewing {len(views)} views"})
        rv = worlds.review_request(views, res["facts"], world=w)
        res["review"] = rv
        status = "done" if rv["source"] == "claude" else ("error" if rv.get("error") else "skipped")
        _send(room, {"type": "agent", "role": "reviewer", "provider": rv["source"], "status": status, "ms": rv.get("ms", 0),
                     "text": rv.get("error") or f"{len(rv['findings'])} findings"})
    if use_subq and views:
        sq = worlds.subq_review(views, res["facts"], wrist_camera="wrist_camera" in (d.electronics.extras if d else ""))
        res["subq"] = sq
        _send(room, {"type": "agent", "role": "vision", "provider": "subq", "status": "done" if sq else "skipped",
                     "ms": (sq or {}).get("ms") or 0, "text": f"{len(sq['findings'])} findings" if sq else "SubQ is off"})
        if sq:
            res["review"]["findings"] = res["review"]["findings"] + sq["findings"]
    res["review"]["verdict"] = res["verdict"]
    _log(room, {"type": "world_inspect", "verdict": res["verdict"], "ms": int((time.time() - t0) * 1000),
                "review": res["review"].get("source") if ai and views and not res["review"].get("skipped") else "none"})
    if res.get("subq"):
        _log(room, {"type": "subq", "purpose": "world_check", "source": res["subq"].get("source") or "subq",
                    "ms": res["subq"].get("ms")})
    _send(room, {"type": "world", "stage": "inspect", "status": "done", "text": f"Verdict: {res['verdict'].replace('_', ' ')}",
                 "result": {"rev": rev, "verdict": res["verdict"], "source": res["review"]["source"]}})
    return 200, {"ok": True, "rev": rev, "world": w["id"], "verdict": res["verdict"], "checks": res["checks"],
                 "review": res["review"], "subq": res.get("subq"), "facts": res["facts"], "notes": worlds.NOTES}


# ---------------------------------------------------------------------- registration

ROUTES = (("GET", "/api/world", get_world, {"role": "viewer"}),
          ("POST", "/api/world", save_world, {}),
          ("PUT", "/api/world/upload", upload_world, {"raw_body": True, "max_body": worlds.MAX_GLB_BYTES}),
          ("POST", "/api/world/obstacles/auto", auto_obstacles, {}),
          ("POST", "/api/world/inspect", inspect_world, {"max_body": INSPECT_MAX_BODY}))


def register(route=None):
    """Add the world routes to the Studio's route table (morph.studio.routes.route by default)."""
    if route is None:
        from .routes import route
    for method, path, fn, kw in ROUTES:
        route(method, path, **kw)(fn)


register()  # the Studio loads every api_*.py at start (contracts section 2)
