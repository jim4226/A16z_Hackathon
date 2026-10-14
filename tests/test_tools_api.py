"""The agent tools in the Studio (morph/studio/api_tools.py): every route of the toolbox, its roles in the full Teams
matrix (owner, editor, viewer, signed out), the room events a run sends, POST /api/keys resuming the runs that waited
for a key, the two settings, and a Studio start turning a run it left working into an interrupted pause. Claude is the
real SDK over a mock transport and the FixParts fixture API answers in-process (toolbox_helpers): nothing leaves the
test."""

import json
import os
import queue
import time

import pytest

from morph import keys, settings
from morph.robot.pipeline2 import RobotProject
from morph.studio import api_team, api_tools, server
from morph.studio.teams import activity, auth, roles
from morph.studio.toolbox import catalog, store
from test_studio_features import caller, start as serve
from test_team_util import build, make_team
from toolbox_helpers import (BOM, ENV, FIXTURES, KEY, SYSTEM, ScriptedClaude, Vendor, final, search_and_call,
                             secrets_absent)

OFFERS = ("fixparts_offers", "toolu_nx", {"mpn": "STS3215"})
CATS = ("fixparts_categories", "toolu_c", {"query": "servo"})
TEAM = (("Jim", "owner", ["generalist"]), ("Ana", "editor", ["sourcing"]), ("Vic", "viewer", []))
ROUTES = [("GET", "/api/tools", "viewer"), ("POST", "/api/tools/enable", "owner"), ("POST", "/api/tools/disable", "owner"),
          ("GET", "/api/runs", "viewer"), ("GET", "/api/runs/answer", "viewer"), ("POST", "/api/runs/start", "editor"),
          ("POST", "/api/runs/decide", "editor"), ("POST", "/api/runs/resume", "editor"), ("POST", "/api/runs/cancel", "editor")]


def _start(studio, room, body, by):
    tb = api_tools.toolbox(studio)
    for v in tb.runs(project=room.name, agent="test_prices"):
        if v["status"] in ("running", "paused"):
            return v, True
    return tb.start(agent="test_prices", system=SYSTEM, prompt="Price the purchased parts of r001.", project=room.name,
                    core=[BOM], by=by, title="Test prices"), False


@pytest.fixture(autouse=True)
def _agent():
    api_tools.agent("test_prices", title="Test prices", start=_start)
    yield
    api_tools.AGENTS.pop("test_prices", None)


class World:
    """A Studio over a fixture project whose toolbox talks to `claude` (scripted) and the in-process FixParts API."""

    def __init__(self, tmp_path, monkeypatch, steps=(), team=False):
        monkeypatch.setenv(ENV, "unset")
        monkeypatch.delenv(ENV)
        monkeypatch.setattr(keys, "ENV_PATH", str(tmp_path / ".env"))
        monkeypatch.setattr(catalog, "CURATED_DIRS", [FIXTURES])
        self.home = str(tmp_path / "projects")
        root = os.path.join(self.home, "egg-picker")
        build(root, "r001")
        self.claude, self.vendor = ScriptedClaude(*steps), Vendor()
        self.studio = server.Studio(RobotProject(root), self.home, team=team)
        tb = self.studio.toolbox
        tb.client_factory, tb.spawn = self.claude.client, (lambda fn: fn())  # drive runs in the request's thread
        tb.http_transport, tb.http_resolver = self.vendor.transport, self.vendor.resolver
        tb.network, tb.builtins = (lambda: True), {BOM.name: BOM}  # this Studio's core tools (an agent module registers its own)
        self.ids, self.jar = {}, {}
        if team:
            self.ids, cookies = make_team(self.home, TEAM)
        self.httpd = serve(self.studio)
        self.call_ = caller(self.httpd)
        port = self.httpd.server_address[1]
        self.jar = {n: f"{auth.cookie_name(port)}={c}" for n, c in cookies.items()} if team else {}

    def call(self, method, path, body=None, who=None):
        s, out, _ = self.call_(method, path, body, headers={"Cookie": self.jar[who]} if who else None)
        return s, out

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def world(tmp_path, monkeypatch):
    made = []

    def make(*steps, team=False):
        w = World(tmp_path, monkeypatch, steps, team)
        made.append(w)
        return w

    yield make
    for w in made:
        w.close()
    api_team.configure(home=None, enabled=False)
    auth.THROTTLE.clear()
    activity.PRESENCE.seen.clear()


