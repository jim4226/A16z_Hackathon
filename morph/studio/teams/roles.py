"""Who may call what in team mode: one table gives every Studio route its minimum role.

Roles are team-wide: owner > editor > viewer. Viewers see everything and, when the policy allows (the default),
comment and propose changes; editors also edit, build, import, export, request reviews, apply change requests
and cut releases; only owners manage members, invites, policy, keys, settings, the audit log and resets.
Approving is decided per discipline by the review itself (collab.decide), on top of the route check here.

authorize(member, method, path) is the hook the router calls for every request when Teams is on. A route that
isn't in the table needs an editor to read it and an owner to change anything, so a new feature that forgets to
list its routes fails closed. A role declared by the router (`route(..., role=)`) can only raise the bar.
"""

from __future__ import annotations

RANK = {"viewer": 1, "editor": 2, "owner": 3}
PUBLIC = "public"
READS = ("GET", "HEAD")
UNKNOWN_READ, UNKNOWN_WRITE = "editor", "owner"

# (method, path) -> role, or (role, policy flag a viewer needs). A path ending in "/*" covers everything below it.
ROUTES = {
    # the page, its assets and revision files
    ("GET", "/"): "viewer", ("GET", "/index.html"): "viewer", ("GET", "/static/*"): "viewer", ("GET", "/files/*"): "viewer",
    ("GET", "/api/state"): "viewer", ("GET", "/api/rev"): "viewer", ("GET", "/api/events"): "viewer",
    # design edits and services
    ("POST", "/api/chat"): "editor", ("POST", "/api/params"): "editor", ("POST", "/api/source"): "editor",
    ("POST", "/api/project"): "viewer",            # rooms: switching only moves this tab
    ("GET", "/api/jobs"): "viewer", ("POST", "/api/jobs/cancel"): "editor", ("POST", "/api/jobs/resolve"): "editor",
    ("GET", "/api/export"): "viewer",
    # host administration
    ("POST", "/api/reset"): "owner",
    ("GET", "/api/keys"): "owner", ("POST", "/api/keys"): "owner", ("POST", "/api/keys/check"): "owner",
    ("GET", "/api/settings"): "viewer", ("POST", "/api/settings"): "owner",
    # projects (W0b)
    ("GET", "/api/projects"): "viewer", ("POST", "/api/projects"): "editor", ("POST", "/api/projects/fork"): "editor",
    ("DELETE", "/api/projects"): "owner",
    # new robot brief (F1)
    ("GET", "/api/brief"): "viewer", ("POST", "/api/brief"): "editor",
    # import (F2)
    ("POST", "/api/import"): "editor", ("GET", "/api/import/*"): "editor", ("POST", "/api/import/*"): "editor",
    ("PUT", "/api/import/*"): "editor", ("DELETE", "/api/import/*"): "editor",
    # video and photos (F3)
    ("GET", "/api/video/info"): "viewer", ("POST", "/api/video/analyze"): "editor", ("PUT", "/api/video/upload"): "editor",
    ("POST", "/api/video/apply"): "editor",
    # world mode (F5)
    ("GET", "/api/world"): "viewer", ("POST", "/api/world"): "editor", ("PUT", "/api/world/upload"): "editor",
    ("POST", "/api/world/obstacles/auto"): "editor", ("POST", "/api/world/inspect"): "editor",
    # robustness studies (B2): viewers read results and replay runs; editors start, stop and set tolerances
    ("GET", "/api/study"): "viewer", ("POST", "/api/study"): "editor", ("POST", "/api/study/cancel"): "editor",
    ("POST", "/api/study/resume"): "editor", ("GET", "/api/study/tolerances"): "viewer",
    ("POST", "/api/study/tolerances"): "editor", ("GET", "/api/study/replay"): "viewer",
    # teams (F4)
    ("POST", "/api/team/join"): PUBLIC,
    ("GET", "/api/me"): "viewer", ("POST", "/api/me"): "viewer", ("POST", "/api/me/link"): "viewer",
    ("POST", "/api/me/tokens"): "viewer", ("POST", "/api/signout"): "viewer",
    ("GET", "/api/team"): "viewer", ("POST", "/api/team/invites"): "owner", ("POST", "/api/team/revoke"): "owner",
    ("POST", "/api/team/members"): "owner", ("POST", "/api/team/policy"): "owner",
    ("GET", "/api/comments"): "viewer", ("POST", "/api/comments"): ("viewer", "viewers_can_comment"),
    ("POST", "/api/comments/resolve"): ("viewer", "viewers_can_comment"),
    ("GET", "/api/proposals"): "viewer", ("POST", "/api/proposals"): ("viewer", "viewers_can_propose"),
    ("POST", "/api/proposals/apply"): "editor", ("POST", "/api/proposals/close"): ("viewer", "viewers_can_propose"),
    ("GET", "/api/reviews"): "viewer", ("POST", "/api/reviews"): "editor",
    ("POST", "/api/reviews/decide"): ("viewer", "viewers_can_approve"),
    ("GET", "/api/activity"): "viewer", ("GET", "/api/inbox"): "viewer", ("POST", "/api/inbox/read"): "viewer",
    ("POST", "/api/presence"): "viewer",
    ("GET", "/api/audit"): "owner",
    ("GET", "/api/branches"): "viewer", ("POST", "/api/branches"): "editor",
    ("GET", "/api/releases"): "viewer", ("POST", "/api/releases"): "editor",
    # agent tools (beta, api_tools): the decide handler lets editors skip and only owners allow or deny
    ("GET", "/api/tools"): "viewer", ("POST", "/api/tools/enable"): "owner", ("POST", "/api/tools/disable"): "owner",
    ("GET", "/api/runs"): "viewer", ("GET", "/api/runs/answer"): "viewer", ("POST", "/api/runs/start"): "editor",
    ("POST", "/api/runs/decide"): "editor", ("POST", "/api/runs/resume"): "editor", ("POST", "/api/runs/cancel"): "editor",
    # Morph writes a tool (preview, api_tools): only the owner drafts, approves or discards; Darwin's suggestions for editors
    ("POST", "/api/tools/draft"): "owner", ("GET", "/api/tools/drafts"): "viewer", ("POST", "/api/tools/drafts/approve"): "owner",
    ("POST", "/api/tools/drafts/discard"): "owner", ("GET", "/api/tools/suggest"): "editor",
}


