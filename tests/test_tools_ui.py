"""static/tools.js in a real browser: the real Studio page against a real Studio server whose toolbox talks to scripted
Claude (the anthropic SDK over a mock transport) and the in-process FixParts fixture API (toolbox_helpers), so nothing
leaves the test. A run's card in the chat: Key needed, Add key opening Keys on that key, "Key added, continuing…", the
lines of what it used, Claude's summary, the same card rebuilt from GET /api/runs after a reload, a Tool request
answered with Allow once and another with Skip, Live prices started and stopped from the BOM tab, and in a team the
editor who may only skip and the viewer told to ask the owner. Skipped without Playwright's Chromium.
MORPH_SHOTS_DIR=<dir> keeps screenshots (tools_ui_*.png)."""

import os
import threading
import time

import pytest

from morph import settings
from morph.studio import api_tools
from morph.studio.toolbox import runs as runs_mod
from test_tools_api import CATS, OFFERS, World, _start
from toolbox_helpers import ENV, KEY, KEY2, final, search_and_call

pw = pytest.importorskip("playwright.sync_api")
CHROME = os.environ.get("PLAYWRIGHT_CHROMIUM") or "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
SHOTS = os.environ.get("MORPH_SHOTS_DIR")


def held(step, gate):
    """A scripted answer that waits for `gate`, the way the API takes a moment: the page shows what comes in between for
    as long as the test looks at it."""
    def answer(body):
        assert gate.wait(30)
        return step
    return answer


def until(fn, timeout=60.0, what="the page"):  # a minute: a busy machine (a full parallel run) can take that long
    t0, last = time.time(), None
    while time.time() - t0 < timeout:
        try:
            v = fn()
        except Exception as exc:  # noqa: BLE001  (a page between two loads)
            v, last = None, exc
        if v:
            return v
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}" + (f" ({last})" if last else ""))


@pytest.fixture
def browser():
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch(executable_path=CHROME if os.path.exists(CHROME) else None, args=["--no-sandbox"])
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"no Chromium for Playwright ({exc.__class__.__name__})")
        yield b
        b.close()


@pytest.fixture
def make_world(tmp_path, monkeypatch):
    api_tools.agent("test_prices", title="Test prices", start=_start)
    made = []

    def make(*steps, team=False):
        w = World(tmp_path, monkeypatch, steps, team)
        w.studio.toolbox.spawn = runs_mod._thread  # runs work in the background, as in the Studio
        made.append(w)
        return w

    yield make
    for w in made:
        w.close()
    api_tools.AGENTS.pop("test_prices", None)
    from morph.studio import api_team
    from morph.studio.teams import auth
    api_team.configure(home=None, enabled=False)
    auth.THROTTLE.clear()


class Page:
    def __init__(self, browser, w, who=None, size=(1280, 720)):
        self.w, self.errors = w, []
        self.ctx = browser.new_context(viewport={"width": size[0], "height": size[1]})
        port = w.httpd.server_address[1]
        if who:
            name, value = w.jar[who].split("=", 1)
            self.ctx.add_cookies([{"name": name, "value": value, "url": f"http://127.0.0.1:{port}/"}])
        self.pg = self.ctx.new_page()
        self.pg.on("pageerror", lambda e: self.errors.append(str(e)))
        # quiet: the browser without WebGL, the fixture project's missing CAD files, and the refusals a test asks for
        self.pg.on("console", lambda m: self.errors.append(m.text) if m.type == "error" and not any(
            q in m.text for q in ("WebGL", "THREE", "RobotViewer", "status of 404", "status of 403")) else None)
        self.base = f"http://127.0.0.1:{port}"

    def open(self, tab="robot"):
        self.pg.goto(f"{self.base}/?tour=0&project=egg-picker&tab={tab}")
        until(lambda: self.pg.evaluate("window.morph?.state?.loaded === true && !!window.morph.state.current"))
        return self

    def card(self) -> str:
        """The newest run card's text."""
        return " ".join(self.pg.evaluate("[...document.querySelectorAll('.tl-run')].pop()?.innerText || ''").split())

    def acts(self) -> list:
        return self.pg.evaluate("[...([...document.querySelectorAll('.tl-run')].pop()?.querySelectorAll('[data-act]') || [])]"
                                ".map(b => b.dataset.act)")

    def shot(self, name):
        if SHOTS:
            time.sleep(0.6)  # the chat scrolls smoothly
            os.makedirs(SHOTS, exist_ok=True)
            self.pg.screenshot(path=os.path.join(SHOTS, f"tools_ui_{name}.png"))


