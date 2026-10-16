"""What happened and who needs to know: the project activity feed, each member's inbox and live presence.

- feed(): one time-ordered stream merged from the ledger (revisions, with who made them), the saved chat,
  comments, change requests, reviews, branches and releases.
- Inbox: MORPH_HOME/.team/inbox/<member>.jsonl holds that member's notifications (mentions, review requests,
  decisions, change requests they watch, their queued jobs finishing or failing), with a read cursor beside it.
- Presence: who has the Studio open, on which tab and revision; idle after 5 minutes. In memory only.
- KEEWANO_EVENTS / keewano_events(): pseudonymous telemetry (hashed member ids, never names), ids 2180-2229.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time

from . import collab
from .disciplines import LABELS
from .store import append_jsonl, locked, new_id, parse_ts, read_json, read_jsonl, team_dir, utc, write_json

INBOX_KEEP, INBOX_MAX = 500, 1000
IDLE_S, GONE_S = 300, 900
KINDS = ("mention", "review_request", "decision", "proposal", "watch", "job", "release", "comment")


# ------------------------------------------------------------------ feed


def _t(ts) -> float:
    return parse_ts(ts) or 0.0


def _item(kind: str, ts, by, text: str, ref: dict, seq: int) -> dict:
    return {"id": f"{kind}:{ref.get('id') or ref.get('rev') or seq}:{seq}", "kind": kind, "ts": ts, "t": _t(ts), "by": by,
            "text": text, "ref": ref, "_seq": seq}


def _and(names: list) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1] if names else ""


def _name(members: dict, mid) -> str:
    return (members.get(mid) or {}).get("name") or ("someone" if not mid else "a former member")


def feed(root: str, members=None, limit: int = 50, before: float | None = None) -> list:
    """Newest first. `before` (epoch seconds) pages back. Items: {id, kind, ts, t, by, text, ref}."""
    members = members if isinstance(members, dict) else {m.get("id"): m for m in members or []}
    items, seq = [], 0

    def add(*a):
        nonlocal seq
        seq += 1
        items.append(_item(*a, seq))

    for e in read_jsonl(os.path.join(root, "ledger.jsonl")):
        if e.get("type") == "revision" and isinstance(e.get("rev"), str):
            who = e.get("by")
            label = str(e.get("label") or "")[:80]
            text = f"{_name(members, who) if who else 'Morph'} made {e['rev']}" + (f": {label}" if label else "")
            if e.get("proposed_by") and e.get("proposed_by") != who:
                text += f" (proposed by {_name(members, e['proposed_by'])})"
            add("revision", e.get("ts"), who, text, {"rev": e["rev"], "recomputed": list(e.get("recomputed") or [])[:9]})
    for e in read_jsonl(os.path.join(root, ".morph", "chat.jsonl")):
        if e.get("type") == "user" and isinstance(e.get("text"), str):
            add("chat", e.get("ts"), e.get("by"), f"{_name(members, e.get('by'))}: {e['text'][:140]}", {"rev": e.get("rev")})
    for op in read_jsonl(collab._file(root, "comments.jsonl")):
        if op.get("op") == "add":
            a = op.get("anchor") or {}
            where = f"{a.get('rev')}" + (f" {a.get('label')}" if a.get("kind") != "rev" and a.get("label") else "")
            verb = "replied on" if op.get("parent") else "commented on"
            add("comment", op.get("ts"), op.get("by"), f"{_name(members, op.get('by'))} {verb} {where}: {str(op.get('body'))[:140]}",
                {"id": op.get("id"), "anchor": a})
        elif op.get("op") == "resolve":
            add("comment", op.get("ts"), op.get("by"), f"{_name(members, op.get('by'))} {'resolved' if op.get('resolved', True) else 'reopened'} a comment thread",
                {"id": op.get("id")})
    titles = {}
    for op in read_jsonl(collab._file(root, "proposals.jsonl")):
        k, pid = op.get("op"), op.get("id")
        if k == "open":
            titles[pid] = (op.get("n"), op.get("title"))
            add("proposal", op.get("ts"), op.get("by"), f"{_name(members, op.get('by'))} opened change request #{op.get('n')}: {op.get('title')}",
                {"id": pid, "n": op.get("n")})
        elif k in ("apply", "close") and pid in titles:
            n, title = titles[pid]
            add("proposal", op.get("ts"), op.get("by"),
                f"{_name(members, op.get('by'))} {'applied' if k == 'apply' else 'closed'} change request #{n}: {title}", {"id": pid, "n": n})
        elif k == "done" and pid in titles and op.get("error"):
            add("proposal", op.get("ts"), None, f"Change request #{titles[pid][0]} failed to build: {str(op.get('error'))[:120]}", {"id": pid})
        elif k == "done" and pid in titles and op.get("noop") and op.get("rev"):  # no revision of its own to list it
            add("proposal", op.get("ts"), None, f"Change request #{titles[pid][0]} needed no build: {op['rev']} already has it",
                {"id": pid, "rev": op["rev"]})
    reviews = {}
    for op in read_jsonl(collab._file(root, "reviews.jsonl")):
        k = op.get("op")
        if k == "request":
            t = op.get("target") or {}
            what = f"change request #{titles.get(t.get('proposal'), ('?',))[0]}" if t.get("kind") == "proposal" else str(t.get("rev"))
            reviews[op.get("id")] = what
            asked = ", ".join(LABELS.get(d, d) for d in op.get("disciplines") or [])
            add("review", op.get("ts"), op.get("by"), f"{_name(members, op.get('by'))} asked for review of {what}" + (f": {asked}" if asked else ""),
                {"id": op.get("id"), "target": t})
        elif k == "decide" and op.get("review") in reviews:
            ds = _and([LABELS.get(d, d) for d in op.get("disciplines") or []])
            verb = "approved" if op.get("decision") == "approve" else "requested changes on"
            add("review", op.get("ts"), op.get("by"), f"{_name(members, op.get('by'))} {verb} {reviews[op['review']]}" + (f" as {ds}" if ds else ""),
                {"id": op.get("review"), "rev": op.get("rev"), "decision": op.get("decision")})
    for b in collab.branches(root)[1:]:
        add("branch", b.get("ts"), b.get("by"), f"{_name(members, b.get('by'))} started branch {b['name']} from {b.get('base_rev')}", {"branch": b["name"]})
    for r in collab.releases(root):
        add("release", r.get("ts"), r.get("by"), f"{_name(members, r.get('by'))} released {r.get('rev')} as {r.get('tag')} ({r.get('level')})",
            {"rev": r.get("rev"), "tag": r.get("tag")})
    items.sort(key=lambda i: (i["t"], i["_seq"]), reverse=True)
    if before is not None:
        items = [i for i in items if i["t"] < float(before)]
    project = os.path.basename(os.path.normpath(root))
    return [{**{k: v for k, v in i.items() if k != "_seq"}, "project": project} for i in items[:max(1, min(int(limit or 50), 500))]]


# ------------------------------------------------------------------ inbox


def _inbox_dir(home: str | None) -> str:
    return os.path.join(team_dir(home), "inbox")


def _inbox_path(home, mid: str) -> str:
    return os.path.join(_inbox_dir(home), f"{mid}.jsonl")


def _cursor_path(home, mid: str) -> str:
    return os.path.join(_inbox_dir(home), f"{mid}.read.json")


def _valid_mid(mid) -> bool:
    return isinstance(mid, str) and mid.startswith("m") and mid[1:].isalnum() and len(mid) <= 24


def notify(home: str | None, member_id: str, kind: str, text: str, project: str | None = None, ref: dict | None = None,
           by: str | None = None) -> dict | None:
    """Add one notification to a member's inbox. Never notifies the member about their own action."""
    if not _valid_mid(member_id) or member_id == by or kind not in KINDS:
        return None
    folder = _inbox_dir(home)
    with locked(team_dir(home)):
        p = _inbox_path(home, member_id)
        items = read_jsonl(p)
        seq = (int(items[-1].get("seq") or 0) if items else int((read_json(_cursor_path(home, member_id), {}) or {}).get("upto") or 0)) + 1
        item = {"seq": seq, "id": new_id("n", 5), "ts": utc(), "kind": kind, "project": project, "text": str(text)[:300],
                "ref": ref or {}, "by": by}
        if len(items) >= INBOX_MAX:
            write_lines = items[-(INBOX_KEEP - 1):] + [item]
            tmp = p + ".tmp"
            os.makedirs(folder, exist_ok=True)
            with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
                for it in write_lines:
                    fh.write(json.dumps(it, ensure_ascii=False, separators=(",", ":")) + "\n")
            os.replace(tmp, p)
        else:
            append_jsonl(p, item)
    return item


