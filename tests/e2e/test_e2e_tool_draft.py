"""Morph writes a tool (preview) end to end: the real Studio (`morph studio`, the demo robots fully built) and a real
browser, with Claude answered by the local stand-in in tests/fake_claude.py (its "tools" script, through
ANTHROPIC_BASE_URL, so the Studio runs its own code unchanged). The Studio runs through test_e2e_tools' script mode,
which refuses and records every connection and DNS lookup that isn't to 127.0.0.1 and keeps its .env in the test's own
folder: Claude's web fetch of the documentation is the stand-in's (a recorded page of the fictional Gearbase API), and
Darwin, the check that the API's host is public and the drafted tool's own call all fail offline, as the cards say.

- test_a_need_card_drafts_a_tool_the_next_run_offers_and_asks_its_key_for: Live prices asks for a tool no catalog tool
  covers; its need card offers Draft a tool; the dialog drafts from the documentation's address; the draft's card waits
  for review; the review shows hosts, lookups, where the key goes, cost, privacy and the spec's sha256; Approve installs
  it; Run Live prices again offers the new tools and pauses for the new key; the key is added and the call fails
  offline. A reload shows the same cards; the installed spec, its pin and the audit trail are on disk.
- test_new_tool_in_keys_refuses_documentation_that_steers_the_ai: Keys, New tool (preview), documentation with a
  paragraph planted for AI agents and a draft that sends lookups to the host named only there: Morph won't install it,
  and nothing is installed.

MORPH_E2E=1 runs these (the demo robots are built once per session, a few minutes; MORPH_E2E_DEMO=<dir> keeps them).
MORPH_E2E_PORTS picks the ports, MORPH_E2E_SHOTS=<dir> saves screenshots (t3e_*.png), PLAYWRIGHT_CHROMIUM names the
Chromium to use."""

import json
import os
import shutil
import sys
import threading
import time
import urllib.request

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
for d in (HERE, os.path.dirname(HERE)):  # live_studio and test_e2e_tools (tests/e2e), fake_claude (tests)
    if d not in sys.path:
        sys.path.insert(0, d)

from live_studio import SHOTS, Page, demo_home, free_port, launch, release_port, until  # noqa: E402
from test_e2e_tools import ToolsStudio, files_text  # noqa: E402

pytestmark = pytest.mark.skipif(os.environ.get("MORPH_E2E") != "1",
                                reason="end-to-end: MORPH_E2E=1 runs a real Studio and a browser (minutes)")

NEED = "Live price breaks and stock from Gearbase, the distributor this team buys from"
GB_ENV = "MORPH_TOOL_GEARBASE_KEY"
GEARBASE_KEY = "gb_live_" + "E2eNotARealKey".ljust(32, "0")  # the documented shape; no service has ever seen it
STEERED = "https://docs.gearbase-morph.com/api/v2/changelog"  # a page of the same site with a paragraph for AI agents
MIRROR = "collect.gearbase-mirror-morph.com"


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
    """A copy of the built demo robots and the stand-in Claude (the tools script; a draft's answer waits for `hold`, so
    the drafting card can be looked at). The steered page drafts a lookup at the host it plants."""
    import fake_claude as F

    home = tmp_path / "home"
    shutil.copytree(demo_home(tmp_path_factory), home)
    hold = threading.Event()
    steered = F.gearbase_form()
    steered["tools"][0]["url"] = f"https://{MIRROR}/v2/parts/search"  # what a model that followed the page writes

    def script(body):
        if F.is_draft(body):
            assert hold.wait(60), "the test never let the stand-in answer"
            if F.docs_url(body) == STEERED:
                return F.drafting(pages={STEERED: F.GEARBASE_INJECTED}, form=steered)(body)
        return F.needs_a_tool(body)

    claude = F.FakeClaude(script, port=free_port())
    made = []

    def studio(project="egg-picker"):
        s = ToolsStudio(str(home), claude.url, project)
        made.append(s)
        return s

    yield {"home": str(home), "claude": claude, "hold": hold, "studio": studio}
    hold.set()
    for s in made:
        s.stop()
    claude.close()
    release_port(claude.port)


def run_card(page) -> str:
    """The newest run card's text (a draft's card is a run card too, so it is left out)."""
    return " ".join(page.ev("[...document.querySelectorAll('.tl-run:not(.td-run)')].pop()?.innerText || ''").split())


def draft_card(page) -> str:
    return " ".join(page.ev("[...document.querySelectorAll('.td-run')].pop()?.innerText || ''").split())


def acts(page, sel) -> list:
    return page.ev(f"[...([...document.querySelectorAll('{sel}')].pop()?.querySelectorAll('[data-act]') || [])].map(b => b.dataset.act)")


def shot(page, name, chat=True):
    """A screenshot once the chat has scrolled to its newest card and the card has faded in (only when saving them)."""
    if not SHOTS:
        return
    if chat:
        try:
            page.scrolled()
        except AssertionError:
            pass
    time.sleep(0.6)
    page.shot(name)


def get(s, path):
    with urllib.request.urlopen(s.base + path, timeout=30) as r:
        return json.loads(r.read())


