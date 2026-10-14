"""Tool keys: Keys entries generated from the manifests of the tools that are on (service, what, where, link, cost,
privacy, hosts, pattern), status() that never returns a value, save() that checks a tool key against its manifest and
keeps .env private, manifests that can't name another program's variable, the chat filter catching tool keys, the
env.example note, and the Keys dialog (static/keys.js) in a real browser: tool keys grouped under their service, and a
`morph:keys` event naming a key opens the dialog on it. MORPH_SHOTS_DIR=<dir> keeps the dialog's screenshots."""

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from morph import keys
from morph.studio import security
from morph.studio.toolbox import ManifestError, catalog
from toolbox_helpers import ENV, FIXTURES, KEY

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(REPO, "morph", "studio", "static")
CHROME = os.environ.get("PLAYWRIGHT_CHROMIUM") or "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
BUILTIN = [k["name"] for k in keys.KEYS]


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k in [*BUILTIN, ENV]:  # recorded, so what keys.save() sets in a test is gone after it
        monkeypatch.setenv(k, "unset")
        monkeypatch.delenv(k)
    monkeypatch.setattr(keys, "ENV_PATH", str(tmp_path / ".env"))
    monkeypatch.setattr(catalog, "CURATED_DIRS", [FIXTURES])
    return tmp_path / ".env"


def _user_manifest(home=None, **over):
    doc = json.load(open(os.path.join(FIXTURES, "fixparts.json"), encoding="utf-8"))
    doc.update(id="ownparts", service="OwnParts", hosts=["api.ownparts-morph.com"])
    doc["credentials"][0].update(id="ownparts", env="MORPH_TOOL_OWNPARTS_TOKEN", hosts=["api.ownparts-morph.com"],
                                 pattern="^[A-Za-z0-9]{32}$", prefix="", label="OwnParts token")
    tool = doc["tools"][0]
    tool.update(name="ownparts_offers", auth={**tool["auth"], "credential": "ownparts"})
    tool["request"]["url"] = "https://api.ownparts-morph.com/v1/parts/{mpn}/offers"
    doc["tools"] = [tool]
    doc.update(over)
    os.makedirs(catalog.user_dir(home), exist_ok=True)
    with open(os.path.join(catalog.user_dir(home), "ownparts.json"), "w", encoding="utf-8") as fh:
        json.dump(doc, fh)


def test_keys_lists_the_keys_of_the_tools_that_are_on(env):
    st = keys.status()
    assert [k["name"] for k in st] == [*BUILTIN, ENV]
    e = st[-1]
    assert e["tool"] is True and e["set"] is False and e["service"] == "FixParts (test fixture)"
    assert e["label"] == "FixParts API key" and e["what"] and e["where"] and e["url"] == "https://www.fixparts-morph.com/developers/keys"
    assert e["cost"] == "Free: 1,000 lookups a month" and "go to FixParts" in e["privacy"]
    assert e["hosts"] == ["api.fixparts-morph.com"] and e["pattern"] == "^fxp_[A-Za-z0-9]{24,64}$"
    assert e["used_by"] == ["FixParts price and stock", "FixParts quote request"]
    assert keys.save({ENV: KEY}) == [ENV]
    st = keys.status()
    assert st[-1]["set"] is True and KEY not in json.dumps(st)  # never the value
    catalog.set_enabled("fixparts_offers", False)
    assert keys.status()[-1]["used_by"] == ["FixParts quote request"]
    catalog.set_enabled("fixparts_quote_request", False)
    off = next(k for k in keys.status() if k["name"] == ENV)  # no tool that is on needs it: listed only while it is saved,
    assert off["unused"] is True and off["used_by"] == [] and off["set"] is True  # so it can be removed
    assert keys.save({ENV: ""}) == [ENV]
    assert ENV not in [k["name"] for k in keys.status()]
    _user_manifest()
    assert "MORPH_TOOL_OWNPARTS_TOKEN" not in [k["name"] for k in keys.status()]  # the owner's manifests start off
    catalog.set_enabled("ownparts_offers", True)
    own = keys.status()[-1]
    assert own["name"] == "MORPH_TOOL_OWNPARTS_TOKEN" and own["curated"] is False


