"""Team identity: the owner link, invites, signed session cookies, device links, script tokens and revocation.

- `init_team(owner)` creates MORPH_HOME/.team/ and returns a one-time owner link.
- Invites are single-use, expire after 7 days and carry a role, disciplines and a name hint. The 128-bit code
  is only ever stored as its SHA-256.
- Redeeming an invite (or a device link: single use, 15 minutes) returns the session cookie value
  `id.issued.HMAC(secret, id|issued|epoch)`, valid 30 days. Nothing else is stored, so sessions survive restarts;
  revoking a member (or "sign out everywhere") bumps their epoch, which ends every session at once.
- Script tokens (`X-Morph-Token`) are stored hashed and shown once.
- Wrong codes are throttled per IP address: past the limit a wrong one is answered 429. Every code here is random (128
  bits or more, never guessable), so a right one is always checked and works: another person's wrong tries at the same
  address never lock out the person who has the link. Every comparison of a secret is constant-time.
"""

from __future__ import annotations

import base64
import collections
import hashlib
import hmac
import re
import secrets
import threading
import time
import unicodedata

from ..security import TEAM_CODE, has_key, looks_like_secret, secret_refused
from . import audit, disciplines
from .store import COLORS, POLICY, TeamStore, merged_policy, new_id, parse_ts, utc

ROLES = ("viewer", "editor", "owner")
INVITE_DAYS = 7
LINK_MINUTES = 15
SESSION_DAYS = 30
SKEW_S = 300
MAX_TOKENS = 10
CODE_RX = re.compile(r"^(mi|ml|mt)_[A-Za-z0-9_-]{20,64}$")
SESSION_RX = re.compile(r"^(m[0-9a-f]{10})\.([0-9]{9,11})\.([A-Za-z0-9_-]{43})$")


class AuthError(Exception):
    """A refusal that is safe to show: `status` is the HTTP code."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def new_code(prefix: str, nbytes: int) -> str:
    """A Teams code: `prefix` and `nbytes` random bytes in base64url (secrets.token_urlsafe), drawn again in the rare
    case it has no capital letter (one draw in about 95,000 for 16 bytes) or reads as a phrase to the key filter (about
    one in 28,000 for 16 bytes, none seen in 500,000 for 32), so the filter catches every code Morph makes
    (security.has_key) while names such as mt_shoulder_joint_torque_limit and ml_policyV1_final_weights pass."""
    while True:
        code = prefix + secrets.token_urlsafe(nbytes)
        if TEAM_CODE.fullmatch(code) and has_key(code):
            return code


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _match(code: str, stored) -> bool:
    return isinstance(stored, str) and hmac.compare_digest(_hash(code), stored)


def _now(now) -> float:
    return time.time() if now is None else float(now)


def clean_name(name, fallback: str = "") -> str:
    """A display name: 1 to 40 printable characters, no control characters or angle brackets, spaces collapsed."""
    if not isinstance(name, str):
        name = ""
    name = "".join(c for c in name if unicodedata.category(c)[0] != "C" and c not in "<>@\\")
    _no_secret(name)
    name = " ".join(name.split())[:40].strip()
    if not name:
        name = " ".join((fallback or "").split())[:40].strip()
    if not name:
        raise ValueError("Type your name (up to 40 characters) so the team knows who made each change.")
    return name


def _no_secret(text: str):
    """Names and labels everyone on the team sees use the Studio's one key filter (security.looks_like_secret)."""
    if looks_like_secret(text):
        raise ValueError(secret_refused("into a name or a label"))


def _unique(name: str, members: dict, skip=None) -> str:
    taken = {m["name"].casefold() for mid, m in members.items() if not m.get("revoked") and mid != skip}
    if name.casefold() not in taken:
        return name
    for i in range(2, 100):
        cand = f"{name[:36]} {i}"
        if cand.casefold() not in taken:
            return cand
    return f"{name[:30]} {secrets.token_hex(3)}"


