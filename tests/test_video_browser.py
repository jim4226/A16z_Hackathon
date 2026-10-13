"""Show Morph the job in a real browser: static/video.js on tools/video_demo.html, served with the Studio's CSP by a small
stand-in for the router that answers /api/video/* with the real handlers (Claude and SubQ faked) and streams events.

Needs Playwright with Chromium and ffmpeg (for the test clips); skipped otherwise. This Chromium build has no H.264,
which makes the H.264 clip a real "this browser can't play it" case. Screenshots go to MORPH_SHOTS (default
/tmp/claude-0/shots) as f3_*.png.
"""

import base64
import hashlib
import io
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest
from PIL import Image

from morph.spec import Design
from morph.studio import api_video, vision

from test_video import AGREE, FakeClaude, egg, fake_subq

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHOTS = os.environ.get("MORPH_SHOTS", "/tmp/claude-0/shots")
CHROME = os.environ.get("MORPH_CHROME", "/opt/pw-browsers/chromium-1194/chrome-linux/chrome")
pw = pytest.importorskip("playwright.sync_api")
if not shutil.which("ffmpeg"):
    pytest.skip("ffmpeg is needed to make the test clips", allow_module_level=True)
if not os.path.exists(CHROME):
    pytest.skip("no Chromium for Playwright", allow_module_level=True)

TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css", ".json": "application/json",
         ".png": "image/png", ".svg": "image/svg+xml", ".woff2": "font/woff2"}


# ------------------------------------------------------------------ media
def _ffmpeg(*args, timeout=180):
    r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *map(str, args)], capture_output=True, timeout=timeout)
    assert r.returncode == 0, r.stderr.decode()[-400:]


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    """A 6 s clip of an egg moving from a tray to a carton across a table: VP9 (plays here) and H.264 (doesn't); a photo
    carrying EXIF with GPS."""
    d = tmp_path_factory.mktemp("media")
    scene = ("[0]drawbox=x=120:y=380:w=330:h=230:color=0x3a3f4b:t=fill,drawbox=x=800:y=120:w=330:h=220:color=0xc9b38c:t=fill,"
             "drawbox=x=0:y=640:w=1280:h=80:color=0x4a3a2c:t=fill[b];[1]format=yuva420p,geq=lum='p(X,Y)':a='255*lte(hypot(X-45,Y-55),45)'[e];"
             "[b][e]overlay=x='230+110*t':y='450-55*t':shortest=1")
    src = ["-f", "lavfi", "-i", "color=c=0x6b5844:s=1280x720:d=6:r=30", "-f", "lavfi", "-i", "color=c=0xf2efe6:s=90x110:d=6:r=30",
           "-filter_complex", scene]
    _ffmpeg(*src, "-c:v", "libvpx-vp9", "-b:v", "900k", "-deadline", "realtime", "-cpu-used", "8", d / "egg.webm")
    _ffmpeg(*src, "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", d / "egg.mp4")
    rotated = None
    try:  # a phone held upright: AV1 (this Chromium plays it) with a 90 degree display matrix
        _ffmpeg("-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=2", "-c:v", "libaom-av1", "-cpu-used", "8",
                "-row-mt", "1", "-b:v", "300k", d / "av1.mp4")
        _ffmpeg("-display_rotation", "90", "-i", d / "av1.mp4", "-c", "copy", d / "av1_rot.mp4")
        rotated = d / "av1_rot.mp4"
    except AssertionError:
        pass
    im = Image.new("RGB", (1600, 1200), (196, 170, 120))
    from PIL import ImageDraw
    ImageDraw.Draw(im).ellipse((640, 380, 960, 820), fill=(242, 239, 230))
    ImageDraw.Draw(im).rectangle((200, 900, 628, 1170), fill=(240, 240, 235))  # a card, 85.6 mm wide, for the measuring tool
    exif = Image.Exif()
    exif[0x010F] = "PhoneMaker"
    exif.get_ifd(0x8825).update({1: "N", 2: (40.0, 26.0, 46.0), 3: "W", 4: (79.0, 58.0, 56.0)})
    im.save(d / "egg.jpg", "JPEG", exif=exif)
    return {"webm": d / "egg.webm", "mp4": d / "egg.mp4", "jpg": d / "egg.jpg", "rot": rotated}


# ------------------------------------------------------------------ a stand-in for the Studio's router
class Hub:
    def __init__(self):
        self.queues, self.sent = [], []

    def send(self, ev):
        self.sent.append(ev)
        for q in list(self.queues):
            q.put(ev)


