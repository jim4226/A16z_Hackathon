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
    with open(os.path.join(room.p.root, "ledger.jsonl"), encoding="utf-8") as fh:
        entry = [json.loads(x) for x in fh if '"revision"' in x][-1]
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
            gate.set()                                                # the build ends now
            pg.wait_for_selector("[data-a=openproj]", timeout=60000)  # the dialog saw it end
            assert st.room("sock-sorter").latest() == "r001"
            assert pg.evaluate("[window.morph.state.current?.rev, window.morph.state.current?.design?.name]") == ["r001", "sock-sorter"]
            assert errors == []
        finally:
            gate.set()
            browser.close()
            httpd.shutdown()
