"""Teams over HTTP: the join and sign-in pages, cookies, tokens, revocation, the route table, change requests with
notifications and live events, releases, gated downloads, registration, and the `morph team` commands."""

import os
import re
from urllib.parse import quote

import pytest

from morph.studio import api_team
from morph.studio.teams import activity, audit, auth, collab
from morph.studio.teams import cli as team_cli
from test_team_util import build, call, make_team, members, request, serve


@pytest.fixture(autouse=True)
def _fresh():
    auth.THROTTLE.clear()
    activity.PRESENCE.seen.clear()
    yield
    api_team.configure(home=None, enabled=False)


@pytest.fixture
def site(tmp_path):
    home = str(tmp_path / "projects")
    root = os.path.join(home, "egg-picker")
    ids, cookies = make_team(home)
    build(root, "r001", by=ids["Jim"])
    httpd, base, room = serve(home, root)
    jar = {n: f"{auth.cookie_name(httpd.server_address[1])}={c}" for n, c in cookies.items()}
    yield {"home": home, "root": root, "ids": ids, "jar": jar, "base": base, "room": room, "port": httpd.server_address[1]}
    httpd.shutdown()
    httpd.server_close()


def test_an_invite_link_shows_a_join_page_and_the_form_signs_the_browser_in(site):
    base, home = site["base"], site["home"]
    s, inv, _ = call(base, "POST", "/api/team/invites", {"role": "editor", "disciplines": ["electronics", "sourcing"], "name_hint": "Kim"},
                     cookie=site["jar"]["Jim"])
    assert s == 200 and inv["path"] == "/?invite=" + inv["code"] and inv["invite"]["state"] == "pending" and "hash" not in inv["invite"]
    cli = team_cli.team_invite("viewer", [], "", home=home, base_url=base, out=None)
    assert cli["url"].startswith(base + "/?invite=mi_") and "You're invited as <b>viewer</b>" in call(base, "GET", cli["path"])[1]
    s, page, h = call(base, "GET", inv["path"])
    assert s == 200 and "Join R Cubed" in page and "Jim invited you" in page and 'value="Kim"' in page
    assert 'action="/api/team/join"' in page and "<script" not in page and "form-action 'self'" in h["Content-Security-Policy"]
    form = f"code={quote(inv['code'])}&name=Kim+Lee"
    s, _, h = call(base, "POST", "/api/team/join", raw=form.encode(), ctype="application/x-www-form-urlencoded")
    assert s == 303 and h["Location"] == "/"
    ck = h["Set-Cookie"]
    assert ck.startswith(f"morph_session_{site['port']}=") and "HttpOnly" in ck and "SameSite=Lax" in ck and "Max-Age=2592000" in ck
    s, me, _ = call(base, "GET", "/api/me", cookie=ck.split(";")[0])
    assert s == 200 and me["team"] and me["member"]["name"] == "Kim Lee" and me["member"]["role"] == "editor"
    assert me["member"]["disciplines"] == ["electronics", "sourcing"] and me["can"]["edit"] and not me["can"]["admin"]
    assert not re.search(r'"(hash|epoch|secret)"', str(me))
    s, page, _ = call(base, "POST", "/api/team/join", raw=form.encode(), ctype="application/x-www-form-urlencoded")
    assert s == 410 and "already used" in page
    s, page, _ = call(base, "GET", inv["path"])
    assert s == 410 and "already used" in page
    s, page, _ = call(base, "GET", "/")
    assert s == 401 and "invite link" in page


def test_device_links_and_json_join(site):
    base, jar = site["base"], site["jar"]
    s, out, _ = call(base, "POST", "/api/me/link", {}, cookie=jar["Ana"])
    assert s == 200 and out["path"].startswith("/?link=ml_") and "code" not in out
    s, page, _ = call(base, "GET", out["path"])
    assert s == 200 and "Sign this browser in as <b>Ana</b>" in page
    s, res, h = call(base, "POST", "/api/team/join", {"code": out["path"].split("=", 1)[1]})
    assert s == 200 and res["member"]["name"] == "Ana" and "Set-Cookie" in h
    s, res, _ = call(base, "POST", "/api/team/join", {"code": out["path"].split("=", 1)[1]})
    assert s == 410 and not res["ok"]
    s, res, _ = call(base, "POST", "/api/team/join", raw=b"[1,2]")
    assert s == 400