class DemoProject:
    def __init__(self, root):
        self.root, self.design, self.ledger = str(root), Design(), []  # the default design, so an egg is a change

    def latest(self):
        return "r001"

    def load_design(self, rev):
        return self.design

    def log(self, entry):
        self.ledger.append(entry)


class DemoStudio:
    def __init__(self, home):
        self.home, self.hub, self.p, self.busy, self.remote = str(home), Hub(), DemoProject(os.path.join(home, "egg-picker")), threading.Lock(), False
        self.applied, self.bodies = [], []

    def apply(self, changes, label, user_text=None, reply=None, source=""):
        self.applied.append({"changes": changes, "label": label, "reply": reply, "source": source})
        self.hub.send({"type": "assistant", "text": reply, "changes": changes, "source": source, "project": "egg-picker"})


def csp(page: str, media_blob: bool = True) -> str:
    """The Studio page's policy (server.app_csp) with the inline scripts allowed by hash, plus media-src blob: when on."""
    hashes = " ".join("'sha256-" + base64.b64encode(hashlib.sha256(s.encode()).digest()).decode() + "'"
                      for s in re.findall(r"<script[^>]*>(.*?)</script>", page, re.S) if s.strip())
    return (f"default-src 'self'; script-src 'self' {hashes}; style-src 'self' 'unsafe-inline'; font-src 'self' data:; "
            "img-src 'self' data: blob:; connect-src 'self' data: blob:; worker-src 'self' blob:; "
            + ("media-src 'self' blob:; " if media_blob else "") + "object-src 'none'; base-uri 'none'; frame-ancestors 'none'")


def make_handler(studio, stop):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, status, body: bytes, ctype="application/json", headers=()):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in headers:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _route(self, method):
            u = urlparse(self.path)
            r = next((r for r in api_video.ROUTES if r["method"] == method and r["path"] == u.path), None)
            if r is None:
                return False
            body = None
            if not r.get("raw_body"):
                n = int(self.headers.get("Content-Length") or 0)
                if n > r.get("max_body", 1 << 20):
                    self._send(413, b'{"ok":false,"error":"too large"}')
                    return True
                body = json.loads(self.rfile.read(n) or b"{}") if n else {}
                if u.path == "/api/video/analyze":
                    studio.bodies.append(body)
            req = types.SimpleNamespace(method=method, path=u.path, query=parse_qs(u.query), headers=self.headers, body=body,
                                        studio=studio, room=None, member=None, handler=self)
            status, payload = r["handler"](req)[:2]
            self._send(status, json.dumps(payload).encode())
            return True

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/api/events":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                q = queue.Queue()
                studio.hub.queues.append(q)
                try:
                    while not stop.is_set():
                        try:
                            self.wfile.write(f"data: {json.dumps(q.get(timeout=0.5))}\n\n".encode())
                        except queue.Empty:
                            self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
                finally:
                    studio.hub.queues.remove(q)
                return
            if self._route("GET"):
                return
            rel = u.path.lstrip("/")
            full = os.path.realpath(os.path.join(ROOT, rel))
            allowed = [os.path.realpath(os.path.join(ROOT, p)) for p in ("tools", os.path.join("morph", "studio", "static"))]
            if not any(os.path.commonpath([full, a]) == a for a in allowed) or not os.path.isfile(full):
                return self._send(404, b"not found", "text/plain")
            with open(full, "rb") as fh:
                data = fh.read()
            ext = os.path.splitext(full)[1]
            extra = [("Content-Security-Policy", csp(data.decode("utf-8"), "csp=app" not in u.query))] if ext == ".html" else []
            self._send(200, data, TYPES.get(ext, "application/octet-stream"), extra)

        def do_POST(self):
            if not self._route("POST"):
                self._send(404, b"not found", "text/plain")

        def do_PUT(self):
            if not self._route("PUT"):
                self._send(404, b"not found", "text/plain")
    return H


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    home = tmp_path_factory.mktemp("home")
    studio, stop = DemoStudio(str(home)), threading.Event()
    httpd = None
    for port in range(8800, 8900):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(studio, stop))
            break
        except OSError:
            continue
    assert httpd, "no free port in 8800-8899"
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield studio, f"http://127.0.0.1:{httpd.server_address[1]}"
    stop.set()
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture(scope="module")
def fakes():
    """Claude answers like the API after a second (the layout at 0.5 confidence, so it becomes a question); SubQ agrees."""
    mp = pytest.MonkeyPatch()
    for k in ("ANTHROPIC_AUTH_TOKEN", "MORPH_OFFLINE", "MORPH_FFMPEG"):
        mp.delenv(k, raising=False)
    mp.setenv("ANTHROPIC_API_KEY", "test-not-a-key")
    mp.setattr(vision, "claude_ready", lambda: True)  # a conftest that clears keys before every test leaves the fake on
    mp.setattr(vision, "_claude_client", lambda: FakeClaude(payload=egg(motion__confidence=0.5), delay=1.0))
    mp.setattr(vision, "subq", fake_subq(AGREE, delay=0.3))
    yield mp
    mp.undo()


