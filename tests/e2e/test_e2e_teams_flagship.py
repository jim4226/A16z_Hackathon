"""Morph Teams end to end in real browsers (RELEASE_CONTRACTS section 8, the flagship workflow).

Three people work on one Studio, each in their own browser context: Jaron (owner and generalist), Ana (electronics)
and Raj (manufacturing). The Studio is the real one, started the way people start it (`morph team init`, then
`morph studio --team --remote`), and its robots are fully built (full simulations, no stubs), so every number the
pages show comes from a real build.

- test_flagship_servo_swap_on_a_remote_team_studio: invites and joins, Ana's servo substitution with its preview
  (torque margins, wiring, firmware, cost), routing chips, Raj's approval, Jaron applying it for Ana, the pilot gate
  saying what's missing and then passing, an order file locked by policy until the release, the release, the export
  with its team record, comments on a BOM line and a stage, and a later edit after which only the affected disciplines
  need fresh approvals while the comments carry forward. People use 1600x900, 1280x720 and 910x800 windows.
- test_viewer_presence_inbox_and_revocation_on_a_local_team_studio: `morph studio --team` on this computer only; a
  viewer who comments and proposes but can't edit, presence, the inbox, and revoking a member whose page is open.

These take minutes and build robots, so they run only with MORPH_E2E=1 (screenshots go to MORPH_E2E_SHOTS when set;
MORPH_E2E_PORTS picks the Studios' ports, as in live_studio.py).
The browsers start without WebGL (the 3D view says so and every other view works): on a machine without a GPU the
software renderer redraws the auto-orbiting robot every frame and slows every click on that page several-fold.
MORPH_E2E_WEBGL=1 gives Jaron's browser WebGL.
"""

import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("MORPH_E2E") != "1",
                                reason="end-to-end browser test: set MORPH_E2E=1 (it builds robots and takes minutes)")

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CHROME = os.environ.get("PLAYWRIGHT_CHROMIUM") or "/opt/pw-browsers/chromium-1194/chrome-linux/chrome"
# what a page may log without failing the test: the browser without WebGL (it shows "3D view unavailable"), and the
# 401 a page gets on its way to the sign-in page
QUIET = ("WebGL", "THREE.WebGLRenderer", "status of 401")
WEBGL = os.environ.get("MORPH_E2E_WEBGL") == "1"


def env(home: str) -> dict:
    e = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "KEEWANO_", "SUBQ_", "GEMINI_", "MORPH_"))}
    e.update(MORPH_HOME=home, MORPH_OFFLINE="1", PYTHONPATH=ROOT, MUJOCO_GL=os.environ.get("MUJOCO_GL") or "osmesa")
    return e


def morph(home: str, *args, timeout=900) -> str:
    r = subprocess.run([sys.executable, "-m", "morph.cli", *args], cwd=ROOT, env=env(home), capture_output=True, text=True,
                       timeout=timeout)
    assert r.returncode == 0, (args, r.stdout[-2000:], r.stderr[-2000:])
    return r.stdout


from live_studio import free_port, release_port  # noqa: E402  (MORPH_E2E_PORTS, shared out between xdist workers)


def until(fn, timeout=60.0, what="the page", step=0.25):
    t0, last = time.time(), None
    while time.time() - t0 < timeout:
        try:
            v = fn()
        except Exception as exc:  # noqa: BLE001  (a page between two loads)
            v, last = None, exc
        if v:
            return v
        time.sleep(step)
    raise AssertionError(f"timed out waiting for {what}" + (f" ({last})" if last else ""))