def test_script_tokens_and_revocation_end_access_at_once(site):
    base, jar, ids = site["base"], site["jar"], site["ids"]
    s, out, _ = call(base, "POST", "/api/me/tokens", {"label": "nightly"}, cookie=jar["Ana"])
    tok = out["token"]
    assert s == 200 and tok.startswith("mt_") and out["tokens"][0]["label"] == "nightly" and "hash" not in str(out["tokens"])
    assert call(base, "GET", "/api/me", token=tok)[1]["member"]["name"] == "Ana"
    s, out, _ = call(base, "POST", "/api/team/revoke", {"member": ids["Ana"]}, cookie=jar["Jim"])
    assert s == 200 and out["member"]["revoked"]
    assert call(base, "GET", "/api/me", cookie=jar["Ana"])[0] == 401
    assert call(base, "GET", "/api/me", token=tok)[0] == 401
    s, out, h = call(base, "POST", "/api/signout", {"everywhere": True}, cookie=jar["Raj"])
    assert s == 200 and "Max-Age=0" in h["Set-Cookie"]
    assert call(base, "GET", "/api/me", cookie=jar["Raj"])[0] == 401


def test_the_route_table_is_enforced_over_http(site):
    base, jar = site["base"], site["jar"]
    assert call(base, "POST", "/api/proposals/apply", {"id": "p1"}, cookie=jar["Vic"])[0] == 403
    assert call(base, "POST", "/api/team/invites", {"role": "owner"}, cookie=jar["Vic"])[0] == 403
    assert call(base, "POST", "/api/team/invites", {"role": "owner"}, cookie=jar["Ana"])[0] == 403
    assert call(base, "GET", "/api/audit", cookie=jar["Ana"])[0] == 403
    assert call(base, "POST", "/api/team/policy", {"values": {"viewers_can_comment": False}}, cookie=jar["Ana"])[0] == 403
    assert call(base, "POST", "/api/reviews", {"rev": "r001"}, cookie=jar["Vic"])[0] == 403
    assert call(base, "GET", "/api/comments", cookie=None)[0] == 401
    s, c, _ = call(base, "POST", "/api/comments", {"body": "Can it reach the top shelf?", "anchor": {"rev": "r001"}}, cookie=jar["Vic"])
    assert s == 200 and c["comment"]["author"]["name"] == "Vic"
    s, _, _ = call(base, "POST", "/api/team/policy", {"values": {"viewers_can_comment": False}}, cookie=jar["Jim"])
    assert s == 200
    assert call(base, "POST", "/api/comments", {"body": "again", "anchor": {"rev": "r001"}}, cookie=jar["Vic"])[0] == 403
    s, t, _ = call(base, "GET", "/api/team", cookie=jar["Ana"])
    assert s == 200 and "invites" not in t and {m["name"] for m in t["members"]} >= {"Jim", "Ana", "Vic"}
    s, t, _ = call(base, "GET", "/api/team", cookie=jar["Jim"])
    assert t["invites"] and not re.search(r"\bm[il]_[A-Za-z0-9_-]{20}|\"hash\"", str(t))
    s, a, _ = call(base, "GET", "/api/audit", cookie=jar["Jim"])
    assert s == 200 and a["verify"]["ok"] and a["entries"][0]["event"] == "policy_changed"


