"""Agent tools (beta) end to end: Live prices in the real Studio (`morph studio`, the demo robots fully built) and a real
browser, with Claude answered by the local stand-in in tests/fake_claude.py (through ANTHROPIC_BASE_URL, so the Studio
runs its own code unchanged) and nothing reaching beyond this computer: the Studio runs as this file's script mode,
which refuses and records every connection and DNS lookup that isn't to 127.0.0.1, and keeps its .env in the test's
own folder. The distributor call therefore fails offline, and the card has to say so.

- test_live_prices_skip_add_key_and_a_reload: so101's BOM tab starts Live prices; the run pauses for the Mouser key;
  Skip goes on without it. Then egg-picker: Key needed, Add key opening Keys on the Mouser key, a key saved, "Key
  added, continuing...", the Mouser call failing offline, Claude's summary, and the same card after a reload.
- test_a_team_editor_may_skip_and_a_viewer_is_told_to_ask_the_owner: a team Studio (`morph team init`, then
  `morph studio --team`); the owner starts Live prices; the editor may skip; the viewer is told to ask the owner and
  can't act through the API either; the owner adds the key and everyone's card continues.

MORPH_E2E=1 runs these (the demo robots are built once per session, a few minutes; MORPH_E2E_DEMO=<dir> keeps them).
MORPH_E2E_PORTS=8844-8849 picks the ports (Studios and the stand-in), MORPH_E2E_SHOTS=<dir> saves screenshots
(t2e_*.png), PLAYWRIGHT_CHROMIUM names the Chromium to use."""

import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
for d in (HERE, os.path.dirname(HERE)):  # live_studio (tests/e2e) and fake_claude (tests)
    if d not in sys.path:
        sys.path.insert(0, d)

from live_studio import ROOT, Page, demo_home, free_port, launch, release_port, until  # noqa: E402

pytestmark = pytest.mark.skipif(os.environ.get("MORPH_E2E") != "1",
                                reason="end-to-end: MORPH_E2E=1 runs a real Studio and a browser (minutes)")

MOUSER_KEY = "e2e0b1c2-d3e4-4f56-8a9b-0c1d2e3f4a5b"  # the shape of a Mouser key; no service has ever seen it
ANTHROPIC_KEY = "sk-ant-e2e-not-a-real-key"


# ------------------------------------------------------------------ the Studio, offline but for the stand-in Claude
def env(home: str, claude_url: str) -> dict:
    e = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "KEEWANO_", "SUBQ_", "GEMINI_", "MORPH_"))
         and k.lower() not in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")}
    e.update(MORPH_HOME=home, PYTHONPATH=ROOT, MUJOCO_GL=os.environ.get("MUJOCO_GL") or "osmesa", PYTHONUTF8="1",
             PYTHONUNBUFFERED="1", ANTHROPIC_API_KEY=ANTHROPIC_KEY, ANTHROPIC_BASE_URL=claude_url)
    return e


class ToolsStudio:
    """`morph studio <project> [--team]` through this file's script mode (see `_offline_morph`)."""

    def __init__(self, home: str, claude_url: str, project: str = "egg-picker", team: bool = False):
        self.home, self.port, self.claude_url = home, free_port(), claude_url
        self.env_file, self.blocked = os.path.join(os.path.dirname(home), "studio.env"), os.path.join(os.path.dirname(home), "blocked.jsonl")
        cmd = [sys.executable, os.path.abspath(__file__), "offline-morph", home, self.env_file, self.blocked,
               "studio", project, "--port", str(self.port)] + (["--team"] if team else [])
        self.proc = subprocess.Popen(cmd, cwd=ROOT, env=env(home, claude_url), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, encoding="utf-8", errors="replace")
        self.lines = []
        threading.Thread(target=lambda: [self.lines.append(ln) for ln in self.proc.stdout], daemon=True).start()
        until(lambda: any(ln.startswith("Morph Studio") for ln in self.lines) or self.proc.poll() is not None, 180,
              "the Studio to start")
        assert self.proc.poll() is None, self.log()[-3000:]
        self.base = f"http://127.0.0.1:{self.port}"
        self.link = until(lambda: next((m.group(1) for ln in list(self.lines) for m in [re.search(r"(http://\S+/\?link=ml_[\w-]+)", ln)] if m), None),
                          30, "the owner's sign-in link") if team else None

    def log(self) -> str:
        return "".join(self.lines)

    def blocked_hosts(self) -> set:
        if not os.path.exists(self.blocked):
            return set()
        with open(self.blocked, encoding="utf-8") as fh:
            return {json.loads(ln)["host"] for ln in fh if ln.strip()}

    def tracebacks(self) -> list:
        return re.findall(r"Traceback \(most recent call last\):.*?(?=\n\S|\Z)", self.log(), re.S)

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        release_port(self.port)


