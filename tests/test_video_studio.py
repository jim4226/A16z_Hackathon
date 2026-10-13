"""Show Morph the job inside the real Studio: Wave 0's router (feature routes found at start), rooms, job queue and
event stream, over HTTP. Skipped until Wave 0 (morph/studio/routes.py and rooms.py) is merged. Nothing leaves the
computer: no keys, network lookups off, and Claude is a local fake.
"""

import base64
import http.client
import io
import json
import pathlib
import re
import shutil
import subprocess
import threading
import time
import types
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
from PIL import Image

pytest.importorskip("morph.studio.routes")
pytest.importorskip("morph.studio.rooms")

from morph.robot.pipeline2 import RobotProject  # noqa: E402
from morph.spec import Design  # noqa: E402
from morph.studio import api_video, routes, server, vision  # noqa: E402
from test_video import FakeClaude  # noqa: E402


def frames(n=4):
    out = []
    for i in range(n):
        b = io.BytesIO()
        Image.new("RGB", (640, 360), (30 * i, 90, 160)).save(b, "JPEG", quality=80)
        out.append({"t_s": 0.5 + i, "jpeg_b64": base64.b64encode(b.getvalue()).decode()})
    return out


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "SUBQ_API_KEY", "MORPH_FFMPEG"):
        mp.delenv(k, raising=False)
    mp.setenv("MORPH_OFFLINE", "1")
    mp.setattr(vision, "subq", None)
    home = tmp_path_factory.mktemp("studio")
    p = RobotProject(str(home / "eggs"))
    first = p.run(Design(), n_trials=1, verbose=False, fast_sim=True)
    studio, httpd = server.Studio(p, str(home)), None
    for port in range(8800, 8900):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", port), server.make_handler(studio, {f"127.0.0.1:{port}"}, None))
            break
        except OSError:
            continue
    assert httpd, "no free port in 8800-8899"
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url, events = f"http://127.0.0.1:{httpd.server_address[1]}", []

    def listen():
        try:
            with urllib.request.urlopen(url + "/api/events?project=eggs", timeout=600) as r:
                for line in r:
                    if line.startswith(b"data: "):
                        events.append(json.loads(line[6:]))
        except OSError:
            pass
    threading.Thread(target=listen, daemon=True).start()
    time.sleep(0.3)
    yield types.SimpleNamespace(url=url, p=p, home=str(home), first=first, events=events, mp=mp)
    httpd.shutdown()
    httpd.server_close()
    mp.undo()


