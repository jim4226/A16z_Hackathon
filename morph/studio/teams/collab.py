"""Per-project collaboration, GitHub-style: comments, change requests (proposals), reviews, the release gate,
branches (variant lines) and releases. Files live in <project>/.team/: comments.jsonl, proposals.jsonl and
reviews.jsonl are append-only operation logs replayed on read; branches.json and releases.json are small
documents written atomically. Every write holds the project's team lock.

Approvals are per discipline and tied to the exact revision: a decision records the revision, its design hash
and the artifact hashes of the stages it covers. On a later revision a discipline's approval carries forward
only while every stage that discipline owns is reused unchanged (provenance "reused from rNNN"); otherwise it is
stale and that discipline approves again (GitHub's "dismiss stale reviews", scoped to the affected work).
"""

from __future__ import annotations

import collections
import fnmatch
import hashlib
import json
import math
import os
import re
import unicodedata

from ...robot.pipeline2 import ORDER, PARAMS, RobotProject
from ...spec import Design, diff
from ..labels import VALUES, show_key, show_value  # noqa: F401  (people's names for keys and values)
from . import audit
from .disciplines import (DISCIPLINES, LABELS, LEVELS, STAGE_TITLES, STAGES, affected_stages, disciplines_for_stages, eligible,
                          route_discipline)
from .store import append_jsonl, locked, new_id, project_dir, read_json, read_jsonl, utc, write_json

KINDS = ("rev", "stage", "bom", "param", "file")
DECISIONS = ("approve", "request_changes")
REV_RX = re.compile(r"^r([0-9]{1,6})$")
BRANCH_RX = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
TAG_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._+-]{0,39}$")
REUSED_RX = re.compile(r"^reused from (r[0-9]+)$")
ROBOT_SOURCE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
SECRET_RX = re.compile(r"\b(sk-ant-[A-Za-z0-9_-]{8,}|kwpat-[A-Za-z0-9_-]{8,}|mt_[A-Za-z0-9_-]{20,}|mi_[A-Za-z0-9_-]{20,}|ml_[A-Za-z0-9_-]{20,})")
MENTION_RX = re.compile(r"(?<![\w@])@([\w.-]{1,40})", re.UNICODE)
ORDER_FILES = ("mfg/*.zip", "mfg/*/gerbers.zip", "mfg/*/jlc_*.csv", "electronics/board/gerbers/*", "electronics/board/jlc_*.csv")
MAX_BODY, MAX_TITLE, MAX_CHANGES = 4000, 120, 40
_PARAMS = {k: (label, typ, extra) for k, label, _, typ, extra in PARAMS}
_DEFAULTS = Design().flat()


class CollabError(ValueError):
    """A refusal that is safe to show. `status` is the HTTP code; `gate` carries the release gate on a 409."""

    def __init__(self, status: int, message: str, gate: dict | None = None):
        super().__init__(message)
        self.status, self.gate = status, gate


def _file(root: str, name: str) -> str:
    return os.path.join(project_dir(root), name)


def _text(value, what: str, limit: int, multiline: bool = True) -> str:
    if not isinstance(value, str):
        raise CollabError(400, f"Write the {what} as text.")
    keep = "\n\t" if multiline else ""
    value = "".join(c for c in value if c in keep or unicodedata.category(c)[0] != "C")
    value = value.strip() if multiline else " ".join(value.split())
    if not value:
        raise CollabError(400, f"The {what} is empty.")
    if len(value) > limit:
        raise CollabError(400, f"The {what} is too long: {limit:,} characters at most.")
    if SECRET_RX.search(value):
        raise CollabError(400, "That looks like an API key or a Morph access code. Never paste those in comments; "
                               "this was not saved.")
    return value


def _canon(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str).encode("utf-8")


def design_sha256(design: dict) -> str:
    """SHA-256 of the design's canonical JSON (sorted keys, no spaces): the identity approvals are tied to."""
    return hashlib.sha256(_canon(design)).hexdigest()


def bom_key(category, item=None) -> str:
    """BOM rows have no id: a BOM comment anchors to sha1(category|item)[:10]."""
    if isinstance(category, dict):
        category, item = category.get("category"), category.get("item")
    return hashlib.sha1(f"{category or ''}|{item or ''}".encode("utf-8")).hexdigest()[:10]


# ------------------------------------------------------------------ revisions


def revs(root: str) -> list:
    """Completed revisions (design.json and results.json both written), sorted by number (r1000 after r999)."""
    try:
        names = os.listdir(root)
    except OSError:
        return []
    out = [n for n in names if REV_RX.match(n) and os.path.isfile(os.path.join(root, n, "results.json"))
           and os.path.isfile(os.path.join(root, n, "design.json"))]
    return sorted(out, key=lambda r: int(r[1:]))


_INFO: "collections.OrderedDict" = collections.OrderedDict()
_LEDGER: dict = {}


def _flat(d: dict, prefix: str = "", out=None) -> dict:
    out = {} if out is None else out
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            _flat(v, key, out)
        else:
            out[key] = v
    return out


def rev_info(root: str, rev) -> dict | None:
    """What the gate and comments need from one revision, cached by the files' mtimes: parent, label, design
    (+ flat keys and hash), provenance, per-stage result hashes, BOM row keys, summary."""
    if not isinstance(rev, str) or not REV_RX.match(rev):
        return None
    d = os.path.join(root, rev)
    try:
        s1, s2 = os.stat(os.path.join(d, "design.json")), os.stat(os.path.join(d, "results.json"))
    except OSError:
        return None
    stamp = (s1.st_mtime_ns, s1.st_size, s2.st_mtime_ns, s2.st_size)
    key = (os.path.normcase(os.path.abspath(root)), rev)
    hit = _INFO.get(key)
    if hit and hit[0] == stamp:
        _INFO.move_to_end(key)
        return hit[1]
    design, res = read_json(os.path.join(d, "design.json"), None), read_json(os.path.join(d, "results.json"), None)
    if not isinstance(design, dict) or not isinstance(res, dict):
        return None
    rows = ((res.get("bom") or {}).get("rows") or []) if isinstance(res.get("bom"), dict) else []
    bom = {}
    for r in rows:
        if isinstance(r, dict):
            bom[bom_key(r)] = {"item": r.get("item"), "category": r.get("category"), "qty": r.get("qty"),
                               "hash": hashlib.sha1(_canon(r)).hexdigest()[:12]}
    info = {"rev": rev, "parent": res.get("parent") if isinstance(res.get("parent"), str) else None,
            "label": res.get("label") or "", "design": design, "flat": _flat(design), "design_sha256": design_sha256(design),
            "provenance": res.get("provenance") if isinstance(res.get("provenance"), dict) else {},
            "sections": {s: hashlib.sha256(_canon([res.get(k) for k in RobotProject._keys(s)])).hexdigest() for s in ORDER},
            "bom": bom, "summary": res.get("summary") if isinstance(res.get("summary"), dict) else {},
            "by": res.get("by"), "proposed_by": res.get("proposed_by")}
    _INFO[key] = (stamp, info)
    while len(_INFO) > 1024:
        _INFO.popitem(last=False)
    return info


def level_of(info: dict) -> str:
    lvl = ((info or {}).get("design") or {}).get("build", {}).get("config") if isinstance((info or {}).get("design"), dict) else None
    return lvl if lvl in LEVELS else "prototype"


def ancestors(root: str, rev: str, limit: int = 5000) -> list:
    """[rev, parent, grandparent, ...] following results.json parents (stops at a missing or repeated revision)."""
    out, seen = [], set()
    while rev and rev not in seen and len(out) < limit:
        info = rev_info(root, rev)
        if info is None:
            break
        out.append(rev)
        seen.add(rev)
        rev = info["parent"]
    return out


