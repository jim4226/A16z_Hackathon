"""The team's security log: MORPH_HOME/.team/audit.jsonl, one JSON object per line, UTC timestamps.

Each line carries `prev`, the SHA-256 of the line before it (64 zeros for the first), and audit.head records the
line count and the hash of the last line, so `verify()` (`morph team audit --verify`) detects an edited, deleted,
reordered or truncated line; a line cut off by a crash or a full disk is reported as that, not as tampering
(morph/studio/chainlog.py). It records who did what, never secrets: key changes list key names only, invite
and token events carry ids, not codes.

Events: team_created, sign_in, invite_created, invite_revoked, link_created, token_created, token_revoked,
member_revoked, sign_out_all, role_changed, disciplines_changed, policy_changed, keys_changed, settings_changed,
export, import, download_gated, reset, release_created, project_deleted.
"""

from __future__ import annotations

import os

from .. import chainlog
from .store import locked, read_json, team_dir, utc, write_json

ZERO = chainlog.ZERO
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
            chainlog.append(path(home), _head_path(home), entry, read_json=read_json,
                            write_json=lambda p, d: write_json(p, d, private=True))
    except (OSError, TimeoutError, ValueError):
        pass
    return entry


def entries(home: str | None = None, limit: int = 200, event: str | None = None) -> list:
    """Newest first. Lines that don't parse are skipped here; verify() reports them."""
    return chainlog.entries(path(home), limit, event)


def verify(home: str | None = None) -> dict:
    """Walk the chain: {"ok", "lines", "error", "line"} where `line` is the first bad line (1-based); a line cut off by a
    crash or a full disk adds "cut" and "note" (chainlog.verify)."""
    return chainlog.verify(path(home), _head_path(home), read_json)
