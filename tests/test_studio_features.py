"""The five feature modules live in the real Studio (contracts section 2): their routes load with the server and go
through its router, rooms and job queues. New robots from a brief and imported robots become projects through the
project service; video, import objects, worlds and Teams change requests are room jobs; Teams signs members in on a
--team Studio only. Builds are stubbed (every stage "pending") so each test takes seconds; nothing leaves the computer."""

import http.client
import json
import os
import queue
import re
import threading
import time

import pytest

from morph.robot.pipeline2 import RobotProject, dirty_nodes
from morph.studio import api_brief, api_team, projects, routes, server
from morph.studio import brief as B
from morph.studio.teams import activity, auth
from morph.studio.teams import audit as team_audit
from test_import_fixtures import arm_urdf
from test_studio_security import make_project
from test_team_util import make_team


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    """Every stage "pending" (instant), network lookups off (no Darwin, Claude or SubQ), Teams state per test."""
    monkeypatch.setattr(RobotProject, "_compute", lambda self, *a, **k: False)
    monkeypatch.setenv("MORPH_OFFLINE", "1")
    auth.THROTTLE.clear()
    activity.PRESENCE.seen.clear()
    yield
    api_team.configure(home=None, enabled=False)


def start(st, token=None, allowed=None):
    httpd = server._Server(("127.0.0.1", 0), server.make_handler(st, allowed, token))
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return httpd


def caller(httpd):
    port = httpd.server_address[1]

    def call(method, path, body=None, headers=None, raw=None, ctype="application/json", client=True):
        hdrs = {"X-Morph-Client": "1"} if method != "GET" and client else {}
        hdrs.update(headers or {})
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        if data is not None:
            hdrs.setdefault("Content-Type", ctype)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
        conn.request(method, path, body=data, headers=hdrs)
        r = conn.getresponse()
        text = r.read().decode("utf-8", "replace")
        conn.close()
        try:
            out = json.loads(text)
        except ValueError:
            out = text
        return r.status, out, dict(r.getheaders())
    return call


