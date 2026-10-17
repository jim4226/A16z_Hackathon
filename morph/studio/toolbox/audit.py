"""The toolbox's log: MORPH_HOME/.tools/audit.jsonl, one JSON object per line, UTC timestamps.

Each line carries `prev`, the SHA-256 of the line before it (64 zeros for the first), and audit.head records the line
count and the hash of the last line, so `verify()` detects an edited, deleted, reordered or truncated line; a line cut off
by a crash or a full disk is reported as that, not as tampering (morph/studio/chainlog.py). It records
names, hosts, outcomes, timings and costs, never a key, a tool's arguments or what a service answered: key changes
list key names only.

Events: tool_enabled, tool_disabled, keys_saved, tool_approved (mode once | always | key_saved), tool_skipped,
tool_denied, tool_refused, tool_call, run_started, run_paused, run_resumed, run_interrupted, run_done, run_failed,
run_refused, run_cancelled, run_expired.
"""

from __future__ import annotations

import os

from .. import chainlog
from . import store

ZERO = chainlog.ZERO
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


def append(event: str, *, home: str | None = None, by: str | None = None, secrets=(), **fields) -> dict:
    """Add one event; never raises (an unwritable log must not stop a run). `secrets`: values that must never appear
    in the line (the caller's known keys); any that slipped into a field is replaced before writing."""
    entry = {"ts": store.utc(), "event": str(event)[:40], "by": by}
    for k, v in fields.items():
        if k.lower() not in SECRET_FIELDS:
            entry[k] = _clean(v)
    folder = store.tools_dir(home)

    def finish(text: str) -> str:
        for s in secrets:
            if s and len(s) >= 8 and s in text:
                text = text.replace(s, "[redacted]")
        return text
    try:
        with store.locked(folder):
            p = path(home)
            chainlog.append(p, _head(home), entry, read_json=store.read_json, write_json=store.write_json, finish=finish)
            try:
                os.chmod(p, 0o600)
            except OSError:
                pass
    except (OSError, TimeoutError, ValueError):
        pass
    return entry


def entries(home: str | None = None, limit: int = 200, event: str | None = None) -> list:
    """Newest first. Lines that don't parse are skipped here; verify() reports them."""
    return chainlog.entries(path(home), limit, event)


def verify(home: str | None = None) -> dict:
    """Walk the chain: {"ok", "lines", "line", "error"}, `line` being the first bad line (1-based); a line cut off by a
    crash or a full disk adds "cut" and "note" (chainlog.verify)."""
    return chainlog.verify(path(home), _head(home), store.read_json)