def test_saving_a_tool_key_checks_it_against_its_manifest(env):
    env.write_text("# my notes\nANTHROPIC_API_KEY=\nOTHER_TOOL=1\n", encoding="utf-8")
    for bad in ("short", "fxp_" + "a" * 10, "nope_" + "a" * 30, "fxp_" + "a" * 30 + " x", 'fxp_"' + "a" * 30,
                "fxp_" + "a" * 5000, "fxp_" + "a" * 30 + "\nANTHROPIC_API_KEY=evil"):
        with pytest.raises(ValueError) as e:
            keys.save({ENV: bad})
        assert bad not in str(e.value) and "FixParts" in str(e.value)  # says whose key, never repeats it
    for name in ("MORPH_TOOL_NOBODY_KEY", "MORPH_TOOL_x", "PATH", "HTTPS_PROXY", "MORPH_TOOL_A"):
        with pytest.raises(ValueError):
            keys.save({name: "fxp_" + "a" * 30})
    assert env.read_text(encoding="utf-8") == "# my notes\nANTHROPIC_API_KEY=\nOTHER_TOOL=1\n" and ENV not in os.environ
    assert keys.save({ENV: f"  {KEY} ", "ANTHROPIC_API_KEY": "sk-ant-test"}) == ["ANTHROPIC_API_KEY", ENV]
    assert env.read_text(encoding="utf-8").splitlines() == ["# my notes", "ANTHROPIC_API_KEY=sk-ant-test", "OTHER_TOOL=1", f"{ENV}={KEY}"]
    assert os.environ[ENV] == KEY and oct(os.stat(env).st_mode & 0o777) == "0o600"
    assert dict(keys.parse(env.read_text(encoding="utf-8")))[ENV] == KEY
    assert keys.save({ENV: ""}) == [ENV] and ENV not in os.environ  # Remove


def test_saves_from_many_threads_keep_every_key(env):
    errors = []

    def save(name, value):
        try:
            for _ in range(10):
                keys.save({name: value})
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=save, args=a) for a in ((ENV, KEY), ("SUBQ_API_KEY", "sk-test0123456789abcdefghij"),
                                                                ("KEEWANO_PROJECT_ID", "p1"))]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert not errors
    saved = dict(keys.parse(env.read_text(encoding="utf-8")))
    assert saved[ENV] == KEY and saved["SUBQ_API_KEY"].startswith("sk-test") and saved["KEEWANO_PROJECT_ID"] == "p1"


@pytest.mark.parametrize("name", ["ANTHROPIC_API_KEY", "PATH", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "MORPH_TOKEN",
                                  "MORPH_HOME", "MORPH_TOOL_X", "MORPH_TOOL_lower", "MORPH_TOOL_" + "A" * 41, "morph_tool_KEY",
                                  "MORPH_TOOL_KEY\nPATH"])
def test_a_manifest_can_never_name_another_programs_variable(name):
    doc = json.load(open(os.path.join(FIXTURES, "fixparts.json"), encoding="utf-8"))
    doc["credentials"][0]["env"] = name
    with pytest.raises(ManifestError, match="MORPH_TOOL_"):
        catalog.parse_manifest(doc)


def test_chat_refuses_tool_keys(env, monkeypatch):
    assert security.looks_like_secret(f"my key is {KEY}.")  # its manifest's prefix and pattern, even before it is saved
    assert security.looks_like_secret(f'use "{KEY}" for FixParts')
    monkeypatch.setenv("MORPH_TOOL_LEGACY_KEY", "a1b2c3d4e5f6g7h8")  # a saved key with no prefix: found by its value
    assert security.looks_like_secret("here it is: a1b2c3d4e5f6g7h8, thanks")
    assert security.looks_like_secret("token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFU")
    for fine in ("Swap to Dynamixel servos", "use the STS3215 and ATMEGA328P-AU, or STM32F407VGT6TR", "fxp_short",
                 "part fxp_" + "a" * 10, "uuid 123e4567-e89b-12d3-a456-426614174000", "Make it 6-DOF with a 550 mm reach",
                 "a1b2c3d4", ""):
        assert not security.looks_like_secret(fine), fine


def test_env_example_explains_tool_keys():
    text = open(os.path.join(REPO, "env.example"), encoding="utf-8").read()
    assert "MORPH_TOOL_<NAME>" in text and "Keys dialog" in text and "never to" in text and "own hosts" in text
    assert not [ln for ln in text.splitlines() if ln.startswith("MORPH_TOOL_")]  # documented, never pre-filled


