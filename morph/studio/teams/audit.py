"""The team's security log: MORPH_HOME/.team/audit.jsonl, one JSON object per line, UTC timestamps.

Each line carries `prev`, the SHA-256 of the line before it (64 zeros for the first), and audit.head records the
line count and the hash of the last line, so `verify()` (`morph team audit --verify`) detects an edited, deleted,
reordered or truncated line. It records who did what, never secrets: key changes list key names only, invite
and token events carry ids, not codes.

Events: team_created, sign_in, invite_created, invite_revoked, link_created, token_created, token_revoked,
member_revoked, sign_out_all, role_changed, disciplines_changed, policy_changed, keys_changed, settings_changed,
export, import, download_gated, reset, release_created, project_deleted.
"""

from __future__ import annotations

import hashlib
import json
import os

from .store import locked, read_json, team_dir, utc, write_json

ZERO = "0" * 64
SECRET_FIELDS = {"code", "token", "secret", "password", "value", "values", "hash", "key", "cookie"}
MAX_FIELD = 300


def path(home: str | None = None) -> str:
    return os.path.join(team_dir(home), "audit.jsonl")


def _head_path(home: str | None = None) -> str:
    return os.path.join(team_dir(home), "audit.head")


def _clean(v, depth=0):
    if isinstance(v, bool) or v is None or isinstance(v, int):
        return v
    if isinstance(v, float):
        return round(v, 3)
    if isinstance(v, str):
        return v[:MAX_FIELD]
    if isinstance(v, (list, tuple, set)) and depth < 2:
        return [_clean(x, depth + 1) for x in list(v)[:50]]
    if isinstance(v, dict) and depth < 2:
        return {str(k)[:40]: _clean(x, depth + 1) for k, x in list(v.items())[:30] if str(k).lower() not in SECRET_FIELDS}
    return str(v)[:MAX_FIELD]


def _last_line(p: str) -> bytes | None:
    """The file's last non-empty line, read from the end (the log only grows)."""
    try:
        with open(p, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            end = fh.tell()
            if end == 0:
                return None
            size = min(end, 65536)
            while True:
                fh.seek(end - size)
                chunk = fh.read(size).rstrip(b"\r\n")
                cut = chunk.rfind(b"\n")
                if cut >= 0 or size == end:
                    return chunk[cut + 1:].rstrip(b"\r") or None
                size = min(end, size * 4)
    except OSError:
        return None


def append(event: str, by: str | None = None, home: str | None = None, **fields) -> dict:
    """Add one event; never raises (an unwritable log must not break sign-in), returns the entry written."""
    entry = {"ts": utc(), "event": str(event)[:40], "by": by}
    for k, v in fields.items():
        if k.lower() in SECRET_FIELDS:
            continue
        entry[k] = _clean(v)
    folder = team_dir(home)
    try:
        with locked(folder):
            p = path(home)
            last = _last_line(p)
            head = read_json(_head_path(home), {}) or {}
            entry["prev"] = hashlib.sha256(last).hexdigest() if last else ZERO
            if (last and head.get("last") != entry["prev"]) or (not last and head.get("lines")):
                # the end of the log changed since Morph wrote it: say so in the chain instead of healing it
                entry["tamper"] = "the log was changed outside Morph before this entry"
            n = int(head.get("lines") or 0) + 1 if last else 1
            line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            with open(p, "ab") as fh:
                fh.write(line + b"\n")
            write_json(_head_path(home), {"lines": n, "last": hashlib.sha256(line).hexdigest(), "ts": entry["ts"]}, private=True)
    except (OSError, TimeoutError, ValueError):
        pass
    return entry


def _lines(home: str | None = None) -> list:
    try:
        with open(path(home), "rb") as fh:
            raw = fh.read()
    except OSError:
        return []
    return [ln.rstrip(b"\r") for ln in raw.split(b"\n") if ln.strip()]


def entries(home: str | None = None, limit: int = 200, event: str | None = None) -> list:
    """Newest first. Lines that don't parse are skipped here; verify() reports them."""
    out = []
    for ln in reversed(_lines(home)):
        try:
            e = json.loads(ln.decode("utf-8"))
        except (ValueError, UnicodeError):
            continue
        if isinstance(e, dict) and (event is None or e.get("event") == event):
            out.append({k: v for k, v in e.items() if k != "prev"})
            if len(out) >= limit:
                break
    return out


def verify(home: str | None = None) -> dict:
    """Walk the chain: {"ok", "lines", "error", "line"} where `line` is the first bad line (1-based)."""
    lines = _lines(home)
    prev = ZERO
    last_ts = ""
    for i, ln in enumerate(lines, 1):
        try:
            e = json.loads(ln.decode("utf-8"))
        except (ValueError, UnicodeError):
            return {"ok": False, "lines": len(lines), "line": i, "error": f"line {i} is not valid JSON (edited by hand?)"}
        if not isinstance(e, dict) or e.get("prev") != prev:
            return {"ok": False, "lines": len(lines), "line": i,
                    "error": f"line {i} does not follow line {i - 1}: a line was edited, removed or moved"}
        if e.get("tamper"):
            return {"ok": False, "lines": len(lines), "line": i, "error": f"line {i}: {e['tamper']}"}
        ts = str(e.get("ts") or "")
        if ts < last_ts:
            return {"ok": False, "lines": len(lines), "line": i, "error": f"line {i} is older than the line before it"}
        last_ts = ts
        prev = hashlib.sha256(ln).hexdigest()
    head = read_json(_head_path(home), None)
    if isinstance(head, dict) and head.get("lines"):
        if int(head["lines"]) != len(lines) or (lines and head.get("last") != prev):
            return {"ok": False, "lines": len(lines), "line": len(lines),
                    "error": f"the log ends early or its last line changed: audit.head records {head['lines']} lines"}
    elif lines:
        return {"ok": False, "lines": len(lines), "line": len(lines), "error": "audit.head is missing, so the end of the log can't be checked"}
    return {"ok": True, "lines": len(lines), "line": None, "error": None}
