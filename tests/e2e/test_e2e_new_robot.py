"""New robot end to end in a real browser, against a real Studio (offline, no keys: Morph's built-in rules).

- test_strawberries_in_your_own_words: a one-liner from the header button, the questions answered by typing the
  placeholder's kind of reply, the brief checked, the robot built with full simulations (the sourcing step says Darwin
  lookups are off) and opened from the dialog; the new project is in the address and the project menu, and its design
  keeps what was typed (1280x720).
- test_a_link_starts_the_plant_waterer_once: `?new=` opens the brief once (a reload doesn't start a second one); the
  waterer says pouring isn't simulated and sets the bottle down by the plant, and builds with sensible defaults.
- test_requests_morph_cannot_build_get_a_reason_and_build_nothing: a vague request gets questions, a drone gets the
  nearest robot Morph can build, a harmful request is refused, and no project is made.
- test_chat_in_an_open_project_tells_a_new_job_from_an_edit: in egg-picker at 910 px, a new job offers New robot
  (which opens with it), a question changes nothing, and "make it cheaper" builds a cheaper revision that still works.

Each builds at most one robot (about 20 to 60 s on 4 cores); they run only with MORPH_E2E=1. See live_studio.py.
"""

import os
import re
import shutil

import pytest

from live_studio import Page, Studio, demo_home, launch, until

pytestmark = pytest.mark.skipif(os.environ.get("MORPH_E2E") != "1",
                                reason="end-to-end browser test: set MORPH_E2E=1 (it builds robots and takes minutes)")

TITLES = ("A few questions", "Check the brief", "Morph can't build that as described", "Morph doesn't design that")


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
def studio(tmp_path, tmp_path_factory):
    home = str(tmp_path / "home")
    shutil.copytree(demo_home(tmp_path_factory), home)
    s = Studio(home, "egg-picker")
    yield s
    s.stop()
    assert not s.tracebacks(), s.tracebacks()


def title(pg: Page) -> str:
    return pg.ev("document.querySelector('.modal.on #nr-title')?.textContent || ''")


def settled(pg: Page, titles=TITLES, timeout=60) -> str:
    """The dialog's next view once it isn't working any more."""
    return until(lambda: (not pg.ev("!!document.querySelector('.modal.on .nr .spin')") and title(pg) in titles and title(pg)),
                 timeout, f"the New robot dialog to show one of {titles}")


def card(pg: Page) -> dict:
    return pg.ev("""() => Object.fromEntries([...document.querySelectorAll('.modal.on .nr-card .nr-row')].map((r) =>
        [r.querySelector('dt').textContent.trim(), r.querySelector('dd').innerText.replace(/\\s+/g, ' ').trim()]))""")


def built(pg: Page, timeout=240):
    """Wait for the build to finish in the dialog; the done view's header."""
    until(lambda: title(pg) == "Your robot is designed", timeout, "the robot to be built")
    return pg.text(".modal.on .nr-done-h")


