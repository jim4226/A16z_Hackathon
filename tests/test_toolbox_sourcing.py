"""Live prices (beta), the price-check agent (morph/studio/agents.py): its four core tools (the parts list, Darwin search,
a request for a tool Morph lacks, the price record that keeps only what a tool's answer in this run shows), starting it
from the Studio, the after-build check that runs only when its setting is on and the purchased parts changed, and
Darwin's status and question kept. Claude is the real SDK over a mock transport (toolbox_helpers.ScriptedClaude); Mouser
answers in-process with the recorded answer of tests/test_toolbox_vendors.py; Darwin's answers are recorded too."""

import io
import json
import os

import pytest

from morph import darwin, keys, settings
from morph.robot.pipeline2 import RobotProject
from morph.studio import agents, api_tools, server
from morph.studio.toolbox import audit, store
from test_team_util import build
from test_toolbox_vendors import MOUSER_KEY, Vendor
from toolbox_helpers import ScriptedClaude, final, search_and_call, secrets_absent

ITEMS = ("Feetech STS3215 serial bus servo 12 V", "ESP32-S3-DevKitC-1")
NEEDS_INPUT = {"searchId": "s_ni", "status": "needs_input", "response": {  # Darwin's answer shape, recorded 2026-10-13
    "agents": [{"name": "Request a quote from matching suppliers", "description": "Sends your RFQ to suppliers.",
                "readiness": "unavailable", "canStartThread": False, "agentId": "cmv08cln8001t06nyw4bux80d",
                "capabilityId": "c_q", "reasons": [], "uncertainties": []}],
    "question": "Could you please provide your work email and confirm that you consent to sharing this request and your "
                "email with suppliers?", "noMatchReason": None}}


def make(root, rev, parent=None, changes=None):
    """test_team_util.build, with each BOM line bought or made as the pipeline's BOM stage says."""
    res = build(str(root), rev, parent, changes)
    path = os.path.join(str(root), rev, "results.json")
    for r in res["bom"]["rows"]:
        r["make_or_buy"] = "make" if r["category"] == "fabricated" else "buy"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(res, fh)
    return res


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("MORPH_TOOL_MOUSER_KEY", "unset")
    monkeypatch.delenv("MORPH_TOOL_MOUSER_KEY")
    monkeypatch.setattr(keys, "ENV_PATH", str(tmp_path / ".env"))
    monkeypatch.setattr(darwin, "CACHE", str(tmp_path / "darwin-cache"))  # never the person's own cache
    home = tmp_path / "projects"
    root = home / "egg-picker"
    make(root, "r001")
    return {"home": str(home), "root": str(root)}


def studio(env, claude, vendor=None, events=None):
    vendor = vendor or Vendor()
    st = server.Studio(RobotProject(env["root"]), env["home"])
    tb = st.toolbox
    tb.client_factory, tb.spawn = claude.client, (lambda fn: fn())
    tb.http_transport, tb.http_resolver, tb.network = vendor.transport, vendor.resolver, (lambda: True)
    if events is not None:
        tb.emit_fn = events.append
    return st, vendor


def results(body):
    return [json.loads(b["content"][0]["text"]) for b in body["messages"][-1]["content"]]