class Studio:
    """`morph studio egg-picker --team [--remote]` on a copy of the built projects, in a subprocess."""

    def __init__(self, template: str, home: str, remote: bool):
        shutil.copytree(template, home)
        out = morph(home, "team", "init", "--owner", "Jaron", "--name", "R Cubed")
        assert "morph studio --team" in out and "?link=ml_" in out          # what to do next comes first
        self.home, self.port = home, free_port()
        self.proc = subprocess.Popen([sys.executable, "-m", "morph.cli", "studio", "egg-picker", "--team", "--port", str(self.port)]
                                     + (["--remote"] if remote else []), cwd=ROOT, env=env(home), stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True)
        self.log, lines = [], []
        threading.Thread(target=self._read, args=(lines,), daemon=True).start()
        self.link = until(lambda: next((m.group(1) for ln in list(lines) for m in [re.search(r"(http://\S+/\?link=ml_[\w-]+)", ln)] if m), None),
                          120, "the owner's sign-in link")
        self.base = self.link.split("/?", 1)[0]

    def _read(self, lines):
        for ln in self.proc.stdout:
            lines.append(ln)
            self.log.append(ln)

    def api(self, method, path, cookie=None, body=None):
        """A request from this computer (127.0.0.1) with a member's session cookie."""
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={**({"Cookie": cookie} if cookie else {}),
                                              **({"X-Morph-Client": "1", "Content-Type": "application/json"} if method != "GET" else {})})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw, status = r.read(), r.status
        except urllib.error.HTTPError as e:
            raw, status = e.read(), e.code
        try:
            return status, json.loads(raw)
        except ValueError:
            return status, raw

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        release_port(self.port)


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    """Both demo robots built the way the first `morph studio` builds them (full simulations), plus egg-picker at a pilot
    build (r002), the team's starting point: a release of it is gated by pilot rules."""
    home = str(tmp_path_factory.mktemp("e2e-teams") / "template")
    os.makedirs(home)
    port = free_port()
    p = subprocess.Popen([sys.executable, "-m", "morph.cli", "studio", "egg-picker", "--port", str(port)], cwd=ROOT, env=env(home),
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        for ln in p.stdout:  # builds egg-picker and so101, then serves
            if ln.startswith("Morph Studio:"):
                break
    finally:
        p.terminate()
        p.wait(10)
        release_port(port)
    morph(home, "robot", "change", "egg-picker", "build.config=pilot")
    return home


@pytest.fixture(scope="module")
def pw():
    sync = pytest.importorskip("playwright.sync_api")
    with sync.sync_playwright() as p:
        exe = CHROME if os.path.exists(CHROME) else None
        try:  # with MORPH_E2E_WEBGL=1 Jaron's browser draws the 3D robot
            nogl = p.chromium.launch(executable_path=exe, args=["--no-sandbox", "--disable-webgl", "--disable-3d-apis"])
            gl = p.chromium.launch(executable_path=exe, args=["--no-sandbox"]) if WEBGL else nogl
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"no Chromium for Playwright ({exc.__class__.__name__})")
        yield gl, nogl
        if gl is not nogl:
            gl.close()
        nogl.close()


SHOTS = os.environ.get("MORPH_E2E_SHOTS")


class Person:
    def __init__(self, browser, name, size, studio, errors, quiet=QUIET):
        self.name, self.studio, self.errors = name, studio, errors
        self.ctx = browser.new_context(viewport={"width": size[0], "height": size[1]}, accept_downloads=True)
        self.pg = self.ctx.new_page()
        self.pg.on("pageerror", lambda e: errors.append(f"{name}: {e}"))
        self.pg.on("console", lambda m: errors.append(f"{name}: {m.text}")
                   if m.type == "error" and not any(q in m.text for q in quiet) else None)

    def ev(self, js):
        return self.pg.evaluate(js)

    def text(self, sel) -> str:
        loc = self.pg.locator(sel)
        return " ".join(loc.first.inner_text().split()) if loc.count() else ""

    def ready(self, timeout=240):
        until(lambda: self.ev("!!(window.morph?.state?.current && document.querySelector('.tm-hdr .tm-share'))"), timeout,
              f"{self.name}'s Studio page")

    def tab(self, name):
        if self.ev("matchMedia('(max-width: 1280px)').matches") and not self.ev("document.body.classList.contains('insp-open')"):
            self.pg.click("#inspBtn")                    # a narrow window: the right panel opens over the views
        self.pg.click(f".tm-tabs [data-tab={name}]")
        until(lambda: self.ev(f"document.querySelector('.tm-tabs [data-tab={name}]').classList.contains('on')"), 10, f"the {name} tab")

    def views(self):
        """A narrow window: close the right panel to get at the views under it."""
        if self.ev("document.body.classList.contains('insp-open')"):
            self.pg.click("#inspBtn")
            until(lambda: not self.ev("document.body.classList.contains('insp-open')"), 10, f"{self.name}'s panel closing")

    def cookie(self) -> str:
        c = next(c for c in self.ctx.cookies() if c["name"].startswith("morph_session_"))
        return f"{c['name']}={c['value']}"

    def api(self, method, path, body=None):
        return self.studio.api(method, path, self.cookie(), body)

    def rev(self):
        return self.ev("window.morph.state.current.rev")

    def wait_rev(self, rev, timeout=420):
        until(lambda: self.rev() == rev and not self.ev("window.morph.state.busy"), timeout, f"{self.name} on {rev}")

    def no_overflow(self):
        assert self.ev("document.scrollingElement.scrollWidth <= innerWidth + 1"), f"{self.name}: the page scrolls sideways"

    def shot(self, label):
        if SHOTS:
            os.makedirs(SHOTS, exist_ok=True)
            self.pg.screenshot(path=os.path.join(SHOTS, f"e2e_{label}_{self.name.lower()}.png"))