def test_a_key_needed_card_add_key_continuing_and_the_card_after_a_reload(browser, make_world):
    gate = threading.Event()
    w = make_world(search_and_call(OFFERS), held(final("STS3215: 412 in stock at FixParts, $13.90 each."), gate))
    page = Page(browser, w).open()
    s, out = w.call("POST", "/api/runs/start?project=egg-picker", {"agent": "test_prices"})
    assert s == 202 and out["already"] is False  # the run works in the background; the card follows its events
    until(lambda: "Key needed: FixParts (test fixture)" in page.card(), what="the Key needed card")
    text = page.card()
    assert "Test prices" in text and "Needs a key" in text and "Free: 1,000 lookups a month" in text
    assert "go to FixParts" in text and "api.fixparts-morph.com only" in text
    assert page.acts() == ["key", "skip", "stop"]
    assert page.pg.get_attribute(".tl-run a.btn", "href") == "https://www.fixparts-morph.com/developers/keys"
    page.shot("key_needed")
    page.pg.click('.tl-run [data-act="key"]')  # Add key: the Keys dialog, on this key
    until(lambda: page.pg.evaluate(f"document.querySelector('#keysDlg.on .kf.kfocus')?.dataset.k === '{ENV}'"), what="Keys on the key")
    page.pg.fill("#keysDlg .kf.kfocus input", KEY)
    page.pg.click('#keysDlg [data-a="save"]')
    until(lambda: page.pg.evaluate("!!document.querySelector('#keysDlg .kchk.good')"), what="the saved note")
    assert "A paused run that waited for it continues now." in page.pg.inner_text("#keysDlg .knote")
    page.pg.keyboard.press("Escape")
    until(lambda: "Key added, continuing" in page.card(), what='"Key added, continuing…"')  # while Claude answers
    page.shot("continuing")
    gate.set()
    until(lambda: "Done" in page.card() and "412 in stock" in page.card(), what="the finished card")
    text = page.card()
    assert "FixParts API key added: the run continued" in text and "FixParts price and stock · api.fixparts-morph.com" in text
    assert "Claude's summary (beta)".upper() in text.upper() and page.acts() == []
    assert KEY not in page.pg.content()
    page.shot("done")
    lines = page.pg.evaluate("[...document.querySelectorAll('.tl-run .tl-lines li')].map(li => li.innerText.replace(/\\d+ ms/, 'ms'))")
    page.open()  # a reload: the card comes back from GET /api/runs, line for line
    until(lambda: "412 in stock" in page.card(), what="the card after a reload")
    assert page.pg.evaluate("[...document.querySelectorAll('.tl-run .tl-lines li')].map(li => li.innerText.replace(/\\d+ ms/, 'ms'))") == lines
    assert page.pg.evaluate("document.querySelectorAll('.tl-run').length") == 1
    assert not page.errors, page.errors