# ------------------------------------------------------------------ the parts list
def test_bom_lines_are_the_purchased_catalogue_items(env):
    p = RobotProject(env["root"])
    lines = agents.bom_lines(p, "r001")
    assert [ln["item"] for ln in lines] == list(ITEMS)  # "Base shell" is fabricated: not a distributor's item
    assert lines[0] == {"item": ITEMS[0], "category": "actuator", "qty": 6, "estimate_unit_usd": None, "estimate_vendor": None}
    res = json.load(open(os.path.join(env["root"], "r001", "results.json"), encoding="utf-8"))
    res["bom"]["rows"] += [{"item": "Morph controller PCBA", "category": "pcb", "qty": 1, "make_or_buy": "buy"},
                           {"item": "PETG filament", "category": "material", "qty": 1, "make_or_buy": "buy"},
                           {"item": ITEMS[1], "category": "electronics", "qty": 1, "make_or_buy": "buy"},
                           {"item": "M3 screw", "category": "fastener", "qty": 40, "make_or_buy": "buy",
                            "selected": {"vendor": "amazon", "unit_price": 0.05}}]
    for r in res["bom"]["rows"]:
        r.setdefault("make_or_buy", "buy" if r["category"] != "fabricated" else "make")
    json.dump(res, open(os.path.join(env["root"], "r001", "results.json"), "w", encoding="utf-8"))
    lines = agents.bom_lines(p, "r001")
    assert [ln["item"] for ln in lines] == [*ITEMS, "M3 screw"] and lines[-1]["estimate_unit_usd"] == 0.05
    assert agents.bom_lines(p, "r999") == []


# ------------------------------------------------------------------ a price check, end to end
def test_a_price_check_reads_the_bom_pauses_for_the_mouser_key_and_records_only_what_mouser_said(env):
    ev = []

    def turn2(body):
        bom = results(body)[0]
        assert bom["rev"] == "r001" and [ln["item"] for ln in bom["lines"]] == list(ITEMS)
        return search_and_call(("mouser_search_keyword", "toolu_m", {"keyword": "30-81794"}))

    def turn3(body):
        assert results(body)[0]["data"]["parts"][0]["PriceBreaks"][0]["Price"] == "$7.95"
        lines = [  # matched; a price Mouser never gave; a part that isn't in the parts list; a tool never called
            {"item": ITEMS[0].upper(), "distributor": "Mouser", "part_number": "934-30-81794", "unit_price": 7.95,
             "currency": "USD", "quantity": 1, "stock": 1286, "tool": "mouser_search_keyword"},
            {"item": ITEMS[1], "distributor": "Mouser", "part_number": "934-30-81794", "unit_price": 3.21, "currency": "USD",
             "quantity": None, "stock": None, "tool": "mouser_search_keyword"},
            {"item": "Flux capacitor", "distributor": "Mouser", "part_number": "FC-1", "unit_price": 1.0, "currency": None,
             "quantity": None, "stock": None, "tool": "mouser_search_keyword"},
            {"item": ITEMS[1], "distributor": "Farnell", "part_number": "1360825", "unit_price": 0.78, "currency": None,
             "quantity": 10, "stock": 518, "tool": "element14_search_keyword"}]
        return {"content": [{"type": "tool_use", "id": "toolu_rec", "name": "morph_prices_record", "input": {"lines": lines}}],
                "stop_reason": "tool_use"}

    def turn4(body):
        rec = results(body)[0]
        assert rec["recorded"] == 2 and [x["matched"] for x in rec["lines"]] == [True, False]
        assert [d["why"] for d in rec["dropped"]] == ["not a line of this parts list", "no answer from that tool in this run"]
        return final("Priced the STS3215 line at Mouser ($7.95). Nothing was ordered.")

    claude = ScriptedClaude(search_and_call(("morph_bom_read", "toolu_b", {}), query="parts list"), turn2, turn3, turn4)
    st, vendor = studio(env, claude, events=ev)
    room = st.room()
    view, already = agents.start_price_check(st, room, {}, "m_ana")
    assert not already and view["status"] == "paused" and view["waiting_on"] == ["credential"]
    assert view["title"] == "Live prices for r001" and view["context"] == {"rev": "r001", "auto": False, "lines": 2}
    req = claude.requests[0]
    assert req["system"][0]["text"].startswith("You are the price-check agent in Morph Studio")
    core = [t["name"] for t in req["tools"] if not t.get("defer_loading")]
    assert core == ["tool_search_tool_bm25", "darwin_search", "morph_bom_read", "morph_prices_record", "morph_tools_request"]
    assert all(t.get("strict") for t in req["tools"][1:5]) and req["output_config"] == {"effort": "medium"}
    assert agents.start_price_check(st, room, {"rev": "r001"}, None) == (st.toolbox.get(view["id"]), True)  # one at a time
    st.toolbox.on_credentials_saved(keys.save({"MORPH_TOOL_MOUSER_KEY": MOUSER_KEY}, home=env["home"]))
    done = st.toolbox.get(view["id"])
    assert done["status"] == "done" and len(vendor.requests) == 1
    lines = done["result"]["lines"]
    assert [(x["item"], x["matched"], x["service"]) for x in lines] == [(ITEMS[0], True, "Mouser Electronics"),
                                                                       (ITEMS[1], False, "Mouser Electronics")]
    assert lines[0]["unit_price"] == 7.95 and lines[0]["stock"] == 1286 and done["result"]["rev"] == "r001"
    assert st.toolbox.answer(view["id"]).startswith("Priced the STS3215 line")
    assert [e["title"] for e in ev if e["type"] == "tool_used"] == ["Parts list", "Mouser keyword search", "Price record"]
    assert secrets_absent([claude.requests, ev, done], MOUSER_KEY)


