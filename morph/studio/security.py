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
# Claude (sk-ant-), SubQ (sk- with a digit somewhere, so "sk-thin-and-light" is no key) and Keewano (kwpat-) keys
SECRET = re.compile(r"\b(sk-ant-[A-Za-z0-9_-]{8,}|sk-(?=[A-Za-z_-]*[0-9])[A-Za-z0-9_-]{20,}|kwpat-[A-Za-z0-9_-]{8,})")


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
    return bool(SECRET.search(text or ""))


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
