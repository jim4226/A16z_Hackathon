"""Import routes for the Studio (contracts section 2); they register themselves when the Studio loads its feature modules.

  POST   /api/import {url?}                         -> {id, fetching}
  GET    /api/import/<id>                           -> {id, files, bytes, fetch, detected}
  PUT    /api/import/<id>/file?path=<rel>           raw body, <= 300 MB -> {path, size, files, bytes}
  POST   /api/import/<id>/detect                    -> {candidates, checklist, warnings, errors, refused, catalog}
  GET    /api/import/<id>/preview.glb?candidate=n   -> model/gltf-binary (robot at its ready pose + TCP marker)
  GET    /api/import/<id>/preview.png?candidate=n   -> image/png thumbnail (no WebGL needed)
  POST   /api/import/<id>/commit {candidate, name, answers} -> 202 {kind, ...}; progress as SSE
  DELETE /api/import/<id>
  POST   /api/import/fixes {fix}                    -> 202 {job}: one of the fixes an import's build was offered

An object's grip that would bend the fingers too far gets the finger repair New robot applies (a thicker beam) before
it is built. When an import's build isn't feasible, the project's chat says why, with the fixes Morph can make
(`fixes`: [{id, label, changes}], changes computed here, never sent by the page): each is built as a new revision.

SSE: {"type": "import", "id", "stage", "status", "text", "project"} while fetching, detecting, saving and building.
In the Studio a robot or project becomes a new project (projects service): {"type": "project_created", "project",
"origin": "import"} goes to every tab, its first build is queued in its own room, and the importing tab follows it
through `import` events in its own room (stage "build": progress with `node`/`node_status`, then done or error).
Errors are {"ok": false, "error": "<what to do>"} with 400 (bad input), 404 (no session), 409 (busy), 429 (queue full).
"""

from __future__ import annotations

import json
import os
import queue
import re
import threading
import traceback

from ..importers import ImportFailed, session as sess
from ..importers.common import LIMITS, ledger_entry, set_home

_busy: dict = {}
_doomed: dict = {}            # sessions deleted while a download or build was running: removed when it ends
_busy_lock = threading.Lock()
_cleaned: set = set()
PATH_RX = re.compile(r"^/api/import/([a-f0-9]{16})(?:/(file|detect|commit|preview\.glb|preview\.png))?/?$")
TORQUE_RX = re.compile(r"needs [\d.]+ N·m with margin")
BEND_RX = re.compile(r"tip deflection|bending safety factor")
MAX_FINGER_MM = 14.0          # as New robot's repair (brief.repair): the beam grows 1.5 mm at a time up to this


def _home(req) -> str:
    home = getattr(req.studio, "home", None)
    if home:
        set_home(home)
        if home not in _cleaned:
            _cleaned.add(home)
            try:
                sess.cleanup(home)
            except OSError:
                pass
    return home


def _emitter(req, project=None):
    room = getattr(req, "room", None)
    hub = getattr(room, "hub", None) or getattr(req.studio, "hub", None)
    proj = project or getattr(room, "name", None) or _room_project(req)

    def emit(ev: dict):
        if hub is None:
            return
        ev = dict(ev)
        ev.setdefault("project", proj)
        by = _member_id(req)
        if by:
            ev.setdefault("by", by)  # the member id (contracts section 3)
        try:
            hub.send(ev)
        except Exception:  # noqa: BLE001  (SSE clients come and go)
            pass
    return emit


def _member_id(req) -> str | None:
    return (getattr(req, "member", None) or {}).get("id")


def _room_project(req):
    p = getattr(getattr(req, "room", None), "p", None) or getattr(req.studio, "p", None)
    return os.path.basename(p.root) if p is not None else None


def _audit(req, s, **fields):
    """Team Studios: what an import brought in goes into the team's audit log (who, what, into which project, and the
    link it came from, without its query)."""
    studio = getattr(req, "studio", None)
    if not getattr(studio, "team", False):
        return
    from urllib.parse import urlsplit, urlunsplit

    from .teams import audit
    try:
        st = s.state() if s is not None else {}
    except (OSError, ValueError):
        st = {}
    url = st.get("source_url") or st.get("url") or ""
    if url:
        u = urlsplit(url)
        url = urlunsplit((u.scheme, u.hostname or "", u.path, "", ""))[:300]
    audit.append("import", by=_member_id(req), home=studio.home, **fields, **({"url": url} if url else {}))


