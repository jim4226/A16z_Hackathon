"""static/team.js in a real browser: a demo page with a mocked `studio` object (the feature contract) talks to the real
Teams handlers through a team-mode test server. Skipped when Playwright or its Chromium isn't installed.

Screenshots go to $MORPH_SHOTS (default: a temp folder) as f4_*.png. TEAM_DEMO_STATIC can point at another static
folder for style.css and fonts (to preview the integrated look)."""

import json
import os
import time

import pytest

from morph.robot.pipeline2 import params_for
from morph.spec import Design
from morph.studio.teams import auth, collab
from test_team_util import STATIC, build, make_team, members, serve

pw = pytest.importorskip("playwright.sync_api")
CHROME = os.environ.get("PLAYWRIGHT_CHROMIUM") or "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Teams demo</title>
<link rel="stylesheet" href="/static/style.css">
<style>
body { opacity: 1; }
#app { display: grid; grid-template-rows: var(--head-h) minmax(0, 1fr); height: 100vh; }
main#main { grid-template-columns: minmax(0, 1fr) var(--right-w) !important; display: grid; min-height: 0; }
#center { display: grid; place-items: center; color: var(--dim); background: radial-gradient(120% 90% at 50% 30%, #161a22 0%, #0b0d11 70%); }
.revcard b { font-family: var(--mono); }
</style></head><body><div id="app">
<header id="top"><div class="brand"><span class="logo"><svg viewBox="0 0 24 24"><path d="M5 18V6l7 6 7-6v12"/></svg></span><span>Morph <span class="sub">Studio</span></span></div>
  <div class="crumb"><span class="sep">/</span><span class="proj"><i></i><span id="project">egg-picker</span></span></div>
  <div id="revs" class="revs"></div><div class="grow"></div><div id="hdrSlot" class="badges"></div>
  <button class="btn keysbtn" type="button">Keys</button><div id="conn" class="conn"><i></i><span>Live</span></div></header>
<main id="main"><section id="center"><div>robot viewer</div></section>
  <aside id="inspector"><div class="in" id="insp"><div class="revcard"><div class="top"><b id="cur"></b><span class="grow"></span><span class="tag acc">current</span></div></div><div id="inspSlot"></div></div></aside></main>
