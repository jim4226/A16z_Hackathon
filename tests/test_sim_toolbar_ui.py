"""The Simulation tab's player in the real Studio page (static/panels.js SimPanel), served by an in-process Studio on a
free port, in Chromium without WebGL: at 910, 1280 and 1600 px the scenario pills, the speed pills and Play/Pause are
inside the window and take clicks (in a narrow view the player wraps under the scenarios), and Space plays and pauses,
as the button's title says, also right after the Simulation tab was clicked; not while typing in the chat.
Screenshots go to $MORPH_SHOTS (sim_toolbar_*.png)."""

import json
import os
import shutil
import threading
import time

import pytest

pytest.importorskip("mujoco")
sync_api = pytest.importorskip("playwright.sync_api")

from morph import keys  # noqa: E402
from morph.robot.pipeline2 import RobotProject  # noqa: E402
from morph.studio import server  # noqa: E402
from study_fixture import egg_project  # noqa: E402

CHROME = os.environ.get("PLAYWRIGHT_CHROMIUM") or "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
SHOTS = os.environ.get("MORPH_SHOTS")
SCENARIOS = {"payload_sweep": "Payload sweep", "workspace": "Workspace", "shake": "Shake test"}


@pytest.fixture(scope="module")
def studio(tmp_path_factory):
    """An egg-picker revision with every scenario a full build has (copies of its pick and place: only the pills and
    the player matter here)."""
    mp = pytest.MonkeyPatch()
    home = tmp_path_factory.mktemp("projects")
    root = egg_project(tmp_path_factory, home, "egg-picker")
    sim = os.path.join(root, "r001", "sim")
    with open(os.path.join(root, "r001", "results.json"), encoding="utf-8") as fh:
        res = json.load(fh)
    for name, title in SCENARIOS.items():
        shutil.copy(os.path.join(sim, "sim_pick_place.json"), os.path.join(sim, f"sim_{name}.json"))
        res["sims"]["scenarios"][name] = {**res["sims"]["scenarios"]["pick_place"], "file": f"sim_{name}.json", "title": title}
    with open(os.path.join(root, "r001", "results.json"), "w", encoding="utf-8") as fh:
        json.dump(res, fh)
    mp.setattr(keys, "ENV_PATH", str(home / ".env-test"))
    st = server.Studio(RobotProject(root), str(home))
    httpd = server._Server(("127.0.0.1", 0), server.make_handler(st, None))
    port = httpd.server_address[1]
    httpd.RequestHandlerClass = server.make_handler(st, {f"127.0.0.1:{port}"})
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield f"http://127.0.0.1:{port}"
    httpd.shutdown()
    httpd.server_close()
    mp.undo()


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as pw:
        try:
            b = pw.chromium.launch(args=["--no-sandbox", "--disable-webgl", "--disable-3d-apis"],
                                   **({"executable_path": CHROME} if os.path.exists(CHROME) else {}))
        except Exception as exc:  # noqa: BLE001  (no browser: `playwright install chromium`)
            pytest.skip(f"no Chromium for the browser test ({exc.__class__.__name__})")
        yield b
        b.close()


def until(page, js, timeout=120.0, what=""):
    """Poll an expression in the page (the Studio's policy blocks eval, so no wait_for_function)."""
    end = time.time() + timeout
    while time.time() < end:
        v = page.evaluate(js)
        if v:
            return v
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what or js}")


REACH = """(sel) => [...document.querySelectorAll(sel)].map((e) => { const r = e.getBoundingClientRect();
  const at = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
  return {right: r.right, bottom: r.bottom, hit: !!at && (at === e || e.contains(at))}; })"""
PLAYING = "() => document.querySelector('.sim .playbtn').innerHTML"


@pytest.mark.parametrize("w,h", [(910, 800), (1280, 720), (1600, 900)])
def test_the_player_fits_takes_clicks_and_space(studio, browser, w, h):
    page = browser.new_page(viewport={"width": w, "height": h})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{studio}/?project=egg-picker&tour=0")
    until(page, "() => !!(window.morph && window.morph.state.loaded)", what="the page")
    page.click("#tabs .tab[data-tab='sim']")                     # the tab strip keeps the focus, as after a real click
    until(page, "() => document.querySelectorAll('.sim [data-scen] button').length === 4", what="four scenarios")
    page.wait_for_timeout(300)
    for sel in (".sim [data-scen] button", ".sim [data-speed] button", ".sim .playbtn"):
        for r in page.evaluate(REACH, sel):
            assert r["right"] <= w and r["bottom"] <= h and r["hit"], (w, sel, r)
    assert page.evaluate("() => document.documentElement.scrollWidth") <= w
    if SHOTS:
        os.makedirs(SHOTS, exist_ok=True)
        page.screenshot(path=os.path.join(SHOTS, f"sim_toolbar_{w}.png"))
    icon = page.evaluate(PLAYING)
    page.click(".sim .playbtn")
    assert page.evaluate(PLAYING) != icon                         # the button plays and pauses
    page.click(".sim [data-speed] button[data-s='2']")
    assert page.evaluate("() => document.querySelector('.sim [data-speed] button.on').dataset.s") == "2"
    page.click("#tabs .tab[data-tab='sim']")                      # focus back on the tab strip, then Space
    icon = page.evaluate(PLAYING)
    page.keyboard.press("Space")
    assert page.evaluate(PLAYING) != icon and page.evaluate("() => document.querySelector('#tabs .tab.on').dataset.tab") == "sim"
    page.keyboard.press("Space")
    assert page.evaluate(PLAYING) == icon
    page.click("#input")                                          # typing in the chat: Space is a space
    page.keyboard.press("Space")
    assert page.evaluate(PLAYING) == icon and page.input_value("#input") == " "
    assert "(Space)" in page.get_attribute(".sim .playbtn", "title")
    assert not errors, errors
    page.close()
