"""Checks for everything a request, a design or a robot bundle can name: project names, robot sources,
file paths, request bodies and parameter edits.

Paths are compared canonically (os.path.realpath + os.path.commonpath), never as string prefixes, so
`proj-secret` is not inside `proj`, and a symlink pointing out of a folder does not count as inside it.
"""

from __future__ import annotations

import json
import math
import os
import re
import urllib.request
from urllib.parse import unquote

MAX_BODY = 1 << 20          # JSON request bodies; routes that need more (video frames) raise it explicitly
MAX_MESSAGE = 8000          # characters in one chat message
PROJECT_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
ROBOT_SOURCE = re.compile(r"[a-z0-9][a-z0-9_-]{0,47}")
WINDOWS_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
# Claude (sk-ant-), SubQ (sk- with a digit somewhere, so "sk-thin-and-light" is no key) and Keewano (kwpat-) keys, and
# Morph Teams' own codes: script tokens (mt_), invite codes (mi_) and sign-in link codes (ml_), which act as a member
SECRET = re.compile(r"\b(sk-ant-[A-Za-z0-9_-]{8,}|sk-(?=[A-Za-z_-]*[0-9])[A-Za-z0-9_-]{20,}|kwpat-[A-Za-z0-9_-]{8,}"
                    r"|m[til]_[A-Za-z0-9_-]{20,})")


def secret_refused(where: str) -> str:
    """The refusal every free-text field gives when looks_like_secret() is true, e.g. where="in chat"."""
    return (f"That looks like an API key or a Morph access code. Keys go in the Keys dialog; never paste either {where}. "
            "This was not sent or saved.")


def project_name(value) -> str:
    """A project folder name: 1 to 40 lowercase letters, digits or hyphens. Raises ValueError."""
    if not isinstance(value, str) or not PROJECT_NAME.fullmatch(value) or value in WINDOWS_RESERVED:
        raise ValueError("Project names are 1 to 40 lowercase letters, digits or hyphens, starting with a letter or digit.")
    return value


def robot_source(value) -> str:
    """An arm.source id (a robot bundle folder): 1 to 48 lowercase letters, digits, _ or -. Raises ValueError."""
    if not isinstance(value, str) or not ROBOT_SOURCE.fullmatch(value) or value in WINDOWS_RESERVED:
        raise ValueError("A robot source is the name of a robot folder, such as so101 "
                         "(lowercase letters, digits, _ or -), never a path.")
    return value


def inside(root: str, path: str) -> bool:
    """True when `path` is `root` or below it, after resolving symlinks, '..' and case on both."""
    root, path = os.path.realpath(root), os.path.realpath(path)
    try:
        return os.path.normcase(os.path.commonpath([root, path])) == os.path.normcase(root)
    except ValueError:  # different drives on Windows
        return False


def contained_path(root: str, relative: str, decode: bool = True) -> str | None:
    """root/relative, or None unless every segment is a plain name (no '', '.', '..' or hidden '.x' segments,
    no backslashes, drive letters or NUL) and the canonical result stays strictly inside root.
    URL paths are percent-decoded once first (decode=False for names that never came from a URL)."""
    rel = unquote(relative) if decode and isinstance(relative, str) else relative
    if not isinstance(rel, str) or not rel or any(c in rel for c in "\\:\x00"):
        return None
    parts = rel.split("/")
    if any(not p or p.startswith(".") for p in parts):
        return None
    full = os.path.realpath(os.path.join(root, *parts))
    if not inside(root, full) or os.path.normcase(full) == os.path.normcase(os.path.realpath(root)):
        return None
    return full


def looks_like_secret(text: str) -> bool:
    """The one filter for free text people type (chat, New robot, Show Morph the job's cards, every Teams text field):
    a Claude, SubQ or Keewano key, a Morph Teams script token, invite or sign-in code, or a tool's key (toolbox: the
    value of a saved MORPH_TOOL_* key, a token with a manifest's key prefix that fits its pattern, a bearer token)."""
    if not isinstance(text, str) or not text:
        return False
    if SECRET.search(text):
        return True
    from .toolbox import catalog
    return catalog.looks_like_tool_key(text)