</div><div id="toast"></div>
<script type="module">
const st = await (await fetch('/api/demo/state')).json();
const handlers = {};
const qs = (p, params) => { const q = new URLSearchParams({ ...(params || {}), project: st.project }); return `${p}?${q}`; };
const headers = { 'Content-Type': 'application/json', 'X-Morph-Client': 'studio' };
const modal = document.createElement('div'); modal.className = 'modal';
modal.addEventListener('click', (e) => { if (e.target === modal) studio.modal.close(); });
document.body.appendChild(modal);
const studio = {
  S: { project: st.project, current: st.current, revs: st.revs, tab: 'robot' },
  project: () => st.project,
  api: {
    get: (p, params) => fetch(qs(p, params), { headers }),
    post: (p, body) => fetch(qs(p), { method: 'POST', headers, body: JSON.stringify(body || {}) }),
    put: (p, blob, params) => fetch(qs(p, params), { method: 'PUT', headers, body: blob }),
    del: (p, params) => fetch(qs(p, params), { method: 'DELETE', headers }),
  },
  on: (type, fn) => { (handlers[type] = handlers[type] || []).push(fn); return () => {}; },
  toast: (t) => { const el = document.getElementById('toast'); el.textContent = t; el.classList.add('on'); clearTimeout(window._tt); window._tt = setTimeout(() => el.classList.remove('on'), 2500); window.lastToast = t; },
  addMsg() {}, esc: (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])), I: () => '',
  modal: { open(el) { modal.innerHTML = ''; modal.appendChild(el); modal.classList.add('on'); }, close() { modal.classList.remove('on'); modal.innerHTML = ''; } },
  openProject() {}, reload() {},
  mount: { header: document.getElementById('hdrSlot'), inspectorTabs: document.getElementById('inspSlot') },
};
document.getElementById('cur').textContent = st.current.rev + ': ' + st.current.label;
document.getElementById('revs').innerHTML = st.revs.map((r) => `<span class="rev${r === st.current.rev ? ' on' : ''}"><i></i>${r}</span>`).join('<span class="revlink"></span>');
const es = new EventSource('/api/events');
es.onmessage = (m) => { const ev = JSON.parse(m.data); (handlers[ev.type] || []).forEach((f) => f(ev)); window.lastEvent = ev; };
const { init } = await import('/static/team.js');
window.studio = studio;
window.team = init(studio);
document.body.dataset.ready = String(await window.team.ready);
</script></body></html>"""


def seed(home, root):
    """A small team that has already been working: a servo swap that went through review and release, a later
    manufacturing edit whose approvals went stale, open change requests, comments, a branch and a review request."""
    ids, cookies = make_team(home)
    ms = members(home)
    build(root, "r001", changes={"build.config": "pilot", "name": "egg-picker"}, by=ids["Jim"], label="first design", ts="2026-10-12T08:00:00Z")
    p1 = collab.create_proposal(root, ids["Mo"], "Swap to Dynamixel XL430", {"arm.actuator_family": "dynamixel"},
                                note="More torque margin at full reach; @Sue can we get 6 of them this month?", members=ms)
    for who in ("Ana", "Raj", "Sue", "Jim"):
        collab.decide(root, p1["review"], ms[ids[who]], "approve", members=ms)
    job = collab.apply_proposal(root, p1["id"], ids["Mo"], members=ms)
    build(root, "r002", "r001", job["changes"], by=ids["Mo"], proposed_by=ids["Mo"], label="#1 Swap to Dynamixel XL430", ts="2026-10-12T09:10:00Z")
    collab.proposal_done(root, p1["id"], rev="r002")
    collab.create_release(root, "Pilot v1", "r002", ids["Jim"], ms, home=home)
    build(root, "r003", "r002", {"fab.process": "cnc_router"}, by=ids["Raj"], label="CNC the base plate", ts="2026-10-12T10:30:00Z")
    dxl = collab.bom_key("actuator", "Dynamixel XL430-W250-T servo")
    c = collab.add_comment(root, ids["Ana"], "@Sue lead time on these? Seeed showed 3 weeks.", {"rev": "r002", "kind": "bom", "key": dxl}, members=ms)
    collab.add_comment(root, ids["Sue"], "Robotis direct has 40 in stock, 5 days.", parent=c["id"], members=ms)
    collab.add_comment(root, ids["Mo"], "Base plate pocket needs 2 mm more clearance for the XL430 horn.", {"rev": "r003", "kind": "stage", "key": "cad"}, members=ms)
    collab.add_comment(root, ids["Vic"], "Can it also pick the 70 mm jars?", {"rev": "r003"}, members=ms)
    collab.create_proposal(root, ids["Vic"], "Add a wrist camera", {"electronics.extras": "wrist_camera"},
                           note="So the line lead can see misses from the dashboard.", members=ms)
    p3 = collab.create_proposal(root, ids["Ana"], "Custom controller PCB", {"electronics.config": "custom_pcb"}, members=ms)
    collab.decide(root, p3["review"], ms[ids["Raj"]], "request_changes", note="PCBA lead time breaks the pilot date", members=ms)
    collab.create_branch(root, "pcb-trial", ids["Ana"], "r002")
    rv = collab.request_review(root, ids["Raj"], rev="r003", members=ms)
    collab.decide(root, rv["id"], ms[ids["Sue"]], "approve", members=ms)
    from morph.studio.teams import activity
    activity.PRESENCE.touch("egg-picker", ids["Ana"], tab="elec", rev="r003")
    activity.PRESENCE.touch("egg-picker", ids["Raj"], tab="mfg", rev="r002", now=time.time() - 400)
    activity.notify(home, ids["Jim"], "review_request", "Raj asks for your review of r003: Manufacturing, Documentation", project="egg-picker", by=ids["Raj"])
    activity.notify(home, ids["Jim"], "mention", "Mo mentioned you on r003 (Robot CAD): check the base plate", project="egg-picker", by=ids["Mo"])
    return ids, cookies


def state_for(root, rev):
    info = collab.rev_info(root, rev)
    with open(os.path.join(root, rev, "results.json"), encoding="utf-8") as fh:
        res = json.load(fh)
    d = Design.from_dict(info["design"])
    return {"rev": rev, "label": info["label"], "design": info["flat"], "params": params_for(d), "bom": res.get("bom"),
            "summary": res.get("summary"), "nodes": [["sizing", "Sizing"], ["gripper", "Gripper + grasp"], ["electronics", "Electronics plan"],
                                                     ["cad", "Robot CAD"], ["sim", "Simulation"], ["wiring", "Schematic · PCB · FW"],
                                                     ["manufacturing", "Manufacturing"], ["bom", "BOM + sourcing"], ["manual", "Assembly manual"]]}


@pytest.fixture
def demo(tmp_path):
    auth.THROTTLE.clear()
    home = str(tmp_path / "projects")
    root = os.path.join(home, "egg-picker")
    ids, cookies = seed(home, root)
    static = os.environ.get("TEAM_DEMO_STATIC") or STATIC

    def extra(h, path, member):
        if path == "/":
            body, ctype = PAGE.encode(), "text/html; charset=utf-8"
        elif path == "/api/demo/state":
            body, ctype = json.dumps({"project": "egg-picker", "revs": collab.revs(root), "current": state_for(root, "r003")}).encode(), "application/json"
        elif path in ("/static/style.css",) or path.startswith("/static/vendor/fonts/"):
            full = os.path.join(static, *path[len("/static/"):].split("/"))
            if not os.path.isfile(full):
                return False
            with open(full, "rb") as fh:
                body = fh.read()
            ctype = "text/css" if path.endswith(".css") else "font/woff2"
        else:
            return False
        h.send_response(200)
        h.send_header("Content-Type", ctype)
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        h.wfile.write(body)
        return True

    httpd, base, room = serve(home, root, extra=extra)
    shots = os.environ.get("MORPH_SHOTS") or str(tmp_path / "shots")
    os.makedirs(shots, exist_ok=True)
    with pw.sync_playwright() as p:
        try:  # this machine's Chromium, else Playwright's own download
            browser = p.chromium.launch(executable_path=CHROME if os.path.exists(CHROME) else None, args=["--no-sandbox"])
        except Exception as exc:  # noqa: BLE001
            httpd.shutdown()
            httpd.server_close()
            pytest.skip(f"no Chromium for Playwright ({exc.__class__.__name__})")
        yield {"home": home, "root": root, "ids": ids, "base": base, "browser": browser, "shots": shots, "room": room}
        browser.close()
    httpd.shutdown()
    httpd.server_close()


def _signed_in(d, who, w=1440, h=900):
    ctx = d["browser"].new_context(viewport={"width": w, "height": h}, device_scale_factor=1)
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    link = auth.create_link(d["ids"][who], home=d["home"])["path"]
    page.goto(d["base"] + link)
    page.click("button[type=submit]")
    page.wait_for_selector("body[data-ready='true']", timeout=15000)
    page.wait_for_selector(".tm-th, .tm-empty", timeout=10000)
    return ctx, page, errors


def test_join_page_and_signed_out_page_render(demo):
    ctx = demo["browser"].new_context(viewport={"width": 900, "height": 640})
    page = ctx.new_page()
    inv = auth.create_invite(demo["ids"]["Jim"], "editor", ["electronics", "manufacturing"], "Priya", home=demo["home"])
    page.goto(demo["base"] + inv["path"])
    assert page.inner_text("h1") == "Join R Cubed on Morph Studio"
    assert page.input_value("input[name=name]") == "Priya"
    page.screenshot(path=os.path.join(demo["shots"], "f4_join.png"))
    page.fill("input[name=name]", "Priya Natarajan")
    page.click("button[type=submit]")
    page.wait_for_selector("body[data-ready='true']", timeout=15000)
    assert "Priya Natarajan" in page.evaluate("window.team.me.member.name")
    ctx2 = demo["browser"].new_context(viewport={"width": 900, "height": 640})
    p2 = ctx2.new_page()
    p2.goto(demo["base"] + "/")
    assert "shared with a team" in p2.inner_text("main")
    ctx.close()
    ctx2.close()


def test_owner_sees_presence_comments_review_gate_changes_activity_and_share(demo):
    ctx, page, errors = _signed_in(demo, "Jim")
    shots = demo["shots"]
    page.wait_for_selector(".tm-avs .tm-av")
    assert page.inner_text(".tm-role").lower() == "owner"
    assert page.locator(".tm-avs .tm-av").count() == 3 and page.locator(".tm-avs .tm-av.idle").count() == 1
    assert page.inner_text(".tm-badge") == "2"
    threads = page.locator(".tm-th")
    assert threads.count() == 3                                  # 2 on r003 + the BOM thread carried from r002
    assert "from r002" in page.inner_text(".tm-insp")
    page.screenshot(path=os.path.join(shots, "f4_comments.png"))

    page.select_option(".tm-compose select", "stage:wiring")
    page.fill(".tm-compose textarea", "@Ana does the new bus need a terminator? @Ra")
    page.wait_for_selector(".tm-mention button")
    page.click(".tm-mention button")
    assert page.input_value(".tm-compose textarea").endswith("@Raj ")
    page.click("[data-a=post]")
    page.wait_for_function("document.querySelectorAll('.tm-th').length === 4")
    assert "Schematic · PCB · FW" in page.inner_text(".tm-insp")

    page.click(".tm-tabs [data-tab=review]")
    page.wait_for_selector(".tm-gate")
    gate = page.inner_text(".tm-gate")
    assert "needs 2 approvals: pilot" in gate and "Manufacturing" in gate and "Documentation" in gate
    assert page.is_disabled("[data-a=release]")
    approve = page.locator("[data-decide=approve]")
    assert approve.count() == 1 and approve.inner_text().strip() == "Approve as Manufacturing, Sourcing and Documentation"
    page.screenshot(path=os.path.join(shots, "f4_review.png"))
    approve.click()
    page.wait_for_function("document.querySelector('.tm-gate') && document.querySelector('.tm-gate').classList.contains('ok')", timeout=10000)
    assert not page.is_disabled("[data-a=release]")
    page.click("[data-a=release]")
    page.fill(".tm-relbox input", "Pilot v2")
    page.click(".tm-relbox button")
    page.wait_for_selector(".tm-rels")
    assert "Pilot v2" in page.inner_text(".tm-rels")
    page.screenshot(path=os.path.join(shots, "f4_released.png"))

    page.click(".tm-tabs [data-tab=changes]")
    page.wait_for_selector(".tm-pr")
    cards = page.inner_text(".tm-sec")
    assert "#2" in cards and "Add a wrist camera" in cards and "needs:" in cards and "Electronics: Ana" in cards
    page.screenshot(path=os.path.join(shots, "f4_changes.png"))

    page.click(".tm-tabs [data-tab=activity]")
    page.wait_for_selector(".tm-feed")
    feed = page.inner_text(".tm-feed")
    assert "Jim released r003 as Pilot v2 (pilot)" in feed and "Mo made r002" in feed
    page.click("[data-tm=inbox]")
    page.wait_for_selector(".tm-pop .tm-note")
    page.wait_for_timeout(400)  # the menu fades in
    page.screenshot(path=os.path.join(shots, "f4_activity_inbox.png"))
    page.click(".tm-pop [data-a=read]")
    page.wait_for_function("!document.querySelector('.tm-badge').classList.contains('on')")

    page.click("[data-tm=share]")
    page.wait_for_selector(".tm-share-dlg .tm-mem")
    assert page.locator(".tm-share-dlg .tm-mem").count() == 6
    page.click(".tm-share-dlg .tm-chk:has-text('Electronics')")
    page.click(".tm-share-dlg .tm-chk:has-text('Generalist')")
    page.fill(".tm-share-dlg input[name=hint]", "Priya")
    page.click("[data-a=invite]")
    page.wait_for_selector("[data-out=invite] input")
    link = page.input_value("[data-out=invite] input")
    assert link.startswith(demo["base"] + "/?invite=mi_")
    page.screenshot(path=os.path.join(shots, "f4_share.png"))
    assert auth.invite_status(link.split("=", 1)[1], home=demo["home"])["disciplines"] == ["electronics", "generalist"]
    assert errors == [], errors
    ctx.close()


def test_the_release_checklist_says_how_much_of_the_unit_cost_is_tooling(demo):
    # a production revision at quantity 1 read "$77742 per unit": nearly all of it the molds, paid once
    p = os.path.join(demo["root"], "r003", "results.json")
    with open(p, encoding="utf-8") as fh:
        res = json.load(fh)
    res["summary"].update(unit_cost=77742.0, tooling_cost=72971.0)
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(res, fh)
    ctx, page, errors = _signed_in(demo, "Jim")
    page.click(".tm-tabs [data-tab=review]")
    page.wait_for_selector(".tm-gate .tm-ck")
    assert "$77742 per unit, incl. $72,971 tooling over 1 unit (estimated)" in page.inner_text(".tm-gate .tm-ck")
    assert not errors, errors


def test_a_viewer_proposes_a_change_and_sees_who_it_will_ask(demo):
    ctx, page, errors = _signed_in(demo, "Vic", w=1440, h=900)
    assert page.inner_text(".tm-role").lower() == "viewer"
    assert page.inner_text(".tm-share").strip() == "Team"
    page.click(".tm-tabs [data-tab=changes]")
    page.wait_for_selector(".tm-new")
    assert page.locator("[data-apply]").count() == 0           # viewers can't apply
    page.click(".tm-new summary")
    page.select_option("[data-a=param]", "arm.actuator_family")
    page.select_option(".tm-val select", "hobby_pwm")
    page.click("[data-a=add]")
    page.fill("[data-a=title]", "Cheaper hobby servos for the classroom kit")
    page.click("[data-a=preview]")
    page.wait_for_selector(".tm-preview .tm-chip")
    prev = page.inner_text(".tm-preview")
    assert "Recomputes 9 of 9 stages: Sizing" in prev and "Electronics: Ana" in prev and "Mechanical: Mo" in prev
    assert " ".join(page.inner_text(".tm-draft").split()) == "Arm · Actuators: Hobby PWM"   # names, as the Parameters panel
    page.screenshot(path=os.path.join(shots_dir(demo), "f4_viewer_propose.png"))
    page.click("[data-a=submit]")
    page.wait_for_function("[...document.querySelectorAll('.tm-pr b')].some((b) => b.textContent.includes('classroom'))")
    p = collab.proposals(demo["root"])[0]
    assert p["by"] == demo["ids"]["Vic"] and p["changes"] == {"arm.actuator_family": "hobby_pwm"}
    page.click(".tm-new summary")                                # a change to the manual only: documentation reviews it
    page.select_option("[data-a=stage]", "manual")
    page.fill("[data-a=title]", "Torque spec for the base screws in step 4")
    page.click("[data-a=preview]")
    page.wait_for_selector(".tm-preview .tm-chip")
    assert page.inner_text(".tm-preview .tm-aff") == "Recomputes 1 of 9 stages: Assembly manual"
    assert [c.strip() for c in page.locator(".tm-preview .tm-chip").all_inner_texts()] == ["Documentation: Jim (generalist)"]
    page.click("[data-a=submit]")
    page.wait_for_function("[...document.querySelectorAll('.tm-pr b')].some((b) => b.textContent.includes('Torque spec'))")
    doc = collab.proposals(demo["root"])[0]
    assert doc["disciplines"] == ["documentation"] and doc["changes"] == {}
    page.click(".tm-tabs [data-tab=review]")
    page.wait_for_selector(".tm-gate")
    assert page.locator("[data-decide]").count() == 0 and page.locator("[data-a=release]").count() == 0
    assert errors == [], errors
    ctx.close()


def test_the_inspector_fits_the_narrow_column(demo):
    ctx, page, errors = _signed_in(demo, "Ana", w=1300, h=820)
    for tab in ("comments", "review", "changes", "activity"):
        page.click(f".tm-tabs [data-tab={tab}]")
        page.wait_for_timeout(250)
        over = page.evaluate("(() => { const i = document.querySelector('#inspector'); return i.scrollWidth - i.clientWidth; })()")
        assert over <= 1, (tab, over)
    page.click(".tm-tabs [data-tab=review]")
    page.wait_for_selector(".tm-gate")
    page.wait_for_timeout(300)  # the pill highlight transitions
    page.screenshot(path=os.path.join(shots_dir(demo), "f4_narrow_review.png"))
    page.click(".tm-tabs [data-tab=changes]")
    page.wait_for_selector(".tm-pr [data-decide=request_changes]")
    own = page.locator(".tm-pr", has_text="Custom controller PCB")          # Ana's own request, at pilot: no buttons, a reason
    assert own.locator("[data-decide]").count() == 0 and "You made this change" in own.inner_text()
    card = page.locator(".tm-pr", has=page.locator("[data-decide=request_changes]")).first
    title = card.locator(".tm-prh b").inner_text()
    card.locator("[data-decide=request_changes]").click()
    page.fill(".tm-ask textarea", "Needs the PCBA lead time first")
    page.click(".tm-ask [data-go]")
    page.wait_for_selector(".tm-pr .tm-done")
    assert "You requested changes" in page.locator(".tm-pr", has_text=title).inner_text()
    assert errors == [], errors
    ctx.close()


def shots_dir(d):
    return d["shots"]


def test_live_updates_never_take_what_you_are_writing(demo):
    """A teammate's comment or change request redraws the panel: a comment being typed, and a change request being
    written (its form, title and preview), stay as they were; the new item shows once you leave the field."""
    ctx, page, errors = _signed_in(demo, "Ana")
    room, root, ids, ms = demo["room"], demo["root"], demo["ids"], members(demo["home"])
    box = ".tm-compose textarea"
    page.click(box)
    page.keyboard.type("Half a comment about the jaw")
    collab.add_comment(root, ids["Raj"], "Raj checked the jaw", {"rev": "r003"}, members=ms)
    room.hub.send({"type": "comment", "action": "add", "project": "egg-picker"})
    time.sleep(1.5)                                                   # well past the 250 ms the panel waits for events
    assert page.input_value(box) == "Half a comment about the jaw" and "Raj checked the jaw" not in page.inner_text(".tm-body")
    page.click(".tm-filter b")                                        # done typing: the update comes in
    page.wait_for_function("document.querySelector('.tm-body').innerText.includes('Raj checked the jaw')")
    assert page.input_value(box) == "Half a comment about the jaw"    # and the comment being written is still there
    page.click(".tm-tabs [data-tab=changes]")
    page.wait_for_selector(".tm-new")
    page.click(".tm-new summary")
    page.select_option("[data-a=param]", "build.quantity")
    page.fill(".tm-val input", "25")
    page.click("[data-a=add]")
    page.fill("[data-a=title]", "A batch of 25")
    page.click("[data-a=preview]")
    page.wait_for_selector(".tm-preview .tm-chip")
    chips = page.locator(".tm-preview .tm-chip").all_inner_texts()
    collab.create_proposal(root, ids["Raj"], "Thicker base plate", {"finger.thickness_mm": 7.0}, members=ms)
    room.hub.send({"type": "proposal", "action": "open", "project": "egg-picker"})
    page.wait_for_function("[...document.querySelectorAll('.tm-pr b')].some((b) => b.textContent.includes('Thicker base plate'))")
    assert page.locator(".tm-preview .tm-chip").all_inner_texts() == chips and chips
    assert page.input_value("[data-a=title]") == "A batch of 25" and page.locator(".tm-new[open]").count() == 1
    assert " ".join(page.inner_text(".tm-draft").split()) == "Manufacturing · Quantity: 25 units"
    assert errors == []
    ctx.close()


def _held(page, path, method="GET"):
    """Holds this page's GETs (or `method`s) of `path` (the server sees nothing yet) until release() lets them through;
    wait() returns once one is held (the request reaches the test a moment after the click that sent it)."""
    from urllib.parse import urlparse
    held = []

    def handle(route):
        if route.request.method == method and urlparse(route.request.url).path == path:
            held.append(route)
        else:
            route.continue_()

    def match(u):
        return urlparse(u).path == path

    page.route(match, handle)

    def release():
        while held:
            held.pop(0).continue_()
        page.unroute(match, handle)

    def wait():
        for _ in range(400):
            if held:
                return
            page.wait_for_timeout(25)
        raise AssertionError(f"no {method} of {path} was held")
    return held, release, wait


def _late(page, path):
    """The page's next GET of `path` is answered by the server at once, but the answer reaches the page only when
    deliver() is called: an older answer that lands after a newer one. wait() returns once it is answered."""
    from urllib.parse import urlparse
    late = []

    def handle(route):
        if route.request.method == "GET" and not late:
            late.append((route, route.fetch()))
        else:
            route.continue_()

    def match(u):
        return urlparse(u).path == path

    page.route(match, handle)

    def deliver():
        route, answer = late[0]
        route.fulfill(response=answer)
        page.unroute(match, handle)

    def wait():
        for _ in range(400):
            if late:
                return
            page.wait_for_timeout(25)
        raise AssertionError(f"no GET of {path} was answered")
    return deliver, wait


def test_the_review_tabs_own_load_never_takes_a_release_name_being_typed(demo):
    """Review is drawn at once from the gate the page already has, then again when the tab's own load lands. On a slow
    link someone can click Release and type the name in between: the box, the name and the focus stay, and it releases."""
    ctx, page, errors = _signed_in(demo, "Jim")
    page.click(".tm-tabs [data-tab=review]")
    page.click("[data-decide=approve]")
    page.wait_for_function("document.querySelector('.tm-gate')?.classList.contains('ok')", timeout=10000)
    page.click(".tm-tabs [data-tab=comments]")
    page.wait_for_selector(".tm-compose")
    page.wait_for_timeout(800)                                        # the approval's own redraws are done
    held, release, wait = _held(page, "/api/reviews")
    page.click(".tm-tabs [data-tab=review]")                          # the cached gate: Release is enabled at once
    page.click("[data-a=release]")
    page.fill(".tm-relbox input", "Pilot v2")
    wait()
    assert len(held) == 1, held                                       # the tab's own load is still on its way
    with page.expect_response(lambda r: "/api/releases" in r.url):   # it lands (reviews, then releases)
        release()
    page.wait_for_timeout(400)
    assert page.locator(".tm-relbox input").count() == 1, "the release box was redrawn away"
    assert page.input_value(".tm-relbox input") == "Pilot v2"
    assert page.evaluate("document.activeElement === document.querySelector('.tm-relbox input')")
    page.click(".tm-relbox button")
    page.wait_for_selector(".tm-rels")
    assert "Pilot v2" in page.inner_text(".tm-rels")
    assert errors == [], errors
    ctx.close()


def test_shares_own_load_never_swaps_a_switch_under_the_owner(demo):
    """Share opens at once with the team the page has, then is drawn again when its own load answers (someone joined
    since). Only what changed is replaced: the policy switch the owner reaches for stays the one on screen, and works."""
    ctx, page, errors = _signed_in(demo, "Jim")
    page.click("[data-tm=share]")
    page.wait_for_selector(".tm-share-dlg .tm-mem")
    page.click(".tm-share-dlg .dlg-f [data-a=close]")
    inv = auth.create_invite(demo["ids"]["Jim"], "editor", ["electronics"], "Priya", home=demo["home"])
    auth.redeem_invite(inv["code"], "Priya", home=demo["home"])      # joins while Jim's page sits there
    held, release, wait = _held(page, "/api/team")
    page.click("[data-tm=share]")
    wait()
    assert len(held) == 1, held                                       # the dialog's own load is still on its way
    first = page.query_selector(".tm-share-dlg [data-pol=unreleased_downloads]")   # what the owner could click now
    assert first is not None and page.locator(".tm-share-dlg .tm-mem:has-text('Priya')").count() == 0
    release()
    page.wait_for_selector(".tm-share-dlg .tm-mem:has-text('Priya')")            # drawn again, with Priya a member
    page.wait_for_timeout(300)
    assert first.evaluate("x => x.isConnected"), "the policy switch was redrawn under the owner"
    with page.expect_response(lambda r: "/api/team/policy" in r.url):
        page.locator(".tm-share-dlg label.tm-sw", has=page.locator("[data-pol=unreleased_downloads]")).locator(".switch").click()
    pol = page.evaluate("fetch('/api/team?project=egg-picker', {headers: {'X-Morph-Client': 'studio'}})"
                        ".then((r) => r.json()).then((d) => d.policy.unreleased_downloads)")
    assert pol == "owner" and not page.is_checked(".tm-share-dlg [data-pol=unreleased_downloads]")
    assert errors == [], errors
    ctx.close()


def test_review_never_offers_release_from_another_revisions_gate(demo):
    """The page moved to another revision (an older one opened, a build landed) while the panel showed another tab:
    Review is not drawn from the gate it has for the revision before, which said 'ready to release'."""
    ctx, page, errors = _signed_in(demo, "Jim")
    page.click(".tm-tabs [data-tab=review]")
    page.click("[data-decide=approve]")
    page.wait_for_function("document.querySelector('.tm-gate')?.classList.contains('ok')", timeout=10000)
    page.click(".tm-tabs [data-tab=comments]")
    page.wait_for_selector(".tm-compose")
    page.wait_for_timeout(800)                                        # the approval's own redraws are done
    page.evaluate("window.studio.S.current = {...window.studio.S.current, rev: 'r001'}")
    held, release, wait = _held(page, "/api/reviews")
    page.click(".tm-tabs [data-tab=review]")
    wait()
    assert len(held) == 1, held                                       # r001's gate is still on its way
    assert page.locator(".tm-gate").count() == 0 and page.locator("[data-a=release]").count() == 0
    with page.expect_response(lambda r: "/api/releases" in r.url):
        release()
    page.wait_for_selector(".tm-gate")
    assert "r001" in page.inner_text(".tm-gate .tm-gh") and page.locator(".tm-gate.ok").count() == 0
    assert page.is_disabled("[data-a=release]")
    assert errors == [], errors
    ctx.close()


def test_a_share_switch_shows_what_the_studio_saved(demo):
    """A policy change the Studio refuses is put back at once, and one another owner saved shows when the dialog is
    drawn again: a section kept across a redraw never shows a value the Studio doesn't have."""
    from urllib.parse import urlparse
    ctx, page, errors = _signed_in(demo, "Jim")
    page.click("[data-tm=share]")
    sw = page.locator(".tm-share-dlg label.tm-sw", has=page.locator("[data-pol=unreleased_downloads]")).locator(".switch")
    box = ".tm-share-dlg [data-pol=unreleased_downloads]"
    page.wait_for_selector(box, state="attached")                     # the switch's own box is drawn under its knob
    assert page.is_checked(box)                                       # editors may download drafts (warn)

    def refuse(route):
        route.fulfill(status=500, content_type="application/json", body='{"error": "The Studio could not save it"}')

    def is_policy(u):
        return urlparse(u).path == "/api/team/policy"

    page.route(is_policy, refuse)
    with page.expect_response(lambda r: is_policy(r.url)):
        sw.click()
    page.wait_for_function(f"document.querySelector('{box}').checked === true", timeout=5000)   # put back at once
    page.unroute(is_policy, refuse)
    with page.expect_response(lambda r: is_policy(r.url)):           # saved this time
        sw.click()
    assert not page.is_checked(box)
    page.evaluate("fetch('/api/team/policy?project=egg-picker', {method: 'POST', headers: {'X-Morph-Client': 'studio', "
                  "'Content-Type': 'application/json'}, body: JSON.stringify({values: {unreleased_downloads: 'warn'}})})"
                  ".then((r) => r.json())")                         # another owner turns it back
    page.evaluate("window.team.loadTeam()")                           # the dialog is drawn again from the Studio
    page.wait_for_function(f"document.querySelector('{box}').checked === true", timeout=5000)
    assert errors == [], errors
    ctx.close()