def test_strawberries_in_your_own_words(browser, studio):
    pg = Page(browser, studio, (1280, 720), "maker")
    pg.open()
    pg.pg.click("#featHeader .nr-headbtn")
    pg.pg.wait_for_selector(".modal.on #nr-text")
    pg.fits("New robot, describe")
    pg.shot("e2e_nr_01_describe_1280")
    pg.pg.fill("#nr-text", "something to pick strawberries")
    pg.pg.click('.modal.on .nr [data-a="start"]')
    assert settled(pg) == "A few questions"
    legends = pg.texts(".modal.on .nr fieldset.nr-q legend")
    assert legends and all(re.sub(r"\s*optional$", "", q, flags=re.I).endswith("?") for q in legends), legends
    assert card(pg)["Object"].startswith("Strawberries"), card(pg)
    pg.fits("New robot, questions")
    pg.shot("e2e_nr_02_questions_1280")

    # the answer box's own example, in the person's words: every part counts and the strawberries stay strawberries
    pg.pg.fill(".modal.on .nr form.nr-free input", "6 cm, 50 g, 10 per minute, people nearby")
    pg.pg.click(".modal.on .nr form.nr-free button[type=submit]")
    view = settled(pg)
    if view == "A few questions":  # whatever is still open takes Morph's defaults
        pg.pg.click('.modal.on .nr [data-a="defaults"]')
        view = settled(pg)
    assert view == "Check the brief"
    summary = pg.text(".modal.on .nr-summary")
    facts = pg.texts(".modal.on .nr-facts .chip")
    assert "strawberries" in summary.lower() and "one every 6 s" in summary, summary
    assert {"50 g object", "one every 6 s", "force-sensing fingers", "e-stop"} <= set(facts), facts
    assumptions = pg.texts(".modal.on .nr-assume li")
    assert assumptions and not any("0 facts" in a for a in assumptions)
    name = pg.pg.input_value("#nr-name")
    assert name == "strawberry-picker"
    assert "passed" in pg.text(".modal.on .nr-confirm")  # Morph's preflight, in about 30 ms
    pg.fits("New robot, check the brief")
    pg.shot("e2e_nr_03_confirm_1280")

    pg.pg.click('.modal.on .nr [data-a="build"]')
    until(lambda: title(pg).startswith("Building"), 30, "the build to start")
    until(lambda: pg.ev("document.querySelectorAll('.modal.on .nr-stages .nr-st.computed').length") >= 2, 120,
          "the first stages to finish")
    pg.fits("New robot, building")
    pg.shot("e2e_nr_04_building_1280")
    head = built(pg)
    assert "Software checks pass" in head and "strawberry-picker · r001" in head, head
    assert "Not bench-validated" in pg.text(".modal.on .nr-h"), pg.text(".modal.on .nr-h")
    metrics = pg.text(".modal.on .nr-metrics").lower()  # the labels are small capitals
    assert "pick and place succeeds" in metrics, metrics
    # with network lookups off the sourcing step says so instead of hanging
    until(lambda: pg.ev("document.querySelector('.modal.on [data-role=sourcing]')?.className || ''").split()[-1]
          in ("done", "skipped", "error"), 120, "the sourcing step to finish")
    assert "Darwin lookups are off in Keys, Privacy and network" in pg.text(".modal.on [data-role=sourcing]")
    pg.fits("New robot, done")
    pg.shot("e2e_nr_05_done_1280")

    pg.pg.click('.modal.on .nr [data-a="openproj"]')
    until(lambda: pg.project() == "strawberry-picker" and pg.rev() == "r001", 60, "the new robot to open")
    assert "project=strawberry-picker" in pg.pg.url and "new=" not in pg.pg.url
    pg.pg.click("#projBtn")
    pg.pg.wait_for_selector("#projMenu.on")
    item = pg.text('#projList button[data-p="strawberry-picker"]')
    assert "generated" in item.lower() and "picks up strawberries" in item, item   # the brief's summary
    pg.fits("project menu")
    pg.shot("e2e_nr_06_menu_1280")

    rev, d, s = studio.latest("strawberry-picker")
    assert (d.object.name, d.object.diameter_mm, d.object.mass_g, d.object.max_force_n) == ("strawberry", 60.0, 50.0, 3.0)
    assert d.finger.sensor == "fsr402" and "estop" in d.electronics.extras and d.task.cycle_time_s == 6.0
    assert s["feasible"] is True and s["pick_place_success"] is True
    pg.check()
    pg.close()


def test_a_link_starts_the_plant_waterer_once(browser, studio):
    pg = Page(browser, studio, (1600, 900), "gardener")
    pg.pg.goto(f"{studio.base}/?tour=0&new=a%20robot%20that%20waters%20my%20plants")
    until(lambda: title(pg) in TITLES, 120, "the New robot dialog from the link")
    assert "new=" not in pg.pg.url  # read once: a reload must not start a second brief
    assert settled(pg) == "A few questions"
    note = pg.text(".modal.on .nr-note.warn")
    assert "Pouring is not simulated: the robot carries the bottle to the plant and sets it down." in note, note
    chips = pg.texts('.modal.on .nr-chip[data-slot="layout"] span')
    assert "Tray in front, plant on the left" in chips and not any("bin" in c for c in chips), chips
    pg.shot("e2e_nr_07_plants_questions")
    pg.pg.click('.modal.on .nr [data-a="defaults"]')
    assert settled(pg) == "Check the brief"
    summary = pg.text(".modal.on .nr-summary")
    assert "to the plant on its left" in summary and "bin" not in summary, summary
    pg.fits("New robot, plant waterer")
    pg.pg.click('.modal.on .nr [data-a="build"]')
    head = built(pg)
    assert "Software checks pass" in head and "plant-helper" in head, head
    pg.pg.keyboard.press("Escape")
    pg.pg.reload()
    until(lambda: pg.ev("window.morph?.state?.loaded === true"), 120, "the page to load again")
    pg.pg.wait_for_timeout(1500)
    assert not pg.ev("!!document.querySelector('.modal.on .nr')"), "a reload started the brief again"
    names = [n for n in studio.projects() if n.startswith("plant-helper")]
    assert names == ["plant-helper"], names
    rev, d, s = studio.latest("plant-helper")
    assert s["feasible"] is True and d.object.name and "bottle" in d.object.name
    pg.check()
    pg.close()