def until(cond, timeout=30.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = cond()
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError("timed out")


def drain(q) -> list:
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out


@pytest.fixture
def studio(tmp_path):
    home = tmp_path / "projects"
    st = server.Studio(make_project(home, "egg-picker"), str(home))
    httpd = start(st)
    yield st, caller(httpd)
    httpd.shutdown()


def test_every_feature_route_loads_with_the_server(studio):
    st, call = studio
    assert server.FEATURE_ERRORS == {}
    table = {(r.method, r.path) for r in routes.routes()}
    for key in (("POST", "/api/brief"), ("GET", "/api/brief"), ("POST", "/api/import"), ("PUT", "/api/import/*"),
                ("POST", "/api/video/analyze"), ("PUT", "/api/video/upload"), ("POST", "/api/video/apply"),
                ("GET", "/api/world"), ("POST", "/api/world"), ("PUT", "/api/world/upload"), ("GET", "/api/me"),
                ("POST", "/api/team/join"), ("POST", "/api/proposals/apply")):
        assert key in table, key
    assert call("GET", "/api/brief?id=nope")[0] == 400
    s, info, _ = call("GET", "/api/video/info")
    assert s == 200 and info["claude"] is False and info["limits"]["frames"] == 12
    s, world, _ = call("GET", "/api/world")
    assert s == 200 and world["world"] is None and {w["id"] for w in world["starters"]} >= {"desk", "counter", "workbench"}
    assert call("POST", "/api/import", {})[0] == 200
    # Teams is off: the page learns so from /api/me, and every other Teams route is not there
    assert call("GET", "/api/me")[:2] == (200, {"ok": True, "team": False, "member": None})
    assert call("GET", "/api/team")[0] == 404 and call("POST", "/api/team/join", {"code": "mi_x"})[0] == 404
    # the page's policy lets the video frame picker play the picked clip
    assert re.search(r"media-src 'self' blob:", server.app_csp())


def test_video_analyze_without_a_key_answers_the_manual_mode(studio):
    st, call = studio
    from test_video import b64frames
    s, out, _ = call("POST", "/api/video/analyze", {"frames": b64frames(2)})
    assert s == 200 and out["result"] is None and out["mode"] == "manual" and out["why"]


def test_a_brief_becomes_a_project_through_the_project_service(studio):
    st, call = studio
    q = st.room().hub.subscribe()
    s, out, _ = call("POST", "/api/brief", {"text": "help me sort socks"})
    assert s == 202
    bid = out["brief_id"]
    until(lambda: call("GET", f"/api/brief?id={bid}")[1]["busy"] is False)
    s, out, _ = call("POST", "/api/brief", {"brief_id": bid, "action": "defaults"})
    until(lambda: (lambda g: g["brief"]["status"] == "ready" and not g["busy"])(call("GET", f"/api/brief?id={bid}")[1]))
    s, out, _ = call("POST", "/api/brief", {"brief_id": bid, "action": "build"})
    assert s == 200 and out["project"] == "sock-sorter" and out["build"]["meta"]["origin"] == "brief"
    room = st.room("sock-sorter")
    meta = projects.read_meta(room.p.root)
    assert meta["origin"] == "brief" and meta["brief_id"] == bid and meta["demo"] is False and meta["need"]
    rev = until(lambda: room.latest())
    assert rev == "r001" and room.p.load_design(rev).name == "sock-sorter"
    until(lambda: os.path.isfile(os.path.join(room.p.root, "brief.json")))     # on_built: the final brief, no draft
    until(lambda: not os.path.exists(B.draft_path(st.home, bid)))
    with open(os.path.join(room.p.root, "brief.json"), encoding="utf-8") as fh:
        assert json.load(fh)["status"] == "built"
    created = [e for e in drain(q) if e["type"] == "project_created"]
    assert created and created[0]["project"] == "sock-sorter" and created[0]["origin"] == "brief"  # the originating tab


def test_the_brief_hook_clamps_and_refuses_what_no_robot_can_have(studio, monkeypatch):
    st, _ = studio
    req = type("R", (), {"studio": st, "member": None, "room": st.room()})()
    create = api_brief._studio_create(req)
    pay = {"name": "big-thing", "changes": {"arm.reach_mm": 9000.0, "arm.dof": 9, "world.id": "../../etc"},
           "meta": {"need": "x", "brief_id": "0123456789abcdef"}}
    out = create(pay)                                       # clamped to the envelope first: a buildable robot
    room = st.room(out["project"])
    design = room.p.load_design(until(lambda: room.latest()))
    assert design.arm.reach_mm == 500 and design.arm.dof == 6 and design.world.id == ""
    assert any("arm.reach_mm" in n for n in out["clamped"]) and any("world.id" in n for n in out["clamped"])


def test_an_imported_robot_builds_in_its_own_room_and_the_tab_follows(studio, tmp_path):
    st, call = studio
    q = st.room().hub.subscribe()
    sid = call("POST", "/api/import", {})[1]["id"]
    src = tmp_path / "arm"
    arm_urdf(str(src), motor_names=True)
    for dp, _, fns in os.walk(src):
        for f in fns:
            p = os.path.join(dp, f)
            rel = "arm/" + os.path.relpath(p, src).replace(os.sep, "/")
            with open(p, "rb") as fh:
                s, out, _ = call("PUT", f"/api/import/{sid}/file?path={rel}", raw=fh.read(), ctype="application/octet-stream")
            assert s == 200, out
    s, det, _ = call("POST", f"/api/import/{sid}/detect", {})
    assert s == 200 and det["candidates"][0]["kind"] == "robot"
    s, out, _ = call("POST", f"/api/import/{sid}/commit", {"candidate": 0, "name": "Box Arm"})
    assert s == 202 and out["project"] == "box-arm" and out["job"]["kind"] == "build"
    room = st.room("box-arm")
    meta = projects.read_meta(room.p.root)
    assert meta["origin"] == "import" and meta["source"] == "box-arm" and meta["demo"] is False
    until(lambda: room.latest())
    assert room.p.load_design("r001").arm.source == "box-arm"
    events = []
    until(lambda: events.extend(drain(q)) or any(e.get("stage") == "build" and e["status"] == "done" for e in events))
    mine = [e for e in events if e["type"] == "import" and e.get("id") == sid and e["stage"] == "build"]
    assert all(e["project"] == "egg-picker" for e in mine)              # in the importing tab's room: it shows them
    assert any(e.get("node") == "sizing" for e in mine) and mine[-1]["built_project"] == "box-arm"
    assert any(e["type"] == "project_created" and e["project"] == "box-arm" for e in events)
    until(lambda: not os.path.isdir(os.path.join(st.home, ".imports", sid)))  # the session goes when the build ends


def test_an_imported_object_is_a_room_job(studio, tmp_path):
    st, call = studio
    from test_import_fixtures import egg_stl
    sid = call("POST", "/api/import", {})[1]["id"]
    with open(egg_stl(str(tmp_path / "egg.stl")), "rb") as fh:
        call("PUT", f"/api/import/{sid}/file?path=egg.stl", raw=fh.read(), ctype="application/octet-stream")
    call("POST", f"/api/import/{sid}/detect", {})
    s, out, _ = call("POST", f"/api/import/{sid}/commit", {"candidate": 0})
    assert s == 202 and out["applied"] and out["job"]["kind"] == "change"
    room = st.room()
    until(lambda: room.latest() == "r002")
    assert room.p.load_design("r002").object.shape == "cylinder"


def test_a_world_save_is_a_room_job_that_reruns_only_the_simulation(studio):
    st, call = studio
    assert dirty_nodes(["world.id", "world.sha"]) == ["sim"]
    q = st.room().hub.subscribe()
    s, out, _ = call("POST", "/api/world", {"starter": "desk"})
    assert s == 202 and out["job"]["kind"] == "world" and out["world"]["id"] == "desk"
    room = st.room()
    until(lambda: room.latest() == "r002")
    d = room.p.load_design("r002")
    assert d.world.id == "desk" and d.world.sha == out["sha"]
    start_ev = next(e for e in drain(q) if e["type"] == "start")
    assert start_ev["dirty"] == ["sim"]


def test_folders_starting_with_a_dot_are_never_projects(studio):
    st, _ = studio
    for name in (".robots", ".imports", ".briefs", ".team", ".uploads", ".trash"):
        os.makedirs(os.path.join(st.home, name, "r001"), exist_ok=True)
        with open(os.path.join(st.home, name, "meta.json"), "w", encoding="utf-8") as fh:
            json.dump({"kind": "robot", "demo": True}, fh)
    assert [p["name"] for p in st.projects()] == ["egg-picker"]
    assert st.reset() is True                                # touches demo projects only, none of the dot folders


# ------------------------------------------------------------------ Teams on a --team Studio


@pytest.fixture
def team_studio(tmp_path):
    home = tmp_path / "projects"
    st = server.Studio(make_project(home, "egg-picker"), str(home), team=True)
    ids, cookies = make_team(str(home), (("Jim", "owner", ["generalist"]), ("Ana", "editor", ["electronics"]),
                                         ("Vic", "viewer", [])))
    httpd = start(st, token="team-code-ignored")
    port = httpd.server_address[1]
    jar = {n: f"{auth.cookie_name(port)}={c}" for n, c in cookies.items()}
    yield st, caller(httpd), ids, jar, port
    httpd.shutdown()


def test_team_studio_signs_members_in_and_checks_their_roles(team_studio):
    st, call, ids, jar, port = team_studio
    s, page, _ = call("GET", "/")
    assert s == 401 and "invite link" in page                       # signed out: the team's sign-in page
    assert call("GET", "/api/state")[0] == 401 and call("GET", "/static/app.js")[0] == 401
    assert call("GET", "/api/state", headers={"X-Morph-Token": "team-code-ignored"})[0] == 401  # MORPH_TOKEN plays no part
    s, me, _ = call("GET", "/api/me", headers={"Cookie": jar["Vic"]})
    assert s == 200 and me["team"] is True and me["member"]["name"] == "Vic"
    assert call("GET", "/", headers={"Cookie": jar["Vic"]})[0] == 200
    assert call("GET", "/api/state", headers={"Cookie": jar["Vic"]})[0] == 200
    s, out, _ = call("POST", "/api/chat", {"message": "make it 6-DOF"}, headers={"Cookie": jar["Vic"]})
    assert s == 403 and "viewer" in out["error"]                    # viewers comment and propose; editors edit
    assert call("POST", "/api/reset", {}, headers={"Cookie": jar["Ana"]})[0] == 403
    s, out, _ = call("POST", "/api/params", {"changes": {"arm.dof": 6}}, headers={"Cookie": jar["Ana"]})
    assert s == 202 and out["job"]["by"] == ids["Ana"]
    room = st.room()
    until(lambda: room.latest() == "r002")
    assert room.p.load_results("r002")["by"] == ids["Ana"]
    # an owner's invite, opened in a new browser: the join page, the form (no X-Morph-Client), then a session
    s, inv, _ = call("POST", "/api/team/invites", {"role": "editor", "disciplines": ["manufacturing"], "name_hint": "Raj"},
                     headers={"Cookie": jar["Jim"]})
    assert s == 200
    code = inv["code"]
    s, page, h = call("GET", f"/?invite={code}")
    assert s == 200 and 'action="/api/team/join"' in page and "form-action 'self'" in h["Content-Security-Policy"]
    assert 'content="strict-origin"' in page          # the form's POST keeps its Origin, which the Studio checks
    assert call("POST", "/api/team/join", raw=f"code={code}&name=Raj".encode(), ctype="application/x-www-form-urlencoded",
                headers={"Origin": "null"}, client=False)[0] == 403               # an opaque origin is refused
    s, _, h = call("POST", "/api/team/join", raw=f"code={code}&name=Raj".encode(),
                   ctype="application/x-www-form-urlencoded", client=False)
    assert s == 303 and h["Location"] == "/" and h["Set-Cookie"].startswith(auth.cookie_name(port))
    raj = h["Set-Cookie"].split(";")[0]
    s, me, _ = call("GET", "/api/me", headers={"Cookie": raj})
    assert s == 200 and me["member"]["name"] == "Raj" and me["member"]["role"] == "editor"
    assert call("POST", "/api/team/join", raw=f"code={code}&name=Again".encode(),
                ctype="application/x-www-form-urlencoded", client=False)[0] in (400, 410)   # one use only
    # the owner changed a setting: the audit log has it (names, never values of keys)
    assert call("POST", "/api/settings", {"values": {"calibration_enabled": False}}, headers={"Cookie": jar["Jim"]})[0] == 200
    assert any(e["event"] == "settings_changed" and e["by"] == ids["Jim"] for e in team_audit.entries(st.home))


def test_a_change_request_is_built_by_the_room_queue_for_its_author(team_studio):
    st, call, ids, jar, _ = team_studio
    s, out, _ = call("POST", "/api/proposals", {"title": "Dynamixel", "changes": {"arm.actuator_family": "dynamixel"}},
                     headers={"Cookie": jar["Vic"]})
    assert s == 200
    pid = out["proposal"]["id"]
    s, out, _ = call("POST", "/api/proposals/apply", {"id": pid}, headers={"Cookie": jar["Ana"]})
    assert s == 202 and out["job"]["by"] == ids["Ana"] and out["job"]["proposed_by"] == ids["Vic"] and out["job"]["job"]
    room = st.room()
    until(lambda: room.latest() == "r002")
    res = room.p.load_results("r002")
    assert res["by"] == ids["Ana"] and res["proposed_by"] == ids["Vic"]
    def revision_entry():  # the ledger line follows results.json by a moment
        try:
            with open(os.path.join(room.p.root, "ledger.jsonl"), encoding="utf-8") as fh:
                return [e for e in map(json.loads, fh) if e.get("type") == "revision" and e.get("rev") == "r002"]
        except FileNotFoundError:
            return []
    entry = until(revision_entry)[-1]
    assert entry["by"] == ids["Ana"] and entry["proposed_by"] == ids["Vic"]
    p = until(lambda: next((x for x in call("GET", "/api/proposals", headers={"Cookie": jar["Vic"]})[1]["proposals"]
                            if x["id"] == pid and x.get("applied_rev")), None))
    assert p["applied_rev"] == "r002"


def test_team_downloads_and_presence_follow_the_hooks(team_studio):
    st, call, ids, jar, port = team_studio
    assert call("GET", "/files/egg-picker/r001/design.json", headers={"Cookie": jar["Vic"]})[0] == 200
    assert call("GET", "/files/egg-picker/r001/design.json")[0] == 401
    # an open event stream counts as presence for its member
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", "/api/events", headers={"Cookie": jar["Ana"]})
    r = conn.getresponse()
    assert r.status == 200
    until(lambda: any(m["id"] == ids["Ana"] for m in activity.PRESENCE.snapshot("egg-picker", auth_members(st))))
    conn.close()


def auth_members(st):
    from morph.studio.teams.store import TeamStore
    return TeamStore(st.home).read().get("members") or {}


def test_morph_studio_team_needs_a_team_and_the_team_commands_work(tmp_path, monkeypatch, capsys):
    import argparse

    from morph import cli, keys
    from morph.studio.teams import cli as team_cli
    from test_keys_and_launch import _robots
    home = tmp_path / "projects"
    _robots(home)
    monkeypatch.setattr(cli, "HOME", str(home))
    monkeypatch.setenv("MORPH_HOME", str(home))
    monkeypatch.setattr(keys, "ENV_PATH", str(tmp_path / ".env"))
    monkeypatch.setenv("MORPH_TOKEN", "a-fixed-access-code")
    got = {}
    monkeypatch.setattr(server, "serve", lambda p, host, port, home=None, token=None, **kw: got.update(token=token, **kw))
    a = lambda **k: argparse.Namespace(**{"name": "egg-picker", "host": "127.0.0.1", "port": 1, "open": False,  # noqa: E731
                                          "remote": False, "team": False, "allow_url_import": False, **k})
    with pytest.raises(SystemExit, match="morph team init"):
        cli.cmd_studio(a(team=True))
    assert cli.main(["team", "init", "--owner", "Jim", "--name", "R Cubed"]) == 0
    assert "?link=ml_" in capsys.readouterr().out
    assert cli.main(["team", "invite", "--role", "viewer", "--name", "Pat"]) == 0
    assert "?invite=mi_" in capsys.readouterr().out
    assert team_cli.team_audit_verify(home=str(home), out=None)["ok"]
    cli.cmd_studio(a(team=True, remote=True))
    assert got["team"] is True and got["token"] is None and "?link=ml_" in got["link"]   # MORPH_TOKEN plays no part
    got.clear()
    cli.cmd_studio(a(remote=True, allow_url_import=True))
    assert got == {"token": "a-fixed-access-code", "allow_url_import": True}


# ------------------------------------------------------------------ the page: every feature module mounts in app.js


def test_the_page_mounts_every_feature_and_a_new_robot_builds_from_it(tmp_path, monkeypatch):
    pw = pytest.importorskip("playwright.sync_api")
    chrome = os.environ.get("PLAYWRIGHT_CHROMIUM") or "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
    from morph import keys
    monkeypatch.setattr(keys, "ENV_PATH", str(tmp_path / ".env"))
    home = tmp_path / "projects"
    st = server.Studio(make_project(home, "egg-picker"), str(home))
    httpd = start(st)
    port = httpd.server_address[1]
    httpd.RequestHandlerClass = server.make_handler(st, {f"127.0.0.1:{port}", f"localhost:{port}"})
    with pw.sync_playwright() as p:
        try:
            browser = p.chromium.launch(executable_path=chrome if os.path.exists(chrome) else None, args=["--no-sandbox"])
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"no Chromium for Playwright ({exc.__class__.__name__})")
        try:
            pg = browser.new_page(viewport={"width": 1400, "height": 900})
            errors = []
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            pg.goto(f"http://127.0.0.1:{port}/?tour=0", wait_until="domcontentloaded")
            pg.wait_for_selector("#featHeader .nr-headbtn", timeout=60000)
            pg.wait_for_selector("#importBtn", timeout=120000)
            assert pg.evaluate("[...document.querySelectorAll('#featHeader button')].map(b => b.textContent.trim())") == \
                ["New robot", "Import"]
            assert pg.inner_text("#chatTools").strip().startswith("Show Morph the job")
            assert pg.evaluate("document.querySelectorAll('[class*=tm-]').length") == 0     # Teams is off: nothing added
            pg.click("#importBtn")
            pg.wait_for_selector("#featModal.on .imp-drop")
            assert pg.evaluate("document.querySelectorAll('#featModal .dlg').length") == 1   # its own dialog, not nested
            pg.keyboard.press("Escape")
            pg.click(".vj-btn")
            pg.wait_for_selector("#featModal.on .vjob")
            until(lambda: "no frame leaves this browser" in pg.inner_text("#featModal"))   # after it asked /api/video/info
            assert pg.evaluate("document.querySelectorAll('#featModal .dlg').length") == 1
            pg.keyboard.press("Escape")
            # (keep the unsubscribe function: Playwright calls a function an expression evaluates to)
            pg.evaluate("window.__switched = []; window.__off = window.morph.studio.on('project', (e) => window.__switched.push(e)); 0")
            pg.click(".nr-headbtn")
            pg.fill("#nr-text", "help me sort socks")
            pg.click("[data-a=start]")
            pg.wait_for_selector("[data-a=defaults]", timeout=30000)
            pg.click("[data-a=defaults]")
            pg.wait_for_selector("[data-a=build]", timeout=30000)
            pg.click("[data-a=build]")
            until(lambda: pg.evaluate("window.morph.state.project") == "sock-sorter")   # the tab follows the build
            # feature modules hear that this tab now shows another project (world mode clears the old room, Teams reloads)
            until(lambda: pg.evaluate("window.__switched") == [{"type": "project", "project": "sock-sorter", "switched": True}])
            pg.wait_for_selector("[data-a=openproj]", timeout=60000)
            assert st.room("sock-sorter").latest() == "r001" and "project=sock-sorter" in pg.url
            assert errors == []
        finally:
            browser.close()
            httpd.shutdown()


