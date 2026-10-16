"""Teams routes (RELEASE_CONTRACTS section 2) and the hooks the Studio's router calls in team mode.

The routes register themselves when the Studio loads its feature modules, and answer only on a Studio started with
`morph studio --team` (Studio.team; GET /api/me says {team: false} elsewhere, every other route 404). A team Studio's
server (server.py) calls the hooks in this order:

    if method == "GET" and api_team.landing(handler): return        # /?invite= and /?link= pages, before sign-in
    member = api_team.authenticate(handler)                          # session cookie or X-Morph-Token, else None
    req.member = api_team.role_hook(req, route.role, member)         # the route table in teams/roles.py:
                                                                     # HTTPError 401 signed out, 403 not allowed
    (page loads without a session get api_team.signed_out_page())

Handlers take the router's Request and return (status, dict) or write the response themselves (join and
sign-out set cookies) and return None. Errors are {"ok": false, "error": "..."}; nothing secret is ever returned
except a code or token to the person who just created it.
"""

from __future__ import annotations

import html
import inspect
import json
import os
import threading
import time
import traceback
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlparse

from ..services import durable
from . import rooms
from .rooms import STARTED
from .teams import activity, audit, auth, collab, roles
from .teams.disciplines import DISCIPLINES, GENERALIST, LABELS, STAGE_TITLES, STAGES, routing, watching
from .teams.store import TeamFileError, TeamStore, exists, parse_ts

CONFIG = {"home": None, "secure": False, "enabled": False}
MAX_FORM = 8192
JOIN_CSP = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
DISCIPLINE_INFO = [{"key": d, "label": LABELS[d], "stages": [STAGE_TITLES[s] for s in STAGES[d]]} for d in DISCIPLINES] + \
    [{"key": GENERALIST, "label": "Generalist (all stages)", "stages": []}]


def configure(home: str | None = None, secure_cookies: bool = False, enabled: bool = True):
    """Turn team mode on for this process (morph studio --team). `secure_cookies` when served over HTTPS."""
    CONFIG.update(home=home, secure=bool(secure_cookies), enabled=bool(enabled))


def enabled(studio=None) -> bool:
    return bool(CONFIG["enabled"] or getattr(studio, "team", False))


# ------------------------------------------------------------------ hooks


def _home(req=None) -> str | None:
    """The team's folder: the Studio's own projects folder (MORPH_HOME), else what configure() set."""
    return (getattr(getattr(req, "studio", None), "home", None) if req is not None else None) or CONFIG["home"]


def _port(handler) -> int:
    try:
        return int(handler.server.server_address[1])
    except (AttributeError, IndexError, TypeError, ValueError):
        return 0


def _ip(handler) -> str:
    try:
        return str(handler.client_address[0])
    except (AttributeError, IndexError, TypeError):
        return "?"


def authenticate(handler, home: str | None = None) -> dict | None:
    """The member behind a request: the morph_session_<port> cookie, or a script's X-Morph-Token. None otherwise."""
    home = home or CONFIG["home"]
    if not exists(home):
        return None
    jar = SimpleCookie()
    try:
        jar.load(handler.headers.get("Cookie") or "")
    except Exception:  # noqa: BLE001  (a malformed cookie header is just "not signed in")
        pass
    name = auth.cookie_name(_port(handler))
    if name in jar:
        m = auth.verify_session(jar[name].value, home=home)
        if m:
            return m
    tok = handler.headers.get("X-Morph-Token")
    if tok:
        return auth.verify_token(tok.strip(), ip=_ip(handler), home=home)
    return None


def authorize(member, method: str, path: str, declared: str | None = None, home: str | None = None) -> bool:
    """The route table check (teams/roles.py), with this team's policy for the viewer switches."""
    pol = TeamStore(home or CONFIG["home"]).policy() if member else None
    return roles.authorize(member, method, path, policy=pol, declared=declared)


def role_hook(req, role: str, member: dict | None = None) -> dict | None:
    """The router's role check on a team Studio: who is asking (`member` when the server already knows, else the
    session cookie or X-Morph-Token) and whether the route table lets them. Returns the member (None on the public join
    route when signed out); raises the router's HTTPError 401 when signed out and 403 when the role isn't enough."""
    from .routes import HTTPError

    home = _home(req)
    try:
        if member is None:
            member = authenticate(req.handler, home=home)
        if role == roles.PUBLIC:
            return member
        allowed = authorize(member, req.method, req.path, declared=role, home=home)
    except TeamFileError as exc:  # team.json can't be read: nothing is allowed until it is fixed
        raise HTTPError(503, team_problem(exc)) from None
    if not allowed:
        if member is None:
            raise HTTPError(401, "Sign in first: open your invite link, or ask the team owner for a sign-in link.")
        raise HTTPError(403, refusal(member, req.method, req.path, role))
    return member


def refusal(member: dict, method: str, path: str, declared: str | None = None) -> str:
    """Why the route table refused a signed-in member, and what they can do instead."""
    need, flag = roles.required(method, path)
    if declared and declared != roles.PUBLIC and roles.RANK.get(declared, 0) > roles.RANK.get(need, 0):
        need = declared
    if need == "owner":
        return "Only the team's owners can do that."
    if flag:
        what = {"viewers_can_comment": "comment", "viewers_can_propose": "propose changes",
                "viewers_can_approve": "approve"}.get(flag, "do that")
        return f"Viewers can't {what} on this team (an owner can allow it under Share, Policy)."
    if member.get("role") == "viewer":
        return ("Viewers can't change the design: propose the change under Changes in the right panel, or ask an owner to "
                "make you an editor.")
    return f"A team {member.get('role') or 'member'} can't do that here: ask an owner."


def _page(title: str, body: str) -> bytes:
    # strict-origin: the form's POST carries this page's Origin (the Studio's no-referrer policy would send "null", which
    # the Origin check refuses) and never the invite code in a Referer
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="strict-origin"><title>{html.escape(title)}</title><style>
:root {{ color-scheme: dark; }} body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; background: #08090c; color: #e9ecf2;
  font: 14px/1.5 Inter, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }}