def _share(page):
    """Opens Share and waits for its own load (the next draws read the same as the dialog on screen)."""
    with page.expect_response(lambda r: r.url.split("?")[0].endswith("/api/team")):
        page.click("[data-tm=share]")
    page.wait_for_selector(".tm-share-dlg .tm-mem")
    page.evaluate("window.team.loadTeam()")


def test_an_invite_link_never_takes_the_focus_or_the_scroll_when_share_is_drawn_again(demo):
    """The link Create invite link made is selected, ready to copy. A redraw after that (a role change, a member event,
    the dialog's own late load) keeps it as it is: it doesn't take the focus, or scroll the dialog back to the top."""
    ctx, page, errors = _signed_in(demo, "Jim", h=640)
    _share(page)
    dlg, link = ".tm-share-dlg", ".tm-share-dlg [data-out=invite] input"
    page.fill(f"{dlg} input[name=hint]", "Priya")
    page.click("[data-a=invite]")
    page.wait_for_selector(link)
    assert page.evaluate(f"document.activeElement === document.querySelector('{link}')")   # made: ready to copy
    url = page.input_value(link)
    assert url.startswith(demo["base"] + "/?invite=mi_")
    page.evaluate(f"document.activeElement.blur(); document.querySelector('{dlg}').scrollTop = 10000")
    top = page.evaluate(f"document.querySelector('{dlg}').scrollTop")
    assert top > 100, top                                             # scrolled down to the policy and You
    shown = page.query_selector(link)
    page.evaluate("window.team.loadTeam()")                           # drawn again, as a teammate's update draws it
    assert page.evaluate(f"document.querySelector('{dlg}').scrollTop") == top, "the dialog scrolled back to the link"
    assert page.evaluate("document.activeElement === document.body"), "the link took the focus"
    assert shown.evaluate("x => x.isConnected") and page.input_value(link) == url   # kept, still shown
    assert errors == [], errors
    ctx.close()