def test_change_requests_route_reviews_notify_and_queue_with_attribution(site):
    base, jar, ids, room, root = site["base"], site["jar"], site["ids"], site["room"], site["root"]
    s, pv, _ = call(base, "POST", "/api/proposals", {"preview": True, "changes": {"arm.actuator_family": "dynamixel"}}, cookie=jar["Vic"])
    assert s == 200 and pv["preview"]["changes"] == {"arm.actuator_family": ["smart_bus", "dynamixel"]} and collab.proposals(root) == []
    assert "Electronics: Ana" in pv["routing"]["text"]
    s, out, _ = call(base, "POST", "/api/proposals", {"title": "Swap to Dynamixel", "changes": {"arm.actuator_family": "dynamixel"},
                                                      "note": "Torque margin at full reach, @Raj?"}, cookie=jar["Vic"])
    p = out["proposal"]
    assert s == 200 and p["n"] == 1 and p["author"]["name"] == "Vic" and p["review"]["state"] == "open"
    chips = p["routing"]["text"]
    assert "Electronics: Ana" in chips and "Manufacturing: Raj" in chips and "Sourcing: Sue" in chips and "Mechanical: Mo" in chips
    box = call(base, "GET", "/api/inbox", cookie=jar["Ana"])[1]
    assert box["unread"] == 1 and box["items"][0]["kind"] == "review_request" and "#1" in box["items"][0]["text"]
    assert any(i["kind"] == "watch" for i in activity.inbox(site["home"], ids["Jim"])["items"]) is False  # Jim is a reviewer, not a watcher
    sent = room.hub.sent
    assert any(e["type"] == "proposal" and e["action"] == "open" and e["by"] == ids["Vic"] for e in sent)  # member ids
    assert {e["to"] for e in sent if e["type"] == "inbox"} >= {ids["Ana"], ids["Raj"], ids["Sue"], ids["Mo"]}
    s, dec, _ = call(base, "POST", "/api/reviews/decide", {"review": p["review"]["id"], "decision": "approve"}, cookie=jar["Ana"])
    assert s == 200 and dec["review"]["decisions"][-1]["disciplines"] == ["electronics"]
    assert any(i["kind"] == "decision" and "Ana approved" in i["text"] for i in activity.inbox(site["home"], ids["Vic"])["items"])
    assert call(base, "POST", "/api/reviews/decide", {"review": p["review"]["id"], "decision": "approve"}, cookie=jar["Vic"])[0] == 403
    s, ap, _ = call(base, "POST", "/api/proposals/apply", {"id": p["id"]}, cookie=jar["Ana"])
    assert s == 202 and ap["queued"] and ap["job"]["by"] == ids["Ana"] and ap["job"]["proposed_by"] == ids["Vic"]
    assert room.jobs[-1]["changes"] == {"arm.actuator_family": "dynamixel"}
    build(root, "r002", "r001", ap["job"]["changes"], by=ids["Ana"], proposed_by=ids["Vic"])
    api_team.job_finished(root, ap["job"], rev="r002", home=site["home"])
    assert collab.get_proposal(root, p["id"])["applied_rev"] == "r002"
    assert any(i["kind"] == "job" and "built r002" in i["text"] for i in activity.inbox(site["home"], ids["Vic"])["items"])
    s, feed, _ = call(base, "GET", "/api/activity?limit=10", cookie=jar["Vic"])
    assert s == 200 and any("applied change request #1" in i["text"] for i in feed["items"])
    s, lst, _ = call(base, "GET", "/api/proposals?status=applied", cookie=jar["Vic"])
    assert [x["n"] for x in lst["proposals"]] == [1]


def test_comment_mentions_reach_the_inbox_and_resolving_needs_the_author_or_an_editor(site):
    base, jar, ids = site["base"], site["jar"], site["ids"]
    key = collab.bom_key("actuator", "Feetech STS3215 serial bus servo 12 V")
    s, c, _ = call(base, "POST", "/api/comments", {"body": "@Ana is the 12 V one right?", "anchor": {"rev": "r001", "kind": "bom", "key": key}},
                   cookie=jar["Raj"])
    assert s == 200 and c["comment"]["mentions"] == [ids["Ana"]] and c["comment"]["anchor"]["label"].startswith("Feetech")
    item = call(base, "GET", "/api/inbox", cookie=jar["Ana"])[1]["items"][0]
    assert item["kind"] == "mention" and item["ref"]["anchor"]["kind"] == "bom"
    s, r, _ = call(base, "POST", "/api/comments", {"body": "Yes, 12 V", "parent": c["comment"]["id"]}, cookie=jar["Ana"])
    assert s == 200
    assert any(i["kind"] == "comment" for i in call(base, "GET", "/api/inbox", cookie=jar["Raj"])[1]["items"])
    assert call(base, "POST", "/api/comments/resolve", {"id": c["comment"]["id"]}, cookie=jar["Vic"])[0] == 403
    s, out, _ = call(base, "POST", "/api/comments/resolve", {"id": r["comment"]["id"]}, cookie=jar["Raj"])
    assert s == 200 and out["comment"]["resolved"] and out["comment"]["resolver"]["name"] == "Raj"
    s, lst, _ = call(base, "GET", "/api/comments?rev=r001&resolved=0", cookie=jar["Vic"])
    assert lst["comments"] == []
    assert call(base, "POST", "/api/inbox/read", {}, cookie=jar["Ana"])[1]["unread"] == 0