def sign_in(person, link):
    person.pg.goto(link, wait_until="domcontentloaded")
    with person.pg.expect_navigation():
        person.pg.click("button[type=submit]")
    person.ready()


def invite(owner, name, role, disciplines) -> str:
    """The owner makes an invite in Share; returns its link."""
    dlg = owner.pg.locator(".tm-share-dlg")
    if not dlg.count():
        owner.pg.click("[data-tm=share]")
        dlg.locator("[data-a=invite]").wait_for()
    before = owner.ev("document.querySelector('[data-out=invite] input')?.value || ''")
    dlg.locator("select[name=role]").select_option(role)
    dlg.locator("input[name=hint]").fill(name)
    for d in disciplines:
        dlg.locator(f"input[name=inv][value={d}] + span").click()
    dlg.locator("[data-a=invite]").click()
    link = until(lambda: (lambda v: v if v and v != before else None)(owner.ev("document.querySelector('[data-out=invite] input')?.value")),
                 20, f"{name}'s invite link")
    assert "expires in 7 days" in dlg.locator(".tm-inv", has_text=name).inner_text()   # listed under Pending invites at once
    assert dlg.locator("input[name=hint]").input_value() == ""         # and the form is ready for the next person
    return link


def join(person, link, name, *says):
    person.pg.goto(link, wait_until="domcontentloaded")
    page = " ".join(person.pg.inner_text("main").split())
    assert all(s in page for s in says), page
    assert person.pg.input_value("input[name=name]") == name
    with person.pg.expect_navigation():
        person.pg.click("button[type=submit]")
    person.ready()


def laps():
    """Prints how long each part of a flow took (pytest -s shows it)."""
    t0 = last = time.time()

    def lap(what):
        nonlocal last
        now = time.time()
        print(f"  {now - t0:6.1f} s  (+{now - last:5.1f})  {what}", flush=True)
        last = now
    return lap


def impact_rows(person, sel) -> dict:
    return {r.locator("b").inner_text().strip().lower(): " ".join(r.locator("span").first.inner_text().split())
            for r in person.pg.locator(f"{sel} .tm-ir").all()}