def stage_origin(root: str, rev: str, stage: str) -> str | None:
    """The revision whose run computed this stage's artifacts (following "reused from <parent>")."""
    cur, seen = rev, set()
    while cur and cur not in seen:
        seen.add(cur)
        info = rev_info(root, cur)
        if info is None:
            return None
        m = REUSED_RX.match(str(info["provenance"].get(stage) or ""))
        if not m or m.group(1) != info["parent"] or rev_info(root, m.group(1)) is None:
            return cur
        cur = m.group(1)
    return cur


def stage_hash(root: str, rev: str, stage: str) -> str | None:
    """Identity of one stage's artifacts in a revision: where they were computed (revision + design hash) and the
    stage's results. Equal across revisions that reused the stage; different once it is recomputed."""
    info, origin = rev_info(root, rev), stage_origin(root, rev, stage)
    oinfo = rev_info(root, origin) if origin else None
    if info is None or oinfo is None:
        return None
    return hashlib.sha256(f"{origin}|{oinfo['design_sha256']}|{info['sections'].get(stage)}".encode()).hexdigest()[:32]


def stage_hashes(root: str, rev: str, stages=ORDER) -> dict:
    return {s: stage_hash(root, rev, s) for s in stages}


def _ledger(root: str) -> dict:
    """rev -> the ledger's revision entries for it (who made it), cached by the ledger's size and mtime."""
    p = os.path.join(root, "ledger.jsonl")
    try:
        st = os.stat(p)
    except OSError:
        return {}
    key = os.path.normcase(os.path.abspath(p))
    hit = _LEDGER.get(key)
    if hit and hit[0] == (st.st_mtime_ns, st.st_size):
        return hit[1]
    out = {}
    for e in read_jsonl(p):
        if e.get("type") == "revision" and isinstance(e.get("rev"), str):
            out.setdefault(e["rev"], []).append({"by": e.get("by"), "proposed_by": e.get("proposed_by"), "ts": e.get("ts"),
                                                "sha": design_sha256(e["design"]) if isinstance(e.get("design"), dict) else None})
    _LEDGER[key] = ((st.st_mtime_ns, st.st_size), out)
    return out


def authors(root: str, rev: str) -> set:
    """Who made a revision (ledger first, then results.json): for an applied change request its author
    (`proposed_by`), since applying it adds nothing of the applier's, the way merging a pull request doesn't make
    you its author; otherwise `by`. Unknown (a launcher rebuild, a solo Studio) is an empty set."""
    info = rev_info(root, rev)
    if info is None:
        return set()
    entries = _ledger(root).get(rev) or []
    match = [e for e in entries if e["sha"] == info["design_sha256"]] or entries
    src = match[-1] if match else {"by": info.get("by"), "proposed_by": info.get("proposed_by")}
    who = src.get("proposed_by") or src.get("by")
    return {who} if isinstance(who, str) and who else set()


def discipline_authors(root: str, rev: str, discipline: str) -> set:
    """The authors of the work a discipline reviews: whoever made the revisions that computed its stages."""
    out = set()
    for s in STAGES.get(discipline, ()):
        o = stage_origin(root, rev, s)
        if o:
            out |= authors(root, o)
    return out


def rev_by(root: str, rev: str) -> dict:
    info = rev_info(root, rev) or {}
    entries = _ledger(root).get(rev) or []
    src = entries[-1] if entries else {}
    return {"by": src.get("by") or info.get("by"), "proposed_by": src.get("proposed_by") or info.get("proposed_by"),
            "ts": src.get("ts")}


# ------------------------------------------------------------------ comments


def _contained(root: str, rel: str) -> str | None:
    if not isinstance(rel, str) or not rel or len(rel) > 240 or any(c in rel for c in "\\:\x00"):
        return None
    parts = rel.split("/")
    if any(not p or p.startswith(".") for p in parts):
        return None
    full = os.path.realpath(os.path.join(root, *parts))
    base = os.path.realpath(root)
    try:
        inside = os.path.normcase(os.path.commonpath([base, full])) == os.path.normcase(base)
    except ValueError:
        return None
    return full if inside and full != base else None


def _key_exists(root: str, info: dict, kind: str, key) -> bool:
    if kind == "stage":
        return key in ORDER
    if kind == "param":
        return key in info["flat"]
    if kind == "bom":
        return key in info["bom"]
    if kind == "file":
        full = _contained(os.path.join(root, info["rev"]), key)
        return bool(full and os.path.isfile(full))
    if kind == "rev":
        return key == info["rev"]
    return False