def _color(members: dict) -> str:
    used = {m.get("color") for m in members.values() if not m.get("revoked")}
    return next((c for c in COLORS if c not in used), COLORS[len(members) % len(COLORS)])


def public_member(m: dict) -> dict:
    """What any member may see about another: never epochs, token hashes or invite codes."""
    return {"id": m.get("id"), "name": m.get("name"), "color": m.get("color"), "role": m.get("role"),
            "disciplines": list(m.get("disciplines") or []), "generalist": disciplines.GENERALIST in (m.get("disciplines") or []),
            "watch": disciplines.watching(m), "revoked": bool(m.get("revoked")), "created_utc": m.get("created_utc")}


def public_invite(inv: dict, now=None) -> dict:
    state = "revoked" if inv.get("revoked") else "used" if inv.get("used_utc") else \
        "expired" if (parse_ts(inv.get("expires_utc")) or 0) <= _now(now) else "pending"
    return {"id": inv.get("id"), "role": inv.get("role"), "disciplines": list(inv.get("disciplines") or []),
            "name_hint": inv.get("name_hint") or "", "by": inv.get("by"), "created_utc": inv.get("created_utc"),
            "expires_utc": inv.get("expires_utc"), "used_utc": inv.get("used_utc"), "used_by": inv.get("used_by"), "state": state}


def _member(name: str, role: str, tags, members: dict, by=None, now=None) -> dict:
    mid = new_id("m", 5)
    while mid in members:
        mid = new_id("m", 5)
    return {"id": mid, "name": _unique(name, members), "color": _color(members), "role": role, "disciplines": tags,
            "epoch": 0, "revoked": False, "created_utc": utc(_now(now)), "invited_by": by, "watch": None, "tokens": []}


# ------------------------------------------------------------------ throttling


class Throttle:
    """Per-address failure counter: `limit` wrong codes within `window` seconds block that address until the oldest
    of its last `limit` failures ages out. Only those last `limit` are kept: a right code is always checked, so wrong
    ones keep coming in while an address is blocked, and a flood from one address must cost the same memory and time
    per request as ten wrong codes. In memory only (a restart forgets it); bounded to `cap` addresses, the one that
    failed longest ago forgotten first."""

    def __init__(self, limit: int = 10, window: float = 600.0, cap: int = 4096):
        self.limit, self.window, self.cap = limit, window, cap
        self.fails, self.lock = {}, threading.Lock()  # address -> deque of its last `limit` failure times, oldest first

    def wait(self, ip, now=None) -> float:
        now = _now(now)
        with self.lock:
            ts = self.fails.get(ip or "?")
            while ts and now - ts[0] >= self.window:
                ts.popleft()
            if not ts:
                self.fails.pop(ip or "?", None)
                return 0.0
            return max(0.0, ts[0] + self.window - now) if len(ts) >= self.limit else 0.0

    def fail(self, ip, now=None):
        now = _now(now)
        with self.lock:
            ts = self.fails.pop(ip or "?", None)  # put back last: the dict's order is when each address last failed
            if ts is None:
                if len(self.fails) >= self.cap:
                    self.fails.pop(next(iter(self.fails)))
                ts = collections.deque(maxlen=self.limit)
            ts.append(now)
            self.fails[ip or "?"] = ts

    def clear(self, ip=None):
        with self.lock:
            if ip is None:
                self.fails.clear()
            else:
                self.fails.pop(ip, None)


THROTTLE = Throttle()


def _failed(ip, now=None):
    """A wrong code from `ip`: counted, and once the address is past the limit the refusal is a 429 instead."""
    wait = THROTTLE.wait(ip, now)
    THROTTLE.fail(ip, now)
    if wait > 0:
        raise AuthError(429, f"Too many wrong codes from this address. Wait {max(1, round(wait / 60))} minute(s) before "
                             "trying another, or open a link you were sent: those still work.")


# ------------------------------------------------------------------ team


