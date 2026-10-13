"""Show Morph the job: the routes behind the "Show Morph the job" button (RELEASE_CONTRACTS sections 2 and 3, F3).

    POST /api/video/analyze   {id?, frames: [{t_s, jpeg_b64}], marks?: {grasp_s, release_s}, crop?, subq?: bool,
                              kind?: video|photos|photo}  (16 MiB)  -> {ok, id, result, mapping} or {ok, id, result: null, mode: "manual"}
    PUT  /api/video/upload?id=&count=   raw video bytes (500 MB): frames extracted on this computer by ffmpeg -> {ok, frames}
    POST /api/video/apply     {changes, label?, base_rev?} -> 202 {ok, job, changes, notes} (queued in the project's room);
                              {result, answers, preview: true} -> {ok, mapping}
    GET  /api/video/info      what this Studio can do: Claude, SubQ, ffmpeg, limits

Each handler takes the router's Request (morph/studio/routes.py) and returns (status, dict). Frames are checked before
they are decoded, uploads are streamed to MORPH_HOME/.uploads in 1 MiB chunks after their size is checked, and the
uploaded video is deleted as soon as its frames are out. Progress goes to the project's live channel as `video` events
(with the request's id) and `agent` events (role "vision"). The ledger gets counts and timings only, never frames.
"""

from __future__ import annotations

import base64
import os
import re
import secrets
import shutil
import threading
import time

from . import ffmpeg, vision

try:  # Wave 0's router; until it is merged the handlers are plain functions (the tests call them directly)
    from .routes import route
except ImportError:  # pragma: no cover - depends on the merge order
    route = None
try:  # Wave 0's room job queue: applied findings wait their turn like a parameter edit; a full queue answers 429
    from .rooms import QueueFull
except ImportError:  # pragma: no cover - depends on the merge order
    class QueueFull(RuntimeError):
        """Stands in for rooms.QueueFull until Wave 0 is merged (nothing raises it then)."""

MAX_BODY = 16 << 20          # JSON with up to 12 base64 frames
MAX_UPLOAD = 500 << 20       # a raw video for the ffmpeg fallback
CHUNK = 1 << 20
MAX_ANALYSES = 2             # at once on one Studio: each holds a thread for up to two minutes
MAX_DECODES = 1
NO_FFMPEG = ("This browser can't play this video, and the computer running Morph has no ffmpeg to extract frames. "
             "Export it as H.264 MP4 (iPhone: Settings > Camera > Formats > Most Compatible), or install ffmpeg "
             "(or pip install morph[video]) on the Studio computer.")
NOT_VIDEO = "That file doesn't look like a video (MP4, MOV, WebM, MKV or AVI)."
ID = re.compile(r"[a-z0-9]{8,32}")
ROUTES: list = []
_analyses = threading.BoundedSemaphore(MAX_ANALYSES)
_decodes = threading.BoundedSemaphore(MAX_DECODES)

# Keewano event types for video (RELEASE_CONTRACTS section 10: ids 2160-2179), for keewano.CUSTOM_EVENTS (append-only):
# counts and timings only. keewano_events() maps this module's ledger entries onto them.
KEEWANO_EVENTS = [
    {"eventTypeId": 2160, "name": "VideoAnalysis", "dataType": "string"},       # kind: video | photos | photo | manual
    {"eventTypeId": 2161, "name": "VideoFrames", "dataType": "uint"},
    {"eventTypeId": 2162, "name": "VideoAnalyzeMs", "dataType": "uint"},
    {"eventTypeId": 2163, "name": "VideoSource", "dataType": "string"},         # claude | subq | claude+subq | none
    {"eventTypeId": 2164, "name": "VideoUsable", "dataType": "uint"},           # 0 | 1
    {"eventTypeId": 2165, "name": "VideoQuestions", "dataType": "uint"},
    {"eventTypeId": 2166, "name": "VideoCostMilliUsd", "dataType": "uint"},
    {"eventTypeId": 2167, "name": "VideoChangesApplied", "dataType": "uint"},
    {"eventTypeId": 2168, "name": "VideoDecodeMs", "dataType": "uint"},
    {"eventTypeId": 2169, "name": "VideoDecoder", "dataType": "string"},        # ffmpeg
    {"eventTypeId": 2170, "name": "VideoSecondOpinion", "dataType": "string"},  # agree | disagree | none
]


