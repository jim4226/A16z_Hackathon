"""The Robustness (beta) card (static/study.js) in the real Studio page, served by an in-process Studio on a free port,
in Chromium without WebGL: the note where no study exists, a study started from the card (the click's request runs at
2 ms here), live progress, the result in the honest words of api_study, the details dialog with every tolerance and
its source, a tolerance edit that makes the study outdated and back, a run replayed (its numbers: no 3D view here),
and a read-only member's page without the editor's controls. Screenshots go to $MORPH_SHOTS (s2_ui_*.png)."""

import json
import os
import re
import threading
import time

import pytest

pytest.importorskip("mujoco")
sync_api = pytest.importorskip("playwright.sync_api")

from morph import keys  # noqa: E402
from morph.robot.pipeline2 import RobotProject  # noqa: E402
from morph.studio import api_study, jobs, server  # noqa: E402
from study_fixture import egg_project  # noqa: E402

CHROME = os.environ.get("PLAYWRIGHT_CHROMIUM") or "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
SHOTS = os.environ.get("MORPH_SHOTS")
CACHE = {}


@pytest.fixture(scope="module")
def studio(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    mp.setenv("MORPH_STUDY_NICE", "0")
    home = tmp_path_factory.mktemp("projects")
    egg_project(tmp_path_factory, home, "egg-picker", CACHE)
    mp.setattr(keys, "ENV_PATH", str(home / ".env-test"))
    st = server.Studio(RobotProject(str(home / "egg-picker")), str(home))
    jobs.manager(st).cache.update(CACHE)
    httpd = server._Server(("127.0.0.1", 0), server.make_handler(st, None))
    port = httpd.server_address[1]
    httpd.RequestHandlerClass = server.make_handler(st, {f"127.0.0.1:{port}"})
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    yield st, f"http://127.0.0.1:{port}"
    jobs.shutdown(st)
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


def until(page, js, timeout=180.0, what=""):
    """Poll an expression in the page (the Studio's policy blocks eval, so no wait_for_function)."""
    end = time.time() + timeout
    while time.time() < end:
        v = page.evaluate(js)
        if v:
            return v
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what or js}")


def card(page) -> str:
    return " ".join(page.evaluate("() => document.querySelector('#studyCard')?.innerText || ''").split())


def shot(page, name):
    if SHOTS:
        os.makedirs(SHOTS, exist_ok=True)
        page.screenshot(path=os.path.join(SHOTS, f"s2_ui_{name}.png"))