def init_team(owner_name: str, team_name: str = "", home: str | None = None, now=None) -> dict:
    """Create the team with its first member (owner + generalist) and return a one-time owner link."""
    st = TeamStore(home)
    name = clean_name(owner_name)
    _no_secret(str(team_name or ""))
    tname = " ".join(str(team_name or "").split())[:60] or f"{name}'s team"

    def fn(data):
        if data.get("members"):
            raise AuthError(409, "This Morph folder already has a team. Use `morph team link` for a new owner link.")
        m = _member(name, "owner", [disciplines.GENERALIST], {}, now=now)
        data.update({"name": tname, "created_utc": utc(_now(now)), "members": {m["id"]: m}, "invites": {}, "links": {},
                     "policy": dict(POLICY)})
        return m

    m = st.update(fn)
    st.secret()
    audit.append("team_created", by=m["id"], home=home, team=tname, owner=m["name"])
    link = create_link(m["id"], by=m["id"], home=home, now=now)
    return {"member": public_member(m), "team": tname, **link}


def team_info(home: str | None = None) -> dict:
    data = TeamStore(home).read()
    return {"name": data.get("name"), "created_utc": data.get("created_utc")}


def find_member(ref: str, home: str | None = None) -> dict | None:
    """A member by id or by (case-insensitive) name; revoked members only by id."""
    ms = TeamStore(home).read().get("members") or {}
    if ref in ms:
        return ms[ref]
    hits = [m for m in ms.values() if not m.get("revoked") and m.get("name", "").casefold() == str(ref).casefold()]
    return hits[0] if len(hits) == 1 else None


# ------------------------------------------------------------------ invites


def create_invite(by: str | None, role: str = "editor", tags=(), name_hint: str = "", home: str | None = None,
                  days: float = INVITE_DAYS, now=None) -> dict:
    """A single-use invite link. Returns {"invite", "code", "path"}: the code appears here once and is stored only
    as its SHA-256. `by` None means the host's shell (the CLI), which has owner authority."""
    if role not in ROLES:
        raise ValueError("Choose a role: viewer, editor or owner.")
    tags = disciplines.validate(tags)
    _no_secret(str(name_hint or ""))  # a key in the name refuses the invite (it used to vanish from the hint unsaid)
    hint = ""
    if name_hint:
        try:
            hint = clean_name(name_hint)
        except ValueError:  # nothing printable left: no hint
            hint = ""
    days = min(max(float(days or INVITE_DAYS), 0.01), INVITE_DAYS)
    code = new_code("mi_", 16)
    t = _now(now)

    def fn(data):
        if not data.get("members"):
            raise AuthError(409, "Create the team first: morph team init --owner <your name>.")
        invites = data.setdefault("invites", {})
        for iid in [i for i, v in invites.items() if (parse_ts(v.get("expires_utc")) or 0) < t - 30 * 86400]:
            invites.pop(iid, None)  # keep a month of history, then forget
        iid = new_id("i", 5)
        invites[iid] = {"id": iid, "hash": _hash(code), "role": role, "disciplines": tags, "name_hint": hint, "by": by,
                        "created_utc": utc(t), "expires_utc": utc(t + days * 86400), "used_utc": None, "used_by": None,
                        "revoked": False}
        return invites[iid]

    inv = TeamStore(home).update(fn)
    audit.append("invite_created", by=by, home=home, invite=inv["id"], role=role, disciplines=tags)
    return {"invite": public_invite(inv, t), "code": code, "path": f"/?invite={code}"}


def _find_hashed(table: dict, code: str):
    hit = None
    for v in table.values():  # constant-time per comparison; tables hold a handful of entries
        if _match(code, v.get("hash")):
            hit = v
    return hit


