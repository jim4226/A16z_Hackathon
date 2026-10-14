"""Robustness (beta) end to end: the real Studio (`morph studio egg-picker`, offline, no keys, full simulations) and a
real browser with software WebGL (the replay plays in the 3D view).

- the Simulation tab's card says one nominal run tests nothing around it; a small study started from the card (8 runs
  at 1 ms, an extra choice in its menu) shows its progress live and ends with the result in plain words, the 95%
  interval and the simulation note, read back as the server recorded it;
- a failing run replayed in the 3D view (the viewer's title names the run), then back to the nominal run;
- a stress test of 200 runs started and cancelled after a few: it stops within seconds, keeps its runs and offers to
  carry on; the details dialog lists every tolerance with its source;
- the card at 1280x720 and 910 px wide: nothing reaches past the window.

MORPH_E2E=1 runs it (about three minutes, plus building the demo robots once: live_studio.demo_home, MORPH_E2E_DEMO
keeps them). MORPH_E2E_PORTS picks the Studio's port; MORPH_E2E_SHOTS=<dir> saves e2e_study_*.png."""

import json
import os
import re
import shutil
import time

import pytest

from live_studio import CHROME, Page, Studio, demo_home, until

pytestmark = pytest.mark.skipif(os.environ.get("MORPH_E2E") != "1",
                                reason="end-to-end: MORPH_E2E=1 runs a real Studio and a browser (minutes)")
ARGS = ["--no-sandbox", "--disable-background-networking", "--use-angle=swiftshader", "--enable-unsafe-swiftshader",
        "--ignore-gpu-blocklist"]
QUIET = ("GPU stall due to ReadPixels", "software WebGL")


@pytest.fixture(scope="module")
def browser():
    sync = pytest.importorskip("playwright.sync_api")
    with sync.sync_playwright() as p:
        try:
            b = p.chromium.launch(args=ARGS, **({"executable_path": CHROME} if os.path.exists(CHROME) else {}))
        except Exception as exc:  # noqa: BLE001  (no browser: `playwright install chromium`)
            pytest.skip(f"no Chromium for the end-to-end test ({exc.__class__.__name__})")
        yield b
        b.close()


@pytest.fixture
def studio(tmp_path, tmp_path_factory):
    home = str(tmp_path / "home")
    shutil.copytree(demo_home(tmp_path_factory), home)
    s = Studio(home, "egg-picker")
    yield s
    s.stop()
    assert not s.tracebacks(), s.tracebacks()


def card(pg: Page) -> str:
    return pg.ev("() => (document.querySelector('#studyCard')?.innerText || '').replace(/\\s+/g, ' ').trim()")