def offline_morph(home: str, env_file: str, record: str, *argv: str):
    """`morph <argv>` that reaches nothing beyond this computer: connections and DNS lookups to anything but loopback
    are refused and recorded in `record` ({"host"} per line); the .env is `env_file` (never the checkout's)."""
    lock = threading.Lock()

    def note(host):
        with lock, open(record, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"host": str(host)}) + "\n")

    def loopback(host) -> bool:
        host = (host.decode() if isinstance(host, bytes) else str(host)).strip("[]").split("%")[0].lower()
        try:
            return host in ("localhost", "") or ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    connect, create_connection, getaddrinfo = socket.socket.connect, socket.create_connection, socket.getaddrinfo

    def guarded_connect(self, address):
        if isinstance(address, tuple) and not loopback(address[0]):
            note(address[0])
            raise ConnectionRefusedError(f"this Studio reaches nothing beyond this computer ({address[0]})")
        return connect(self, address)

    def guarded_create_connection(address, *a, **k):
        if not loopback(address[0]):
            note(address[0])
            raise ConnectionRefusedError(f"this Studio reaches nothing beyond this computer ({address[0]})")
        return create_connection(address, *a, **k)

    def guarded_getaddrinfo(host, *a, **k):
        if host is not None and not loopback(host):
            note(host)
            raise socket.gaierror(socket.EAI_NONAME, f"this Studio looks up nothing beyond this computer ({host})")
        return getaddrinfo(host, *a, **k)

    socket.socket.connect, socket.create_connection, socket.getaddrinfo = guarded_connect, guarded_create_connection, guarded_getaddrinfo
    sys.path.insert(0, ROOT)
    os.environ["MORPH_HOME"] = home
    from morph import keys
    keys.ENV_PATH = env_file
    from morph import cli
    return cli.main(list(argv))


# ------------------------------------------------------------------ fixtures
@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = launch(p)
        if b is None:
            pytest.skip("no Chromium for Playwright")
        yield b
        b.close()


@pytest.fixture
def world(tmp_path, tmp_path_factory):
    """A copy of the built demo robots, the stand-in Claude (Live prices' script; its answer after a distributor call
    that failed offline waits for `hold` to be released, so the page's in-between state can be looked at)."""
    import fake_claude

    home = tmp_path / "home"
    shutil.copytree(demo_home(tmp_path_factory), home)
    hold = threading.Event()

    def script(body):
        if any(n == "mouser_search_keyword" and isinstance(d, dict) and d.get("error") == "network"
               for n, d, _ in fake_claude._results(body)):
            assert hold.wait(60), "the test never let the stand-in answer"
        return fake_claude.price_check(body)

    claude = fake_claude.FakeClaude(script, port=free_port())
    made = []

    def studio(project="egg-picker", team=False):
        s = ToolsStudio(str(home), claude.url, project, team)
        made.append(s)
        return s

    yield {"home": str(home), "claude": claude, "hold": hold, "studio": studio}
    hold.set()
    for s in made:
        s.stop()
    claude.close()
    release_port(claude.port)


def card(page) -> str:
    """The newest run card's text."""
    return " ".join(page.ev("[...document.querySelectorAll('.tl-run')].pop()?.innerText || ''").split())


def acts(page) -> list:
    return page.ev("[...([...document.querySelectorAll('.tl-run')].pop()?.querySelectorAll('[data-act]') || [])].map(b => b.dataset.act)")


def tool_results(claude) -> list:
    import fake_claude
    return [r for body in claude.requests for r in fake_claude._results(body)]


def files_text(folder: str) -> str:
    out = []
    for dp, _, fns in os.walk(folder):
        for fn in fns:
            if fn.endswith((".json", ".jsonl", ".head")):
                with open(os.path.join(dp, fn), encoding="utf-8", errors="replace") as fh:
                    out.append(fh.read())
    return "\n".join(out)