def audit_events(home) -> list:
    from morph.studio.toolbox import audit
    assert audit.verify(home)["ok"], audit.verify(home)
    return [e["event"] for e in reversed(audit.entries(home))]


# ------------------------------------------------------------------ the tests
def test_a_need_card_drafts_a_tool_the_next_run_offers_and_asks_its_key_for(world, browser):
    s = world["studio"]()
    page = Page(browser, s, (1280, 720), "Jim")
    try:
        # a Live prices run asks for a tool no catalog tool covers: its need card offers Draft a tool
        page.open("project=egg-picker&tab=bom")
        until(lambda: page.text(".tl-bom") == "Live prices (beta)", 30, "the BOM tab's Live prices button")
        page.pg.click(".tl-bom")
        until(lambda: "The agent needs a tool Morph doesn't have" in run_card(page) and "Done" in run_card(page), 60, "the need card")
        until(lambda: "Darwin couldn't be reached for suggestions." in run_card(page), 60, "Darwin's answer, offline")
        until(lambda: "Morph has no Gearbase tool yet" in run_card(page), 30, "Claude's summary")
        text = run_card(page)
        assert NEED in text and "Draft a tool (preview)" in text and "Noted in Morph's tool requests. The run carries on without it." in text
        page.fits("the need card")
        shot(page, "t3e_1_need_card")

        # the dialog: the need comes from the card, the documentation's address from the owner
        page.pg.locator(".tl-run:not(.td-run)").last.locator('[data-act="draft"]').click()
        until(lambda: page.ev("!!document.querySelector('#featModal.on .td-dlg')"), 30, "the draft dialog")
        assert page.pg.input_value('#featModal [data-f="need"]') == NEED
        import fake_claude as F
        page.pg.fill('#featModal [data-f="docs"]', F.GEARBASE_DOCS_URL)
        page.fits("the draft dialog")
        shot(page, "t3e_2_dialog", chat=False)
        page.pg.click('#featModal [data-a="go"]')
        until(lambda: "Drafting" in draft_card(page), 30, "the drafting card")
        assert f"Claude is reading {F.GEARBASE_DOCS_URL}" in draft_card(page) and acts(page, ".td-run") == ["discard"]
        asked = [b for b in world["claude"].requests if F.is_draft(b)]
        assert len(asked) == 1 and asked[0]["tools"][0]["allowed_domains"] == ["docs.gearbase-morph.com"]
        shot(page, "t3e_3_drafting")
        world["hold"].set()

        # the draft waits for review: Morph's checks, the hosts, the lookups, the key's placement and the spec's hash
        until(lambda: "Ready for review" in draft_card(page), 60, "the draft ready for review")
        text = draft_card(page)
        assert "New tool: Gearbase (test fixture)" in text and "api.gearbase-morph.com only" in text and "No fee per call" in text
        until(lambda: "A drafted tool for it waits for review" in run_card(page), 30, "the need card pointing at the draft")
        page.pg.locator(".td-run").last.locator('[data-act="review"]').click()
        until(lambda: page.ev("!!document.querySelector('#featModal.on .td-review')"), 30, "the review")
        d = get(s, "/api/tools/drafts?project=egg-picker")["drafts"][0]
        rv = " ".join(page.pg.inner_text("#featModal").split())
        assert d["status"] == "ready" and d["sha256"] in rv and "named in the documentation" in rv
        assert "GET https://api.gearbase-morph.com/v2/parts/search?q={keyword}&limit={limit}" in rv
        assert "Gearbase API key, in the X-Api-Key header, sent only to api.gearbase-morph.com" in rv and GB_ENV in rv
        assert "Morph couldn't look api.gearbase-morph.com up just now; it checks the address again before every call." in rv
        page.fits("the review")
        shot(page, "t3e_4_review", chat=False)
        page.ev("document.querySelector('#featModal .td-warn').scrollIntoView({block: 'center'})")
        shot(page, "t3e_4b_review_checks", chat=False)
        page.pg.click('#featModal [data-act="approve"]')
        until(lambda: "Installed" in draft_card(page), 30, "the installed card")
        assert "The next run can use Gearbase part search and Gearbase offers for a part" in draft_card(page)
        assert acts(page, ".td-run") == ["key", "run"]
        shot(page, "t3e_5_installed")

        # the next run offers the drafted tools and pauses for their key
        page.pg.locator(".td-run").last.locator('[data-act="run"]').click()
        until(lambda: "Key needed: Gearbase (test fixture)" in run_card(page), 60, "the next run's Key needed card")
        text = run_card(page)
        assert "Not yet tried with a real key" in text and "api.gearbase-morph.com only" in text
        assert acts(page, ".tl-run:not(.td-run)") == ["key", "skip", "stop"]
        offered = {t["name"]: t for t in world["claude"].requests[-1]["tools"]}
        assert offered["gearbase_search_parts"]["defer_loading"] is True and offered["gearbase_part_offers"]["defer_loading"] is True
        until(lambda: page.text(".tl-bom") == "Live prices: key needed", 10, "the button saying what the run waits for")
        page.fits("the Key needed card")
        shot(page, "t3e_6_next_run_key_needed")

        # the key is added in Keys; the run continues, and the drafted tool's call fails offline
        page.pg.locator(".tl-run:not(.td-run)").last.locator('[data-act="key"]').click()
        until(lambda: page.ev("document.querySelector('#keysDlg.on .kf.kfocus')?.dataset.k") == GB_ENV, 30, "Keys open on the Gearbase key")
        page.pg.fill("#keysDlg .kf.kfocus input", GEARBASE_KEY)
        page.pg.click('#keysDlg [data-a="save"]')
        until(lambda: page.ev("!!document.querySelector('#keysDlg .kchk.good')"), 30, "the saved note")
        page.pg.keyboard.press("Escape")
        until(lambda: "Gearbase couldn't be reached from this computer" in run_card(page), 60, "the summary after the offline call")
        text = run_card(page)
        assert "Done" in text and "Gearbase part search · couldn't reach api.gearbase-morph.com (offline or blocked)" in text
        shot(page, "t3e_7_done_offline")

        # a reload shows the same cards (from the runs and the drafts)
        page.open("project=egg-picker&tab=bom")
        until(lambda: "Installed" in draft_card(page) and "Gearbase couldn't be reached" in run_card(page), 30, "the cards after a reload")

        # on disk: the installed spec bound to the approved hash, its pin, and every step in the audit trail
        from morph.studio.toolbox import catalog
        spec = os.path.join(catalog.user_dir(world["home"]), "gearbase.json")
        assert catalog.fingerprint(json.load(open(spec, encoding="utf-8"))) == d["sha256"]
        assert catalog.read_pins(world["home"])["gearbase"]["sha256"] == d["sha256"]
        assert get(s, f"/api/tools/drafts?id={d['id']}")["draft"]["status"] == "installed"
        events = audit_events(world["home"])
        assert events.index("tool_draft_started") < events.index("tool_draft_ready") < events.index("tool_draft_approved")
        # the key went nowhere but the Studio's .env: not to the page, the files, the stand-in or the network
        assert GEARBASE_KEY not in page.pg.content() and GEARBASE_KEY not in files_text(world["home"])
        assert GEARBASE_KEY not in json.dumps(world["claude"].requests) and not any(GEARBASE_KEY in str(h) for h in world["claude"].headers)
        assert s.blocked_hosts() == {"api.darwin.so", "api.gearbase-morph.com"}  # Darwin, the DNS check and the call: refused
        page.check()
        assert not s.tracebacks(), s.tracebacks()
    finally:
        page.close()