def notify_many(home, member_ids, kind: str, text: str, project=None, ref=None, by=None) -> list:
    out, seen = [], set()
    for mid in member_ids or []:
        if mid in seen:
            continue
        seen.add(mid)
        it = notify(home, mid, kind, text, project, ref, by)
        if it:
            out.append((mid, it))
    return out


def inbox(home: str | None, member_id: str, limit: int = 50) -> dict:
    if not _valid_mid(member_id):
        return {"items": [], "unread": 0, "read_upto": 0}
    items = read_jsonl(_inbox_path(home, member_id))
    upto = int((read_json(_cursor_path(home, member_id), {}) or {}).get("upto") or 0)
    unread = sum(1 for i in items if int(i.get("seq") or 0) > upto)
    out = [{**i, "unread": int(i.get("seq") or 0) > upto} for i in reversed(items[-max(1, min(int(limit or 50), INBOX_KEEP)):])]
    return {"items": out, "unread": unread, "read_upto": upto}


def mark_read(home: str | None, member_id: str, upto: int | None = None) -> dict:
    """Move the read cursor (to the newest notification by default). Returns the inbox's unread count."""
    if not _valid_mid(member_id):
        return {"unread": 0, "read_upto": 0}
    with locked(team_dir(home)):
        items = read_jsonl(_inbox_path(home, member_id))
        last = int(items[-1].get("seq") or 0) if items else 0
        cur = int((read_json(_cursor_path(home, member_id), {}) or {}).get("upto") or 0)
        new = last if upto is None else max(cur, min(int(upto), last))
        write_json(_cursor_path(home, member_id), {"upto": new, "ts": utc()})
    return {"unread": sum(1 for i in items if int(i.get("seq") or 0) > new), "read_upto": new}


