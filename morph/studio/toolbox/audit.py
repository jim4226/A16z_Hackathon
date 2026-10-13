"""The toolbox's log: MORPH_HOME/.tools/audit.jsonl, one JSON object per line, UTC timestamps.

Each line carries `prev`, the SHA-256 of the line before it (64 zeros for the first), and audit.head records the line
count and the hash of the last line, so `verify()` detects an edited, deleted, reordered or truncated line. It records
names, hosts, outcomes, timings and costs, never a key, a tool's arguments or what a service answered: key changes
list key names only.

Events: tool_enabled, tool_disabled, keys_saved, tool_approved (mode once | always | key_saved), tool_skipped,
tool_denied, tool_refused, tool_call, run_started, run_paused, run_resumed, run_interrupted, run_done, run_failed,
run_refused, run_cancelled, run_expired.
"""

from __future__ import annotations

import hashlib
import json
import os

from . import store

ZERO = "0" * 64
SECRET_FIELDS = {"value", "values", "key", "secret", "token", "password", "authorization", "code", "cookie", "input",
                 "content", "messages", "prompt", "system"}
MAX_FIELD = 300


def path(home: str | None = None) -> str:
    return os.path.join(store.tools_dir(home), "audit.jsonl")


def _head(home: str | None = None) -> str:
    return os.path.join(store.tools_dir(home), "audit.head")


def _clean(v, depth=0):
    if isinstance(v, bool) or v is None or isinstance(v, int):
        return v
    if isinstance(v, float):
        return round(v, 4)
    if isinstance(v, str):
        return v[:MAX_FIELD]
    if isinstance(v, (list, tuple, set)) and depth < 2:
        return [_clean(x, depth + 1) for x in list(v)[:50]]
    if isinstance(v, dict) and depth < 2:
        return {str(k)[:40]: _clean(x, depth + 1) for k, x in list(v.items())[:30] if str(k).lower() not in SECRET_FIELDS}
    return str(v)[:MAX_FIELD]


def _last_line(p: str) -> bytes | None:
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


def append(event: str, *, home: str | None = None, by: str | None = None, secrets=(), **fields) -> dict:
    """Add one event; never raises (an unwritable log must not stop a run). `secrets`: values that must never appear
    in the line (the caller's known keys); any that slipped into a field is replaced before writing."""
    entry = {"ts": store.utc(), "event": str(event)[:40], "by": by}
    for k, v in fields.items():
        if k.lower() not in SECRET_FIELDS:
            entry[k] = _clean(v)
    folder = store.tools_dir(home)
    try:
        with store.locked(folder):
            p = path(home)
            last = _last_line(p)
            head = store.read_json(_head(home), {}) or {}
            entry["prev"] = hashlib.sha256(last).hexdigest() if last else ZERO
            if (last and head.get("last") != entry["prev"]) or (not last and head.get("lines")):
                entry["tamper"] = "the log was changed outside Morph before this entry"
            n = int(head.get("lines") or 0) + 1 if last else 1
            text = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
            for s in secrets:
                if s and len(s) >= 8 and s in text:
                    text = text.replace(s, "[redacted]")
            line = text.encode("utf-8")
            with open(p, "ab") as fh:
                fh.write(line + b"\n")
            try:
                os.chmod(p, 0o600)
            except OSError:
                pass
            store.write_json(_head(home), {"lines": n, "last": hashlib.sha256(line).hexdigest(), "ts": entry["ts"]})
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
    """Walk the chain: {"ok", "lines", "line", "error"}, `line` being the first bad line (1-based)."""
    lines = _lines(home)
    prev, last_ts = ZERO, ""
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
        last_ts, prev = ts, hashlib.sha256(ln).hexdigest()
    head = store.read_json(_head(home), None)
    if isinstance(head, dict) and head.get("lines"):
        if int(head["lines"]) != len(lines) or (lines and head.get("last") != prev):
            return {"ok": False, "lines": len(lines), "line": len(lines),
                    "error": f"the log ends early or its last line changed: audit.head records {head['lines']} lines"}
    elif lines:
        return {"ok": False, "lines": len(lines), "line": len(lines), "error": "audit.head is missing, so the end of the log can't be checked"}
    return {"ok": True, "lines": len(lines), "line": None, "error": None}