# ------------------------------------------------------------------ a run, start to finish, over HTTP
def test_a_run_pauses_for_a_key_and_saving_it_in_keys_resumes_it(world):
    w = world(search_and_call(OFFERS), final("STS3215: 412 in stock at FixParts, $13.90."))
    q = w.studio.room().hub.subscribe()
    s, out = w.call("POST", "/api/runs/start?project=egg-picker", {"agent": "test_prices"})
    assert s == 202 and out["already"] is False
    run = out["run"]
    assert run["status"] == "paused" and run["waiting_on"] == ["credential"] and run["title"] == "Test prices"
    assert run["cards"][0]["type"] == "credential_needed" and run["cards"][0]["env"] == ENV
    s, again = w.call("POST", "/api/runs/start", {"agent": "test_prices"})
    assert s == 200 and again["already"] and again["run"]["id"] == run["id"]  # one paused run, not two
    s, listed = w.call("GET", "/api/runs?project=egg-picker")
    assert s == 200 and [r["id"] for r in listed["runs"]] == [run["id"]] and listed["can"]["allow"] is True
    assert listed["ready"]["agent_tools"] is True and {"name": "test_prices", "title": "Test prices"} in listed["agents"]
    s, out = w.call("POST", "/api/keys", {"values": {ENV: KEY}})
    assert s == 200 and out["resumed"] == [run["id"]] and out["changed"] == [ENV]
    s, done = w.call("GET", "/api/runs?status=done")
    assert [r["id"] for r in done["runs"]] == [run["id"]] and done["runs"][0]["cards"] == []
    s, ans = w.call("GET", f"/api/runs/answer?run={run['id']}")
    assert s == 200 and ans == {"run": run["id"], "status": "done", "text": "STS3215: 412 in stock at FixParts, $13.90."}
    events = []
    while not q.empty():
        events.append(q.get_nowait())
    kinds = [e["type"] for e in events if e["type"] not in ("queue",)]
    assert kinds == ["run_started", "credential_needed", "run_paused", "run_resumed", "tool_used", "run_done"]
    assert all(e["project"] == "egg-picker" and e["run"] == run["id"] and e["agent"] == "test_prices" for e in events)
    used = next(e for e in events if e["type"] == "tool_used")
    assert used["title"] == "FixParts price and stock" and used["host"] == "api.fixparts-morph.com" and used["ok"] is True
    assert next(e for e in events if e["type"] == "run_resumed")["keys"] == ["FixParts API key"]
    # the card's lines are kept with the run, so a page loaded later shows what the live events showed
    steps = done["runs"][0]["steps"]
    assert [s["type"] for s in steps] == ["key_saved", "tool_used"] and steps[0]["keys"] == ["FixParts API key"]
    assert {k: steps[1][k] for k in ("tool", "title", "ok", "host")} == {
        "tool": "fixparts_offers", "title": "FixParts price and stock", "ok": True, "host": "api.fixparts-morph.com"}
    assert secrets_absent(events, KEY) and secrets_absent(listed, KEY) and secrets_absent(done, KEY)
    assert "412 in stock" not in json.dumps(events + [done])  # message text only through /api/runs/answer


def test_skip_from_the_card_and_a_failed_call_says_why(world, monkeypatch):
    w = world(search_and_call(OFFERS), final("Carried on without FixParts."))
    run = w.call("POST", "/api/runs/start", {"agent": "test_prices"})[1]["run"]
    q = w.studio.room().hub.subscribe()
    s, out = w.call("POST", "/api/runs/decide", {"run": run["id"], "decisions": {"toolu_nx": "skip"}})
    assert s == 200 and out["run"]["status"] == "done"
    res = w.claude.requests[-1]["messages"][-1]["content"][0]
    assert res["is_error"] and json.loads(res["content"][0]["text"])["status"] == "skipped_by_user"
    events = []
    while not q.empty():
        events.append(q.get_nowait())
    assert [(e["trigger"], e["skipped"]) for e in events if e["type"] == "run_resumed"] == [  # every tab's card says it
        ("decision", [{"tool": "fixparts_offers", "title": "FixParts price and stock", "decision": "skip"}])]
    # a call that fails at the service: the tool_used event names the error and the HTTP status, never the answer
    w2 = world(search_and_call(CATS), final("ok"))
    q = w2.studio.room().hub.subscribe()
    monkeypatch.setattr(w2.vendor, "transport", lambda *a: (503, {"content-type": "application/json"}, b'{"detail": "down"}'))
    w2.studio.toolbox.http_transport = w2.vendor.transport
    run = w2.call("POST", "/api/runs/start", {"agent": "test_prices"})[1]["run"]
    assert run["waiting_on"] == ["approval"] and run["cards"][0]["type"] == "tool_request"
    assert w2.call("POST", "/api/runs/decide", {"run": run["id"], "decisions": {"toolu_c": "allow_once"}})[0] == 200
    used = []
    while not q.empty():
        e = q.get_nowait()
        if e["type"] == "tool_used":
            used.append(e)
    assert used == [{"type": "tool_used", "tool": "fixparts_categories", "title": "FixParts categories", "ok": False,
                     "ms": used[0]["ms"], "host": "api.fixparts-morph.com", "cost_usd": 0, "error": "http_error",
                     "status": 503, "run": run["id"], "project": "egg-picker", "agent": "test_prices"}]


