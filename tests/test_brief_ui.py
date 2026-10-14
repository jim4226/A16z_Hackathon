"""New robot dialog (static/newrobot.js) in a real browser: tools/newrobot_demo.py serves it inside a mock Studio shell
with the real brief engine behind POST /api/brief. Skipped when Playwright or Chromium isn't installed.
MORPH_SHOTS_DIR=<dir> keeps a screenshot of every state."""

import os
import sys
import threading

import pytest

sync_api = pytest.importorskip("playwright.sync_api")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tools"))
CHROME = "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"


def _serve(home):
    """The demo server on the first free port in 8800-8899, bound here (parallel test workers race for ports)."""
    import newrobot_demo

    for port in range(8800, 8900):
        try:
            return newrobot_demo.serve(port, home), port
        except OSError:
            continue
    pytest.skip("no free port in 8800-8899")


@pytest.fixture(scope="module")
def page(tmp_path_factory):
    for k in list(os.environ):
        if k.startswith(("ANTHROPIC", "KEEWANO", "SUBQ")):
            os.environ.pop(k)
    srv, port = _serve(str(tmp_path_factory.mktemp("home")))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        pw = sync_api.sync_playwright().start()
    except Exception as exc:  # noqa: BLE001
        srv.shutdown()
        pytest.skip(f"playwright unavailable: {exc}")
    try:
        br = pw.chromium.launch(**({"executable_path": CHROME} if os.path.exists(CHROME) else {}))
    except Exception as exc:  # noqa: BLE001
        pw.stop()
        srv.shutdown()
        pytest.skip(f"no chromium: {exc}")
    pg = br.new_page(viewport={"width": 1360, "height": 860})
    pg.errors = []
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))
    pg.on("console", lambda m: pg.errors.append(m.text) if m.type == "error" else None)
    pg.base = f"http://127.0.0.1:{port}/"
    yield pg
    br.close()
    pw.stop()
    srv.shutdown()


def shot(pg, name):
    d = os.environ.get("MORPH_SHOTS_DIR")
    if d:
        os.makedirs(d, exist_ok=True)
        pg.screenshot(path=os.path.join(d, f"f1_{name}.png"))


def test_describe_clarify_confirm_build_done(page):
    pg = page
    pg.goto(pg.base)
    pg.click(".nr-headbtn")
    pg.wait_for_selector("#nr-text")
    assert pg.get_attribute(".nr", "role") == "dialog" and pg.get_attribute(".nr", "aria-modal") == "true"
    assert pg.is_disabled('[data-a="start"]')
    shot(pg, "describe")
    pg.fill("#nr-text", "something to pick strawberries")
    pg.click('[data-a="start"]')
    pg.wait_for_selector(".nr-q", timeout=15000)
    shot(pg, "clarify")
    assert pg.locator(".nr-q").count() == 2 and pg.locator('[role="radiogroup"]').count() == 2
    assert "you said" in pg.inner_text(".nr-card")
    # keyboard: arrows move between chips, space picks
    first = pg.locator('.nr-q[data-slot="layout"] .nr-chip').first
    first.focus()
    pg.keyboard.press("ArrowRight")
    pg.keyboard.press(" ")
    assert pg.get_attribute('.nr-q[data-slot="layout"] .nr-chip[data-i="1"]', "aria-checked") == "true"
    assert "picked" in pg.inner_text(".nr-card")
    pg.click('.nr-q[data-slot="throughput"] .nr-chip[data-i="0"]')   # the last open question sends at once
    pg.wait_for_selector(".nr-summary", timeout=15000)
    shot(pg, "confirm")
    assert "strawberries" in pg.inner_text(".nr-summary") and "every 10 s" in pg.inner_text(".nr-summary")
    assert "passed" in pg.inner_text(".nr-confirm") and "estimated" in pg.inner_text(".nr-confirm")
    assert pg.input_value("#nr-name") == "strawberry-picker"
    pg.fill("#nr-name", "Bad Name!")
    assert pg.is_disabled('[data-a="build"]')
    pg.fill("#nr-name", "berry-bot")
    pg.click('[data-a="build"]')
    pg.wait_for_selector(".nr-build-h", timeout=15000)
    shot(pg, "building")
    pg.wait_for_selector(".nr-metrics", timeout=30000)
    shot(pg, "done")
    assert "Feasible" in pg.inner_text(".nr-done-h") and pg.inner_text("#project") == "berry-bot"
    assert pg.locator('.nr-ag[data-role="pipeline"].done').count() == 1
    pg.click('[data-a="openproj"]')
    assert pg.locator(".nr").count() == 0


