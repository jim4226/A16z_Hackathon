"""The box gizmo and the AI review's pictures (World mode): the six review views hide the transform gizmo while they
render and then put it back as it was. A box selected and then deselected leaves no gizmo on screen after a review (it
used to come back, detached, where the box had been); a box still selected keeps its gizmo. Checked on the pixels:
the view after the review is drawn again with the gizmo forced off, and the two pictures must match.
tools/world_demo.html with the real RobotViewer and world.js, software WebGL (about a minute)."""

import functools
import http.server
import io
import os
import sys
import threading
import time

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

sys.path.insert(0, os.path.dirname(__file__))
from test_world_browser import ARGS, CHROME, ROOT, SHOTS, _desk_inputs, _Quiet  # noqa: E402

GIZMO = "(() => { const tc = window.demo.wv.tc; return tc ? {visible: tc.visible, attached: !!tc.object} : null; })()"


@pytest.fixture(scope="module")
def server():
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Quiet, directory=ROOT))  # any free port
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as pw:
        try:
            b = pw.chromium.launch(args=["--no-sandbox", *ARGS], **({"executable_path": CHROME} if CHROME else {}))
        except Exception as exc:            # no browser installed: `playwright install chromium`
            pytest.skip(f"no Chromium for the browser test ({exc.__class__.__name__})")
        yield b
        b.close()


def _until(page, js, timeout=180.0):
    end = time.time() + timeout
    while time.time() < end:
        if page.evaluate(js):
            return
        page.wait_for_timeout(200)
    raise AssertionError(f"timed out waiting for {js}")


# the 3D view drawn twice in one go, as it is and with the gizmo forced off (read back before the page composites)
TWICE = """() => { const v = window.demo.viewer, tc = window.demo.wv.tc, c = v.renderer.domElement;
  v._render(); const shown = c.toDataURL('image/png');
  const was = tc.visible; tc.visible = false; v._render(); const hidden = c.toDataURL('image/png');
  tc.visible = was; v._render(); return [shown, hidden]; }"""


def _gizmo_pixels(page) -> int:
    """How many pixels of the 3D view change when the gizmo is forced off (none, when it isn't drawn)."""
    import base64

    from PIL import Image, ImageChops

    shown, hidden = (Image.open(io.BytesIO(base64.b64decode(u.split(",", 1)[1]))).convert("RGB") for u in page.evaluate(TWICE))
    diff = ImageChops.difference(shown, hidden).convert("L").point(lambda v: 255 if v > 24 else 0)
    return diff.histogram()[255]


def _press(page, sel):
    """A panel button, pressed from the page (Playwright's own click waits for frames that software WebGL draws slowly)."""
    assert page.evaluate("(s) => { const b = document.querySelector(s); if (!b || b.disabled) return false; b.click(); return true; }", sel), sel


def _review(page):
    page.evaluate("window.demo.store.lastViews = null")
    _press(page, '.wv-panel [data-a="review"]')
    _until(page, "!!(window.demo.panel.state.result && !window.demo.panel.state.busy && window.demo.store.lastViews"
                 " && window.demo.store.lastViews.length === 6)")