def test_a_browser_signs_in_from_the_teams_link_page(tmp_path, monkeypatch):
    """The sign-in and join forms post from a page whose Origin the Studio checks: the page keeps its Origin on the POST
    (the Studio's no-referrer policy alone makes a browser send "null", which is refused)."""
    pw = pytest.importorskip("playwright.sync_api")
    chrome = os.environ.get("PLAYWRIGHT_CHROMIUM") or "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
    from morph import keys
    monkeypatch.setattr(keys, "ENV_PATH", str(tmp_path / ".env"))
    home = tmp_path / "projects"
    st = server.Studio(make_project(home, "egg-picker"), str(home), team=True)
    link = auth.init_team("Jim", "R Cubed", home=str(home))["path"]
    httpd = start(st)
    port = httpd.server_address[1]
    with pw.sync_playwright() as p:
        try:
            browser = p.chromium.launch(executable_path=chrome if os.path.exists(chrome) else None, args=["--no-sandbox"])
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"no Chromium for Playwright ({exc.__class__.__name__})")
        try:
            pg = browser.new_page(viewport={"width": 1400, "height": 900})
            pg.goto(f"http://127.0.0.1:{port}{link}", wait_until="domcontentloaded")
            assert 'content="strict-origin"' in pg.content()
            with pg.expect_navigation():
                pg.click("button[type=submit]")
            pg.wait_for_selector("#featHeader .nr-headbtn", timeout=60000)
            assert pg.url.startswith(f"http://127.0.0.1:{port}/") and "/api/team/join" not in pg.url
            until(lambda: pg.evaluate("document.querySelectorAll('[class*=tm-]').length") > 0)   # Teams UI on the page
            assert pg.evaluate("fetch('/api/me').then(r => r.json())")["member"]["name"] == "Jim"
        finally:
            browser.close()
            httpd.shutdown()