def test_answer_in_free_text_and_defaults(page):
    pg = page
    pg.goto(pg.base)
    pg.click(".nr-headbtn")
    pg.fill("#nr-text", "make me a robot arm")
    pg.keyboard.press("Enter")
    pg.wait_for_selector(".nr-q", timeout=15000)
    assert pg.locator(".nr-q").count() == 3
    pg.fill(".nr-free input", "eggs, one every 8 seconds")
    pg.click(".nr-free button")                     # the free text fills the one required answer: straight to the brief
    pg.wait_for_selector(".nr-summary", timeout=15000)
    assert "eggs" in pg.inner_text(".nr-summary") and "every 8 s" in pg.inner_text(".nr-summary")
    assert pg.input_value("#nr-name") == "egg-picker"
    pg.click('[data-a="restart"]')
    pg.fill("#nr-text", "help me sort socks")
    pg.click('[data-a="start"]')
    pg.wait_for_selector(".nr-q", timeout=15000)
    pg.click('[data-a="defaults"]')                 # "Use sensible defaults"
    pg.wait_for_selector(".nr-summary", timeout=15000)
    assert "socks" in pg.inner_text(".nr-summary") and pg.locator(".nr-assume li").count() >= 3


def test_build_now_stops_at_the_confirm_card_on_preflight_issues(page):
    pg = page
    pg.goto(pg.base)
    pg.click(".nr-headbtn")
    pg.fill("#nr-text", "pick up 200 g glass jars without breaking them")
    pg.click('[data-a="start"]')
    pg.wait_for_selector(".nr-q", timeout=15000)
    pg.click('[data-a="buildnow"]')
    pg.wait_for_selector(".nr-stopped", timeout=15000)       # not built: the issues and what they come from first
    shot(pg, "buildnow_stopped")
    assert pg.locator(".nr-build-h").count() == 0 and "Not built yet" in pg.inner_text(".nr-stopped")
    assert "Morph's reading of 'without breaking'" in pg.inner_text(".nr-issues")
    assert pg.inner_text('[data-a="build"]').strip() == "Build anyway"
    pg.click('.nr-alt:has-text("It takes a firm squeeze")')   # the assumption's option clears the issue
    pg.wait_for_function("() => document.querySelector('[data-a=\"build\"]')?.textContent.trim() === 'Build robot'", timeout=15000)
    assert pg.locator(".nr-stopped").count() == 0 and "passed" in pg.inner_text(".nr-confirm")
    pg.click('[data-a="restart"]')
    pg.fill("#nr-text", "help me sort socks")
    pg.click('[data-a="start"]')
    pg.wait_for_selector(".nr-q", timeout=15000)
    pg.click('[data-a="buildnow"]')                           # nothing to look at: straight to the build
    pg.wait_for_selector(".nr-build-h", timeout=15000)


def test_out_of_scope_and_nearest(page):
    pg = page
    pg.goto(pg.base)
    pg.click(".nr-headbtn")
    pg.fill("#nr-text", "a humanoid that folds laundry")
    pg.click('[data-a="start"]')
    pg.wait_for_selector(".nr-nearest", timeout=15000)
    shot(pg, "out_of_scope")
    assert "humanoids" in pg.inner_text(".nr-note.bad") and "socks" in pg.inner_text(".nr-nearest")
    pg.click('[data-a="nearest"]')
    pg.wait_for_selector(".nr-summary", timeout=15000)
    assert "Adjusted" in pg.inner_text(".nr-confirm") and "socks" in pg.inner_text(".nr-summary")
    pg.click('[data-a="restart"]')
    assert pg.input_value("#nr-text") == "a humanoid that folds laundry"