def test_a_deselected_box_leaves_no_gizmo_after_an_ai_review(server, browser):
    page = browser.new_page(viewport={"width": 1100, "height": 720})
    page.set_default_timeout(90000)                   # software WebGL on a busy machine can be very slow
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{server}/tools/world_demo.html?studio=1", timeout=120000)
    _until(page, "window.ready === true")
    desk, doc, inspect = _desk_inputs()
    page.evaluate("([w, d, i]) => window.demo.load(w, d, {starters: {desk: w}, inspect: i})", [desk, doc, inspect])
    box = desk["obstacles"][0]["id"]
    page.evaluate("window.demo.wv.frameOverview(0); window.demo.viewer.pause()")  # a still scene: software WebGL draws on demand

    # select a box from the panel's list: Edit mode, the gizmo on the box
    _press(page, f'.wv-panel [data-a="sel"][data-arg="{box}"]')
    assert page.evaluate(GIZMO) == {"visible": True, "attached": True}
    assert page.evaluate("window.demo.wv.selected") == box
    # deselect it the way a person does: a click on empty space while editing
    page.evaluate("window.demo.panel.el.style.display = 'none'")
    r = page.evaluate("(() => { const r = window.demo.viewer.renderer.domElement.getBoundingClientRect(); "
                      "return [r.left + 12, r.top + 12]; })()")
    page.mouse.click(*r)
    page.evaluate("window.demo.panel.el.style.display = 'block'; window.demo.panel.render()")
    assert page.evaluate("window.demo.wv.selected") is None
    assert page.evaluate(GIZMO) == {"visible": False, "attached": False}

    # the AI review renders its six views; afterwards the gizmo is still hidden, and not a pixel of it is drawn
    _review(page)
    assert page.evaluate(GIZMO) == {"visible": False, "attached": False}
    assert _gizmo_pixels(page) == 0
    if SHOTS:
        os.makedirs(SHOTS, exist_ok=True)
        page.evaluate("window.demo.panel.el.style.display = 'none'")
        page.screenshot(path=os.path.join(SHOTS, "h0a_after_review_deselected.png"))
        page.evaluate("window.demo.panel.el.style.display = 'block'")

    # a box still selected keeps its gizmo through the review's pictures (one small view here), and the pixel check
    # sees a gizmo that is drawn
    _press(page, f'.wv-panel [data-a="sel"][data-arg="{box}"]')
    assert page.evaluate(GIZMO) == {"visible": True, "attached": True}
    page.evaluate("async () => (await window.demo.wv.snapshotViews(window.demo.wv._docCache, "
                  "{names: ['overview'], width: 320, height: 240})).length")
    assert page.evaluate(GIZMO) == {"visible": True, "attached": True}
    assert _gizmo_pixels(page) > 200
    if SHOTS:
        page.evaluate("window.demo.panel.el.style.display = 'none'")
        page.screenshot(path=os.path.join(SHOTS, "h0a_after_review_selected.png"))
    assert not errors, errors
    page.close()


def test_a_box_edited_by_hand_no_longer_covers_its_place(server, browser):
    """A box Add box here made keeps the place it was fitted for ("covers"); resized or moved by hand (the gizmo or the
    panel's numbers), or the room rescaled, it is no longer that box, so its place goes with the edit and the next check
    can offer the place again. A box not touched keeps it."""
    page = browser.new_page(viewport={"width": 1100, "height": 720})
    page.set_default_timeout(90000)
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{server}/tools/world_demo.html?studio=1", timeout=120000)
    _until(page, "window.ready === true")
    desk, doc, inspect = _desk_inputs()
    for o in desk["obstacles"][:3]:
        o["covers"] = {"center": o["center"], "half": [h + 10.0 for h in o["half"]], "yaw_deg": o["yaw_deg"],
                       "clearance_mm": 6.0, "rev": "r001"}
    page.evaluate("([w, d, i]) => window.demo.load(w, d, {starters: {desk: w}, inspect: i})", [desk, doc, inspect])
    page.evaluate("window.demo.wv.frameOverview(0); window.demo.viewer.pause()")
    ids = [o["id"] for o in desk["obstacles"][:3]]
    covers = "(id) => 'covers' in window.demo.wv.world.obstacles.find((o) => o.id === id)"
    assert all(page.evaluate(covers, i) for i in ids)
    _press(page, f'.wv-panel [data-a="sel"][data-arg="{ids[0]}"]')        # the gizmo on it: a drag that sizes it
    page.evaluate("(() => { const tc = window.demo.wv.tc; tc.setMode('scale'); tc.object.scale.set(1, 1, 0.8);"
                  " tc.dispatchEvent({type: 'objectChange'}); })()")
    page.evaluate("([id]) => window.demo.wv.setBoxFromBase(id, {h: 120})", [ids[1]])     # the panel's numbers
    assert [page.evaluate(covers, i) for i in ids] == [False, False, True]
    page.evaluate("(() => { const wv = window.demo.wv; wv.scaleRef = {measured: 100, a: [0, 0, 0], b: [100, 0, 0]};"
                  " return wv.applyScale(101); })()")                        # the room rescaled
    assert page.evaluate(covers, ids[2]) is False
    assert not errors, errors
    page.close()