def test_requests_the_routes_refuse(world):
    w = world()
    assert w.call("POST", "/api/runs/start", {"agent": "nobody"}) == (400, {"ok": False, "error": "There is no agent called nobody."})
    assert w.call("POST", "/api/runs/start", {"agent": ["test_prices"]})[0] == 400
    for bad in ({}, {"run": "nope"}, {"run": "run_" + "g" * 16}, {"run": 7}):
        assert w.call("POST", "/api/runs/cancel", bad)[0] == 400
    assert w.call("POST", "/api/runs/cancel", {"run": "run_" + "0" * 16})[0] == 404
    assert w.call("GET", "/api/runs/answer?run=run_" + "0" * 16)[0] == 404
    assert w.call("GET", "/api/runs?status=everything")[0] == 400
    assert w.call("GET", "/api/runs?agent=../x")[0] == 400
    assert w.call("GET", "/api/runs?limit=lots")[0] == 400
    assert w.call("GET", "/api/runs?project=../etc")[0] == 400
    assert w.call("GET", "/api/runs?project=nosuch")[0] == 404
    assert w.call("POST", "/api/tools/enable", {"tool": "nosuch_tool"})[0] == 404
    assert w.call("POST", "/api/tools/enable", {"tool": "../x"})[0] == 400
    assert w.call("POST", "/api/runs/decide", {"run": "run_" + "0" * 16, "decisions": {"t": "maybe"}})[0] == 400


def test_without_claude_a_start_says_how_to_fix_it(world):
    w = world()
    w.studio.toolbox.client_factory = lambda: None
    s, out = w.call("POST", "/api/runs/start", {"agent": "test_prices"})
    assert s == 503 and "add the Anthropic key in Keys" in out["error"]
    assert w.call("GET", "/api/runs")[1]["ready"]["claude"] is False


def test_agent_tools_off_stops_runs_starting_and_continuing_but_keys_still_save(world):
    w = world(search_and_call(OFFERS), final("ok"))
    run = w.call("POST", "/api/runs/start", {"agent": "test_prices"})[1]["run"]
    settings.save({"agent_tools_enabled": False})
    s, out = w.call("POST", "/api/runs/start", {"agent": "test_prices"})
    assert s == 409 and "Agent tools are off" in out["error"]
    assert w.call("POST", "/api/runs/decide", {"run": run["id"], "decisions": {"toolu_nx": "skip"}})[0] == 409
    assert w.call("POST", "/api/runs/resume", {"run": run["id"]})[0] == 409
    s, out = w.call("POST", "/api/keys", {"values": {ENV: KEY}})
    assert s == 200 and out["resumed"] == [] and w.studio.toolbox.get(run["id"])["status"] == "paused"
    assert w.studio.toolbox.approvals.get("fixparts_offers")["mode"] == "key_saved"  # recorded, so it won't ask later
    settings.save({"agent_tools_enabled": True})
    s, out = w.call("POST", "/api/runs/resume", {"run": run["id"]})
    assert s == 200 and out["run"]["status"] == "done"
    assert w.call("POST", "/api/runs/cancel", {"run": run["id"]})[1]["ended"] is True  # cancel always works