def _allow_url(studio) -> bool:
    remote = bool(getattr(studio, "remote", False) or getattr(studio, "team", None))
    return (not remote) or bool(getattr(studio, "allow_url_import", False))


def _err(status: int, msg: str):
    return status, {"ok": False, "error": msg}


def _claim(sid: str) -> bool:
    with _busy_lock:
        if _busy.get(sid):
            return False
        _busy[sid] = True
        return True


def _release(sid: str):
    with _busy_lock:
        _busy.pop(sid, None)
        home = _doomed.pop(sid, False)
    if home is not False:
        sess.Session(sid, home).delete()


def _open(req, sid):
    return sess.Session.open(sid, _home(req))


# ------------------------------------------------------------------------------------- handlers
def create(req):
    """POST /api/import {url?}: a new session; with a url, the download starts in the background."""
    body = req.body or {}
    url = body.get("url")
    if url is not None and (not isinstance(url, str) or not url.strip()):
        return _err(400, "Paste a GitHub or Hugging Face link.")
    home = _home(req)
    if url:
        from ..importers import fetch
        try:
            fetch.plan(url.strip())                  # refuse bad links before creating anything
            if not _allow_url(req.studio):
                raise ImportFailed("Importing from a link is off on this shared Studio. Ask its owner to start it with "
                                   "--allow-url-import, or download the files and drop them here.")
        except ImportFailed as exc:
            return _err(400, str(exc))
    s = sess.Session.create(home, url=url.strip() if url else None)
    if not url:
        return 200, {"ok": True, "id": s.id, "fetching": False}
    emit = _emitter(req)
    _set_fetch(s, "running")
    _claim(s.id)

    def run():
        try:
            s.fetch(url.strip(), emit, allow=_allow_url(req.studio))
            _set_fetch(s, "done")
        except ImportFailed as exc:
            _set_fetch(s, "error", str(exc))
            emit({"type": "import", "id": s.id, "stage": "fetch", "status": "error", "text": str(exc)})
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            _set_fetch(s, "error", f"The download failed ({exc.__class__.__name__}).")
            emit({"type": "import", "id": s.id, "stage": "fetch", "status": "error", "text": "The download failed."})
        finally:
            _release(s.id)

    threading.Thread(target=run, daemon=True, name=f"import-fetch-{s.id}").start()
    return 200, {"ok": True, "id": s.id, "fetching": True}


def _set_fetch(s, status, error=None):
    with sess._lock(s.id):
        st = s.state()
        st["fetch"] = {"status": status, "error": error}
        s._save(st)


def status(req, sid):
    s = _open(req, sid)
    st = s.state()
    det = None
    try:
        det = sess._public(s.detection())
    except ImportFailed:
        pass
    return 200, {"ok": True, "id": sid, "files": len(st.get("files") or {}), "bytes": st.get("bytes", 0),
                 "fetch": st.get("fetch"), "status": st.get("status"), "detected": det}


def upload(req, sid):
    """PUT /api/import/<id>/file?path=<rel>: the raw body is the file, streamed in 1 MiB chunks."""
    s = _open(req, sid)
    rel = (req.query.get("path") or [""])[0]
    raw = req.handler.headers.get("Content-Length")
    if raw is None or not raw.isdigit():
        return _err(411, "Send the file with a Content-Length header.")
    length = int(raw)
    if length > LIMITS["upload_file"]:
        return _err(413, f"That file is {length / (1 << 20):.0f} MB; the limit is {LIMITS['upload_file'] >> 20} MB.")
    if _busy.get(sid):
        return _err(409, "This import is busy (downloading, detecting or building); start a new import to add files.")
    req.handler._read = True
    out = s.add_file(rel, req.handler.rfile, length)
    st = s.state()
    return 200, {"ok": True, **out, "files": len(st["files"]), "bytes": st["bytes"]}