def invite_status(code: str, home: str | None = None, now=None) -> dict | None:
    """For the join page: the invite's role, disciplines, name hint and inviter, or None for an unknown code."""
    if not isinstance(code, str) or not CODE_RX.match(code) or not code.startswith("mi_"):
        return None
    data = TeamStore(home).read()
    inv = _find_hashed(data.get("invites") or {}, code)
    if inv is None:
        return None
    by = (data.get("members") or {}).get(inv.get("by") or "") or {}
    return {**public_invite(inv, now), "team": data.get("name"), "invited_by": by.get("name")}


def revoke_invite(invite_id: str, by: str | None, home: str | None = None) -> dict:
    def fn(data):
        inv = (data.get("invites") or {}).get(invite_id)
        if inv is None:
            raise AuthError(404, "No such invite.")
        inv["revoked"] = True
        return inv

    inv = TeamStore(home).update(fn)
    audit.append("invite_revoked", by=by, home=home, invite=invite_id)
    return public_invite(inv)


def redeem_invite(code: str, name: str = "", ip: str | None = None, home: str | None = None, now=None) -> tuple:
    """Join with an invite: creates the member and returns (public member, session cookie value)."""
    t = _now(now)
    if not isinstance(code, str) or not CODE_RX.match(code) or not code.startswith("mi_"):
        _failed(ip, now)
        raise AuthError(400, "That invite link is not valid. Ask the team owner for a new one.")

    def fn(data):
        inv = _find_hashed(data.get("invites") or {}, code)
        if inv is None:
            raise AuthError(400, "That invite link is not valid. Ask the team owner for a new one.")
        if inv.get("revoked"):
            raise AuthError(410, "That invite was cancelled. Ask the team owner for a new one.")
        if inv.get("used_utc"):
            raise AuthError(410, "That invite link was already used. Each link works once: ask the team owner for a new one.")
        if (parse_ts(inv.get("expires_utc")) or 0) <= t:
            raise AuthError(410, "That invite link has expired (they last 7 days). Ask the team owner for a new one.")
        members = data.setdefault("members", {})
        m = _member(clean_name(name, inv.get("name_hint") or ""), inv.get("role") or "viewer", list(inv.get("disciplines") or []),
                    members, by=inv.get("by"), now=t)
        members[m["id"]] = m
        inv["used_utc"], inv["used_by"] = utc(t), m["id"]
        return m, inv["id"]

    st = TeamStore(home)
    try:
        m, extra = st.update(fn)  # a refusal raises inside fn, so nothing is written
    except AuthError:
        _failed(ip, now)
        raise
    audit.append("sign_in", by=m["id"], home=home, via="invite", invite=extra, role=m["role"], ip=ip or "")
    return public_member(m), sign_session(m["id"], m.get("epoch", 0), st.secret(), t)


# ------------------------------------------------------------------ device links


def create_link(member_id: str, by: str | None = None, home: str | None = None, minutes: float = LINK_MINUTES, now=None) -> dict:
    """A one-time sign-in link for a member's other device (or the owner link from the CLI): 15 minutes, single use."""
    t = _now(now)
    code = new_code("ml_", 16)
    minutes = min(max(float(minutes or LINK_MINUTES), 0.1), LINK_MINUTES)

    def fn(data):
        m = (data.get("members") or {}).get(member_id)
        if m is None or m.get("revoked"):
            raise AuthError(404, "No such member.")
        links = data.setdefault("links", {})
        for lid in [k for k, v in links.items() if v.get("used_utc") or (parse_ts(v.get("expires_utc")) or 0) < t]:
            links.pop(lid, None)
        lid = new_id("l", 5)
        links[lid] = {"id": lid, "hash": _hash(code), "member": member_id, "created_utc": utc(t),
                      "expires_utc": utc(t + minutes * 60), "used_utc": None}
        return links[lid]

    ln = TeamStore(home).update(fn)
    audit.append("link_created", by=by, home=home, member=member_id)
    return {"code": code, "path": f"/?link={code}", "expires_utc": ln["expires_utc"]}


