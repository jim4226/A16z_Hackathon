"""A real Morph Studio and a browser page for the end-to-end tests of New robot, Import and Show Morph the job.

- demo_home(): egg-picker and so101 built the way the first `morph studio` builds them (full simulations), plus
  box-picker made from a one-liner (`morph robot new "..." --defaults`): an arm with plain fingers for a small box, so an
  egg or a strawberry is a real change for it. Built once per test session (about a minute) and copied for each test.
- Studio: `morph studio <project>` in a subprocess on such a copy, offline (MORPH_OFFLINE=1, no keys). With a
  `stand_in` file it is the same server with the vision call answered by a local stand-in instead (this file run as a
  script): it records what the Studio sent, and refuses (and records) any connection beyond this computer.
- Page: a browser page at one window size that records script errors, console errors and requests to other sites,
  and finds anything that makes the page or a dialog scroll sideways.

Environment: MORPH_E2E=1 runs these tests; MORPH_E2E_SHOTS=<dir> saves screenshots; MORPH_E2E_WEBGL=1 gives the
browsers WebGL (by default the 3D view says it is unavailable: software WebGL redraws the orbiting robot every frame
and slows every click on a machine without a GPU); MORPH_E2E_PORTS=8812-8819 picks the Studios' ports from a range,
shared out between xdist workers (default: any free port); MORPH_E2E_DEMO=<dir> keeps the built demo robots between
runs; PLAYWRIGHT_CHROMIUM names the Chromium to use.
"""

import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CHROME = os.environ.get("PLAYWRIGHT_CHROMIUM") or "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
SHOTS = os.environ.get("MORPH_E2E_SHOTS")
WEBGL = os.environ.get("MORPH_E2E_WEBGL") == "1"
# what a page may log without failing a test: the browser without WebGL (the 3D view says so)
QUIET = ("WebGL", "THREE.WebGLRenderer")
BOX_REQUEST = "a robot that moves small boxes from one spot to another"


def env(home: str, offline: bool = True) -> dict:
    e = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "KEEWANO_", "SUBQ_", "GEMINI_", "MORPH_"))}
    e.update(MORPH_HOME=home, PYTHONPATH=ROOT, MUJOCO_GL=os.environ.get("MUJOCO_GL") or "osmesa", PYTHONUTF8="1")
    if offline:
        e["MORPH_OFFLINE"] = "1"
    return e


def morph(home: str, *args, timeout=900) -> str:
    r = subprocess.run([sys.executable, "-m", "morph.cli", *args], cwd=ROOT, env=env(home), capture_output=True, text=True,
                       timeout=timeout)
    assert r.returncode == 0, (args, r.stdout[-2000:], r.stderr[-2000:])
    return r.stdout


_TAKEN = set()


def ports() -> list:
    """MORPH_E2E_PORTS ("8812-8819") as the ports this test process may use; [] when it isn't set. Under pytest-xdist
    each worker takes every n-th port of the range (gw0 the first, gw1 the second, ...), so two workers never pick the
    same one."""
    lo_hi = os.environ.get("MORPH_E2E_PORTS", "").strip()
    if not lo_hi:
        return []
    m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", lo_hi)
    if not m or int(m.group(1)) > int(m.group(2)):
        raise ValueError(f'MORPH_E2E_PORTS should read like "8812-8819", not {lo_hi!r}')
    out = list(range(int(m.group(1)), int(m.group(2)) + 1))
    worker, n = os.environ.get("PYTEST_XDIST_WORKER", ""), os.environ.get("PYTEST_XDIST_WORKER_COUNT", "")
    if re.fullmatch(r"gw\d+", worker) and n.isdigit() and int(n) > 1:
        out = out[int(worker[2:]) % int(n)::int(n)]
    return out


def free_port() -> int:
    """A port nobody listens on: from MORPH_E2E_PORTS when set (never one outside it), else one the system picks."""
    mine = ports()
    for p in mine:
        if p in _TAKEN:
            continue
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                _TAKEN.add(p)
                return p
    if mine:
        raise RuntimeError(f"MORPH_E2E_PORTS: no free port left for this test process (it may use {mine}); give it a "
                           "wider range")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def release_port(port: int):
    _TAKEN.discard(port)