def test_darwin_search_passes_darwins_question_on_and_never_answers_it(env, monkeypatch):
    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    sent = []
    monkeypatch.setattr(darwin, "urlopen", lambda req, timeout: sent.append(json.loads(req.data)) or Resp(json.dumps(NEEDS_INPUT).encode()))
    out = darwin.search("injection molding quote")
    assert out["status"] == "needs_input" and out["question"].startswith("Could you please provide your work email")
    assert out["agents"][0]["name"] == "Request a quote from matching suppliers" and sent == [{"query": "injection molding quote",
                                                                                               "maxResults": 5}]
    ctx = {"home": env["home"], "project": "egg-picker", "run": "run_" + "0" * 16, "agent": "price_check", "context": {}}
    got = agents._darwin_search({"query": "injection molding quote"}, ctx)
    assert got["status"] == "needs_input" and got["question"] == NEEDS_INPUT["response"]["question"]
    assert "never orders" in got["note"] and "Pass any question Darwin asks on to the person" in got["note"]
    assert agents._darwin_search({"query": "x"}, ctx)["error"] == "invalid_arguments"
    settings.save({"sourcing_enabled": False})  # Darwin lookups off: saved answers only, nothing sent
    sent.clear()
    assert agents._darwin_search({"query": "injection molding quote"}, ctx)["cached"] is True and sent == []


def test_a_need_no_tool_covers_becomes_a_card_and_a_local_log_line(env):
    ev = []
    claude = ScriptedClaude(search_and_call(("morph_tools_request", "toolu_t", {
        "need": "PCB assembly quotes from Gerbers", "why": "To price the controller board, which no tool here quotes."})),
        final("No assembly quote tool; noted."))
    st, _ = studio(env, claude, events=ev)
    view, _ = agents.start_price_check(st, st.room(), {}, None)
    done = st.toolbox.get(view["id"])
    assert done["status"] == "done" and done["requests"][0]["need"] == "PCB assembly quotes from Gerbers"
    card = next(e for e in ev if e["type"] == "tool_request")
    assert card["reason"] == "capability" and card["tool"] == "morph_tools_request" and card["title"] == "Tool request"
    assert results(claude.requests[-1])[0]["noted"] is True
    log = os.path.join(store.tools_dir(env["home"]), "requests.jsonl")
    line = json.loads(open(log, encoding="utf-8").read().splitlines()[0])
    assert line["need"] == "PCB assembly quotes from Gerbers" and line["run"] == view["id"] and line["project"] == "egg-picker"
    assert oct(os.stat(log).st_mode & 0o777) == "0o600"
    events = [e["event"] for e in audit.entries(env["home"])]
    assert "tool_requested" in events and audit.verify(env["home"])["ok"]
    assert "PCB assembly" not in open(audit.path(env["home"]), encoding="utf-8").read()  # names only in the audit log


