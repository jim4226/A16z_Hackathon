"""`morph team ...` from the Studio computer's shell, which has owner authority (it can read the team files anyway).

    morph team init --owner Jim [--name "R Cubed"]      create the team; prints a one-time owner link
    morph team link [member]                            a new one-time sign-in link (default: the owner)
    morph team invite --role editor --disciplines electronics,manufacturing --name Ana
    morph team members                                  who is on the team
    morph team revoke <member id or name>               end that member's sessions and tokens now
    morph team audit [--verify]                         the security log, newest first; --verify checks the chain

Wire it into morph/cli.py with `teams_cli.add_parser(sub)`; each command is also a plain function returning a
dict, so tests and the launcher can call it directly. Output is ASCII only (Windows consoles).
"""

from __future__ import annotations

import os

from . import audit, auth
from .disciplines import LABELS
from .store import TeamStore, exists, utc

BASE_URL = "http://127.0.0.1:8765"


def _url(base_url: str | None, path: str) -> str:
    return (base_url or os.environ.get("MORPH_TEAM_URL") or BASE_URL).rstrip("/") + path


def _say(out, text: str):
    if out:
        out(text.encode("ascii", "replace").decode("ascii"))


def team_init(owner_name: str, team_name: str = "", home: str | None = None, base_url: str | None = None, out=print) -> dict:
    """Create the team; the creator is owner + generalist. Returns {"member", "team", "code", "path", "url", "expires_utc"}."""
    res = auth.init_team(owner_name, team_name, home=home)
    res["url"] = _url(base_url, res["path"])
    _say(out, f"Team \"{res['team']}\" created. {res['member']['name']} is the owner and a generalist (reviews every stage "
              "that has no specialist yet).")
    _say(out, "Next, start the Studio in team mode: morph studio --team (add --remote so teammates' computers can reach it).")
    _say(out, "It prints a one-time link that signs you in; then invite people with Share in the Studio, or morph team invite.")
    _say(out, f"If a team Studio is already running at {_url(base_url, '')}, this link signs you in (once, within 15 minutes):")
    _say(out, f"    {res['url']}")
    return res


def team_link(member: str | None = None, home: str | None = None, base_url: str | None = None, out=print) -> dict:
    """A new one-time sign-in link for a member (id or name; default: the first owner)."""
    if not exists(home):
        raise SystemExit("There is no team here yet: run `morph team init --owner <your name>` first.")
    if member:
        m = auth.find_member(member, home)
        if m is None or m.get("revoked"):
            raise SystemExit(f"No active member called {member!r}. `morph team members` lists them.")
    else:
        owners = [m for m in TeamStore(home).members().values() if m.get("role") == "owner"]
        if not owners:
            raise SystemExit("The team has no owner.")
        m = sorted(owners, key=lambda x: x.get("created_utc") or "")[0]
    res = auth.create_link(m["id"], by=None, home=home)
    res["url"], res["member"] = _url(base_url, res["path"]), auth.public_member(m)
    _say(out, f"Sign-in link for {m['name']} ({m['role']}), works once within 15 minutes:")
    _say(out, f"    {res['url']}")
    return res


def team_invite(role: str = "editor", disciplines=(), name_hint: str = "", home: str | None = None, base_url: str | None = None,
                days: float = auth.INVITE_DAYS, out=print) -> dict:
    if not exists(home):
        raise SystemExit("There is no team here yet: run `morph team init --owner <your name>` first.")
    res = auth.create_invite(None, role, disciplines, name_hint, home=home, days=days)
    res["url"] = _url(base_url, res["path"])
    tags = ", ".join(LABELS.get(d, d) for d in res["invite"]["disciplines"]) or "no discipline"
    _say(out, f"Invite for {name_hint or 'a new member'}: {role}, {tags}. Works once, until {res['invite']['expires_utc']}:")
    _say(out, f"    {res['url']}")
    _say(out, "Send it privately: whoever opens it first joins the team.")
    return res