def test_the_page_offers_a_new_robot_for_a_new_job_and_fits_a_laptop_window(tmp_path, monkeypatch):
    """At 1280 x 720: the header fits, the chat tools sit under the message, the right panel opens from the header, a
    project without a conversation names the feature entry points, and a chat line that asks for another robot is offered
    the New robot dialog (or, when asked, edits the open robot after all). A viewer on a team Studio gets no editing tools."""
    pw = pytest.importorskip("playwright.sync_api")
    chrome = os.environ.get("PLAYWRIGHT_CHROMIUM") or "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
    from morph import keys
    monkeypatch.setattr(keys, "ENV_PATH", str(tmp_path / ".env"))
    home = tmp_path / "projects"
    st = server.Studio(make_project(home, "egg-picker"), str(home))
    httpd = start(st)
    port = httpd.server_address[1]
    with pw.sync_playwright() as p:
        try:
            browser = p.chromium.launch(executable_path=chrome if os.path.exists(chrome) else None, args=["--no-sandbox"])
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"no Chromium for Playwright ({exc.__class__.__name__})")
        try:
            pg = browser.new_page(viewport={"width": 1280, "height": 720})
            errors = []
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            pg.goto(f"http://127.0.0.1:{port}/?tour=0", wait_until="domcontentloaded")
            pg.wait_for_selector("#chatTools .vj-btn", timeout=60000)
            until(lambda: "Also here:" in pg.inner_text("#messages"))
            tips = pg.inner_text("#messages")
            assert "New robot (top right)" in tips and "Import (top right)" in tips and "Show Morph the job (below)" in tips
            box = pg.evaluate("""() => { const r = (s) => document.querySelector(s).getBoundingClientRect(), top = document.querySelector('#top');
                return {input: r('#input').bottom, tools: r('#chatTools').top, send: r('#send').left, toolsLeft: r('#chatTools').left,
                        overflow: top.scrollWidth - top.clientWidth, page: document.documentElement.scrollWidth - innerWidth}; }""")
            assert box["tools"] >= box["input"] - 1 and box["send"] > box["toolsLeft"]   # tools and Send under the message
            assert box["overflow"] <= 0 and box["page"] <= 0                              # nothing pushed off the header
            assert pg.is_hidden("#inspector")
            pg.click("#inspBtn")
            assert pg.is_visible("#inspector") and pg.get_attribute("#inspBtn", "aria-pressed") == "true"
            pg.keyboard.press("Escape")
            assert pg.is_hidden("#inspector")
            pg.fill("#input", "help me sort socks")
            pg.keyboard.press("Enter")
            pg.wait_for_selector(".msg.assistant .qacts [data-o=go]", timeout=30000)
            assert "egg-picker stays as it is" in pg.inner_text("#messages")
            pg.click(".msg.assistant .qacts [data-o=go]")
            pg.wait_for_selector("#featModal.on .nr", timeout=10000)
            until(lambda: "help me sort socks" in pg.inner_text("#featModal"))     # the dialog started from the line
            pg.keyboard.press("Escape")
            pg.click(".msg.assistant .qacts [data-o=edit]")
            until(lambda: pg.evaluate("document.querySelectorAll('.msg.user').length") == 2)
            until(lambda: "couldn't map that" in pg.inner_text("#messages"))     # edited after all: nothing to change
            assert pg.evaluate("[...document.querySelectorAll('.qacts [data-o]')].every((b) => b.disabled)")
            assert st.room().p.revs() == ["r001"] and errors == []
        finally:
            browser.close()
            httpd.shutdown()