def link_status(code: str, home: str | None = None, now=None) -> dict | None:
    if not isinstance(code, str) or not CODE_RX.match(code) or not code.startswith("ml_"):
        return None
    data = TeamStore(home).read()
    ln = _find_hashed(data.get("links") or {}, code)
    if ln is None or ln.get("used_utc") or (parse_ts(ln.get("expires_utc")) or 0) <= _now(now):
        return None
    m = (data.get("members") or {}).get(ln.get("member")) or {}
    if not m or m.get("revoked"):
        return None
    return {"member": public_member(m), "team": data.get("name"), "expires_utc": ln.get("expires_utc")}


def redeem_link(code: str, ip: str | None = None, home: str | None = None, now=None) -> tuple:
    t = _now(now)
    if not isinstance(code, str) or not CODE_RX.match(code) or not code.startswith("ml_"):
        _failed(ip, now)
        raise AuthError(400, "That sign-in link is not valid.")

    def fn(data):
        ln = _find_hashed(data.get("links") or {}, code)
        m = (data.get("members") or {}).get((ln or {}).get("member"))
        if ln is None or ln.get("used_utc") or (parse_ts(ln.get("expires_utc")) or 0) <= t or not m or m.get("revoked"):
            raise AuthError(410, "That sign-in link has expired or was already used (they work once, for 15 minutes). "
                                 "Make a new one from the Share dialog, or run `morph team link` on the Studio computer.")
        ln["used_utc"] = utc(t)
        return m

    st = TeamStore(home)
    try:
        m = st.update(fn)
    except AuthError:
        _failed(ip, now)
        raise
    audit.append("sign_in", by=m["id"], home=home, via="link", ip=ip or "")
    return public_member(m), sign_session(m["id"], m.get("epoch", 0), st.secret(), t)


# ------------------------------------------------------------------ sessions


