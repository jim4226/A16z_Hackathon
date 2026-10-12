"""Show Morph the job: still frames (or photos) of a person doing a task by hand -> what a desk robot arm would have to
do, as editable estimates with a confidence each, and the design changes they imply.

    from morph.studio import vision
    res = vision.analyze_frames(frames, marks={"grasp_s": 2.1, "release_s": 4.3}, use_subq=True, on_event=print)
    vision.map_to_changes(res, design)   # {"changes", "items", "notes", "questions", "slots"}: nothing is applied here

Claude reads the frames with structured output (the JSON schema of VideoAnalysis, validated again here); SubQ Snap can
give a beta second opinion on four of them. Models only propose: map_to_changes() turns the estimates into dotted
design changes with Morph's own presets (an egg is 25 N) and rules, every value clamped, and anything under 0.6
confidence becomes a question instead of a change. With no key, or network lookups switched off, analyze_frames()
returns None and nothing leaves the computer: the Studio shows the same cards empty, with a measuring tool. Images are
re-encoded here (EXIF and GPS dropped) before they go anywhere, and nothing is written to disk.
"""

from __future__ import annotations

import base64
import binascii
import copy
import io
import math
import os
import re
import threading
import time
import warnings
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from .intent import ALIASES, MODEL, OBJECTS, fit_finger, object_changes

try:  # Wave 0's SubQ client; missing or switched off means "no second opinion"
    from . import subq
except ImportError:  # pragma: no cover - depends on the merge order
    subq = None

MAX_FRAMES = 12
MAX_FRAME_BYTES = 4 << 20                 # one JPEG, as sent by the browser
MAX_PHOTO_BYTES = 20 << 20                # one photo file handed to analyze_photo()
MAX_PIXELS = 50_000_000                   # refused before decoding (decompression bombs)
FRAME_EDGE, PHOTO_EDGE, SUBQ_EDGE = 1280, 2576, 768
TIMEOUT_S = 120.0                         # the whole analysis; progress is reported every PROGRESS_S
PROGRESS_S = 3.0
CLAUDE_TIMEOUT_S = 55.0                   # per attempt: one retry still ends inside TIMEOUT_S
SUBQ_TIMEOUT_S = 15.0
MAX_TOKENS = 4096
MIN_CONF = 0.6
PRICE_IN, PRICE_OUT = 4e-6, 20e-6         # USD per token for intent.MODEL
SUBQ_PRICE_IN = 0.05e-6

TASKS = ("pick_place", "sort", "pour", "insert", "stack", "other")
SHAPES = ("cylinder", "box", "sphere", "irregular")
SCALES = ("ruler", "card", "known_product", "hand", "none")
FRAGILITY = ("robust", "firm", "delicate", "very_delicate")
SURFACES = ("dry", "smooth", "wet")
SETTINGS = ("food", "lab", "office", "workshop", "warehouse", "outdoor", "home")
CLUTTER = ("low", "medium", "high")
SECTIONS = ("task", "object", "motion", "timing", "environment", "safety")
KINDS = ("video", "photos", "photo")
DAMAGE_N = {"robust": 0.0, "firm": 30.0, "delicate": 10.0, "very_delicate": 3.0}  # custom objects; presets win
MASS_CHIPS = ((30.0, "Under 50 g"), (120.0, "50 to 200 g"), (350.0, "200 to 500 g"), (700.0, "Over 500 g"))
TASK_LABEL = {"pick_place": "pick and place", "sort": "sorting", "pour": "pouring", "insert": "inserting",
              "stack": "stacking", "other": "something else"}
FRAGILITY_LABEL = {"robust": "robust", "firm": "firm", "delicate": "delicate", "very_delicate": "very delicate"}

SYSTEM = (
    "You are Morph's task analyst. The images are frames from one phone video of a person doing a job by hand, each "
    "labelled \"Frame i, t = s\". Work out what a desk robot arm would have to do, using only what is visible. Sizes: "
    "derive them from a visible reference (ruler, ID card, known product, then a hand), name it, and give a range; with "
    "no reference, give a range and confidence ≤ 0.4. Positions: robot base at the table edge nearest the demonstrator, "
    "x forward, y left, in mm. Cycle time: only from actions repeated across timestamps. People: count them; never "
    "describe or identify them. If no task is visible, set usable=false and say why. Ask at most 3 questions, the ones "
    "that would most change the robot. Morph builds arms with 200–500 mm reach and grippers for cylinders, boxes and "
    "spheres.")
PHOTO_SYSTEM = (
    "You are Morph's object analyst. The image is one photo of an object a desk robot arm should pick up. Using only "
    "what is visible, say what the object is and estimate its shape, size, mass, fragility and surface. Sizes: derive "
    "them from a visible reference (ruler, ID card, known product, then a hand), name it, and give a range; with no "
    "reference, give a range and confidence ≤ 0.4. People: never describe or identify them. If no single object a "
    "gripper could hold is visible, set usable=false and say why. Ask at most 3 questions, the ones that would most "
    "change the robot. Morph builds grippers for cylinders, boxes and spheres about 26 to 75 mm wide.")


class ImageError(ValueError):
    """An image Morph refuses; the message is safe to show."""


def _text(v, n: int) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()[:n]


def _texts(v, n: int, most: int) -> list:
    return [t for t in (_text(x, n) for x in (v if isinstance(v, list) else [])) if t][:most]


# ------------------------------------------------------------------ the schema Claude fills (and the server re-checks)
class _Sec(BaseModel):
    confidence: float = Field(description="0 to 1: how sure, from what is visible")
    evidence_frames: list[int] = Field(description="the frame numbers this is based on")

    @field_validator("confidence", mode="before")
    @classmethod
    def _confidence(cls, v):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return 0.0
        return min(max(v, 0.0), 1.0) if math.isfinite(v) else 0.0

    @field_validator("evidence_frames", mode="before")
    @classmethod
    def _evidence(cls, v):
        out = []
        for x in v if isinstance(v, list) else []:
            if isinstance(x, (int, float)) and not isinstance(x, bool) and 1 <= x <= MAX_FRAMES and int(x) not in out:
                out.append(int(x))
        return out


class TaskEst(_Sec):
    type: Literal[TASKS]
    summary: str = Field(description="one sentence: what the hands do")

    @field_validator("summary", mode="before")
    @classmethod
    def _summary(cls, v):
        return _text(v, 240)


class ObjectEst(_Sec):
    name: str = Field(description="what the handled object is, e.g. egg")
    shape: Literal[SHAPES]
    diameter_mm: float | None = Field(description="width where a two-finger gripper would hold it")
    length_mm: float | None
    size_range_mm: list[float] = Field(description="[smallest, largest] plausible width in mm")
    scale_reference: Literal[SCALES]
    mass_g: float | None
    fragility: Literal[FRAGILITY]
    surface: Literal[SURFACES]
    count_per_cycle: int

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, v):
        return _text(v, 60)


class Point(BaseModel):
    x_mm: float
    y_mm: float
    z_mm: float


class MotionEst(_Sec):
    pick: Point | None
    place: Point | None
    positions_vary: bool = Field(description="objects are picked from different spots in different cycles")


class TimingEst(_Sec):
    cycle_time_s: float | None = Field(description="seconds per repeated cycle; null unless an action repeats")
    cycles_seen: int


class EnvironmentEst(_Sec):
    setting: Literal[SETTINGS]
    clutter: Literal[CLUTTER]


class SafetyEst(_Sec):
    other_people_visible: int = Field(description="people visible besides the demonstrator's hands: a count only")
    hands_share_workspace: bool = Field(description="people will keep working within the robot's reach")
    hazards: list[str] = Field(description="e.g. glass, sharp edges, hot surfaces")

    @field_validator("hazards", mode="before")
    @classmethod
    def _hazards(cls, v):
        return _texts(v, 40, 6)


class VideoAnalysis(BaseModel):
    usable: bool
    unusable_reason: str | None
    task: TaskEst
    object: ObjectEst
    motion: MotionEst
    timing: TimingEst
    environment: EnvironmentEst
    safety: SafetyEst
    questions: list[str] = Field(description="at most 3")

    @field_validator("questions", mode="before")
    @classmethod
    def _questions(cls, v):
        return _texts(v, 200, 3)


class PhotoAnalysis(BaseModel):
    usable: bool
    unusable_reason: str | None
    object: ObjectEst
    questions: list[str] = Field(description="at most 3")

    @field_validator("questions", mode="before")
    @classmethod
    def _questions(cls, v):
        return _texts(v, 200, 3)


_SCHEMAS: dict = {}


def schema(model=VideoAnalysis) -> dict:
    """The JSON schema sent as output_config.format (the SDK's transform moves what structured output can't enforce into
    descriptions; pydantic re-checks everything on the way back)."""
    if model not in _SCHEMAS:
        import anthropic
        _SCHEMAS[model] = anthropic.transform_schema(model)
    return _SCHEMAS[model]


# ------------------------------------------------------------------ availability (no network)
def _network_allowed() -> bool:
    try:
        from .. import settings
    except ImportError:  # pragma: no cover - Wave 0 adds morph/settings.py
        return os.environ.get("MORPH_OFFLINE", "").strip().lower() not in ("1", "true", "yes")
    return settings.network_allowed()


def claude_ready() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")) and _network_allowed()


def subq_ready() -> bool:
    try:
        return subq is not None and bool(subq.enabled()) and _network_allowed()
    except Exception:  # noqa: BLE001  (the client promises not to raise; a broken one means "off")
        return False


def offline_reason() -> str:
    if not _network_allowed():
        return "Network lookups are off on this computer, so nothing is sent anywhere: fill in the cards yourself."
    return "There is no Claude key on this computer, so nothing is sent anywhere: fill in the cards yourself."