def keewano_events(entry: dict) -> list:
    """A video ledger entry -> [(event name, value)] for keewano.events_for (W2 wires it in)."""
    t = entry.get("type")
    if t == "video_analysis":
        ev = [("VideoAnalysis", entry.get("kind")), ("VideoFrames", entry.get("frames")), ("VideoAnalyzeMs", entry.get("ms")),
              ("VideoSource", entry.get("source")), ("VideoUsable", int(bool(entry.get("usable")))),
              ("VideoQuestions", entry.get("questions")), ("VideoCostMilliUsd", round(1000 * (entry.get("cost_usd") or 0))),
              ("VideoSecondOpinion", entry.get("second_opinion"))]
    elif t == "video_apply":
        ev = [("VideoChangesApplied", entry.get("changes"))]
    elif t == "video_decode":
        ev = [("VideoDecoder", entry.get("decoder")), ("VideoDecodeMs", entry.get("ms")), ("VideoFrames", entry.get("frames"))]
    else:
        return []
    return [(n, v) for n, v in ev if v is not None and v != ""]


def _route(method: str, path: str, **opts):
    """Register with the router when it exists, and always in ROUTES (what W2 wires, and what tests read)."""
    def deco(fn):
        ROUTES.append({"method": method, "path": path, "handler": fn, **opts})
        if route is not None:
            route(method, path, **opts)(fn)
        return fn
    return deco