def test_a_viewer_gets_no_editing_tools(team_studio, monkeypatch):
    pw = pytest.importorskip("playwright.sync_api")
    chrome = os.environ.get("PLAYWRIGHT_CHROMIUM") or "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
    st, call, ids, jar, port = team_studio
    with pw.sync_playwright() as p:
        try:
            browser = p.chromium.launch(executable_path=chrome if os.path.exists(chrome) else None, args=["--no-sandbox"])
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"no Chromium for Playwright ({exc.__class__.__name__})")
        try:
            ctx = browser.new_context(viewport={"width": 1400, "height": 900})
            name, value = jar["Vic"].split("=", 1)
            ctx.add_cookies([{"name": name, "value": value, "domain": "127.0.0.1", "path": "/"}])
            pg = ctx.new_page()
            pg.goto(f"http://127.0.0.1:{port}/?tour=0", wait_until="domcontentloaded")
            pg.wait_for_selector(".tm-tabs", timeout=60000)
            until(lambda: pg.evaluate("document.body.classList.contains('tm-readonly')"))
            assert pg.is_disabled("#input") and pg.is_disabled("#send") and pg.is_disabled("#paramsApply")
            assert "Changes" in pg.get_attribute("#input", "placeholder")
            for sel in (".nr-headbtn", "#importBtn", ".vj-btn", "#suggest"):
                assert pg.is_hidden(sel), sel
        finally:
            browser.close()


# ------------------------------------------------------------------ I2b: what the wiring guarantees beyond each route


