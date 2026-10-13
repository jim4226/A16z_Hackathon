"""World mode in a real browser: tools/world_demo.html with the real RobotViewer, world.js and the fixture robot.
Loads the desk starter through init(studio), places the robot by clicking the desk, walks (keys and drag-look),
snapshots the six views and plays the guided tour; then a room scan (.glb) with the two-point scale tool and
proposed boxes. Screenshots go to $MORPH_SHOTS (default: morph-shots in the temp folder), f5_*.png. Skipped without
Playwright or a Chromium it can start ($MORPH_CHROME, else Playwright's own). Software WebGL is slow: about two minutes."""

import base64
import functools
import http.server
import io
import json
import os
import sys
import tempfile
import threading

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

from morph.cli import EGG_ROBOT  # noqa: E402
from morph.spec import Design  # noqa: E402
from morph.studio import worlds  # noqa: E402

sys.path.insert(0, os.path.dirname(__file__))
from test_world_checks import ROBOT, results  # noqa: E402
from test_world_obstacles import counter_scene_glb  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHROME = os.environ.get("MORPH_CHROME") or next(
    (p for p in ("/opt/pw-browsers/chromium-1194/chrome-linux/chrome",) if os.path.exists(p)), None)
SHOTS = os.environ.get("MORPH_SHOTS") or os.path.join(tempfile.gettempdir(), "morph-shots")
ARGS = ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"]   # WebGL without a GPU


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def server():
    handler = functools.partial(_Quiet, directory=ROOT)
    for port in range(8800, 8900):
        try:
            httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
            break
        except OSError:
            continue
    else:
        pytest.skip("no free port in 8800-8899")
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as pw:
        try:
            b = pw.chromium.launch(args=ARGS, **({"executable_path": CHROME} if CHROME else {}))
        except Exception as exc:            # no browser installed: `playwright install chromium`
            pytest.skip(f"no Chromium for the browser test ({exc.__class__.__name__})")
        yield b
        b.close()


def _page(browser):
    page = browser.new_page(viewport={"width": 1100, "height": 720})
    page.set_default_timeout(90000)                   # software WebGL on a busy machine can be very slow
    return page


def shot(page, name):
    os.makedirs(SHOTS, exist_ok=True)
    page.screenshot(path=os.path.join(SHOTS, name))


def _image(b64):
    from PIL import Image

    return Image.open(io.BytesIO(base64.b64decode(b64)))


def _desk_inputs():
    desk = worlds.starter("desk")
    with open(os.path.join(ROOT, "morph/studio/static/fixtures/sims/sim_pick_place.json"), encoding="utf-8") as fh:
        doc = json.load(fh)
    doc["world"] = worlds.sim_world(desk)
    doc["metrics"].update({"world": "desk", "world_sha": doc["world"]["sha"], "min_clearance_mm": 6.5, "world_contacts": [],
                           "min_clearance_at": {"t": 3.5, "link": "jaw_left", "obstacle": "Mug"}, "reach_3d_mm": 560})
    res = worlds.checks(desk, results(desk, pp={"min_clearance_mm": 6.5}), robot=ROBOT, design=Design().with_changes(EGG_ROBOT))
    inspect = {"ok": True, "rev": "r002", "world": "desk", "verdict": res["verdict"], "checks": res["checks"],
               "review": worlds.rules_review(res["facts"], worlds.VIEWS, desk), "subq": None, "facts": res["facts"]}
    return desk, doc, inspect