def test_a_token_or_sign_in_link_shown_in_share_stays_until_the_dialog_closes(demo):
    """A script token is shown once (Morph keeps only its hash): a redraw of Share keeps it on screen, and so a sign-in
    link for another device. Revoking the token shown takes it away; closing the dialog takes the link away."""
    ctx, page, errors = _signed_in(demo, "Ana")
    _share(page)
    out = ".tm-share-dlg [data-out=you] input"
    page.click(".tm-share-dlg [data-a=token]")
    page.wait_for_selector(out)
    token = page.input_value(out)
    assert token and page.evaluate(f"document.activeElement === document.querySelector('{out}')")
    page.evaluate("window.team.loadTeam()")                           # a teammate's update draws the dialog again
    assert page.locator(out).count() == 1 and page.input_value(out) == token, "the token was wiped"
    page.click(".tm-share-dlg [data-tok]")                            # revoked: it no longer works, so it goes
    page.wait_for_function(f"!document.querySelector('{out}')")
    page.click(".tm-share-dlg [data-a=device]")
    page.wait_for_selector(out)
    link = page.input_value(out)
    assert link.startswith(demo["base"] + "/?link=ml_"), link
    page.evaluate("window.team.loadTeam()")
    assert page.locator(out).count() == 1 and page.input_value(out) == link, "the sign-in link was wiped"
    page.click(".tm-share-dlg .dlg-f [data-a=close]")
    _share(page)
    assert page.locator(out).count() == 0                             # shown once: not again after closing
    assert errors == [], errors
    ctx.close()