def test_flagship_servo_swap_on_a_remote_team_studio(template, pw, tmp_path):
    gl, nogl = pw
    studio = Studio(template, str(tmp_path / "projects"), remote=True)
    errors = []
    try:
        jaron = Person(gl, "Jaron", (1600, 900), studio, errors)
        ana = Person(nogl, "Ana", (1280, 720), studio, errors)
        raj = Person(nogl, "Raj", (910, 800), studio, errors)
        t0, lap = time.time(), laps()

        # the owner signs in from the link `morph studio --team --remote` printed (the address teammates use) and invites
        sign_in(jaron, studio.link)
        assert jaron.text(".tm-role").lower() == "owner"
        until(lambda: "You're the only one on the team so far" in jaron.text(".tm-body"), 20, "the owner's first hint")
        lap("Jaron signed in")
        a_link = invite(jaron, "Ana", "editor", ["electronics"])
        r_link = invite(jaron, "Raj", "editor", ["manufacturing"])
        lap("two invites")
        assert a_link.startswith(studio.base + "/?invite=mi_") and "only works on this computer" not in jaron.text(".tm-share-dlg")
        jaron.shot("01_share")
        jaron.pg.keyboard.press("Escape")
        join(ana, a_link, "Ana", "Jaron invited you as editor", "You review changes to: Electronics")
        lap("Ana joined")
        join(raj, r_link, "Raj", "You review changes to: Manufacturing")
        lap("Raj joined")
        until(lambda: jaron.ev("document.querySelectorAll('.tm-avs .tm-av').length") >= 3, 30, "three people present")
        lap("all three present")

        # Ana proposes a servo substitution and previews what it changes, in numbers
        ana.tab("changes")
        ana.pg.click(".tm-new summary")
        ana.pg.select_option("[data-a=param]", "arm.actuator_family")
        ana.pg.select_option(".tm-val select", "dynamixel")
        ana.pg.click("[data-a=add]")
        assert ana.text(".tm-draft") == "Arm · Actuators: Dynamixel"
        ana.pg.fill("[data-a=title]", "Swap the arm to Dynamixel servos")
        ana.pg.fill("[data-a=note]", "Absolute encoders on every joint. @Raj can the brackets take the XM430 body?")
        ana.pg.click("[data-a=preview]")
        until(lambda: ana.pg.locator(".tm-preview .tm-ir").count() >= 5, 60, "the preview's numbers")
        rows = impact_rows(ana, ".tm-preview")
        assert re.search(r"weakest joint has [\d.]+× → [\d.]+× the torque it needs", rows["torque"]), rows
        assert "STS3215 →" in rows["servos"] and "XM430-W350" in rows["servos"], rows
        assert "DYNAMIXEL Protocol 2.0" in rows["electronics"] and "OpenRB-150" in rows["electronics"], rows
        assert "Dynamixel2Arduino" in rows["wiring and firmware"] and "JST EH" in rows["wiring and firmware"], rows
        assert re.search(r"about \+\$[\d,]+ per robot \(servos \+\$[\d,]+", rows["cost"]), rows
        chips = [" ".join(c.split()) for c in ana.pg.locator(".tm-preview .tm-chip").all_inner_texts()]
        assert "Electronics: Jaron (generalist)" in chips and "Manufacturing: Raj" in chips   # Ana can't review her own
        assert "Recomputes 9 of 9 stages" in ana.text(".tm-preview .tm-aff")
        ana.shot("02_preview")
        lap("preview")
        ana.pg.click("[data-a=submit]")
        card = ana.pg.locator(".tm-pr", has_text="Swap the arm to Dynamixel servos")
        card.wait_for()
        assert card.locator("[data-decide]").count() == 0       # her own change, at pilot: no approve button, and why
        assert "You made this change, so at pilot your approval doesn't count for Electronics: Jaron (generalist)" in card.inner_text()
        status, out = ana.api("POST", "/api/reviews/decide", {"review": out_review(ana), "decision": "approve"})
        assert status == 403 and "You made this change" in out["error"]

        # the reviewers hear about it: Raj in his inbox (the review request says he was mentioned), Jaron as generalist
        inbox = raj.api("GET", "/api/inbox")[1]["items"]
        assert any(i["kind"] == "review_request" and "change request #1: Swap the arm" in i["text"] and "mentioned you" in i["text"]
                   for i in inbox), inbox
        raj.tab("changes")
        rcard = raj.pg.locator(".tm-pr", has_text="Swap the arm to Dynamixel servos")
        rcard.locator("[data-decide=approve]").wait_for()
        assert rcard.locator("[data-decide=approve]").inner_text().strip() == "Approve as Manufacturing"
        rcard.locator("[data-decide=approve]").click()
        until(lambda: "You approved" in rcard.inner_text(), 20, "Raj's approval")
        raj.no_overflow()
        raj.shot("03_approved_cr")
        lap("proposed, Raj approved")

        # Jaron applies it: the room's queue builds r003 as Jaron's build of Ana's change
        jaron.tab("changes")
        jcard = jaron.pg.locator(".tm-pr", has_text="Swap the arm to Dynamixel servos")
        jcard.locator("[data-apply]").click()
        jaron.wait_rev("r003")
        lap("r003 built (Jaron's page)")
        until(lambda: "Built r003" in jcard.inner_text(), 60, "the change request's build summary")
        assert re.search(r"Built r003: weakest joint [\d.]+× → [\d.]+×, \$[\d,]+ → \$[\d,]+ per unit, software checks pass", " ".join(jcard.inner_text().split()))
        feed = [i["text"] for i in jaron.api("GET", "/api/activity?project=egg-picker")[1]["items"]]
        assert any(t.startswith("Jaron made r003: #1 Swap the arm") and "(proposed by Ana)" in t for t in feed), feed[:5]
        lap("applied and built r003")

        # the pilot gate says what is missing; Jaron asks for reviews; Ana can't approve her own work above prototype
        for p in (ana, raj):
            p.wait_rev("r003")
        jaron.tab("review")
        until(lambda: "needs 5 approvals: pilot" in jaron.text(".tm-gate"), 30, "the pilot gate")
        assert "Release locked: needs 5 approvals: pilot (Mechanical, Simulation, Electronics, Sourcing, Documentation)" in jaron.text(".tm-gate")
        assert jaron.pg.locator(".tm-gr.approved", has_text="Manufacturing").count() == 1   # Raj's approval of the request counts
        jaron.pg.click("[data-a=request]")
        until(lambda: jaron.pg.locator(".tm-routes .tm-chip").count() == 5, 20, "the review request")
        ana.tab("review")
        until(lambda: "You made this change" in ana.text(".tm-gate"), 20, "Ana's reason")
        assert ana.pg.locator(".tm-gate [data-decide]").count() == 0
        ana.shot("04_review_author")
        jaron.pg.locator("[data-decide=approve]").click()
        until(lambda: "ready to release: pilot" in jaron.text(".tm-gate"), 30, "the gate passing")
        assert all(c.get_attribute("class").split()[-1] == "approved" for c in jaron.pg.locator(".tm-routes .tm-chip").all())
        jaron.shot("05_gate_ready")
        lap("reviews requested, gate passes")

        # an order file stays locked for editors until the release (Jaron turns draft downloads off)
        jaron.pg.click("[data-tm=share]")
        jaron.pg.locator(".tm-share-dlg [data-pol=unreleased_downloads]").evaluate("x => x.click()")
        until(lambda: studio.api("GET", "/api/team", jaron.cookie())[1]["policy"]["unreleased_downloads"] == "owner", 10, "the policy")
        jaron.pg.keyboard.press("Escape")
        order = "/files/egg-picker/r003/mfg/mfg_pilot.zip"
        status, out = raj.api("GET", order)
        assert status == 403 and "unlock when r003 is released" in out["error"], (status, out)
        status, raw = raj.api("GET", "/api/export?project=egg-picker&rev=r003")      # an export leaves them out too
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            assert status == 200 and not any(n.endswith("mfg/mfg_pilot.zip") for n in z.namelist())

        # Raj releases it, the order files unlock and the export carries the team's record
        raj.tab("review")
        until(lambda: "ready to release: pilot" in raj.text(".tm-gate"), 30, "Raj's gate")
        raj.pg.click("[data-a=release]")
        raj.pg.fill(".tm-relbox input", "Pilot v1")
        raj.pg.click(".tm-relbox button")
        until(lambda: "released: Pilot v1" in raj.text(".tm-gate"), 30, "the release")
        assert raj.api("GET", order)[0] == 200
        raj.shot("06_released")
        raj.views()                                              # Export is in the revision bar, under the panel at 910 px
        with raj.pg.expect_download(timeout=120000) as dl:
            raj.pg.click("#tabsInfo [data-export]")
        path = str(tmp_path / "r003.zip")
        dl.value.save_as(path)
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            record = json.loads(z.read(next(n for n in names if n.endswith("team.json"))))
        assert any(n.endswith("mfg/mfg_pilot.zip") for n in names)
        assert record["made_by"] == ["Ana"] and record["applied_by"] == "Jaron" and record["release_gate"]["ok"]
        assert {d["discipline"]: [a["name"] for a in d["approved_by"]] for d in record["disciplines"]}["manufacturing"] == ["Raj"]
        assert [r["tag"] for r in record["releases"]] == ["Pilot v1"]
        lap("released and exported")

        # comments on a BOM line and a stage; a mention reaches Jaron's inbox
        raj.tab("comments")
        sel = raj.pg.locator("select[data-a=anchor]")
        sel.select_option(label=next(o for o in sel.locator("option").all_text_contents() if "XM430-W350" in o))
        raj.pg.fill(".tm-compose textarea", "ROBOTIS ships the XM430 in 3 days; ordering 3 for the pilot.")
        raj.pg.click("[data-a=post]")
        until(lambda: "ordering 3 for the pilot" in raj.text(".tm-body"), 20, "Raj's BOM comment")
        ana.tab("comments")
        ana.pg.locator("select[data-a=anchor]").select_option(label="Schematic · PCB · FW")
        ana.pg.fill(".tm-compose textarea", "@Jaron the firmware now uses Dynamixel2Arduino: flash the OpenRB before the bench test.")
        ana.pg.click("[data-a=post]")
        until(lambda: "flash the OpenRB" in ana.text(".tm-body"), 20, "Ana's stage comment")
        until(lambda: any(i["kind"] == "mention" and "flash the OpenRB" in i["text"]
                          for i in jaron.api("GET", "/api/inbox")[1]["items"]), 20, "Jaron's mention")
        lap("comments")

        # a later edit: Ana moves the electronics to a custom PCB. Only the disciplines whose work it rebuilt need approving
        # again; Simulation's approval carries forward (its stage was reused), and the comments follow the revision.
        if not ana.ev("document.querySelector('#params').classList.contains('open')"):
            ana.pg.click("#paramsToggle")
        ana.pg.select_option('#paramsBody .prow[data-key="electronics.config"] select', "custom_pcb")
        ana.pg.click("#paramsApply")
        ana.wait_rev("r004")
        jaron.wait_rev("r004")
        jaron.tab("review")
        until(lambda: "needs 5 approvals: pilot" in jaron.text(".tm-gate"), 30, "the gate on r004")
        assert "(Mechanical, Electronics, Manufacturing, Sourcing, Documentation)" in jaron.text(".tm-gate")
        sim = jaron.pg.locator(".tm-gr", has_text="Simulation")
        assert "approved" in sim.get_attribute("class") and "r003" in sim.inner_text()       # carried from r003
        jaron.shot("07_stale_after_later_edit")
        lap("later edit built r004")
        jaron.tab("comments")
        until(lambda: jaron.text(".tm-filter b") == "2 threads on r004", 20, "the comments on r004")
        threads = [" ".join(t.split()) for t in jaron.pg.locator(".tm-th").all_inner_texts()]
        assert any("XM430-W350" in t and "from r003" in t and "changed since" not in t for t in threads), threads
        assert any("Schematic · PCB · FW" in t and "from r003" in t and "changed since" in t for t in threads), threads
        jaron.shot("08_comments_carried")

        # the audit log verifies, in the Studio and from the command line
        assert studio.api("GET", "/api/audit", jaron.cookie())[1]["verify"]["ok"] is True
        assert "hash chain intact" in morph(studio.home, "team", "audit", "--verify")
        for p in (jaron, ana, raj):
            p.no_overflow()
        assert errors == [], errors
        print(f"flagship flow took {time.time() - t0:.0f} s")
    finally:
        studio.stop()