def test_a_key_the_service_refuses_can_be_replaced_from_the_card(browser, make_world):
    w = make_world(search_and_call(OFFERS), final("STS3215: 412 in stock at FixParts."))
    assert w.call("POST", "/api/keys", {"values": {ENV: KEY}})[0] == 200
    w.vendor.reject = True  # the service answers 401 to this key
    page = Page(browser, w).open()
    w.call("POST", "/api/runs/start?project=egg-picker", {"agent": "test_prices"})
    until(lambda: "FixParts (test fixture) refused the key" in page.card(), what="the refused-key card")
    assert page.acts() == ["key", "skip", "stop"] and page.pg.inner_text('.tl-run [data-act="key"]').strip() == "Replace key"
    assert ("It may be mistyped, expired or not enabled for this service: copy it again or make a new one, then replace it."
            in page.card())
    page.shot("key_refused")
    page.pg.click('.tl-run [data-act="key"]')
    until(lambda: page.pg.evaluate(f"document.querySelector('#keysDlg.on .kf.kfocus')?.dataset.k === '{ENV}'"), what="Keys on the key")
    w.vendor.reject = False
    page.pg.fill("#keysDlg .kf.kfocus input", KEY2)
    page.pg.click('#keysDlg [data-a="save"]')
    until(lambda: page.pg.evaluate("!!document.querySelector('#keysDlg .kchk.good')"), what="the saved note")
    page.pg.keyboard.press("Escape")
    until(lambda: "Done" in page.card() and "412 in stock" in page.card(), what="the run with the new key")
    assert "FixParts API key added: the run continued" in page.card()
    assert w.vendor.calls[-1][2]["Authorization"] == f"Bearer {KEY2}" and KEY2 not in page.pg.content()
    assert not page.errors, page.errors


def test_parallel_calls_share_one_card_and_one_answer(browser, make_world):
    """Claude often looks several parts up at once: one Key needed card for the key, one Tool request card for the
    tool, and one answer covers every call."""
    w = make_world(search_and_call(("fixparts_offers", "toolu_a", {"mpn": "STS3215"}), ("fixparts_offers", "toolu_b", {"mpn": "SCS0009"})),
                   search_and_call(("fixparts_categories", "toolu_c", {"query": "servo"}), ("fixparts_categories", "toolu_d", {"query": "board"})),
                   final("Skipped the prices; listed two categories."))
    page = Page(browser, w).open()
    w.call("POST", "/api/runs/start?project=egg-picker", {"agent": "test_prices"})
    until(lambda: "Key needed: FixParts (test fixture)" in page.card(), what="the Key needed card")
    assert page.pg.evaluate("[...document.querySelectorAll('.tl-run')].pop().querySelectorAll('.tl-card').length") == 1
    assert "2 lookups wait for it." in page.card()
    page.pg.click('.tl-run [data-act="skip"]')
    until(lambda: "Allow FixParts categories?" in page.card(), what="the Tool request card for both calls")
    assert page.pg.evaluate("[...document.querySelectorAll('.tl-run')].pop().querySelectorAll('.tl-card').length") == 1
    assert "and 1 more call of it in this step: your answer covers them all." in page.card()
    page.shot("parallel_calls")
    page.pg.click('.tl-run [data-act="allow_once"]')
    until(lambda: "Done" in page.card(), what="the finished run")
    text = page.card()
    assert text.count("FixParts price and stock · skipped") == 2 and text.count("FixParts categories · api.fixparts-morph.com") == 2
    sent = w.claude.requests[-1]["messages"][-1]["content"]
    assert [b["tool_use_id"] for b in sent] == ["toolu_c", "toolu_d"] and not any(b.get("is_error") for b in sent)
    assert not page.errors, page.errors


def test_a_tab_whose_event_stream_opens_late_still_shows_the_run(browser, make_world):
    """A slow stream: the page loads its state (after waiting 3 s for the stream), the run starts and pauses before the
    stream is open, so its events never reach this tab; the card comes from the refresh when the stream opens."""
    w = make_world(search_and_call(OFFERS))
    page = Page(browser, w)
    held = []
    page.pg.route("**/api/events*", lambda route: held.append(route), times=1)  # the stream's request waits in the browser
    page.open()
    until(lambda: held, what="the stream's request held back")
    s, out = w.call("POST", "/api/runs/start?project=egg-picker", {"agent": "test_prices"})
    assert s == 202
    until(lambda: w.call("GET", "/api/runs?project=egg-picker")[1]["runs"][0]["status"] == "paused", what="the run to pause")
    time.sleep(0.5)
    assert page.pg.evaluate("document.querySelectorAll('.tl-run').length") == 0  # its events went to no tab
    for r in held:  # the stream opens now
        r.continue_()
    until(lambda: "Key needed: FixParts (test fixture)" in page.card(), what="the card once the stream opened")
    assert not page.errors, page.errors