# ------------------------------------------------------------------ the dialog in a browser
PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Keys</title>
<link rel="stylesheet" href="/static/style.css"></head><body style="opacity:1"><script type="module">
import { KeysDialog } from '/static/keys.js';
window.dlg = new KeysDialog({ toast: (t) => { window.lastToast = t; }, onChanged() {}, reset: async () => true });
window.ready = true;
</script></body></html>"""


def _serve(state):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _keys(self):
            return {"keys": keys.status(), "integrations": {"keewano": {}}, "settings": {"network_enabled": True},
                    "file": ".env", "compute": {"remote": False}}

        def do_GET(self):
            if self.path == "/":
                return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            if self.path.startswith("/api/keys"):
                return self._send(403, {"error": "owner only"}) if state["forbid"] else self._send(200, self._keys())
            rel = self.path.split("?")[0][len("/static/"):]
            p = os.path.realpath(os.path.join(STATIC, rel))
            if self.path.startswith("/static/") and p.startswith(os.path.realpath(STATIC) + os.sep) and os.path.isfile(p):
                ctype = {".js": "text/javascript", ".css": "text/css", ".woff2": "font/woff2"}.get(os.path.splitext(p)[1], "application/octet-stream")
                return self._send(200, open(p, "rb").read(), ctype)
            return self._send(404, {"error": "no"})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            state["posts"].append(body.get("values"))
            try:
                changed = keys.save(body.get("values") or {})
            except ValueError as exc:
                return self._send(400, {"error": str(exc)})
            return self._send(200, {"ok": True, "changed": changed, **self._keys()})

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_the_keys_dialog_groups_tool_keys_and_opens_on_one(env, tmp_path):
    sync_api = pytest.importorskip("playwright.sync_api")
    state = {"posts": [], "forbid": False}
    srv = _serve(state)
    shots = os.environ.get("MORPH_SHOTS_DIR")
    try:
        with sync_api.sync_playwright() as p:
            try:
                browser = p.chromium.launch(executable_path=CHROME if os.path.exists(CHROME) else None, args=["--no-sandbox"])
            except Exception as exc:  # noqa: BLE001
                pytest.skip(f"no Chromium for Playwright ({exc.__class__.__name__})")
            page = browser.new_page(viewport={"width": 1280, "height": 760})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"http://127.0.0.1:{srv.server_address[1]}/")
            page.wait_for_function("window.ready === true", timeout=15000)
            page.evaluate("window.dispatchEvent(new CustomEvent('morph:keys', {detail: {key: 'MORPH_TOOL_FIXPARTS_KEY'}}))")
            field = f'.kf.kfocus[data-k="{ENV}"]'
            page.wait_for_selector(field, timeout=15000)
            assert page.evaluate("document.activeElement.closest('.kf')?.dataset.k") == ENV
            box, view = page.locator(field).bounding_box(), page.viewport_size
            assert box and 0 <= box["y"] and box["y"] + box["height"] <= view["height"]  # scrolled into view
            group = page.inner_text(".ksec.ktool")
            assert "FixParts (test fixture)" in group and "not set up" in group and "Free: 1,000 lookups a month." in group
            assert "Sent only to api.fixparts-morph.com; used by FixParts price and stock, FixParts quote request" in group
            assert page.inner_text(".ksec.ktools .ks-h").startswith("Tool keys")
            assert page.get_attribute(f'.kf[data-k="{ENV}"] .kh a', "href") == "https://www.fixparts-morph.com/developers/keys"
            sections = page.eval_on_selector_all(".ksec .ks-h b", "els => els.map(e => e.textContent)")
            assert sections.index("Tool keys") > sections.index("Darwin") and sections[-1] == "Privacy and network"
            if shots:
                os.makedirs(shots, exist_ok=True)
                page.screenshot(path=os.path.join(shots, "keys_tool_focus.png"))
            page.fill(f'.kf[data-k="{ENV}"] input', "not-a-fixparts-key")
            page.click('[data-a="save"]')
            page.wait_for_selector(".kchk.bad", timeout=5000)
            assert "doesn't look like a FixParts (test fixture) key" in page.inner_text(".knote") and state["posts"] == []
            page.fill(f'.kf[data-k="{ENV}"] input', KEY)
            page.click('[data-a="save"]')
            page.wait_for_selector(f'.kf[data-k="{ENV}"] .tag.good', timeout=5000)
            assert state["posts"] == [{ENV: KEY}] and os.environ[ENV] == KEY
            assert page.locator('[data-a="test"]').count() == 0  # tool keys are tried by their tools, not by Test keys
            assert "set up" in page.inner_text(".ksec.ktool .ks-h") and KEY not in page.content()
            if shots:
                page.screenshot(path=os.path.join(shots, "keys_tool_saved.png"))
            page.keyboard.press("Escape")
            state["forbid"] = True  # a member who isn't the owner
            page.evaluate("window.dispatchEvent(new CustomEvent('morph:keys', {detail: 'MORPH_TOOL_FIXPARTS_KEY'}))")
            page.wait_for_selector("#keysDlg.on .dlg p", timeout=5000)
            assert "Only the owner of this Studio" in page.inner_text("#keysDlg")
            state["forbid"] = False
            page.keyboard.press("Escape")
            page.evaluate("window.dispatchEvent(new CustomEvent('morph:keys', {detail: {key: 'MORPH_TOOL_GONE_KEY'}}))")
            page.wait_for_selector(".kchk.bad", timeout=5000)
            assert "MORPH_TOOL_GONE_KEY isn't in this list" in page.inner_text(".knote")
            assert not errors, errors
            browser.close()
    finally:
        srv.shutdown()
        srv.server_close()