def until(fn, timeout=60.0, what="the page", step=0.25):
    t0, last = time.time(), None
    while time.time() - t0 < timeout:
        try:
            v = fn()
        except Exception as exc:  # noqa: BLE001  (a page between two renders)
            v, last = None, exc
        if v:
            return v
        time.sleep(step)
    raise AssertionError(f"timed out after {timeout:.0f} s waiting for {what}" + (f" ({last})" if last else ""))


def demo_home(tmp_path_factory) -> str:
    """The built demo robots and box-picker, made once per session (per worker with xdist) and reused; with
    MORPH_E2E_DEMO=<dir>, kept there between runs (delete the folder to rebuild them)."""
    import pathlib

    keep = os.environ.get("MORPH_E2E_DEMO")
    root = pathlib.Path(keep) if keep else tmp_path_factory.getbasetemp() / "morph-e2e-demo"
    home = root / "home"
    if (root / "built").exists():
        return str(home)
    if home.exists():  # a build that stopped half way
        shutil.rmtree(home)
    home.mkdir(parents=True)
    port = free_port()
    p = subprocess.Popen([sys.executable, "-m", "morph.cli", "studio", "egg-picker", "--port", str(port)], cwd=ROOT,
                         env=env(str(home)), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    out = []
    try:
        for ln in p.stdout:  # builds egg-picker and so101, then serves
            out.append(ln)
            if ln.startswith("Morph Studio:"):
                break
    finally:
        p.terminate()
        p.wait(15)
        release_port(port)
    assert any(ln.startswith("Morph Studio:") for ln in out), "".join(out)[-3000:]
    morph(str(home), "robot", "new", BOX_REQUEST, "--defaults", "--name", "box-picker")
    (root / "built").write_text("egg-picker, so101, box-picker\n")
    return str(home)


class Studio:
    """`morph studio <project>` on `home` (offline), or with `stand_in` (a file for what the vision stand-in records):
    this file run as a script, the same server with the vision call answered locally and network lookups on."""

    def __init__(self, home: str, project: str = "egg-picker", stand_in: str | None = None):
        self.home, self.port, self.stand_in = home, free_port(), stand_in
        if stand_in:
            cmd = [sys.executable, os.path.abspath(__file__), "stand-in", str(self.port), home, project, stand_in]
        else:
            cmd = [sys.executable, "-m", "morph.cli", "studio", project, "--port", str(self.port)]
        self.proc = subprocess.Popen(cmd, cwd=ROOT, env=env(home, offline=not stand_in), stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        self.lines = []
        threading.Thread(target=self._read, daemon=True).start()
        until(lambda: any(ln.startswith("Morph Studio:") for ln in self.lines) or self.proc.poll() is not None, 180,
              "the Studio to start")
        assert self.proc.poll() is None, self.log()[-3000:]
        self.base = f"http://127.0.0.1:{self.port}"

    def _read(self):
        for ln in self.proc.stdout:
            self.lines.append(ln)

    def log(self) -> str:
        return "".join(self.lines)

    def api(self, method: str, path: str, body=None):
        req = urllib.request.Request(self.base + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                     headers={"X-Morph-Client": "1", "Content-Type": "application/json"} if method != "GET" else {})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw, status = r.read(), r.status
        except urllib.error.HTTPError as e:
            raw, status = e.read(), e.code
        try:
            return status, json.loads(raw)
        except ValueError:
            return status, raw

    def projects(self) -> dict:
        status, out = self.api("GET", "/api/projects")
        assert status == 200, out
        return {p["name"]: p for p in out["projects"]}

    def project(self, name: str):
        from morph.robot.pipeline2 import RobotProject
        return RobotProject(os.path.join(self.home, name))

    def latest(self, name: str):
        """(rev, design, summary) of a project's newest revision, read from disk."""
        p = self.project(name)
        rev = p.latest()
        return rev, p.load_design(rev), (p.load_results(rev) or {}).get("summary") or {}

    def calls(self) -> list:
        """What the vision stand-in recorded: {"images": n} per call and {"blocked": address} per refused connection."""
        if not self.stand_in or not os.path.exists(self.stand_in):
            return []
        with open(self.stand_in, encoding="utf-8") as fh:
            return [json.loads(ln) for ln in fh if ln.strip()]

    def tracebacks(self) -> list:
        return re.findall(r"Traceback \(most recent call last\):.*?(?=\n\S|\Z)", self.log(), re.S)

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        release_port(self.port)


def launch(playwright):
    """Chromium for the tests (no WebGL unless MORPH_E2E_WEBGL=1), or None when there is none."""
    exe = CHROME if os.path.exists(CHROME) else None
    args = ["--no-sandbox", "--disable-background-networking"] + ([] if WEBGL else ["--disable-webgl", "--disable-3d-apis"])
    try:
        return playwright.chromium.launch(executable_path=exe, args=args)
    except Exception:  # noqa: BLE001
        return None


# the elements of the open dialog (or the page) that reach past the window's edges, and whether the page scrolls sideways
SIDEWAYS = """() => {
  const root = document.querySelector('.modal.on') || document.body;
  const out = [...root.querySelectorAll('*')].filter((e) => {
    const r = e.getBoundingClientRect();
    if (!r.width || !r.height) return false;
    for (let p = e.parentElement; p && p !== root.parentElement; p = p.parentElement) {  // clipped by a scroller
      const s = getComputedStyle(p);
      if (/(auto|scroll|hidden|clip)/.test(s.overflowX)) {
        const q = p.getBoundingClientRect();
        if (q.right <= innerWidth + 1 && q.left >= -1) return false;
      }
    }
    return r.right > innerWidth + 1 || r.left < -1;
  }).map((e) => `${e.tagName.toLowerCase()}.${String(e.className || '').trim().split(/\\s+/).join('.')}`).slice(0, 8);
  const page = document.scrollingElement.scrollWidth > innerWidth + 1 ? [`page ${document.scrollingElement.scrollWidth} px wide`] : [];
  return page.concat(out);
}"""


class Page:
    """One person's browser page on a Studio, at one window size."""

    def __init__(self, browser, studio: Studio, size=(1600, 900), name="page", quiet=()):
        self.studio, self.name, self.size = studio, name, size
        self.errors, self.outside = [], []
        self.ctx = browser.new_context(viewport={"width": size[0], "height": size[1]}, accept_downloads=True)
        self.pg = self.ctx.new_page()
        self.pg.set_default_timeout(60_000)
        quiet = QUIET + tuple(quiet)
        self.pg.on("pageerror", lambda e: self.errors.append(f"{name}: {e}"))
        self.pg.on("console", lambda m: self.errors.append(f"{name}: {m.text}")
                   if m.type == "error" and not any(q in m.text for q in quiet) else None)
        host = f"127.0.0.1:{studio.port}"
        self.pg.on("request", lambda r: None if r.url.startswith(("data:", "blob:")) or r.url.split("/")[2:3] == [host]
                   else self.outside.append(r.url))

    def open(self, query: str = ""):
        self.pg.goto(f"{self.studio.base}/?tour=0{'&' + query if query else ''}")
        until(lambda: self.ev("window.morph?.state?.loaded === true && !!window.morph.state.current"), 120,
              f"{self.name}'s Studio page to load")

    def ev(self, js: str, arg=None):
        """An expression, or a function of `arg`."""
        return self.pg.evaluate(js, arg)

    def text(self, sel: str) -> str:
        loc = self.pg.locator(sel)
        return " ".join(loc.first.inner_text().split()) if loc.count() else ""

    def texts(self, sel: str) -> list:
        return [" ".join(t.split()) for t in self.pg.locator(sel).all_inner_texts()]

    def sideways(self) -> list:
        return self.ev(SIDEWAYS)

    def fits(self, what: str):
        bad = self.sideways()
        assert not bad, f"{what} at {self.size[0]}x{self.size[1]}: {bad}"

    def shot(self, name: str):
        if not SHOTS:
            return
        os.makedirs(SHOTS, exist_ok=True)
        try:
            self.pg.screenshot(path=os.path.join(SHOTS, f"{name}.png"), timeout=60_000)
        except Exception:  # noqa: BLE001  (a screenshot never fails a test)
            pass

    def project(self) -> str:
        return self.ev("window.morph.state.project")

    def rev(self):
        return self.ev("window.morph.state.current && window.morph.state.current.rev")

    def idle(self, timeout=240):
        until(lambda: not self.ev("window.morph.state.busy"), timeout, f"{self.name}'s project to finish building")

    def chat(self, text: str) -> int:
        n = self.ev("document.querySelectorAll('#messages .msg').length")
        self.pg.fill("#input", text)
        self.pg.press("#input", "Enter")
        return n

    def scrolled(self):
        """Wait for the chat's smooth scroll to reach the newest message."""
        until(lambda: self.ev("(() => { const m = document.querySelector('#messages'); "
                              "return m.scrollHeight - m.clientHeight - m.scrollTop < 4; })()"), 10, "the chat to scroll down")

    def replies(self, since: int) -> list:
        return self.ev("(n) => [...document.querySelectorAll('#messages .msg')].slice(n).filter((m) => m.classList.contains('assistant'))"
                       ".map((m) => m.innerText.replace(/\\s+/g, ' ').trim())", since)

    def check(self):
        assert not self.errors, self.errors
        assert not self.outside, f"the page loaded something from another site: {self.outside}"

    def close(self):
        self.ctx.close()


# ------------------------------------------------------------------ the vision stand-in (this file run as a script)
SEEN = {  # what the stand-in says a clip shows: strawberries moved from a tray into a punnet, the layout unsure
    "usable": True, "unusable_reason": None,
    "task": {"type": "pick_place", "summary": "Moves strawberries from a tray into a punnet.", "confidence": 0.85,
             "evidence_frames": [2, 5]},
    "object": {"name": "strawberry", "shape": "sphere", "diameter_mm": 31, "length_mm": 36, "size_range_mm": [26, 36],
               "scale_reference": "hand", "mass_g": 22, "fragility": "very_delicate", "surface": "dry", "count_per_cycle": 1,
               "confidence": 0.8, "evidence_frames": [3]},
    "motion": {"pick": {"x_mm": 240, "y_mm": -110, "z_mm": 0}, "place": {"x_mm": 180, "y_mm": 150, "z_mm": 0},
               "positions_vary": False, "confidence": 0.5, "evidence_frames": [2, 6]},
    "timing": {"cycle_time_s": 5.5, "cycles_seen": 3, "confidence": 0.7, "evidence_frames": [1, 8]},
    "environment": {"setting": "food", "clutter": "low", "confidence": 0.9, "evidence_frames": [1]},
    "safety": {"other_people_visible": 0, "hands_share_workspace": False, "hazards": [], "confidence": 0.9,
               "evidence_frames": [1]},
    "questions": ["Are the punnets always in the same place?"]}


def _stand_in(port: int, home: str, project: str, record: str):
    import types

    lock = threading.Lock()

    def note(entry):
        with lock, open(record, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")

    def loopback(address) -> bool:
        if not isinstance(address, tuple):
            return True
        host = str(address[0]).strip("[]").split("%")[0].lower()
        try:
            return host in ("localhost", "") or ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    connect, create_connection = socket.socket.connect, socket.create_connection

    def guarded_connect(self, address):
        if not loopback(address):
            note({"blocked": repr(address)})
            raise ConnectionRefusedError(f"the stand-in Studio reaches nothing beyond this computer ({address!r})")
        return connect(self, address)

    def guarded_create_connection(address, *a, **k):
        if not loopback(address):
            note({"blocked": repr(address)})
            raise ConnectionRefusedError(f"the stand-in Studio reaches nothing beyond this computer ({address!r})")
        return create_connection(address, *a, **k)

    socket.socket.connect, socket.create_connection = guarded_connect, guarded_create_connection
    sys.path.insert(0, ROOT)
    from morph.robot.pipeline2 import RobotProject
    from morph.studio import server, vision

    class Claude:
        def __init__(self):
            self.messages = self

        def create(self, **kw):
            content = (kw.get("messages") or [{}])[0].get("content") or []
            note({"images": sum(1 for b in content if isinstance(b, dict) and b.get("type") == "image")})
            time.sleep(1.0)
            return types.SimpleNamespace(stop_reason="end_turn", content=[types.SimpleNamespace(type="text", text=json.dumps(SEEN))],
                                         usage=types.SimpleNamespace(input_tokens=9000, output_tokens=1500))

    vision.claude_ready = lambda: True
    vision._claude_client = Claude
    vision.subq = None
    print("vision stand-in: a strawberry clip, the layout at 0.5 confidence; nothing leaves this computer", flush=True)
    server.serve(RobotProject(os.path.join(home, project)), "127.0.0.1", port, home=home)


if __name__ == "__main__" and len(sys.argv) == 6 and sys.argv[1] == "stand-in":
    _stand_in(int(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5])