def test_shares_redraw_leaves_the_focus_and_the_selection_where_they_are(demo):
    """A redraw keeps the focus on the discipline chip it was on (not the first chip of the row) and a selection in
    Name as it was, whether that section was kept or drawn again."""
    ctx, page, errors = _signed_in(demo, "Jim")
    _share(page)
    chips, hint = ".tm-share-dlg input[name=inv]", ".tm-share-dlg input[name=hint]"
    third = page.evaluate(f"(() => {{ const c = document.querySelectorAll('{chips}')[2]; c.focus(); return c.value; }})()")
    focus = "(() => { const a = document.activeElement; return [a.name, a.value, a.checked]; })()"
    page.evaluate("window.team.loadTeam()")                           # nothing changed: every section is kept
    assert page.evaluate(focus) == ["inv", third, False]
    page.keyboard.press("Space")                                      # ticked: the invite form reads differently...
    page.evaluate("window.team.loadTeam()")                           # ...so it is drawn again
    assert page.evaluate(focus) == ["inv", third, True]
    page.fill(hint, "Priya Natarajan")
    page.evaluate(f"document.querySelector('{hint}').setSelectionRange(0, 5)")
    sel = "(() => { const a = document.activeElement; return [a.name, a.selectionStart, a.selectionEnd]; })()"
    page.evaluate("window.team.loadTeam()")                           # drawn again with the name typed
    assert page.evaluate(sel) == ["hint", 0, 5]
    page.evaluate("window.team.loadTeam()")                           # kept
    assert page.evaluate(sel) == ["hint", 0, 5]
    assert errors == [], errors
    ctx.close()