# ------------------------------------------------------------------ the tests
def test_live_prices_skip_add_key_and_a_reload(world, browser):
    s = world["studio"]()
    page = Page(browser, s, (1280, 720), "Jim")
    try:
        # so101: Live prices from the BOM tab, the run pauses for the Mouser key, and Skip goes on without it
        page.open("project=so101&tab=bom")
        until(lambda: page.text(".tl-bom") == "Live prices (beta)", 30, "the BOM tab's Live prices button")
        page.pg.click(".tl-bom")
        until(lambda: "Key needed: Mouser Electronics" in card(page), 60, "the Key needed card")
        text = card(page)
        assert "Live prices for r001" in text and "Needs a key" in text and "Not yet tried with a real key" in text
        assert "Morph adds no fee; check Mouser's Search API page for its terms and limits." in text
        assert "go to Mouser Electronics with your Search API key. Nothing else from your project is sent." in text
        assert "api.mouser.com only" in text and acts(page) == ["key", "skip", "stop"]
        assert page.ev("[...document.querySelectorAll('.tl-run')].pop().querySelector('a.btn').href") == "https://www.mouser.com/apihome/"
        until(lambda: page.text(".tl-bom") == "Live prices: key needed", 10, "the button saying what the run waits for")
        page.fits("the Key needed card")
        page.shot("t2e_1_key_needed_so101")
        page.pg.locator(".tl-run").last.locator('[data-act="skip"]').click()
        until(lambda: "you chose to continue without the Mouser key" in card(page), 60, "Claude's summary after Skip")
        text = card(page)
        assert "Done" in text and "Mouser keyword search · skipped: the run went on without it" in text
        assert "No prices were recorded in this run." in text
        skipped = [d for n, d, e in tool_results(world["claude"]) if n == "mouser_search_keyword"]
        assert skipped and skipped[-1]["status"] == "skipped_by_user"
        page.shot("t2e_2_skipped_so101")

        # egg-picker: Add key opens Keys on the Mouser key; saving it continues the run, whose call fails offline
        page.open("project=egg-picker&tab=bom")
        until(lambda: page.text(".tl-bom") == "Live prices (beta)", 30, "egg-picker's Live prices button")
        page.pg.click(".tl-bom")
        until(lambda: "Key needed: Mouser Electronics" in card(page), 60, "egg-picker's Key needed card")
        page.pg.locator(".tl-run").last.locator('[data-act="key"]').click()
        until(lambda: page.ev("document.querySelector('#keysDlg.on .kf.kfocus')?.dataset.k") == "MORPH_TOOL_MOUSER_KEY", 30,
              "Keys open on the Mouser key")
        page.shot("t2e_3_keys_on_mouser")
        page.pg.fill("#keysDlg .kf.kfocus input", MOUSER_KEY)
        page.pg.click('#keysDlg [data-a="save"]')
        until(lambda: page.ev("!!document.querySelector('#keysDlg .kchk.good')"), 30, "the saved note")
        assert "A paused run that waited for it continues now." in page.text("#keysDlg .knote")
        page.pg.keyboard.press("Escape")
        until(lambda: "Key added, continuing" in card(page), 30, '"Key added, continuing..."')
        page.shot("t2e_4_key_added_continuing")
        world["hold"].set()
        until(lambda: "Mouser couldn't be reached from this computer" in card(page), 60, "the summary after the offline call")
        text = card(page)
        assert "Done" in text and "Mouser Search API key added: the run continued" in text
        assert "Mouser keyword search · couldn't reach api.mouser.com (offline or blocked)" in text
        failed = [d for n, d, e in tool_results(world["claude"]) if n == "mouser_search_keyword" and isinstance(d, dict)]
        assert failed[-1]["error"] == "network"
        page.shot("t2e_5_done_egg_picker")
        lines = page.ev("[...document.querySelectorAll('.tl-run')].pop().innerText.replace(/\\d+ ms/g, 'ms')")
        page.open("project=egg-picker&tab=bom")  # a reload: the same card from GET /api/runs
        until(lambda: "Mouser couldn't be reached" in card(page), 30, "the card after a reload")
        assert page.ev("[...document.querySelectorAll('.tl-run')].pop().innerText.replace(/\\d+ ms/g, 'ms')") == lines
        page.fits("the finished card")

        # the key is saved for the Studio (in the test's .env, private), never shown, sent or written anywhere else
        import urllib.request
        with urllib.request.urlopen(s.base + "/api/keys", timeout=30) as r:
            keys_state = json.loads(r.read())
        assert next(k for k in keys_state["keys"] if k["name"] == "MORPH_TOOL_MOUSER_KEY")["set"] is True
        assert oct(os.stat(s.env_file).st_mode & 0o777) == "0o600"
        with urllib.request.urlopen(s.base + "/api/runs?project=egg-picker", timeout=30) as r:
            runs_doc = r.read().decode()
        assert MOUSER_KEY not in page.pg.content() and MOUSER_KEY not in runs_doc
        assert MOUSER_KEY not in files_text(world["home"]) and MOUSER_KEY not in json.dumps(world["claude"].requests)
        assert not any(MOUSER_KEY in str(h) for h in world["claude"].headers)
        assert s.blocked_hosts() <= {"api.mouser.com"} and "api.mouser.com" in s.blocked_hosts()  # nothing else left
        page.check()
        assert not s.tracebacks(), s.tracebacks()
    finally:
        page.close()


def _team_member(browser, s, name, size, path):
    p = Page(browser, s, size, name, quiet=("status of 401", "status of 403"))
    p.pg.goto(s.base + path if path.startswith("/") else path, wait_until="domcontentloaded")
    with p.pg.expect_navigation():
        p.pg.click("button[type=submit]")
    until(lambda: p.ev("!!(window.morph?.state?.current && document.querySelector('.tm-hdr'))"), 120, f"{name}'s Studio page")
    return p