@pytest.fixture(scope="module")
def browser():
    with pw.sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME, headless=True, args=["--no-sandbox"])
        yield b
        b.close()


def page_for(browser, base, query=""):
    ctx = browser.new_context(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{base}/tools/video_demo.html{query}")
    page.wait_for_selector("html[data-ready='1']")
    page.errors = errors
    return page


def wait_js(page, expr, timeout=10000):
    """page.wait_for_function compiles its predicate with new Function, which the Studio's CSP (rightly) refuses; poll
    through evaluate instead, which the policy doesn't apply to."""
    t0 = time.time()
    while not page.evaluate(expr):
        if (time.time() - t0) * 1000 > timeout:
            raise AssertionError(f"timed out waiting for {expr}")
        time.sleep(0.1)


def shot(page, name):
    os.makedirs(SHOTS, exist_ok=True)
    page.screenshot(path=os.path.join(SHOTS, f"f3_{name}.png"))


# ------------------------------------------------------------------ tests
def test_frames_are_picked_in_the_browser(server, browser, media, fakes):
    studio, base = server
    page = page_for(browser, base)
    page.set_input_files("#probe", str(media["webm"]))
    r = page.evaluate("demo.extract()")
    assert r["ok"] and r["n"] == 8 and r["ms"] < 5000, r
    assert all(max(w, h) <= 1280 for w, h in r["sizes"]) and r["times"] == sorted(r["times"]) and r["times"][-1] < 6.0
    page.set_input_files("#probe", str(media["mp4"]))  # this Chromium has no H.264: a real unplayable clip
    r = page.evaluate("demo.extract()")
    assert not r["ok"] and r["name"] == "UnplayableError" and r["ms"] < 1000, r
    page.set_input_files("#probe", str(media["jpg"]))
    r = page.evaluate("demo.extract()")
    assert r["ok"] and r["kind"] == "photo" and r["n"] == 1 and r["sizes"] == [[1600, 1200]] and r["exif"] is False, r
    if media["rot"]:
        page.set_input_files("#probe", str(media["rot"]))
        r = page.evaluate("demo.extract('#probe', {count: 2})")
        assert r["ok"] and r["sizes"] == [[360, 640], [360, 640]], r  # upright, as the phone showed it
    assert not page.errors


def test_the_studio_page_policy_needs_media_blob(server, browser, media, fakes):
    """Today's Studio CSP has no media-src, so a <video> can't read a picked file: the page must add media-src 'self' blob:."""
    studio, base = server
    page = page_for(browser, base, "?csp=app")
    page.set_input_files("#probe", str(media["webm"]))
    r = page.evaluate("demo.extract()")
    assert not r["ok"] and r["name"] == "UnplayableError", r


def test_show_morph_the_job_end_to_end(server, browser, media, fakes):
    studio, base = server
    page = page_for(browser, base)
    page.click(".vj-btn")
    page.wait_for_selector(".vj-consent")
    assert vision_copy("consent") in page.inner_text(".vj-consent")
    shot(page, "consent")
    page.click("text=Analyze with Claude")
    page.wait_for_selector(".vj-pickbig")
    page.set_input_files(".vj-file", str(media["webm"]))
    page.wait_for_selector(".vj-th >> nth=7")
    assert page.locator(".vj-th:not(.add)").count() == 8
    shot(page, "picker")
    # marks, a crop, one frame out and one moment in
    page.click(".vj-th >> nth=2")
    page.click("text=Mark grasp")
    page.click(".vj-th >> nth=5")
    page.click("text=Mark release")
    page.click("[data-a='crop']")
    box = page.locator(".vj-stage .vj-ov").bounding_box()
    page.mouse.move(box["x"] + box["width"] * 0.08, box["y"] + box["height"] * 0.1)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] * 0.92, box["y"] + box["height"] * 0.95, steps=6)
    page.mouse.up()
    page.wait_for_selector(".vj-crop")
    page.click(".vj-th >> nth=7 >> .x")
    assert page.locator(".vj-th:not(.add)").count() == 7
    page.locator(".vj-scrub input").fill("4.6")
    page.wait_for_timeout(300)
    page.click("[data-a='addmoment']")
    wait_js(page, "document.querySelectorAll('.vj-th:not(.add)').length === 8")
    marks = page.inner_text(".vj-marks")
    assert "Grasp" in marks and "release" in marks and "carry" in marks
    page.click(".vj-subq .switch")
    page.wait_for_selector(".vj-subqnote")
    assert "Second opinion (beta): also sends 4 frames to SubQ." in page.inner_text(".vj-subqnote")
    shot(page, "picker_marks")
    page.click("[data-a='analyze']")
    page.wait_for_selector(".vj-prog .vj-row[data-k='agent-claude']")
    shot(page, "analyzing")
    page.wait_for_selector(".vj-cards", timeout=20000)
    body = studio.bodies[-1]
    assert len(body["frames"]) == 8 and body["subq"] is True and "crop" not in body and body["kind"] == "video"
    assert body["marks"]["grasp_s"] < body["marks"]["release_s"]
    w, h = Image.open(io.BytesIO(base64.b64decode(body["frames"][0]["jpeg_b64"]))).size
    assert 1000 < w < 1200 and 540 < h < 640  # cropped here in the browser: the rest of the picture never left it
    assert vision_copy("results") in page.inner_text(".vj-honest")
    assert "Claude (AI) + SubQ Snap (AI)" in page.inner_text(".dlg-h .s")
    page.wait_for_selector(".vj-qs")
    assert "Use the pick and place spots" in page.inner_text(".vj-qs")
    shot(page, "results")
    page.click(".vj-chip:has-text('Use them')")
    wait_js(page, "!document.querySelector('.vj-qs') || !document.querySelector('.vj-qs').innerText.includes('Use the pick')")
    page.fill("[data-path='object.mass_g']", "65")
    dot = page.locator(".vj-table [data-pt='place']").bounding_box()
    page.mouse.move(dot["x"] + dot["width"] / 2, dot["y"] + dot["height"] / 2)
    page.mouse.down()
    page.mouse.move(dot["x"] + dot["width"] / 2 - 30, dot["y"] + dot["height"] / 2 - 10, steps=5)
    page.mouse.up()
    page.wait_for_timeout(600)
    wait_js(page, "document.querySelector('[data-a=review]') && !document.querySelector('[data-a=review]').disabled")
    shot(page, "results_answered")
    page.click("[data-a='review']")
    page.wait_for_selector(".vj-items")
    rows = page.inner_text(".vj-items")
    assert "Damage force" in rows and "raw egg" in rows and "Object mass" in rows
    shot(page, "review")
    page.click("[data-a='apply']")
    page.wait_for_selector(".vjob", state="hidden", timeout=10000)
    for _ in range(50):
        if studio.applied:
            break
        time.sleep(0.05)
    ch = studio.applied[-1]["changes"]
    assert ch["object.name"] == "raw egg" and ch["object.max_force_n"] == 25 and ch["object.friction"] == 0.35
    assert ch["object.mass_g"] == 65 and "task.place_x_mm" in ch and studio.applied[-1]["source"] == "video"
    assert not page.errors, page.errors