def out_review(person) -> str:
    """The id of the review of the newest change request."""
    return person.api("GET", "/api/proposals?project=egg-picker")[1]["proposals"][0]["review"]["id"]


def test_viewer_presence_inbox_and_revocation_on_a_local_team_studio(template, pw, tmp_path):
    gl, nogl = pw
    studio = Studio(template, str(tmp_path / "projects"), remote=False)
    errors = []
    try:
        assert studio.base.startswith("http://127.0.0.1:")                       # this computer only (an SSH tunnel's)
        jaron = Person(gl, "Jaron", (1280, 720), studio, errors)
        vic = Person(nogl, "Vic", (1600, 900), studio, errors)
        raj = Person(nogl, "Raj", (910, 800), studio, errors)
        sign_in(jaron, studio.link)
        v_link = invite(jaron, "Vic", "viewer", [])
        r_link = invite(jaron, "Raj", "editor", ["manufacturing"])
        jaron.pg.keyboard.press("Escape")
        join(vic, v_link, "Vic", "You'll see the robot live, comment and propose changes")
        join(raj, r_link, "Raj", "You review changes to: Manufacturing")
        until(lambda: jaron.ev("document.querySelectorAll('.tm-avs .tm-av').length") >= 3, 30, "presence")

        # a viewer sees everything live but can't edit: no editing tools, and the Studio says what to do instead
        assert vic.text(".tm-role").lower() == "viewer" and vic.ev("document.body.classList.contains('tm-readonly')")
        assert vic.pg.is_disabled("#input") and vic.pg.is_disabled("#send")
        assert vic.ev("getComputedStyle(document.querySelector('#presentBtn')).display") == "none"
        assert vic.pg.get_attribute("#input", "placeholder") == "View only: propose changes in Changes"
        status, out = vic.api("POST", "/api/chat?project=egg-picker", {"message": "make it 6-DOF"})
        assert status == 403 and "propose the change under Changes" in out["error"]
        vic.tab("comments")
        vic.pg.fill(".tm-compose textarea", "From the customer: can it take 70 g eggs? @Raj")
        vic.pg.click("[data-a=post]")
        until(lambda: "70 g eggs" in vic.text(".tm-body"), 20, "Vic's comment")
        vic.tab("changes")
        vic.pg.click(".tm-new summary")
        vic.pg.select_option("[data-a=param]", "object.mass_g")
        vic.pg.fill(".tm-val input", "70")
        vic.pg.click("[data-a=add]")
        vic.pg.click("[data-a=submit]")
        card = vic.pg.locator(".tm-pr", has_text="Object mass to 70 g")
        card.wait_for()
        assert card.locator("[data-apply]").count() == 0 and "Task · Object mass" in card.inner_text()
        vic.shot("10_viewer_proposed")
        vic.no_overflow()

        # the inbox: Raj was mentioned and asked to review; Jaron (generalist, watching) heard about the request
        until(lambda: any(i["kind"] == "mention" and "70 g eggs" in i["text"] for i in raj.api("GET", "/api/inbox")[1]["items"]),
              20, "Raj's mention")
        assert any(i["kind"] == "review_request" and "change request #1: Object mass to 70 g" in i["text"]
                   for i in raj.api("GET", "/api/inbox")[1]["items"])
        until(lambda: jaron.ev("document.querySelector('.tm-badge').textContent") not in ("", "0"), 30, "Jaron's unread badge")
        jaron.pg.click("[data-tm=inbox]")
        assert "Object mass to 70 g" in jaron.text(".tm-pop")
        jaron.shot("11_inbox")
        jaron.pg.click("[data-tm=inbox]")

        # revoking Raj while his page is open: his open page stops being live and lands on a page that says why
        jaron.pg.click("[data-tm=share]")
        row = jaron.pg.locator(".tm-share-dlg .tm-mem", has_text="Raj")
        row.locator("[data-a=revoke]").click()
        row.locator("[data-a=revoke]").click()                                    # "Remove Raj?"
        until(lambda: raj.pg.locator(".tm-gone").count() or "no longer on R Cubed" in (raj.pg.content() or ""), 30,
              "Raj's page noticing")
        until(lambda: "no longer on R Cubed" in raj.pg.content(), 30, "the signed-out page")
        assert "Jaron removed Raj from this Studio" in " ".join(raj.pg.inner_text("main").split())
        raj.shot("12_removed")
        assert studio.api("GET", "/api/state?project=egg-picker", raj.cookie())[0] == 401
        until(lambda: all(m["name"] != "Raj" for m in studio.api("GET", "/api/team", jaron.cookie())[1]["presence"]), 30,
              "presence without Raj")
        assert studio.api("GET", "/api/audit", jaron.cookie())[1]["verify"]["ok"] is True
        assert errors == [], errors
    finally:
        studio.stop()
