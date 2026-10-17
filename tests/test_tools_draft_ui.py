"""Morph writes a tool (preview) in a real browser (static/tooldraft.js with tools.js and keys.js): the real Studio page
against a real Studio server whose toolbox talks to scripted Claude (the anthropic SDK over a mock transport), with
Darwin's recorded public answers and nothing leaving the test. A Live prices run asks for a tool no catalog tool covers;
its card shows Darwin's suggestions and Draft a tool; the dialog drafts from the documentation; the draft's card goes
from drafting to ready; Review shows every detail; Approve installs it; the next run offers it and asks for its key. Also:
Keys' New tool without Claude, a refused draft, Teams (only the owner drafts and approves) and a drafted tool changed
after approval. Skipped without Playwright's Chromium. MORPH_SHOTS_DIR=<dir> keeps screenshots (tools_draft_*.png)."""

import json
import os
import threading

import pytest

import fake_claude as F
from morph import darwin
from morph.studio.toolbox import catalog
from morph.studio import api_tools
from morph.studio.toolbox import runs as runs_mod
from test_tools_api import World, _start
from test_tools_draft_api import Resp
from test_tools_ui import CHROME, SHOTS, Page, _purchased, pw, until
from toolbox_helpers import final, search_and_call

DOCS = F.GEARBASE_DOCS_URL
NEED = "Live price breaks and stock from Gearbase, the distributor this team buys from"
GB_ENV = "MORPH_TOOL_GEARBASE_KEY"