def test_a_tool_request_card_allow_once_and_skip(browser, make_world):
    w = make_world(search_and_call(CATS), final("Servo categories listed."), search_and_call(CATS), final("Went on without it."))
    page = Page(browser, w).open()
    w.call("POST", "/api/runs/start?project=egg-picker", {"agent": "test_prices"})
    until(lambda: "Allow FixParts categories?" in page.card(), what="the Tool request card")
    text = page.card()
    assert "Read-only: it only looks things up." in text and "api.fixparts-morph.com" in text and "No fee per call" in text
    assert page.acts() == ["allow_once", "allow_always", "skip", "stop"] and '"query": "servo"' in text
    page.shot("tool_request")
    page.pg.click('.tl-run [data-act="allow_once"]')
    until(lambda: "Done" in page.card(), what="the run after Allow once")
    assert "FixParts categories · api.fixparts-morph.com" in page.card()
    # the next run asks again (allowed once), and Skip goes on without it
    w.call("POST", "/api/runs/start?project=egg-picker", {"agent": "test_prices"})
    until(lambda: page.pg.evaluate("document.querySelectorAll('.tl-run').length") == 2 and "Allow FixParts categories?" in page.card(),
          what="the second Tool request card")
    page.pg.locator('.tl-run').last.locator('[data-act="skip"]').click()
    until(lambda: "Done" in page.card(), what="the run after Skip")
    assert "FixParts categories · skipped: the run went on without it" in page.card()
    assert [m["content"][0]["is_error"] for m in w.claude.requests[-1]["messages"][-1:]] == [True]
    assert not page.errors, page.errors


def test_a_late_answer_never_takes_a_finished_card_back_to_working(browser, make_world):
    """Allow once answers with the run as it was when the call went ahead. When the run ends before that answer arrives
    (a slow network, a busy Studio), neither it nor a list that was on its way may show the finished card as Working."""
    gate, slow = threading.Event(), threading.Event()
    w = make_world(search_and_call(CATS), held(final("Servo categories listed."), gate))
    tb = w.studio.toolbox
    decide, listing = tb.decide, tb.runs

    def late_decide(run_id, decisions, **kw):
        out = decide(run_id, decisions, **kw)  # the run works on: Claude's answer waits for the gate
        assert out["status"] == "running"
        gate.set()
        until(lambda: tb.get(run_id)["status"] == "done", what="the run's end")
        time.sleep(0.6)  # its run_done event reaches the page before this answer does
        slow.set()
        return out

    def slow_listing(*a, **kw):
        if slow.is_set():
            time.sleep(1.0)
        return listing(*a, **kw)
    tb.decide, tb.runs = late_decide, slow_listing
    page = Page(browser, w).open()
    w.call("POST", "/api/runs/start?project=egg-picker", {"agent": "test_prices"})
    until(lambda: "Allow FixParts categories?" in page.card(), what="the Tool request card")
    page.pg.click('.tl-run [data-act="allow_once"]')
    until(lambda: "Done" in page.card(), what="the finished card")
    until(slow.is_set, what="the late answer")
    seen, t0 = [], time.time()
    while time.time() - t0 < 2.0:  # the late answer reaches the page, then the slow list after it
        seen.append(page.card())
        time.sleep(0.05)
    assert not [t for t in seen if "Working" in t or "Done" not in t], seen[:3]
    assert "FixParts categories · api.fixparts-morph.com" in page.card() and "Servo categories listed." in page.card()
    assert not page.errors, page.errors


