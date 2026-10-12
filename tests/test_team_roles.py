"""The team route table: every route in RELEASE_CONTRACTS section 2 plus the existing Studio routes has a minimum role;
viewers never reach a mutating route except the ones the policy opens to them; unknown routes fail closed."""

import pytest

from morph.studio.teams import roles

# RELEASE_CONTRACTS.md section 2, plus the Studio's existing routes: (method, path, minimum role)
CONTRACT = [
    # existing Studio routes
    ("GET", "/", "viewer"), ("GET", "/index.html", "viewer"), ("GET", "/static/app.js", "viewer"),
    ("GET", "/files/egg-picker/r003/cad/robot.glb", "viewer"), ("GET", "/api/state", "viewer"), ("GET", "/api/rev", "viewer"),
    ("GET", "/api/events", "viewer"), ("POST", "/api/chat", "editor"), ("POST", "/api/params", "editor"),
    ("POST", "/api/source", "editor"), ("POST", "/api/project", "viewer"), ("POST", "/api/reset", "owner"),
    ("GET", "/api/keys", "owner"), ("POST", "/api/keys", "owner"), ("POST", "/api/keys/check", "owner"),
    ("GET", "/api/settings", "viewer"), ("POST", "/api/settings", "owner"), ("GET", "/api/export", "viewer"),
    # W0b projects
    ("GET", "/api/projects", "viewer"), ("POST", "/api/projects", "editor"), ("POST", "/api/projects/fork", "editor"),
    ("DELETE", "/api/projects", "owner"),
    # F1 brief
    ("POST", "/api/brief", "editor"), ("GET", "/api/brief", "viewer"),
    # F2 import
    ("POST", "/api/import", "editor"), ("PUT", "/api/import/0123456789abcdef/file", "editor"),
    ("POST", "/api/import/0123456789abcdef/detect", "editor"), ("GET", "/api/import/0123456789abcdef/preview.glb", "editor"),
    ("POST", "/api/import/0123456789abcdef/commit", "editor"), ("DELETE", "/api/import/0123456789abcdef", "editor"),
    # F3 video
    ("POST", "/api/video/analyze", "editor"), ("PUT", "/api/video/upload", "editor"), ("POST", "/api/video/apply", "editor"),
    # F4 teams
    ("GET", "/api/me", "viewer"), ("GET", "/api/team", "viewer"), ("POST", "/api/team/join", "public"),
    ("POST", "/api/team/invites", "owner"), ("POST", "/api/team/revoke", "owner"), ("POST", "/api/team/members", "owner"),
    ("POST", "/api/team/policy", "owner"), ("GET", "/api/comments", "viewer"), ("POST", "/api/comments", "viewer"),
    ("POST", "/api/comments/resolve", "viewer"), ("GET", "/api/proposals", "viewer"), ("POST", "/api/proposals", "viewer"),
    ("POST", "/api/proposals/apply", "editor"), ("GET", "/api/reviews", "viewer"), ("POST", "/api/reviews", "editor"),
    ("POST", "/api/reviews/decide", "viewer"), ("GET", "/api/activity", "viewer"), ("GET", "/api/inbox", "viewer"),
    ("POST", "/api/presence", "viewer"), ("GET", "/api/audit", "owner"), ("GET", "/api/branches", "viewer"),
    ("POST", "/api/branches", "editor"), ("GET", "/api/releases", "viewer"), ("POST", "/api/releases", "editor"),
    # F5 world
    ("GET", "/api/world", "viewer"), ("POST", "/api/world", "editor"), ("PUT", "/api/world/upload", "editor"),
    ("POST", "/api/world/obstacles/auto", "editor"), ("POST", "/api/world/inspect", "editor"),
]
# mutating routes a viewer may call: their own profile, presence and inbox, a tab's project, and the policy-gated ones
VIEWER_WRITES = {"/api/project", "/api/me", "/api/me/link", "/api/me/tokens", "/api/signout", "/api/presence", "/api/inbox/read",
                 "/api/comments", "/api/comments/resolve", "/api/proposals", "/api/proposals/close", "/api/reviews/decide",
                 "/api/team/join"}