def _file_sig(root: str, rev: str, rel: str):
    full = _contained(os.path.join(root, rev), rel)
    try:
        st = os.stat(full)
    except (OSError, TypeError):
        return None
    if st.st_size > 8 << 20:
        return st.st_size
    h = hashlib.sha1()
    with open(full, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _anchor_label(info: dict, kind: str, key) -> str:
    if kind == "stage":
        return STAGE_TITLES.get(key, key)
    if kind == "param":
        return (_PARAMS.get(key) or (key,))[0]
    if kind == "bom":
        return str((info["bom"].get(key) or {}).get("item") or key)[:120]
    if kind == "file":
        return key
    return f"{info['rev']}" + (f": {info['label']}" if info.get("label") else "")


def make_anchor(root: str, rev: str, kind: str = "rev", key=None, item=None, category=None) -> dict:
    """{rev, design_sha256, kind, key, label} for a comment, checked against that revision."""
    info = rev_info(root, rev)
    if info is None:
        raise CollabError(404, f"There is no completed revision {str(rev)[:12]} in this project.")
    if kind not in KINDS:
        raise CollabError(400, "Comment on a revision, stage, BOM line, parameter or file.")
    key = rev if kind == "rev" else key
    if kind == "bom" and key is None and isinstance(item, str):  # the browser names the row; the server derives its key
        key = bom_key(category, item)
    if not isinstance(key, str) or not _key_exists(root, info, kind, key):
        raise CollabError(400, f"{rev} has no {'BOM line' if kind == 'bom' else kind} {str(key)[:60]!r} to comment on.")
    return {"rev": rev, "design_sha256": info["design_sha256"], "kind": kind, "key": key, "label": _anchor_label(info, kind, key)}


def mentions(text: str, members) -> list:
    """Member ids @mentioned in text: @name with spaces removed (case-insensitive), or a unique first name."""
    ms = [m for m in (members.values() if isinstance(members, dict) else members or []) if not m.get("revoked")]
    handles, first = {}, collections.defaultdict(list)
    for m in ms:
        name = str(m.get("name") or "")
        handles["".join(name.split()).casefold()] = m["id"]
        if name.split():
            first[name.split()[0].casefold()].append(m["id"])
    out = []
    for tok in MENTION_RX.findall(text or ""):
        t = tok.rstrip(".-").casefold()
        mid = handles.get(t) or (first[t][0] if len(first.get(t, [])) == 1 else None)
        if mid and mid not in out:
            out.append(mid)
    return out


def add_comment(root: str, by: str, body, anchor: dict | None = None, parent: str | None = None, members=None) -> dict:
    """A comment (or a reply when `parent` is a comment id; replies share their thread's anchor)."""
    body = _text(body, "comment", MAX_BODY)
    with locked(project_dir(root)):
        state = _comment_state(root)
        if parent:
            p = state.get(parent)
            if p is None:
                raise CollabError(404, "That comment no longer exists.")
            root_id = p.get("parent") or p["id"]
            anc = state[root_id]["anchor"]
        else:
            if not isinstance(anchor, dict):
                raise CollabError(400, "Say what the comment is about: {rev, kind, key}.")
            anc = make_anchor(root, anchor.get("rev"), anchor.get("kind") or "rev", anchor.get("key"), anchor.get("item"),
                              anchor.get("category"))
            root_id = None
        c = {"id": new_id("c", 5), "anchor": anc, "body": body, "by": by, "ts": utc(), "parent": root_id,
             "mentions": mentions(body, members) if members else []}
        append_jsonl(_file(root, "comments.jsonl"), {"op": "add", **c})
    return {**c, "resolved": False}


def _comment_state(root: str) -> "collections.OrderedDict":
    state = collections.OrderedDict()
    for op in read_jsonl(_file(root, "comments.jsonl")):
        if op.get("op") == "add" and isinstance(op.get("id"), str):
            c = {k: v for k, v in op.items() if k != "op"}
            c.update(resolved=False, resolved_by=None, resolved_ts=None)
            state[c["id"]] = c
        elif op.get("op") == "resolve" and op.get("id") in state:
            state[op["id"]].update(resolved=bool(op.get("resolved", True)), resolved_by=op.get("by"), resolved_ts=op.get("ts"))
    return state


def resolve_comment(root: str, cid: str, by: str, resolved: bool = True) -> dict:
    with locked(project_dir(root)):
        state = _comment_state(root)
        c = state.get(cid)
        if c is None:
            raise CollabError(404, "That comment no longer exists.")
        rid = c.get("parent") or cid
        append_jsonl(_file(root, "comments.jsonl"), {"op": "resolve", "id": rid, "by": by, "ts": utc(), "resolved": bool(resolved)})
        state[rid].update(resolved=bool(resolved), resolved_by=by, resolved_ts=utc())
        return dict(state[rid])


def comment(root: str, cid: str) -> dict | None:
    c = _comment_state(root).get(cid)
    return dict(c) if c else None


def comments(root: str, rev: str | None = None, kind: str | None = None, key=None, include_resolved: bool = True) -> list:
    """Threads in time order. With `rev`: comments made on that revision plus those carried forward from its
    ancestors while their anchor still exists there (a BOM line that is gone stops them), each marked
    carried/outdated. Revision-level comments stay on their revision."""
    state = _comment_state(root)
    target = rev_info(root, rev) if rev else None
    if rev and target is None:
        return []
    anc = set(ancestors(root, rev)) if rev else set()
    keep = {}
    for c in state.values():
        if c.get("parent"):
            continue
        a = c.get("anchor") or {}
        if kind and a.get("kind") != kind or key is not None and a.get("key") != key:
            continue
        if not include_resolved and c.get("resolved"):
            continue
        if rev is None:
            keep[c["id"]] = {**c, "carried": False, "outdated": False}
            continue
        ainfo = rev_info(root, a.get("rev"))
        if ainfo is None or a.get("rev") not in anc or ainfo["design_sha256"] != a.get("design_sha256"):
            continue
        if a.get("rev") == rev:
            keep[c["id"]] = {**c, "carried": False, "outdated": False}
        elif a.get("kind") != "rev" and _key_exists(root, target, a.get("kind"), a.get("key")):
            keep[c["id"]] = {**c, "carried": True, "outdated": _outdated(root, ainfo, target, a)}
    out = []
    for c in state.values():
        if c["id"] in keep:
            out.append(keep[c["id"]])
        elif c.get("parent") in keep:
            out.append({**c, "carried": keep[c["parent"]]["carried"], "outdated": keep[c["parent"]]["outdated"]})
    return out


def _outdated(root: str, ainfo: dict, target: dict, a: dict) -> bool:
    k, key = a.get("kind"), a.get("key")
    if k == "stage":
        return stage_origin(root, target["rev"], key) != stage_origin(root, ainfo["rev"], key)
    if k == "param":
        return target["flat"].get(key) != ainfo["flat"].get(key)
    if k == "bom":
        return (target["bom"].get(key) or {}).get("hash") != (ainfo["bom"].get(key) or {}).get("hash")
    if k == "file":
        return _file_sig(root, target["rev"], key) != _file_sig(root, ainfo["rev"], key)
    return False


# ------------------------------------------------------------------ change requests (proposals)


def validate_changes(changes) -> dict:
    """{dotted design key: value}: known keys, scalar values, editor ranges and options for Parameters-panel keys.
    Model output and pasted JSON go through here before they can touch a design."""
    if changes is None:
        return {}
    if not isinstance(changes, dict):
        raise CollabError(400, "Send the changes as {parameter: value}.")
    if len(changes) > MAX_CHANGES:
        raise CollabError(400, f"A change request can change {MAX_CHANGES} parameters at most.")
    out = {}
    for k, v in changes.items():
        if not isinstance(k, str) or k not in _DEFAULTS:
            raise CollabError(400, f"{str(k)[:60]} is not a design parameter.")
        if isinstance(v, bool) or not isinstance(v, (str, int, float)) or (isinstance(v, float) and not math.isfinite(v)):
            raise CollabError(400, f"{k}: send a single number or option.")
        if isinstance(v, str) and len(v) > 200:
            raise CollabError(400, f"{k}: that value is too long.")
        like = _DEFAULTS[k]
        if k == "arm.source" and v != "" and not (isinstance(v, str) and ROBOT_SOURCE.match(v)):
            raise CollabError(400, "arm.source names a robot folder such as so101, never a path.")
        if k in ("world.id", "world.sha"):     # World mode names its room by id and pins it by hash, never by a path
            from ..security import DESIGN_TEXT
            if not (isinstance(v, str) and DESIGN_TEXT[k].fullmatch(v)):
                raise CollabError(400, f"{k} must be a world id (lower-case letters, digits and -) or its hash, never a path.")
        if k in _PARAMS:
            label, typ, extra = _PARAMS[k]
            if typ == "select":
                hit = next((o for o in extra.get("options", []) if v == o or str(v) == str(o)), None)
                if hit is None:
                    raise CollabError(400, f"{label}: choose one of " + ", ".join(str(o) or "(none)" for o in extra.get("options", [])) + ".")
                out[k] = hit
                continue
            try:
                num = float(v)
            except ValueError:
                raise CollabError(400, f"{label}: send a number.") from None
            lo, hi = extra.get("min", -math.inf), extra.get("max", math.inf)
            if not (math.isfinite(num) and lo <= num <= hi):
                raise CollabError(400, f"{label} must be between {lo:g} and {hi:g}{(' ' + extra['unit']) if extra.get('unit') else ''}.")
            if isinstance(like, int) and not float(num).is_integer():
                raise CollabError(400, f"{label} must be a whole number.")
            out[k] = int(num) if isinstance(like, int) else num
            continue
        if isinstance(like, str):
            out[k] = str(v)
        else:
            try:
                num = float(v)
            except ValueError:
                raise CollabError(400, f"{k}: send a number.") from None
            if not math.isfinite(num):
                raise CollabError(400, f"{k}: send a finite number.")
            if isinstance(like, int) and not float(num).is_integer():
                raise CollabError(400, f"{k} must be a whole number.")
            out[k] = int(num) if isinstance(like, int) else num
    try:
        Design().with_changes(out)
    except (KeyError, ValueError, TypeError) as exc:
        raise CollabError(400, f"Those changes don't make a valid design ({exc}).") from None
    return out


def _design(root: str, rev: str | None) -> Design:
    info = rev_info(root, rev) if rev else None
    if rev and info is None:
        raise CollabError(404, f"There is no completed revision {str(rev)[:12]} in this project.")
    return Design.from_dict(info["design"]) if info else Design()


def preview(root: str, changes=None, base_rev: str | None = None, stages=(), branch: str = "main") -> dict:
    """What a change would do, without side effects: the diff against the base revision, the stages it recomputes
    (pipeline2's own graph), the disciplines that own them and the resulting design hash."""
    changes = validate_changes(changes)
    stages = [s for s in (stages or []) if s in ORDER]
    base_rev = base_rev or head(root, branch)
    old = _design(root, base_rev)
    try:
        new = old.with_changes(changes)
    except (KeyError, ValueError, TypeError) as exc:
        raise CollabError(400, f"Couldn't apply those changes ({exc}).") from None
    d = {k: [a, b] for k, (a, b) in diff(old, new).items()}
    st = affected_stages(list(d), stages)
    from .impact import impact
    return {"base_rev": base_rev, "branch": branch, "changes": d, "noop": not d and not stages, "stages": st,
            "stage_titles": [STAGE_TITLES[s] for s in st], "disciplines": disciplines_for_stages(st),
            "design_sha256": design_sha256(new.to_dict()), "level": new.build.config if new.build.config in LEVELS else "prototype",
            "shown": shown_changes(d), "impact": impact(root, base_rev, {k: v[1] for k, v in d.items()}, stages) if d else None}


def _proposal_state(root: str) -> "collections.OrderedDict":
    state = collections.OrderedDict()
    for op in read_jsonl(_file(root, "proposals.jsonl")):
        kind, pid = op.get("op"), op.get("id")
        if kind == "open" and isinstance(pid, str):
            state[pid] = {k: v for k, v in op.items() if k != "op"}
            state[pid].setdefault("status", "open")
            continue
        p = state.get(pid)
        if p is None:
            continue
        if kind == "apply":
            p.update(status="applied", applied_by=op.get("by"), applied_ts=op.get("ts"), job=op.get("job"), last_error=None)
        elif kind == "done" and p.get("status") == "applied":
            if op.get("error"):
                p.update(status="open", last_error=op.get("error"), applied_rev=None)
            else:
                p.update(status="applied", applied_rev=op.get("rev"))
        elif kind == "close":
            p.update(status="closed", closed_by=op.get("by"), closed_ts=op.get("ts"), close_reason=op.get("reason") or "")
    return state


def proposals(root: str, status: str | None = None) -> list:
    """Newest first."""
    out = [p for p in _proposal_state(root).values() if status is None or p.get("status") == status]
    return sorted(out, key=lambda p: -int(p.get("n") or 0))


def get_proposal(root: str, pid) -> dict | None:
    p = _proposal_state(root).get(pid) if isinstance(pid, str) else None
    return dict(p) if p else None


def _title(changes: dict, stages) -> str:
    if changes:
        k, v = next(iter(changes.items()))
        more = f" (+{len(changes) - 1} more)" if len(changes) > 1 else ""
        return f"{(_PARAMS.get(k) or (k,))[0]} to {show_value(k, v)}{more}"[:MAX_TITLE]
    return ("Change " + ", ".join(STAGE_TITLES.get(s, s) for s in stages))[:MAX_TITLE]


def shown_changes(diff_: dict) -> list:
    """A change request's diff for people: [{key, label, old, new}] with parameter names and readable values."""
    return [{"key": k, "label": show_key(k), "old": show_value(k, ab[0]), "new": show_value(k, ab[1])}
            for k, ab in (diff_ or {}).items() if isinstance(ab, (list, tuple)) and len(ab) == 2]


def create_proposal(root: str, by: str, title=None, changes=None, base_rev: str | None = None, branch: str = "main",
                    stages=(), note: str = "", members=None, policy=None, source: dict | None = None) -> dict:
    """Open a change request (a pull request): its diff against the base revision, the affected stages and
    disciplines, and a review request routed to the owners of those disciplines (never the author)."""
    if branch != "main" and branch not in _branch_doc(root)["branches"]:
        raise CollabError(404, f"There is no branch {str(branch)[:40]!r}.")
    pv = preview(root, changes, base_rev, stages, branch)
    if pv["noop"]:
        raise CollabError(400, "That changes nothing in the design. Name a parameter to change, or the stage the change is about.")
    title = _text(title, "title", MAX_TITLE, multiline=False) if title else _title({k: v[1] for k, v in pv["changes"].items()}, pv["stages"])
    note = _text(note, "note", MAX_BODY) if note else ""
    with locked(project_dir(root)):
        state = _proposal_state(root)
        n = max([int(p.get("n") or 0) for p in state.values()] + [0]) + 1
        base = rev_info(root, pv["base_rev"]) if pv["base_rev"] else None
        p = {"id": f"p{n}", "n": n, "title": title, "note": note, "changes": {k: v[1] for k, v in pv["changes"].items()},
             "diff": pv["changes"], "stages": pv["stages"], "explicit_stages": [s for s in (stages or []) if s in ORDER],
             "disciplines": pv["disciplines"], "base_rev": pv["base_rev"], "base_sha256": base["design_sha256"] if base else None,
             "design_sha256": pv["design_sha256"], "level": pv["level"], "branch": branch, "by": by, "ts": utc(),
             "status": "open", "source": source or None, "mentions": mentions(note, members) if (note and members) else [],
             "impact": pv.get("impact")}
        append_jsonl(_file(root, "proposals.jsonl"), {"op": "open", **p})
        rv = request_review(root, by, proposal=p["id"], members=members, policy=policy, _proposal=p)
    p["review"] = rv["id"]
    return p


def built_summary(root: str, p: dict) -> dict | None:
    """What an applied change request's revision actually built, next to its base revision: {rev, base_rev, feasible,
    issues, min_torque_margin: [base, built], unit_cost: [base, built], tooling_cost: [base, built], quantity: [base,
    built]}; None until it is built. A unit cost includes its mold tooling spread over the build quantity."""
    rev, base = p.get("applied_rev"), p.get("base_rev")
    a = rev_info(root, rev) if rev else None
    if a is None:
        return None
    i0 = (rev_info(root, base) or {}) if base else {}
    s0, s1 = i0.get("summary") or {}, a["summary"]
    return {"rev": rev, "base_rev": base, "feasible": s1.get("feasible"), "issues": len(s1.get("issues") or []),
            "min_torque_margin": [s0.get("min_torque_margin"), s1.get("min_torque_margin")],
            "unit_cost": [s0.get("unit_cost"), s1.get("unit_cost")],
            "tooling_cost": [s0.get("tooling_cost"), s1.get("tooling_cost")],
            "quantity": [(i0.get("flat") or {}).get("build.quantity"), (a.get("flat") or {}).get("build.quantity")]}


def proposal_name(root: str, pid) -> str:
    """'change request #3: Swap to Dynamixel' (or the id when it is gone)."""
    p = get_proposal(root, pid) or {}
    return f"change request #{p['n']}: {p.get('title')}" if p.get("n") else f"change request {pid}"


def head_conflicts(root: str, p: dict) -> list:
    """Keys the change request changes that its branch also changed since the request's base revision."""
    h = head(root, p.get("branch") or "main")
    if not h or not p.get("base_rev") or h == p.get("base_rev"):
        return []
    moved = diff(_design(root, p["base_rev"]), _design(root, h))
    return [k for k, v in (p.get("changes") or {}).items() if k in moved and moved[k][1] != v]


def apply_proposal(root: str, pid: str, by: str, members=None) -> dict:
    """Mark an open change request applied and return the job spec for the room's queue: the edit is built by the
    applier (`by`) on behalf of the author (`proposed_by`)."""
    with locked(project_dir(root)):
        p = _proposal_state(root).get(pid)
        if p is None:
            raise CollabError(404, "No such change request.")
        if p.get("status") != "open":
            raise CollabError(409, f"Change request #{p.get('n')} is already {p.get('status')}.")
        if not p.get("changes"):
            raise CollabError(409, "This change request names stages but no design parameters: make the change by hand, "
                                   "then close it.")
        names = {m.get("id"): m.get("name") for m in (members.values() if isinstance(members, dict) else members or [])}
        job = {"kind": "proposal", "proposal": pid, "n": p.get("n"), "changes": dict(p["changes"]), "base_rev": p.get("base_rev"),
               "head_rev": head(root, p.get("branch") or "main"), "branch": p.get("branch") or "main",
               "label": f"#{p.get('n')} {p.get('title')}"[:48], "by": by, "proposed_by": p.get("by"),
               "design_sha256": p.get("design_sha256"), "conflicts": head_conflicts(root, p), "source": "proposal",
               "text": f"Change request #{p.get('n')} from {names.get(p.get('by')) or 'a teammate'}: {p.get('title')}"}
        append_jsonl(_file(root, "proposals.jsonl"), {"op": "apply", "id": pid, "by": by, "ts": utc(),
                                                      "job": {k: job[k] for k in ("base_rev", "head_rev", "branch", "conflicts")}})
    return job


def proposal_done(root: str, pid: str, rev: str | None = None, error: str | None = None) -> dict | None:
    """The queue reports how an applied change request ended: the revision it built, or an error (which reopens it)."""
    with locked(project_dir(root)):
        if pid not in _proposal_state(root):
            return None
        append_jsonl(_file(root, "proposals.jsonl"), {"op": "done", "id": pid, "rev": rev if isinstance(rev, str) else None,
                                                      "error": str(error)[:300] if error else None, "ts": utc()})
    return get_proposal(root, pid)


def close_proposal(root: str, pid: str, by: str, reason: str = "") -> dict:
    reason = _text(reason, "reason", 500) if reason else ""
    with locked(project_dir(root)):
        p = _proposal_state(root).get(pid)
        if p is None:
            raise CollabError(404, "No such change request.")
        if p.get("status") != "open":
            raise CollabError(409, f"Change request #{p.get('n')} is already {p.get('status')}.")
        append_jsonl(_file(root, "proposals.jsonl"), {"op": "close", "id": pid, "by": by, "ts": utc(), "reason": reason})
    return get_proposal(root, pid)


# ------------------------------------------------------------------ reviews


def _review_ops(root: str) -> list:
    return read_jsonl(_file(root, "reviews.jsonl"))


def _reviews_state(root: str) -> "collections.OrderedDict":
    state = collections.OrderedDict()
    for op in _review_ops(root):
        kind = op.get("op")
        if kind == "request" and isinstance(op.get("id"), str):
            state[op["id"]] = {**{k: v for k, v in op.items() if k != "op"}, "decisions": [], "withdrawn": False}
        elif kind == "decide" and op.get("review") in state:
            state[op["review"]]["decisions"].append({k: v for k, v in op.items() if k not in ("op", "review")})
        elif kind == "withdraw" and op.get("review") in state:
            state[op["review"]].update(withdrawn=True, withdrawn_by=op.get("by"), withdrawn_ts=op.get("ts"))
    return state


def _members_dict(members) -> dict:
    if isinstance(members, dict):
        return members
    return {m.get("id"): m for m in members or [] if isinstance(m, dict)}


def gate_missing(root: str, rev: str, members=None, policy=None) -> list:
    g = release_gate(root, rev, members, policy)
    return [d["discipline"] for d in g["disciplines"] if d["state"] != "approved"]


def request_review(root: str, by: str, rev: str | None = None, proposal: str | None = None, disciplines=None, note: str = "",
                   members=None, policy=None, _proposal: dict | None = None) -> dict:
    """Ask the owners of the affected disciplines to review a change request or a revision. For a revision the
    default is every discipline whose approval isn't current yet (all of them if none is missing)."""
    members = _members_dict(members)
    if (rev is None) == (proposal is None):
        raise CollabError(400, "Review either a revision or a change request.")
    note = _text(note, "note", MAX_BODY) if note else ""
    if proposal is not None:
        p = _proposal or get_proposal(root, proposal)
        if p is None:
            raise CollabError(404, "No such change request.")
        target = {"kind": "proposal", "proposal": proposal, "rev": None}
        ds, sha, auth, level = list(p.get("disciplines") or []), p.get("design_sha256"), {p.get("by")}, p.get("level") or "prototype"
    else:
        info = rev_info(root, rev)
        if info is None:
            raise CollabError(404, f"There is no completed revision {str(rev)[:12]} in this project.")
        target = {"kind": "revision", "proposal": None, "rev": rev}
        ds = gate_missing(root, rev, members, policy) or list(DISCIPLINES)
        sha, auth, level = info["design_sha256"], authors(root, rev), level_of(info)
    if disciplines:
        ds = [d for d in DISCIPLINES if d in set(disciplines) & set(DISCIPLINES)] or ds
    routes = [route_discipline(d, auth, members) for d in ds]
    r = {"id": new_id("v", 5), "target": target, "design_sha256": sha, "level": level, "disciplines": ds,
         "requested_from": [{"discipline": x["discipline"], "via": x["via"], "members": [m["id"] for m in x["members"]]} for x in routes],
         "authors": sorted(a for a in auth if a), "by": by, "ts": utc(), "note": note}
    with locked(project_dir(root)):
        append_jsonl(_file(root, "reviews.jsonl"), {"op": "request", **r})
    return {**r, "decisions": [], "withdrawn": False, "state": "open"}


def work_authors(root: str, r: dict) -> dict:
    """{discipline: who made the work it reviews} for a stored review: for a revision, per discipline, whoever made the
    revisions that computed its stages; for a change request, its author."""
    t, ds = r.get("target") or {}, r.get("disciplines") or []
    if t.get("kind") == "revision" and t.get("rev") and rev_info(root, t["rev"]) is not None:
        return {d: discipline_authors(root, t["rev"], d) or set(r.get("authors") or []) for d in ds}
    return {d: set(r.get("authors") or []) for d in ds}


def _names(ms) -> str:
    out = []
    for m, via in ms:
        out.append(f"{m.get('name') or 'a former member'}" + (f" ({via})" if via in ("generalist", "owner") else ""))
    return " or ".join(out) if len(out) < 3 else ", ".join(out[:-1]) + " or " + out[-1]


def decision_scope(root: str, r: dict, member: dict | None, members=None, policy=None) -> dict:
    """What `member` may decide on review `r`: {"covers": the disciplines their approval counts for, "authored": the ones
    whose work they made (above prototype nobody reviews their own work, as on GitHub), "why": why they can't decide, or
    None}. The server checks it on every decision and the page shows the same buttons and reasons."""
    members = _members_dict(members)
    level, ds = r.get("level") or "prototype", list(r.get("disciplines") or [])
    mid, work = (member or {}).get("id"), work_authors(root, r)
    elig = [d for d in ds if eligible(member, d, members, policy, level, work.get(d, ()))]
    authored = [d for d in elig if level != "prototype" and mid in work.get(d, ())]
    covers = [d for d in elig if d not in authored]
    why = None
    if not covers:
        label = lambda xs: ", ".join(LABELS[d] for d in xs)  # noqa: E731
        if not member or member.get("revoked"):
            why = "Sign in again to review."
        elif member.get("role") == "viewer" and not (policy or {}).get("viewers_can_approve"):
            why = "Viewers can't approve on this team (an owner can allow it under Share, Policy)."
        elif authored:
            others = []
            for d in authored:
                for m in members.values():
                    via = eligible(m, d, members, policy, level, work.get(d, ()))
                    if via and not m.get("revoked") and m.get("id") not in work.get(d, ()) and m not in [x for x, _ in others]:
                        others.append((m, via))
            why = (f"You made this change, so at {level} your approval doesn't count for {label(authored)}: "
                   + (f"{_names(others)} can approve it." if others else
                      "nobody else on the team can yet. An owner can add someone with that discipline in Share."))
        else:
            tags = [LABELS.get(t, t) for t in (member or {}).get("disciplines") or []]
            why = (f"This review asks for {label(ds)}; your disciplines ({', '.join(tags) or 'none yet'}) don't cover "
                   f"{'it' if len(ds) == 1 else 'them'}.")
    return {"covers": covers, "authored": authored, "why": why}


def _summarize(r: dict, members: dict, policy, work: dict | None = None) -> dict:
    """A review's state for the UI: per discipline, the latest decision of each eligible reviewer (above prototype, never
    one by whoever made that discipline's work)."""
    latest = {}
    for dec in r.get("decisions") or []:
        for d in dec.get("disciplines") or []:
            latest[(dec.get("by"), d)] = dec
    per, state = [], "open"
    authors_ = set(r.get("authors") or [])
    level = r.get("level") or "prototype"
    for d in r.get("disciplines") or []:
        wa = set((work or {}).get(d) or authors_)
        appr = [b for (b, dd), dec in latest.items() if dd == d and dec.get("decision") == "approve"
                and eligible(members.get(b), d, members, policy, level, wa) and (level == "prototype" or b not in wa)]
        chg = [b for (b, dd), dec in latest.items() if dd == d and dec.get("decision") == "request_changes"
               and eligible(members.get(b), d, members, policy, level, wa) and (level == "prototype" or b not in wa)]
        per.append({"discipline": d, "label": LABELS[d], "approved_by": appr, "changes_by": chg,
                    "state": "changes_requested" if chg else "approved" if appr else "waiting"})
    if r.get("withdrawn"):
        state = "withdrawn"
    elif any(x["changes_by"] for x in per):
        state = "changes_requested"
    elif per and all(x["approved_by"] for x in per):
        state = "approved"
    return {**r, "state": state, "per_discipline": per}


def reviews(root: str, rev: str | None = None, proposal: str | None = None, state: str | None = None, members=None,
            policy=None) -> list:
    """Newest first, each with its computed state and per-discipline summary."""
    members = _members_dict(members)
    out = []
    for r in _reviews_state(root).values():
        t = r.get("target") or {}
        if rev and t.get("rev") != rev or proposal and t.get("proposal") != proposal:
            continue
        s = _summarize(r, members, policy, work_authors(root, r))
        if state is None or s["state"] == state:
            out.append(s)
    return list(reversed(out))


def get_review(root: str, review_id: str, members=None, policy=None) -> dict | None:
    r = _reviews_state(root).get(review_id)
    return _summarize(r, _members_dict(members), policy, work_authors(root, r)) if r else None


def decide(root: str, review_id: str, member: dict, decision: str, note: str = "", disciplines=None, members=None,
           policy=None) -> dict:
    """Approve or request changes, as every requested discipline the member owns (or a chosen subset). Recorded
    against the exact revision: its design hash and the artifact hashes of the covered disciplines' stages."""
    members = _members_dict(members)
    if decision not in DECISIONS:
        raise CollabError(400, "Decide approve or request_changes.")
    note = _text(note, "note", MAX_BODY) if note else ""
    with locked(project_dir(root)):
        r = _reviews_state(root).get(review_id)
        if r is None:
            raise CollabError(404, "No such review.")
        if r.get("withdrawn"):
            raise CollabError(409, "That review was withdrawn.")
        t = r.get("target") or {}
        if t.get("kind") == "proposal":
            p = get_proposal(root, t.get("proposal")) or {}
            if p.get("status") == "closed":
                raise CollabError(409, f"Change request #{p.get('n')} is closed.")
        scope = decision_scope(root, r, member, members, policy)
        mine = scope["covers"]
        if not mine:
            raise CollabError(403, scope["why"] or "You can't decide on this review.")
        if disciplines:
            want = [d for d in disciplines if isinstance(d, str)]
            if not set(want) <= set(mine):
                raise CollabError(403, "You can only decide for " + ", ".join(LABELS[d] for d in mine) + " here.")
            mine = [d for d in mine if d in want]
        rev = t.get("rev")
        if t.get("kind") == "proposal":
            p = get_proposal(root, t.get("proposal")) or {}
            ar = p.get("applied_rev")
            rev = ar if ar and (rev_info(root, ar) or {}).get("design_sha256") == r.get("design_sha256") else None
        elif (rev_info(root, rev) or {}).get("design_sha256") != r.get("design_sha256"):
            raise CollabError(409, f"{rev} is no longer the revision this review was for. Request a new review.")
        hashes = {s: stage_hash(root, rev, s) for d in mine for s in STAGES[d]} if rev else {}
        dec = {"by": member.get("id"), "decision": decision, "disciplines": mine, "rev": rev, "design_sha256": r.get("design_sha256"),
               "stage_hashes": hashes, "note": note, "ts": utc()}
        append_jsonl(_file(root, "reviews.jsonl"), {"op": "decide", "review": review_id, **dec})
    return get_review(root, review_id, members, policy)


def withdraw_review(root: str, review_id: str, by: str) -> dict:
    with locked(project_dir(root)):
        r = _reviews_state(root).get(review_id)
        if r is None:
            raise CollabError(404, "No such review.")
        append_jsonl(_file(root, "reviews.jsonl"), {"op": "withdraw", "review": review_id, "by": by, "ts": utc()})
    return get_review(root, review_id)


# ------------------------------------------------------------------ the release gate


def current_decisions(root: str, rev: str) -> list:
    """Every decision that still applies to `rev`, one record per discipline, in time order. A decision applies when
    it was made on `rev` or an ancestor with the same design hash, and every stage of the discipline is unchanged
    since (same artifacts: reused, not recomputed). Change-request decisions apply to the first revision built
    from the approved design."""
    anc = ancestors(root, rev)
    if not anc:
        return []
    sha = {r: rev_info(root, r)["design_sha256"] for r in anc}
    now_hash, out = {}, []
    state = _reviews_state(root)
    withdrawn = {r["id"] for r in state.values() if r.get("withdrawn")}
    built = {pid: p.get("applied_rev") for pid, p in _proposal_state(root).items() if p.get("applied_rev")}
    for op in _review_ops(root):
        if op.get("op") != "decide" or op.get("review") in withdrawn or op.get("decision") not in DECISIONS:
            continue
        bound = op.get("rev") or built.get(((state.get(op.get("review")) or {}).get("target") or {}).get("proposal"))
        if bound:
            anchor = bound if bound in sha and sha[bound] == op.get("design_sha256") else None
        else:  # a change request approved before it was built: the nearest revision built from that exact design
            anchor = next((r for r in anc if sha[r] == op.get("design_sha256")), None)
        if anchor is None:
            continue
        for d in op.get("disciplines") or []:
            if d not in STAGES:
                continue
            ok = True
            for s in STAGES[d]:
                if (rev, s) not in now_hash:
                    now_hash[(rev, s)] = stage_hash(root, rev, s)
                then = stage_hash(root, anchor, s)
                rec = (op.get("stage_hashes") or {}).get(s)
                if then is None or now_hash[(rev, s)] != then or (rec is not None and rec != then):
                    ok = False
                    break
            if ok:
                out.append({"by": op.get("by"), "decision": op["decision"], "disciplines": [d], "anchor": anchor,
                            "carried": anchor != rev, "ts": op.get("ts"), "note": op.get("note") or "",
                            "authors": sorted(discipline_authors(root, rev, d))})
    return out


def _pub(m: dict | None, mid=None, via=None) -> dict:
    m = m or {}
    out = {"id": m.get("id") or mid, "name": m.get("name") or "former member", "color": m.get("color")}
    if via:
        out["via"] = via
    return out


def can_release(level: str, decisions, members, *, authors=(), required=None, policy=None, work_authors=None) -> dict:
    """The release gate as a pure function.

    level: the revision's build.config. decisions: current decisions for the revision, in time order, each
    {"by", "decision": "approve" | "request_changes", "disciplines": [...], "authors"?: authors of the work it covers}.
    authors: who made the revision; work_authors: {discipline: who made the work it reviews} (default: authors).
    Rules:
      prototype   1 approval made on this revision (not only carried from an earlier one); the author may approve
      pilot       every required discipline approved by an eligible member who didn't author that work, and at
                  least 1 approver who isn't the revision's author
      production  as pilot, with at least 2 distinct approvers who aren't the author, one of them an owner
    An outstanding "request changes" from an eligible reviewer blocks at every level (above prototype, not one by whoever
    made that work: their review of it counts neither way)."""
    if level not in LEVELS:
        raise ValueError(f"unknown build level {level!r}")
    members = _members_dict(members)
    required = [d for d in (required or DISCIPLINES) if d in DISCIPLINES]
    rev_authors = {a for a in (authors or ()) if a}
    latest = {}
    for dec in decisions or []:
        for d in dec.get("disciplines") or []:
            latest[(dec.get("by"), d)] = dec
    rows, counted, blockers, any_ok = [], set(), [], set()
    for d in required:
        appr, chg = [], []
        work_d = set((work_authors or {}).get(d, rev_authors) or ())
        for (b, dd), dec in latest.items():
            if dd != d:
                continue
            m = members.get(b)
            work = set(dec["authors"]) if dec.get("authors") is not None else work_d
            via = eligible(m, d, members, policy, level, work)
            if not via or (level != "prototype" and b in work):  # above prototype nobody's review of their own work counts
                continue
            if dec.get("decision") == "request_changes":
                chg.append(_pub(m, b, via))
            else:
                appr.append({**_pub(m, b, via), "anchor": dec.get("anchor"), "carried": bool(dec.get("carried"))})
        if level == "prototype":  # someone looked at this revision: an approval carried from an earlier one isn't enough
            any_ok |= {a["id"] for a in appr if not a["carried"]}
        counted |= {a["id"] for a in appr}
        blockers += [(d, c) for c in chg]
        state = "changes_requested" if chg else "approved" if appr else "missing"
        rows.append({"discipline": d, "label": LABELS[d], "state": state, "approvers": appr, "requested_changes": chg,
                     "needs": [] if appr else [_pub(m, m.get("id"), eligible(m, d, members, policy, level, work_d))
                                               for m in members.values() if not m.get("revoked")
                                               and eligible(m, d, members, policy, level, work_d)
                                               and (level == "prototype" or m.get("id") not in work_d)]})
    distinct = counted - rev_authors
    owner_ok = any((members.get(b) or {}).get("role") == "owner" for b in distinct)
    missing = [r["discipline"] for r in rows if r["state"] == "missing"]
    lab = {"prototype": "prototype", "pilot": "pilot", "production": "production"}[level]
    if blockers:
        d, who = blockers[0]
        ok, short = False, f"changes requested: {LABELS[d]} ({who['name']})"
    elif level == "prototype":
        ok = bool(any_ok)
        short = "ready to release: prototype" if ok else "needs 1 approval: prototype"
    else:
        need_people = 1 if level == "pilot" else 2
        ok = not missing and len(distinct) >= need_people and (level == "pilot" or owner_ok)
        if missing:
            short = f"needs {len(missing)} approval{'s' if len(missing) != 1 else ''}: {lab}"
        elif len(distinct) < need_people:
            short = f"needs {need_people - len(distinct)} more approver{'s' if need_people - len(distinct) != 1 else ''} who didn't make the change: {lab}"
        elif level == "production" and not owner_ok:
            short = "needs an owner's approval: production"
        else:
            short = f"ready to release: {lab}"
    text = short + (" (" + ", ".join(LABELS[d] for d in missing) + ")" if missing and level != "prototype" and not blockers else "")
    return {"ok": ok, "level": level, "short": short, "text": text, "disciplines": rows, "missing": missing,
            "approvers": sorted(counted), "distinct": len(distinct), "need_distinct": {"prototype": 1, "pilot": 1, "production": 2}[level],
            "owner_ok": owner_ok if level == "production" else None, "authors": sorted(rev_authors)}


def release_gate(root: str, rev: str, members=None, policy=None) -> dict:
    """can_release for a stored revision, plus the automatic checklist from its pipeline results."""
    info = rev_info(root, rev)
    if info is None:
        raise CollabError(404, f"There is no completed revision {str(rev)[:12]} in this project.")
    members = _members_dict(members)
    work = {d: sorted(discipline_authors(root, rev, d)) for d in DISCIPLINES}
    g = can_release(level_of(info), current_decisions(root, rev), members, authors=authors(root, rev), policy=policy,
                    work_authors=work)
    s = info["summary"]
    g.update(rev=rev, design_sha256=info["design_sha256"],
             checklist={"feasible": s.get("feasible"), "issues": list(s.get("issues") or [])[:6],
                        "pick_place_success": s.get("pick_place_success"), "unit_cost": s.get("unit_cost"),
                        "tooling_cost": s.get("tooling_cost"), "quantity": (info.get("flat") or {}).get("build.quantity"),
                        "min_torque_margin": s.get("min_torque_margin")},
             released=[r["tag"] for r in releases(root) if r.get("rev") == rev and r.get("design_sha256") == info["design_sha256"]])
    return g


# ------------------------------------------------------------------ branches (variant lines)


def _branch_doc(root: str) -> dict:
    doc = read_json(_file(root, "branches.json"), {}) or {}
    if not isinstance(doc.get("branches"), dict):
        doc["branches"] = {}
    return doc


def head(root: str, branch: str = "main") -> str | None:
    """A branch's newest revision. main: the newest revision that isn't on another branch."""
    doc = _branch_doc(root)
    if branch and branch != "main":
        b = doc["branches"].get(branch)
        return b.get("head_rev") if b else None
    taken = {r for b in doc["branches"].values() for r in b.get("revs") or []}
    rs = [r for r in revs(root) if r not in taken]
    return rs[-1] if rs else None


def branches(root: str) -> list:
    doc = _branch_doc(root)
    out = [{"name": "main", "head_rev": head(root, "main"), "base_rev": None, "by": None, "ts": None, "revs": []}]
    for name in sorted(doc["branches"]):
        b = doc["branches"][name]
        out.append({"name": name, "head_rev": b.get("head_rev"), "base_rev": b.get("base_rev"), "by": b.get("by"),
                    "ts": b.get("ts"), "revs": list(b.get("revs") or [])})
    return out


def create_branch(root: str, name: str, by: str, base_rev: str | None = None) -> dict:
    if not isinstance(name, str) or not BRANCH_RX.match(name) or name == "main":
        raise CollabError(400, "Branch names are 1 to 40 lowercase letters, digits or hyphens (not main).")
    base_rev = base_rev or head(root, "main")
    if rev_info(root, base_rev) is None:
        raise CollabError(404, "Pick a completed revision to branch from.")
    with locked(project_dir(root)):
        doc = _branch_doc(root)
        if name in doc["branches"]:
            raise CollabError(409, f"There is already a branch called {name}.")
        doc["branches"][name] = {"name": name, "base_rev": base_rev, "head_rev": base_rev, "by": by, "ts": utc(), "revs": []}
        write_json(_file(root, "branches.json"), doc)
    return {**doc["branches"][name]}


def advance_branch(root: str, name: str, rev: str, by: str | None = None) -> dict:
    """Move a branch to a revision built on it (the queue calls this after a branch job)."""
    if rev_info(root, rev) is None:
        raise CollabError(404, f"There is no completed revision {str(rev)[:12]} in this project.")
    with locked(project_dir(root)):
        doc = _branch_doc(root)
        b = doc["branches"].get(name)
        if b is None:
            raise CollabError(404, f"There is no branch {str(name)[:40]!r}.")
        if b.get("head_rev") not in ancestors(root, rev):
            raise CollabError(409, f"{rev} doesn't build on {name}'s head {b.get('head_rev')}.")
        b["head_rev"] = rev
        if rev not in b.setdefault("revs", []):
            b["revs"].append(rev)
        write_json(_file(root, "branches.json"), doc)
    return dict(b)


def resolve_ref(root: str, ref) -> str | None:
    """A branch name or a revision -> a revision."""
    if not isinstance(ref, str):
        return None
    if REV_RX.match(ref):
        return ref if rev_info(root, ref) else None
    return head(root, ref)


def merge_base(root: str, a: str, b: str) -> str | None:
    seen = set(ancestors(root, a))
    return next((r for r in ancestors(root, b) if r in seen), None)


def compare(root: str, a, b) -> dict:
    """Diff of the design dicts between two refs (branch names or revisions): what changes going from a to b."""
    ra, rb = resolve_ref(root, a), resolve_ref(root, b)
    if not ra or not rb:
        raise CollabError(404, "Compare two branches or completed revisions.")
    d = {k: [x, y] for k, (x, y) in diff(_design(root, ra), _design(root, rb)).items()}
    st = affected_stages(list(d))
    return {"a": {"ref": a, "rev": ra}, "b": {"ref": b, "rev": rb}, "merge_base": merge_base(root, ra, rb), "changes": d,
            "stages": st, "disciplines": disciplines_for_stages(st)}


def merge_as_proposal(root: str, name: str, by: str, title=None, members=None, policy=None) -> dict:
    """Open a change request that brings a branch's changes (since it split from main) onto main's head. Keys main
    also changed since the split are listed as conflicts for the reviewers; the branch's values are proposed."""
    doc = _branch_doc(root)
    b = doc["branches"].get(name)
    if b is None:
        raise CollabError(404, f"There is no branch {str(name)[:40]!r}.")
    base, bh, mh = b.get("base_rev"), b.get("head_rev"), head(root, "main")
    ours = diff(_design(root, base), _design(root, bh))
    if not ours:
        raise CollabError(400, f"{name} has no design changes since it split from {base}.")
    theirs = diff(_design(root, base), _design(root, mh)) if mh else {}
    conflicts = [{"key": k, "base": v[0], "main": theirs[k][1], "branch": v[1]} for k, v in ours.items()
                 if k in theirs and theirs[k][1] != v[1]]
    main_flat = _design(root, mh).flat() if mh else {}
    changes = {k: v[1] for k, v in ours.items() if main_flat.get(k) != v[1]}
    if not changes:
        raise CollabError(400, f"main already has every change from {name}.")
    note = f"Merges branch {name} ({base} to {bh}) onto main at {mh}."
    if conflicts:
        note += " Conflicts with main: " + ", ".join(c["key"] for c in conflicts) + "."
    p = create_proposal(root, by, title or f"Merge {name} into main", changes, base_rev=mh, branch="main", note=note,
                        members=members, policy=policy, source={"branch": name, "head_rev": bh, "conflicts": conflicts})
    return p


# ------------------------------------------------------------------ releases


def releases(root: str) -> list:
    doc = read_json(_file(root, "releases.json"), {}) or {}
    rel = doc.get("releases") if isinstance(doc.get("releases"), list) else []
    return sorted([r for r in rel if isinstance(r, dict)], key=lambda r: str(r.get("ts") or ""), reverse=True)


def pinned(root: str) -> set:
    """Revisions that are released: never pruned."""
    return {r.get("rev") for r in releases(root) if r.get("rev")}


def create_release(root: str, tag, rev: str, by: str, members=None, policy=None, note: str = "", home: str | None = None) -> dict:
    """Tag a revision ("Pilot v1") once the gate for its build level passes. Raises CollabError(409) with the gate."""
    if not isinstance(tag, str) or not TAG_RX.match(" ".join(tag.split())):
        raise CollabError(400, "Release names are 1 to 40 letters, digits, spaces, dots, + or -, like \"Pilot v1\".")
    tag = " ".join(tag.split())
    note = _text(note, "note", 500) if note else ""
    members = _members_dict(members)
    with locked(project_dir(root)):
        g = release_gate(root, rev, members, policy)
        if not g["ok"]:
            raise CollabError(409, f"{rev} can't be released yet: {g['text']}.", gate=g)
        doc = read_json(_file(root, "releases.json"), {}) or {}
        rel = doc.get("releases") if isinstance(doc.get("releases"), list) else []
        if any(str(r.get("tag")).casefold() == tag.casefold() for r in rel if isinstance(r, dict)):
            raise CollabError(409, f"There is already a release called {tag}.")
        decs = [d for d in current_decisions(root, rev) if d["decision"] == "approve" and d["by"] in g["approvers"]]
        r = {"tag": tag, "rev": rev, "design_sha256": g["design_sha256"], "level": g["level"], "by": by, "ts": utc(), "note": note,
             "approvals": [{"by": d["by"], "name": (members.get(d["by"]) or {}).get("name"), "discipline": d["disciplines"][0],
                            "anchor": d["anchor"]} for d in decs], "gate": g["short"]}
        rel.append(r)
        write_json(_file(root, "releases.json"), {"releases": rel})
    audit.append("release_created", by=by, home=home, project=os.path.basename(os.path.normpath(root)), rev=rev, tag=tag, level=g["level"])
    return r


# ------------------------------------------------------------------ order downloads


def order_package(rel: str) -> bool:
    """Is this file (relative to the revision folder, '/' separators) an order package: manufacturing zips,
    Gerbers, JLC BOM/CPL files? Those unlock when the revision is released."""
    rel = (rel or "").replace("\\", "/").lstrip("/")
    return any(fnmatch.fnmatch(rel, pat) for pat in ORDER_FILES)


def download(root: str, rev: str, rel: str, member: dict | None, policy=None) -> dict:
    """{"allowed", "draft", "reason"} for a file download in team mode: order packages of an unreleased revision
    go to owners (and to editors when the policy is "warn"), logged and marked draft."""
    if not order_package(rel):
        return {"allowed": True, "draft": False, "reason": ""}
    info = rev_info(root, rev)
    if info and any(r.get("rev") == rev and r.get("design_sha256") == info["design_sha256"] for r in releases(root)):
        return {"allowed": True, "draft": False, "reason": ""}
    role = (member or {}).get("role")
    pol = (policy or {}).get("unreleased_downloads", "warn")
    if role == "owner" or (role == "editor" and pol == "warn"):
        return {"allowed": True, "draft": True, "reason": f"{rev} is not released yet: this is a draft order package."}
    return {"allowed": False, "draft": True,
            "reason": f"Order files unlock when {rev} is released. Ask for reviews in the Review tab, or ask the owner."}


# ------------------------------------------------------------------ export


def export_bundle(root: str, members=None) -> dict:
    """Comments, change requests, reviews, branches and releases for a project export, with member ids replaced by
    names (an imported copy keeps them as history only)."""
    members = _members_dict(members)

    def name(mid):
        return (members.get(mid) or {}).get("name") or ("former member" if mid else None)

    def scrub(obj):
        if isinstance(obj, dict):
            return {k: (name(v) if k in ("by", "proposed_by", "applied_by", "closed_by", "resolved_by", "withdrawn_by", "revoked_by")
                        and (v is None or isinstance(v, str)) else scrub(v))
                    for k, v in obj.items() if k not in ("mentions", "requested_from", "authors")}
        if isinstance(obj, list):
            return [scrub(x) for x in obj]
        return obj

    return {"format": "morph-team-history/1", "comments": scrub(comments(root)), "proposals": scrub(proposals(root)),
            "reviews": scrub([r for r in _reviews_state(root).values()]), "branches": scrub(branches(root)),
            "releases": scrub(releases(root))}


def revision_record(root: str, rev: str, members=None, policy=None) -> dict:
    """What a revision export carries about the team (team.json): who made the revision, its release gate, every
    discipline's approvals of this exact revision (and the revision each was made on) and the releases that tag it; names
    in place of member ids."""
    members = _members_dict(members)
    g = release_gate(root, rev, members, policy)

    def name(mid):
        return (members.get(mid) or {}).get("name") or ("former member" if mid else None)

    def person(a):
        return {"name": a.get("name"), "via": a.get("via"), "approved_on": a.get("anchor"), "carried": bool(a.get("carried"))}

    who = rev_by(root, rev)
    return {"format": "morph-team-record/1", "rev": rev, "design_sha256": g["design_sha256"], "level": g["level"],
            "release_gate": {"ok": g["ok"], "text": g["text"]}, "made_by": [name(a) for a in g["authors"]],
            "applied_by": name(who.get("by")) if who.get("proposed_by") and who.get("by") != who.get("proposed_by") else None,
            "disciplines": [{"discipline": d["discipline"], "label": d["label"], "state": d["state"],
                             "approved_by": [person(a) for a in d["approvers"]],
                             "changes_requested_by": [a.get("name") for a in d["requested_changes"]]} for d in g["disciplines"]],
            "releases": [{"tag": r.get("tag"), "level": r.get("level"), "by": name(r.get("by")), "ts": r.get("ts"),
                          "note": r.get("note") or ""} for r in releases(root)
                         if r.get("rev") == rev and r.get("design_sha256") == g["design_sha256"]]}