def test_a_study_in_the_real_studio_from_the_card(browser, studio):
    pg = Page(browser, studio, (1600, 900), "editor", quiet=QUIET)
    pg.open("tab=sim")
    until(lambda: pg.ev("!!document.querySelector('#studyCard .sd-start')"), 60, "the Robustness card")
    text = card(pg)
    assert "One nominal run. Small differences in placement or hardware aren't tested." in text and "Run a study" in text
    assert pg.ev("document.querySelector('#studyCard').previousElementSibling.classList.contains('success')")
    pg.shot("e2e_study_1_none")

    # a small study from the card: progress while it runs, then the result
    pg.ev("() => { const s = document.querySelector('#studyCard select[data-f=\"runs\"]'); s.add(new Option('8 runs', '8'));"
          " s.value = '8'; s.dispatchEvent(new Event('change', {bubbles: true})); }")
    t0 = time.time()
    pg.pg.click("#studyCard [data-a='start']")
    seen, first, shot = [], None, False
    while time.time() - t0 < 240 and not pg.ev("!!document.querySelector('#studyCard .sd-head') && "
                                                "!document.querySelector('#studyCard .sd-run')"):
        t = card(pg)
        if "So far" in t and first is None:
            first = round(time.time() - t0, 1)
        if "So far" in t and not shot:
            pg.shot("e2e_study_2_running")
            shot = True
        seen.append(t)
        time.sleep(0.3)
    took = round(time.time() - t0, 1)
    assert any(re.search(r"Run \d of 8\.", t) for t in seen) and first is not None, seen[-3:]
    text = card(pg)
    m = re.search(r"Succeeds in (\d) of 8 randomized runs \(95% CI (\d+)-(\d+)%\); the nominal run succeeds\.", text)
    assert m, text
    assert "This is simulation, for the tolerances shown; not a field failure rate." in text
    status, info = studio.api("GET", "/api/study?project=egg-picker")
    latest = info["latest"]
    assert status == 200 and latest["status"] == "done" and latest["summary"]["successes"] == int(m.group(1))
    assert latest["timestep"] == 0.001 and latest["seed"] == 1 and latest["n"] == 8 and latest["model"]["matches_revision"]
    pg.shot("e2e_study_3_done")
    print(json.dumps({"study_s": took, "first_so_far_s": first, "result": m.group(0),
                      "timing": {k: latest["timing"].get(k) for k in ("prepare_s", "first_result_s", "runs_per_s", "workers",
                                                                       "checks_runs", "total_s")}}))

    # a failing run in the 3D view, then the nominal run again
    groups = latest["card"]["groups"]
    if groups:
        run = groups[0]["example_run"]
        t1 = time.time()
        pg.pg.click(f"#studyCard [data-a='replay'][data-run='{run}']")
        until(lambda: pg.ev("!!(window.morphStudy.rp && window.morphStudy.rp.doc)"), 120, "the replay")
        replay_s = round(time.time() - t1, 1)
        until(lambda: pg.ev("(window.morph.viewer.v._traj?.src?.title || '').startsWith('Study run')"), 30, "the replay to play")
        text = card(pg)
        assert f"Replay of run {run}" in text and "Back to the nominal run" in text
        assert pg.ev("window.morphStudy.rp.doc.robustness.matches_record") is True
        # the moment that matters: a second after the release (the jaws still on the egg when they jam)
        pg.ev("(() => { const d = window.morphStudy.rp.doc, f = d.frames.find((x) => x.phase === 'release');"
              " const v = window.morph.viewer; if (f) { v.seek(f.t - d.frames[0].t + 1.0); v.pauseTraj(); } })()")
        time.sleep(2.5)
        pg.shot("e2e_study_4_replay")
        pg.ev("window.morph.viewer.resumeTraj()")
        pg.pg.click("#studyCard [data-a='stopreplay']")
        until(lambda: pg.ev("!(window.morph.viewer.v._traj?.src?.title || 'Study run').startsWith('Study run')"), 30,
              "the nominal run to play again")
        print(json.dumps({"replay_s": replay_s, "run": run, "group": groups[0]["id"]}))

    # a stress test, cancelled after a few runs: it keeps them and offers to carry on
    pg.pg.click("#studyCard [data-f='stress']")
    pg.ev("() => { const s = document.querySelector('#studyCard select[data-f=\"runs\"]'); s.value = '200'; "
          "s.dispatchEvent(new Event('change', {bubbles: true})); }")
    assert "Run a stress test" in card(pg)
    pg.pg.click("#studyCard [data-a='start']")
    until(lambda: re.search(r"Run ([3-9]|\d\d+) of 200 \(stress test\)", card(pg)), 180, "a few stress runs")
    t2 = time.time()
    pg.pg.click("#studyCard [data-a='cancel']")
    until(lambda: not pg.ev("!!document.querySelector('#studyCard .sd-run')"), 30, "the stress test to stop")
    page_s = round(time.time() - t2, 2)          # the click, the study stopping, its last event and the card's reload
    until(lambda: "A stress study stopped after" in card(pg), 30, "the stopped stress test")
    text = card(pg)
    assert "Carry on" in text and "With every tolerance doubled (stress test), succeeds in" in text
    status, info = studio.api("GET", "/api/study?project=egg-picker")
    stopped = info["stopped"][0]
    assert stopped["stress"] and stopped["status"] == "cancelled" and 3 <= stopped["done"] < 200
    assert info["job"]["cancel_s"] < 2.0 and page_s < 15, (info["job"]["cancel_s"], page_s)   # the study itself: 2 s
    pg.shot("e2e_study_5_cancelled")
    print(json.dumps({"cancel_s": info["job"]["cancel_s"], "page_s": page_s, "kept": stopped["done"]}))

    # the details: the result again and every tolerance with where it comes from
    pg.pg.click("#studyCard [data-a='details']")
    until(lambda: pg.ev("document.querySelectorAll('.sd-dlg .sd-tbl tbody tr').length === 11"), 30, "the tolerances")
    assert "USDA egg weight classes" in pg.text(".sd-dlg") and "ST3215" in pg.text(".sd-dlg")
    # software WebGL can hold the dialog's 0.2 s fade-in for seconds: wait until it is on screen for the picture
    until(lambda: pg.ev("getComputedStyle(document.querySelector('#featModal')).opacity === '1'"), 60, "the dialog to show")
    pg.fits("the study details")
    pg.shot("e2e_study_6_details")
    pg.pg.click(".sd-dlg [data-a='close']")
    pg.check()
    pg.close()

    # the card at the other window sizes: it fits the Simulation tab's column
    for size in ((1280, 720), (910, 900)):
        p2 = Page(browser, studio, size, f"view{size[0]}", quiet=QUIET)
        p2.open("tab=sim")
        until(lambda: p2.ev("!!document.querySelector('#studyCard .sd-head')"), 60, f"the card at {size[0]} px")
        p2.fits(f"the Simulation tab with the study card at {size[0]} px")
        box = p2.ev("(() => { const r = document.querySelector('#studyCard').getBoundingClientRect(); return [r.left, r.right]; })()")
        assert 0 <= box[0] < box[1] <= size[0], box
        p2.shot(f"e2e_study_7_card_{size[0]}")
        p2.check()
        p2.close()