def test_refused(page):
    pg = page
    pg.goto(pg.base)
    pg.click(".nr-headbtn")
    pg.fill("#nr-text", "build a robot that shoots nerf darts at my brother")
    pg.click('[data-a="start"]')
    pg.wait_for_selector(".nr-note.bad", timeout=15000)
    shot(pg, "refused")
    assert "Nothing was designed or sent anywhere" in pg.inner_text(".nr-note.bad")
    assert pg.locator('[data-a="build"], [data-a="nearest"]').count() == 0


def test_refused_by_a_service_says_what_was_sent(page):
    """Only the local filter refuses before anything leaves the computer; a SubQ or Claude refusal says so honestly."""
    pg = page
    pg.goto(pg.base)
    pg.click(".nr-headbtn")
    pg.fill("#nr-text", "help me sort socks")
    with pg.expect_response(lambda r: r.url.endswith("/api/brief") and r.request.method == "POST") as resp:
        pg.click('[data-a="start"]')
    ev = [e for e in resp.value.json()["_events"] if e["type"] == "brief"][-1]
    pg.wait_for_selector(".nr-q", timeout=15000)
    ev = dict(ev, status="refused", questions=[], updated=ev["updated"] + 1, flags=dict(ev["flags"], harm="subq harm 0.91", harm_by="subq"),
              scope={"status": "out_of_scope", "notes": ["Morph doesn't design that."], "nearest": None})
    pg.evaluate("ev => window.newrobot.emit(ev)", ev)
    pg.wait_for_selector(".nr-note.bad", timeout=5000)
    t = pg.inner_text(".nr-note.bad")
    assert "Nothing was designed, and nothing more was sent to SubQ Snap (AI)." in t and "sent anywhere" not in t


def test_late_older_copy_is_ignored(page):
    """A brief event or poll reply older than what the dialog shows (a slow GET, a replayed stream) changes nothing."""
    pg = page
    pg.goto(pg.base)
    pg.click(".nr-headbtn")
    pg.fill("#nr-text", "help me sort socks")
    with pg.expect_response(lambda r: r.url.endswith("/api/brief") and r.request.method == "POST") as resp:
        pg.click('[data-a="start"]')
    old = [e for e in resp.value.json()["_events"] if e["type"] == "brief"][-1]
    assert old["status"] == "asking"
    pg.wait_for_selector(".nr-q", timeout=15000)
    pg.click('[data-a="defaults"]')
    pg.wait_for_selector(".nr-summary", timeout=15000)
    pg.evaluate("ev => window.newrobot.emit(ev)", old)
    assert pg.locator(".nr-summary").count() == 1 and pg.locator(".nr-q").count() == 0


def test_events_that_beat_the_reply(page):
    """On the Studio a first turn's events can arrive before the 202 reply names the brief: they are kept, not lost."""
    pg = page
    pg.goto(pg.base)
    pg.evaluate("""() => {
      const s = window.newrobot.studio;
      s.api.get = () => new Promise(() => {});                    // no poll to fall back on
      s.api.post = async (path, body) => {
        const r = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
        const data = await r.json();
        for (const ev of data._events || []) window.newrobot.emit(ev);   // the stream wins the race
        delete data._events;
        return { ...data, status: 202, busy: true, ok: true };
      };
    }""")
    pg.click(".nr-headbtn")
    pg.fill("#nr-text", "something to pick strawberries")
    pg.click('[data-a="start"]')
    pg.wait_for_selector(".nr-q", timeout=5000)
    assert "feasibility" in pg.inner_text(".nr-agents-mini")


def test_stylesheet_loads_once(page):
    """newrobot.css loads itself unless its section was appended to style.css (the --nr-css marker)."""
    pg = page

    def appended(route):
        resp = route.fetch()
        route.fulfill(response=resp, body=resp.text() + "\n:root { --nr-css: 1; }\n")

    pg.route("**/static/style.css", appended)
    try:
        pg.goto(pg.base)
        pg.wait_for_selector(".nr-headbtn")
        assert pg.locator('link[href$="newrobot.css"]').count() == 0
    finally:
        pg.unroute("**/static/style.css")
    pg.goto(pg.base)
    pg.wait_for_selector(".nr-headbtn")
    assert pg.locator('link[href$="newrobot.css"]').count() == 1


def test_no_script_errors(page):
    assert page.errors == []
