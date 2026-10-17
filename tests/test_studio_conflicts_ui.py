"""A change that waits for an answer (someone else changed the same setting first) in the real page: the question has
Apply and Drop while it waits, also after a reload or in a tab opened later; once any tab answers it, the other tabs'
buttons go; a viewer on a team Studio sees no buttons but who can answer. Real Studio server, stand-in builds held at a
gate; skipped without Playwright's Chromium."""

import os
import threading
import time

import pytest

from morph import keys
from morph.robot.pipeline2 import RobotProject
from morph.studio import api_team, server
from morph.studio.teams import activity, auth
from test_studio_refresh_ui import GatedBuild, make_project
from test_team_util import make_team

pw = pytest.importorskip("playwright.sync_api")
CHROME = os.environ.get("PLAYWRIGHT_CHROMIUM") or "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
ASK = "#messages .msg.assistant:has-text('Not applied yet')"


@pytest.fixture
def studio_at(tmp_path, monkeypatch):
    """start(team=False) -> (studio, build, url, port): a Studio over HTTP whose builds wait at `build.gate`."""
    monkeypatch.setattr(keys, "ENV_PATH", str(tmp_path / ".env"))
    monkeypatch.setenv("MORPH_OFFLINE", "1")
    build = GatedBuild()
    monkeypatch.setattr(RobotProject, "run", lambda self, *a, **k: build(self, *a, **k))
    servers = []

    def start(team=False):
        home = tmp_path / "projects"
        studio = server.Studio(make_project(home, "egg"), str(home), team=team)
        httpd = server._Server(("127.0.0.1", 0), server.make_handler(studio, None))
        port = httpd.server_address[1]
        if not team:
            httpd.RequestHandlerClass = server.make_handler(studio, {f"127.0.0.1:{port}", f"localhost:{port}"})
        threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        servers.append(httpd)
        return studio, build, f"http://127.0.0.1:{port}/?tour=0", port

    auth.THROTTLE.clear()
    activity.PRESENCE.seen.clear()
    yield start
    build.gate.set()
    for httpd in servers:
        httpd.shutdown()
        httpd.server_close()
    api_team.configure(home=None, enabled=False)


@pytest.fixture
def browser():
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch(executable_path=CHROME if os.path.exists(CHROME) else None,
                                  args=["--no-sandbox", "--disable-webgl", "--disable-3d-apis"])  # the chat is what's tested
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"no Chromium for Playwright ({exc.__class__.__name__})")
        try:
            yield b
        finally:
            b.close()


def page_at(browser, url, cookie=None, port=None):
    ctx = browser.new_context(viewport={"width": 1400, "height": 900})
    if cookie:
        name, value = cookie.split("=", 1)
        ctx.add_cookies([{"name": name, "value": value, "url": f"http://127.0.0.1:{port}"}])
    pg = ctx.new_page()
    pg.set_default_timeout(60000)
    errors = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    pg.goto(url)
    pg.wait_for_selector("#messages .msg")  # selectors, not predicates: the page's policy allows no eval
    until(lambda: pg.evaluate("!!(window.morph && window.morph.studio)"))
    return pg, errors


def params(pg, changes):
    """An edit from this tab, as its Parameters panel sends it (its own X-Morph-Client, the revision it shows)."""
    return pg.evaluate("(c) => window.morph.studio.api.post('/api/params', {changes: c, base_rev: 'r001'})", changes)


def until(cond, timeout=20.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.05)
    raise AssertionError("timed out")


def test_a_waiting_conflict_keeps_its_buttons_after_a_reload_and_loses_them_once_answered(studio_at, browser):
    studio, build, url, _ = studio_at()
    a, a_errors = page_at(browser, url)
    b, b_errors = page_at(browser, url)
    params(a, {"build.config": "pilot"})                       # tab A's edit builds...
    assert build.started.wait(20)
    params(b, {"build.config": "production"})                  # ...while tab B, also showing r001, edits the same setting
    build.gate.set()
    room = studio.room()
    until(lambda: any(e.get("conflict") for e in room.hub.history))
    for pg in (a, b):
        pg.wait_for_selector(f"{ASK} .qacts.qconf button[data-q='apply']")
        assert "Build changed after r001" in pg.inner_text(ASK)
    a.reload()                                                  # after a reload the question still has its buttons
    a.wait_for_selector(f"{ASK} .qacts.qconf button[data-q='apply']:visible")
    c, c_errors = page_at(browser, url)                         # and so does a tab opened later
    c.wait_for_selector(f"{ASK} .qacts.qconf button[data-q='drop']:visible")
    b.click(f"{ASK} button[data-q='drop']")                    # tab B answers: everyone else's buttons go
    for pg in (a, c):
        pg.wait_for_selector(f"{ASK} .qgone", timeout=10000)
        assert pg.locator(f"{ASK} .qacts.qconf").count() == 0
        assert "No longer waiting for an answer." in pg.inner_text(ASK)
    assert studio.state(room)["conflicts"] == [] and room.latest() == "r002"
    a.reload()
    a.wait_for_selector(f"{ASK} .qgone")
    assert a.locator(f"{ASK} .qacts.qconf").count() == 0
    assert a_errors == b_errors == c_errors == []


def test_a_viewer_sees_who_can_answer_a_conflict_instead_of_buttons(studio_at, browser):
    studio, build, url, port = studio_at(team=True)
    ids, cookies = make_team(studio.home, (("Jim", "owner", ["generalist"]), ("Ed", "editor", ["electronics"]),
                                           ("Vic", "viewer", [])))
    jar = {n: f"{auth.cookie_name(port)}={c}" for n, c in cookies.items()}
    ed, _ = page_at(browser, url, jar["Ed"], port)
    vic, vic_errors = page_at(browser, url, jar["Vic"], port)
    vic.wait_for_selector("body.tm-readonly")
    params(ed, {"build.config": "pilot"})
    assert build.started.wait(20)
    jim, _ = page_at(browser, url, jar["Jim"], port)
    params(jim, {"build.config": "production"})
    build.gate.set()
    until(lambda: any(e.get("conflict") for e in studio.room().hub.history))
    vic.wait_for_selector(f"{ASK} .qacts.qconf")
    assert vic.locator(f"{ASK} .qacts.qconf .btn:visible").count() == 0
    assert vic.locator(f"{ASK} .qnote").is_visible() and "An editor can apply it or drop it." in vic.inner_text(ASK)
    ed.wait_for_selector(f"{ASK} .qacts.qconf .btn[data-q='apply']:visible")   # an editor can answer
    assert vic_errors == []