POLICY = {"viewers_can_comment": True, "viewers_can_propose": True, "viewers_can_approve": False}
OWNER, EDITOR, VIEWER = ({"id": "m1", "role": r} for r in ("owner", "editor", "viewer"))


@pytest.mark.parametrize("method,path,role", CONTRACT)
def test_every_contract_route_has_its_minimum_role(method, path, role):
    assert roles.required(method, path)[0] == role
    for m, ok in ((OWNER, True), (EDITOR, role in ("public", "viewer", "editor")), (VIEWER, role in ("public", "viewer"))):
        flag = roles.required(method, path)[1]
        expect = ok and (not flag or m is not VIEWER or POLICY.get(flag, False))
        assert roles.authorize(m, method, path, POLICY) is expect, (m["role"], method, path)


def test_viewers_never_reach_a_mutating_route_outside_the_allowed_set():
    for row in roles.table():
        if row["method"] == "GET":
            continue
        path = row["path"].replace("/*", "/0123456789abcdef")
        allowed = roles.authorize(VIEWER, row["method"], path, POLICY)
        assert allowed == (row["path"] in VIEWER_WRITES and row["path"] != "/api/reviews/decide"), row


def test_policy_switches_open_and_close_viewer_comments_and_proposals():
    closed = {"viewers_can_comment": False, "viewers_can_propose": False, "viewers_can_approve": False}
    assert not roles.authorize(VIEWER, "POST", "/api/comments", closed)
    assert not roles.authorize(VIEWER, "POST", "/api/proposals", closed)
    assert roles.authorize(EDITOR, "POST", "/api/comments", closed)      # the switches only concern viewers
    assert roles.authorize(VIEWER, "POST", "/api/reviews/decide", {**closed, "viewers_can_approve": True})
    assert roles.can(VIEWER, "comment", POLICY) and not roles.can(VIEWER, "comment", closed)
    assert roles.can(EDITOR, "apply") and not roles.can(VIEWER, "apply", POLICY) and roles.can(OWNER, "admin")
    assert not roles.can(EDITOR, "keys") and not roles.can(EDITOR, "audit")


def test_unknown_routes_fail_closed_and_declared_roles_only_raise_the_bar():
    assert roles.required("GET", "/api/brand-new-thing") == ("editor", None)
    assert roles.required("POST", "/api/brand-new-thing") == ("owner", None)
    assert roles.required("PATCH", "/api/me") == ("owner", None)
    assert not roles.authorize(EDITOR, "POST", "/api/brand-new-thing", POLICY)
    assert roles.authorize(OWNER, "DELETE", "/api/brand-new-thing", POLICY)
    assert not roles.authorize(EDITOR, "GET", "/api/state", POLICY, declared="owner")   # stricter declared role wins
    assert roles.authorize(VIEWER, "GET", "/api/state", POLICY, declared="viewer")
    assert not roles.authorize(VIEWER, "POST", "/api/reset", POLICY, declared="viewer")  # but never loosens the table
    assert not roles.authorize(None, "POST", "/api/team/join", POLICY, declared="editor")
    assert roles.authorize(None, "POST", "/api/team/join", POLICY, declared="public")


def test_signed_out_and_revoked_members_get_nothing_but_join():
    for method, path, role in CONTRACT:
        assert roles.authorize(None, method, path, POLICY) is (role == "public")
        assert roles.authorize({**OWNER, "revoked": True}, method, path, POLICY) is (role == "public")


def test_paths_are_normalised_before_lookup():
    assert roles.required("HEAD", "/api/state") == ("viewer", None)
    assert roles.required("GET", "/api/state/") == ("viewer", None)
    assert roles.required("GET", "/api/keys?x=1") == ("owner", None)
    assert roles.required("GET", "/static") == ("viewer", None)
    assert roles.required("GET", "/staticx/app.js") == ("editor", None)  # not under /static/
    assert roles.required("post", "/api/chat") == ("editor", None)