def test_unplayable_clip_goes_to_the_studio_computer(server, browser, media, fakes):
    studio, base = server
    page = page_for(browser, base)
    page.evaluate("localStorage.setItem('morph.video.consent', 'cloud')")
    page.click(".vj-btn")
    page.set_input_files(".vj-file", str(media["mp4"]))
    page.wait_for_selector("[data-a='upload']")
    assert vision_copy("unplayable") in page.inner_text(".vjob")
    shot(page, "unplayable")
    page.click("[data-a='upload']")
    page.wait_for_selector(".vj-th >> nth=7", timeout=30000)
    assert "extracted on the Studio computer" in page.inner_text(".dlg-h .s")
    assert not os.listdir(os.path.join(studio.home, ".uploads"))  # the uploaded clip is already deleted
    shot(page, "fallback_frames")
    assert not page.errors, page.errors


def test_a_clip_too_big_for_the_studio_is_not_uploaded(server, browser, media, fakes, tmp_path, monkeypatch):
    studio, base = server
    monkeypatch.setattr(api_video, "MAX_UPLOAD", 1 << 20)  # the page reads the limit from /api/video/info
    big = tmp_path / "long.mp4"
    big.write_bytes(media["mp4"].read_bytes() + bytes(3 << 19))
    page = page_for(browser, base)
    page.evaluate("localStorage.setItem('morph.video.consent', 'cloud')")
    page.click(".vj-btn")
    page.set_input_files(".vj-file", str(big))
    page.wait_for_selector("text=the Studio computer takes videos up to 1 MB")
    assert page.locator("[data-a='upload']").count() == 0 and vision_copy("unplayable") in page.inner_text(".vjob")
    assert not page.errors, page.errors