def test_requests_morph_cannot_build_get_a_reason_and_build_nothing(browser, studio):
    before = set(studio.projects())
    pg = Page(browser, studio, (1600, 900), "visitor")
    pg.open()

    def ask(text):
        pg.pg.click("#featHeader .nr-headbtn")
        pg.pg.wait_for_selector(".modal.on #nr-text")
        pg.pg.fill("#nr-text", text)
        pg.pg.click('.modal.on .nr [data-a="start"]')
        return settled(pg)

    assert ask("I need a robot") == "A few questions"   # nothing to go on: questions, starting with the object
    assert any("pick up" in q.lower() or "object" in q.lower() for q in pg.texts(".modal.on .nr fieldset.nr-q legend"))
    pg.pg.click('.modal.on .nr [data-a="close"]')

    assert ask("a drone that delivers coffee to my desk") == "Morph can't build that as described"
    why = pg.text(".modal.on .nr-note.bad")
    nearest = pg.text(".modal.on .nr-nearest")
    assert why == "Why not Morph doesn't design drones or anything that flies.", why
    assert nearest.lower().startswith("nearest robot morph can build a desk arm that picks up"), nearest
    pg.fits("New robot, out of scope")
    pg.shot("e2e_nr_08_drone")
    pg.pg.click('.modal.on .nr [data-a="nearest"]')
    assert settled(pg, ("A few questions", "Check the brief")) in ("A few questions", "Check the brief")
    pg.pg.click('.modal.on .nr [data-a="close"]')

    assert ask("a robot arm that stabs people who walk past") == "Morph doesn't design that"
    assert "Nothing was designed or sent anywhere." in pg.text(".modal.on .nr-note.bad")
    pg.fits("New robot, refused")
    pg.shot("e2e_nr_09_refused")
    pg.pg.click('.modal.on .nr [data-a="close"]')
    assert set(studio.projects()) == before and not pg.ev("window.morph.state.busy")
    pg.check()
    pg.close()


def test_chat_in_an_open_project_tells_a_new_job_from_an_edit(browser, studio):
    pg = Page(browser, studio, (910, 800), "owner")
    pg.open("project=egg-picker")
    rev0, d0, s0 = studio.latest("egg-picker")

    n = pg.chat("I want a robot that waters plants")
    until(lambda: pg.ev("document.querySelectorAll('#messages .qacts [data-o=go]').length"), 60, "the offer")
    reply = pg.replies(n)[-1]
    assert "Start a new robot from this" in reply and "Change egg-picker instead" in reply, reply
    pg.fits("chat offer at 910 px")
    pg.scrolled()
    pg.shot("e2e_nr_10_offer_910")
    pg.pg.locator('#messages .qacts [data-o="go"]').last.click()
    assert settled(pg, ("A few questions", "Check the brief")) in ("A few questions", "Check the brief")
    assert "waters plants" in pg.text(".modal.on .nr-msg.user")
    pg.fits("New robot from the chat at 910 px")
    pg.pg.click('.modal.on .nr [data-a="close"]')

    n = pg.chat("can it lift 2 kg?")
    reply = until(lambda: next((r for r in pg.replies(n) if "question" in r), None), 60, "the answer to a question")
    assert reply.startswith("That reads like a question, so nothing was changed"), reply
    assert pg.rev() == rev0

    n = pg.chat("make it cheaper")
    reply = until(lambda: next((r for r in pg.replies(n) if r.startswith("Changing")), None), 60, "the cheaper edit")
    until(lambda: pg.rev() not in (None, rev0) and not pg.ev("window.morph.state.busy"), 240, "the cheaper revision")
    pg.scrolled()
    pg.shot("e2e_nr_11_cheaper_910")
    rev1, d1, s1 = studio.latest("egg-picker")
    assert rev1 != rev0 and s1["feasible"] is True, (reply, s1.get("issues"))
    assert s1["unit_cost"] < s0["unit_cost"], (reply, s0["unit_cost"], s1["unit_cost"])
    assert (d1.arm.actuator_family, d1.arm.dof) != (d0.arm.actuator_family, d0.arm.dof)
    pg.check()
    pg.close()