def unread(home: str | None, member_id: str) -> int:
    return inbox(home, member_id, limit=1)["unread"]


# ------------------------------------------------------------------ presence


class Presence:
    """Who has the Studio open: POST /api/presence heartbeats plus live event-stream connections.
    A member is active within 5 minutes of their last heartbeat, idle until 15, then gone."""

    def __init__(self):
        self.lock = threading.Lock()
        self.seen = {}      # (project, member) -> {"t", "tab", "rev"}
        self.streams = {}   # (project, member) -> open event streams

    def touch(self, project: str, member_id: str, tab=None, rev=None, now=None):
        with self.lock:
            cur = self.seen.get((project, member_id)) or {}
            self.seen[(project, member_id)] = {"t": time.time() if now is None else now,
                                               "tab": str(tab)[:24] if tab else cur.get("tab"),
                                               "rev": str(rev)[:12] if rev else cur.get("rev")}

    def connected(self, project: str, member_id: str, delta: int = 1, now=None):
        with self.lock:
            n = max(0, self.streams.get((project, member_id), 0) + delta)
            self.streams[(project, member_id)] = n
            if n == 0:
                self.streams.pop((project, member_id), None)
        if delta > 0:
            self.touch(project, member_id, now=now)

    def snapshot(self, project: str, members=None, now=None) -> list:
        now = time.time() if now is None else now
        members = members if isinstance(members, dict) else {m.get("id"): m for m in members or []}
        out = []
        with self.lock:
            for (proj, mid), s in list(self.seen.items()):
                if proj != project:
                    continue
                age = now - s["t"]
                live = self.streams.get((proj, mid), 0) > 0
                if age > GONE_S and not live:
                    self.seen.pop((proj, mid), None)
                    continue
                m = members.get(mid)
                if not m or m.get("revoked"):
                    continue
                out.append({"id": mid, "name": m.get("name"), "color": m.get("color"), "role": m.get("role"),
                            "state": "active" if age <= IDLE_S else "idle", "tab": s.get("tab"), "rev": s.get("rev"),
                            "seen_s": round(age)})
        return sorted(out, key=lambda x: (x["state"] != "active", x["name"] or ""))


PRESENCE = Presence()


# ------------------------------------------------------------------ Keewano (pseudonymous)

# For keewano.CUSTOM_EVENTS (append-only; teams owns ids 2180-2229). Member ids are hashed, never names.
KEEWANO_EVENTS = [
    {"eventTypeId": 2180, "name": "TeamActor", "dataType": "string"},
    {"eventTypeId": 2181, "name": "TeamRole", "dataType": "string"},
    {"eventTypeId": 2182, "name": "CommentAdded", "dataType": "string"},
    {"eventTypeId": 2183, "name": "ChangeRequestOpened", "dataType": "string"},
    {"eventTypeId": 2184, "name": "ChangeRequestApplied", "dataType": "string"},
    {"eventTypeId": 2185, "name": "ReviewRequested", "dataType": "string"},
    {"eventTypeId": 2186, "name": "ReviewDecision", "dataType": "string"},
    {"eventTypeId": 2187, "name": "ApprovedRevision", "dataType": "string"},
    {"eventTypeId": 2188, "name": "ReleaseLevel", "dataType": "string"},
    {"eventTypeId": 2189, "name": "ReviewDisciplines", "dataType": "uint"},
]


def actor(member_id: str | None, secret: bytes = b"") -> str | None:
    """A stable pseudonym for telemetry: never the name, never the raw id."""
    if not member_id:
        return None
    return hashlib.sha256(secret + b"|morph-actor|" + member_id.encode()).hexdigest()[:16]


def keewano_events(entry: dict, secret: bytes = b"") -> list:
    """Ledger-style team entry -> [(event name, value)] for keewano.events_for (type "team")."""
    if entry.get("type") != "team":
        return []
    ev = [("TeamActor", actor(entry.get("by"), secret))]
    a = entry.get("action")
    if a == "comment":
        ev.append(("CommentAdded", entry.get("kind") or "rev"))
    elif a == "proposal":
        ev.append(("ChangeRequestOpened", str(entry.get("n"))))
    elif a == "apply":
        ev.append(("ChangeRequestApplied", str(entry.get("n"))))
    elif a == "review":
        ev += [("ReviewRequested", entry.get("rev") or "proposal"), ("ReviewDisciplines", len(entry.get("disciplines") or []))]
    elif a == "decide":
        ev.append(("ReviewDecision", entry.get("decision")))
    elif a == "release":
        ev += [("ApprovedRevision", entry.get("rev")), ("ReleaseLevel", entry.get("level"))]
    return [(n, v) for n, v in ev if v is not None]