def test_enable_and_disable_a_tool(world):
    w = world()
    s, out = w.call("GET", "/api/tools")
    assert s == 200 and {t["name"] for t in out["tools"]} == {"fixparts_offers", "fixparts_categories", "fixparts_quote_request"}
    assert out["can"]["tools"] and out["ready"]["agent_tools"] and out["errors"] == []
    assert w.call("POST", "/api/tools/disable", {"tool": "fixparts_offers"}) == (200, {"ok": True, "tool": "fixparts_offers",
                                                                                      "enabled": False, "changed": True})
    assert not next(t for t in w.call("GET", "/api/tools")[1]["tools"] if t["name"] == "fixparts_offers")["enabled"]
    assert w.call("POST", "/api/tools/disable", {"tool": "fixparts_offers"})[1]["changed"] is False
    assert w.call("POST", "/api/tools/enable", {"tool": "fixparts_offers"})[1]["enabled"] is True


def test_a_studio_start_turns_a_run_it_left_working_into_an_interrupted_pause(world):
    w = world()
    rid = "run_" + "ab" * 8
    with open(os.path.join(w.home, "egg-picker", "r001", "results.json"), encoding="utf-8") as fh:
        assert json.load(fh)["rev"] == "r001"
    run = {"schema": "morph.run/1", "id": rid, "agent": "test_prices", "project": "egg-picker", "title": "Test prices",
           "status": "running", "created": 1e9, "updated": time.time(), "request": {}, "tools": {}, "core": [],
           "messages": [], "turns": 1, "pending": None, "calls": {}, "context": {}, "waiting_on": []}
    store.write_json(os.path.join(store.runs_dir(w.home), rid + ".json"), run)
    st = server.Studio(RobotProject(os.path.join(w.home, "egg-picker")), w.home)  # the Studio starts again
    view = st.toolbox.get(rid)
    assert view["status"] == "paused" and view["waiting_on"] == ["interrupted"]
    assert view["cards"] == [{"type": "run_interrupted", "run": rid, "project": "egg-picker", "reason": "restart",
                              "note": "The Studio stopped while this run was working."}]


def test_events_of_a_deleted_project_go_nowhere_and_a_run_without_one_reaches_every_tab(world):
    w = world()
    q = w.studio.room().hub.subscribe()
    api_tools._emit(w.studio, {"type": "run_paused", "project": "gone-project", "run": "run_" + "0" * 16})
    api_tools._emit(w.studio, {"type": "run_paused", "project": None, "run": "run_" + "1" * 16})
    got = [q.get(timeout=2)]
    with pytest.raises(queue.Empty):
        q.get(timeout=0.2)
    assert got[0]["run"] == "run_" + "1" * 16


# ------------------------------------------------------------------ Teams: the full role matrix
def test_every_tools_route_has_its_minimum_role_in_the_team_table():
    for method, path, role in ROUTES:
        assert roles.required(method, path) == (role, None), (method, path)
        for who, ok in (("owner", True), ("editor", role != "owner"), ("viewer", role == "viewer")):
            assert roles.authorize({"id": "m", "role": who}, method, path) is ok, (who, method, path)
        assert roles.authorize(None, method, path) is False