def detect(req, sid):
    s = _open(req, sid)
    if (s.state().get("fetch") or {}).get("status") == "running":
        return _err(409, "Still downloading; detection starts when the download finishes.")
    if not _claim(sid):
        return _err(409, "This import is busy; try again in a moment.")
    try:
        return 200, {"ok": True, **s.detect(_emitter(req))}
    finally:
        _release(sid)


def preview(req, sid, fmt):
    s = _open(req, sid)
    try:
        idx = int((req.query.get("candidate") or ["0"])[0])
    except ValueError:
        return _err(400, "candidate must be a number.")
    data, ctype = s.preview(idx, fmt)
    return 200, data, ctype


def commit(req, sid):
    """POST /api/import/<id>/commit {candidate, name, answers}. Robots and projects are saved, then built in the
    background (202); an object is applied to this tab's project; a parts list or board comes back as data."""
    body = req.body or {}
    s = _open(req, sid)
    name = body.get("name")
    if name is not None and (not isinstance(name, str) or len(name) > 80):
        return _err(400, "The name must be text of at most 80 characters.")
    if not _claim(sid):
        return _err(409, "This import is busy; try again in a moment.")
    emit = _emitter(req)
    try:
        plan = s.commit(body.get("candidate", 0), name, body.get("answers") or {}, emit)
    except BaseException:
        _release(sid)
        raise
    if plan["kind"] in ("robot", "project") and callable(getattr(req.studio, "create_project", None)):
        return _create_project(req, sid, s, plan, emit)
    if plan["kind"] in ("robot", "project"):
        home = _home(req)
        project = sess.unique_project(plan.get("project") or plan.get("source") or "robot", home)
        plan["project"] = project

        def run():
            ev = _emitter(req, project)
            try:
                out = sess.build(plan, home, on_event=ev, fast_sim=False, n_trials=2, session_id=sid,
                                 created_by=(getattr(req, "member", None) or {}).get("id"))
                summary = out.get("summary") or {}
                ev({"type": "import", "id": sid, "stage": "build", "status": "done",
                    "text": f"Built {out['project']} ({out['rev']})", "feasible": summary.get("feasible"),
                    "issues": [str(x)[:240] for x in (summary.get("issues") or [])[:4]],
                    "pick_place": summary.get("pick_place_success")})
            except Exception as exc:  # noqa: BLE001
                traceback.print_exc()
                src = plan.get("source")
                retry = (f"The robot is saved as {src}; build it in a new project with: "
                         f"morph robot new NAME --source {src}" if plan["kind"] == "robot" and src
                         else "Import the file again to retry.")
                ev({"type": "import", "id": sid, "stage": "build", "status": "error",
                    "text": f"The build failed ({exc.__class__.__name__}: {str(exc)[:200]}). {retry}"})
            finally:
                _release(sid)
                s.delete()

        threading.Thread(target=run, daemon=True, name=f"import-build-{sid}").start()
        return 202, {"ok": True, **{k: v for k, v in plan.items() if k not in ("design", "history")}}
    _release(sid)
    if plan["kind"] == "object" and (body.get("apply", True) is not False):
        queued = _queue_object(req, plan["changes"], plan.get("label") or "imported object")
        if queued is not None:
            if queued[0] == 202:
                _audit(req, s, kind="object", project=req.room.name, file=str(plan.get("file") or "")[:160])
                _ledger(req.room.p, s, plan)
            return queued if queued[0] != 202 else (202, {"ok": True, **plan, "applied": True, "job": queued[1]})
        if _apply_async(req, plan["changes"], plan.get("label") or "imported object"):
            return 202, {"ok": True, **plan, "applied": True}
    out = {"ok": True, **plan}
    if plan["kind"] == "object":
        out["applied"] = False                   # no edit path on this Studio: the dialog shows the keys to type
    return 200, out


def _ledger(p, s, plan: dict):
    """The import in the project's ledger: what kind of thing in which format, from files or a link, whether the motors
    were picked, how many warnings (Keewano gets these when its events are on; never names, files or links)."""
    try:
        st = s.state()
        p.log(ledger_entry(plan, "link" if (st.get("url") or st.get("source_url")) else "upload"))
    except Exception:  # noqa: BLE001  (a ledger that can't be written never fails the import)
        traceback.print_exc()