def _post(page, path, body):
    return page.ev("""async ([path, body]) => { const r = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json',
        'X-Morph-Client': '1'}, body: JSON.stringify(body)}); return [r.status, await r.json()]; }""", [path, body])


def test_a_team_editor_may_skip_and_a_viewer_is_told_to_ask_the_owner(world, browser):
    home = world["home"]
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "offline-morph", home, os.path.join(os.path.dirname(home), "studio.env"),
                        os.path.join(os.path.dirname(home), "blocked.jsonl"), "team", "init", "--owner", "Jim", "--name", "R Cubed"],
                       cwd=ROOT, env=env(home, world["claude"].url), capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and "Team \"R Cubed\" created" in r.stdout, r.stdout + r.stderr
    s = world["studio"](team=True)
    jim = _team_member(browser, s, "Jim", (1280, 720), s.link)
    people = [jim]
    try:
        for name, role, size in (("Ana", "editor", (1280, 720)), ("Vic", "viewer", (1600, 900))):
            st, inv = _post(jim, "/api/team/invites", {"role": role, "name_hint": name, "disciplines": []})
            assert st == 200, inv
            people.append(_team_member(browser, s, name, size, inv["path"]))
        ana, vic = people[1], people[2]
        for p in people:
            p.open("project=egg-picker&tab=bom")
        until(lambda: jim.text(".tl-bom") == "Live prices (beta)" and ana.text(".tl-bom") == "Live prices (beta)", 30,
              "the owner's and the editor's Live prices button")
        assert vic.ev("document.querySelector('.tl-bom')") is None  # a viewer can't start one
        jim.pg.click(".tl-bom")
        for p in people:
            until(lambda: "Key needed: Mouser Electronics" in card(p), 60, f"{p.name}'s Key needed card")
        assert acts(jim) == ["key", "skip", "stop"]
        assert acts(ana) == ["skip", "stop"] and ana.ev("[...document.querySelectorAll('.tl-run a.btn')].length") == 0
        assert "Ask the owner to add the Mouser Search API key in Keys. Or skip it: the run carries on without it." in card(ana)
        assert acts(vic) == [] and vic.ev("[...document.querySelectorAll('.tl-run a.btn')].length") == 0
        assert "Ask the owner to add the Mouser Search API key in Keys." in card(vic) and "Or skip it" not in card(vic)
        for p in people:
            p.fits("the Key needed card")
            p.shot(f"t2e_team_1_{p.name.lower()}_key_needed")
        # nor can they act through the API
        run = jim.ev("fetch('/api/runs?project=egg-picker').then(r => r.json())")["runs"][0]
        tu = run["cards"][0]["tool_use_id"]
        assert _post(vic, "/api/runs/decide?project=egg-picker", {"run": run["id"], "decisions": {tu: "skip"}})[0] == 403
        st, out = _post(ana, "/api/runs/decide?project=egg-picker", {"run": run["id"], "decisions": {tu: "allow_once"}})
        assert st == 403 and out["error"] == "Only the owner can allow or deny a tool; you can skip it."
        assert _post(vic, "/api/runs/start?project=egg-picker", {"agent": "price_check"})[0] == 403
        assert _post(ana, "/api/keys", {"values": {"MORPH_TOOL_MOUSER_KEY": MOUSER_KEY}})[0] == 403
        # the owner adds the key: everyone's card continues
        jim.pg.locator(".tl-run").last.locator('[data-act="key"]').click()
        until(lambda: jim.ev("document.querySelector('#keysDlg.on .kf.kfocus')?.dataset.k") == "MORPH_TOOL_MOUSER_KEY", 30, "Keys")
        jim.pg.fill("#keysDlg .kf.kfocus input", MOUSER_KEY)
        jim.pg.click('#keysDlg [data-a="save"]')
        until(lambda: jim.ev("!!document.querySelector('#keysDlg .kchk.good')"), 30, "the saved note")
        jim.pg.keyboard.press("Escape")
        for p in people:
            until(lambda: "Key added, continuing" in card(p), 30, f"{p.name}'s card continuing")
        vic.shot("t2e_team_2_vic_continuing")
        world["hold"].set()
        for p in people:
            until(lambda: "Mouser couldn't be reached from this computer" in card(p), 60, f"{p.name}'s finished card")
            assert "Mouser Search API key added: the run continued" in card(p)
        vic.shot("t2e_team_3_vic_done")
        for p in people:
            p.check()
        assert not s.tracebacks(), s.tracebacks()
    finally:
        for p in people:
            p.close()


if __name__ == "__main__" and len(sys.argv) > 5 and sys.argv[1] == "offline-morph":
    sys.exit(offline_morph(*sys.argv[2:]))