def test_live_prices_starts_from_the_bom_tab_and_stops_from_its_card(browser, make_world, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-ui-test-not-a-key")  # the page starts Live prices only with Claude ready
    w = make_world({"content": [{"type": "tool_use", "id": "toolu_bom", "name": "morph_bom_read", "input": {}}],
                    "stop_reason": "tool_use"}, search_and_call(OFFERS))
    w.studio.toolbox.builtins = None  # the registered core tools, Live prices' own
    _purchased(w)
    page = Page(browser, w).open(tab="bom")
    until(lambda: page.pg.evaluate("document.querySelector('.tl-bom')?.innerText === 'Live prices (beta)'"), what="the BOM tab's button")
    page.pg.click(".tl-bom")
    until(lambda: "Live prices for r001" in page.card() and "Key needed" in page.card(), what="the price check's card")
    until(lambda: page.pg.inner_text(".tl-bom") == "Live prices: key needed", what="the button saying what it waits for")
    assert "Parts list" in page.card()
    page.shot("bom_button")
    page.pg.click(".tl-bom")  # the button of a waiting run shows its card, never a second run
    assert len(w.call("GET", "/api/runs?project=egg-picker")[1]["runs"]) == 1
    page.pg.click('.tl-run [data-act="stop"]')
    until(lambda: "Cancelled" in page.card(), what="the stopped run")
    until(lambda: page.pg.inner_text(".tl-bom") == "Live prices (beta)", what="the button ready again")
    assert not page.errors, page.errors


def test_stop_while_claude_answers_says_stopping_until_the_run_ends(browser, make_world):
    """Stop pressed while Claude's answer is under way can't cut it short: the card says Stopping (no second Stop) until
    the answer comes back, then the run ends cancelled without running the call the answer asked for."""
    gate = threading.Event()
    w = make_world(held(search_and_call(CATS), gate), final("never sent"))
    page = Page(browser, w).open()
    w.call("POST", "/api/runs/start?project=egg-picker", {"agent": "test_prices"})
    until(lambda: "Working" in page.card() and "stop" in page.acts(), what="the working card")
    page.pg.click('.tl-run [data-act="stop"]')
    until(lambda: "Stopping" in page.card(), what="Stopping")
    assert "Working" not in page.card() and "stop" not in page.acts()
    page.shot("stopping")
    gate.set()
    until(lambda: "Cancelled" in page.card(), what="the stopped run")
    assert "Stopping" not in page.card() and page.acts() == []
    assert len(w.claude.requests) == 1 and w.vendor.calls == []
    assert not page.errors, page.errors


def _purchased(w):
    """The fixture BOM's lines bought or made the way the pipeline's BOM stage says (Live prices reads bought lines)."""
    import json
    path = os.path.join(w.home, "egg-picker", "r001", "results.json")
    res = json.load(open(path, encoding="utf-8"))
    for r in res["bom"]["rows"]:
        r["make_or_buy"] = "make" if r["category"] == "fabricated" else "buy"
    json.dump(res, open(path, "w", encoding="utf-8"))


def test_without_claude_the_bom_button_says_so_and_opens_keys_on_the_anthropic_key(browser, make_world):
    w = make_world()
    w.studio.toolbox.client_factory = lambda: None  # no Anthropic key (or network lookups off)
    w.studio.toolbox.builtins = None
    _purchased(w)
    page = Page(browser, w).open(tab="bom")
    until(lambda: page.pg.evaluate("document.querySelector('.tl-bom')?.innerText === 'Live prices (beta)'"), what="the BOM tab's button")
    page.pg.click(".tl-bom")
    until(lambda: page.pg.evaluate("document.querySelector('#keysDlg.on .kf.kfocus')?.dataset.k === 'ANTHROPIC_API_KEY'"),
          what="Keys open on the Anthropic key")
    assert page.pg.evaluate("window.lastToast || document.querySelector('#toast')?.innerText || ''").startswith("Live prices need Claude")
    assert w.call("GET", "/api/runs?project=egg-picker")[1]["runs"] == [] and page.pg.evaluate("document.querySelectorAll('.tl-run').length") == 0
    page.shot("no_claude")
    assert page.errors == []  # the page knew Claude isn't there: no start refused with a 503 in the console


def test_prices_found_show_on_the_card_in_show_prices_and_on_the_bom_button(browser, make_world, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-ui-test-not-a-key")
    import json

    def result(body):
        return json.loads(body["messages"][-1]["content"][0]["content"][0]["text"])

    items = []

    def call_fixparts(body):
        items.extend(ln["item"] for ln in result(body)["lines"])
        return search_and_call(("fixparts_offers", "toolu_fx", {"mpn": "STS3215"}))

    def record(body):
        assert result(body)["data"]["offers"][0]["price_usd"] == 13.9
        lines = [{"item": items[0], "distributor": "FixParts", "part_number": "STS3215", "unit_price": 13.9, "currency": "USD",
                  "quantity": 1, "stock": 412, "tool": "fixparts_offers"},
                 {"item": items[1], "distributor": "FixParts", "part_number": "ESP32-S3", "unit_price": 4.2, "currency": "USD",
                  "quantity": 1, "stock": None, "tool": "fixparts_offers"}]  # a price the tool never gave: "check it"
        return {"content": [{"type": "tool_use", "id": "toolu_rec", "name": "morph_prices_record", "input": {"lines": lines}}],
                "stop_reason": "tool_use"}

    w = make_world({"content": [{"type": "tool_use", "id": "toolu_bom", "name": "morph_bom_read", "input": {}}],
                    "stop_reason": "tool_use"}, call_fixparts, record, final("STS3215 at FixParts: $13.90, 412 in stock."))
    w.studio.toolbox.builtins = None
    _purchased(w)
    vendor = w.studio.toolbox.http_transport

    def transport(r, ip, timeout, max_bytes):  # the fixture API, answering with the part number it was asked for
        if r["path"].startswith("/v1/parts/"):
            mpn = r["path"].split("/")[3]
            return 200, {"content-type": "application/json"}, json.dumps(
                {"data": {"offers": [{"mpn": mpn, "distributor": "FixParts", "stock": 412, "price_usd": 13.9}]}}).encode()
        return vendor(r, ip, timeout, max_bytes)
    w.studio.toolbox.http_transport = transport
    assert w.call("POST", "/api/keys", {"values": {ENV: KEY}})[0] == 200  # saved first: its first use needs no OK
    page = Page(browser, w).open(tab="bom")
    until(lambda: page.pg.evaluate("document.querySelector('.tl-bom')?.innerText === 'Live prices (beta)'"), what="the BOM tab's button")
    page.pg.click(".tl-bom")
    until(lambda: "Done" in page.card() and "STS3215 at FixParts" in page.card(), what="the finished price check")
    text = page.card()
    total = len({r["item"] for r in json.load(open(os.path.join(w.home, "egg-picker", "r001", "results.json")))["bom"]["rows"]
                 if r["make_or_buy"] == "buy" and r["category"] not in ("pcb", "material", "fabricated")})
    assert f"2 of {total} purchased lines priced · from FixParts (test fixture)".upper() in text.upper()  # an uppercase label
    assert "$13.90 from FixParts (test fixture)" in text and "$4.20 check from FixParts (test fixture)" in text  # every price credited
    assert page.acts() == ["prices"]
    until(lambda: page.pg.inner_text(".tl-bom") == "Live prices (beta) · 2 priced", what="the button's count")
    page.shot("prices_card")
    page.pg.click('.tl-run [data-act="prices"]')
    until(lambda: page.pg.evaluate("!!document.querySelector('#featModal.on .tl-table')"), what="Show prices")
    rows = page.pg.evaluate("[...document.querySelectorAll('#featModal .tl-table tbody tr')].map(r => r.innerText.split('\\t').map(x => x.trim()))")
    assert rows[0][:6] == [items[0], "FixParts", "STS3215", "$13.90", "1", "412"] and "from FixParts" in rows[0][6]
    assert rows[1][:6] == [items[1], "FixParts", "ESP32-S3", "$4.20", "1", "–"] and rows[1][6] == "check it from FixParts (test fixture)"
    assert "Nothing was ordered." in page.pg.inner_text("#featModal") and "Check again" in page.pg.inner_text("#featModal")
    page.shot("show_prices")
    page.pg.keyboard.press("Escape")
    page.pg.click(".tl-bom")  # the button opens the same prices
    until(lambda: page.pg.evaluate("!!document.querySelector('#featModal.on .tl-table')"), what="Show prices from the button")
    assert not page.errors, page.errors


def test_in_a_team_the_editor_may_skip_and_the_viewer_is_told_to_ask_the_owner(browser, make_world):
    w = make_world(search_and_call(OFFERS), final("Carried on without FixParts."), team=True)
    vic = Page(browser, w, "Vic", (1600, 900)).open()
    ana = Page(browser, w, "Ana").open()
    s, out = w.call("POST", "/api/runs/start?project=egg-picker", {"agent": "test_prices"}, who="Jim")
    assert s == 202
    for p in (vic, ana):
        until(lambda: "Key needed: FixParts (test fixture)" in p.card(), what="the Key needed card")
    assert vic.acts() == [] and vic.pg.evaluate("document.querySelectorAll('.tl-run a.btn').length") == 0
    assert "Ask the owner to add the FixParts API key in Keys." in vic.card() and "Or skip it" not in vic.card()
    assert ana.acts() == ["skip", "stop"]
    assert "Ask the owner to add the FixParts API key in Keys. Or skip it: the run carries on without it." in ana.card()
    vic.shot("team_viewer")
    ana.shot("team_editor")
    ana.pg.click('.tl-run [data-act="skip"]')
    for p in (vic, ana):
        until(lambda: "Done" in p.card() and "skipped" in p.card(), what="everyone's card after the editor skipped")
    assert not vic.errors and not ana.errors, vic.errors + ana.errors


def test_a_run_stopped_by_turning_agent_tools_off_opens_keys_on_the_switch_and_the_others_ask_the_owner(browser, make_world,
                                                                                                       monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-ui-test-not-a-key")  # an interrupted run continues only with Claude ready

    def off_meanwhile(body):  # the owner turns agent tools off while Claude answers: the run stops before its call
        settings.save({"agent_tools_enabled": False})
        return search_and_call(OFFERS)
    w = make_world(off_meanwhile, final("STS3215: 412 in stock at FixParts."), team=True)
    assert w.call("POST", "/api/keys", {"values": {ENV: KEY}}, who="Jim")[0] == 200
    jim, ana, vic = (Page(browser, w, who).open() for who in ("Jim", "Ana", "Vic"))
    assert w.call("POST", "/api/runs/start?project=egg-picker", {"agent": "test_prices"}, who="Jim")[0] == 202
    for p in (jim, ana, vic):
        until(lambda: "Agent tools were turned off" in p.card(), what="the interrupted card")
    assert w.vendor.calls == [] and len(w.claude.requests) == 1
    ask = "Ask the owner to turn agent tools back on (Keys), then continue it."
    assert jim.acts() == ["continue", "key", "stop"] and "Ask the owner" not in jim.card()
    assert ana.acts() == ["continue", "stop"] and ask in ana.card()
    assert vic.acts() == [] and ask in vic.card() and "Ask an editor" not in vic.card()
    jim.shot("interrupted_owner")
    ana.shot("interrupted_editor")
    jim.pg.click('.tl-run [data-act="key"]')  # Open Keys: the dialog, on the switch
    until(lambda: jim.pg.evaluate("document.querySelector('#keysDlg.on .kt.kfocus [data-s]')?.dataset.s") == "agent_tools_enabled",
          what="Keys on the agent tools switch")
    jim.pg.click("#keysDlg .kt.kfocus b")
    until(settings.agent_tools_enabled, what="agent tools on again")
    jim.pg.keyboard.press("Escape")
    jim.pg.click('.tl-run [data-act="continue"]')
    for p in (jim, ana, vic):
        until(lambda: "Done" in p.card() and "412 in stock" in p.card(), what="the run after Continue")
    assert len(w.vendor.calls) == 1
    assert not jim.errors and not ana.errors and not vic.errors, jim.errors + ana.errors + vic.errors