def test_releases_answer_with_the_gate_and_downloads_are_gated_and_logged(site):
    base, jar, ids, root, home = site["base"], site["jar"], site["ids"], site["root"], site["home"]
    s, out, _ = call(base, "POST", "/api/releases", {"tag": "Proto 1", "rev": "r001"}, cookie=jar["Ana"])
    assert s == 409 and out["gate"]["short"] == "needs 1 approval: prototype" and not out["ok"]
    s, _, _ = call(base, "POST", "/api/me", {"watch": True}, cookie=jar["Vic"])   # a viewer watching everything
    assert s == 200
    s, rv, _ = call(base, "POST", "/api/reviews", {"rev": "r001"}, cookie=jar["Ana"])
    assert s == 200 and rv["review"]["target"]["rev"] == "r001"
    assert any(i["kind"] == "watch" and "asked for review of r001" in i["text"] for i in activity.inbox(home, ids["Vic"])["items"])
    s, dec, _ = call(base, "POST", "/api/reviews/decide", {"review": rv["review"]["id"], "decision": "approve"}, cookie=jar["Raj"])
    assert s == 200 and dec["gate"]["ok"]
    s, out, _ = call(base, "POST", "/api/releases", {"tag": "Proto 1", "rev": "r001"}, cookie=jar["Ana"])
    assert s == 200 and out["release"]["tag"] == "Proto 1"
    s, out, _ = call(base, "GET", "/api/releases?rev=r001", cookie=jar["Vic"])
    assert out["pinned"] == ["r001"] and out["gate"]["released"] == ["Proto 1"]
    build(root, "r002", "r001", {"fab.process": "cnc_router"}, by=ids["Raj"])
    ms = members(home)
    res = api_team.check_download(ms[ids["Jim"]], root, "r002/mfg/mfg_prototype.zip", home=home)
    assert res["allowed"] and res["draft"]
    assert audit.entries(home)[0]["event"] == "download_gated"
    assert not api_team.check_download(ms[ids["Vic"]], root, "r002/mfg/mfg_prototype.zip", home=home)["allowed"]
    assert api_team.check_download(ms[ids["Vic"]], root, "r001/mfg/mfg_prototype.zip", home=home) == {"allowed": True, "draft": False, "reason": ""}


def test_presence_branches_and_errors(site):
    base, jar = site["base"], site["jar"]
    s, out, _ = call(base, "POST", "/api/presence", {"tab": "elec", "rev": "r001"}, cookie=jar["Ana"])
    assert s == 200 and out["members"][0]["name"] == "Ana" and out["members"][0]["tab"] == "elec"
    assert any(e["type"] == "presence" for e in site["room"].hub.sent)
    s, out, _ = call(base, "POST", "/api/branches", {"action": "create", "name": "pcb-try"}, cookie=jar["Ana"])
    assert s == 200 and [b["name"] for b in out["branches"]] == ["main", "pcb-try"]
    assert call(base, "POST", "/api/branches", {"action": "create", "name": "x"}, cookie=jar["Vic"])[0] == 403
    s, out, _ = call(base, "GET", "/api/branches?compare=main...pcb-try", cookie=jar["Vic"])
    assert s == 200 and out["compare"]["changes"] == {}
    s, out, _ = call(base, "POST", "/api/branches", {"action": "explode"}, cookie=jar["Ana"])
    assert s == 400 and out["error"].startswith("action is")
    s, out, _ = call(base, "POST", "/api/proposals", {"changes": {"arm.reach_mm": 9000}}, cookie=jar["Ana"])
    assert s == 400 and "between" in out["error"]
    s, out, _ = call(base, "GET", "/api/activity?before=yesterday", cookie=jar["Ana"])
    assert s == 400