def test_every_route_has_its_role_in_the_teams_table(studio):
    """A route a feature registers gets the same role from the Teams table (teams/roles.py), so a team Studio never falls
    back to the table's catch-all for it (an editor to read, an owner to change)."""
    from morph.studio.teams import roles
    seen = 0
    for r in routes.routes():
        if r.path.startswith("/api/test-"):                           # registered by test_studio_routes.py
            continue
        path = r.path[:-1] + "x" if r.path.endswith("/*") else r.path
        listed = (r.method, r.path) in roles.ROUTES or any(
            m == r.method and p.endswith("/*") and path.startswith(p[:-1]) for m, p in roles.ROUTES)
        assert listed and roles.required(r.method, path)[0] == r.role, (r.method, r.path, r.role)
        seen += 1
    assert seen >= 60


def _brief_ready(call, text):
    s, out, _ = call("POST", "/api/brief", {"text": text})
    bid = out["brief_id"]
    until(lambda: call("GET", f"/api/brief?id={bid}")[1]["busy"] is False)
    call("POST", "/api/brief", {"brief_id": bid, "action": "defaults"})
    until(lambda: (lambda g: g["brief"]["status"] == "ready" and not g["busy"])(call("GET", f"/api/brief?id={bid}")[1]))
    return bid


def test_building_a_brief_twice_makes_one_project(studio):
    st, call = studio
    bid = _brief_ready(call, "help me sort socks")
    st.builds.acquire()                                               # the first build waits for the build slot
    try:
        s1, o1, _ = call("POST", "/api/brief", {"brief_id": bid, "action": "build"})
        s2, o2, _ = call("POST", "/api/brief", {"brief_id": bid, "action": "build"})   # a second click while it builds
    finally:
        st.builds.release()
    assert s1 == 200 and o1["project"] == "sock-sorter" and not o1.get("already")
    assert s2 == 200 and o2["project"] == "sock-sorter" and o2["already"] is True
    room = st.room("sock-sorter")
    until(lambda: room.latest())
    assert [p["name"] for p in st.projects()] == ["egg-picker", "sock-sorter"]   # no sock-sorter-2
    until(lambda: not os.path.exists(B.draft_path(st.home, bid)))
    assert call("POST", "/api/brief", {"brief_id": bid, "action": "build"})[0] == 404   # built: the brief is done