def test_comments_on_another_revision_show_while_you_write_one(demo):
    """The page moved to another revision: Comments is drawn at once with a placeholder while that revision's comments
    load. Someone who starts writing meanwhile sees them when they land, and keeps their words and the focus."""
    ctx, page, errors = _signed_in(demo, "Ana")
    page.click(".tm-tabs [data-tab=activity]")
    page.wait_for_selector(".tm-feed")
    page.evaluate("window.studio.S.current = {...window.studio.S.current, rev: 'r002'}")
    _, release, wait = _held(page, "/api/comments")
    page.click(".tm-tabs [data-tab=comments]")
    wait()
    assert "Loading the comments on r002" in page.inner_text(".tm-body")
    page.click(".tm-compose textarea")
    page.keyboard.type("While they load")
    composer = page.query_selector(".tm-compose textarea")
    with page.expect_response(lambda r: "/api/comments" in r.url):
        release()
    page.wait_for_function("!document.querySelector('.tm-body').innerText.includes('Loading the comments')", timeout=5000)
    assert page.inner_text(".tm-filter b") == "1 thread on r002" and "lead time on these" in page.inner_text(".tm-threads")
    assert composer.evaluate("x => x.isConnected && x === document.activeElement"), "the composer was redrawn"
    assert page.input_value(".tm-compose textarea") == "While they load"
    assert errors == [], errors
    ctx.close()