def test_wrong_links_are_throttled_and_a_refused_build_reopens_the_change_request(site):
    base, root, ids = site["base"], site["root"], site["ids"]
    for _ in range(auth.THROTTLE.limit):
        assert call(base, "GET", "/?invite=mi_" + "x" * 22)[0] == 400
    s, page, _ = call(base, "GET", "/?link=ml_" + "y" * 22)
    assert s == 429 and "Too many wrong links" in page
    auth.THROTTLE.clear()
    p = collab.create_proposal(root, ids["Vic"], "CNC it", {"fab.process": "cnc_router"}, members=members(site["home"]))

    def full(job):
        raise RuntimeError("the room's queue is full (10 jobs)")

    site["room"].enqueue = full
    s, out, _ = call(base, "POST", "/api/proposals/apply", {"id": p["id"]}, cookie=site["jar"]["Ana"])
    assert s == 409 and "queue is full" in out["error"]
    got = collab.get_proposal(root, p["id"])
    assert got["status"] == "open" and "queue is full" in got["last_error"]


def test_handlers_never_leak_a_traceback(tmp_path):
    home = str(tmp_path / "projects")
    ids, _ = make_team(home)
    req = request(home, str(tmp_path / "nowhere"), member={"id": ids["Ana"], "name": "Ana", "role": "editor"}, method="POST",
                  path="/api/comments", body={"body": "hi", "anchor": {"rev": "r001"}})
    req.room.p = None
    req.studio.p = None
    s, out = api_team.comments_post(req)
    assert s == 500 and out["error"].startswith("The team service failed") and "Traceback" not in out["error"]


def test_me_without_team_mode_and_registration_follows_the_route_table(tmp_path):
    s, out = api_team.me_get(request(str(tmp_path), str(tmp_path)))
    assert s == 200 and out == {"ok": True, "team": False, "member": None}
    seen = []

    def route(method, path, role="editor", **opts):
        def deco(fn):
            seen.append((method, path, role, opts, fn))
            return fn
        return deco

    api_team.register(route)
    by = {(m, p): (r, o) for m, p, r, o, _ in seen}
    assert by[("POST", "/api/team/join")] == ("public", {"raw_body": True, "max_body": api_team.MAX_FORM})
    assert by[("GET", "/api/audit")][0] == "owner" and by[("POST", "/api/team/invites")][0] == "owner"
    assert by[("POST", "/api/proposals/apply")][0] == "editor" and by[("POST", "/api/comments")][0] == "viewer"
    assert len(seen) == len(api_team.ROUTES) and len({(m, p) for m, p, *_ in seen}) == len(seen)


def test_cli_commands(tmp_path, capsys):
    home = str(tmp_path / "projects")
    res = team_cli.team_init("Jim", "R Cubed", home=home, base_url="http://studio.local:8765")
    out = capsys.readouterr().out
    assert res["url"] == "http://studio.local:8765" + res["path"] and res["url"] in out and out.isascii()
    inv = team_cli.team_invite("viewer", ["documentation"], "Pat", home=home)
    assert inv["url"].startswith("http://127.0.0.1:8765/?invite=mi_")
    m, _ = auth.redeem_invite(inv["code"], "Pat", home=home)
    assert [x["name"] for x in team_cli.team_members(home=home)] == ["Jim", "Pat"]
    link = team_cli.team_link(None, home=home)
    assert link["member"]["name"] == "Jim" and link["path"].startswith("/?link=ml_")
    assert team_cli.team_link("pat", home=home)["member"]["id"] == m["id"]
    team_cli.team_revoke("Pat", home=home)
    with pytest.raises(SystemExit):
        team_cli.team_link("Pat", home=home)
    assert team_cli.team_audit_verify(home=home)["ok"]
    with pytest.raises(SystemExit):
        team_cli.team_link(None, home=str(tmp_path / "empty"))
    import argparse

    parser = argparse.ArgumentParser()
    team_cli.add_parser(parser.add_subparsers(dest="cmd"))
    a = parser.parse_args(["team", "audit", "--verify"])
    a.home = home
    assert a.func(a) == 0
    a = parser.parse_args(["team", "invite", "--role", "editor", "--disciplines", "electronics,sourcing", "--name", "Ana"])
    a.home = home
    assert a.func(a) == 0 and "Invite for Ana: editor, Electronics, Sourcing" in capsys.readouterr().out