def test_desk_world_place_walk_views_and_tour(server, browser):
    page = _page(browser)
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{server}/tools/world_demo.html?studio=1", timeout=120000)
    page.wait_for_function("window.ready === true", timeout=120000)
    desk, doc, inspect = _desk_inputs()
    page.evaluate("([w, d, i]) => window.demo.load(w, d, {starters: {desk: w}, inspect: i})", [desk, doc, inspect])
    assert page.evaluate("window.demo.wv.active && window.demo.panel.open")
    assert page.evaluate("window.demo.wv.world.id") == "desk" and page.evaluate("!!window.demo.wv._docCache")
    page.wait_for_timeout(1500)
    shot(page, "f5_desk_loaded.png")
    png = page.screenshot()
    from PIL import Image, ImageStat
    assert max(ImageStat.Stat(Image.open(io.BytesIO(png)).convert("L")).stddev) > 20, "the 3D view rendered nothing"
    assert page.evaluate("window.viewer._floor.visible") is False        # the plain stage floor hides in a room

    # ---- place the robot: Place mode, click the desk top
    page.evaluate("window.demo.panel.el.style.display = 'none'; window.demo.wv.setMode('place')")
    page.evaluate("window.demo.wv.flyToView({eye: [900, -700, 900], target: [100, 0, 0]}, 0)")
    page.wait_for_timeout(600)
    target = page.evaluate("window.demo.wv.baseToWorld([200, 150, 0])")
    s = page.evaluate("p => window.demo.screenOf(p)", target)
    assert s["visible"] and 0 < s["x"] < 1100 and 0 < s["y"] < 720
    page.mouse.click(s["x"], s["y"])
    page.wait_for_timeout(800)
    m = page.evaluate("window.demo.wv.world.mount")
    assert abs(m["position"][0] - target[0]) < 30 and abs(m["position"][1] - target[1]) < 30
    assert m["position"][2] == pytest.approx(740.0) and m["yaw_deg"] == pytest.approx(-90.0)
    page.evaluate("window.demo.wv.rotate(15)")
    assert page.evaluate("window.demo.wv.world.mount.yaw_deg") == pytest.approx(-75.0)
    assert page.evaluate("window.demo.wv.world._dirty") is True
    shot(page, "f5_desk_placed.png")
    page.evaluate("window.demo.wv.setMode('orbit')")

    # ---- walk: keys move a capsule at eye height (pointer lock may not exist headless: drag-look still works)
    page.evaluate("window.demo.wv.walk(true)")
    page.wait_for_timeout(600)
    moved = []
    for key in ("KeyS", "KeyA", "KeyD", "KeyW"):                       # furniture may block some directions
        p0 = page.evaluate("window.demo.wv.capsule.end.toArray()")
        page.keyboard.down(key)
        try:                                                          # software frames can be slow: hold until it moves
            page.wait_for_function("([x, z]) => { const p = window.demo.wv.capsule.end; return Math.hypot(p.x - x, p.z - z) > 250; }",
                                   arg=[p0[0], p0[2]], timeout=5000)
        except sync_api.TimeoutError:
            pass
        page.keyboard.up(key)
        page.wait_for_timeout(300)
        p1 = page.evaluate("window.demo.wv.capsule.end.toArray()")
        moved.append(((p1[0] - p0[0]) ** 2 + (p1[2] - p0[2]) ** 2) ** 0.5)
    assert max(moved) > 150 and sum(d > 50 for d in moved) >= 2, moved
    eye = page.evaluate("window.demo.viewer.camera.position.y - window.demo.wv.floorZ")
    assert 1500 < eye < 1700, eye                                         # eyes 1.6 m above the floor
    q0 = page.evaluate("window.demo.viewer.camera.quaternion.toArray()")
    page.evaluate("window.demo.wv.look(80, 20)")
    assert page.evaluate("window.demo.viewer.camera.quaternion.toArray()") != q0
    fps = page.evaluate("window.demo.wv.stats.fps")
    shot(page, "f5_desk_walk.png")
    page.evaluate("document.exitPointerLock && document.exitPointerLock()")
    page.keyboard.press("Escape")                                         # with the mouse free, Esc stops walking
    page.wait_for_timeout(300)
    assert page.evaluate("!window.demo.wv.walking && window.demo.wv.mode === 'orbit' && window.demo.viewer.controls.enabled")

    # ---- the six views, 1024 x 768 JPEG with labels drawn in
    views = page.evaluate("async () => (await window.demo.wv.snapshotViews(window.demo.wv._docCache)).map(v => ({name: v.name, b: v.jpeg_b64}))")
    assert [v["name"] for v in views] == list(worlds.VIEWS)
    os.makedirs(SHOTS, exist_ok=True)
    for v in views:
        img = _image(v["b"])
        assert img.format == "JPEG" and img.size == (1024, 768)
        assert max(img.getpixel((4, 4))) < 80                              # the title bar is drawn over the render
        assert ImageStat.Stat(img.convert("L").crop((0, 40, 1024, 740))).stddev[0] > 8, v["name"]   # not blank
        img.save(os.path.join(SHOTS, f"f5_view_{v['name']}.png"))
    assert worlds.clean_views([{"name": v["name"], "jpeg_b64": v["b"]} for v in views])     # the server accepts them

    # ---- check through the panel (it needs the world saved: the check uses its simulation), then the guided tour
    page.evaluate("window.demo.panel.el.style.display = 'block'; window.demo.panel.render()")
    assert page.is_disabled('.wv-panel [data-a="check"]') and page.is_disabled('.wv-panel [data-a="review"]')
    page.click('.wv-panel [data-a="save"]')
    page.wait_for_function("!window.demo.panel.state.busy && !window.demo.wv.world._dirty", timeout=60000)
    assert page.evaluate("window.demo.store.saved.at(-1).mount.yaw_deg") == pytest.approx(-75.0)
    page.click('.wv-panel [data-a="check"]')
    page.wait_for_function("window.demo.panel.state.result && !window.demo.panel.state.busy", timeout=180000)
    assert page.evaluate("window.demo.store.lastViews.length") == 0         # measured only: no pictures go anywhere
    page.click('.wv-panel [data-a="review"]')
    page.wait_for_function("window.demo.panel.state.result && !window.demo.panel.state.busy && window.demo.store.lastViews.length",
                           timeout=180000)
    assert page.evaluate("window.demo.store.lastViews.length") == 6
    assert page.inner_text(".wv-panel .wv-verdict") == inspect["verdict"].replace("_", " ")
    page.click('.wv-panel [data-a="tour"]')
    page.wait_for_selector(".wv-cap.on", timeout=30000)
    step = lambda: page.inner_text(".wv-cap .top b")                    # noqa: E731
    assert step().endswith("/6")
    # read the step and press Next in one go (the tour advances by itself until a button is pressed, then holds)
    n0 = page.evaluate("() => { const n = document.querySelector('.wv-cap .top b').textContent; document.querySelector('.wv-cap [data-k=\"next\"]').click(); return n; }")
    n1 = int(n0.split("/")[0]) + 1
    assert step() == f"{n1}/6"
    page.wait_for_timeout(5000)                                           # held: no step on its own any more
    assert step() == f"{n1}/6"
    shot(page, "f5_desk_tour.png")
    page.click('.wv-cap [data-k="prev"]')
    page.wait_for_timeout(300)
    assert step() == n0
    page.click('.wv-cap [data-k="stop"]')
    assert not page.evaluate("document.querySelector('.wv-cap').classList.contains('on')")
    with open(os.path.join(SHOTS, "f5_fps.json"), "w", encoding="utf-8") as fh:
        json.dump({"walk_fps": fps, "page_fps": page.evaluate("window.demo.fps"), "renderer": "SwiftShader (software)"}, fh)
    assert not errors, errors
    page.close()


