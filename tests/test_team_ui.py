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
    assert "Recomputes Sizing" in prev and "Electronics: Ana" in prev and "Mechanical: Mo" in prev
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
    assert page.inner_text(".tm-preview .tm-aff") == "Recomputes Assembly manual"
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
    page.locator(".tm-pr [data-decide=request_changes]").first.click()
    page.fill(".tm-ask textarea", "Needs the PCBA lead time first")
    page.click(".tm-ask [data-go]")
    page.wait_for_selector(".tm-pr .tm-done")
    assert "You requested changes" in page.inner_text(".tm-pr")
    assert errors == [], errors
    ctx.close()


def shots_dir(d):
    return d["shots"]
