"""A chat message typed in an open project that asks for another robot is offered the New robot dialog instead of
becoming an edit of the open one (and "import this arm" the Import dialog); "Change it instead" sends it again as an edit.
Code decides from the wording first; SubQ, when set, settles only what the wording leaves open. Offline: no keys, and
builds are stubbed."""

import json
import os
import queue

import pytest

from morph.robot.pipeline2 import RobotProject
from morph.studio import agents, server
from morph.studio.projects import template
from test_brief_agents import FakeSubQ, choice, noul
from test_studio_features import caller, start, until
from test_studio_security import make_project

EGG = template("egg-picker", "")
EDITS = ["Swap to Dynamixel servos", "Use a custom PCB", "Make it 6-DOF", "Add a wrist camera", "Pilot build: CNC + sheet metal",
         "Make it work for a strawberry", "Increase reach to 550 mm", "make it strong enough to pick 300 g",
         "make the so101 gripper firmer", "Make the grip gentler", "lighter fingers please", "pick up eggs", "strawberries",
         "set a new arm length of 300", "reach 380 mm"]
NEW = ["help me sort socks", "I need a robot that sorts screws", "a robot that picks eggs", "move test tubes between racks",
       "build me an arm that hands me screwdrivers", "something to pick strawberries", "another robot for my lab",
       "make me a robot that folds laundry", "I want to pack cookies into boxes"]


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setenv("MORPH_OFFLINE", "1")
    monkeypatch.setattr(agents, "_subq", lambda: None)


@pytest.mark.parametrize("text", EDITS)
def test_a_change_to_the_open_robot_stays_an_edit(text):
    assert agents.chat_offer(text, EGG) is None, text


@pytest.mark.parametrize("text", NEW)
def test_a_request_for_another_robot_is_offered_the_new_robot_dialog(text):
    offer = agents.chat_offer(text, EGG)
    assert offer and offer["kind"] == "new_robot" and offer["route"]["source"] == "rules", text


@pytest.mark.parametrize("text", ["hello", "thanks", "can it lift 2 kg?", "what does it cost?", "make it sparkle", "",
                                  "cheaper please", "shoot my brother with a robot"])
def test_questions_small_talk_and_harm_get_no_offer(text):
    assert agents.chat_offer(text, EGG) is None


def test_importing_an_arm_is_offered_the_import_dialog():
    assert agents.chat_offer("import the Koch arm", EGG)["kind"] == "import"
    assert agents.chat_offer("load my URDF from github.com/me/arm", EGG)["kind"] == "import"


def test_subq_settles_only_what_the_wording_leaves_open(monkeypatch):
    sq = FakeSubQ({"route": choice("edit_current", 0.8), "harm": noul(0.0)})
    monkeypatch.setattr(agents, "_subq", lambda: sq)
    info = {}
    assert agents.chat_offer("help me sort socks", EGG, {"name": "egg-picker"}, info=info) is None  # SubQ: an edit
    assert sq.calls[-1]["purpose"] == "chat_route" and sq.calls[-1]["timeout"] == agents.CHAT_ROUTE_TIMEOUT_S
    assert sq.calls[-1]["state"]["open_project"] == {"name": "egg-picker"}
    assert info == {"source": "subq", "ms": 420, "input_tokens": 900}
    n = len(sq.calls)
    for text in ("Swap to Dynamixel servos", "a robot that picks eggs", "can it lift 2 kg?"):
        agents.chat_offer(text, EGG)
    assert len(sq.calls) == n                      # clear wording never waits for SubQ
    sq.answers = {"route": choice("new_robot", 0.9), "harm": noul(0.0)}
    assert agents.chat_offer("socks", EGG)["route"] == {"value": "new_robot", "confidence": 0.9, "source": "subq"}
    sq.answers = {"route": choice("new_robot", 0.9), "harm": noul(0.9)}
    assert agents.chat_offer("socks", EGG) is None
    sq.fail = True                                 # SubQ didn't answer: the wording decides
    assert agents.chat_offer("socks", EGG)["route"]["source"] == "rules"


@pytest.fixture
def studio(tmp_path, monkeypatch):
    monkeypatch.setattr(RobotProject, "_compute", lambda self, *a, **k: False)
    home = tmp_path / "projects"
    st = server.Studio(make_project(home, "egg-picker"), str(home))
    httpd = start(st)
    yield st, caller(httpd)
    httpd.shutdown()


def _events(q, until_type="queue", status=None, timeout=20.0):
    out = []
    import time
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            ev = q.get(timeout=0.2)
        except queue.Empty:
            continue
        out.append(ev)
        if ev.get("type") == until_type and any(j.get("position") is None and (status is None or j["status"] == status)
                                                 for j in ev.get("jobs") or []):
            return out
    raise AssertionError(f"timed out: {[e.get('type') for e in out]}")


def test_the_studio_offers_a_new_robot_and_edits_only_when_asked(studio, monkeypatch):
    st, call = studio
    room = st.room()
    q = room.hub.subscribe()
    seen, real = [], server.interpret
    monkeypatch.setattr(server, "interpret", lambda msg, design, grip: seen.append(msg) or real(msg, design, grip))
    s, out, _ = call("POST", "/api/chat", {"message": "help me sort socks"})
    assert s == 202
    evs = _events(q, status="noop")
    said = [e for e in evs if e["type"] == "assistant"][-1]
    assert said["noop"] is True and said["offer"] == {"kind": "new_robot", "text": "help me sort socks", "source": "rules"}
    assert "new robot" in said["text"] and "egg-picker stays as it is" in said["text"] and "SubQ" not in said["text"]
    assert seen == [] and room.p.revs() == ["r001"]            # not interpreted, nothing built
    assert [m for m in room.hub.history if m.get("offer")]      # the chat history keeps the message (the page shows
                                                               # its buttons only while it is live)
    s, out, _ = call("POST", "/api/chat", {"message": "help me sort socks", "edit": True})
    assert s == 202
    _events(q, status="noop")
    assert seen == ["help me sort socks"]                       # "Change egg-picker instead": an ordinary edit
    assert room.p.revs() == ["r001"]                            # (which the rules can't map to a parameter)
    for text in ("Make it 6-DOF", "import the Koch arm"):
        call("POST", "/api/chat", {"message": text})
        evs = _events(q, status="done" if "6-DOF" in text else "noop")
    assert any(e.get("offer", {}).get("kind") == "import" for e in evs if e["type"] == "assistant")
    until(lambda: room.latest() == "r002")
    assert room.p.load_design("r002").arm.dof == 6


def test_a_subq_routing_decision_is_counted_in_the_ledger(studio, monkeypatch):
    st, call = studio
    room = st.room()
    q = room.hub.subscribe()
    monkeypatch.setattr(agents, "_subq", lambda: FakeSubQ({"route": choice("new_robot", 0.9), "harm": noul(0.0)},
                                                            source="cache"))
    call("POST", "/api/chat", {"message": "socks"})
    said = [e for e in _events(q, status="noop") if e["type"] == "assistant"][-1]
    assert said["offer"]["source"] == "subq" and said["text"].endswith("(SubQ Snap (AI) read it that way.)")
    with open(os.path.join(room.p.root, "ledger.jsonl"), encoding="utf-8") as fh:
        entries = [json.loads(line) for line in fh]
    sub = [e for e in entries if e.get("type") == "subq"]
    assert sub and sub[-1]["purpose"] == "chat_route" and sub[-1]["source"] == "cache" and "socks" not in json.dumps(sub)