def test_glb_room_scale_place_and_boxes(server, browser, tmp_path):
    """A scan that came in at half size: place the robot on the counter, measure the counter with the two-point tool
    and type its true length, then find boxes (the server's answer for the page's world), use the scanned surface,
    keep the boxes and render the six views in the scan."""
    from PIL import ImageStat

    glb = tmp_path / "room.glb"
    counter_scene_glb(glb, noise=0.002)
    info = worlds.check_glb(str(glb))
    draft = {**worlds.draft_from_glb(info, "counter scan.glb"), "mount": {"position": [0, 0, 0], "yaw_deg": -90}, "surface": None,
             "scale": 0.5}
    data = glb.read_bytes()
    page = _page(browser)
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.route("**/room.glb*", lambda r: r.fulfill(body=data, headers={"Content-Type": "model/gltf-binary"}))
    page.goto(f"{server}/tools/world_demo.html", timeout=120000)
    page.wait_for_function("window.ready === true", timeout=120000)
    # as the panel's upload leaves it: an unsaved draft (the panel keeps it however the server's world is)
    page.evaluate("async ([w]) => { await window.demo.wv.setWorld(w, {assetUrl: '/room.glb'}); window.demo.wv.world._dirty = true; "
                  "await window.demo.panel.toggle(true); }", [draft])
    assert page.evaluate("!!window.demo.wv.assetG && window.demo.wv.assetTris") > 500
    # 3-D clicks must reach the canvas: the panel is hidden while clicking in the room
    page.evaluate("window.demo.panel.el.style.display = 'none'; window.demo.wv.flyToView({eye: [0, -950, 750], target: [0, 0, 450]}, 0)")
    page.wait_for_timeout(800)
    # place on the counter: 450 mm high while the scan is at half size
    page.evaluate("window.demo.wv.setMode('place')")
    s = page.evaluate("window.demo.screenOf([0, -50, 450])")
    page.mouse.click(s["x"], s["y"])
    page.wait_for_timeout(600)
    m = page.evaluate("window.demo.wv.world.mount")
    assert abs(m["position"][2] - 450) < 6 and page.evaluate("window.demo.wv.world.surface.half")[0] == 300
    # two-point scale: the points are 500 mm apart in the half-size scan; their true distance is 1000 mm
    page.evaluate("window.demo.wv.setMode('scale')")
    for pt in ([-250, -125, 450], [250, -125, 450]):
        s = page.evaluate("p => window.demo.screenOf(p)", pt)
        page.mouse.click(s["x"], s["y"])
        page.wait_for_timeout(300)
    ref = page.evaluate("window.demo.wv.scaleRef")
    assert ref and abs(ref["measured"] - 500) < 10
    page.evaluate("window.demo.panel.el.style.display = 'block'; window.demo.panel.render()")
    page.fill('.wv-panel [data-f="len"]', "1000")
    page.click('.wv-panel [data-a="scale"]')
    k = 1000 / ref["measured"]
    assert page.evaluate("window.demo.wv.world.scale") == pytest.approx(0.5 * k, rel=1e-3)
    assert page.evaluate("window.demo.wv.world.mount.position[2]") == pytest.approx(m["position"][2] * k, abs=1)
    assert page.evaluate("window.demo.wv.world.scale_ref.length_mm") == pytest.approx(1000, abs=0.2)
    assert page.evaluate("!!window.demo.wv.assetG")                          # the scan survives the rebuild
    # find boxes: the page sends its world, the server boxes the scan (worlds.auto_obstacles, as api_world does)
    raw = page.evaluate("window.demo.panel.worldBody()")
    auto = worlds.auto_obstacles(str(glb), worlds.validate({**raw, "asset": worlds.ASSET}))
    assert auto["obstacles"] and auto["surface"]
    page.evaluate("([a]) => { window.demo.store.auto = a; }", [auto])
    page.click('.wv-panel [data-a="auto"]')
    page.wait_for_selector('.wv-panel [data-a="keep"]', timeout=30000)
    assert page.evaluate("window.demo.store.autoBody.world.scale") == pytest.approx(1.0, rel=2e-3)
    assert page.evaluate("window.demo.wv.proposals.length") == len(auto["obstacles"])
    page.click('.wv-panel [data-a="surface"]')                                # use the scanned counter top
    sf = auto["surface"]
    assert max(page.evaluate("window.demo.wv.world.surface.half")[:2]) > 500          # the whole counter, not 600 x 600
    assert page.evaluate("window.demo.wv.world.mount.position[2]") == pytest.approx(sf["center"][2] + sf["half"][2], abs=0.2)
    page.click('.wv-panel [data-a="keep"]')
    assert page.evaluate("window.demo.wv.world.obstacles.length") == len(auto["obstacles"])
    assert page.evaluate("window.demo.wv.world._dirty") is True
    page.evaluate("window.demo.wv.setMode('orbit'); window.demo.wv.frameOverview(0)")
    page.wait_for_timeout(1500)
    shot(page, "f5_glb_room.png")
    # the six views in the scan: none is blank (an eye inside the scan or behind a wall would be)
    with open(os.path.join(ROOT, "morph/studio/static/fixtures/sims/sim_pick_place.json"), encoding="utf-8") as fh:
        doc = json.load(fh)
    views = page.evaluate("async ([d]) => (await window.demo.wv.snapshotViews(d)).map(v => ({name: v.name, b: v.jpeg_b64}))", [doc])
    assert [v["name"] for v in views] == list(worlds.VIEWS)
    for v in views:
        img = _image(v["b"])
        assert ImageStat.Stat(img.convert("L").crop((0, 40, 1024, 740))).stddev[0] > 8, v["name"]
        img.save(os.path.join(SHOTS, f"f5_scan_{v['name']}.png"))
    assert not errors, errors
    page.close()