def holds_secret(value) -> bool:
    """looks_like_secret() over every string in a request value (dict keys and values, lists, nested), so a key in a
    structured answer is caught too. The strings are checked as one text, a line each: no key pattern spans a line."""
    todo, texts = [value], []
    while todo:
        v = todo.pop()
        if isinstance(v, str):
            texts.append(v)
        elif isinstance(v, dict):
            todo.extend(v.keys())
            todo.extend(v.values())
        elif isinstance(v, (list, tuple)):
            todo.extend(v)
    return looks_like_secret("\n".join(texts))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # the redirect becomes an HTTPError: urllib would resend the key's header to the new address


_KEYED = urllib.request.build_opener(_NoRedirect)


def open_keyed(request: urllib.request.Request, timeout: float):
    """urlopen for a request that carries an API key (SubQ, Keewano): same proxies and TLS checks, but a redirect is
    refused (HTTPError 30x) instead of followed, so the key never goes to another server."""
    return _KEYED.open(request, timeout=timeout)


class BodyError(ValueError):
    """A request body the Studio refuses; `status` is the HTTP code, the message is safe to show."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def content_length(headers, max_body: int = MAX_BODY) -> int:
    """The declared body size, checked before a byte is read: 400 when missing-but-chunked or malformed,
    413 above max_body. No Content-Length means an empty body."""
    if headers.get("Transfer-Encoding"):
        raise BodyError(400, "Send the request body with a Content-Length header.")
    values = headers.get_all("Content-Length") or []
    if not values:
        return 0
    if len(values) > 1 or not re.fullmatch(r"[0-9]{1,15}", values[0].strip()):
        raise BodyError(400, "Malformed Content-Length header.")
    n = int(values[0])
    if n > max_body:
        raise BodyError(413, f"The request body is larger than the {max_body // 1024} KiB limit.")
    return n


def _no_constant(name):
    raise ValueError(f"{name} is not valid JSON")


def json_object(raw: bytes) -> dict:
    """A JSON object from a request body (empty body = {}); NaN/Infinity, arrays and scalars are refused."""
    if not raw:
        return {}
    try:
        body = json.loads(raw.decode("utf-8"), parse_constant=_no_constant)
    except (ValueError, UnicodeError, RecursionError):
        body = None
    if not isinstance(body, dict):
        raise BodyError(400, "Send a JSON object.")
    return body


# What a design value can be at all: far wider than the editor's PARAMS ranges or what Morph can build, but the
# pipeline's work grows with these sizes (a 1 km egg means millions of gear teeth and hours of simulation), so a typo or
# a model's answer must never tie up the Studio. Sizes in mm, masses in g, forces in N, inclusive.
DESIGN_LIMITS = {
    "object.diameter_mm": (1, 250), "object.length_mm": (1, 300), "object.mass_g": (0, 100_000),
    "object.friction": (0.01, 5), "object.max_force_n": (0, 10_000),
    "finger.length_mm": (5, 300), "finger.thickness_mm": (0.5, 50), "finger.width_mm": (0.5, 100),
    "finger.pad_height_mm": (0.5, 300), "finger.pad_depth_mm": (0.1, 100), "finger.mount_hole_d_mm": (0.5, 50),
    "finger.mount_hole_spacing_mm": (0, 200), "gripper.force_n": (0, 10_000), "gripper.stroke_mm": (1, 1000),
    "fab.tool_d_mm": (0.1, 50), "fab.quantity": (1, 1_000_000), "electronics.supply_v": (0.5, 1000),
    "electronics.pinion_radius_mm": (1, 200), "electronics.target_force_n": (0, 10_000), "arm.dof": (4, 6),
    "arm.reach_mm": (50, 2000), "arm.base_height_mm": (10, 1000), "arm.payload_margin": (0.1, 100),
    "task.pick_x_mm": (-2000, 2000), "task.pick_y_mm": (-2000, 2000), "task.place_x_mm": (-2000, 2000),
    "task.place_y_mm": (-2000, 2000), "task.cycle_time_s": (0.1, 3600), "build.quantity": (1, 1_000_000),
    "safety_factor": (0.1, 100),
}
MAX_TEXT = 500  # characters in a design's text value (names, extras, actuator overrides)
# text values with a fixed shape: world mode's room is named by id (never a path) and pinned by its hash
DESIGN_TEXT = {"world.id": re.compile(r"(?:[a-z0-9][a-z0-9-]{0,39})?"), "world.sha": re.compile(r"[0-9a-f]{0,64}")}


def design_values(flat: dict, defaults: dict, keys=None):
    """ValueError unless each value of `flat` (a Design.flat(); only `keys` when given) has its default's type, is
    finite and is inside DESIGN_LIMITS: for a design from outside, or one a chat or model edit is about to build."""
    for key in sorted(flat if keys is None else set(keys) & set(flat)):
        value, like = flat[key], defaults.get(key)
        if isinstance(like, str):
            if not isinstance(value, str) or len(value) > MAX_TEXT:
                raise ValueError(f"{key} must be text of at most {MAX_TEXT} characters.")
            if key in DESIGN_TEXT and not DESIGN_TEXT[key].fullmatch(value):
                raise ValueError(f"{key} must be a world id (lower-case letters, digits and -) or its hash, never a path.")
            continue
        if like is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{key} must be a finite number.")
        if isinstance(like, int) and value != int(value):
            raise ValueError(f"{key} must be a whole number.")
        lo, hi = DESIGN_LIMITS.get(key, (-math.inf, math.inf))
        if not lo <= value <= hi:
            num = lambda x: f"{x:,.0f}" if x == int(x) and abs(x) < 1e15 else f"{x:g}"  # noqa: E731
            raise ValueError(f"{key} = {num(value)} is outside what Morph can work with ({num(lo)} to {num(hi)}).")


def param_changes(changes, params) -> dict:
    """Parameter-editor changes: a non-empty dict of known PARAMS keys with in-range scalar values.
    `params` is pipeline2.PARAMS: (key, label, group, "select" | "number", {options | min, max, unit})."""
    if not isinstance(changes, dict) or not changes:
        raise ValueError("Send the changes as {parameter: value}.")
    schema = {key: (label, typ, extra) for key, label, _, typ, extra in params}
    out = {}
    for key, value in changes.items():
        if key not in schema:
            raise ValueError(f"{key} is not an editable parameter.")
        label, typ, extra = schema[key]
        if isinstance(value, bool) or not isinstance(value, (str, int, float)) or (isinstance(value, str) and len(value) > 200):
            raise ValueError(f"{label}: send a single number or option.")
        if typ == "select":
            match = next((o for o in extra.get("options", []) if value == o or str(value) == str(o)), None)
            if match is None:
                raise ValueError(f"{label}: choose one of " + ", ".join(str(o) or "(none)" for o in extra.get("options", [])) + ".")
            out[key] = match
            continue
        try:
            num = float(value)
        except ValueError:
            raise ValueError(f"{label}: send a number.") from None
        if not math.isfinite(num):
            raise ValueError(f"{label}: send a finite number.")
        lo, hi = extra.get("min", -math.inf), extra.get("max", math.inf)
        if not lo <= num <= hi:
            raise ValueError(f"{label} must be between {lo:g} and {hi:g}{(' ' + extra['unit']) if extra.get('unit') else ''}.")
        out[key] = int(num) if isinstance(value, int) or (isinstance(value, str) and re.fullmatch(r"-?[0-9]+", value.strip())) else num
    return out
