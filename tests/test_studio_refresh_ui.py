"""A page opened (or refreshed) while a build runs: it listens to live events before its state loads, follows the
build, retries the state with backoff instead of staying offline, and shows the new revision when the build is done.
Runs the real page against a real Studio server whose builds are stand-ins; skipped without Playwright's Chromium."""

import json
import os
import threading

import pytest

from morph import keys
from morph.robot.pipeline2 import ORDER, RobotProject
from morph.spec import Design
from morph.studio import server

pw = pytest.importorskip("playwright.sync_api")
CHROME = os.environ.get("PLAYWRIGHT_CHROMIUM") or "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"


def make_project(home, name):
    root = home / name
    (root / "r001").mkdir(parents=True)
    (root / "meta.json").write_text(json.dumps({"kind": "robot", "source": "", "need": name}), encoding="utf-8")
    (root / "r001" / "design.json").write_text(Design().dumps(), encoding="utf-8")
    (root / "r001" / "results.json").write_text(json.dumps({"rev": "r001", "parent": None, "label": "first design",
                                                            "summary": {}}), encoding="utf-8")
    return RobotProject(str(root))


class GatedBuild:
    """RobotProject.run without the pipeline: starts, waits for `gate`, then runs every stage and writes results."""

    def __init__(self):
        self.gate, self.started = threading.Event(), threading.Event()

    def __call__(self, p, design, parent=None, label="", verbose=True, on_event=None, by=None, **kw):
        emit = on_event or (lambda e: None)
        rev = p.claim_rev()
        with open(os.path.join(p.rev_dir(rev), "design.json"), "w", encoding="utf-8") as fh:
            fh.write(design.dumps())
        emit({"type": "start", "rev": rev, "parent": parent, "changes": {}, "dirty": list(ORDER)})
        emit({"type": "node", "node": "sizing", "status": "computing"})
        self.started.set()
        assert self.gate.wait(60)
        for node in ORDER:
            emit({"type": "node", "node": node, "status": "computing"})
            emit({"type": "node", "node": node, "status": "computed", "seconds": 0.01})
        with open(os.path.join(p.rev_dir(rev), "results.json"), "w", encoding="utf-8") as fh:
            json.dump({"rev": rev, "parent": parent, "label": label, "summary": {},
                       "provenance": {n: "computed" for n in ORDER}, "stage_status": {n: "computed" for n in ORDER}}, fh)
        return rev


@pytest.fixture
def live(tmp_path, monkeypatch):
    """A Studio over HTTP whose first build is held at the gate, and a Chromium page to open it with."""
    monkeypatch.setattr(keys, "ENV_PATH", str(tmp_path / ".env"))
    build = GatedBuild()
    monkeypatch.setattr(RobotProject, "run", lambda self, *a, **k: build(self, *a, **k))
    home = tmp_path / "projects"
    studio = server.Studio(make_project(home, "egg"), str(home))
    httpd = server._Server(("127.0.0.1", 0), server.make_handler(studio, None))
    port = httpd.server_address[1]
    httpd.RequestHandlerClass = server.make_handler(studio, {f"127.0.0.1:{port}", f"localhost:{port}"})
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    with pw.sync_playwright() as p:
        try:
            browser = p.chromium.launch(executable_path=CHROME if os.path.exists(CHROME) else None, args=["--no-sandbox"])
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"no Chromium for Playwright ({exc.__class__.__name__})")
        page = browser.new_page(viewport={"width": 1400, "height": 900})
        try:
            yield studio, build, page, f"http://127.0.0.1:{port}/?tour=0"
        finally:
            build.gate.set()
            browser.close()
            httpd.shutdown()
            httpd.server_close()


def failing_state(page, studio, fails):
    """Answer /api/state with 500 while fails() says so; record each try and how many tabs were listening then."""
    tries = []

    def handle(route):
        tries.append(len(studio.room().hub.online()))
        if fails(len(tries)):
            route.fulfill(status=500, content_type="application/json", body='{"ok": false, "error": "boom"}')
        else:
            route.continue_()
    page.route("**/api/state*", handle)
    return tries


def conn(page):
    return page.inner_text("#conn").strip()


def test_a_tab_opened_mid_build_recovers_when_its_state_fails(live):
    studio, build, page, url = live
    studio.queue_params(studio.room(), {"arm.dof": 6})
    assert build.started.wait(10)
    tries = failing_state(page, studio, lambda n: n <= 2)          # the first two state loads fail
    page.goto(url)
    page.wait_for_function("() => window.morph && window.morph.state.loaded === true", timeout=30000)
    assert len(tries) >= 3 and max(tries[:2]) >= 1                  # it was listening while its state failed
    assert conn(page) == "Regenerating"                              # the build in progress, not "Offline"
    assert page.locator("#pipeline .node.computing").count() >= 1
    build.gate.set()
    page.wait_for_selector('#revs .rev[data-rev="r002"]', timeout=30000)
    page.wait_for_function("() => window.morph.state.current?.rev === 'r002' && !window.morph.state.busy", timeout=30000)
    assert conn(page) == "Live"
    assert "r002 ready" in page.inner_text("#messages")


def test_events_show_the_result_even_while_the_state_keeps_failing(live):
    studio, build, page, url = live
    studio.queue_params(studio.room(), {"arm.dof": 6})
    assert build.started.wait(10)
    broken = {"on": True}
    tries = failing_state(page, studio, lambda n: broken["on"])
    page.goto(url)
    page.wait_for_function("() => document.querySelector('#insp')?.innerText.includes('Trying again')", timeout=30000)
    assert "Can't load the project yet" in page.inner_text("#insp") and "500" in page.inner_text("#insp")
    page.wait_for_function("() => window.morph.studio && true", timeout=5000)
    assert len(studio.room().hub.online()) >= 1                      # subscribed although the state never loaded
    build.gate.set()                                                 # its node events show the build, done shows r002
    page.wait_for_function("() => window.morph.state.current?.rev === 'r002'", timeout=30000)
    assert not page.evaluate("window.morph.state.loaded")
    broken["on"] = False                                             # the server answers again: the next try loads it
    page.wait_for_function("() => window.morph.state.loaded === true", timeout=40000)
    assert page.evaluate("window.morph.state.revs") == ["r001", "r002"] and len(tries) >= 3
    assert conn(page) == "Live"
