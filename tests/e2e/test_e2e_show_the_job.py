"""Show Morph the job end to end in a real browser, against a real Studio, on box-picker (a plain-finger arm for a small
box), with an 8 s VP9 clip of a hand moving something across a table, made here with ffmpeg.

- test_by_hand_with_the_measuring_tool: no key and network lookups off, so nothing is sent anywhere: frames picked in
  the browser, grasp and release marked, the cards filled in by hand (a strawberry, sized with the measuring tool
  against a bank card, a typed cycle time), the list of changes reviewed and applied as a new revision with the
  force-sensing fingers a strawberry needs; the build passes its own checks (910 px wide).
- test_an_analyzed_clip_asks_what_it_is_unsure_of: the same Studio code with its vision call answered by a local
  stand-in (live_studio.py): nothing is sent before Analyze, the frames go once, the cards show what was seen, the
  unsure layout waits for an answer, and Apply builds a robot its own checks pass (1600x900).

They run only with MORPH_E2E=1 and need ffmpeg (each builds one revision). See live_studio.py.
"""

import os
import shutil
import subprocess

import pytest

from live_studio import Page, Studio, demo_home, launch, until

pytestmark = pytest.mark.skipif(os.environ.get("MORPH_E2E") != "1",
                                reason="end-to-end browser test: set MORPH_E2E=1 (it builds robots and takes minutes)")

DIALOG = ".modal.on .vjob"


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg makes the test clip")
    path = str(tmp_path_factory.mktemp("clip") / "job.webm")
    flt = ("color=c=0x7a6a58:s=1280x720:d=8,format=yuv420p[bg];"
           "color=c=0xc0282d:s=120x150:d=8,format=yuv420p,geq=lum='if(lte((X-60)*(X-60)/3600+(Y-75)*(Y-75)/5625,1),90,16)':"
           "cb=110:cr=200[obj];"
           "[bg][obj]overlay=x='if(lt(t,2),300,if(lt(t,6),300+(t-2)*150,900))':y='420-if(between(t,2,6),sin((t-2)/4*PI)*160,0)'")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-filter_complex", flt, "-c:v", "libvpx-vp9", "-b:v", "600k",
                    "-deadline", "realtime", "-cpu-used", "8", "-t", "8", path], check=True, timeout=300)
    return path


@pytest.fixture(scope="module")
def browser():
    sync = pytest.importorskip("playwright.sync_api")
    with sync.sync_playwright() as p:
        b = launch(p)
        if b is None:
            pytest.skip("no Chromium for Playwright")
        yield b
        b.close()


@pytest.fixture
def home(tmp_path, tmp_path_factory):
    h = str(tmp_path / "home")
    shutil.copytree(demo_home(tmp_path_factory), h)
    return h


def start(pg: Page, clip: str, button: str):
    """Open Show Morph the job from the chat's tools, agree to `button` (local or cloud) and choose the clip."""
    pg.ev("() => { try { for (const k of ['morph.video.consent', 'morph.video.consent.local-seen']) localStorage.removeItem(k); }"
          " catch (e) { /* no storage */ } }")  # the consent question every time
    pg.pg.click("#chatTools .vj-btn")
    pg.pg.wait_for_selector(f'{DIALOG} [data-a="{button}"]')
    consent = pg.text(f"{DIALOG} .vj-consent")
    pg.fits("Show Morph the job, consent")
    with pg.pg.expect_file_chooser() as fc:
        pg.pg.click(f'{DIALOG} [data-a="{button}"]')
    fc.value.set_files(clip)
    pg.pg.wait_for_selector(f"{DIALOG} .vj-strip .vj-th", timeout=120_000)
    return consent


def mark(pg: Page, t: float, which: str):
    pg.pg.locator(f"{DIALOG} .vj-scrub input[type=range]").evaluate(
        "(el, t) => { el.value = t; for (const e of ['input', 'change']) el.dispatchEvent(new Event(e, {bubbles: true})); }", t)
    pg.pg.wait_for_timeout(800)  # the video seeks to the moment
    pg.pg.click(f'{DIALOG} [data-a="{which}"]')