def _create_project(req, sid: str, s, plan: dict, emit):
    """A robot or Morph project as a new project through the Studio's project service: projects.create (names, meta,
    design limits), the first build queued in the new room, project_created to every tab. The importing tab stays where
    it is and follows the build through `import` events; the session is removed when the build ends."""
    from .rooms import QueueFull

    studio, meta, sub = req.studio, dict(plan.get("meta") or {}), {}
    name = plan.get("project") or plan.get("source") or "robot"
    try:
        design = sess.plan_design(plan, name)
        room, job = studio.create_project(name, design=design, need=meta.pop("need", None), origin="import",
                                          by=_member_id(req), meta=meta, label=plan.get("label") or "imported",
                                          on_room=lambda r: sub.update(q=r.hub.subscribe()))
    except (ValueError, FileExistsError, QueueFull) as exc:
        _release(sid)
        return _err(429 if isinstance(exc, QueueFull) else 400, f"Couldn't create the project: {exc}")
    except BaseException:
        _release(sid)
        raise
    plan["project"] = room.name
    for i, d in enumerate(plan.get("history") or []):  # a Morph export's earlier designs, for reference
        try:
            with open(os.path.join(room.p.root, f"imported_design_{i + 1:03d}.json"), "w", encoding="utf-8") as fh:
                json.dump(d, fh, indent=1)
        except (OSError, TypeError, ValueError):
            break
    _audit(req, s, kind=plan["kind"], project=room.name, source=plan.get("source") or "")  # before _follow removes the session
    _ledger(room.p, s, plan)
    _follow(sid, s, room, job, emit, sub.get("q") or room.hub.subscribe())
    emit({"type": "import", "id": sid, "stage": "build", "status": "start", "text": f"Building {room.name}",
          "built_project": room.name})
    return 202, {"ok": True, **{k: v for k, v in plan.items() if k not in ("design", "history")},
                 "job": {"id": job.id, "kind": job.kind, "status": job.status}}


def _follow(sid: str, s, room, job, emit, q):
    """Relay the new project's build to the importing tab, whose page only shows its own project's events: each stage
    as an `import` event (stage "build", `node`, `node_status`), then done or error. `q` is a subscription to the new
    room made before its build was queued. Then the session goes."""

    def run():
        try:
            while True:
                ended = job.wait(0)  # once it has, what is still queued is relayed, then the loop stops
                try:
                    ev = q.get_nowait() if ended else q.get(timeout=0.5)
                except queue.Empty:
                    if ended:
                        break
                    continue
                if ev.get("type") == "node":
                    emit({"type": "import", "id": sid, "stage": "build", "status": "progress", "node": ev.get("node"),
                          "node_status": ev.get("status"), "built_project": room.name,
                          "text": f"Building {room.name}: {ev.get('node')} {ev.get('status')}"})
        finally:
            room.hub.unsubscribe(q)
        if job.result:
            try:  # the verdict travels with "done": a robot that built but isn't feasible must not read as ready
                res = room.p.load_results(job.result)
            except (OSError, ValueError):
                res = {}
            summary = res.get("summary") or {}
            _explain(room, job.result, f"The imported robot {room.name}")   # in its own chat, before the tab opens it
            emit({"type": "import", "id": sid, "stage": "build", "status": "done", "built_project": room.name,
                  "rev": job.result, "text": f"Built {room.name} ({job.result})",
                  "feasible": summary.get("feasible"), "issues": [str(x)[:240] for x in (summary.get("issues") or [])[:4]],
                  "pick_place": summary.get("pick_place_success"),
                  "stages": {k: v for k, v in (res.get("stage_status") or {}).items() if isinstance(v, str)}})
        else:
            why = job.error or f"the build ended as {job.status}"
            emit({"type": "import", "id": sid, "stage": "build", "status": "error", "built_project": room.name,
                  "text": f"The build failed ({str(why)[:200]}). The project is there: edit it, or import the files again."})
        _release(sid)
        s.delete()

    threading.Thread(target=run, daemon=True, name=f"import-follow-{sid}").start()