def test_the_card_starts_follows_and_explains_a_study(studio, browser):
    st, base = studio
    ctx = browser.new_context(viewport={"width": 1280, "height": 720})
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" and "WebGL" not in m.text else None)
    started = []

    def faster(route):                # the card's own request, at 2 ms steps so the test takes seconds
        body = json.loads(route.request.post_data or "{}")
        started.append(body)
        route.continue_(post_data=json.dumps({**body, "timestep_ms": 2, "workers": 2}))
    page.route(re.compile(r".*/api/study(\?.*)?$"), lambda route: faster(route) if route.request.method == "POST" else route.continue_())
    page.goto(f"{base}/?project=egg-picker&tab=sim&tour=0")
    until(page, "() => !!(window.morph && window.morph.state.loaded && document.querySelector('#studyCard .sd-start'))",
          what="the card")
    text = card(page)
    assert "Robustness beta" in text and api_study.NOTES["none"] in text and "Run a study" in text
    assert "on this computer" in text and "estimated" in text
    # the card sits right under the pick-and-place result in the Simulation tab
    assert page.evaluate("() => document.querySelector('#studyCard').previousElementSibling.classList.contains('success')")
    shot(page, "none")

    # start: a small study (an extra choice in the menu), then follow it
    page.evaluate("() => { const s = document.querySelector('#studyCard select[data-f=\"runs\"]'); s.add(new Option('4 runs', '4'));"
                  " s.value = '4'; s.dispatchEvent(new Event('change', {bubbles: true})); }")
    page.click("#studyCard [data-a='start']")
    until(page, "() => !!document.querySelector('#studyCard .sd-run')", 30, "progress")
    assert started == [{"runs": 4, "stress": False, "rev": "r001"}]
    seen = set()
    end = time.time() + 240
    while time.time() < end and not page.evaluate("() => !!document.querySelector('#studyCard .sd-head')"):
        t = card(page)
        seen |= {m for m in ("Preparing", "Run ", "So far", "Checking failures") if m in t}
        time.sleep(0.15)
    assert {"Run ", "So far"} <= seen or "Checking failures" in seen, seen
    until(page, "() => !document.querySelector('#studyCard .sd-run')", 60, "the progress to end")
    text = card(page)
    m = re.search(r"Succeeds in (all 4|\d of 4) randomized runs \(([^)]*)\); the nominal run succeeds\.", text)
    assert m, text
    assert m.group(2).startswith(("likely ", "at least ")) and "CI" not in text and "solver" not in text, text
    assert "95% confidence interval" in page.get_attribute("#studyCard .sd-head", "title")
    assert api_study.NOTES["simulation"] in text and "Run a study" in text      # 2 ms here: the card's own run 1 ms
    shot(page, "done")
    # the card's own settings (seed 1, 1 ms) again deepen that study: "Run 46 more", or nothing to do at 4 runs
    page.evaluate("() => { const c = window.morphStudy; c.info.latest = {...c.info.latest, timestep: 0.001, seed: 1};"
                  " c.form.runs = 50; c.render(); }")
    assert "Run 46 more" in card(page) and "estimated" in card(page)
    page.evaluate("() => { const c = window.morphStudy; c.form.runs = 4; c.render(); }")
    assert "Done: 4 runs" in card(page) and "This study already has these runs: choose more to deepen it." in card(page)
    assert page.evaluate("() => document.querySelector('#studyCard [data-a=\"start\"]').disabled")
    page.evaluate("() => { window.morphStudy.form.runs = 200; window.morphStudy.refresh(); }")
    until(page, "() => document.querySelector('#studyCard').innerText.includes('Run a study')", 30, "the card again")

    # the details: the result again, and every tolerance with where its range comes from
    page.click("#studyCard [data-a='details']")
    until(page, "() => !!document.querySelector('.sd-dlg .sd-tbl tbody tr')", 30, "the tolerances")
    rows = page.evaluate("() => [...document.querySelectorAll('.sd-dlg .sd-tbl tbody tr')].map((r) => r.innerText)")
    assert len(rows) == 11 and any("USDA egg weight classes" in r for r in rows) and any("FSR 400" in r for r in rows)
    shot(page, "details")
    # widen nothing, narrow the yaw range: the study is outdated, and back to the defaults
    page.fill(".sd-dlg [data-t='object_yaw_deg'][data-k='lo']", "-5")
    page.fill(".sd-dlg [data-t='object_yaw_deg'][data-k='hi']", "5")
    page.click(".sd-dlg [data-a='savetol']")
    until(page, "() => (document.querySelector('.sd-dlg [data-out=\"tol\"]') || {}).textContent === 'Saved.'", 30, "saved")
    until(page, "() => document.querySelector('#studyCard').innerText.includes('outdated')", 30, "outdated")
    assert "Run again with these tolerances" in card(page)
    page.click(".sd-dlg [data-a='resettol']")
    until(page, "() => !document.querySelector('#studyCard').innerText.includes('outdated')", 30, "current again")
    page.click(".sd-dlg [data-a='close']")

    # a run replayed: no 3D view in this browser, so the card gives its numbers
    sid = page.evaluate("() => window.morphStudy.info.latest.id")
    page.evaluate("(sid) => window.morphStudy.replay(sid, 1)", sid)
    until(page, "() => !!(window.morphStudy.rp && window.morphStudy.rp.doc)", 120, "the replay")
    text = card(page)
    assert "Replay of run 1" in text and "can't play it here" in text and "Back to the nominal run" in text
    shot(page, "replay")
    page.click("#studyCard [data-a='stopreplay']")
    until(page, "() => !document.querySelector('#studyCard .sd-replay')", 10, "the replay to close")

    # on a Studio other computers use (--remote), the card says where the study runs: the Studio's computer
    st.remote = True
    try:
        page.evaluate("() => window.morphStudy.refresh()")
        until(page, "() => document.querySelector('#studyCard').innerText.includes(\"the Studio's computer\")", 30, "the host")
    finally:
        st.remote = False

    # a read-only member (Teams marks the page): the result stays, the editor's controls go
    page.evaluate("() => document.body.classList.add('tm-readonly')")
    hidden = page.evaluate("() => [...document.querySelectorAll('#studyCard .sd-edit')].every((e) => !e.offsetParent)")
    assert hidden and "Succeeds in" in card(page)
    page.evaluate("() => { document.body.classList.remove('tm-readonly'); window.morphStudy.info.may.start = false; window.morphStudy.render(); }")
    assert not page.evaluate("() => !!document.querySelector('#studyCard [data-a=\"start\"]')")

    # another study, started with the Run button scrolled down to the bottom of the column under an earlier result with
    # failure groups: its progress and Cancel come into view, and the earlier result waits while it runs
    page.evaluate("() => window.morphStudy.refresh()")
    until(page, "() => !!document.querySelector('#studyCard [data-a=\"start\"]')", 30, "the editor's controls again")
    page.evaluate("""() => { const c = window.morphStudy, g = {id: 'jam', label: 'Jaws stayed shut at release', count: 3,
        share: '3 of 4 runs (likely between 30% and 96%)', rule: null, sensitivity: 'Not checked with other simulator settings yet.',
        model_sensitive: null, example_run: 1};
      c.info.latest = {...c.info.latest, card: {...c.info.latest.card, groups: [g, {...g, id: 'drag'}, {...g, id: 'miss'}]}};
      c.form.runs = 50; c.render(); }""")                                     # long enough to look at it running
    side = "document.querySelector('#views .view[data-view=\"sim\"] .sim .side')"
    page.evaluate(f"() => {{ const s = {side}; s.scrollTop = s.scrollHeight; }}")
    before = page.evaluate(f"() => ({{top: {side}.scrollTop, card: document.querySelector('#studyCard').getBoundingClientRect().top,"
                           f" col: {side}.getBoundingClientRect().top}})")
    assert before["top"] > 0 and before["card"] < before["col"], before    # the card's top is scrolled out of view
    page.click("#studyCard [data-a='start']")
    until(page, "() => !!document.querySelector('#studyCard [data-a=\"cancel\"]')", 30, "Cancel")
    page.wait_for_timeout(300)
    box = page.evaluate(f"""() => {{ if (!document.querySelector('#studyCard [data-a="cancel"]')) return null;
      const b = document.querySelector('#studyCard [data-a="cancel"]').getBoundingClientRect(),
        s = {side}.getBoundingClientRect(), r = document.querySelector('#studyCard .sd-run').getBoundingClientRect(),
        at = document.elementFromPoint(b.left + b.width / 2, b.top + b.height / 2);
      return {{cancel: [b.top, b.bottom], run: [r.top, r.bottom], col: [s.top, s.bottom], vh: innerHeight,
              hit: !!at && !!at.closest('[data-a="cancel"]'), groups: document.querySelectorAll('#studyCard .sd-g').length}}; }}""")
    assert box, "the study ended before it could be looked at"
    assert box["col"][0] <= box["run"][0] and box["cancel"][1] <= min(box["col"][1], box["vh"]) and box["hit"], box
    assert box["groups"] == 0 and "Jaws stayed shut" not in card(page), box
    shot(page, "running_again")
    page.click("#studyCard [data-a='cancel']")
    until(page, "() => !document.querySelector('#studyCard .sd-run')", 120, "the study to stop")
    assert not errors, errors
    ctx.close()