def changes(pg: Page) -> dict:
    """The review's list: label -> (now, after)."""
    return pg.ev("""() => Object.fromEntries([...document.querySelectorAll('.modal.on .vjob .vj-items tbody tr')].map((r) => {
        const td = r.querySelectorAll('td');
        return [td[0].firstChild.textContent.trim(), [td[1].innerText.trim(), td[3].innerText.trim()]]; }))""")


def applied(pg: Page, studio: Studio, rev0: str, timeout=240):
    until(lambda: not pg.ev("!!document.querySelector('.modal.on .vjob')"), 30, "the dialog to close on Apply")
    until(lambda: pg.rev() not in (None, rev0) and not pg.ev("window.morph.state.busy"), timeout, "the new revision")
    return studio.latest("box-picker")


def test_by_hand_with_the_measuring_tool(browser, home, clip):
    studio = Studio(home, "box-picker")
    try:
        rev0, d0, _ = studio.latest("box-picker")
        pg = Page(browser, studio, (910, 800), "maker")
        pg.open("project=box-picker")
        consent = start(pg, clip, "local")
        assert "Network lookups are off on this computer, so nothing is sent anywhere" in consent, consent
        head = pg.text(f"{DIALOG} .dlg-h .s")
        assert "No frame leaves this browser: you fill in the cards." in head, head
        assert pg.text(f"{DIALOG} .vj-mode") == "Kept on this computer (network lookups are off)"
        mark(pg, 2.1, "grasp")
        mark(pg, 6.0, "release")
        assert pg.text(f"{DIALOG} .vj-marks") == "Grasp 2.1 s · release 6.0 s · carry 3.9 s"
        pg.fits("Show Morph the job, frames at 910 px")
        pg.shot("e2e_vid_01_frames_910")

        pg.pg.click(f'{DIALOG} [data-a="manual"]')
        pg.pg.wait_for_selector(f"{DIALOG} .vj-cards")
        pg.pg.fill(f'{DIALOG} input[data-path="object.name"]', "strawberry")
        until(lambda: pg.text(f"{DIALOG} .vj-preset").startswith("Morph's strawberry preset"), 30, "the strawberry preset")
        pg.pg.click(f'{DIALOG} [data-a="measure"]')
        pg.pg.wait_for_selector(f"{DIALOG} .vj-measure-stage")
        box = pg.pg.locator(f"{DIALOG} .vj-measure-stage .vj-ov").bounding_box()
        for x, y in ((0.20, 0.30), (0.50, 0.30), (0.40, 0.60), (0.55, 0.60)):   # a card's width, then half of it
            pg.pg.mouse.click(box["x"] + x * box["width"], box["y"] + y * box["height"])
        assert pg.text(f"{DIALOG} .vj-mstep") == "The object is about 43 mm wide."
        pg.fits("Show Morph the job, measuring at 910 px")
        pg.shot("e2e_vid_02_measure_910")
        pg.pg.click(f'{DIALOG} [data-a="muse"]')
        pg.pg.wait_for_selector(f"{DIALOG} .vj-cards")
        assert pg.pg.input_value(f'{DIALOG} input[data-path="object.diameter_mm"]') == "43"
        timing = f'{DIALOG} .vj-card[data-sec="timing"]'
        assert pg.text(f"{timing} .vj-note").startswith("from your grasp and release marks")
        pg.pg.fill(f'{timing} input[data-path="timing.cycle_time_s"]', "7.5")
        assert pg.text(f"{timing} .vj-note") == "you entered it"
        until(lambda: pg.text(f'{DIALOG} [data-a="review"]').startswith("Review "), 30, "the list of changes")
        pg.fits("Show Morph the job, cards at 910 px")
        pg.shot("e2e_vid_03_cards_910")

        pg.pg.click(f'{DIALOG} [data-a="review"]')
        pg.pg.wait_for_selector(f"{DIALOG} .vj-items")
        ch = changes(pg)
        assert ch["Object"] == ["small box", "strawberry"] and ch["Object width"][1] == "43 mm", ch
        assert ch["Finger force sensor"] == ["none", "FSR 402"] and ch["Cycle time"] == ["6 s", "7.5 s"], ch
        pg.fits("Show Morph the job, review at 910 px")
        pg.shot("e2e_vid_04_review_910")
        pg.pg.click(f'{DIALOG} [data-a="apply"]')
        rev, d, s = applied(pg, studio, rev0)
        assert (d.object.name, d.object.diameter_mm, d.task.cycle_time_s, d.finger.sensor) == ("strawberry", 43.0, 7.5, "fsr402")
        assert s["feasible"] is True and s["pick_place_success"] is True, s.get("issues")
        said = pg.texts("#messages .msg.assistant")
        assert any("From the cards you filled in" in m for m in said), said[-3:]
        pg.check()
        pg.close()
    finally:
        studio.stop()
    assert not studio.tracebacks(), studio.tracebacks()