def team_members(home: str | None = None, out=print) -> list:
    ms = sorted((TeamStore(home).read().get("members") or {}).values(), key=lambda m: m.get("created_utc") or "")
    for m in ms:
        tags = ", ".join(LABELS.get(d, d) for d in m.get("disciplines") or []) or "-"
        _say(out, f"  {m['id']}  {m['name']:<20} {m['role']:<7} {tags}{'  (revoked)' if m.get('revoked') else ''}")
    return [auth.public_member(m) for m in ms]


def team_revoke(member: str, home: str | None = None, out=print) -> dict:
    m = auth.find_member(member, home)
    if m is None:
        raise SystemExit(f"No member called {member!r}. `morph team members` lists them.")
    try:
        res = auth.revoke_member(m["id"], by=None, home=home)
    except auth.AuthError as exc:
        raise SystemExit(str(exc)) from None
    _say(out, f"{res['name']} is signed out everywhere; their links and script tokens no longer work. Their name stays on past work.")
    return res


def team_audit_verify(home: str | None = None, out=print) -> dict:
    res = audit.verify(home)
    if res["ok"]:
        _say(out, f"Audit log OK: {res['lines']} entries, hash chain intact ({utc()}).")
    else:
        _say(out, f"Audit log FAILED at entry {res['line']}: {res['error']}.")
    return res


def team_audit(home: str | None = None, limit: int = 30, out=print) -> list:
    rows = audit.entries(home, limit=limit)
    for e in rows:
        extra = " ".join(f"{k}={v}" for k, v in e.items() if k not in ("ts", "event", "by"))
        _say(out, f"  {e.get('ts')}  {e.get('event'):<20} by={e.get('by') or 'host'}  {extra}"[:200])
    return rows


def add_parser(sub):
    """Register `morph team ...` on morph/cli.py's subparsers; returns the parser (its func is run)."""
    p = sub.add_parser("team", help="Morph Teams (preview): invites, members, sign-in links, audit log")
    tsub = p.add_subparsers(dest="team_cmd", required=True)
    a = tsub.add_parser("init", help="create the team (you become the owner)")
    a.add_argument("--owner", required=True, help="your display name")
    a.add_argument("--name", default="", help="team name")
    a.add_argument("--url", default=None, help=f"the Studio's address as teammates reach it (default {BASE_URL})")
    a = tsub.add_parser("link", help="a one-time sign-in link (default: the owner)")
    a.add_argument("member", nargs="?", default=None)
    a.add_argument("--url", default=None)
    a = tsub.add_parser("invite", help="a single-use invite link (7 days)")
    a.add_argument("--role", default="editor", choices=auth.ROLES)
    a.add_argument("--disciplines", default="", help="comma list: " + ",".join(LABELS))
    a.add_argument("--name", default="", help="name hint shown on the join page")
    a.add_argument("--url", default=None)
    tsub.add_parser("members", help="list members")
    a = tsub.add_parser("revoke", help="sign a member out everywhere and remove their access")
    a.add_argument("member")
    a = tsub.add_parser("audit", help="show or verify the security log")
    a.add_argument("--verify", action="store_true")
    a.add_argument("--limit", type=int, default=30)
    p.set_defaults(func=run)
    return p


def run(a) -> int:
    home = getattr(a, "home", None) or os.environ.get("MORPH_HOME") or None
    try:
        if a.team_cmd == "init":
            team_init(a.owner, a.name, home=home, base_url=a.url)
        elif a.team_cmd == "link":
            team_link(a.member, home=home, base_url=a.url)
        elif a.team_cmd == "invite":
            team_invite(a.role, [d for d in a.disciplines.split(",") if d], a.name, home=home, base_url=a.url)
        elif a.team_cmd == "members":
            team_members(home=home)
        elif a.team_cmd == "revoke":
            team_revoke(a.member, home=home)
        elif a.team_cmd == "audit":
            if a.verify:
                return 0 if team_audit_verify(home=home)["ok"] else 1
            team_audit(home=home, limit=a.limit)
    except (auth.AuthError, ValueError) as exc:
        raise SystemExit(str(exc)) from None
    return 0