def test_starting_a_price_check_needs_a_built_revision_with_purchased_parts(env):
    st, _ = studio(env, ScriptedClaude())
    room = st.room()
    with pytest.raises(ValueError, match="isn't built"):
        agents.start_price_check(st, room, {"rev": "r009"}, None)
    res = json.load(open(os.path.join(env["root"], "r001", "results.json"), encoding="utf-8"))
    res["bom"]["rows"] = [r for r in res["bom"]["rows"] if r["category"] == "fabricated"]
    json.dump(res, open(os.path.join(env["root"], "r001", "results.json"), "w", encoding="utf-8"))
    with pytest.raises(ValueError, match="no purchased parts"):
        agents.start_price_check(st, room, {}, None)
    assert api_tools.AGENTS["price_check"]["title"] == "Live prices"


def test_two_starts_at_once_make_one_run(env, monkeypatch):
    """Two people press Live prices together: the slower listing of runs can't let both through."""
    import threading
    import time

    claude = ScriptedClaude(search_and_call(("morph_bom_read", "toolu_b", {}), query="parts list"),
                            search_and_call(("mouser_search_keyword", "toolu_m", {"keyword": "STS3215"})))
    st, _ = studio(env, claude)
    tb, real = st.toolbox, st.toolbox.runs

    def slow_runs(*a, **k):
        out = real(*a, **k)
        time.sleep(0.3)
        return out

    monkeypatch.setattr(tb, "runs", slow_runs)
    room, out = st.room(), []
    threads = [threading.Thread(target=lambda: out.append(agents.price_check(st, room, {}, None))) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert sorted(already for _, already in out) == [False, True] and len({v["id"] for v, _ in out}) == 1
    assert [v["status"] for v in real(project=room.name)] == ["paused"]  # waiting for the Mouser key, once


# ------------------------------------------------------------------ after a build
def test_after_a_build_live_prices_start_only_when_on_and_the_purchased_parts_changed(env, monkeypatch):
    started = []
    monkeypatch.setattr(agents, "start_price_check", lambda st, room, body, by, auto=False: started.append((body, by, auto)))
    st, _ = studio(env, ScriptedClaude())
    room = st.room()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-a-real-key")

    def after(rev):
        t = api_tools.after_build(st, room, rev)
        if t is not None:
            t.join(10)
        return t is not None

    assert not after("r001")  # off by default: no surprise Claude spend
    settings.save({"auto_price_check": True})
    assert after("r001") and started[-1] == ({"rev": "r001"}, None, True)  # a first build: every part is new
    make(env["root"], "r002", "r001", {"arm.reach_mm": 420.0})  # same servos and board: nothing to price again
    assert not after("r002")
    make(env["root"], "r003", "r002", {"arm.actuator_family": "dynamixel"})
    assert after("r003") and started[-1][0] == {"rev": "r003"}
    settings.save({"agent_tools_enabled": False})
    assert not after("r003")
    settings.save({"agent_tools_enabled": True, "network_enabled": False})
    assert not after("r003")
    settings.save({"network_enabled": True})
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert not after("r003") and len(started) == 2


def test_a_studio_build_calls_the_after_build_hook(env, monkeypatch):
    seen = []
    monkeypatch.setattr(api_tools, "after_build", lambda st, room, rev: seen.append((room.name, rev)))
    monkeypatch.setattr(RobotProject, "run", lambda self, design, parent=None, label="", **kw:
                        make(self.root, self.claim_rev(), parent, {}) and self.revs()[-1])
    st, _ = studio(env, ScriptedClaude())
    rev = st.apply({"arm.reach_mm": 410.0}, "longer reach")
    assert rev == "r002" and seen == [("egg-picker", "r002")]