def _mac(secret: bytes, mid: str, issued: int, epoch: int) -> str:
    d = hmac.new(secret, f"{mid}|{issued}|{epoch}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(d).decode().rstrip("=")


def sign_session(member_id: str, epoch: int, secret: bytes, now=None) -> str:
    issued = int(_now(now))
    return f"{member_id}.{issued}.{_mac(secret, member_id, issued, int(epoch or 0))}"


def verify_session(value, home: str | None = None, now=None) -> dict | None:
    """The member a session cookie belongs to, or None: bad shape, unknown or revoked member, older than 30 days,
    issued in the future, signed under an old epoch, or a wrong signature."""
    if not isinstance(value, str) or len(value) > 120:
        return None
    mt = SESSION_RX.match(value)
    if not mt:
        return None
    mid, issued, mac = mt.group(1), int(mt.group(2)), mt.group(3)
    t = _now(now)
    if issued > t + SKEW_S or t - issued > SESSION_DAYS * 86400:
        return None
    st = TeamStore(home)
    if not st.exists():
        return None
    m = st.member(mid)
    if not m or m.get("revoked"):
        return None
    if not hmac.compare_digest(_mac(st.secret(), mid, issued, int(m.get("epoch") or 0)), mac):
        return None
    return public_member(m)


def session_state(value, home: str | None = None, now=None) -> tuple:
    """(state, public member) for a session cookie: "ok"; "removed" when the owner revoked its member; "ended" when it was
    signed out everywhere (it was genuinely signed under an older epoch); (None, None) for anything else (malformed,
    forged, expired, unknown). The signed-out page tells a removed member why, without telling anyone else anything."""
    ok = verify_session(value, home=home, now=now)
    if ok:
        return "ok", ok
    mt = SESSION_RX.match(value) if isinstance(value, str) and len(value) <= 120 else None
    if not mt:
        return None, None
    mid, issued, mac = mt.group(1), int(mt.group(2)), mt.group(3)
    t = _now(now)
    st = TeamStore(home)
    m = st.member(mid) if st.exists() else None
    if not m or issued > t + SKEW_S or t - issued > SESSION_DAYS * 86400:
        return None, None
    secret = st.secret()
    if not any(hmac.compare_digest(_mac(secret, mid, issued, e), mac) for e in range(max(0, int(m.get("epoch") or 0)))):
        return None, None
    return ("removed" if m.get("revoked") else "ended"), public_member(m)


def cookie_name(port) -> str:
    return f"morph_session_{int(port)}"  # cookies ignore the port, so two Studios on one host need their own


def session_cookie(port, value: str, secure: bool = False) -> str:
    return (f"{cookie_name(port)}={value}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_DAYS * 86400}"
            + ("; Secure" if secure else ""))


def clear_cookie(port, secure: bool = False) -> str:
    return f"{cookie_name(port)}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0" + ("; Secure" if secure else "")


# ------------------------------------------------------------------ script tokens


def create_token(member_id: str, label: str = "", home: str | None = None, now=None) -> dict:
    """A script token for X-Morph-Token: shown once, stored as its SHA-256, acts with the member's role."""
    _no_secret(str(label or ""))
    label = " ".join(str(label or "").split())[:40] or "script"
    token = new_code("mt_", 32)
    t = _now(now)

    def fn(data):
        m = (data.get("members") or {}).get(member_id)
        if m is None or m.get("revoked"):
            raise AuthError(404, "No such member.")
        toks = m.setdefault("tokens", [])
        if len(toks) >= MAX_TOKENS:
            raise AuthError(409, f"You already have {MAX_TOKENS} script tokens. Revoke one first.")
        tid = new_id("t", 4)
        toks.append({"id": tid, "hash": _hash(token), "label": label, "created_utc": utc(t)})
        return tid

    tid = TeamStore(home).update(fn)
    audit.append("token_created", by=member_id, home=home, token=tid, label=label)
    return {"id": tid, "label": label, "token": token}


def tokens(member_id: str, home: str | None = None) -> list:
    m = TeamStore(home).member(member_id) or {}
    return [{"id": t.get("id"), "label": t.get("label"), "created_utc": t.get("created_utc")} for t in m.get("tokens") or []]


def revoke_token(member_id: str, token_id: str, home: str | None = None) -> bool:
    def fn(data):
        m = (data.get("members") or {}).get(member_id)
        if m is None:
            return False
        before = len(m.get("tokens") or [])
        m["tokens"] = [t for t in m.get("tokens") or [] if t.get("id") != token_id]
        return len(m["tokens"]) != before

    ok = TeamStore(home).update(fn)
    if ok:
        audit.append("token_revoked", by=member_id, home=home, token=token_id)
    return ok


def verify_token(token, ip: str | None = None, home: str | None = None, now=None) -> dict | None:
    if not isinstance(token, str) or not token:
        return None
    if not CODE_RX.match(token) or not token.startswith("mt_"):
        THROTTLE.fail(ip, now)
        return None
    hit = None
    for m in (TeamStore(home).read().get("members") or {}).values():
        for t in m.get("tokens") or []:
            if _match(token, t.get("hash")) and not m.get("revoked"):
                hit = m
    if hit is None:
        THROTTLE.fail(ip, now)
        return None
    return public_member(hit)


# ------------------------------------------------------------------ members and policy


def _owners_left(members: dict, skip: str) -> int:
    return sum(1 for mid, m in members.items() if mid != skip and not m.get("revoked") and m.get("role") == "owner")


def revoke_member(member_id: str, by: str | None, home: str | None = None) -> dict:
    """End every session, script token and pending device link of a member at once. Their name stays on past work."""
    def fn(data):
        members = data.get("members") or {}
        m = members.get(member_id)
        if m is None:
            raise AuthError(404, "No such member.")
        if m.get("role") == "owner" and not m.get("revoked") and _owners_left(members, member_id) == 0:
            raise AuthError(409, "That is the team's only owner. Make someone else an owner first.")
        m["revoked"], m["epoch"], m["tokens"] = True, int(m.get("epoch") or 0) + 1, []
        m["revoked_utc"], m["revoked_by"] = utc(), by
        data["links"] = {k: v for k, v in (data.get("links") or {}).items() if v.get("member") != member_id}
        return m

    m = TeamStore(home).update(fn)
    audit.append("member_revoked", by=by, home=home, member=member_id, name=m.get("name"))
    return public_member(m)


def sign_out_everywhere(member_id: str, home: str | None = None) -> int:
    def fn(data):
        m = (data.get("members") or {}).get(member_id)
        if m is None:
            raise AuthError(404, "No such member.")
        m["epoch"] = int(m.get("epoch") or 0) + 1
        return m["epoch"]

    epoch = TeamStore(home).update(fn)
    audit.append("sign_out_all", by=member_id, home=home, member=member_id)
    return epoch


def update_member(member_id: str, by: str | None, role=None, tags=None, name=None, watch=None, color=None,
                  home: str | None = None) -> dict:
    """Owner changes (role, disciplines) are audited; self-service changes (name, watch, color) are not.
    The team always keeps at least one owner."""
    if role is not None and role not in ROLES:
        raise ValueError("Choose a role: viewer, editor or owner.")
    tags = disciplines.validate(tags) if tags is not None else None
    name = clean_name(name) if name is not None else None
    if watch is not None and not isinstance(watch, bool):
        raise ValueError("watch must be true or false.")
    if color is not None and color not in COLORS:
        raise ValueError("Pick one of the team colours.")
    changed = {}

    def fn(data):
        members = data.get("members") or {}
        m = members.get(member_id)
        if m is None or m.get("revoked"):
            raise AuthError(404, "No such member.")
        if role is not None and role != m.get("role"):
            if m.get("role") == "owner" and _owners_left(members, member_id) == 0:
                raise AuthError(409, "That is the team's only owner. Make someone else an owner first.")
            changed["role"] = [m.get("role"), role]
            m["role"] = role
        if tags is not None and tags != m.get("disciplines"):
            changed["disciplines"] = [m.get("disciplines"), tags]
            m["disciplines"] = tags
        if name is not None and name != m.get("name"):
            m["name"] = _unique(name, members, skip=member_id)
        if watch is not None:
            m["watch"] = watch
        if color is not None:
            m["color"] = color
        return m

    m = TeamStore(home).update(fn)
    if "role" in changed:
        audit.append("role_changed", by=by, home=home, member=member_id, old=changed["role"][0], new=changed["role"][1])
    if "disciplines" in changed:
        audit.append("disciplines_changed", by=by, home=home, member=member_id, old=changed["disciplines"][0],
                     new=changed["disciplines"][1])
    return public_member(m)


def set_policy(changes: dict, by: str | None, home: str | None = None) -> dict:
    """Validate and save policy changes; returns the whole policy."""
    if not isinstance(changes, dict) or not changes:
        raise ValueError("Send the policy changes as {\"values\": {...}}.")
    clean = {}
    for k, v in changes.items():
        if k not in POLICY:
            raise ValueError(f"Unknown policy setting {str(k)[:40]!r}.")
        if k == "unreleased_downloads":
            if v not in ("warn", "owner"):
                raise ValueError("unreleased_downloads is \"warn\" (editors may download drafts, logged) or \"owner\".")
        elif k == "specialist_required":
            if not isinstance(v, dict) or set(v) - {"pilot", "production"}:
                raise ValueError("specialist_required is {\"pilot\": [...], \"production\": [...]}.")
            v = {lvl: [d for d in disciplines.validate(v.get(lvl) or []) if d != disciplines.GENERALIST] for lvl in ("pilot", "production")}
        elif not isinstance(v, bool):
            raise ValueError(f"{k} must be true or false.")
        clean[k] = v

    def fn(data):
        pol = merged_policy(data.get("policy"))
        pol.update(clean)
        data["policy"] = pol
        return pol

    pol = TeamStore(home).update(fn)
    audit.append("policy_changed", by=by, home=home, keys=sorted(clean))
    return pol