def test_a_teammates_update_waits_for_a_click_on_the_panel(demo):
    """A teammate's update waits while you write a comment, and is drawn once you leave the field. Pressing a button
    of the panel leaves it: the update waits for the click too, so the button clicked is the one that answers."""
    ctx, page, errors = _signed_in(demo, "Ana")
    room, root, ids, ms = demo["room"], demo["root"], demo["ids"], members(demo["home"])
    page.click(".tm-compose textarea")
    page.keyboard.type("Half a comment")
    collab.add_comment(root, ids["Raj"], "Raj checked the jaw", {"rev": "r003"}, members=ms)
    with page.expect_response(lambda r: "/api/comments" in r.url):
        room.hub.send({"type": "comment", "action": "add", "project": "egg-picker"})
    page.wait_for_timeout(100)
    assert "Raj checked the jaw" not in page.inner_text(".tm-body")  # waits for the composer
    box = page.locator(".tm-th [data-reply]").first.bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.down()                                                 # the composer has lost the focus...
    page.wait_for_timeout(1500)                                       # ...and the panel checks again every second
    page.mouse.up()
    page.wait_for_selector(".tm-replybox textarea", timeout=3000)
    assert page.evaluate("document.activeElement === document.querySelector('.tm-replybox textarea')")
    assert page.input_value(".tm-compose textarea") == "Half a comment"
    assert errors == [], errors
    ctx.close()