def _queue_object(req, changes: dict, label: str):
    """Object keys as an edit of this tab's project, queued in its room like a parameter edit (W0b), with the finger
    repair when its grip would bend the fingers too far; when that build isn't feasible, the chat says why (_explain).
    None when the Studio has no queue (then _apply_async)."""
    from .rooms import QueueFull

    room, queue_changes = getattr(req, "room", None), getattr(req.studio, "queue_changes", None)
    if room is None or not callable(queue_changes) or getattr(room, "jobs", None) is None:
        return None
    changes, note = _fit_fingers(room, changes)
    try:
        job = queue_changes(room, changes, label=label, reply=f"Imported {label}: object and fingers updated.{note}",
                            source="import", by=_member_id(req))
    except QueueFull as exc:
        return _err(429, str(exc))
    when_done = getattr(req.studio, "when_done", None)
    if callable(when_done):
        when_done(job, lambda j: j.result and _explain(room, j.result, f"With the imported {label}, {room.name}"))
    return 202, job.info()


def _bends(flat: dict) -> bool:
    """Whether New robot's preflight (brief.preflight) flags the fingers' bending for this design."""
    from . import brief
    return "deflection" in brief.classify(brief.preflight(flat)["issues"])


def _fit_fingers(room, changes: dict) -> tuple[dict, str]:
    """New robot's finger repair for an imported object (brief.repair): while the grip would bend the fingers more than
    they may, the beam gets 1.5 mm thicker, up to 14 mm. Returns (changes, a sentence for the reply or "")."""
    try:
        base = room.latest()
        if base is None:
            return changes, ""
        flat = room.p.load_design(base).with_changes(changes).flat()
        if flat.get("arm.source"):          # an imported arm's own gripper: no finger beam to size
            return changes, ""
        t0 = t = float(flat["finger.thickness_mm"])
        while t < MAX_FINGER_MM and _bends({**flat, "finger.thickness_mm": t}):
            t = round(min(MAX_FINGER_MM, t + 1.5), 1)
    except Exception:  # noqa: BLE001  (the repair is a courtesy: the object is applied either way)
        traceback.print_exc()
        return changes, ""
    if t == t0:
        return changes, ""
    return ({**changes, "finger.thickness_mm": t},
            f" Its grip would bend the {t0:g} mm finger beams too far, so they are {t:g} mm thick now, as New robot "
            "would make them.")


def fixes_for(design, issues: list) -> list:
    """The changes Morph can make for a build's issues, as chips: [{id, label, changes}] (computed here: the page only
    names the one it wants). A joint short of torque: the 12 V STS3215 for an imported arm on 7.4 V ones, else Dynamixel
    servos; fingers that bend too far: the thickness that New robot's check passes."""
    import uuid

    out = []
    if any(TORQUE_RX.search(i) for i in issues):
        if design.arm.source and any("sts3215_7v4" in i for i in issues):
            out.append(("Use 12 V STS3215 servos", {"arm.actuator_overrides": "all=sts3215"}))
        elif design.arm.actuator_family != "dynamixel":
            out.append(("Swap to Dynamixel servos", {"arm.actuator_family": "dynamixel", "arm.actuator_overrides": ""}))
    if any(BEND_RX.search(i) for i in issues) and not design.arm.source:
        flat, t = design.flat(), float(design.finger.thickness_mm)
        try:
            while t < MAX_FINGER_MM:
                t = round(min(MAX_FINGER_MM, t + 1.5), 1)
                if not _bends({**flat, "finger.thickness_mm": t}):
                    break
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        if t > design.finger.thickness_mm:
            out.append((f"Thicker fingers ({t:g} mm)", {"finger.thickness_mm": t}))
    return [{"id": uuid.uuid4().hex[:12], "label": label, "changes": ch} for label, ch in out]


def _explain(room, rev: str, what: str):
    """After an import's build: when it isn't feasible, a chat message in its project says why, with the fixes Morph
    can make (each a chip that builds it as a new revision)."""
    try:
        res = room.p.load_results(rev)
        design = room.p.load_design(rev)
        summary = res.get("summary") or {}
        if summary.get("feasible") is not False:
            return
        issues = [str(x)[:240] for x in (summary.get("issues") or [])][:4]
        if not issues:
            return
        fixes = fixes_for(design, issues)
        text = (f"{what} built {rev}, but it isn't feasible yet:\n" + "\n".join(f"· {i}" for i in issues) +
                ("\nA fix Morph can make builds a new revision:" if fixes else
                 "\nChange what it flags here in the chat, or in the parameters."))
        room.hub.send({"type": "assistant", "text": text, "fixes": fixes, "source": "import", "rev": rev})
    except Exception:  # noqa: BLE001  (an explanation never breaks the import)
        traceback.print_exc()