def call(live, method, path, body=None, raw=None, headers=None):
    data = raw if raw is not None else json.dumps(body).encode() if body is not None else None
    h = {"X-Morph-Client": "1", **({"Content-Type": "application/json"} if body is not None else {}), **(headers or {})}
    rq = urllib.request.Request(live.url + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(rq, timeout=180) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_the_studio_loads_the_video_routes_at_start():
    for method, path in (("GET", "/api/video/info"), ("POST", "/api/video/analyze"), ("PUT", "/api/video/upload"),
                         ("POST", "/api/video/apply")):
        r = routes.find(method, path)
        assert r is not None and r.fn.__module__ == api_video.__name__, (method, path)
    up = routes.find("PUT", "/api/video/upload")
    assert up.raw_body and up.max_body == api_video.MAX_UPLOAD and up.role == "editor"
    assert routes.find("POST", "/api/video/analyze").max_body == api_video.MAX_BODY
    assert routes.find("GET", "/api/video/info").role == "viewer"


def test_the_router_refuses_before_anything_is_read(live):
    assert call(live, "POST", "/api/video/analyze?project=nope", {"frames": frames(1)})[0] == 404
    assert call(live, "PUT", "/api/video/upload?project=nope", raw=b"\x1aE\xdf\xa3" + b"x" * 100)[0] == 404
    c = http.client.HTTPConnection("127.0.0.1", int(live.url.rsplit(":", 1)[1]), timeout=10)
    c.putrequest("POST", "/api/video/analyze?project=eggs")
    for k, v in (("X-Morph-Client", "1"), ("Content-Type", "application/json"), ("Content-Length", str(17 << 20))):
        c.putheader(k, v)
    c.endheaders()
    assert c.getresponse().status == 413
    c.close()
    rq = urllib.request.Request(live.url + "/api/video/apply?project=eggs", data=b"{}", method="POST",
                                headers={"Content-Type": "application/json"})  # no X-Morph-Client: another site's page
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(rq, timeout=10)
    assert e.value.code == 403


def test_analyze_upload_and_apply_over_http(live):
    st, info = call(live, "GET", "/api/video/info?project=eggs")
    assert st == 200 and info["claude"] is False and info["manual_reason"]
    st, out = call(live, "POST", "/api/video/analyze?project=eggs", {"id": "studio0001", "frames": frames()})
    assert st == 200 and out["mode"] == "manual" and out["result"] is None
    # with a (local, fake) Claude: progress reaches the project's event stream under the request's id
    live.mp.setattr(vision, "claude_ready", lambda: True)
    live.mp.setattr(vision, "_claude_client", lambda: FakeClaude(delay=0.3))
    st, out = call(live, "POST", "/api/video/analyze?project=eggs",
                   {"id": "studio0002", "frames": frames(), "marks": {"grasp_s": 1.5, "release_s": 2.5}})
    assert st == 200 and out["result"]["usable"] and out["mapping"]["changes"]["object.name"] == "raw egg"
    time.sleep(0.5)
    mine = [e for e in live.events if e.get("id") == "studio0002"]
    assert any(e["type"] == "agent" and e["role"] == "vision" and e["status"] == "done" for e in mine)
    assert any(e["type"] == "video" and e["stage"] == "done" for e in mine) and all(e["project"] == "eggs" for e in mine)
    ledger = [json.loads(x) for x in open(live.p.ledger_path, encoding="utf-8")]
    assert any(e.get("type") == "video_analysis" and e.get("source") == "claude" for e in ledger)
    # the ffmpeg fallback: streamed in through the router, frames out, the video deleted
    ff = shutil.which("ffmpeg")
    clip = str(pathlib.Path(live.home) / "clip.webm")
    if ff and subprocess.run([ff, "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=3:size=320x240:rate=15", "-c:v",
                              "libvpx-vp9", "-b:v", "200k", clip], capture_output=True).returncode == 0:
        with open(clip, "rb") as fh:
            data = fh.read()
        st, up = call(live, "PUT", "/api/video/upload?project=eggs&id=studio0003&count=4", raw=data,
                      headers={"Content-Type": "application/octet-stream"})
        assert st == 200 and len(up["frames"]) == 4 and up["decoder"] == "ffmpeg"
        assert not list((pathlib.Path(live.home) / ".uploads").glob("video-*"))
    # apply: queued in the room like a parameter edit, built, and said in the chat
    st, ap = call(live, "POST", "/api/video/apply?project=eggs",
                  {"changes": out["mapping"]["changes"], "label": "video: raw egg", "base_rev": live.first})
    assert st == 202 and ap["job"]["id"] and ap["job"]["base_rev"] == live.first
    t0 = time.time()
    while time.time() - t0 < 300 and (call(live, "GET", "/api/jobs?project=eggs")[1]["jobs"] or live.p.latest() == live.first):
        time.sleep(0.5)
    rev = live.p.latest()
    d = live.p.load_design(rev).to_dict()
    assert rev != live.first and d["object"]["name"] == "raw egg" and d["object"]["max_force_n"] == 25
    assert d["task"]["pick_x_mm"] == 230 and d["task"]["place_y_mm"] == 160
    said = [e for e in live.events if e.get("type") == "assistant" and e.get("source") == "video"]
    assert said and said[-1]["text"].startswith("From the video:")


def test_the_studio_page_lets_the_browser_play_the_clip():
    """The frame picker plays the chosen clip from a blob: URL in a <video>; without media-src the page's policy
    falls back to default-src 'self' and the browser refuses it (see test_video_browser)."""
    assert re.search(r"media-src[^;]*\bblob:", server.app_csp()), "add media-src 'self' blob: to app_csp()"