def test_a_brief_whose_first_build_failed_can_be_built_again(studio, monkeypatch):
    st, call = studio
    bid = _brief_ready(call, "help me sort socks")
    real = RobotProject.run
    monkeypatch.setattr(RobotProject, "run", lambda self, *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    s, out, _ = call("POST", "/api/brief", {"brief_id": bid, "action": "build"})
    assert s == 200 and out["project"] == "sock-sorter"
    until(lambda: (lambda g: g[0] == 200 and g[1]["brief"]["status"] == "ready" and not g[1]["busy"])(
        call("GET", f"/api/brief?id={bid}")))
    monkeypatch.setattr(RobotProject, "run", real)
    s, out, _ = call("POST", "/api/brief", {"brief_id": bid, "action": "build"})
    assert s == 200 and out["project"] == "sock-sorter-2" and not out.get("already")   # a new project, built this time
    room = st.room("sock-sorter-2")
    assert until(lambda: room.latest()) == "r001"
    until(lambda: os.path.isfile(os.path.join(room.p.root, "brief.json")))


def test_imports_go_into_the_team_audit_log(team_studio, tmp_path):
    st, call, ids, jar, _ = team_studio
    from test_import_fixtures import egg_stl
    ana = {"Cookie": jar["Ana"]}
    sid = call("POST", "/api/import", {}, headers=ana)[1]["id"]
    with open(egg_stl(str(tmp_path / "egg.stl")), "rb") as fh:
        call("PUT", f"/api/import/{sid}/file?path=egg.stl", raw=fh.read(), ctype="application/octet-stream", headers=ana)
    call("POST", f"/api/import/{sid}/detect", {}, headers=ana)
    s, out, _ = call("POST", f"/api/import/{sid}/commit", {"candidate": 0}, headers=ana)
    assert s == 202 and out["applied"]
    sid = call("POST", "/api/import", {}, headers=ana)[1]["id"]
    src = tmp_path / "arm"
    arm_urdf(str(src), motor_names=True)
    for dp, _, fns in os.walk(src):
        for f in fns:
            p = os.path.join(dp, f)
            with open(p, "rb") as fh:
                call("PUT", f"/api/import/{sid}/file?path=arm/{os.path.relpath(p, src).replace(os.sep, '/')}", raw=fh.read(),
                     ctype="application/octet-stream", headers=ana)
    call("POST", f"/api/import/{sid}/detect", {}, headers=ana)
    s, out, _ = call("POST", f"/api/import/{sid}/commit", {"candidate": 0, "name": "Box Arm"}, headers=ana)
    assert s == 202 and out["project"] == "box-arm"
    rows = team_audit.entries(st.home, event="import")[::-1]          # entries() is newest first
    assert [(e["by"], e["kind"], e["project"]) for e in rows] == [(ids["Ana"], "object", "egg-picker"),
                                                                  (ids["Ana"], "robot", "box-arm")]
    assert rows[0]["file"] == "egg.stl" and rows[1]["source"] == "box-arm" and team_audit.verify(st.home)["ok"]
    from morph.studio import api_import                              # a link import is logged without its query
    req = type("R", (), {"studio": st, "member": {"id": ids["Ana"]}})()
    s_url = type("S", (), {"state": lambda self: {"source_url": "https://github.com/acme/arm/tree/main?token=x#readme"}})()
    api_import._audit(req, s_url, kind="robot", project="arm")
    assert team_audit.entries(st.home, event="import")[0]["url"] == "https://github.com/acme/arm/tree/main"


def _zip(port, path, cookie=None):
    import io
    import zipfile
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    conn.request("GET", path, headers={"Cookie": cookie} if cookie else {})
    r = conn.getresponse()
    data = r.read()
    conn.close()
    assert r.status == 200, (r.status, data[:200])
    z = zipfile.ZipFile(io.BytesIO(data))
    return z, json.loads(z.read("export-manifest.json"))


ORDER_FILES = ("mfg/mfg_prototype.zip", "mfg/controller_pcba/gerbers.zip")


def _order_files(root):
    for rel in ORDER_FILES + ("mfg/plan.json",):
        os.makedirs(os.path.dirname(os.path.join(root, "r001", *rel.split("/"))), exist_ok=True)
        with open(os.path.join(root, "r001", *rel.split("/")), "wb") as fh:
            fh.write(b"{}" if rel.endswith(".json") else b"PK\x05\x06" + bytes(18))


def test_a_team_export_follows_the_download_policy_and_carries_the_teams_record(team_studio):
    from morph.studio.teams import collab
    from morph.studio.teams.store import TeamStore
    st, call, ids, jar, port = team_studio
    root = st.room().p.root
    _order_files(root)
    # a viewer: the order files of a revision nobody released stay out (as on /files), and the manifest says so
    z, man = _zip(port, "/api/export?rev=r001", jar["Vic"])
    names = set(z.namelist())
    assert "mfg/plan.json" in names and not names & set(ORDER_FILES) and sorted(man["left_out"]) == sorted(ORDER_FILES)
    assert "release" in man["left_out_note"]
    rec = json.loads(z.read("team.json"))
    assert rec["format"] == "morph-team-record/1" and rec["rev"] == "r001" and rec["level"] == "prototype"
    assert rec["release_gate"]["ok"] is False and rec["releases"] == []
    assert any(f["path"] == "team.json" for f in man["files"])
    # the owner may take a draft order package: it goes out, and the audit log says so
    z, man = _zip(port, "/api/export?rev=r001", jar["Jim"])
    assert set(ORDER_FILES) <= set(z.namelist()) and "left_out" not in man
    row = team_audit.entries(st.home, event="export")[0]
    assert row["by"] == ids["Jim"] and sorted(row["draft_order_files"]) == sorted(ORDER_FILES)
    # reviewed and released: everyone's export has the order files, and the record names who approved it
    ms = TeamStore(st.home).read()["members"]
    rv = collab.request_review(root, ids["Jim"], rev="r001", members=ms)
    collab.decide(root, rv["id"], ms[ids["Ana"]], "approve", members=ms)
    collab.create_release(root, "Proto 1", "r001", ids["Jim"], ms, home=st.home)
    z, man = _zip(port, "/api/export?rev=r001", jar["Vic"])
    rec = json.loads(z.read("team.json"))
    assert set(ORDER_FILES) <= set(z.namelist()) and rec["release_gate"]["ok"] is True
    assert [(r["tag"], r["by"]) for r in rec["releases"]] == [("Proto 1", "Jim")]
    assert {a["name"] for d in rec["disciplines"] for a in d["approved_by"]} == {"Ana"}
    blob = z.read("team.json").decode()
    assert not any(mid in blob for mid in ids.values())               # names, never member ids
    # the project's history: the whole team record
    z, man = _zip(port, "/api/export?full=1", jar["Vic"])
    hist = json.loads(z.read("team.json"))
    assert hist["format"] == "morph-team-history/1" and [r["tag"] for r in hist["releases"]] == ["Proto 1"]
    assert call("GET", "/api/export?rev=r009", headers={"Cookie": jar["Vic"]})[0] == 404


def test_a_team_export_the_team_cant_vouch_for_is_refused_with_its_reason(team_studio):
    """A finished revision whose design.json can't be read has no team record (team.json): its export is refused with the
    team's reason, as the button's check and as the download, never a server error, and nothing goes out. The project's
    history still exports: its record is the history."""
    st, call, ids, jar, port = team_studio
    with open(os.path.join(st.room().p.root, "r001", "design.json"), "w", encoding="utf-8") as fh:
        fh.write("{not json")
    for q in ("rev=r001&check=1", "rev=r001"):
        s, out, _ = call("GET", f"/api/export?{q}", headers={"Cookie": jar["Jim"]})
        assert s == 404 and "no completed revision r001" in out["error"], (q, s, out)
    assert team_audit.entries(st.home, event="export") == []
    z, man = _zip(port, "/api/export?full=1&rev=r001", jar["Jim"])
    assert json.loads(z.read("team.json"))["format"] == "morph-team-history/1"


def test_an_export_without_teams_has_no_team_record_and_keeps_order_files(tmp_path):
    home = tmp_path / "projects"
    st = server.Studio(make_project(home, "egg-picker"), str(home))
    httpd = start(st)
    try:
        _order_files(st.room().p.root)
        z, man = _zip(httpd.server_address[1], "/api/export?rev=r001")
        assert set(ORDER_FILES) <= set(z.namelist()) and "team.json" not in z.namelist() and "left_out" not in man
    finally:
        httpd.shutdown()


def test_a_build_that_ends_while_the_tab_switches_to_it_is_not_missed(tmp_path, monkeypatch):
    """The New robot dialog switches its tab to the new project while that project's first build runs. The tab listens
    to the new project before it loads its state (and the server subscribes a stream before answering it), so a build
    that ends in between is in the state or in the events: here the new room's stream is 2 s slow to subscribe and the
    build ends as soon as the tab shows it waiting."""
    pw = pytest.importorskip("playwright.sync_api")
    chrome = os.environ.get("PLAYWRIGHT_CHROMIUM") or "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
    from morph import keys
    from morph.studio import rooms
    monkeypatch.setattr(keys, "ENV_PATH", str(tmp_path / ".env"))
    gate, real_run, real_sub = threading.Event(), RobotProject.run, rooms.Hub.subscribe

    def gated(self, *a, **k):
        if os.path.basename(self.root) == "sock-sorter":
            assert gate.wait(60)
        return real_run(self, *a, **k)

    def slow(self, member=None):
        if self.project == "sock-sorter":
            time.sleep(2.0)
        return real_sub(self, member)
    monkeypatch.setattr(RobotProject, "run", gated)
    monkeypatch.setattr(rooms.Hub, "subscribe", slow)
    home = tmp_path / "projects"
    st = server.Studio(make_project(home, "egg-picker"), str(home))
    httpd = start(st)
    port = httpd.server_address[1]
    httpd.RequestHandlerClass = server.make_handler(st, {f"127.0.0.1:{port}", f"localhost:{port}"})
    with pw.sync_playwright() as p:
        try:
            browser = p.chromium.launch(executable_path=chrome if os.path.exists(chrome) else None, args=["--no-sandbox"])
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"no Chromium for Playwright ({exc.__class__.__name__})")
        try:
            pg = browser.new_page(viewport={"width": 1400, "height": 900})
            errors = []
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.goto(f"http://127.0.0.1:{port}/?tour=0", wait_until="domcontentloaded")
            pg.wait_for_selector("#featHeader .nr-headbtn", timeout=120000)
            pg.click(".nr-headbtn")
            pg.fill("#nr-text", "help me sort socks")
            pg.click("[data-a=start]")
            pg.wait_for_selector("[data-a=defaults]", timeout=60000)
            pg.click("[data-a=defaults]")
            pg.wait_for_selector("[data-a=build]", timeout=60000)
            pg.click("[data-a=build]")
            until(lambda: pg.evaluate("window.morph.state.project === 'sock-sorter' && window.morph.state.queue.length > 0"))
            time.sleep(1.0)
            assert pg.query_selector("[data-a=openproj]") is None     # still building: not done with the old project's revision
            assert pg.evaluate("window.morph.state.current") is None  # the tab shows no revision of egg-picker as sock-sorter's
            # nor anything of it: the views wait (no egg-picker robot), no Export of its revision, none of its parameters
            assert pg.evaluate("document.querySelector('#views').classList.contains('norev')")
            assert pg.is_visible("#viewsEmpty") and not pg.is_visible("#views .view.on")
            assert "sock-sorter" in pg.inner_text("#viewsEmpty")
            assert pg.inner_text("#tabsInfo").strip() == "" and pg.query_selector("#paramsBody .prow") is None
            gate.set()                                                # the build ends now
            pg.wait_for_selector("[data-a=openproj]", timeout=60000)  # the dialog saw it end
            assert st.room("sock-sorter").latest() == "r001"
            assert pg.evaluate("[window.morph.state.current?.rev, window.morph.state.current?.design?.name]") == ["r001", "sock-sorter"]
            assert not pg.is_visible("#viewsEmpty") and pg.is_visible("#views .view.on")   # its own revision shows
            assert "r001" in pg.inner_text("#tabsInfo") and pg.query_selector("#paramsBody .prow") is not None
            assert errors == []
        finally:
            gate.set()
            browser.close()
            httpd.shutdown()


