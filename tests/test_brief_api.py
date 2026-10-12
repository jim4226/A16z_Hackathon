"""POST/GET /api/brief handlers against the router contract (fake Request objects, no server): validation, the
conversation, the build hand-off, cancel, drafts, busy briefs and the SSE events they send."""

import json
import os
import threading
import time
from types import SimpleNamespace

import pytest

from morph.studio import agents, api_brief
from morph.studio import brief as B


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    for k in list(os.environ):
        if k.startswith(("ANTHROPIC", "KEEWANO", "SUBQ")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(api_brief, "STORE", api_brief.Store())


class Hub:
    def __init__(self):
        self.events = []

    def send(self, ev):
        self.events.append(ev)


def req(home, body=None, query=None, project="egg-picker"):
    hub = Hub()
    return SimpleNamespace(method="POST" if body is not None else "GET", path="/api/brief", body=body, query=query or {},
                           studio=SimpleNamespace(home=str(home)), room=SimpleNamespace(name=project, hub=hub), member=None,
                           handler=None), hub


def post(home, body, **kw):
    r, hub = req(home, body)
    st, out = api_brief.post(r, home=str(home), sync=True, **kw)
    return st, out, hub.events


def test_conversation_to_build(tmp_path):
    st, out, ev = post(tmp_path, {"text": "something to pick strawberries"})
    assert st == 200 and out["ok"] and B.ID_RE.match(out["brief_id"])
    bid, b = out["brief_id"], out["brief"]
    assert b["status"] == "asking" and b["questions"] and b["card"]
    assert [e["type"] for e in ev].count("brief") == 1 and ev[-1]["status"] == "asking" and ev[-1]["id"] == bid
    assert all(e["project"] == "egg-picker" for e in ev)
    assert {e["role"] for e in ev if e["type"] == "agent"} == {"triage", "designer", "feasibility"}
    assert os.path.isfile(os.path.join(os.path.realpath(tmp_path), ".briefs", bid + ".json"))
    st, out, ev = post(tmp_path, {"brief_id": bid, "answers": {"layout": "side_to_side"}})
    assert st == 200 and out["brief"]["slots"]["layout"]["source"] == "user"
    st, out, ev = post(tmp_path, {"brief_id": bid, "action": "defaults"})
    assert st == 200 and out["brief"]["status"] == "ready" and out["brief"]["preflight"]["issues"] == []
    calls = []
    st, out, ev = post(tmp_path, {"brief_id": bid, "action": "build", "name": "berry-bot"},
                       create_project=lambda payload: calls.append(payload) or {"project": payload["name"]})
    assert st == 200 and out["build"]["name"] == "berry-bot" and out["project"] == "berry-bot"
    assert calls[0]["meta"]["origin"] == "brief" and calls[0]["meta"]["demo"] is False and calls[0]["changes"]["name"] == "berry-bot"
    assert ev[-1]["type"] == "brief" and ev[-1]["status"] == "building"
    st, out, _ = post(tmp_path, {"brief_id": bid, "answers": {"layout": "short_hop"}})
    assert st == 409                                    # a building brief is final
    b = api_brief.on_built(str(tmp_path), bid, "berry-bot", "r001", project_root=str(tmp_path))
    assert b.status == "built" and json.load(open(tmp_path / "brief.json", encoding="utf-8"))["project"] == "berry-bot"
    assert not os.path.exists(os.path.join(os.path.realpath(tmp_path), ".briefs", bid + ".json"))


def test_build_from_asking_fills_defaults(tmp_path):
    _, out, _ = post(tmp_path, {"text": "make me a robot arm"})
    st, out, ev = post(tmp_path, {"brief_id": out["brief_id"], "action": "build"})
    assert st == 200 and out["build"]["name"] == "box-picker" and out["build"]["changes"]["object.name"] == "small box"
    assert "project" not in out                        # no project service passed: integration creates it


def test_out_of_scope_then_nearest(tmp_path):
    _, out, _ = post(tmp_path, {"text": "a drone that delivers packages"})
    assert out["brief"]["status"] == "out_of_scope" and out["brief"]["scope"]["nearest"]
    st, out, _ = post(tmp_path, {"brief_id": out["brief_id"], "action": "defaults"})
    assert out["brief"]["status"] == "ready" and out["brief"]["scope"]["status"] == "adjusted"


def test_refused_cannot_build(tmp_path):
    _, out, ev = post(tmp_path, {"text": "a robot that shoots nerf darts at my brother"})
    assert out["brief"]["status"] == "refused" and not [e for e in ev if e.get("role") == "designer"]
    st, out2, _ = post(tmp_path, {"brief_id": out["brief_id"], "action": "build"})
    assert st == 409 and "can't be built" in out2["error"]


def test_cancel_and_get(tmp_path):
    _, out, _ = post(tmp_path, {"text": "help me sort socks"})
    bid = out["brief_id"]
    r, _ = req(tmp_path, query={"id": [bid]})
    st, got = api_brief.get(r, home=str(tmp_path))
    assert st == 200 and got["brief"]["id"] == bid and got["busy"] is False
    st, out, ev = post(tmp_path, {"brief_id": bid, "action": "cancel"})
    assert st == 200 and out["status"] == "cancelled" and ev[-1] == {"type": "brief", "id": bid, "status": "cancelled", "project": "egg-picker"}
    st, _ = api_brief.get(r, home=str(tmp_path))
    assert st == 404


def test_drafts_survive_a_restart(tmp_path, monkeypatch):
    _, out, _ = post(tmp_path, {"text": "help me sort socks"})
    monkeypatch.setattr(api_brief, "STORE", api_brief.Store())   # a new server process: memory is gone
    r, _ = req(tmp_path, query={"id": [out["brief_id"]]})
    st, got = api_brief.get(r, home=str(tmp_path))
    assert st == 200 and got["brief"]["archetype"]["key"] == "soft_goods"


@pytest.mark.parametrize("body,status,msg", [
    ([], 400, "JSON object"), ({}, 400, "describe"), ({"text": 5}, 400, "string"), ({"text": "x" * 2001}, 400, "2000"),
    ({"text": "hi", "action": "explode"}, 400, "action"), ({"action": "defaults"}, 400, "start with"),
    ({"answers": {"layout": "short_hop"}}, 400, "start with"), ({"brief_id": "../../etc/passwd", "text": "x"}, 400, "brief_id"),
    ({"brief_id": "0123456789abcdef", "text": "x"}, 404, "no such brief"), ({"text": "x", "name": "../evil"}, 400, "names"),
    ({"brief_id": "0123456789abcdef", "answers": {"rm -rf": 1}}, 400, "unknown slot"),
    ({"brief_id": "0123456789abcdef", "answers": {"layout": {"a": [1, 2]}}}, 400, "bad value"),
    ({"brief_id": "0123456789abcdef", "answers": {"object": {"name": "x" * 5000}}}, 400, "too long"),
])
def test_validation(tmp_path, body, status, msg):
    r, _ = req(tmp_path, body)
    st, out = api_brief.post(r, home=str(tmp_path), sync=True)
    assert st == status and not out["ok"] and msg in out["error"]


def test_bad_get_ids(tmp_path):
    for q in ({}, {"id": ["../../x"]}, {"id": ["ABCDEF0123456789"]}):
        r, _ = req(tmp_path, query=q)
        assert api_brief.get(r, home=str(tmp_path))[0] == 400


def test_bad_answer_values_are_ignored_not_trusted(tmp_path):
    _, out, _ = post(tmp_path, {"text": "make me a robot arm"})
    st, out, _ = post(tmp_path, {"brief_id": out["brief_id"], "answers": {"layout": "teleport", "quantity": "lots",
                                                                       "object": {"name": "huge", "diameter_mm": 1e9}}})
    b = out["brief"]
    assert st == 200 and "layout" not in b["slots"] and "quantity" not in b["slots"]
    assert b["slots"]["object"]["value"]["diameter_mm"] == 1000.0 and b["status"] == "out_of_scope"


def test_turns_run_off_the_request_thread_and_one_at_a_time(tmp_path, monkeypatch):
    gate = threading.Event()
    real = agents.run_turn

    def slow(*a, **kw):
        gate.wait(5)
        return real(*a, **kw)

    monkeypatch.setattr(agents, "run_turn", slow)
    r, hub = req(tmp_path, {"text": "something to pick strawberries"})
    st, out = api_brief.post(r, home=str(tmp_path))
    assert st == 202 and out["busy"] is True and out["brief"]["status"] == "asking" and not out["brief"]["questions"]
    r2, _ = req(tmp_path, {"brief_id": out["brief_id"], "answers": {"layout": "short_hop"}})
    st2, out2 = api_brief.post(r2, home=str(tmp_path))
    assert st2 == 409 and "still working" in out2["error"]
    r3, _ = req(tmp_path, query={"id": [out["brief_id"]]})
    assert api_brief.get(r3, home=str(tmp_path))[1]["busy"] is True
    gate.set()
    for _ in range(100):
        if hub.events and hub.events[-1]["type"] == "brief":
            break
        time.sleep(0.05)
    assert hub.events[-1]["type"] == "brief" and hub.events[-1]["status"] == "asking" and hub.events[-1]["busy"] is False
    assert api_brief.get(r3, home=str(tmp_path))[1]["busy"] is False


def test_register_with_a_router(tmp_path):
    table = {}

    def route(method, path, *, role="editor", raw_body=False, max_body=1 << 20):
        def deco(fn):
            table[(method, path)] = (role, fn)
            return fn
        return deco

    api_brief.register(route, home_for=lambda r: str(tmp_path))
    assert table[("POST", "/api/brief")][0] == "editor" and table[("GET", "/api/brief")][0] == "viewer"
    r, hub = req(tmp_path, {"text": "help me sort socks"})
    st, out = table[("POST", "/api/brief")][1](r)
    assert st == 202
    for _ in range(100):
        if hub.events and hub.events[-1]["type"] == "brief":
            break
        time.sleep(0.05)
    assert hub.events[-1]["status"] == "asking" and hub.events[-1]["project"] == "egg-picker"