# ------------------------------------------------------------------ images
def sniff_image(data: bytes) -> str | None:
    if data[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _crop_box(crop, w: int, h: int):
    """A crop as {x, y, w, h} fractions of the upright image -> a pixel box, or None for no crop."""
    if not crop:
        return None
    try:
        x, y, cw, ch = (float(crop[k]) for k in ("x", "y", "w", "h"))
    except (KeyError, TypeError, ValueError):
        raise ImageError("The crop box needs x, y, w and h between 0 and 1.") from None
    if not all(math.isfinite(v) for v in (x, y, cw, ch)):
        raise ImageError("The crop box needs x, y, w and h between 0 and 1.")
    x, y = min(max(x, 0.0), 0.95), min(max(y, 0.0), 0.95)
    cw, ch = min(max(cw, 0.05), 1.0 - x), min(max(ch, 0.05), 1.0 - y)
    box = (round(x * w), round(y * h), round((x + cw) * w), round((y + ch) * h))
    return None if box == (0, 0, w, h) else box


def prepare_image(data: bytes, *, long_edge: int = FRAME_EDGE, crop=None, quality: int = 85,
                  max_bytes: int = MAX_FRAME_BYTES) -> dict:
    """Decode a JPEG, PNG or WebP with Pillow, turn it upright, crop, shrink to `long_edge` and save a fresh JPEG: only
    the pixels survive (no EXIF, GPS, maker notes or ICC profile). Returns {"jpeg", "w", "h"}; raises ImageError."""
    from PIL import Image, ImageOps

    if not isinstance(data, (bytes, bytearray)) or not data:
        raise ImageError("An image was empty.")
    if len(data) > max_bytes:
        raise ImageError(f"An image is larger than {max_bytes >> 20} MiB.")
    if not sniff_image(bytes(data[:16])):
        raise ImageError("Send JPEG, PNG or WebP images.")
    try:
        with warnings.catch_warnings():  # our own pixel cap below answers first; Pillow's warning is noise
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            im = Image.open(io.BytesIO(data))
        with im:
            if im.width * im.height > MAX_PIXELS:
                raise ImageError("An image has more than 50 megapixels.")
            im.draft("RGB", (long_edge * 2, long_edge * 2))  # JPEG: decode a huge photo at a fraction of full size
            img = ImageOps.exif_transpose(im).convert("RGB")
    except ImageError:
        raise
    except Exception:  # noqa: BLE001  (truncated or malformed files: Pillow raises many kinds)
        raise ImageError("Morph couldn't read one of the images.") from None
    box = _crop_box(crop, img.width, img.height)
    if box:
        img = img.crop(box)
    img.thumbnail((long_edge, long_edge), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, "JPEG", quality=quality, optimize=True)
    return {"jpeg": out.getvalue(), "w": img.width, "h": img.height}


def decode_frames(raw, max_frames: int = MAX_FRAMES) -> list:
    """Request frames [{t_s, jpeg_b64}] -> [{"t_s", "jpeg"}], checked before anything is decoded: 1 to 12 frames,
    each a base64 JPEG (by its first bytes) of at most 4 MiB. Raises ValueError with a message to show."""
    if not isinstance(raw, list) or not raw:
        raise ValueError("Send at least one frame.")
    if len(raw) > max_frames:
        raise ValueError(f"Send at most {max_frames} frames.")
    limit = 4 * ((MAX_FRAME_BYTES + 2) // 3)
    out = []
    for i, f in enumerate(raw, 1):
        if not isinstance(f, dict) or not isinstance(f.get("jpeg_b64"), str):
            raise ValueError(f"Frame {i} needs jpeg_b64.")
        s = f["jpeg_b64"]
        if s.startswith("data:"):
            s = s.split(",", 1)[-1]
        if len(s) > limit:
            raise ValueError(f"Frame {i} is larger than {MAX_FRAME_BYTES >> 20} MiB.")
        try:
            data = base64.b64decode(s, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError(f"Frame {i} isn't valid base64.") from None
        if data[:3] != b"\xff\xd8\xff":
            raise ValueError(f"Frame {i} isn't a JPEG.")
        t = f.get("t_s")
        good_t = t is None or (isinstance(t, (int, float)) and not isinstance(t, bool) and math.isfinite(t) and 0 <= t <= 86400)
        if not good_t:
            raise ValueError(f"Frame {i} has a bad timestamp.")
        out.append({"t_s": None if t is None else round(float(t), 2), "jpeg": data})
    return out


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def image_tokens(w: int, h: int) -> int:
    return math.ceil(w / 28) * math.ceil(h / 28)


# ------------------------------------------------------------------ marks, events, threads
def clean_marks(marks) -> dict | None:
    """{"grasp_s", "release_s"} (either may be None), or None when nothing usable was marked."""
    if not isinstance(marks, dict):
        return None
    out = {}
    for k in ("grasp_s", "release_s"):
        v = marks.get(k)
        ok = isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and 0 <= v <= 86400
        out[k] = round(float(v), 2) if ok else None
    return out if any(v is not None for v in out.values()) else None


def _emitter(on_event):
    def emit(**ev):
        if on_event:
            try:
                on_event(ev)
            except Exception:  # noqa: BLE001  (a broken listener never breaks an analysis)
                pass
    return emit


def _in_thread(fn, *args):
    box = {}

    def run():
        try:
            box["value"] = fn(*args)
        except Exception as exc:  # noqa: BLE001  (reported as this provider's error)
            box["error"] = exc
    t = threading.Thread(target=run, daemon=True)  # daemon: a slow call never holds the Studio open at exit
    t.start()
    return t, box


def _wait(jobs: dict, emit, text: str, t0: float) -> list:
    """Wait for every job until TIMEOUT_S, reporting progress; returns the names still running at the deadline."""
    deadline = t0 + TIMEOUT_S
    while True:
        alive = [n for n, (t, _) in jobs.items() if t.is_alive()]
        left = deadline - time.time()
        if not alive or left <= 0:
            return alive
        jobs[alive[0]][0].join(min(PROGRESS_S, left))
        if any(t.is_alive() for t, _ in jobs.values()) and time.time() < deadline:
            emit(type="video", stage="analyze", status="progress", text=f"{text} ({time.time() - t0:.0f} s)")


# ------------------------------------------------------------------ Claude
def _claude_client():
    import anthropic
    return anthropic.Anthropic(timeout=CLAUDE_TIMEOUT_S, max_retries=1)


def _ask_claude(system: str, content: list, model_cls):
    """(parsed model | None, error | None, usage | None). Structured output; refusals, cut-offs and anything that fails
    the schema come back as an error name, never as an exception."""
    resp = _claude_client().messages.create(
        model=MODEL, max_tokens=MAX_TOKENS, system=system,
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema(model_cls)}},
        messages=[{"role": "user", "content": content}])
    usage = getattr(resp, "usage", None)
    if getattr(resp, "stop_reason", None) == "refusal":
        return None, "refusal", usage
    if getattr(resp, "stop_reason", None) == "max_tokens":
        return None, "cut_off", usage
    text = next((b.text for b in getattr(resp, "content", None) or [] if getattr(b, "type", "") == "text"), "")
    try:
        return model_cls.model_validate_json(text), None, usage
    except (ValidationError, ValueError):
        return None, "invalid", usage


def _usage_cost(usage) -> float | None:
    if usage is None:
        return None
    try:
        return (float(getattr(usage, "input_tokens", 0) or 0) * PRICE_IN
                + float(getattr(usage, "output_tokens", 0) or 0) * PRICE_OUT)
    except (TypeError, ValueError):
        return None


ERRORS = {
    "timeout": "Claude didn't answer within {s:.0f} s, so nothing was estimated.",
    "refusal": "Claude declined to analyze these images, so nothing was estimated.",
    "cut_off": "Claude's answer was cut off, so nothing was estimated.",
    "invalid": "Claude's answer didn't match what Morph expects, so it was ignored.",
    "failed": "Claude couldn't be reached ({why}), so nothing was estimated.",
}
WHY = {"AuthenticationError": "the key was rejected: check it under Keys", "PermissionDeniedError": "the key isn't allowed to",
       "RateLimitError": "too many requests: try again in a minute", "APIConnectionError": "no connection",
       "InternalServerError": "Anthropic's servers are busy", "OverloadedError": "Anthropic's servers are busy"}


def _claude_error_text(code: str, exc=None) -> str:
    why = WHY.get(exc.__class__.__name__, exc.__class__.__name__) if exc else "error"
    return ERRORS.get(code, ERRORS["failed"]).format(s=TIMEOUT_S, why=why) + " Fill in the cards yourself, or try again."


# ------------------------------------------------------------------ SubQ second opinion
def _noul(text: str) -> dict:
    return {"type": "noul", "instructions": text}


SUBQ_FRAGILITY = {"type": "score", "instructions": "How easily would a firm squeeze damage the handled object?",
                  "criteria": ["robust (tools, cans)", "firm (bottles)", "delicate (eggs, bulbs)", "very delicate (berries, chips)"]}
SUBQ_VIDEO = {
    "task_type": {"type": "choice", "instructions": "Which job is the person demonstrating?",
                  "criteria": {"pick_place": "moves an object to another spot", "sort": "routes objects by type",
                               "pour": "tilts a container", "insert": "fits a part into a hole or slot",
                               "stack": "piles objects", "other": None}},
    "fragility": SUBQ_FRAGILITY,
    "object_visible": _noul("Is the handled object clearly visible in at least one image?"),
    "hand_for_scale": _noul("Is a hand, ruler or card beside the object for scale?"),
    "other_people": _noul("Is anyone visible besides the demonstrator's hands?"),
    "food": _noul("Is the object food, or does the task touch food?"),
    "repeats": _noul("Is the same action repeated more than once?"),
}
SUBQ_PHOTO = {
    "one_object": _noul("Is the main subject one object small enough for a desk robot gripper (under about 10 cm wide)?"),
    "shows_person": _noul("Is a person, a face or a body part the main subject of the photo?"),
    "fragility": SUBQ_FRAGILITY,
    "hand_for_scale": _noul("Is a credit card, coin, ruler or hand visible next to the object for scale?"),
    "food": _noul("Is the object food?"),
}


def subq_frames(frames: list, marks: dict | None) -> list:
    """Indices (0-based) of up to 4 frames for SubQ: overview, grasp, carrying, release (the marked ones when given)."""
    n = len(frames)
    if n <= 4:
        return list(range(n))
    ts = [f.get("t_s") for f in frames]

    def near(t):
        if t is None or any(x is None for x in ts):
            return None
        return min(range(n), key=lambda i: abs(ts[i] - t))
    g, r = near((marks or {}).get("grasp_s")), near((marks or {}).get("release_s"))
    c = near((ts[g] + ts[r]) / 2) if g is not None and r is not None else None
    picks = [0, g if g is not None else round((n - 1) / 3), c if c is not None else round((n - 1) / 2),
             r if r is not None else round(2 * (n - 1) / 3)]
    mid = picks[2]  # when two picks land on one frame, the next nearest to the carrying moment stands in
    spare = sorted(range(n), key=lambda i: abs(ts[i] - ts[mid]) if None not in ts else abs(i - mid))
    out = []
    for i in picks + spare:
        if i not in out:
            out.append(i)
    return sorted(out[:4])


def subq_roles(frames: list, idx: list, marks: dict | None) -> list:
    """What each chosen frame shows, as far as Morph knows: the first is the overview, the ones nearest the person's
    grasp and release marks are those; the rest are just frames (no guessing)."""
    ts = [frames[i].get("t_s") for i in idx]
    roles = ["overview"] + ["frame"] * (len(idx) - 1)
    for t, name in (((marks or {}).get("grasp_s"), "grasp"), ((marks or {}).get("release_s"), "release")):
        if t is not None and None not in ts:
            k = min(range(len(idx)), key=lambda j: abs(ts[j] - t))
            if roles[k] == "frame":
                roles[k] = name
    return roles


def _ask_subq(frames: list, idx: list, kind: str, marks=None):
    imgs, state = [], {"frames": []}
    roles = ["photo"] * len(idx) if kind == "photo" else subq_roles(frames, idx, marks)
    for i, role in zip(idx, roles):
        small = prepare_image(frames[i]["jpeg"], long_edge=SUBQ_EDGE, quality=80)
        imgs.append({"mediaType": "image/jpeg", "base64": _b64(small["jpeg"])})
        state["frames"].append({"i": i + 1, "t_s": frames[i].get("t_s"), "role": role})
    if kind == "photo":
        state["note"] = "One photo of an object a desk robot arm should pick up. Judge only what is visible."
        questions = SUBQ_PHOTO
    else:
        state["note"] = ("Frames from a phone video of a person doing a job by hand that a desk robot arm should take "
                         "over. Judge only what is visible.")
        questions = SUBQ_VIDEO
    return subq.decide(state, questions, images=imgs, timeout=SUBQ_TIMEOUT_S, retries=1, cache=False, purpose="video")


def _answer(res, name) -> dict:
    a = ((res or {}).get("answers") or {}).get(name)
    return a if isinstance(a, dict) else {}


def _p_yes(res, name):
    v = _answer(res, name).get("noul")
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _pick(res, name, min_conf=MIN_CONF):
    a = _answer(res, name)
    c = a.get("confidence")
    return a.get("choice") if isinstance(c, (int, float)) and c >= min_conf else None


def _score(res, name):
    a = _answer(res, name)
    s, c = a.get("score"), a.get("confidence")
    if not isinstance(s, (int, float)) or isinstance(s, bool):
        return None, None
    return float(s), float(c) if isinstance(c, (int, float)) else 0.0


def _second_opinion(res: dict, sub, idx: list, ms: int) -> dict:
    """Fold SubQ's answers into Claude's result: agreement keeps it, disagreement on the task, the fragility, people or
    repeats lowers that part below 0.6 (so the person is asked, with both answers as chips), and visibility, scale and
    food become notes."""
    op = {"provider": "subq", "label": "SubQ Snap (AI)", "source": sub.get("source", "subq"), "ms": ms,
          "frames": [i + 1 for i in idx], "agree": {}, "disagree": {}, "notes": []}
    t, o, sf, env, tm = (res.get(k) for k in ("task", "object", "safety", "environment", "timing"))
    sub_task = _pick(sub, "task_type")
    if t and sub_task:
        op["agree"]["task"] = sub_task == t.get("type")
        if not op["agree"]["task"]:
            op["disagree"]["task.type"] = {"claude": t.get("type"), "subq": sub_task}
            t["confidence"] = min(t.get("confidence", 0), 0.5)
    s, sc = _score(sub, "fragility")
    if o and s is not None and sc >= MIN_CONF and o.get("fragility") in FRAGILITY:
        op["agree"]["fragility"] = abs(FRAGILITY.index(o["fragility"]) - s) < 1.0
        if not op["agree"]["fragility"]:
            op["disagree"]["object.fragility"] = {"claude": o["fragility"], "subq": FRAGILITY[min(max(round(s), 0), 3)]}
            o["confidence"] = min(o.get("confidence", 0), 0.5)
    p = _p_yes(sub, "object_visible")
    if o and p is not None and p < 0.5:
        op["notes"].append("SubQ couldn't clearly see the handled object: a closer clip helps.")
        o["confidence"] = min(o.get("confidence", 0), 0.55)
    p = _p_yes(sub, "one_object")
    if o and p is not None and p < 0.5:
        op["notes"].append("SubQ isn't sure this is one object a desk gripper can hold.")
        o["confidence"] = min(o.get("confidence", 0), 0.55)
    p = _p_yes(sub, "hand_for_scale")
    if o and p is not None and p < 0.5 and o.get("scale_reference") not in (None, "none"):
        op["notes"].append("SubQ saw no hand, ruler or card next to the object, so sizes are rough.")
    p = _p_yes(sub, "other_people")
    if p is not None and p > 0.5:
        op["notes"].append("Someone besides the demonstrator seems to be in view: crop them out, or leave those frames out.")
        if sf and not sf.get("other_people_visible"):
            op["disagree"]["safety.other_people_visible"] = {"claude": 0, "subq": 1}
            sf["confidence"] = min(sf.get("confidence", 0), 0.5)
    p = _p_yes(sub, "food")
    if p is not None and p > 0.5 and env and env.get("setting") != "food":
        op["notes"].append("SubQ thinks the task touches food: printed parts aren't food-safe as printed.")
    p = _p_yes(sub, "repeats")
    if p is not None and tm:
        if p < 0.5 and tm.get("cycle_time_s") and tm.get("basis") == "frames":
            op["disagree"]["timing.cycle_time_s"] = {"claude": tm["cycle_time_s"], "subq": None}
            tm["confidence"] = min(tm.get("confidence", 0), 0.5)
        elif p > 0.5 and not tm.get("cycle_time_s"):
            op["notes"].append("The action seems to repeat: mark one grasp and one release to time it.")
    p = _p_yes(sub, "shows_person")
    if p is not None and p >= 0.5:
        res.update(usable=False, unusable_reason="This photo seems to show a person rather than an object. "
                                                 "Morph only reads photos of objects.")
    return op


def _from_subq(sub, idx: list, kind: str) -> dict:
    """A partial result from SubQ alone (no Claude key): the task type, fragility and people; the rest is for the person."""
    res = _empty(kind)
    ev = [i + 1 for i in idx]
    a = _answer(sub, "task_type")
    if kind != "photo" and a.get("choice") in TASKS:
        res["task"] = {"type": a["choice"], "summary": "", "confidence": _conf(a.get("confidence")), "evidence_frames": ev}
    p = _p_yes(sub, "other_people")
    if kind != "photo" and p is not None:
        res["safety"] = {"other_people_visible": int(p > 0.5), "hands_share_workspace": False, "hazards": [],
                         "confidence": round(max(p, 1 - p), 2), "evidence_frames": ev}
    p = _p_yes(sub, "food")
    if kind != "photo" and p is not None and p > 0.5:
        res["environment"] = {"setting": "food", "clutter": None, "confidence": round(p, 2), "evidence_frames": ev}
    s, sc = _score(sub, "fragility")
    if s is not None:
        res["object"] = {"name": "", "shape": None, "diameter_mm": None, "length_mm": None, "size_range_mm": None,
                         "scale_reference": None, "mass_g": None, "fragility": FRAGILITY[min(max(round(s), 0), 3)],
                         "surface": None, "count_per_cycle": 1, "confidence": round(min(sc, 0.5), 2), "evidence_frames": ev}
    p = _p_yes(sub, "shows_person")
    if p is not None and p >= 0.5:
        res.update(usable=False, unusable_reason="This photo seems to show a person rather than an object. "
                                                 "Morph only reads photos of objects.")
    res["notes"].append("SubQ gives a second opinion only (task type, fragility, people): fill in the rest yourself.")
    return res


# ------------------------------------------------------------------ results
def _empty(kind: str) -> dict:
    return {"kind": kind, "usable": True, "unusable_reason": None, "error": None, "frames": 0,
            **{s: None for s in SECTIONS}, "questions": [], "notes": [], "marks": None, "source": None,
            "providers": [], "second_opinion": None, "cost_usd_est": 0.0, "ms": 0, "confidence": {}, "evidence_frames": {}}


def _num(v, lo=None, hi=None, nd=1):
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    v = float(v)
    if lo is not None:
        v = max(v, lo)
    if hi is not None:
        v = min(v, hi)
    return round(v, nd)


def _enum(v, options):
    return v if isinstance(v, str) and v in options else None


def _ev(v, n: int) -> list:
    out = []
    for x in v if isinstance(v, list) else []:
        if isinstance(x, int) and not isinstance(x, bool) and 1 <= x <= max(n, 1) and x not in out:
            out.append(x)
    return out[:MAX_FRAMES]


def _conf(v) -> float:
    c = _num(v, 0.0, 1.0, 2)
    return 0.0 if c is None else c


def _point(p):
    if not isinstance(p, dict):
        return None
    x, y = _num(p.get("x_mm"), -2000, 2000, 0), _num(p.get("y_mm"), -2000, 2000, 0)
    return None if x is None or y is None else {"x_mm": x, "y_mm": y, "z_mm": _num(p.get("z_mm"), -500, 2000, 0) or 0.0}


def _object_sec(s: dict) -> dict:
    rng = s.get("size_range_mm")
    rng = [_num(x, 1, 1000) for x in rng[:2]] if isinstance(rng, list) and len(rng) >= 2 else None
    return {"name": _text(s.get("name"), 60), "shape": _enum(s.get("shape"), SHAPES),
            "diameter_mm": _num(s.get("diameter_mm"), 1, 1000), "length_mm": _num(s.get("length_mm"), 1, 2000),
            "size_range_mm": sorted(rng) if rng and None not in rng else None,
            "scale_reference": _enum(s.get("scale_reference"), SCALES), "mass_g": _num(s.get("mass_g"), 0.1, 100000),
            "fragility": _enum(s.get("fragility"), FRAGILITY), "surface": _enum(s.get("surface"), SURFACES),
            "count_per_cycle": int(_num(s.get("count_per_cycle"), 1, 1000, 0) or 1)}


_BUILD = {
    "task": lambda s: {"type": _enum(s.get("type"), TASKS), "summary": _text(s.get("summary"), 240)},
    "object": _object_sec,
    "motion": lambda s: {"pick": _point(s.get("pick")), "place": _point(s.get("place")),
                         "positions_vary": s.get("positions_vary") is True},
    "timing": lambda s: {"cycle_time_s": _num(s.get("cycle_time_s"), 0.1, 3600), "carry_s": _num(s.get("carry_s"), 0, 3600, 2),
                         "cycles_seen": int(_num(s.get("cycles_seen"), 0, 1000, 0) or 0),
                         "basis": _enum(s.get("basis"), ("frames", "marks", "user")) or "frames"},
    "environment": lambda s: {"setting": _enum(s.get("setting"), SETTINGS), "clutter": _enum(s.get("clutter"), CLUTTER)},
    "safety": lambda s: {"other_people_visible": int(_num(s.get("other_people_visible"), 0, 100, 0) or 0),
                         "hands_share_workspace": s.get("hands_share_workspace") is True,
                         "hazards": _texts(s.get("hazards"), 40, 6)},
}


def clean_result(raw, n_frames: int = MAX_FRAMES) -> dict:
    """Any result-shaped dict (Claude's after pydantic, or one a browser sends back) -> the canonical result: only known
    fields, sane types and ranges. Missing sections stay None. Never trusts a client's numbers beyond these bounds."""
    raw = raw if isinstance(raw, dict) else {}
    res = _empty(raw.get("kind") if raw.get("kind") in KINDS else "video")
    res["usable"] = raw.get("usable") is not False
    res["unusable_reason"] = _text(raw.get("unusable_reason"), 300) or None
    res["error"] = _enum(raw.get("error"), tuple(ERRORS))
    res["frames"] = int(_num(raw.get("frames"), 0, MAX_FRAMES, 0) or 0)
    n = res["frames"] or n_frames
    for name, build in _BUILD.items():
        s = raw.get(name)
        if isinstance(s, dict):
            res[name] = {**build(s), "confidence": _conf(s.get("confidence")), "evidence_frames": _ev(s.get("evidence_frames"), n)}
    res["questions"] = _texts(raw.get("questions"), 200, 3)
    res["notes"] = _texts(raw.get("notes"), 300, 8)
    res["marks"] = clean_marks(raw.get("marks"))
    op = raw.get("second_opinion")
    if isinstance(op, dict):  # only what the mapping reads back: what the two models disagreed on
        allowed = {"task.type": TASKS, "object.fragility": FRAGILITY}
        dis = {}
        for k, v in (op.get("disagree") or {}).items() if isinstance(op.get("disagree"), dict) else []:
            if k in allowed and isinstance(v, dict) and v.get("claude") in allowed[k] and v.get("subq") in allowed[k]:
                dis[k] = {"claude": v["claude"], "subq": v["subq"]}
            elif k in ("safety.other_people_visible", "timing.cycle_time_s") and isinstance(v, dict):
                dis[k] = {"claude": _num(v.get("claude")), "subq": _num(v.get("subq"))}
        res["second_opinion"] = {"provider": "subq", "label": "SubQ Snap (AI)", "disagree": dis, "notes": [],
                                 "agree": {k: bool(v) for k, v in (op.get("agree") or {}).items() if k in ("task", "fragility")}
                                 if isinstance(op.get("agree"), dict) else {}}
    res["source"] = _text(raw.get("source"), 40) or None
    res["cost_usd_est"], res["ms"] = _num(raw.get("cost_usd_est"), 0, 100, 4) or 0.0, int(_num(raw.get("ms"), 0, 1e7, 0) or 0)
    return _summarize(res)


def _summarize(res: dict) -> dict:
    res["confidence"] = {s: res[s]["confidence"] for s in SECTIONS if isinstance(res.get(s), dict)}
    res["evidence_frames"] = {s: res[s]["evidence_frames"] for s in SECTIONS if isinstance(res.get(s), dict)}
    return res


def _timing_from_marks(res: dict, frames: list, marks: dict | None):
    """The person's grasp and release marks beat any estimate: one cycle is about twice the carry (out, then back)."""
    g, r = (marks or {}).get("grasp_s"), (marks or {}).get("release_s")
    if g is None or r is None or r <= g:
        return
    ts = [f.get("t_s") for f in frames]
    ev = sorted({min(range(len(ts)), key=lambda i: abs(ts[i] - t)) + 1 for t in (g, r)}) if ts and None not in ts else []
    prev = res.get("timing") or {}
    res["timing"] = {"cycle_time_s": round(2 * (r - g), 1), "carry_s": round(r - g, 2), "cycles_seen": prev.get("cycles_seen") or 1,
                     "basis": "marks", "confidence": 0.8, "evidence_frames": ev}


def _prepare(frames: list, crop, long_edge: int, emit) -> list:
    if not isinstance(frames, list) or not frames:
        raise ImageError("Send at least one frame.")
    if len(frames) > MAX_FRAMES:
        raise ImageError(f"Send at most {MAX_FRAMES} frames.")
    out = []
    for f in frames:
        f = f if isinstance(f, dict) else {}
        data = f.get("jpeg")
        if data is None and isinstance(f.get("jpeg_b64"), str):
            try:
                data = decode_frames([f])[0]["jpeg"]
            except ValueError as exc:
                raise ImageError(str(exc)) from None
        img = prepare_image(data, long_edge=long_edge, crop=crop)
        out.append({"t_s": _num(f.get("t_s"), 0, 86400, 2), **img})
    mb = sum(len(f["jpeg"]) for f in out) / 1048576
    emit(type="video", stage="prepare", status="done", text=f"{len(out)} frame{'s' if len(out) != 1 else ''} ready ({mb:.1f} MB)")
    return out


def _content(frames: list, kind: str, marks, crop) -> list:
    content = []
    for i, f in enumerate(frames, 1):
        label = f"Frame {i}, t = {f['t_s']:.1f} s" if f.get("t_s") is not None else f"Frame {i} (a photo, no timestamp)"
        content += [{"type": "text", "text": label},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": _b64(f["jpeg"])}}]
    note = ["These are separate photos of the same job, in order."] if kind == "photos" else []
    if marks and marks.get("grasp_s") is not None:
        note.append(f"The person marked the grasp at t = {marks['grasp_s']:.1f} s.")
    if marks and marks.get("release_s") is not None:
        note.append(f"The person marked the release at t = {marks['release_s']:.1f} s.")
    if crop:
        note.append("Every frame is cropped to the same region of the original.")
    content.append({"type": "text", "text": " ".join(note + ["Describe the job in these frames."])})
    return content


def analyze_frames(frames: list, *, marks=None, crop=None, use_subq: bool = False, on_event=None, kind: str = "video"):
    """Frames [{"t_s", "jpeg" (bytes) | "jpeg_b64"}] of one job -> the estimates, or None in keyless/offline mode (nothing
    sent). Claude and the optional SubQ second opinion run in parallel within TIMEOUT_S, with progress events
    {type: video|agent, ...} for the Studio's live channel. A failure comes back as a result with usable=False and an
    `error` (timeout | refusal | cut_off | invalid | failed), so the Studio falls back to the cards. Never raises, except
    ImageError for images Morph refuses (before anything is sent)."""
    kind = kind if kind in ("video", "photos") else "video"
    emit, t0 = _emitter(on_event), time.time()
    claude_on, subq_on = claude_ready(), bool(use_subq) and subq_ready()
    if not claude_on and not subq_on:
        return None
    marks = clean_marks(marks)
    prepared = _prepare(frames, crop, FRAME_EDGE, emit)
    n = len(prepared)
    jobs, idx = {}, []
    if claude_on:
        emit(type="agent", role="vision", provider="claude", status="start", text=f"Claude is reading {n} frames")
        jobs["claude"] = _in_thread(_ask_claude, SYSTEM, _content(prepared, kind, marks, crop), VideoAnalysis)
    if subq_on:
        idx = subq_frames(prepared, marks)
        emit(type="agent", role="vision", provider="subq", status="start", text=f"SubQ second opinion on {len(idx)} frames")
        jobs["subq"] = _in_thread(_ask_subq, prepared, idx, kind, marks)
    elif use_subq:
        emit(type="agent", role="vision", provider="subq", status="skipped", text="SubQ is not set up on this computer")
    late = _wait(jobs, emit, f"Reading {n} frames", t0)
    return _combine(jobs, late, prepared, idx, kind, marks, emit, t0)


def analyze_photo(image, *, use_subq: bool = False, on_event=None, crop=None):
    """One photo of the object to pick (bytes of a JPEG, PNG or WebP, or a frame dict) -> the object card's estimates
    (the other sections stay None), or None in keyless/offline mode. Same failure handling as analyze_frames()."""
    emit, t0 = _emitter(on_event), time.time()
    claude_on, subq_on = claude_ready(), bool(use_subq) and subq_ready()
    if not claude_on and not subq_on:
        return None
    data = image.get("jpeg") if isinstance(image, dict) else image
    if data is None and isinstance(image, dict) and isinstance(image.get("jpeg_b64"), str):
        try:
            data = decode_frames([{"jpeg_b64": image["jpeg_b64"]}])[0]["jpeg"]
        except ValueError as exc:
            raise ImageError(str(exc)) from None
    img = prepare_image(data, long_edge=PHOTO_EDGE, crop=crop, max_bytes=MAX_PHOTO_BYTES)
    prepared = [{"t_s": None, **img}]
    emit(type="video", stage="prepare", status="done", text=f"Photo ready ({img['w']} x {img['h']})")
    jobs = {}
    if claude_on:
        emit(type="agent", role="vision", provider="claude", status="start", text="Claude is looking at the photo")
        content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": _b64(img["jpeg"])}},
                   {"type": "text", "text": "Describe the object in this photo."}]
        jobs["claude"] = _in_thread(_ask_claude, PHOTO_SYSTEM, content, PhotoAnalysis)
    if subq_on:
        emit(type="agent", role="vision", provider="subq", status="start", text="SubQ second opinion on the photo")
        jobs["subq"] = _in_thread(_ask_subq, prepared, [0], "photo")
    elif use_subq:
        emit(type="agent", role="vision", provider="subq", status="skipped", text="SubQ is not set up on this computer")
    late = _wait(jobs, emit, "Reading the photo", t0)
    return _combine(jobs, late, prepared, [0], "photo", None, emit, t0)


def _subq_outcome(jobs: dict, late: list, t0: float, emit):
    """(SubQ's answers or None, ms, provider row, cost) for the SubQ job, reporting how it went."""
    got = None if "subq" in late else jobs["subq"][1].get("value")
    ok = isinstance(got, dict) and isinstance(got.get("answers"), dict)
    ms = int(_num(got.get("ms"), 0, 1e9, 0) or 0) if ok else round((time.time() - t0) * 1000)
    cost = float(_num((got.get("usage") or {}).get("input_tokens"), 0, 1e9, 0) or 0) * SUBQ_PRICE_IN if ok else 0.0
    text = "Second opinion ready" if ok else "SubQ didn't answer in time" if "subq" in late else "SubQ gave no answer"
    emit(type="agent", role="vision", provider="subq", status="done" if ok else "error", ms=ms, text=text)
    return (got if ok else None), ms, {"provider": "subq", "status": "done" if ok else "error", "ms": ms}, cost


def _claude_outcome(jobs: dict, late: list, prepared: list, t0: float, emit):
    """(parsed model or None, error code or None, exception or None, provider row, cost) for the Claude job."""
    box, ms = jobs["claude"][1], round((time.time() - t0) * 1000)
    parsed, err, exc, usage = None, None, None, None
    if "claude" in late:
        err = "timeout"
    elif "error" in box:
        exc = box["error"]
        err = "timeout" if "Timeout" in exc.__class__.__name__ else "failed"
    else:
        parsed, err, usage = box["value"]
    cost = _usage_cost(usage)
    if cost is None:  # no usage came back: count the images as sent when it timed out (it may still be billed)
        cost = (sum(image_tokens(f["w"], f["h"]) for f in prepared) + 1500) * PRICE_IN if err == "timeout" else 0.0
    n = len(prepared)
    if parsed is not None:
        emit(type="agent", role="vision", provider="claude", status="done", ms=ms,
             text=f"Estimated from {n} frame{'s' if n != 1 else ''} (about ${cost:.2f})")
    else:
        emit(type="agent", role="vision", provider="claude", status="error", ms=ms, text=_claude_error_text(err, exc))
    row = {"provider": "claude", "status": "done" if parsed is not None else "error", "ms": ms, "error": err}
    return parsed, err, exc, row, cost


def _combine(jobs: dict, late: list, prepared: list, idx: list, kind: str, marks, emit, t0: float) -> dict:
    """Both providers' outcomes -> one result: Claude's estimates, folded with SubQ's second opinion; SubQ alone when
    Claude isn't set up or failed; an unusable result with the reason when nothing came back."""
    n, cost, providers = len(prepared), 0.0, []
    parsed = err = exc = sub = None
    if "claude" in jobs:
        parsed, err, exc, row, c = _claude_outcome(jobs, late, prepared, t0, emit)
        providers.append(row)
        cost += c
    if "subq" in jobs:
        sub, sub_ms, row, c = _subq_outcome(jobs, late, t0, emit)
        providers.append(row)
        cost += c
    if parsed is not None:
        res = clean_result({**parsed.model_dump(), "kind": kind, "frames": n}, n)
        if res["usable"] and kind != "photo":
            _timing_from_marks(res, prepared, marks)  # before the second opinion: marks are the person's own timing
        if sub is not None and res["usable"]:
            res["second_opinion"] = _second_opinion(res, sub, idx, sub_ms)
            res["notes"] += res["second_opinion"]["notes"]
    elif sub is not None:
        res = _from_subq(sub, idx, kind)
        if err:
            res["notes"].insert(0, _claude_error_text(err, exc))
        if res["usable"] and kind != "photo":
            _timing_from_marks(res, prepared, marks)
    else:
        res = _empty(kind)
        why = _claude_error_text(err or "failed", exc) if "claude" in jobs else \
            "SubQ gave no answer, so nothing was estimated. Fill in the cards yourself, or try again."
        res.update(usable=False, error=err or "failed", unusable_reason=why)
    res.update(frames=n, providers=providers, marks=marks, ms=round((time.time() - t0) * 1000), cost_usd_est=round(cost, 4))
    res["source"] = "+".join(p["provider"] for p in providers if p["status"] == "done") or None
    _summarize(res)
    emit(type="video", stage="done", status="done" if res["usable"] else "error",
         text=res.get("unusable_reason") or "Done: check the estimates, then apply what fits.", result=res)
    return res


# ------------------------------------------------------------------ estimates -> design changes
LABELS = {"object.name": "Object", "object.shape": "Object shape", "object.diameter_mm": "Object width",
          "object.length_mm": "Object length", "object.mass_g": "Object mass", "object.friction": "Surface friction",
          "object.max_force_n": "Damage force", "finger.length_mm": "Finger length", "finger.pad_height_mm": "Pad height",
          "finger.pad_depth_mm": "Pad depth", "task.pick_x_mm": "Pick x", "task.pick_y_mm": "Pick y",
          "task.place_x_mm": "Place x", "task.place_y_mm": "Place y", "task.cycle_time_s": "Cycle time",
          "arm.reach_mm": "Reach", "electronics.extras": "Extras"}
# the keys video and photo estimates may change, within the ranges the generator accepts (PARAMS where it has them)
BOUNDS = {"object.diameter_mm": (5.0, 300.0), "object.length_mm": (5.0, 500.0), "object.mass_g": (1.0, 1500.0),
          "object.friction": (0.1, 1.2), "object.max_force_n": (0.0, 100.0), "finger.length_mm": (30.0, 120.0),
          "finger.pad_height_mm": (10.0, 60.0), "finger.pad_depth_mm": (2.0, 15.0), "task.pick_x_mm": (80.0, 400.0),
          "task.pick_y_mm": (-300.0, 300.0), "task.place_x_mm": (80.0, 400.0), "task.place_y_mm": (-300.0, 300.0),
          "task.cycle_time_s": (4.0, 60.0), "arm.reach_mm": (200.0, 500.0)}
ENUMS = {"object.shape": ("cylinder", "box", "sphere")}
EXTRAS = ("imu", "wrist_camera", "estop", "wifi")  # the parameter editor's order
KEY_ORDER = tuple(LABELS)  # how the list of changes reads: object first, then the fingers, layout, timing, arm, extras
KEYS = frozenset(BOUNDS) | frozenset(ENUMS) | {"object.name", "electronics.extras"}
ANSWERS = {  # result fields a person may set or confirm: path -> allowed values or (min, max)
    "task.type": TASKS, "object.name": str, "object.shape": SHAPES[:3], "object.diameter_mm": (1.0, 300.0),
    "object.length_mm": (1.0, 500.0), "object.mass_g": (0.1, 1500.0), "object.fragility": FRAGILITY,
    "object.surface": SURFACES, "motion.pick.x_mm": (-2000.0, 2000.0), "motion.pick.y_mm": (-2000.0, 2000.0),
    "motion.place.x_mm": (-2000.0, 2000.0), "motion.place.y_mm": (-2000.0, 2000.0), "motion.positions_vary": bool,
    "timing.cycle_time_s": (0.5, 600.0), "environment.setting": SETTINGS, "safety.hands_share_workspace": bool,
    "safety.other_people_visible": (0.0, 100.0),
}
FROM = {"object.name": "object.name", "object.shape": "object.shape", "object.diameter_mm": "object.diameter_mm",
        "object.length_mm": "object.length_mm", "object.mass_g": "object.mass_g", "object.friction": "object.surface",
        "object.max_force_n": "object.fragility", "task.pick_x_mm": "motion.pick.x_mm", "task.pick_y_mm": "motion.pick.y_mm",
        "task.place_x_mm": "motion.place.x_mm", "task.place_y_mm": "motion.place.y_mm",
        "task.cycle_time_s": "timing.cycle_time_s"}


def _flat_design(design) -> dict:
    if design is None:
        return {}
    if hasattr(design, "flat"):
        return design.flat()
    if isinstance(design, dict) and isinstance(design.get("object"), dict):
        from ..spec import Design
        try:
            return Design.from_dict(design).flat()
        except (TypeError, ValueError):
            return {}
    return dict(design) if isinstance(design, dict) else {}


def preset_for(name) -> str | None:
    """The intent.OBJECTS preset a free-text object name means ("eggs", "Raw egg", "a soda can"), or None."""
    t = str(name or "").lower().strip()
    if not t:
        return None
    if t in OBJECTS:
        return t
    for alias, key in ALIASES.items():
        if re.search(rf"\b{re.escape(alias)}", t) and not (alias == "can" and re.search(r"\bcan\s+(it|i|you|we|be)\b", t)):
            return key
    return None


def _clean_name(name) -> str:
    return re.sub(r"[^\w .,'()/+-]", "", str(name or ""))[:60].strip()


def _set_path(d: dict, path: str, value):
    parts = path.split(".")
    for p in parts[:-1]:
        if not isinstance(d.get(p), dict):
            d[p] = {}
        d = d[p]
    d[parts[-1]] = value


def _answer_value(path: str, v):
    rule = ANSWERS[path]
    if rule is str:
        s = _clean_name(v)
        return s or None, bool(s)
    if rule is bool:
        return (v, True) if isinstance(v, bool) else (None, False)
    if isinstance(rule[0], str):
        return (v, True) if v in rule else (None, False)
    n = _num(v, nd=2)
    return (None, False) if n is None else (min(max(n, rule[0]), rule[1]), True)


def apply_answers(result, answers) -> tuple:
    """(result with the person's values filled in, confirmed paths, kept paths, notes about refused answers).
    An answer is a value, "@accept" (use the estimate as it is) or "@keep" (leave the design as it is); a section name
    ("object", "motion", ...) takes "@accept" or "@keep" for the whole card."""
    res = copy.deepcopy(result) if isinstance(result, dict) else _empty("video")
    confirmed, kept, notes = set(), set(), []
    for path, v in (answers.items() if isinstance(answers, dict) else ()):
        if not isinstance(path, str) or (path not in ANSWERS and path not in SECTIONS):
            notes.append(f"ignored an answer for {str(path)[:40]}")
            continue
        if v in ("@keep", "@accept"):
            (kept if v == "@keep" else confirmed).add(path)
            continue
        if path in SECTIONS:
            notes.append(f"ignored an answer for {path}")
            continue
        val, ok = _answer_value(path, v)
        if not ok:
            notes.append(f"ignored {path} = {str(v)[:40]}")
            continue
        sec = path.split(".")[0]
        if not isinstance(res.get(sec), dict):
            res[sec] = {"confidence": 0.0, "evidence_frames": []}
        _set_path(res, path, val)
        if path.startswith("timing."):
            res[sec]["basis"] = "user"
        confirmed.add(path)
    return res, confirmed, kept, notes


class _Mapping:
    """One map_to_changes() call: the result, what the person confirmed or kept, and what it has decided so far."""

    def __init__(self, res: dict, flat: dict, confirmed: set, kept: set, notes: list, valued=()):
        self.res, self.flat, self.confirmed, self.kept, self.notes = res, flat, confirmed, kept, notes
        self.valued = set(valued)  # paths the person typed or picked a value for (not just "@accept")
        self.dis = ((res.get("second_opinion") or {}).get("disagree") or {})
        self.src = "photo" if res.get("kind") == "photo" else "video"
        self.changes, self.why, self.srcs, self.questions = {}, {}, {}, []

    def conf(self, sec: str) -> float:
        return (self.res.get(sec) or {}).get("confidence", 0.0)

    def is_kept(self, path: str) -> bool:
        return path in self.kept or path.split(".")[0] in self.kept

    def section_ok(self, sec: str) -> bool:
        s = self.res.get(sec)
        return isinstance(s, dict) and not self.is_kept(sec) and (sec in self.confirmed or s.get("confidence", 0) >= MIN_CONF)

    def sure(self, path: str) -> bool:
        """The person confirmed it, or its card is at 0.6 confidence or more and the two models don't disagree on it."""
        if self.is_kept(path):
            return False
        if path in self.confirmed or path.split(".")[0] in self.confirmed:
            return True
        return self.section_ok(path.split(".")[0]) and path not in self.dis

    def put(self, key: str, value, why: str, source: str | None = None):
        self.changes[key], self.why[key] = value, why
        self.srcs[key] = source or ("user" if FROM.get(key) in self.confirmed else self.src)

    def ask(self, field: str, text: str, chips: list, why: str):
        if len(self.questions) < 6 and not any(q["id"] == field for q in self.questions):
            self.questions.append({"id": field, "field": field, "text": text, "chips": chips, "why": why})

    def low(self, sec: str) -> str:
        return f"{sec} confidence {self.conf(sec):.2f}"


def map_to_changes(result, design=None, answers=None) -> dict:
    """Estimates (plus the person's answers and card edits) -> {"changes": {dotted key: value}, "items": [{key, label,
    old, new, why, source}], "notes": [...], "questions": [{id, field, text, chips: [{label, value}], why}], "slots"}.

    Morph's rules, not a model, decide every value: object presets win (an egg is 25 N, friction 0.35), fragility sets
    the damage force of other objects, the surface sets friction, pick and place spots are clamped to the table and the
    reach to 200-500 mm (with a note), people within reach add an emergency stop and objects in varying spots a wrist
    camera. A part under 0.6 confidence, or one Claude and SubQ disagree on, is never changed: it becomes a question (at
    most 3 at a time), answered with a chip or by editing its card. `items` lists exactly what Apply would change."""
    res, confirmed, kept, notes = apply_answers(clean_result(result) if result else _empty("video"), answers)
    tm = res.get("timing")
    if res.get("marks") and res.get("kind") != "photo" and "timing.cycle_time_s" not in confirmed \
            and (not isinstance(tm, dict) or tm.get("basis") == "frames"):
        _timing_from_marks(res, [], res["marks"])  # cards filled in by hand still get the marked timing
    valued = {p for p, v in (answers.items() if isinstance(answers, dict) else ()) if p in confirmed and v != "@accept"}
    m = _Mapping(res, _flat_design(design), confirmed, kept, notes, valued)
    if not res.get("usable", True):
        notes.insert(0, res.get("unusable_reason") or "Nothing usable was seen.")
        return _finish_mapping(m)
    o = res.get("object") or {}
    for path, d in m.dis.items():  # disagreements first: the person picks between the two answers
        if path in confirmed or m.is_kept(path):
            continue
        if path == "task.type":
            m.ask(path, "Claude and SubQ disagree: what is the person doing?",
                  [{"label": TASK_LABEL[d["claude"]].capitalize(), "value": d["claude"]},
                   {"label": TASK_LABEL[d["subq"]].capitalize(), "value": d["subq"]}], "second opinion")
        elif path == "object.fragility" and not preset_for(o.get("name")):
            m.ask(path, "Claude and SubQ disagree: how easily does it break?",
                  [{"label": FRAGILITY_LABEL[d["claude"]].capitalize(), "value": d["claude"]},
                   {"label": FRAGILITY_LABEL[d["subq"]].capitalize(), "value": d["subq"]}], "second opinion")
    _map_object(m)
    _map_motion(m)
    _map_timing(m)
    _map_safety(m)
    _map_task_and_setting(m)
    return _finish_mapping(m)


def _r5(v: float) -> float:
    return float(max(5, round(v / 5) * 5))


def _map_object(m: _Mapping):
    o = m.res.get("object")
    if not isinstance(o, dict) or m.is_kept("object"):
        return
    name, low = o.get("name") or "", m.low("object")
    label, preset = name or "object", preset_for(name)
    if preset:
        if m.sure("object.name"):
            for k, v in object_changes(preset).items():
                m.put(k, v, f"Morph's {preset} preset", "preset")
            _user_overrides(m, o)  # the preset beats the model's guesses, never what the person typed
            frag = o.get("fragility")
            if frag and DAMAGE_N[frag] and not OBJECTS[preset]["max_force_n"] and "object.fragility" not in m.valued:
                m.notes.append(f"The video suggests a {FRAGILITY_LABEL[frag]} object; the {preset} preset treats it as "
                               "robust. Edit the damage force if yours breaks.")
        elif not m.is_kept("object.name"):
            m.ask("object.name", f"Is it a {preset}?",
                  [{"label": f"Yes, a {preset}", "value": preset}, {"label": "No: I'll edit the card", "value": "@keep"}], low)
        return
    # anything else: each value needs its own confidence, or the person's answer
    if name and m.sure("object.name"):
        m.put("object.name", _clean_name(name) or "object", "seen in the video" if "object.name" not in m.confirmed else "you entered it")
    elif name and not m.is_kept("object.name"):
        m.ask("object.name", f"Is it a {name}?", [{"label": "Yes", "value": "@accept"},
                                                  {"label": "No: I'll edit the card", "value": "@keep"}], low)
    shape = o.get("shape")
    if shape and m.sure("object.shape"):
        if shape == "irregular":
            d, ln = o.get("diameter_mm"), o.get("length_mm")
            shape = "cylinder" if d and ln and ln / d > 1.4 else "sphere"
            m.notes.append(f"The {label} is irregular: modelled as a {shape} (Morph's grippers model cylinders, boxes "
                           "and spheres).")
        m.put("object.shape", shape, "seen in the video")
    dia, rng = o.get("diameter_mm"), o.get("size_range_mm")
    if dia is None and rng:
        dia = round(sum(rng) / 2, 1)
    if dia is not None and m.sure("object.diameter_mm"):
        ref = o.get("scale_reference")
        basis = ("you entered it" if "object.diameter_mm" in m.confirmed else
                 f"measured against a {ref.replace('_', ' ')}" if ref and ref != "none" else "estimated without a size reference")
        m.put("object.diameter_mm", dia, basis)
        for k, v in fit_finger({"diameter_mm": dia}).items():
            m.put(k, v, f"fingers fitted to a {dia:g} mm object", "rules")
        if not 26 <= dia <= 75:
            m.notes.append(f"A {dia:g} mm object is outside the gripper's usual 26 to 75 mm range.")
        ln = o.get("length_mm") if o.get("length_mm") is not None else (dia if o.get("shape") == "sphere" else None)
        if ln is not None and m.sure("object.length_mm"):
            m.put("object.length_mm", ln, "seen in the video" if "object.length_mm" not in m.confirmed else "you entered it")
    elif dia is not None and not m.is_kept("object.diameter_mm"):
        lo, hi = (rng or [dia * 0.8, dia * 1.2])[:2]
        vals = sorted({_r5(lo), _r5((lo + hi) / 2), _r5(hi)})
        m.ask("object.diameter_mm", f"How wide is the {label} where the gripper holds it?",
              [{"label": f"{v:g} mm", "value": v} for v in vals] + [{"label": "Measure it on a frame", "value": "@measure"}],
              "no size reference in view" if o.get("scale_reference") in (None, "none") else low)
    mass = o.get("mass_g")
    if mass is not None and m.sure("object.mass_g"):
        m.put("object.mass_g", mass, "estimated from the video" if "object.mass_g" not in m.confirmed else "you entered it")
    elif not m.is_kept("object.mass_g") and (name or dia is not None):
        m.ask("object.mass_g", f"How heavy is one {label}?", [{"label": lab, "value": v} for v, lab in MASS_CHIPS],
              "a video can't show weight" if mass is None else low)
    frag = o.get("fragility")
    if frag and m.sure("object.fragility"):
        m.put("object.max_force_n", DAMAGE_N[frag], f"a {FRAGILITY_LABEL[frag]} object")
    elif frag and not m.is_kept("object.fragility"):
        m.ask("object.fragility", f"How easily does the {label} break?",
              [{"label": FRAGILITY_LABEL[f].capitalize(), "value": f} for f in FRAGILITY], low)
    surf = o.get("surface")
    if surf and m.sure("object.surface"):
        m.put("object.friction", 0.35 if surf in ("smooth", "wet") else 0.6, f"a {surf} surface")
    if (o.get("count_per_cycle") or 1) > 1 and m.section_ok("object"):
        k = o["count_per_cycle"]
        m.notes.append(f"The person moves {k} at a time; the gripper picks one, so expect {k} times as many cycles.")


def _user_overrides(m: _Mapping, o: dict):
    """Values the person entered on the object card, over a preset."""
    for f in ("mass_g", "length_mm", "shape"):
        if f"object.{f}" in m.valued and o.get(f) not in (None, "irregular"):
            m.put(f"object.{f}", o[f], "you entered it", "user")
    if "object.diameter_mm" in m.valued and o.get("diameter_mm") is not None:
        m.put("object.diameter_mm", o["diameter_mm"], "you entered it", "user")
        for k, v in fit_finger({"diameter_mm": o["diameter_mm"]}).items():
            m.put(k, v, f"fingers fitted to a {o['diameter_mm']:g} mm object", "rules")
    if "object.fragility" in m.valued and o.get("fragility") in DAMAGE_N:
        m.put("object.max_force_n", DAMAGE_N[o["fragility"]], f"you said {FRAGILITY_LABEL[o['fragility']]}", "user")
    if "object.surface" in m.valued and o.get("surface") in SURFACES:
        m.put("object.friction", 0.35 if o["surface"] in ("smooth", "wet") else 0.6, f"you said {o['surface']}", "user")


def _map_motion(m: _Mapping):
    mo = m.res.get("motion")
    if not isinstance(mo, dict) or m.is_kept("motion"):
        return
    pick, place = mo.get("pick"), mo.get("place")
    coords = [f"motion.{a}.{c}" for a in ("pick", "place") for c in ("x_mm", "y_mm")]
    if pick and place and (m.sure("motion") or all(p in m.confirmed for p in coords)):
        far = 0.0
        for pt, name in ((pick, "pick"), (place, "place")):
            for c in ("x", "y"):
                key, v = f"task.{name}_{c}_mm", float(pt[f"{c}_mm"])
                lo, hi = BOUNDS[key]
                if not lo <= v <= hi:
                    m.notes.append(f"{name.capitalize()} {c} = {v:g} mm is off the table area ({lo:g} to {hi:g} mm): "
                                   f"moved to {min(max(v, lo), hi):g} mm.")
                m.put(key, min(max(v, lo), hi), "from the video (approximate)" if FROM[key] not in m.confirmed else "you placed it")
            far = max(far, math.hypot(pt["x_mm"], pt["y_mm"]))
            if (pt.get("z_mm") or 0) > 30:
                m.notes.append(f"The {name} spot is {pt['z_mm']:g} mm above the table; Morph places on the table plane only, "
                               "so the simulation uses table height.")
        need = math.ceil(far * 1.15 / 10.0) * 10.0  # some margin past the farthest spot; sizing adds the hand and hover
        if need > float(m.flat.get("arm.reach_mm") or 320.0):
            m.put("arm.reach_mm", min(max(need, 200.0), 500.0), f"spots up to {far:.0f} mm from the base", "rules")
            if need > 500:
                m.notes.append(f"This layout needs about {need:.0f} mm of reach; the reach setting stops at 500 mm (sizing "
                               "still lengthens the links for top-down picks). Move the spots closer if the arm comes out too big.")
    elif pick and place:
        m.ask("motion", "Use the pick and place spots from the video? They are rough estimates.",
              [{"label": "Use them", "value": "@accept"}, {"label": "Keep the current spots", "value": "@keep"}], m.low("motion"))
    if mo.get("positions_vary") and m.sure("motion.positions_vary"):
        m.notes.append("Objects come from different spots, so a wrist camera is added. Morph wires it but writes no vision "
                       "code: the firmware is joint-level only.")
        m.put("electronics.extras", _extras(m, "wrist_camera"), "objects are picked from varying spots", "rules")
    elif mo.get("positions_vary") and not m.is_kept("motion.positions_vary"):
        m.ask("motion.positions_vary", "Are objects always picked from the same spot?",
              [{"label": "Same spot each time", "value": False}, {"label": "It varies: add a wrist camera", "value": True}],
              m.low("motion"))


def _map_timing(m: _Mapping):
    tm = m.res.get("timing")
    if not isinstance(tm, dict) or tm.get("cycle_time_s") is None or m.is_kept("timing"):
        return
    t = float(tm["cycle_time_s"])
    lo, hi = BOUNDS["task.cycle_time_s"]
    if m.sure("timing.cycle_time_s"):
        if t < lo:
            m.notes.append(f"The person takes about {t:g} s per cycle; one pick and place takes at least about {lo:g} s "
                           f"on these arms, so the target is {lo:g} s.")
        elif t > hi:
            m.notes.append(f"A {t:g} s cycle is longer than the {hi:g} s the simulation plans for; using {hi:g} s.")
        basis = {"marks": "from your grasp and release marks", "user": "you entered it"}.get(tm.get("basis"),
                                                                                           "from repeated cycles in the video")
        m.put("task.cycle_time_s", round(min(max(t, lo), hi), 1), basis, "marks" if tm.get("basis") == "marks" else None)
    elif not m.is_kept("timing.cycle_time_s"):
        vals = sorted({round(min(max(t, lo), hi), 1), 6.0, 10.0})
        m.ask("timing.cycle_time_s", "How long does one pick and place take?",
              [{"label": f"About {v:g} s", "value": v} for v in vals] + [{"label": "Keep the current time", "value": "@keep"}],
              m.low("timing"))


def _map_safety(m: _Mapping):
    sf = m.res.get("safety")
    if not isinstance(sf, dict) or m.is_kept("safety"):
        return
    if "safety.hands_share_workspace" in m.confirmed:  # the person's answer wins over what was seen
        people = bool(sf.get("hands_share_workspace"))
    else:
        people = (sf.get("other_people_visible") or 0) > 0 or sf.get("hands_share_workspace") is True \
            or "safety.other_people_visible" in m.dis
    if people and (m.sure("safety.hands_share_workspace") or m.sure("safety.other_people_visible")):
        m.notes.append("People work within the robot's reach, so an emergency stop is added.")
        m.put("electronics.extras", _extras(m, "estop"), "people work within reach", "rules")
    elif people and not m.is_kept("safety.hands_share_workspace"):
        m.ask("safety.hands_share_workspace", "Will people work within reach of the robot?",
              [{"label": "Yes: add an emergency stop", "value": True}, {"label": "No", "value": False}], m.low("safety"))
    hz = [h for h in sf.get("hazards") or [] if h]
    if hz and m.section_ok("safety"):
        m.notes.append("Hazards seen: " + ", ".join(hz) + ".")


def _map_task_and_setting(m: _Mapping):
    t, env = m.res.get("task") or {}, m.res.get("environment") or {}
    if t.get("type") in ("pour", "other") and m.sure("task.type"):
        m.notes.append(f"Morph's arms do top-down pick and place; {TASK_LABEL[t['type']]} isn't something they do. "
                       "The design keeps a pick and place of the object.")
    elif t.get("type") in ("sort", "stack", "insert") and m.sure("task.type"):
        m.notes.append(f"Simulated as a pick and place: Morph doesn't model the {TASK_LABEL[t['type']]} itself.")
    elif t.get("type") and not m.sure("task.type") and not m.is_kept("task.type") and "task.type" not in m.dis:
        m.ask("task.type", "What is the person doing?",
              [{"label": TASK_LABEL[k].capitalize(), "value": k} for k in ("pick_place", "sort", "stack", "insert")], m.low("task"))
    if env.get("setting") == "food" and m.sure("environment.setting"):
        m.notes.append("Food contact: printed parts aren't food-safe as printed; use food-safe pads or gloves.")
    if env.get("clutter") == "high" and m.section_ok("environment"):
        m.notes.append("A cluttered workspace: clear the pick and place spots before the robot runs.")


def _extras(m: _Mapping, add: str) -> str:
    cur = [e.strip() for e in str(m.changes.get("electronics.extras", m.flat.get("electronics.extras") or "")).split(",") if e.strip()]
    if add not in cur:
        cur.append(add)
    return ",".join([e for e in EXTRAS if e in cur] + [e for e in cur if e not in EXTRAS])


def _clamp(changes: dict) -> tuple:
    """Only the keys video estimates may set, in the generator's ranges; then capability.clamp() too when the envelope
    module is there."""
    out, notes = {}, []
    for k, v in changes.items():
        if k not in KEYS:
            notes.append(f"ignored {str(k)[:40]}: a video can't change it")
        elif k in ENUMS:
            if v in ENUMS[k]:
                out[k] = v
            else:
                notes.append(f"ignored {k} = {str(v)[:30]}")
        elif k == "object.name":
            s = _clean_name(v)
            if s:
                out[k] = s
        elif k == "electronics.extras":
            out[k] = ",".join(t for t in dict.fromkeys(x.strip() for x in str(v or "").split(",")) if t in EXTRAS)
        else:
            if isinstance(v, str) and re.fullmatch(r"\s*-?\d+(\.\d+)?\s*", v):
                v = float(v)
            n = _num(v, nd=3)
            if n is None:
                notes.append(f"ignored {k} = {str(v)[:30]}")
            else:
                out[k] = min(max(n, BOUNDS[k][0]), BOUNDS[k][1])
    try:
        from . import capability
        out2, n2 = capability.clamp(out)
        return out2, notes + [n for n in n2 if not n.startswith("ignored")]
    except Exception:  # noqa: BLE001  (no envelope module yet, or it refused: the bounds above already hold)
        return out, notes


def validate_changes(changes) -> tuple:
    """Changes a browser sends to /api/video/apply -> (the ones Morph accepts, clamped; notes). Raises ValueError when
    nothing usable is left."""
    if not isinstance(changes, dict) or not changes:
        raise ValueError("Send the changes to apply as {parameter: value}.")
    if len(changes) > 40:
        raise ValueError("Too many changes in one request.")
    out, notes = _clamp({k: v for k, v in changes.items() if isinstance(k, str)})
    if not out:
        raise ValueError(("None of those changes can come from a video: " + "; ".join(notes[:3])) if notes else "Nothing to apply.")
    return out, notes


def _nest(flat: dict) -> dict:
    out = {}
    for k, v in flat.items():
        _set_path(out, k, v)
    return out


def _finish_mapping(m: _Mapping) -> dict:
    clean, cnotes = _clamp(m.changes)
    notes = m.notes + cnotes
    if clean and m.flat:
        try:  # what the envelope says about the result (a payload above what this arm lifts, ...): notes, no changes
            from . import capability
            from ..spec import Design
            chk = capability.check(Design.from_dict(_nest(m.flat)).with_changes(clean))
            notes += chk.get("notes") or []
        except Exception:  # noqa: BLE001  (no envelope module yet, or a design it can't read)
            pass
    items = []
    for k, v in sorted(clean.items(), key=lambda kv: KEY_ORDER.index(kv[0]) if kv[0] in KEY_ORDER else len(KEY_ORDER)):
        old = m.flat.get(k) if m.flat else None
        same = old == v or (isinstance(old, (int, float)) and isinstance(v, (int, float)) and abs(float(old) - float(v)) < 1e-9)
        if m.flat and same:
            continue
        items.append({"key": k, "label": LABELS.get(k, k), "old": old, "new": v, "why": m.why.get(k, ""),
                      "source": m.srcs.get(k, m.src)})
    asked = m.questions[:3]
    model_qs = [{"id": f"claude.{i}", "field": None, "text": q, "chips": [], "why": "Claude asks"}
                for i, q in enumerate(m.res.get("questions") or [])]
    return {"changes": {i["key"]: i["new"] for i in items}, "items": items,
            "notes": list(dict.fromkeys(n for n in notes if n)), "questions": (asked + model_qs)[:3],
            "slots": to_slots(m.res, confirmed=m.confirmed, kept=m.kept)}


# ------------------------------------------------------------------ estimates -> brief slots (a new robot, no project open)
ENV_SLOT = {"food": "kitchen", "lab": "lab", "office": "office", "workshop": "workshop", "warehouse": "workshop", "home": "home"}
FRAGILITY_SLOT = {"robust": "robust", "firm": "robust", "delicate": "care", "very_delicate": "fragile"}


def to_slots(result, *, confirmed=frozenset(), kept=frozenset(), answers=None) -> dict:
    """Canonical brief slots (RELEASE_CONTRACTS section 7) with source video or photo, for the brief engine when no
    project is open: {name: {value, source, confidence, evidence}}. Only parts at 0.6 confidence or more, or confirmed
    by the person, are included, so the brief engine still asks about the rest."""
    if answers is not None:
        result, confirmed, kept, _ = apply_answers(clean_result(result), answers)
    m = _Mapping(result if isinstance(result, dict) else _empty("video"), {}, set(confirmed), set(kept), [])
    res, slots = m.res, {}

    def slot(name, value, sec, *paths):
        ev = (res.get(sec) or {}).get("evidence_frames") or []
        mine = sec in m.confirmed or any(p in m.confirmed for p in paths)
        slots[name] = {"value": value, "source": m.src, "confidence": 1.0 if mine else m.conf(sec),
                       "evidence": ("frames " + ", ".join(map(str, ev))) if ev else m.src}
    if not res.get("usable", True):
        return slots
    t = res.get("task") or {}
    if t.get("type") in ("pick_place", "sort", "stack", "insert") and m.sure("task.type"):
        slot("task", "sort" if t["type"] == "sort" else "pick_place", "task", "task.type")
    o = res.get("object") or {}
    preset = preset_for(o.get("name"))
    if o and preset and m.sure("object.name"):
        slot("object", {"key": preset, "name": preset, **OBJECTS[preset]}, "object", "object.name")
    elif o and o.get("name") and o.get("diameter_mm") and m.sure("object.name") and m.sure("object.diameter_mm"):
        slot("object", {"key": None, "name": _clean_name(o["name"]), "shape": o.get("shape") if o.get("shape") in SHAPES[:3] else "cylinder",
                        "diameter_mm": o["diameter_mm"], "length_mm": o.get("length_mm") or o["diameter_mm"],
                        "mass_g": o.get("mass_g") if m.sure("object.mass_g") else None,
                        "friction": 0.35 if o.get("surface") in ("smooth", "wet") else 0.6,
                        "max_force_n": DAMAGE_N.get(o.get("fragility"), 0.0)}, "object", "object.name", "object.diameter_mm")
    if o.get("fragility") and not preset and m.sure("object.fragility"):
        slot("fragility", FRAGILITY_SLOT[o["fragility"]], "object", "object.fragility")
    mo = res.get("motion") or {}
    coords = [f"motion.{a}.{c}" for a in ("pick", "place") for c in ("x_mm", "y_mm")]
    if mo.get("pick") and mo.get("place") and (m.sure("motion") or all(p in m.confirmed for p in coords)):
        slot("layout", {"pick": [mo["pick"]["x_mm"], mo["pick"]["y_mm"]], "place": [mo["place"]["x_mm"], mo["place"]["y_mm"]]},
             "motion", *coords)
    if mo.get("positions_vary") and m.sure("motion.positions_vary"):
        slot("perception", "wrist_camera", "motion", "motion.positions_vary")
    tm = res.get("timing") or {}
    if tm.get("cycle_time_s") and m.sure("timing.cycle_time_s"):
        slot("throughput", float(min(max(tm["cycle_time_s"], 4.0), 60.0)), "timing", "timing.cycle_time_s")
    env = res.get("environment") or {}
    if ENV_SLOT.get(env.get("setting")) and m.sure("environment.setting"):
        slot("environment", ENV_SLOT[env["setting"]], "environment", "environment.setting")
    sf = res.get("safety") or {}
    if sf and m.sure("safety.hands_share_workspace"):
        near = (sf.get("other_people_visible") or 0) > 0 or sf.get("hands_share_workspace") is True
        slot("people_nearby", "yes" if near else "no", "safety", "safety.hands_share_workspace")
    return slots


UNITS = {"_mm": " mm", "_g": " g", "_n": " N", "_s": " s"}


def _fmt(key: str, v) -> str:
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return str(v) if v != "" else "none"
    unit = next((u for suf, u in UNITS.items() if key.endswith(suf)), "")
    return f"{v:g}{unit}"


def describe(mapping: dict) -> str:
    """One line for the chat after Apply: what changed (the fingers follow the object, so they are left out)."""
    ch = {it["key"]: it["new"] for it in mapping.get("items") or []}
    parts = []
    if "object.name" in ch:
        parts.append(f"object {ch['object.name']}")
    pick = [ch.get(k) for k in ("task.pick_x_mm", "task.pick_y_mm")]
    place = [ch.get(k) for k in ("task.place_x_mm", "task.place_y_mm")]
    if None not in pick or None not in place:
        parts.append(", ".join(f"{n} ({p[0]:g}, {p[1]:g}) mm" for n, p in (("pick", pick), ("place", place)) if None not in p))
    for k in KEY_ORDER:
        if k in ch and k != "object.name" and not k.startswith(("finger.", "task.pick_", "task.place_")):
            parts.append(f"{LABELS.get(k, k).lower()} {_fmt(k, ch[k])}")
    return ("From the video: " + "; ".join(parts[:9]) + ".") if parts else "From the video: nothing to change."
