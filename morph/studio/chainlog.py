"""Hash-chained JSON-lines logs: the team's audit log (teams/audit.py) and the toolbox's (toolbox/audit.py).

Each line carries `prev`, the SHA-256 of the line before it (64 zeros for the first), and a head file records how many
entries there are and the hash of the last one, so verify() finds an entry that was edited, removed, moved or cut from
the end. The callers hold their folder's lock around append().

A crash or a full disk can stop an append half way. That is told apart from tampering, and costs only the entry being
written:
- a last line without its newline is ended before the next entry, which records it as `cut`: {bytes, sha256} of the
  cut-off line (so the cut-off bytes are chained too: changing them later is caught) and chains on from the last whole
  entry. verify() reports it (`cut`, `note`) and still answers ok;
- an entry written whole whose head update didn't happen (a stop between the two writes) is chained on, not flagged.
"""

from __future__ import annotations

import hashlib
import json
import os

ZERO = "0" * 64
CUT_NOTE = "cut off by a crash or a full disk while it was written; the event it held is lost"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def tail(path: str) -> tuple:
    """(the last whole line or None, the bytes after the last newline: a cut-off line, or b"")."""
    try:
        with open(path, "rb") as fh:
            end = fh.seek(0, os.SEEK_END)
            if end == 0:
                return None, b""
            size = min(end, 65536)
            while True:
                fh.seek(end - size)
                chunk = fh.read(size)
                lines = chunk.split(b"\n")
                torn = lines[-1]
                whole = [ln.rstrip(b"\r") for ln in lines[:-1] if ln.strip()]
                if len(whole) >= 2 or size == end:  # the first piece of a partial chunk may be cut: never use it
                    if size < end:
                        whole = whole[1:]
                    return (whole[-1] if whole else None), torn
                size = min(end, size * 4)
    except OSError:
        return None, b""


def append(path: str, head_path: str, entry: dict, *, read_json, write_json, finish=None) -> bytes:
    """Chain `entry` onto the log and update the head; returns the line written. `finish(text) -> text` may change the
    serialised line last (the toolbox replaces any secret that slipped in). Raises OSError (the callers swallow it)."""
    last, raw = tail(path)
    ended, torn = bool(raw), raw.rstrip(b"\r")  # bytes after the last newline: ended before this entry
    if torn.strip() and _prev_of(torn) is not None:  # a whole entry that lost only its newline: it is no cut-off line
        last, torn = torn, b""
    head = read_json(head_path, {}) or {}
    if not isinstance(head, dict):
        head = {}
    count = int(head.get("lines") or 0) if str(head.get("lines") or "0").isdigit() else 0
    prev = _sha(last) if last else ZERO
    if last is None and not count:
        n = 1
    elif last is not None and head.get("last") == prev:
        n = count + 1
    elif last is not None and _prev_of(last) == (head.get("last") or (ZERO if not count else None)):
        n = count + 2  # `last` was written whole, then the process stopped before the head was updated
    else:  # the end of the log changed since Morph wrote it: say so in the chain instead of healing it
        entry["tamper"] = "the log was changed outside Morph before this entry"
        n = count + 1 if last else 1
    entry["prev"] = prev
    if torn.strip():
        entry["cut"] = {"bytes": len(torn), "sha256": _sha(torn)}
    text = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
    if finish is not None:
        text = finish(text)
    line = text.encode("utf-8")
    with open(path, "ab") as fh:
        fh.write((b"\n" if ended else b"") + line + b"\n")  # one write: the cut-off line ends, then this entry
    write_json(head_path, {"lines": n, "last": _sha(line), "ts": entry.get("ts")})
    return line


def _prev_of(line: bytes):
    try:
        e = json.loads(line.decode("utf-8"))
    except (ValueError, UnicodeError):
        return None
    return e.get("prev") if isinstance(e, dict) else None


def lines(path: str) -> list:
    """Every non-empty line, the cut-off last one included."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return []
    return [ln.rstrip(b"\r") for ln in raw.split(b"\n") if ln.strip()]


def entries(path: str, limit: int = 200, event: str | None = None) -> list:
    """Newest first, without `prev`. Lines that don't parse are skipped here; verify() reports them."""
    out = []
    for ln in reversed(lines(path)):
        try:
            e = json.loads(ln.decode("utf-8"))
        except (ValueError, UnicodeError):
            continue
        if isinstance(e, dict) and (event is None or e.get("event") == event):
            out.append({k: v for k, v in e.items() if k != "prev"})
            if len(out) >= limit:
                break
    return out


def verify(path: str, head_path: str, read_json) -> dict:
    """Walk the chain: {"ok", "lines" (entries), "line", "error"}; `line` is the first bad line (1-based, counting every
    line of the file). Lines cut off by a crash or a full disk add "cut" (their line numbers) and "note"."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        raw = b""
    parts = raw.split(b"\n")
    torn = parts[-1].rstrip(b"\r")             # the bytes after the last newline: a cut-off last line, or b""
    rows = [ln.rstrip(b"\r") for ln in parts[:-1] if ln.strip()] + ([torn] if torn.strip() else [])
    total = len(rows)
    prev, last_ts, n, cut, pending, hashes = ZERO, "", 0, [], None, [ZERO]

    def bad(i, why):
        return {"ok": False, "lines": n, "line": i, "error": why}

    for i, ln in enumerate(rows, 1):
        try:
            e = json.loads(ln.decode("utf-8"))
        except (ValueError, UnicodeError):
            e = None
        if not isinstance(e, dict):
            if pending is not None:
                return bad(i - 1, f"line {i - 1} is not valid JSON (edited by hand?)")
            pending = (i, _sha(ln))            # a cut-off line, if the entry after it says so (or nothing follows)
            continue
        if pending is not None:
            if (e.get("cut") or {}).get("sha256") != pending[1]:
                return bad(pending[0], f"line {pending[0]} is not valid JSON (edited by hand?)")
            cut.append(pending[0])
            pending = None
        elif e.get("cut"):
            return bad(i, f"line {i} says the line before it was cut off by a crash, but that line isn't there: a line "
                          "was edited or removed")
        if e.get("prev") != prev:
            return bad(i, f"line {i} does not follow line {i - 1}: a line was edited, removed or moved")
        if e.get("tamper"):
            return bad(i, f"line {i}: {e['tamper']}")
        ts = str(e.get("ts") or "")
        if ts < last_ts:
            return bad(i, f"line {i} is older than the line before it")
        last_ts, prev, n = ts, _sha(ln), n + 1
        hashes.append(prev)
    if pending is not None:                    # the last line is cut off and nothing was written after it yet
        if pending[0] != total:
            return bad(pending[0], f"line {pending[0]} is not valid JSON (edited by hand?)")
        cut.append(pending[0])
    head = read_json(head_path, None)
    if isinstance(head, dict) and head.get("lines"):
        try:
            recorded = int(head["lines"])
        except (TypeError, ValueError):
            recorded = -1
        whole = recorded == n and head.get("last") == prev
        behind = recorded == n - 1 and n >= 1 and head.get("last") == hashes[-2]  # a stop before the head was updated
        if not (whole or behind):
            return {"ok": False, "lines": n, "line": total,
                    "error": f"the log ends early or its last line changed: audit.head records {head['lines']} lines"}
    elif n:
        return {"ok": False, "lines": n, "line": total, "error": "audit.head is missing, so the end of the log can't be checked"}
    out = {"ok": True, "lines": n, "line": None, "error": None}
    if cut:
        out["cut"] = cut
        out["note"] = "; ".join(f"line {c} was {CUT_NOTE}" for c in cut)
    return out