def test_the_role_matrix_over_http_on_a_team_studio(world):
    w = world(search_and_call(OFFERS), final("ok"), search_and_call(CATS), final("ok"), team=True)
    for who, can in (("Jim", {"start": True, "skip": True, "allow": True, "keys": True, "tools": True, "draft": True}),
                     ("Ana", {"start": True, "skip": True, "allow": False, "keys": False, "tools": False, "draft": False}),
                     ("Vic", {"start": False, "skip": False, "allow": False, "keys": False, "tools": False, "draft": False})):
        s, out = w.call("GET", "/api/runs", who=who)
        assert s == 200 and out["can"] == can, who
        assert w.call("GET", "/api/tools", who=who)[0] == 200
    assert w.call("GET", "/api/runs")[0] == 401 and w.call("GET", "/api/tools")[0] == 401
    for who in ("Ana", "Vic"):
        for path in ("/api/tools/enable", "/api/tools/disable"):
            s, out = w.call("POST", path, {"tool": "fixparts_offers"}, who=who)
            assert s == 403 and out["error"] == "Only the team's owners can do that.", (who, path)
    assert w.call("POST", "/api/tools/disable", {"tool": "fixparts_quote_request"}, who="Jim")[0] == 200
    s, out = w.call("POST", "/api/runs/start", {"agent": "test_prices"}, who="Vic")
    assert s == 403 and "Viewers can't change the design" in out["error"]
    s, out = w.call("POST", "/api/runs/start", {"agent": "test_prices"}, who="Ana")
    assert s == 202 and out["run"]["by"] == w.ids["Ana"] and out["run"]["waiting_on"] == ["credential"]
    rid = out["run"]["id"]
    s, out = w.call("POST", "/api/runs/decide", {"run": rid, "decisions": {"toolu_nx": "allow_once"}}, who="Ana")
    assert s == 403 and out["error"] == "Only the owner can allow or deny a tool; you can skip it."
    assert w.call("POST", "/api/runs/decide", {"run": rid, "decisions": {"toolu_nx": "skip"}}, who="Vic")[0] == 403
    assert w.call("POST", "/api/runs/cancel", {"run": rid}, who="Vic")[0] == 403
    assert w.call("POST", "/api/runs/resume", {"run": rid}, who="Vic")[0] == 403
    assert w.call("GET", f"/api/runs/answer?run={rid}", who="Vic")[1]["text"] is None  # not done yet
    assert w.call("POST", "/api/keys", {"values": {ENV: KEY}}, who="Ana")[0] == 403  # only the owner adds keys
    s, out = w.call("POST", "/api/runs/decide", {"run": rid, "decisions": {"toolu_nx": "skip"}}, who="Ana")
    assert s == 200 and out["run"]["status"] == "done"
    s, out = w.call("POST", "/api/runs/start", {"agent": "test_prices"}, who="Jim")
    rid2 = out["run"]["id"]
    assert out["run"]["waiting_on"] == ["approval"]  # FixParts categories needs no key: its first use asks
    s, out = w.call("POST", "/api/runs/decide", {"run": rid2, "decisions": {"toolu_c": "allow_always"}}, who="Jim")
    assert s == 200 and out["run"]["status"] == "done"
    assert w.call("POST", "/api/runs/resume", {"run": rid2}, who="Ana")[1]["run"]["status"] == "done"  # editors may
    assert w.call("POST", "/api/runs/cancel", {"run": rid2}, who="Ana")[1]["ended"] is True
    s, log = w.call("GET", "/api/audit", who="Jim")
    events = [e["event"] for e in log["entries"]]
    assert "tool_disabled" in events and events.count("tool_decision") == 2  # the team's own log has them too


def test_a_core_tool_can_record_what_it_found_and_ask_for_a_tool_morph_lacks(world, monkeypatch):
    from morph.studio.toolbox import Builtin

    def note(inp, ctx):
        ctx["record"]({"lines": [{"item": "Feetech STS3215", "unit_price": 13.9}], "secret": KEY})
        ctx["need_tool"]("PCB assembly quotes from Gerbers", f"No catalog tool quotes assembly {KEY}")
        return {"ok": True}

    rec = Builtin("morph_test_record", "A core tool that records a result and asks for a missing tool.",
                  {"type": "object", "properties": {}, "required": [], "additionalProperties": False}, note, "Test record")

    def start(studio, room, body, by):
        return api_tools.toolbox(studio).start(agent="test_prices", system=SYSTEM, prompt="Go.", project=room.name,
                                               core=[rec], by=by), False

    monkeypatch.setitem(api_tools.AGENTS, "test_prices", {"name": "test_prices", "title": "Test prices", "start": start})
    w = world(search_and_call(("morph_test_record", "toolu_r", {})), final("ok"))
    w.studio.toolbox.builtins[rec.name] = rec  # known by name, as an agent module's register() makes it
    q = w.studio.room().hub.subscribe()
    monkeypatch.setenv(ENV, KEY)  # a known key: it never reaches a view or an event, even from a core tool
    s, out = w.call("POST", "/api/runs/start", {"agent": "test_prices"})
    view = w.call("GET", "/api/runs")[1]["runs"][0]
    assert s == 202 and view["status"] == "done" and view["result"]["lines"][0]["unit_price"] == 13.9
    assert view["result"]["secret"] == "[redacted]" and view["requests"][0]["need"] == "PCB assembly quotes from Gerbers"
    assert view["requests"][0]["why"].endswith("[redacted]") and view["requests"][0]["tool_use_id"] == "toolu_r"
    events = []
    while not q.empty():
        events.append(q.get_nowait())
    req = next(e for e in events if e["type"] == "tool_request")
    assert req["reason"] == "capability" and req["tool"] == "morph_test_record" and req["title"] == "Test record"
    used = next(e for e in events if e["type"] == "tool_used")
    assert used["title"] == "Test record" and used["ok"] is True and "error" not in used
    assert secrets_absent([events, view], KEY)