def test_the_new_robot_dialog_shows_stages_that_ran_before_the_tab_joined(tmp_path, monkeypatch):
    """Sizing takes a tenth of a second, so it is done before the dialog's tab joins the new project's room: the dialog
    reads the room's state for what it missed instead of showing it still queued."""
    pw = pytest.importorskip("playwright.sync_api")
    chrome = os.environ.get("PLAYWRIGHT_CHROMIUM") or "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
    from morph import keys
    from morph.robot.pipeline2 import ORDER
    monkeypatch.setattr(keys, "ENV_PATH", str(tmp_path / ".env"))
    gate, real_run = threading.Event(), RobotProject.run

    def run(self, *a, on_event=None, **k):
        if os.path.basename(self.root) == "sock-sorter":
            emit = on_event or (lambda e: None)
            emit({"type": "start", "rev": "r001", "parent": None, "changes": {}, "dirty": list(ORDER)})
            emit({"type": "node", "node": "sizing", "status": "computing"})
            emit({"type": "node", "node": "sizing", "status": "computed", "seconds": 0.1})
            assert gate.wait(60)
        return real_run(self, *a, on_event=on_event, **k)
    monkeypatch.setattr(RobotProject, "run", run)
    home = tmp_path / "projects"
    st = server.Studio(make_project(home, "egg-picker"), str(home))
    httpd = start(st)
    port = httpd.server_address[1]
    httpd.RequestHandlerClass = server.make_handler(st, {f"127.0.0.1:{port}", f"localhost:{port}"})
    with pw.sync_playwright() as p:
        try:
            browser = p.chromium.launch(executable_path=chrome if os.path.exists(chrome) else None, args=["--no-sandbox"])
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"no Chromium for Playwright ({exc.__class__.__name__})")
        try:
            pg = browser.new_page(viewport={"width": 1400, "height": 900})
            pg.goto(f"http://127.0.0.1:{port}/?tour=0", wait_until="domcontentloaded")
            pg.wait_for_selector("#featHeader .nr-headbtn", timeout=120000)
            pg.click(".nr-headbtn")
            pg.fill("#nr-text", "help me sort socks")
            pg.click("[data-a=start]")
            pg.wait_for_selector("[data-a=defaults]", timeout=60000)
            pg.click("[data-a=defaults]")
            pg.wait_for_selector("[data-a=build]", timeout=60000)
            pg.click("[data-a=build]")
            until(lambda: pg.evaluate("[...document.querySelectorAll('.nr-st.computed')].map((e) => e.textContent.trim())") == ["Sizing"])
            assert "1 of 9 stages" in pg.inner_text(".nr-build-h")
            gate.set()
            pg.wait_for_selector("[data-a=openproj]", timeout=60000)
        finally:
            gate.set()
            browser.close()
            httpd.shutdown()