def test_keep_on_this_computer_and_measure(server, browser, media, fakes):
    studio, base = server
    page = page_for(browser, base)
    page.click(".vj-btn")
    page.wait_for_selector(".vj-consent")
    page.click("text=Keep on this computer")
    page.set_input_files(".vj-file", str(media["jpg"]))
    page.wait_for_selector("[data-a='manual']")
    n_before = len(studio.bodies)
    page.click("[data-a='manual']")
    page.wait_for_selector(".vj-cards")
    assert "no frame leaves this browser" in page.inner_text(".vjob")
    page.click("[data-a='measure']")
    page.wait_for_selector(".vj-measure-stage")
    img = page.locator(".vj-measure-stage .vj-ov").bounding_box()
    sx, sy = img["width"] / 1600, img["height"] / 1200
    for x, y in ((200, 1035), (628, 1035), (640, 600), (960, 600)):  # the card's width, then the egg's
        page.mouse.click(img["x"] + x * sx, img["y"] + y * sy)
        page.wait_for_timeout(120)
    text = page.inner_text(".vj-mstep")
    mm = int(re.search(r"(\d+) mm", text).group(1))
    assert 60 <= mm <= 68, text  # 320 px against 428 px for 85.6 mm: 64 mm
    shot(page, "measure")
    page.click("[data-a='muse']")
    page.wait_for_selector(".vj-cards")
    assert page.input_value("[data-path='object.diameter_mm']") == str(mm)
    page.fill("[data-path='object.name']", "tomato")
    wait_js(page, "(demo.api.job.mapping?.changes || {})['object.name'] === 'tomato'")
    ch = page.evaluate("demo.api.job.mapping.changes")
    assert ch["object.diameter_mm"] == mm and ch["object.mass_g"] == 120 and ch["object.max_force_n"] == 8  # the preset, the measured width
    assert "How heavy" not in page.inner_text(".vj-summary")
    shot(page, "manual")
    assert len(studio.bodies) == n_before  # nothing was sent for analysis
    assert not page.errors, page.errors


def test_a_failed_analysis_leaves_the_cards_to_fill_in(server, browser, media, fakes):
    studio, base = server
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(vision, "_claude_client", lambda: FakeClaude(stop="refusal", text=""))
        page = page_for(browser, base)
        page.evaluate("localStorage.setItem('morph.video.consent', 'cloud')")
        page.click(".vj-btn")
        page.set_input_files(".vj-file", str(media["webm"]))
        page.wait_for_selector("[data-a='analyze']")
        page.click("[data-a='analyze']")
        page.wait_for_selector(".vj-cards", timeout=20000)
    assert "Claude declined to analyze these images" in page.inner_text(".kchk.bad")
    assert "Fill in the cards yourself" in page.inner_text(".kchk.bad")
    page.fill("[data-path='object.name']", "egg")
    wait_js(page, "(demo.api.job.mapping?.changes || {})['object.name'] === 'raw egg'")
    assert page.inner_text(".vj-preset").startswith("Morph's raw egg preset")
    shot(page, "failed_fill_in")
    assert not page.errors, page.errors


def test_no_project_hands_the_slots_to_the_brief_engine(server, browser, media, fakes):
    studio, base = server
    page = page_for(browser, base, "?noproject=1")
    page.evaluate("localStorage.setItem('morph.video.consent', 'cloud')")
    page.click(".vj-btn")
    page.set_input_files(".vj-file", str(media["webm"]))
    page.wait_for_selector("[data-a='analyze']")
    page.click("[data-a='analyze']")
    page.wait_for_selector(".vj-cards", timeout=20000)
    wait_js(page, "document.querySelector('[data-a=review]').textContent === 'Use for a new robot'")
    page.click("[data-a='review']")
    slots = page.evaluate("demo.slots[0].slots")
    assert slots["object"]["value"]["key"] == "raw egg" and slots["object"]["source"] == "video" and "layout" not in slots
    # the brief engine can open the dialog without importing video.js
    page.evaluate("window.dispatchEvent(new CustomEvent('morph:video-open'))")
    page.wait_for_selector(".vjob .vj-pickbig")
    assert not page.errors, page.errors


def vision_copy(key):
    """One of video.js's COPY strings (the release memo's exact wording), as the page must show it."""
    js = open(os.path.join(ROOT, "morph", "studio", "static", "video.js"), encoding="utf-8").read()
    return re.search(rf"\n  {key}: (['\"])(.*?)\1,\n", js).group(2)