def _norm(method: str, path: str) -> tuple:
    m = (method or "GET").upper()
    m = "GET" if m == "HEAD" else m
    p = (path or "/").split("?", 1)[0].split("#", 1)[0] or "/"
    if len(p) > 1:
        p = p.rstrip("/") or "/"
    return m, p


def required(method: str, path: str) -> tuple:
    """(minimum role or "public", policy flag a viewer needs or None) for one request."""
    m, p = _norm(method, path)
    spec = ROUTES.get((m, p))
    if spec is None:
        best = None
        for (rm, rp), s in ROUTES.items():
            if rm == m and rp.endswith("/*") and (p + "/").startswith(rp[:-1]) and (best is None or len(rp) > len(best[0])):
                best = (rp, s)
        spec = best[1] if best else (UNKNOWN_READ if m in READS else UNKNOWN_WRITE)
    return (spec, None) if isinstance(spec, str) else (spec[0], spec[1])


def at_least(role, need: str) -> bool:
    return RANK.get(role, 0) >= RANK.get(need, 99)


def authorize(member, method: str, path: str, policy=None, declared: str | None = None) -> bool:
    """True when `member` ({"role", "revoked"} or None when signed out) may make this request."""
    need, flag = required(method, path)
    if declared and declared != PUBLIC and RANK.get(declared, 99) > RANK.get(need, 0):
        need = declared  # the stricter of the two wins; a router can't open a route this table protects
    if need == PUBLIC:
        return True
    if not isinstance(member, dict) or member.get("revoked"):
        return False
    role = member.get("role")
    if not at_least(role, need):
        return False
    if flag and not at_least(role, "editor"):
        return bool((policy or {}).get(flag))
    return True


def can(member, action: str, policy=None) -> bool:
    """Object-level checks the handlers make on top of the route table (what the UI greys out)."""
    if not isinstance(member, dict) or member.get("revoked"):
        return False
    role = member.get("role")
    flags = {"comment": "viewers_can_comment", "propose": "viewers_can_propose", "approve": "viewers_can_approve"}
    if action in flags:
        return at_least(role, "editor") or bool((policy or {}).get(flags[action]))
    need = {"edit": "editor", "apply": "editor", "request_review": "editor", "release": "editor", "branch": "editor",
            "admin": "owner", "invite": "owner", "keys": "owner", "audit": "owner", "view": "viewer"}.get(action, "owner")
    return at_least(role, need)


def table() -> list:
    """The route table as rows, for docs and the Share dialog's role help."""
    rows = []
    for (m, p), s in sorted(ROUTES.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        role, flag = (s, None) if isinstance(s, str) else s
        rows.append({"method": m, "path": p, "role": role, "policy": flag})
    return rows