def test_an_older_answer_about_the_team_never_draws_over_a_newer_one(demo):
    """Two loads of the team in flight (Share opened, closed and opened again; a member event) can answer out of
    order: the dialog shows the newer answer, not whichever came last."""
    ctx, page, errors = _signed_in(demo, "Jim")
    _share(page)
    deliver, wait = _late(page, "/api/team")
    page.evaluate("() => { window.older = window.team.loadTeam(); }")  # answered now, before Priya joins...
    wait()
    inv = auth.create_invite(demo["ids"]["Jim"], "editor", ["electronics"], "Priya", home=demo["home"])
    auth.redeem_invite(inv["code"], "Priya", home=demo["home"])
    page.evaluate("window.team.loadTeam()")                            # ...this one after: Priya is a member
    priya = page.locator(".tm-share-dlg .tm-mem:has-text('Priya')")
    assert priya.count() == 1
    deliver()
    page.evaluate("window.older")                                      # the older answer lands last
    assert priya.count() == 1, "an older answer drew the team without Priya"
    assert "Priya" in page.evaluate("window.team.data.team.members.map((m) => m.name)")
    assert errors == [], errors
    ctx.close()


def test_your_own_actions_answer_never_takes_a_release_or_branch_name_being_typed(demo):
    """Review is drawn again when your approval (or a branch you created) is answered. A branch or release name typed
    while it was on its way stays, with the focus, and creates the branch or the release."""
    from urllib.parse import urlparse
    ctx, page, errors = _signed_in(demo, "Jim")
    page.click(".tm-tabs [data-tab=review]")
    page.wait_for_selector("[data-decide=approve]")
    _, release, wait = _held(page, "/api/reviews/decide", method="POST")
    page.click("[data-decide=approve]")
    wait()
    page.click("[data-a=newbranch]")
    page.keyboard.type("servo-trial")
    release()
    page.wait_for_selector(".tm-gate.ok", timeout=10000)              # the approval's answer drew the tab again
    assert page.locator(".tm-branchbox input").count() == 1, "the branch box was redrawn away"
    assert page.input_value(".tm-branchbox input") == "servo-trial"
    assert page.evaluate("document.activeElement === document.querySelector('.tm-branchbox input')")
    page.click("[data-a=release]")
    page.keyboard.type("Pilot v2")
    with page.expect_response(lambda r: urlparse(r.url).path == "/api/branches" and r.request.method == "GET"):
        page.click(".tm-branchbox button")                            # Create: its answer draws the tab again
    page.wait_for_selector("[data-a=branch] option[value=servo-trial]", state="attached")
    assert page.locator(".tm-relbox input").count() == 1, "the release box was redrawn away"
    assert page.input_value(".tm-relbox input") == "Pilot v2"
    page.click(".tm-relbox button")
    page.wait_for_selector(".tm-rels")
    assert "Pilot v2" in page.inner_text(".tm-rels") and page.locator(".tm-relbox input").count() == 0
    assert errors == [], errors
    ctx.close()


def test_your_own_actions_answer_never_takes_a_request_for_changes_or_a_reply_being_written(demo):
    """Closing a change request, or resolving a thread, draws the tab again when the Studio answers: a request for
    changes or a reply written meanwhile stays, with the focus and the @name picked, and is sent."""
    ctx, page, errors = _signed_in(demo, "Jim")
    page.click(".tm-tabs [data-tab=changes]")
    page.wait_for_selector(".tm-pr [data-decide=request_changes]")
    ask = page.locator(".tm-pr", has=page.locator("[data-decide=request_changes]")).first
    title = ask.locator(".tm-prh b").inner_text()
    other = page.locator(".tm-pr", has=page.locator("[data-close]")).filter(has_not_text=title).first
    gone = other.locator(".tm-prh b").inner_text()
    _, release, wait = _held(page, "/api/proposals/close", method="POST")
    other.locator("[data-close]").click()
    wait()
    ask.locator("[data-decide=request_changes]").click()
    page.keyboard.type("Needs the PCBA lead time first")
    release()
    page.wait_for_function(f"[...document.querySelectorAll('.tm-pr.closed b')].some((b) => b.textContent === {json.dumps(gone)})")
    assert page.locator(".tm-ask textarea").count() == 1, "the request for changes was redrawn away"
    assert page.input_value(".tm-ask textarea") == "Needs the PCBA lead time first"
    assert page.evaluate("document.activeElement === document.querySelector('.tm-ask textarea')")
    page.click(".tm-ask [data-go]")
    page.wait_for_function(f"[...document.querySelectorAll('.tm-pr')].some((c) => c.textContent.includes({json.dumps(title)}) "
                           "&& c.textContent.includes('You requested changes'))")
    assert page.locator(".tm-ask").count() == 0
    page.click(".tm-tabs [data-tab=comments]")
    page.wait_for_selector(".tm-th")
    n = page.locator(".tm-th").count()
    _, release, wait = _held(page, "/api/comments/resolve", method="POST")
    page.locator(".tm-th").nth(0).locator("[data-resolve]").click()
    wait()
    page.locator(".tm-th").nth(1).locator("[data-reply]").click()
    page.keyboard.type("@Su")
    page.click(".tm-replybox .tm-mention button")
    release()
    page.wait_for_function(f"document.querySelectorAll('.tm-th').length === {n - 1}")   # resolved: hidden
    assert page.locator(".tm-replybox textarea").count() == 1, "the reply was redrawn away"
    assert page.input_value(".tm-replybox textarea") == "@Sue "
    assert page.evaluate("document.activeElement === document.querySelector('.tm-replybox textarea')")
    page.keyboard.type("can Robotis ship them by Friday?")
    page.click(".tm-replybox .tm-row button")
    page.wait_for_function("document.querySelector('.tm-body').innerText.includes('ship them by Friday?')")
    assert page.locator(".tm-replybox textarea").count() == 0
    assert errors == [], errors
    ctx.close()
