"""Shared limits, the user-facing error type and small path/XML helpers for morph.importers.

Everything an import reads is hostile until proven otherwise (docs/internal/RELEASE_CONTRACTS.md section 9):
paths are canonicalized with realpath and checked with commonpath, XML with a DOCTYPE or ENTITY is refused,
and every limit below is enforced while bytes are streamed, never from headers alone.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
import xml.etree.ElementTree as ET

MB = 1 << 20
LIMITS = {
    "zip_entries": 2000, "zip_total": 300 * MB, "zip_file": 100 * MB, "zip_ratio": 100,
    "xml_bytes": 20 * MB, "mesh_faces": 1_500_000, "upload_file": 300 * MB, "upload_total": 300 * MB,
    "session_files": 2000, "fetch_bytes": 300 * MB, "fetch_timeout": 30.0, "fetch_redirects": 3, "fetch_files": 400,
}
ROBOT_RX = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")      # arm.source / .robots/<name>
PROJECT_RX = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")     # projects/<name>
SESSION_RX = re.compile(r"^[a-f0-9]{16}$")
WINDOWS_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
_HOME = None


class ImportFailed(ValueError):
    """An actionable message for the person importing; safe to show in the UI and return over HTTP."""


def set_home(path: str | None):
    """The Studio calls this with its projects folder; the CLI and tests rely on MORPH_HOME."""
    global _HOME
    _HOME = os.path.abspath(path) if path else None


def home() -> str:
    return _HOME or os.path.abspath(os.environ.get("MORPH_HOME") or "projects")


def utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def slug(text: str, sep: str = "-", maxlen: int = 40, rx=PROJECT_RX, fallback: str = "robot") -> str:
    """A safe folder name from free text (a URDF robot name, a repo name, what the person typed)."""
    s = re.sub(r"[^a-z0-9]+", sep, (text or "").lower()).strip(sep + "_-")[:maxlen].strip(sep + "_-")
    if not s or not s[0].isalnum():
        s = fallback
    if s in WINDOWS_RESERVED:
        s = f"{s}{sep}1"
    return s if rx.match(s) else fallback


def contained(root: str, path: str) -> str | None:
    """Real path of `path` if it stays inside `root` (symlinks resolved), else None. Never a prefix check."""
    try:
        base = os.path.realpath(root)
        real = os.path.realpath(path)
        return real if os.path.commonpath([base, real]) == base else None
    except (OSError, ValueError):
        return None


def safe_relpath(rel: str, maxlen: int = 1024) -> str:
    """Validate a client-supplied relative path ("folder/meshes/a.stl"); returns it with "/" separators."""
    if not isinstance(rel, str) or not rel or len(rel) > maxlen:
        raise ImportFailed("Each file needs a relative path of at most 1,024 characters.")
    rel = rel.replace("\\", "/")
    if rel.startswith("/") or re.match(r"^[A-Za-z]:", rel) or any(c in rel for c in ":\x00") or \
            any(ord(c) < 32 for c in rel):
        raise ImportFailed(f"Refused the path {rel[:80]!r}: use a plain relative path inside the folder.")
    parts = rel.split("/")
    if any(p in ("", ".", "..") or p.startswith(".") or len(p) > 255 for p in parts):
        raise ImportFailed(f"Refused the path {rel[:80]!r}: no '..', hidden or empty folder names.")
    return "/".join(parts)


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(MB), b""):
            h.update(chunk)
    return h.hexdigest()


def expat_ok() -> bool:
    """Linux builds may use the system expat; versions before 2.4.1 lack the amplification limits."""
    try:
        import pyexpat
        return tuple(pyexpat.version_info) >= (2, 4, 1)
    except Exception:  # noqa: BLE001
        return False


def xml_bytes(path: str, max_bytes: int | None = None) -> bytes:
    """Read an XML file for parsing: size cap, and no DOCTYPE/ENTITY declarations at all."""
    cap = max_bytes or LIMITS["xml_bytes"]
    with open(path, "rb") as fh:
        data = fh.read(cap + 1)
    if len(data) > cap:
        raise ImportFailed(f"{os.path.basename(path)} is over the {cap // MB} MB limit for XML files.")
    check_xml_text(data, os.path.basename(path))
    return data


def check_xml_text(data, name: str = "file"):
    if isinstance(data, bytes):
        if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
            text = data.decode("utf-16", errors="replace")
        else:
            text = data.decode("utf-8", errors="replace")
    else:
        text = data
    if re.search(r"<!\s*(DOCTYPE|ENTITY)", text, re.I):
        raise ImportFailed(f"{name} declares a DOCTYPE or ENTITY; Morph refuses those in imported XML. "
                           "Export the file again without a DTD.")


def parse_xml(path: str, max_bytes: int | None = None) -> ET.Element:
    if not expat_ok():
        raise ImportFailed("This Python's XML parser is too old to read untrusted files safely; update Python.")
    data = xml_bytes(path, max_bytes)
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise ImportFailed(f"{os.path.basename(path)} is not valid XML ({exc}).") from None


def parse_xml_text(text: str, name: str = "file") -> ET.Element:
    check_xml_text(text, name)
    if len(text) > LIMITS["xml_bytes"]:
        raise ImportFailed(f"{name} is over the {LIMITS['xml_bytes'] // MB} MB limit for XML files.")
    try:
        return ET.fromstring(text)
    except ET.ParseError as exc:
        raise ImportFailed(f"{name} is not valid XML ({exc}).") from None


def walk_files(root: str, max_files: int = 20000):
    """Regular files under root (relative "/" paths), skipping symlinks, hidden folders and files that resolve
    outside root. Git clones and dropped folders can hold symlinks; zips extracted by zipsafe never do."""
    base = os.path.realpath(root)
    n = 0
    for dp, dns, fns in os.walk(base, followlinks=False):
        dns[:] = sorted(d for d in dns if not d.startswith(".") and d != "__MACOSX"
                        and not os.path.islink(os.path.join(dp, d)))
        for fn in sorted(fns):
            if fn.startswith("."):
                continue
            p = os.path.join(dp, fn)
            if os.path.islink(p) or not os.path.isfile(p) or contained(base, p) is None:
                continue
            n += 1
            if n > max_files:
                raise ImportFailed(f"The folder holds more than {max_files:,} files; drop only the robot's folder.")
            yield os.path.relpath(p, base).replace(os.sep, "/")