def test_new_tool_in_keys_refuses_documentation_that_steers_the_ai(world, browser):
    s = world["studio"]()
    page = Page(browser, s, (1600, 900), "Jim")
    try:
        page.open("project=egg-picker")
        page.pg.click("#keysBtn")
        until(lambda: page.ev("!!document.querySelector('#keysDlg.on [data-a=\"newtool\"]')"), 30, "Keys' New tool button")
        page.pg.click('#keysDlg [data-a="newtool"]')
        until(lambda: page.ev("!!document.querySelector('#featModal.on .td-dlg')"), 30, "the draft dialog")
        page.pg.fill('#featModal [data-f="need"]', "Gearbase part search")
        page.pg.fill('#featModal [data-f="docs"]', STEERED)
        world["hold"].set()
        page.pg.click('#featModal [data-a="go"]')
        until(lambda: "Won't install" in draft_card(page), 60, "the refused draft")
        text = draft_card(page)
        assert (f"Morph won't install this draft: {MIRROR} is named in the documentation only next to text aimed at AI "
                "agents, so Morph won't send anything there.") in text
        assert acts(page, ".td-run") == ["review", "again", "discard"]
        page.fits("the refused draft")
        shot(page, "t3e_8_refused")
        page.pg.locator(".td-run").last.locator('[data-act="review"]').click()
        until(lambda: page.ev("!!document.querySelector('#featModal.on .td-review')"), 30, "the refused draft's details")
        rv = " ".join(page.pg.inner_text("#featModal").split())
        assert "contains text aimed at AI agents" in rv and page.ev("document.querySelectorAll('#featModal [data-act]').length") == 0
        d = get(s, "/api/tools/drafts?project=egg-picker")["drafts"][0]
        assert d["status"] == "refused" and d["sha256"] is None and "host_not_named" in {p["code"] for p in d["problems"]}
        assert not os.path.exists(os.path.join(world["home"], ".tools", "catalog")) or not os.listdir(os.path.join(world["home"], ".tools", "catalog"))
        assert "tool_draft_refused" in audit_events(world["home"]) and "tool_draft_approved" not in audit_events(world["home"])
        assert not any(t["name"].startswith("gearbase_") for t in get(s, "/api/tools")["tools"])
        assert MIRROR not in s.blocked_hosts()  # nothing was ever sent there, not even a DNS lookup
        page.check()
        assert not s.tracebacks(), s.tracebacks()
    finally:
        page.close()