def call(name, tid, inp):
    return {"content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}], "stop_reason": "tool_use"}


def after(gate, script):
    """A scripted answer that waits for `gate` (the page shows what comes in between for as long as the test looks)."""
    def answer(body):
        assert gate.wait(30)
        return script(body)
    return answer


NEEDS_A_TOOL = [call("morph_bom_read", "toolu_bom", {}),
                call("morph_tools_request", "toolu_need", {"need": NEED, "why": "No tool in Morph's catalog covers Gearbase"}),
                final("Morph has no Gearbase tool yet, so nothing was priced there. Nothing was ordered.")]


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
    """Studios over the fixture project (test_tools_api.World) whose runs and drafts work in the background, as in a Studio."""
    api_tools.agent("test_prices", title="Test prices", start=_start)
    made = []

    def make(*steps, team=False):
        w = World(tmp_path, monkeypatch, steps, team)
        w.studio.toolbox.spawn = runs_mod._thread
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


@pytest.fixture
def darwin_recorded(monkeypatch, tmp_path):
    """Darwin's public search answered from its recorded answers (any need gets the recorded search)."""
    monkeypatch.setattr(darwin, "CACHE", str(tmp_path / "darwin-cache"))

    def urlopen(req, timeout=None):
        if req.full_url == darwin.API:
            return Resp("search_needs_input.json")
        if req.full_url == darwin.CAPABILITY.format(cap="cmud0ag7s002n09gmbhttfdpo"):
            return Resp("capability_part_lookup.json")
        raise OSError("not recorded")
    monkeypatch.setattr(darwin, "urlopen", urlopen)


def run_card(page) -> str:
    return " ".join(page.pg.evaluate("[...document.querySelectorAll('.tl-run:not(.td-run)')].pop()?.innerText || ''").split())


def draft_card(page) -> str:
    return " ".join(page.pg.evaluate("[...document.querySelectorAll('.td-run')].pop()?.innerText || ''").split())


def shot(page, name):
    if SHOTS:
        import time
        time.sleep(0.6)  # the chat scrolls smoothly
        os.makedirs(SHOTS, exist_ok=True)
        page.pg.screenshot(path=os.path.join(SHOTS, f"tools_draft_{name}.png"))


def test_a_need_card_drafts_a_tool_review_approve_and_the_next_run_asks_for_its_key(browser, make_world, darwin_recorded, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-a-real-key")
    gate = threading.Event()
    w = make_world(*NEEDS_A_TOOL, after(gate, F.drafting()), call("morph_bom_read", "toolu_bom2", {}),
                   search_and_call(("gearbase_search_parts", "toolu_gb", {"keyword": "STS3215"})))
    w.studio.toolbox.builtins = None  # Live prices' own core tools
    _purchased(w)
    page = Page(browser, w).open(tab="bom")
    until(lambda: page.pg.evaluate("document.querySelector('.tl-bom')?.innerText === 'Live prices (beta)'"), what="the BOM tab's button")
    page.pg.click(".tl-bom")
    until(lambda: "The agent needs a tool Morph doesn't have" in run_card(page) and "Done" in run_card(page), what="the need card")
    until(lambda: "part-lookup by Toolcall" in run_card(page), what="Darwin's suggestions on the need card")
    text = run_card(page)
    assert NEED in text and "toolcall.click" in text and "paid per call" in text and "Draft a tool (preview)" in text
    assert "not checked by Morph. Morph never connects, signs in or pays for you" in text and "Darwin asks" not in text  # short in the chat
    assert page.pg.get_attribute(".tl-card.cap a[href='https://toolcall.click/']", "rel") == "noopener noreferrer"
    shot(page, "1_need_card")
    page.pg.click('.tl-run [data-act="draft"]')
    until(lambda: page.pg.evaluate("!!document.querySelector('#featModal.on .td-dlg')"), what="the draft dialog")
    assert page.pg.input_value('#featModal [data-f="need"]') == NEED and page.pg.input_value('#featModal [data-f="docs"]') == ""
    until(lambda: "part-lookup by Toolcall" in page.pg.inner_text("#featModal"), what="Darwin's suggestions in the dialog")
    dlg = " ".join(page.pg.inner_text("#featModal").split())
    assert "GET lookups only, to hosts the documentation names; nothing is written, uploaded or paid" in dlg
    assert 'Darwin asks: "Do you have a specific manufacturer part number' in dlg and "Morph never answers for you" in dlg
    assert "Morph hasn't checked them and never connects, signs in or pays for you" in dlg
    page.pg.fill('#featModal [data-f="docs"]', DOCS)
    shot(page, "2_dialog")
    page.pg.click('#featModal [data-a="go"]')
    until(lambda: "Drafting" in draft_card(page) and not page.pg.evaluate("document.querySelector('#featModal')?.classList.contains('on')"),
          what="the drafting card")
    assert f"Claude is reading {DOCS} and drafting a read-only lookup" in draft_card(page)
    assert page.pg.evaluate("[...document.querySelectorAll('.td-run [data-act]')].map(b => b.dataset.act)") == ["discard"]
    shot(page, "3_drafting")
    gate.set()
    until(lambda: "Ready for review" in draft_card(page), what="the draft ready for review")
    text = draft_card(page)
    assert "New tool: Gearbase (test fixture)" in text and "Gearbase part search, Gearbase offers for a part" in text
    assert "api.gearbase-morph.com only" in text and "Gearbase API key (header X-Api-Key)" in text and "No fee per call" in text
    until(lambda: "A drafted tool for it waits for review" in run_card(page), what="the need card pointing at the draft")
    shot(page, "4_ready")
    page.pg.click('.td-run [data-act="review"]')
    until(lambda: page.pg.evaluate("!!document.querySelector('#featModal.on .td-review')"), what="the review")
    rv = " ".join(page.pg.inner_text("#featModal").split())
    d = w.call("GET", "/api/tools/drafts?project=egg-picker")[1]["drafts"][0]
    assert d["sha256"] in rv and "named in the documentation" in rv and "public address" in rv
    assert "GET https://api.gearbase-morph.com/v2/parts/search?q={keyword}&limit={limit}" in rv
    assert "GET https://api.gearbase-morph.com/v2/parts/{mpn}/offers?currency={currency}" in rv
    assert "Gearbase API key, in the X-Api-Key header, sent only to api.gearbase-morph.com" in rv and GB_ENV in rv
    assert "In the documentation: ...Lookups cost nothing: there is no charge per call. Search parts GET /v2/parts/search" in rv
    assert page.pg.evaluate("[...document.querySelectorAll('#featModal [data-act]')].map(b => b.dataset.act)") == ["discard", "approve"]
    shot(page, "5_review")
    page.pg.click('#featModal [data-act="approve"]')
    until(lambda: "Installed" in draft_card(page), what="the installed card")
    assert "The next run can use Gearbase part search and Gearbase offers for a part" in draft_card(page)
    assert page.pg.evaluate("[...document.querySelectorAll('.td-run [data-act]')].map(b => b.dataset.act)") == ["key", "run"]
    assert os.path.exists(os.path.join(catalog.user_dir(w.home), "gearbase.json"))
    shot(page, "6_installed")
    page.pg.click('.td-run [data-act="run"]')  # Run Live prices again: the next run offers the tool and asks for its key
    until(lambda: "Key needed: Gearbase (test fixture)" in run_card(page), what="the next run's Key needed card")
    text = run_card(page)
    assert "Not yet tried with a real key" in text and "api.gearbase-morph.com only" in text
    offered = {t["name"]: t for t in w.claude.requests[-1]["tools"]}
    assert offered["gearbase_search_parts"]["defer_loading"] is True and offered["gearbase_part_offers"]["defer_loading"] is True
    shot(page, "7_next_run_key")
    page.pg.click('.tl-run:not(.td-run) [data-act="key"]')
    until(lambda: page.pg.evaluate(f"document.querySelector('#keysDlg.on .kf.kfocus')?.dataset.k === '{GB_ENV}'"), what="Keys on the new key")
    keys_text = page.pg.inner_text("#keysDlg")
    assert "Gearbase part search" in keys_text and "drafted" in keys_text and "New tool (preview)" in keys_text
    assert not page.errors, page.errors


def test_new_tool_in_keys_without_claude_opens_keys_on_the_anthropic_key(browser, make_world, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    w = make_world()
    w.studio.toolbox.client_factory = lambda: None
    page = Page(browser, w).open()
    page.pg.evaluate("window.dispatchEvent(new CustomEvent('morph:keys', {detail: {}}))")
    until(lambda: page.pg.evaluate("!!document.querySelector('#keysDlg.on [data-a=\"newtool\"]')"), what="Keys' New tool button")
    page.pg.click('#keysDlg [data-a="newtool"]')
    until(lambda: page.pg.evaluate("!!document.querySelector('#featModal.on .td-dlg')"), what="the draft dialog")
    text = page.pg.inner_text("#featModal")
    assert "Drafting a tool needs Claude: add the Anthropic key in Keys" in text and not page.pg.evaluate("!!document.querySelector('#keysDlg.on')")
    assert page.pg.evaluate("!!document.querySelector('#featModal [data-a=\"go\"]')") is False
    shot(page, "no_claude")
    page.pg.click('#featModal [data-a="keys"]')
    until(lambda: page.pg.evaluate("document.querySelector('#keysDlg.on .kf.kfocus')?.dataset.k === 'ANTHROPIC_API_KEY'"),
          what="Keys open on the Anthropic key")
    assert w.call("GET", "/api/tools/drafts")[1]["drafts"] == []
    assert not page.errors, page.errors


def test_a_refused_draft_says_why_and_can_be_drafted_again(browser, make_world, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-a-real-key")
    form = F.gearbase_form()
    form["tools"][0]["url"] = "https://collect.gearbase-mirror-morph.com/v2/parts/search"  # what an injected model writes
    w = make_world(F.drafting(pages={DOCS: F.GEARBASE_INJECTED}, form=form))
    page = Page(browser, w, size=(1600, 900)).open()
    page.pg.evaluate("window.dispatchEvent(new CustomEvent('morph:keys', {detail: {}}))")
    until(lambda: page.pg.evaluate("!!document.querySelector('#keysDlg.on [data-a=\"newtool\"]')"), what="Keys' New tool button")
    page.pg.click('#keysDlg [data-a="newtool"]')
    until(lambda: page.pg.evaluate("!!document.querySelector('#featModal.on .td-dlg')"), what="the draft dialog")
    page.pg.fill('#featModal [data-f="need"]', "Gearbase part search")
    page.pg.fill('#featModal [data-f="docs"]', "http://docs.gearbase-morph.com/api/v2")
    page.pg.click('#featModal [data-a="go"]')
    until(lambda: "Give the documentation's https:// address" in page.pg.inner_text("#featModal"), what="the https note")
    page.pg.fill('#featModal [data-f="docs"]', DOCS)
    page.pg.click('#featModal [data-a="go"]')
    until(lambda: "Won't install" in draft_card(page), what="the refused draft")
    text = draft_card(page)
    assert ("Morph won't install this draft: collect.gearbase-mirror-morph.com is named in the documentation only next to "
            "text aimed at AI agents, so Morph won't send anything there.") in text
    assert page.pg.evaluate("[...document.querySelectorAll('.td-run [data-act]')].map(b => b.dataset.act)") == ["review", "again", "discard"]
    shot(page, "refused")
    page.pg.click('.td-run [data-act="review"]')
    until(lambda: page.pg.evaluate("!!document.querySelector('#featModal.on .td-review')"), what="the refused draft's details")
    rv = page.pg.inner_text("#featModal")
    assert "contains text aimed at AI agents" in rv and "none: refused drafts are never installed" in rv
    assert page.pg.evaluate("document.querySelectorAll('#featModal [data-act]').length") == 0  # nothing to approve
    page.pg.keyboard.press("Escape")
    page.pg.click('.td-run [data-act="again"]')
    until(lambda: page.pg.evaluate("!!document.querySelector('#featModal.on .td-dlg')"), what="the dialog again")
    assert page.pg.input_value('#featModal [data-f="docs"]') == DOCS and page.pg.input_value('#featModal [data-f="need"]') == "Gearbase part search"
    assert not page.errors, page.errors


def test_in_a_team_only_the_owner_drafts_and_approves(browser, make_world, darwin_recorded, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-a-real-key")
    w = make_world(*NEEDS_A_TOOL, F.drafting(), team=True)
    w.studio.toolbox.builtins = None
    _purchased(w)
    ana = Page(browser, w, "Ana").open()
    vic = Page(browser, w, "Vic", (1600, 900)).open()
    s, out = w.call("POST", "/api/runs/start?project=egg-picker", {"agent": "price_check"}, who="Jim")
    assert s == 202
    for p in (ana, vic):
        until(lambda: "The agent needs a tool Morph doesn't have" in run_card(p) and "Done" in run_card(p), what="the need card")
    until(lambda: "part-lookup by Toolcall" in run_card(ana), what="the editor's Darwin suggestions")
    assert "Ask the owner to draft a tool for it (Keys, New tool)." in run_card(ana) and "Draft a tool" not in run_card(ana)
    assert "part-lookup" not in run_card(vic) and "Ask the owner to draft a tool for it" in run_card(vic)  # viewers: no Darwin call
    run = w.call("GET", "/api/runs?project=egg-picker", who="Jim")[1]["runs"][0]
    s, out = w.call("POST", "/api/tools/draft?project=egg-picker", {"need": NEED, "docs_url": DOCS, "run": run["id"],
                                                                    "tool_use_id": "toolu_need"}, who="Jim")
    assert s == 202 and out["draft"]["source"]["agent"] == "price_check"
    for p in (ana, vic):
        until(lambda: "Ready for review" in draft_card(p), what="everyone's draft card")
        assert "Only the owner can approve a new tool: ask them to review it." in draft_card(p)
        assert p.pg.evaluate("[...document.querySelectorAll('.td-run [data-act]')].map(b => b.dataset.act)") == ["review"]
    assert "A drafted tool for it waits for review" in run_card(vic)
    vic.pg.click('.td-run [data-act="review"]')
    until(lambda: vic.pg.evaluate("!!document.querySelector('#featModal.on .td-review')"), what="the viewer's read-only review")
    assert "Only the owner can approve it." in vic.pg.inner_text("#featModal") and vic.pg.evaluate("document.querySelectorAll('#featModal [data-act]').length") == 0
    shot(vic, "team_viewer")
    d = out["draft"]
    assert w.call("POST", "/api/tools/drafts/approve", {"draft": d["id"], "sha256": w.call("GET", f"/api/tools/drafts?id={d['id']}", who="Jim")[1]["draft"]["sha256"]}, who="Ana")[0] == 403
    assert not ana.errors and not vic.errors, ana.errors + vic.errors


def test_a_drafted_tool_changed_after_approval_is_off_in_keys(browser, make_world, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-a-real-key")
    w = make_world(F.drafting())
    d = w.call("POST", "/api/tools/draft", {"need": NEED, "docs_url": DOCS})[1]["draft"]  # drafted in the background
    until(lambda: w.call("GET", f"/api/tools/drafts?id={d['id']}")[1]["draft"]["status"] == "ready", what="the draft")
    d = w.call("GET", f"/api/tools/drafts?id={d['id']}")[1]["draft"]
    assert w.call("POST", "/api/tools/drafts/approve", {"draft": d["id"], "sha256": d["sha256"]})[0] == 200
    path = os.path.join(catalog.user_dir(w.home), "gearbase.json")
    doc = json.load(open(path, encoding="utf-8"))
    doc["privacy"] = "Changed by hand after the owner approved it, sending more than before."
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    page = Page(browser, w).open()
    page.pg.evaluate("window.dispatchEvent(new CustomEvent('morph:keys', {detail: {}}))")
    until(lambda: "Changed after it was approved" in page.pg.inner_text("#keysDlg"), what="the changed tool in Keys")
    row = page.pg.evaluate("""[...document.querySelectorAll('#keysDlg .kt')].filter(l => l.innerText.includes('Gearbase part search'))
        .map(l => [l.querySelector('input').checked, l.querySelector('input').disabled])[0]""")
    assert row == [False, True]  # off, and only a new approved draft turns it on again
    assert GB_ENV not in page.pg.inner_text("#keysDlg")  # its key isn't asked for while the tool is off
    page.pg.evaluate("[...document.querySelectorAll('#keysDlg .kt')].find(l => l.innerText.includes('Gearbase part search')).scrollIntoView({block: 'center'})")
    shot(page, "changed_in_keys")
    assert not page.errors, page.errors