def test_an_analyzed_clip_asks_what_it_is_unsure_of(browser, home, clip, tmp_path):
    record = str(tmp_path / "vision-stand-in.jsonl")
    studio = Studio(home, "box-picker", stand_in=record)
    try:
        rev0, d0, _ = studio.latest("box-picker")
        pg = Page(browser, studio, (1600, 900), "maker")
        pg.open("project=box-picker")
        consent = start(pg, clip, "cloud")
        assert "Claude" in consent, consent
        assert studio.calls() == []                       # frames are picked in the browser; nothing is sent yet
        head = pg.text(f"{DIALOG} .dlg-h .s")
        assert "Nothing leaves this browser until you press Analyze." in head, head
        n = pg.ev("document.querySelectorAll('.modal.on .vjob .vj-strip .vj-th:not(.add)').length")
        assert 4 <= n <= 12, n
        mark(pg, 2.1, "grasp")
        mark(pg, 6.0, "release")
        pg.fits("Show Morph the job, frames")
        pg.pg.click(f'{DIALOG} [data-a="analyze"]')
        pg.pg.wait_for_selector(f"{DIALOG} .vj-cards", timeout=150_000)
        calls = [c for c in studio.calls() if "images" in c]
        assert [c["images"] for c in calls] == [n], studio.calls()   # one call with the chosen frames, no more
        assert pg.pg.input_value(f'{DIALOG} input[data-path="object.name"]') == "strawberry"
        assert "Claude (AI)" in pg.text(f"{DIALOG} .dlg-h .s")
        until(lambda: pg.texts(f"{DIALOG} .vj-q .vj-qt"), 30, "the low-confidence question")
        assert pg.text(f"{DIALOG} .vj-nchg") == "Low-confidence parts wait for your answer"
        pg.fits("Show Morph the job, results")
        pg.shot("e2e_vid_05_results")
        pg.pg.locator(f"{DIALOG} .vj-q .vj-chip").first.click()
        until(lambda: pg.text(f'{DIALOG} [data-a="review"]').startswith("Review "), 30, "the list of changes")

        pg.pg.click(f'{DIALOG} [data-a="review"]')
        pg.pg.wait_for_selector(f"{DIALOG} .vj-items")
        assert pg.ev("document.querySelector('.modal.on .vjob').scrollTop") == 0   # the list starts at its title
        ch = changes(pg)
        assert ch["Object"] == ["small box", "strawberry"] and ch["Damage force"][1] == "3 N", ch
        assert ch["Finger force sensor"] == ["none", "FSR 402"] and ch["Finger process"] == ["automatic", "3D printed"], ch
        pg.shot("e2e_vid_06_review")
        pg.pg.click(f'{DIALOG} [data-a="apply"]')
        rev, d, s = applied(pg, studio, rev0)
        assert (d.object.name, d.object.max_force_n, d.finger.sensor) == ("strawberry", 3.0, "fsr402")
        assert s["feasible"] is True and s["pick_place_success"] is True, s.get("issues")
        assert not [c for c in studio.calls() if "blocked" in c], studio.calls()   # nothing tried to leave this computer
        pg.check()
        pg.close()
    finally:
        studio.stop()
    assert not studio.tracebacks(), studio.tracebacks()