main {{ width: min(460px, calc(100vw - 32px)); padding: 22px; border: 1px solid #262c38; border-radius: 14px; background: #0d0f14; }}
h1 {{ font-size: 17px; margin: 0 0 6px; }} p {{ color: #b9c0cd; margin: 0 0 14px; }} .bad {{ color: #ff6b6b; }} .dim {{ color: #838c9d; font-size: 12.5px; }}
.tags {{ display: flex; flex-wrap: wrap; gap: 6px; margin: 0 0 14px; }} .tag {{ padding: 2px 9px; border-radius: 99px; background: rgba(112,145,255,.12); color: #9db2ff; font-size: 12px; font-weight: 600; }}
form {{ display: flex; gap: 8px; }} input {{ flex: 1; min-width: 0; height: 36px; border-radius: 8px; border: 1px solid #343b49; background: #08090c;
  color: #e9ecf2; padding: 0 10px; font: inherit; }}
button {{ height: 36px; border: 0; border-radius: 8px; padding: 0 14px; background: #7091ff; color: #0a0f24; font-weight: 650; cursor: pointer; }}
</style></head><body><main>{body}</main></body></html>""".encode("utf-8")


def join_page(code: str, error: str = "", home: str | None = None) -> tuple:
    """(status, html bytes) for GET /?invite=<code>: who invited you, the role and disciplines, and a name field.
    No script: the form posts to /api/team/join."""
    inv = auth.invite_status(code, home=home or CONFIG["home"])
    if inv is None or inv["state"] != "pending":
        why = {"used": "This invite link was already used: each link works once.", "expired": "This invite link has expired (they last 7 days).",
               "revoked": "This invite was cancelled."}.get((inv or {}).get("state"), "This invite link is not valid.")
        return 410 if inv else 400, _page("Morph Studio: invite", f"<h1>Morph Studio</h1><p class=\"bad\">{html.escape(why)}</p>"
                                                                 "<p>Ask the team owner for a new invite link.</p>")
    tags = "".join(f"<span class=\"tag\">{html.escape(LABELS.get(d, d))}</span>" for d in inv["disciplines"] if d != GENERALIST)
    who = f"{html.escape(inv['invited_by'])} invited you" if inv.get("invited_by") else "You're invited"
    err = f"<p class=\"bad\">{html.escape(error)}</p>" if error else ""
    does = {"viewer": "You'll see the robot live, comment and propose changes.", "editor": "You'll edit and build the robot, "
            "propose and apply changes, review and release.", "owner": "You'll run the team: everything an editor does, plus "
            "members, invites and settings."}.get(inv["role"], "")
    reviews = ("<p class=\"dim\" style=\"margin:0 0 6px\">You review changes to:</p>" if tags else "") + \
        (f"<div class=\"tags\">{tags}</div>" if tags else "") + \
        ("<p class=\"dim\">As a generalist you also review every stage that has no specialist on the team.</p>"
         if GENERALIST in inv["disciplines"] else "")
    body = (f"<h1>Join {html.escape(inv.get('team') or 'the team')} on Morph Studio</h1>"
            f"<p>{who} as <b>{html.escape(inv['role'])}</b>. {does} This link works once.</p>{reviews}{err}"
            f"<form method=\"post\" action=\"/api/team/join\"><input type=\"hidden\" name=\"code\" value=\"{html.escape(code)}\">"
            f"<input name=\"name\" maxlength=\"40\" autocomplete=\"name\" placeholder=\"Your name\" aria-label=\"Your name\" required autofocus "
            f"value=\"{html.escape(inv.get('name_hint') or '')}\"><button type=\"submit\">Join</button></form>"
            "<p class=\"dim\" style=\"margin-top:12px\">Your name appears on your changes, comments and approvals. "
            "Morph Teams is a preview that runs on the team owner's computer.</p>")
    return 200, _page("Morph Studio: join the team", body)


def link_page(code: str, error: str = "", home: str | None = None) -> tuple:
    st = auth.link_status(code, home=home or CONFIG["home"])
    if st is None:
        return 410, _page("Morph Studio: sign in", "<h1>Morph Studio</h1><p class=\"bad\">This sign-in link has expired or was already used.</p>"
                                                  "<p>Links work once, for 15 minutes. Make a new one from the Share dialog on a signed-in "
                                                  "device, or run <code>morph team link</code> on the Studio computer.</p>")
    err = f"<p class=\"bad\">{html.escape(error)}</p>" if error else ""
    body = (f"<h1>Sign in to {html.escape(st.get('team') or 'the team')}</h1>"
            f"<p>Sign this browser in as <b>{html.escape(st['member']['name'])}</b> ({html.escape(st['member']['role'])}).</p>{err}"
            f"<form method=\"post\" action=\"/api/team/join\"><input type=\"hidden\" name=\"code\" value=\"{html.escape(code)}\">"
            "<button type=\"submit\">Sign in</button></form>")
    return 200, _page("Morph Studio: sign in", body)


def _session_value(handler) -> str | None:
    jar = SimpleCookie()
    try:
        jar.load(handler.headers.get("Cookie") or "")
    except Exception:  # noqa: BLE001
        return None
    name = auth.cookie_name(_port(handler))
    return jar[name].value if name in jar else None


def signed_out_page(home: str | None = None, handler=None) -> tuple:
    """(status, html bytes, content type, csp) for a page load without a session in team mode. A browser whose member was
    removed (or signed out everywhere) is told so, from its own genuine cookie; everyone else gets the general page."""
    home = home or CONFIG["home"]
    team = (auth.team_info(home) or {}).get("name") or "a team"
    state, m = auth.session_state(_session_value(handler), home=home) if handler is not None else (None, None)
    if state == "removed":
        store = TeamStore(home)
        by = (store.member((store.member(m["id"]) or {}).get("revoked_by") or "") or {}).get("name")
        body = (f"<h1>You're no longer on {html.escape(team)}</h1><p>{html.escape(by or 'The team owner')} removed "
                f"{html.escape(m['name'])} from this Studio, so this browser is signed out. Your name stays on the work you did.</p>"
                "<p class=\"dim\">If that's a mistake, ask the team owner for a new invite link.</p>")
    elif state == "ended":
        body = (f"<h1>Signed out of {html.escape(team)}</h1><p>{html.escape(m['name'])} signed out everywhere, so this browser "
                "needs a new sign-in link: make one under Team on a signed-in device, or ask the team owner.</p>"
                "<p class=\"dim\">Owner: <code>morph team link</code> on the Studio computer prints one.</p>")
    else:
        body = (f"<h1>Morph Studio for {html.escape(team)}</h1><p>This Studio is shared with a team. Open the invite link the "
                "team owner sent you, or a sign-in link from one of your signed-in devices.</p>"
                "<p class=\"dim\">Owner: run <code>morph team link</code> on the Studio computer for a new sign-in link.</p>")
    return 401, _page("Morph Studio: sign in", body), "text/html; charset=utf-8", JOIN_CSP


class StreamCheck:
    """For an open event stream on a team Studio: is the member behind it still signed in? Checked at most every
    `every` seconds (the cookie the stream opened with, against the team's current members and epochs), so removing a
    member, or their "Sign out everywhere", ends their open streams within seconds. `goodbye()` is the stream's last event:
    the page then shows why it was signed out."""

    def __init__(self, handler, member: dict, home: str | None = None, every: float = 2.0):
        self.handler, self.member, self.home, self.every = handler, member, home or CONFIG["home"], every
        self.next, self.ok, self.why = 0.0, True, ""

    def __call__(self) -> bool:
        now = time.monotonic()
        if self.ok and now >= self.next:
            self.next = now + self.every
            try:
                m = authenticate(self.handler, home=self.home)
            except Exception:  # noqa: BLE001  (a busy team file: keep the stream, check again)
                return True
            if not m or m.get("id") != self.member.get("id"):
                cur = TeamStore(self.home).member(self.member.get("id")) or {}
                self.ok, self.why = False, "removed" if cur.get("revoked") or not cur else "signed_out"
        return self.ok

    def goodbye(self, project: str) -> bytes:
        return f"data: {json.dumps({'type': 'signed_out', 'project': project, 'reason': self.why})}\n\n".encode()


def stream_check(handler, member: dict | None, home: str | None = None):
    """server.py's /api/events: a StreamCheck for a member's stream (None without a member)."""
    return StreamCheck(handler, member, home) if member else None


def _write(handler, status: int, body: bytes, ctype: str = "application/json", headers=(), csp: str | None = None):
    handler.csp = csp  # W0a's end_headers sends this policy (and its security headers)
    handler.send_response(status)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    if csp and not getattr(handler, "sends_csp", False):  # the Studio's own handler sends handler.csp itself
        handler.send_header("Content-Security-Policy", csp)
    for k, v in headers:
        handler.send_header(k, v)
    handler.end_headers()
    if handler.command != "HEAD":
        handler.wfile.write(body)


def landing(handler, home: str | None = None) -> bool:
    """GET / with ?invite= or ?link=: answer with the join or sign-in page (no session needed). True if handled."""
    u = urlparse(handler.path)
    if handler.command not in ("GET", "HEAD") or u.path not in ("/", "/index.html"):
        return False
    q = parse_qs(u.query)
    if "invite" not in q and "link" not in q:
        return False
    ip = _ip(handler)
    if "invite" in q:
        status, page = join_page(q["invite"][0].strip(), home=home)
    else:
        status, page = link_page(q["link"][0].strip(), home=home)
    if status == 400 or (status == 410 and "link" in q):  # the page tells a valid code from a wrong one: wrong ones count
        past = auth.THROTTLE.wait(ip) > 0
        auth.THROTTLE.fail(ip)
        if past:  # a right link still opens (its codes are random: guessing one is hopeless)
            status, page = 429, _page("Morph Studio", "<h1>Morph Studio</h1><p class=\"bad\">Too many wrong links from this "
                                                      "address. Wait a few minutes before trying another one.</p>")
    _write(handler, status, page, "text/html; charset=utf-8", csp=JOIN_CSP)
    return True


# ------------------------------------------------------------------ request helpers


def _err(status: int, msg: str):
    return status, {"ok": False, "error": msg}


def _q(req, name, default=None):
    v = (getattr(req, "query", None) or {}).get(name)
    return v[0] if isinstance(v, list) and v else (v if isinstance(v, str) else default)


def _root(req) -> str:
    p = getattr(getattr(req, "room", None), "p", None) or getattr(getattr(req, "studio", None), "p", None)
    return p.root


def _project(req) -> str:
    room = getattr(req, "room", None)
    return getattr(room, "name", None) or os.path.basename(os.path.normpath(_root(req)))


def _team(req) -> dict:
    return TeamStore(_home(req)).read()


def _members(req) -> dict:
    return _team(req).get("members") or {}


def _policy(req) -> dict:
    return TeamStore(_home(req)).policy()


def _me(req) -> dict:
    m = getattr(req, "member", None)
    if not m:
        raise auth.AuthError(401, "Sign in first.")
    return m


def _by(member: dict | None):
    return member.get("id") if member else None


def _hub(req):
    return getattr(getattr(req, "room", None), "hub", None) or getattr(getattr(req, "studio", None), "hub", None)


def _emit(req, ev: dict):
    """Broadcast a live event to the project's room (never fails the request)."""
    hub = _hub(req)
    if hub is None:
        return
    ev = {**ev, "project": _project(req)}
    if getattr(req, "member", None):
        ev.setdefault("by", _by(req.member))
    try:
        hub.send(ev)
    except Exception:  # noqa: BLE001  (event streams come and go)
        pass


def _notify(req, ids, kind: str, text: str, ref: dict | None = None):
    """Inbox notifications, plus an `inbox` event per member when the hub can address one member (Hub.addressed);
    otherwise the page polls /api/inbox, so one member's notifications never reach another's browser."""
    by = (getattr(req, "member", None) or {}).get("id")
    sent = activity.notify_many(_home(req), ids, kind, text, project=_project(req), ref=ref, by=by)
    hub = _hub(req)
    if hub is not None and getattr(hub, "addressed", False):
        for mid, item in sent:
            try:
                hub.send({"type": "inbox", "to": mid, "project": _project(req), "item": item,
                          "unread": activity.unread(_home(req), mid)})
            except Exception:  # noqa: BLE001
                pass
    return sent


_TOLD = {"problem": None}


def team_problem(exc) -> str:
    """What a browser is told about a team.json that can't be read (TeamFileError.public: the file named from the
    projects folder, never the server's own path); the terminal gets the full text once, when it changes."""
    full = str(exc)
    if _TOLD["problem"] != full:
        _TOLD["problem"] = full
        print(full, flush=True)
    return getattr(exc, "public", None) or full


def _safe(fn):
    def run(req, *a):
        try:
            return fn(req, *a)
        except (auth.AuthError, collab.CollabError) as exc:
            out = {"ok": False, "error": str(exc)}
            if getattr(exc, "gate", None):
                out["gate"] = exc.gate
            return exc.status, out
        except TeamFileError as exc:  # team.json can't be read: nothing changes until it is fixed
            return _err(503, team_problem(exc))
        except ValueError as exc:
            return _err(400, str(exc))
        except TimeoutError as exc:
            return _err(503, str(exc))
        except Exception as exc:  # noqa: BLE001  (never leak a traceback to the browser)
            traceback.print_exc()
            if durable.disk_full(exc):
                return _err(507, durable.DISK_FULL)
            return _err(500, f"The team service failed ({exc.__class__.__name__}); see the Studio's terminal.")
    run.__name__ = fn.__name__
    run.__doc__ = fn.__doc__
    return run


def _body(req) -> dict:
    b = getattr(req, "body", None)
    return b if isinstance(b, dict) else {}


def _author(members: dict, mid) -> dict:
    m = members.get(mid) or {}
    return {"id": mid, "name": m.get("name") or ("former member" if mid else "Morph"), "color": m.get("color"),
            "revoked": bool(m.get("revoked"))}


# ------------------------------------------------------------------ me and the team


@_safe
def me_get(req):
    m = getattr(req, "member", None)
    if not m:
        return 200, {"ok": True, "team": False, "member": None}
    home, pol = _home(req), _policy(req)
    can = {a: roles.can(m, a, pol) for a in ("comment", "propose", "approve", "edit", "apply", "request_review", "release", "branch",
                                              "admin", "keys", "audit")}
    return 200, {"ok": True, "team": True, "team_name": auth.team_info(home).get("name"), "member": m,
                 "tokens": auth.tokens(m["id"], home), "policy": pol, "can": can, "disciplines": DISCIPLINE_INFO,
                 "roles": list(auth.ROLES), "unread": activity.unread(home, m["id"])}


@_safe
def me_post(req):
    m, b = _me(req), _body(req)
    out = auth.update_member(m["id"], m["id"], name=b.get("name"), watch=b.get("watch"), color=b.get("color"), home=_home(req))
    return 200, {"ok": True, "member": out}


@_safe
def me_link(req):
    m = _me(req)
    res = auth.create_link(m["id"], by=m["id"], home=_home(req))
    return 200, {"ok": True, "path": res["path"], "expires_utc": res["expires_utc"]}


@_safe
def me_tokens(req):
    m, b = _me(req), _body(req)
    if b.get("action") == "revoke":
        if not auth.revoke_token(m["id"], str(b.get("id") or ""), home=_home(req)):
            return _err(404, "No such token.")
        return 200, {"ok": True, "tokens": auth.tokens(m["id"], _home(req))}
    res = auth.create_token(m["id"], b.get("label") or "", home=_home(req))
    return 200, {"ok": True, "token": res["token"], "id": res["id"], "label": res["label"], "tokens": auth.tokens(m["id"], _home(req)),
                 "note": "Copy it now: Morph stores only its hash and can't show it again. Scripts send it as X-Morph-Token."}


def signout(req):
    """POST /api/signout {everywhere?}: clears this browser's cookie; everywhere also ends every other session."""
    m = getattr(req, "member", None)
    b = _body(req)
    if m and b.get("everywhere") is True:
        try:
            auth.sign_out_everywhere(m["id"], home=_home(req))
        except (auth.AuthError, OSError, TimeoutError):
            pass  # this browser's cookie is cleared either way
    _write(req.handler, 200, json.dumps({"ok": True}).encode(), headers=[("Set-Cookie", auth.clear_cookie(_port(req.handler), CONFIG["secure"]))])
    return None


@_safe
def team_get(req):
    m = getattr(req, "member", None) or {}
    data = _team(req)
    members = [auth.public_member(x) for x in sorted((data.get("members") or {}).values(), key=lambda x: x.get("created_utc") or "")]
    try:
        presence = activity.PRESENCE.snapshot(_project(req), data.get("members") or {})
    except AttributeError:  # no project on this request
        presence = []
    out = {"ok": True, "team": {"name": data.get("name"), "created_utc": data.get("created_utc")},
           "members": [x for x in members if not x["revoked"]], "former": [x for x in members if x["revoked"]],
           "policy": TeamStore(_home(req)).policy(), "disciplines": DISCIPLINE_INFO, "presence": presence}
    if m.get("role") == "owner":
        invs = [auth.public_invite(i) for i in (data.get("invites") or {}).values()]
        out["invites"] = sorted(invs, key=lambda i: i.get("created_utc") or "", reverse=True)[:50]
    return 200, out


def _join_body(req) -> tuple:
    """(fields, is_form): JSON from scripts and team.js, or the join page's url-encoded form."""
    b = getattr(req, "body", None)
    if isinstance(b, dict):
        return b, False
    h = req.handler
    try:
        n = int(h.headers.get("Content-Length") or 0)
    except ValueError:
        n = -1
    if n < 0 or n > MAX_FORM:
        raise auth.AuthError(413, "That request is too large.")
    h._read = True  # the body is this route's own to read: nothing is drained after it
    raw = h.rfile.read(n) if n else b""
    ctype = (h.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if ctype == "application/x-www-form-urlencoded":
        q = parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)
        return {k: v[0] for k, v in q.items() if v}, True
    try:
        obj = json.loads(raw.decode("utf-8")) if raw else {}
    except (ValueError, UnicodeError):
        obj = None
    if not isinstance(obj, dict):
        raise auth.AuthError(400, "Send a JSON object.")
    return obj, False


def team_join(req):
    """POST /api/team/join {code, name}: an invite (mi_...) or device link (ml_...) -> member + session cookie.
    Public route. The join page's form gets a 303 to the Studio; JSON callers get {ok, member}."""
    home = _home(req)
    try:
        b, form = _join_body(req)
    except auth.AuthError as exc:
        _write(req.handler, exc.status, json.dumps({"ok": False, "error": str(exc)}).encode())
        return None
    code = str(b.get("code") or b.get("invite") or b.get("link") or "").strip()
    try:
        if code.startswith("ml_"):
            member, cookie = auth.redeem_link(code, ip=_ip(req.handler), home=home)
        else:
            member, cookie = auth.redeem_invite(code, b.get("name") or "", ip=_ip(req.handler), home=home)
    except (auth.AuthError, ValueError) as exc:
        status = getattr(exc, "status", 400)
        if form:
            st, page = (link_page if code.startswith("ml_") else join_page)(code, error=str(exc), home=home)
            _write(req.handler, status if status == 429 else st if st != 200 else 400, page, "text/html; charset=utf-8", csp=JOIN_CSP)
        else:
            _write(req.handler, status, json.dumps({"ok": False, "error": str(exc)}).encode())
        return None
    set_cookie = ("Set-Cookie", auth.session_cookie(_port(req.handler), cookie, CONFIG["secure"]))
    if form:
        _write(req.handler, 303, b"", "text/plain; charset=utf-8", headers=[set_cookie, ("Location", "/")])
    else:
        _write(req.handler, 200, json.dumps({"ok": True, "member": member}).encode(), headers=[set_cookie])
    return None


@_safe
def team_invites(req):
    m, b = _me(req), _body(req)
    res = auth.create_invite(m["id"], b.get("role") or "editor", b.get("disciplines") or [], b.get("name_hint") or b.get("name") or "",
                             home=_home(req), days=b.get("days") or auth.INVITE_DAYS)
    return 200, {"ok": True, **res}


@_safe
def team_revoke(req):
    m, b = _me(req), _body(req)
    if b.get("invite"):
        return 200, {"ok": True, "invite": auth.revoke_invite(str(b["invite"]), m["id"], home=_home(req))}
    if not b.get("member"):
        return _err(400, "Name the member or invite to revoke.")
    out = auth.revoke_member(str(b["member"]), m["id"], home=_home(req))
    ev = {"type": "member", "action": "revoked", "member": out, "by": m["id"]}
    studio = getattr(req, "studio", None)
    if callable(getattr(studio, "broadcast", None)):  # every project's tabs: the member may be on any of them
        try:
            studio.broadcast(ev)
        except Exception:  # noqa: BLE001
            pass
    else:
        _emit(req, ev)
    return 200, {"ok": True, "member": out}


@_safe
def team_members(req):
    m, b = _me(req), _body(req)
    if not b.get("id"):
        return _err(400, "Name the member to change.")
    out = auth.update_member(str(b["id"]), m["id"], role=b.get("role"), tags=b.get("disciplines"), name=b.get("name"),
                             watch=b.get("watch") if isinstance(b.get("watch"), bool) else None, home=_home(req))
    _emit(req, {"type": "member", "action": "changed", "member": out})
    return 200, {"ok": True, "member": out}


@_safe
def team_policy(req):
    m, b = _me(req), _body(req)
    pol = auth.set_policy(b.get("values") if isinstance(b.get("values"), dict) else b, m["id"], home=_home(req))
    _emit(req, {"type": "policy", "policy": pol})
    return 200, {"ok": True, "policy": pol}


# ------------------------------------------------------------------ comments


def _decorate_comment(c: dict, members: dict) -> dict:
    return {**c, "author": _author(members, c.get("by")), "resolver": _author(members, c["resolved_by"]) if c.get("resolved_by") else None}


@_safe
def comments_get(req):
    root, members = _root(req), _members(req)
    rev = _q(req, "rev") or collab.head(root)
    kind, key = _q(req, "kind"), _q(req, "key")
    resolved = _q(req, "resolved", "1") not in ("0", "false", "no")
    out = collab.comments(root, rev, kind=kind, key=key, include_resolved=resolved)
    return 200, {"ok": True, "rev": rev, "comments": [_decorate_comment(c, members) for c in out]}


@_safe
def comments_post(req):
    m, b, root, members = _me(req), _body(req), _root(req), _members(req)
    anchor = b.get("anchor") if isinstance(b.get("anchor"), dict) else None
    if anchor is not None and not anchor.get("rev"):
        anchor = {**anchor, "rev": collab.head(root)}
    c = collab.add_comment(root, m["id"], b.get("body"), anchor=anchor, parent=b.get("parent") or None, members=members)
    where = c["anchor"]["rev"] + (f" ({c['anchor']['label']})" if c["anchor"]["kind"] != "rev" else "")
    _notify(req, c["mentions"], "mention", f"{m['name']} mentioned you on {where}: {c['body'][:120]}",
            {"comment": c["id"], "rev": c["anchor"]["rev"], "anchor": c["anchor"]})
    if c.get("parent"):
        thread = [x for x in collab.comments(root) if x["id"] == c["parent"] or x.get("parent") == c["parent"]]
        _notify(req, [x["by"] for x in thread if x["by"] not in c["mentions"]], "comment",
                f"{m['name']} replied on {where}: {c['body'][:120]}", {"comment": c["id"], "rev": c["anchor"]["rev"], "anchor": c["anchor"]})
    out = _decorate_comment(c, members)
    _emit(req, {"type": "comment", "action": "add", "comment": out})
    return 200, {"ok": True, "comment": out}


@_safe
def comments_resolve(req):
    m, b, root = _me(req), _body(req), _root(req)
    c = collab.comment(root, str(b.get("id") or ""))
    if c is None:
        return _err(404, "That comment no longer exists.")
    thread = collab.comment(root, c.get("parent")) if c.get("parent") else c
    if thread["by"] != m["id"] and not roles.at_least(m.get("role"), "editor"):
        return _err(403, "Only the person who started the thread or an editor can resolve it.")
    out = collab.resolve_comment(root, c["id"], m["id"], resolved=b.get("resolved", True) is not False)
    dec = _decorate_comment(out, _members(req))
    _emit(req, {"type": "comment", "action": "resolve", "comment": dec})
    return 200, {"ok": True, "comment": dec}


# ------------------------------------------------------------------ change requests


def _decorate_proposal(root: str, p: dict, members: dict, policy: dict, reviews=None, member=None) -> dict:
    """A change request for the page: its author, routing chips, review, conflicts with main, readable diff and, once it
    is built, what it built. With `member` (a GET by that member) the review says what they may decide (`mine`); events
    to every tab never carry it."""
    authors = {p.get("by")}
    r = routing(p.get("changes"), authors, members, stages=p.get("explicit_stages") or ())
    revs = [x for x in (reviews if reviews is not None else collab.reviews(root, members=members, policy=policy))
            if (x.get("target") or {}).get("proposal") == p["id"]]
    rv = revs[0] if revs else None
    if rv is not None and member:
        rv = {**rv, "mine": collab.decision_scope(root, rv, members.get(member.get("id")) or member, members, policy)}
    conflicts = collab.head_conflicts(root, p) if p.get("status") == "open" else []
    return {**{k: v for k, v in p.items() if k != "applied_in"}, "author": _author(members, p.get("by")), "routing": r,
            "review": rv, "conflicts": conflicts,
            "shown": collab.shown_changes(p.get("diff")), "built": collab.built_summary(root, p)}


@_safe
def proposals_get(req):
    root, members, pol = _root(req), _members(req), _policy(req)
    settle_stopped(root, _home(req))
    status = _q(req, "status")
    items = collab.proposals(root, status=status if status in ("open", "applied", "closed") else None)
    rvs = collab.reviews(root, members=members, policy=pol)
    me = getattr(req, "member", None)
    return 200, {"ok": True, "proposals": [_decorate_proposal(root, p, members, pol, rvs, member=me) for p in items[:100]]}


@_safe
def proposals_post(req):
    m, b, root, members, pol = _me(req), _body(req), _root(req), _members(req), _policy(req)
    stages = b.get("stages") if isinstance(b.get("stages"), list) else []
    if b.get("preview"):
        pv = collab.preview(root, b.get("changes"), b.get("base_rev") or None, stages, b.get("branch") or "main")
        return 200, {"ok": True, "preview": pv, "routing": routing(list(pv["changes"]), {m["id"]}, members, stages=stages)}
    p = collab.create_proposal(root, m["id"], b.get("title") or None, b.get("changes"), base_rev=b.get("base_rev") or None,
                               branch=b.get("branch") or "main", stages=stages, note=b.get("note") or "", members=members, policy=pol)
    out = _decorate_proposal(root, p, members, pol)
    asked, said = out["routing"]["reviewers"], set(p.get("mentions") or [])
    what = f"change request #{p['n']}: {p['title']}"
    for mid in asked:  # a reviewer who was also @mentioned gets one notification that says both
        _notify(req, [mid], "review_request", f"{m['name']} asks for your review of {what}"
                + (f", and mentioned you: {p.get('note', '')[:120]}" if mid in said else ""), {"proposal": p["id"], "review": p.get("review")})
    _notify(req, out["routing"]["watchers"], "watch", f"{m['name']} opened {what}", {"proposal": p["id"]})
    _notify(req, [x for x in said if x not in asked], "mention", f"{m['name']} mentioned you in {what}: {p.get('note', '')[:120]}",
            {"proposal": p["id"]})
    _emit(req, {"type": "proposal", "action": "open", "proposal": out})
    return 200, {"ok": True, "proposal": _decorate_proposal(root, p, members, pol, member=m)}


def _enqueue(req, job: dict) -> bool:
    """Hand the job to the room's queue: the Studio's queue_changes (W0b) builds it in turn, as the applier (`by`) on
    behalf of the author (`proposed_by`), and job_finished() records how it ended. A room with its own enqueue() (tests)
    gets the job spec; a Studio without a queue runs the edit the old way, in a thread."""
    room, studio = getattr(req, "room", None), getattr(req, "studio", None)
    for name in ("enqueue", "submit"):
        fn = getattr(room, name, None)
        if callable(fn):
            fn(job)
            return True
    queue = getattr(studio, "queue_changes", None)
    if room is not None and callable(queue):
        q = queue(room, job["changes"], label=job["label"], reply=job["text"], source="proposal", by=job.get("by"),
                  proposed_by=job.get("proposed_by"), kind="proposal")  # rooms.QueueFull: the caller reopens the request
        job["job"] = q.id
        root, home, send = room.p.root, _home(req), room.hub.send

        def finished(q):
            rev = q.result or (room.latest() if q.status == "noop" else None)  # the design already has these values:
            error = None if rev else (q.error or {"dropped": "it clashed with a newer change and was dropped"}  # applied
                                      .get(q.status, f"the build ended as {q.status}"))                     # there
            job_finished(root, job, rev=rev, error=error, home=home, send=send)
        studio.when_done(q, finished)
        return True
    apply = getattr(room, "apply", None) or getattr(getattr(req, "studio", None), "apply", None)
    if not callable(apply):
        return False
    try:
        params = inspect.signature(apply).parameters
    except (TypeError, ValueError):
        params = {}
    kw = {k: job[k] for k in ("by", "proposed_by") if k in params}
    if "reply" in params:
        kw["reply"] = job["text"]
    if "source" in params:
        kw["source"] = "proposal"
    threading.Thread(target=apply, args=(job["changes"], job["label"]), kwargs=kw, daemon=True, name="team-apply").start()
    return True


@_safe
def proposals_apply(req):
    m, b, root, members = _me(req), _body(req), _root(req), _members(req)
    settle_stopped(root, _home(req))  # applied in a Studio that has stopped since: open again (or built after all)
    job = collab.apply_proposal(root, str(b.get("id") or ""), m["id"], members=members, studio=rooms.OWNER)
    try:
        queued = _enqueue(req, job)
    except Exception as exc:  # noqa: BLE001  (a full queue, a stopped room: reopen the request and say why)
        why = durable.explain(exc, _home(req))
        collab.proposal_done(root, job["proposal"], error=f"couldn't queue the build ({why})")
        return _err(getattr(exc, "status", 409), f"Couldn't queue change request #{job['n']}: {why}")
    if not queued:
        collab.proposal_done(root, job["proposal"], error="this Studio can't build edits")
        return _err(503, "This Studio can't build edits right now, so the change request stays open.")
    p = collab.get_proposal(root, job["proposal"])
    _notify(req, [p["by"]], "proposal", f"{m['name']} applied your change request #{p['n']}: {p['title']}", {"proposal": p["id"]})
    _emit(req, {"type": "proposal", "action": "apply", "proposal": _decorate_proposal(root, p, members, _policy(req)), "job": job})
    return 202, {"ok": True, "job": job, "queued": queued}


@_safe
def proposals_close(req):
    m, b, root, members = _me(req), _body(req), _root(req), _members(req)
    settle_stopped(root, _home(req))
    p = collab.get_proposal(root, str(b.get("id") or ""))
    if p is None:
        return _err(404, "No such change request.")
    if p["by"] != m["id"] and not roles.at_least(m.get("role"), "editor"):
        return _err(403, "Only its author or an editor can close a change request.")
    p = collab.close_proposal(root, p["id"], m["id"], b.get("reason") or "")
    _notify(req, [p["by"]], "proposal", f"{m['name']} closed your change request #{p['n']}: {p['title']}", {"proposal": p["id"]})
    out = _decorate_proposal(root, p, members, _policy(req))
    _emit(req, {"type": "proposal", "action": "close", "proposal": out})
    return 200, {"ok": True, "proposal": out}


STOPPED = "the Studio stopped before it finished. Apply it again"


def reopen_stopped(studio) -> list:
    """When a team Studio starts: a change request that was applied but has no revision was being built (or waited to
    be) when the Studio that applied it stopped, so nothing will finish it (settle_stopped). No build of this Studio is
    running yet, so none is cut short. Returns [(project, id)]."""
    if not getattr(studio, "team", False):
        return []
    from . import projects
    return [(info["name"], pid) for info in projects.list_projects(studio.home)
            for pid in settle_stopped(os.path.join(studio.home, info["name"]), studio.home, starting=True)]


def built_after_all(root: str, p: dict) -> str | None:
    """The finished revision an applied change request built when its Studio stopped before it recorded it (the record
    comes a few ms after results.json): one labelled with the request's number and title, for its author, finished
    after it was applied. None when there is none."""
    from ..robot.pipeline2 import RobotProject
    label = f"#{p.get('n')} {p.get('title')}"[:48]
    since = (parse_ts(p.get("applied_ts")) or 0.0) - 1  # its times are to the second
    proj = RobotProject(root)
    for rev in reversed(proj.finished()[-50:]):
        try:
            res = proj.load_results(rev)
            when = os.path.getmtime(os.path.join(proj.rev_dir(rev), "results.json"))
        except (OSError, ValueError):
            continue
        if isinstance(res, dict) and res.get("label") == label and res.get("proposed_by") == p.get("by") and when >= since:
            return rev
    return None


def settle_stopped(root: str, home: str | None, *, starting: bool = False) -> list:
    """Change requests of project folder `root` applied in a Studio that has stopped since (its lock is free:
    rooms.owner_live) and never recorded as built: nothing will finish them, so each is recorded as built when its
    revision exists after all (built_after_all: the stop came between the build and its record), else opened again
    with why; the person who applied it and its author are told. At a start (`starting`) a request applied with no
    Studio named counts too; one this Studio applied is its own queue's. Read before a request is listed, applied or
    closed, so a request a stopped Studio applied never stays "applied" in one that keeps running. Returns the ids."""
    out = []
    try:
        applied = collab.proposals(root, status="applied")
    except Exception:  # noqa: BLE001  (an unreadable record is reported by the call that reads it next)
        return out
    project = os.path.basename(os.path.normpath(root))
    for p in applied:
        who = p.get("applied_in")
        if p.get("applied_rev") or rooms.owner_live(home, who) or (not starting and who in (None, rooms.OWNER)):
            continue
        rev = built_after_all(root, p)
        collab.proposal_done(root, p["id"], rev=rev, error=None if rev else STOPPED)
        out.append(p["id"])
        text = (f"Change request #{p.get('n')} built {rev}." if rev else
                f"Change request #{p.get('n')} wasn't built: the Studio stopped. It is open again, to apply again.")
        for mid in {p.get("applied_by"), p.get("by")} - {None}:
            activity.notify(home, mid, "job", text, project=project, ref={"proposal": p["id"], "rev": rev})
    return out


def job_finished(project_root: str, job: dict, rev: str | None = None, error: str | None = None, home: str | None = None, send=None):
    """The room's queue calls this when a team job ends: records the change request's revision (or reopens it on an
    error), moves its branch, and tells the applier and the author."""
    home = home or CONFIG["home"]
    project = os.path.basename(os.path.normpath(project_root))
    if job.get("proposal"):
        collab.proposal_done(project_root, job["proposal"], rev=rev, error=error)
    if rev and job.get("branch") and job["branch"] != "main":
        try:
            collab.advance_branch(project_root, job["branch"], rev, by=job.get("by"))
        except collab.CollabError:
            pass
    what = f"change request #{job.get('n')}" if job.get("proposal") else "your edit"
    text = f"{what} built {rev}" if rev else f"{what} failed: {str(error or 'unknown error')[:160]}"
    for mid in {job.get("by"), job.get("proposed_by")} - {None}:
        activity.notify(home, mid, "job", text[0].upper() + text[1:], project=project, ref={"rev": rev, "proposal": job.get("proposal")})
    if send:
        try:
            send({"type": "activity", "project": project, "text": text})
        except Exception:  # noqa: BLE001
            pass


# ------------------------------------------------------------------ reviews and releases


@_safe
def reviews_get(req):
    root, members, pol = _root(req), _members(req), _policy(req)
    rev, prop = _q(req, "rev"), _q(req, "proposal")
    st = _q(req, "state")
    items = collab.reviews(root, rev=rev, proposal=prop, state=st, members=members, policy=pol)
    me = getattr(req, "member", None)
    if me:  # what this member may decide on each, and why not (the page's buttons and notes)
        mm = members.get(me.get("id")) or me
        items = [{**x, "mine": collab.decision_scope(root, x, mm, members, pol)} for x in items[:100]]
    out = {"ok": True, "reviews": items[:100]}
    if rev:
        out["gate"] = collab.release_gate(root, rev, members, pol)
    return 200, out


@_safe
def reviews_post(req):
    m, b, root, members, pol = _me(req), _body(req), _root(req), _members(req), _policy(req)
    rv = collab.request_review(root, m["id"], rev=b.get("rev") or None, proposal=b.get("proposal") or None,
                               disciplines=b.get("disciplines") or None, note=b.get("note") or "", members=members, policy=pol)
    what = rv["target"]["rev"] or collab.proposal_name(root, rv["target"]["proposal"])
    asked = sorted({x for r in rv["requested_from"] for x in r["members"]})
    _notify(req, asked, "review_request", f"{m['name']} asks for your review of {what}: "
            + ", ".join(LABELS[d] for d in rv["disciplines"]), {"review": rv["id"], "rev": rv["target"]["rev"]})
    watchers = [mid for mid, x in members.items() if not x.get("revoked") and watching(x) and mid not in asked]
    _notify(req, watchers, "watch", f"{m['name']} asked for review of {what}", {"review": rv["id"], "rev": rv["target"]["rev"]})
    _emit(req, {"type": "review", "action": "request", "review": rv})
    return 200, {"ok": True, "review": rv}


@_safe
def reviews_decide(req):
    m, b, root, members, pol = _me(req), _body(req), _root(req), _members(req), _policy(req)
    rv = collab.decide(root, str(b.get("review") or ""), members.get(m["id"]) or m, b.get("decision"), b.get("note") or "",
                       disciplines=b.get("disciplines") or None, members=members, policy=pol)
    last = rv["decisions"][-1]
    verb = "approved" if last["decision"] == "approve" else "requested changes on"
    what = rv["target"]["rev"] or collab.proposal_name(root, rv["target"]["proposal"])
    tell = set(rv.get("authors") or []) | {rv.get("by")}
    note = f": {last['note'][:140]}" if last.get("note") else ""
    _notify(req, sorted(x for x in tell if x), "decision", f"{m['name']} {verb} {what} as "
            + activity._and([LABELS[d] for d in last["disciplines"]]) + note, {"review": rv["id"], "rev": rv["target"]["rev"]})
    out = {"ok": True, "review": rv}
    if rv["target"]["rev"]:
        out["gate"] = collab.release_gate(root, rv["target"]["rev"], members, pol)
    _emit(req, {"type": "review", "action": "decide", "review": rv, "gate": out.get("gate")})
    return 200, out


@_safe
def releases_get(req):
    root, members, pol = _root(req), _members(req), _policy(req)
    out = {"ok": True, "releases": collab.releases(root), "pinned": sorted(collab.pinned(root))}
    rev = _q(req, "rev")
    if rev:
        out["gate"] = collab.release_gate(root, rev, members, pol)
    return 200, out


@_safe
def releases_post(req):
    m, b, root, members, pol = _me(req), _body(req), _root(req), _members(req), _policy(req)
    rev = b.get("rev") or collab.head(root)
    r = collab.create_release(root, b.get("tag"), rev, m["id"], members, pol, b.get("note") or "", home=_home(req))
    everyone = [x for x in members if not members[x].get("revoked")]
    _notify(req, everyone, "release", f"{m['name']} released {rev} as {r['tag']} ({r['level']})", {"rev": rev, "tag": r["tag"]})
    _emit(req, {"type": "activity", "kind": "release", "release": r})
    return 200, {"ok": True, "release": r}


@_safe
def branches_get(req):
    root = _root(req)
    cmp = _q(req, "compare")
    if cmp:
        a, _, bb = cmp.partition("...")
        return 200, {"ok": True, "compare": collab.compare(root, a, bb or "main")}
    return 200, {"ok": True, "branches": collab.branches(root), "main": collab.head(root)}


@_safe
def branches_post(req):
    m, b, root, members, pol = _me(req), _body(req), _root(req), _members(req), _policy(req)
    act = b.get("action") or "create"
    if act == "create":
        out = collab.create_branch(root, b.get("name"), m["id"], b.get("base_rev") or None)
    elif act == "advance":
        out = collab.advance_branch(root, str(b.get("name") or ""), str(b.get("rev") or ""), by=m["id"])
    elif act == "compare":
        return 200, {"ok": True, "compare": collab.compare(root, b.get("a") or "main", b.get("b") or "main")}
    elif act == "merge":
        p = collab.merge_as_proposal(root, str(b.get("name") or ""), m["id"], b.get("title") or None, members=members, policy=pol)
        out = _decorate_proposal(root, p, members, pol)
        _notify(req, out["routing"]["reviewers"], "review_request",
                f"{m['name']} asks for your review of change request #{p['n']}: {p['title']}", {"proposal": p["id"]})
        _emit(req, {"type": "proposal", "action": "open", "proposal": out})
        return 200, {"ok": True, "proposal": out}
    else:
        return _err(400, "action is create, advance, compare or merge.")
    _emit(req, {"type": "activity", "kind": "branch", "branch": out})
    return 200, {"ok": True, "branch": out, "branches": collab.branches(root)}


# ------------------------------------------------------------------ activity, inbox, presence, audit


@_safe
def activity_get(req):
    before = _q(req, "before")
    try:
        before = float(before) if before else None
        limit = int(_q(req, "limit", "50"))
    except ValueError:
        return _err(400, "before is epoch seconds and limit a number.")
    return 200, {"ok": True, "items": activity.feed(_root(req), _members(req), limit=limit, before=before)}


@_safe
def inbox_get(req):
    m = _me(req)
    try:
        limit = int(_q(req, "limit", "50"))
    except ValueError:
        limit = 50
    return 200, {"ok": True, **activity.inbox(_home(req), m["id"], limit=limit)}


@_safe
def inbox_read(req):
    m, b = _me(req), _body(req)
    upto = b.get("upto")
    return 200, {"ok": True, **activity.mark_read(_home(req), m["id"], int(upto) if isinstance(upto, int) else None)}


@_safe
def presence_post(req):
    m, b = _me(req), _body(req)
    project, members = _project(req), _members(req)
    before = {(x["id"], x["state"]) for x in activity.PRESENCE.snapshot(project, members)}
    activity.PRESENCE.touch(project, m["id"], tab=b.get("tab"), rev=b.get("rev"))
    snap = activity.PRESENCE.snapshot(project, members)
    if {(x["id"], x["state"]) for x in snap} != before:
        _emit(req, {"type": "presence", "members": snap})
    return 200, {"ok": True, "members": snap}


def stream(project: str, member: dict | None, opened: bool, send=None, home: str | None = None):
    """The event stream hook: count a member's open tabs for presence and broadcast the change."""
    if not member:
        return
    activity.PRESENCE.connected(project, member["id"], 1 if opened else -1)
    if send:
        try:
            send({"type": "presence", "project": project,
                  "members": activity.PRESENCE.snapshot(project, TeamStore(home or CONFIG["home"]).read().get("members") or {})})
        except Exception:  # noqa: BLE001
            pass


@_safe
def audit_get(req):
    try:
        limit = max(1, min(int(_q(req, "limit", "200")), 1000))
    except ValueError:
        limit = 200
    return 200, {"ok": True, "entries": audit.entries(_home(req), limit=limit), "verify": audit.verify(_home(req))}


def check_download(member: dict | None, project_root: str, rel: str, home: str | None = None) -> dict:
    """For /files/<project>/<rev>/... in team mode: may this member download it? Draft downloads of order packages
    (unreleased revision) are allowed per policy and written to the audit log."""
    rev, _, inner = (rel or "").replace("\\", "/").lstrip("/").partition("/")
    pol = TeamStore(home or CONFIG["home"]).policy()
    res = collab.download(project_root, rev, inner, member, pol)
    if res["allowed"] and res["draft"]:
        audit.append("download_gated", by=(member or {}).get("id"), home=home or CONFIG["home"],
                     project=os.path.basename(os.path.normpath(project_root)), rev=rev, file=inner[:160], draft=True)
    return res


def export_parts(member: dict | None, project_root: str, rev: str | None, full: bool, home: str | None = None) -> tuple:
    """For /api/export on a team Studio: (keep(rel), extra, drafts). keep: the exported revision's files that this member
    may take, by the same rule as /files (order files of a revision nobody released go to owners, and to editors when
    the policy is "warn"); extra: team.json, the team's record (a revision's approvals and releases, or the whole
    project's comments, change requests, reviews, branches and releases); drafts: the draft order files that went in."""
    home = home or CONFIG["home"]
    store = TeamStore(home)
    members, pol = store.read().get("members") or {}, store.policy()
    drafts = set()

    def keep(rel: str) -> bool:
        res = collab.download(project_root, rev, rel, member, pol)
        if res["allowed"] and res["draft"]:
            drafts.add(rel)
        return res["allowed"]

    if full:
        record = collab.export_bundle(project_root, members)
    elif rev:
        record = collab.revision_record(project_root, rev, members, pol)
    else:
        return None, None, drafts  # no revision: the export refuses it
    return (keep if rev else None), {"team.json": record}, drafts


# ------------------------------------------------------------------ registration

ROUTES = [
    ("GET", "/api/me", me_get, {}), ("POST", "/api/me", me_post, {}), ("POST", "/api/me/link", me_link, {}),
    ("POST", "/api/me/tokens", me_tokens, {}), ("POST", "/api/signout", signout, {}),
    ("GET", "/api/team", team_get, {}), ("POST", "/api/team/join", team_join, {"raw_body": True, "max_body": MAX_FORM}),
    ("POST", "/api/team/invites", team_invites, {}), ("POST", "/api/team/revoke", team_revoke, {}),
    ("POST", "/api/team/members", team_members, {}), ("POST", "/api/team/policy", team_policy, {}),
    ("GET", "/api/comments", comments_get, {}), ("POST", "/api/comments", comments_post, {}),
    ("POST", "/api/comments/resolve", comments_resolve, {}),
    ("GET", "/api/proposals", proposals_get, {}), ("POST", "/api/proposals", proposals_post, {}),
    ("POST", "/api/proposals/apply", proposals_apply, {}), ("POST", "/api/proposals/close", proposals_close, {}),
    ("GET", "/api/reviews", reviews_get, {}), ("POST", "/api/reviews", reviews_post, {}), ("POST", "/api/reviews/decide", reviews_decide, {}),
    ("GET", "/api/activity", activity_get, {}), ("GET", "/api/inbox", inbox_get, {}), ("POST", "/api/inbox/read", inbox_read, {}),
    ("POST", "/api/presence", presence_post, {}), ("GET", "/api/audit", audit_get, {}),
    ("GET", "/api/branches", branches_get, {}), ("POST", "/api/branches", branches_post, {}),
    ("GET", "/api/releases", releases_get, {}), ("POST", "/api/releases", releases_post, {}),
]


def _team_only(method: str, path: str, fn):
    """A route that answers only on a team Studio (morph studio --team): elsewhere GET /api/me says Teams is off (the
    page then adds nothing) and every other Teams route is 404, so a Studio without Teams has no Teams surface."""
    def run(req):
        if getattr(getattr(req, "studio", None), "team", False):
            return fn(req)
        if (method, path) == ("GET", "/api/me"):
            return 200, {"ok": True, "team": False, "member": None}
        return _err(404, "Teams is off on this Studio: start it with morph studio --team.")
    run.__name__ = fn.__name__
    run.__doc__ = fn.__doc__
    return run


def register(route=None):
    """Register every Teams route with W0b's router, each with its role from teams/roles.py ("public" for join). With
    the Studio's own router (the default) each route answers only on a team Studio (_team_only)."""
    real = route is None
    if real:
        from .routes import route  # noqa: F811  (W0b)
    for method, path, fn, opts in ROUTES:
        role, _ = roles.required(method, path)
        route(method, path, role=role, **opts)(_team_only(method, path, fn) if real else fn)
    return ROUTES


def dispatch(req):
    """One entry point for every Teams route (for routers that prefer a single handler per prefix)."""
    path = (req.path or "").split("?", 1)[0].rstrip("/") or "/"
    for method, p, fn, _ in ROUTES:
        if method == req.method and p == path:
            return fn(req)
    return _err(404, "Not found.")


register()  # the Studio loads every api_*.py at start (contracts section 2); the routes answer only in team mode


STARTED.append(reopen_stopped)