# ------------------------------------------------------------------ request plumbing
class BodyError(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def content_length(headers, max_body: int) -> int:
    """The declared body size, checked before a byte is read (the security module's when Wave 0 has merged)."""
    try:
        from .security import BodyError as _SecError, content_length as _cl
    except ImportError:
        _cl = None
    if _cl is not None:
        try:
            return _cl(headers, max_body)
        except _SecError as exc:
            if exc.status == 413:  # in MiB: "512000 KiB" means nothing to someone holding a phone video
                raise BodyError(413, f"The request body is larger than the {max_body >> 20} MiB limit.") from None
            raise BodyError(exc.status, str(exc)) from None
    if headers.get("Transfer-Encoding"):
        raise BodyError(400, "Send the request body with a Content-Length header.")
    values = headers.get_all("Content-Length") if hasattr(headers, "get_all") else [headers.get("Content-Length")]
    values = [v for v in values or [] if v is not None]
    if not values:
        return 0
    if len(values) > 1 or not re.fullmatch(r"[0-9]{1,15}", str(values[0]).strip()):
        raise BodyError(400, "Malformed Content-Length header.")
    n = int(values[0])
    if n > max_body:
        raise BodyError(413, f"The request body is larger than the {max_body >> 20} MiB limit.")
    return n


def _room(req):
    """The ?project= room (W0b), or None before Wave 0. The router's Request resolves it on first use and raises its
    HTTPError (400 bad name, 404 unknown project): handlers touch it first, so a bad project fails before any work."""
    return getattr(req, "room", None)


def _studio(req):
    return getattr(req, "studio", None)


def _project(req):
    """The project the request is about: the room's (W0b), or the Studio's current one."""
    for owner in (_room(req), _studio(req)):
        p = getattr(owner, "p", None)
        if p is not None:
            return p
    return None


def _project_name(req) -> str | None:
    room = _room(req)
    name = getattr(room, "name", None)
    if isinstance(name, str):
        return name
    p = _project(req)
    return os.path.basename(os.path.normpath(p.root)) if p is not None else None


def _hub(req):
    for owner in (_room(req), _studio(req)):
        hub = getattr(owner, "hub", None)
        if hub is not None:
            return hub
    return None


def _member(req) -> dict:
    m = getattr(req, "member", None)
    return m if isinstance(m, dict) and m.get("id") else {}


def send(req, ev: dict):
    """An event on the project's live channel; never raises (a closed tab, a missing hub or an unknown project must not
    break a request). With Teams on it carries by: {id, name, color}."""
    try:
        hub = _hub(req)
        if hub is None:
            return
        ev = {**ev, "project": _project_name(req)}
        member = _member(req)
        if member:
            ev.setdefault("by", {k: member[k] for k in ("id", "name", "color") if member.get(k) is not None})
        hub.send(ev)
    except Exception:  # noqa: BLE001
        pass


def _design(req):
    """The design a change would apply to: the room's newest finished revision (W0b), else the project's latest."""
    room, p = _room(req), _project(req)
    try:
        latest = getattr(room, "latest", None)
        rev = latest() if callable(latest) else p.latest() if p is not None else None
        return p.load_design(rev) if p is not None and rev else None
    except Exception:  # noqa: BLE001  (a project without a finished revision: map without "old" values)
        return None


def _home(req) -> str:
    studio = _studio(req)
    home = getattr(studio, "home", None)
    return home or os.environ.get("MORPH_HOME") or "projects"


def _log(req, entry: dict):
    """Counts and timings in the project's ledger (and Keewano, when it is on); never frames, text or object names."""
    p = _project(req)
    if p is None:
        return
    member = _member(req)
    if member:  # Teams: the ledger names who did it by member id
        entry = {**entry, "by": member["id"]}
    try:
        p.log(entry)
    except Exception:  # noqa: BLE001  (a read-only projects folder must not fail the request)
        pass


def _job_id(raw) -> str:
    return raw if isinstance(raw, str) and ID.fullmatch(raw) else secrets.token_hex(8)


def _query(req, name: str):
    q = getattr(req, "query", None) or {}
    v = q.get(name)
    return v[0] if isinstance(v, list) and v else v if isinstance(v, str) else None


def _int(v, default: int, lo: int, hi: int) -> int:
    try:
        return min(max(int(v), lo), hi)
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------------ routes
@_route("GET", "/api/video/info", role="viewer")
def info(req):
    """What the upload screen needs before anything is sent: which services and the ffmpeg fallback are available here."""
    claude, sub = vision.claude_ready(), vision.subq_ready()
    ff = ffmpeg.status()
    studio = _studio(req)
    return 200, {"ok": True, "claude": claude, "subq": sub, "ffmpeg": ff["available"], "ffmpeg_version": ff.get("version"),
                 "manual_reason": None if claude or sub else vision.offline_reason(),
                 "remote": bool(getattr(studio, "remote", False)),
                 "limits": {"frames": vision.MAX_FRAMES, "frame_mb": vision.MAX_FRAME_BYTES >> 20, "body_mb": MAX_BODY >> 20,
                            "upload_mb": MAX_UPLOAD >> 20, "seconds": int(ffmpeg.MAX_SECONDS), "long_edge": vision.FRAME_EDGE,
                            "timeout_s": int(vision.TIMEOUT_S)}}


@_route("POST", "/api/video/analyze", role="editor", max_body=MAX_BODY)
def analyze(req):
    """Frames -> estimates and the design changes they imply (nothing is applied). Holds the request until Claude (and
    SubQ, when asked) answer, at most two minutes, while `video`/`agent` events report progress under the request's id."""
    _room(req)  # an unknown ?project= is refused (404) before any work
    body = req.body if isinstance(getattr(req, "body", None), dict) else {}
    kind = body.get("kind") if body.get("kind") in vision.KINDS else "video"
    try:
        frames = vision.decode_frames(body.get("frames"), max_frames=1 if kind == "photo" else vision.MAX_FRAMES)
    except ValueError as exc:
        return 400, {"ok": False, "error": str(exc)}
    crop = body.get("crop") if isinstance(body.get("crop"), dict) else None
    vid, use_subq = _job_id(body.get("id")), body.get("subq") is True
    if not _analyses.acquire(blocking=False):
        return 429, {"ok": False, "error": "Two analyses are already running on this Studio; try again in a moment."}
    t0 = time.time()
    try:
        send(req, {"type": "video", "id": vid, "stage": "analyze", "status": "start",
                   "text": f"Analyzing {len(frames)} {'photo' if kind == 'photo' else 'frame' + ('s' if len(frames) != 1 else '')}"})
        emit = lambda ev: send(req, {**ev, "id": vid})  # noqa: E731
        if kind == "photo":
            result = vision.analyze_photo(frames[0], use_subq=use_subq, on_event=emit, crop=crop)
        else:
            result = vision.analyze_frames(frames, marks=body.get("marks"), crop=crop, use_subq=use_subq, on_event=emit,
                                           kind=kind)
    except vision.ImageError as exc:
        send(req, {"type": "video", "id": vid, "stage": "analyze", "status": "error", "text": str(exc)})
        return 400, {"ok": False, "error": str(exc)}
    finally:
        _analyses.release()
    if result is None:  # keyless or offline: nothing was sent anywhere; the cards are filled in by hand
        send(req, {"type": "video", "id": vid, "stage": "analyze", "status": "skipped", "text": vision.offline_reason()})
        _log(req, {"type": "video_analysis", "kind": "manual", "frames": len(frames), "ms": round((time.time() - t0) * 1000),
                   "source": "none", "usable": False, "questions": 0, "cost_usd": 0.0, "second_opinion": "none"})
        return 200, {"ok": True, "id": vid, "result": None, "mode": "manual", "why": vision.offline_reason(),
                     "mapping": vision.map_to_changes(None, _design(req))}
    mapping = vision.map_to_changes(result, _design(req))
    op = result.get("second_opinion") or {}
    _log(req, {"type": "video_analysis", "kind": kind, "frames": result["frames"], "ms": result["ms"],
               "source": result.get("source") or "none", "usable": result["usable"], "questions": len(mapping["questions"]),
               "cost_usd": result["cost_usd_est"],
               "second_opinion": ("disagree" if op.get("disagree") else "agree") if op else "none"})
    return 200, {"ok": True, "id": vid, "result": result, "mapping": mapping}


def _chunks(req, n: int):
    """The raw body, exactly n bytes in 1 MiB chunks: the router's stream (W0b; it marks the body as read and raises
    its HTTPError 400 when the upload is cut off), else straight from the socket."""
    stream = getattr(req, "stream", None)
    if callable(stream) and getattr(req, "length", None) == n:
        yield from stream(CHUNK)
        return
    rfile, left = req.handler.rfile, n
    while left > 0:
        chunk = rfile.read(min(CHUNK, left))
        if not chunk:
            raise BodyError(400, "The upload was cut off.")
        left -= len(chunk)
        yield chunk


def _receive(chunks, path: str) -> str:
    """Write the body's chunks into `path`; the first one must look like a video. Returns the container ffmpeg should
    read it as."""
    fmt = None
    with open(path, "wb") as fh:
        for chunk in chunks:
            if fmt is None:
                fmt = ffmpeg.sniff(chunk[:64])
                if fmt is None:
                    raise BodyError(415, NOT_VIDEO)
            fh.write(chunk)
    return fmt


@_route("PUT", "/api/video/upload", role="editor", raw_body=True, max_body=MAX_UPLOAD)
def upload(req):
    """The fallback for clips the browser can't play: the raw video is streamed to a private folder under
    MORPH_HOME/.uploads, ffmpeg keeps `count` frames (one pass, the first 180 s), and the folder is deleted before the
    answer goes out, whatever happened. Returns {ok, frames: [{t_s, jpeg_b64, w, h}], duration_s, decoder, ms}."""
    handler = getattr(req, "handler", None)
    try:
        n = content_length(handler.headers, MAX_UPLOAD)
    except BodyError as exc:
        return exc.status, {"ok": False, "error": str(exc)}
    _room(req)  # an unknown ?project= is refused before the body is read
    exe = ffmpeg.find()
    if not exe:
        return 501, {"ok": False, "code": "no_ffmpeg", "error": NO_FFMPEG}
    if n == 0:
        return 400, {"ok": False, "error": "Send the video file as the request body."}
    vid, count = _job_id(_query(req, "id")), _int(_query(req, "count"), ffmpeg.COUNT, 1, ffmpeg.MAX_COUNT)
    if not _decodes.acquire(blocking=False):
        return 429, {"ok": False, "error": "Another video is being extracted on this Studio; try again in a moment."}
    home, work, t0 = _home(req), None, time.time()
    try:
        ffmpeg.cleanup(home, older_than_s=3600)  # leftovers from a crash; a running upload's folder is younger
        work = ffmpeg.new_workdir(home)
        send(req, {"type": "video", "id": vid, "stage": "upload", "status": "start",
                   "text": f"Receiving the video ({n / 1048576:.0f} MB) on the Studio computer"})
        fmt = _receive(_chunks(req, n), os.path.join(work, "clip"))
        send(req, {"type": "video", "id": vid, "stage": "decode", "status": "start", "text": "Extracting frames with ffmpeg"})
        out = ffmpeg.extract(os.path.join(work, "clip"), count=count, fmt=fmt, exe=exe, workdir=work)
        frames = [{"t_s": f["t_s"], "jpeg_b64": base64.b64encode(f["jpeg"]).decode("ascii"), "w": f["w"], "h": f["h"]}
                  for f in out["frames"]]
        send(req, {"type": "video", "id": vid, "stage": "decode", "status": "done",
                   "text": f"{len(frames)} frames extracted in {out['ms'] / 1000:.1f} s; the video was deleted"})
        _log(req, {"type": "video_decode", "decoder": "ffmpeg", "ms": round((time.time() - t0) * 1000), "frames": len(frames)})
        return 200, {"ok": True, "id": vid, "frames": frames, "duration_s": out["duration_s"], "decoder": "ffmpeg", "ms": out["ms"]}
    except BodyError as exc:
        return exc.status, {"ok": False, "error": str(exc)}
    except ffmpeg.FFmpegError as exc:
        send(req, {"type": "video", "id": vid, "stage": "decode", "status": "error", "text": str(exc)})
        status = {"timeout": 504, "not_video": 415, "no_ffmpeg": 501}.get(exc.code, 422)
        return status, {"ok": False, "code": exc.code, "error": str(exc)}
    except OSError:
        return 507, {"ok": False, "error": "Couldn't store the upload on the Studio computer (is the disk full?)."}
    finally:
        if work:
            shutil.rmtree(work, ignore_errors=True)
        _decodes.release()


def _target(req):
    """Before Wave 0: whoever runs pipeline edits, the Studio itself, with apply(changes, label, user_text=None,
    reply=None, source="")."""
    for owner in (_room(req), _studio(req)):
        if callable(getattr(owner, "apply", None)):
            return owner
    return None


def _queued(room, job) -> dict:
    """A queued job as the room's queue shows it (with its position), like /api/params answers."""
    try:
        return next((j for j in room.jobs.snapshot() if j.get("id") == job.id), None) or job.info()
    except Exception:  # noqa: BLE001
        return {"id": getattr(job, "id", None)}


@_route("POST", "/api/video/apply", role="editor")
def apply(req):
    """{result, answers, preview: true}: the exact list of design changes for the cards as edited (nothing applied).
    {changes, label?, base_rev?}: apply them; they are checked again here (only the keys a video may set, clamped),
    then queued in the project's room like a parameter edit (W0b: 202 {ok, job, changes, notes}; 429 when the queue
    is full; a conflict with someone else's newer change is asked about in the chat, as for any edit)."""
    room = _room(req)
    body = req.body if isinstance(getattr(req, "body", None), dict) else {}
    if body.get("preview") is True:
        result, answers = body.get("result"), body.get("answers")
        if result is not None and not isinstance(result, dict) or answers is not None and not isinstance(answers, dict):
            return 400, {"ok": False, "error": "Send result and answers as JSON objects."}
        return 200, {"ok": True, "mapping": vision.map_to_changes(result, _design(req), answers)}
    try:
        changes, notes = vision.validate_changes(body.get("changes"))
    except ValueError as exc:
        return 400, {"ok": False, "error": str(exc)}
    label = re.sub(r"[^\w .,:()/+-]", "", str(body.get("label") or ""))[:48].strip() or "from a video"
    items = [{"key": k, "label": vision.LABELS.get(k, k), "new": v} for k, v in changes.items()]
    reply = vision.describe({"items": items})
    queue = getattr(_studio(req), "queue_changes", None)
    if room is not None and callable(queue):
        base = body.get("base_rev")
        try:
            base = base if isinstance(base, str) and base in room.completed() else None
        except Exception:  # noqa: BLE001
            base = None
        try:
            job = queue(room, changes, label=label, reply=reply, source="video", base_rev=base,
                        by=_member(req).get("id"))
        except QueueFull as exc:
            return 429, {"ok": False, "error": str(exc)}
        _log(req, {"type": "video_apply", "changes": len(changes)})
        return 202, {"ok": True, "job": _queued(room, job), "changes": changes, "notes": notes}
    target = _target(req)  # before Wave 0: one project, applied in a thread while nothing else is building
    if target is None:
        return 409, {"ok": False, "error": "No project is open: start a new robot from the video instead."}
    busy = getattr(target, "busy", None) or getattr(_studio(req), "busy", None)
    if busy is not None and getattr(busy, "locked", lambda: False)():
        return 409, {"ok": False, "error": "Still regenerating the last change; apply again when the pipeline strip is idle."}
    threading.Thread(target=target.apply, args=(changes, label), kwargs={"reply": reply, "source": "video"}, daemon=True).start()
    _log(req, {"type": "video_apply", "changes": len(changes)})
    return 202, {"ok": True, "changes": changes, "notes": notes}