def fix(req):
    """POST /api/import/fixes {fix}: build one of the fixes an import's build was offered in this project's chat."""
    from .rooms import QueueFull

    fid = (req.body or {}).get("fix") if isinstance(req.body, dict) else None
    room = getattr(req, "room", None)
    if not isinstance(fid, str) or not re.fullmatch(r"[a-f0-9]{12}", fid) or room is None:
        return _err(400, "Name one of the fixes in this project's chat.")
    with room.hub.lock:
        found = next((f for m in reversed(room.hub.history) for f in (m.get("fixes") or [])
                      if isinstance(f, dict) and f.get("id") == fid), None)
    if found is None:
        return _err(404, "That fix is no longer offered in this project's chat.")
    try:
        job = req.studio.queue_changes(room, dict(found["changes"]), label=found["label"],
                                       reply=f"{found['label']}: building it as a new revision.", source="import",
                                       user_text=found["label"], by=_member_id(req))
    except QueueFull as exc:
        return _err(429, str(exc))
    return 202, {"ok": True, "job": job.info()}


def _apply_async(req, changes: dict, label: str) -> bool:
    """Object keys go through the room's normal edit path (one pipeline run at a time)."""
    target = getattr(getattr(req, "room", None), "apply", None) or getattr(req.studio, "apply", None)
    if target is None:
        return False
    threading.Thread(target=lambda: target(changes, label), daemon=True, name="import-apply").start()
    return True


def delete(req, sid):
    """DELETE /api/import/<id>. A running download or build finishes first; its files go when it ends."""
    home = _home(req)
    with _busy_lock:
        if _busy.get(sid):
            _doomed[sid] = home
            return 202, {"ok": True, "pending": True}
    sess.Session(sid, home).delete()
    return 200, {"ok": True}


def dispatch(req):
    """One handler for the /api/import/* prefix (all methods)."""
    path = req.path.split("?", 1)[0]
    try:
        if path.rstrip("/") == "/api/import":
            if req.method == "POST":
                return create(req)
            return _err(405, "Use POST to start an import.")
        if path.rstrip("/") == "/api/import/fixes":
            return fix(req) if req.method == "POST" else _err(405, "Use POST to build a fix.")
        m = PATH_RX.match(path)
        if not m:
            return _err(404, "Unknown import route.")
        sid, sub = m.group(1), m.group(2)
        routes = {("GET", None): status, ("DELETE", None): delete, ("PUT", "file"): upload,
                  ("POST", "detect"): detect, ("POST", "commit"): commit}
        if sub in ("preview.glb", "preview.png") and req.method == "GET":
            return preview(req, sid, sub.split(".")[1])
        fn = routes.get((req.method, sub))
        if fn is None:
            return _err(405, "That method isn't supported here.")
        return fn(req, sid)
    except ImportFailed as exc:
        msg = str(exc)
        return _err(404 if "expired" in msg or "Unknown import session" in msg else 400, msg)
    except Exception as exc:  # noqa: BLE001  (never leak a traceback to the browser)
        traceback.print_exc()
        return _err(500, f"The import failed ({exc.__class__.__name__}); see the Studio's terminal for details.")


def register(route=None):
    """Register the import routes with W0b's router (the Studio's own when `route` is None)."""
    if route is None:
        from .routes import route  # noqa: F811  (W0b)
    route("POST", "/api/import", role="editor")(dispatch)
    route("GET", "/api/import/*", role="editor")(dispatch)
    route("POST", "/api/import/*", role="editor")(dispatch)
    route("DELETE", "/api/import/*", role="editor")(dispatch)
    route("PUT", "/api/import/*", role="editor", raw_body=True, max_body=LIMITS["upload_file"])(dispatch)
    return dispatch


register()  # the Studio loads every api_*.py at start (contracts section 2)
