"""Chat interpretation: a Claude call has a deadline, and one room's grip target never reaches another room's chat."""

import json
import sys
import threading
import types

from morph import settings
from morph.robot.pipeline2 import RobotProject
from morph.spec import Design
from morph.studio import intent, server


def test_claude_calls_have_a_deadline(monkeypatch):
    seen = {}

    class Client:
        def __init__(self, **kw):
            seen.update(kw)
            self.messages = types.SimpleNamespace(create=self.create)

        def create(self, **kw):
            raise TimeoutError("the model took too long")

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=Client))
    monkeypatch.setattr(settings, "network_allowed", lambda: True)
    out = intent.interpret("Make it 6-DOF", Design())
    assert seen == {"timeout": 30, "max_retries": 1}
    assert out["source"] == "rules" and out["changes"] == {"arm.dof": 6}     # a slow model falls back to the rules


def test_the_grip_target_goes_in_and_out_with_each_message():
    d = Design()
    a = intent.interpret("Make the grip gentler", d, 5.0)
    assert a["changes"] == {"electronics.target_force_n": 3.5} and a["last_target"] == 3.5
    b = intent.interpret("Make the grip gentler", d)                          # nothing left over from the last call
    assert b["changes"] == {"electronics.target_force_n": 1.4} and b["last_target"] == 1.4
    assert intent.interpret("Make it 6-DOF", d, 4.2)["last_target"] == 4.2    # an unchanged target passes through


def test_rooms_interpreting_at_once_keep_their_own_targets(monkeypatch):
    monkeypatch.setattr(settings, "network_allowed", lambda: False)          # the rules, quickly
    d, wrong, gate = Design(), [], threading.Barrier(4)

    def room(target):
        gate.wait()
        for _ in range(100):
            got = intent.interpret("Make the grip firmer", d, target)["changes"]["electronics.target_force_n"]
            if got != round(target * 1.4, 2):
                wrong.append((target, got))

    threads = [threading.Thread(target=room, args=(t,)) for t in (1.0, 2.5, 4.0, 6.0)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert wrong == []


def _project(home, name, grip_target=None):
    root = home / name
    (root / "r001").mkdir(parents=True)
    (root / "meta.json").write_text(json.dumps({"kind": "robot", "source": "", "need": name}), encoding="utf-8")
    (root / "r001" / "design.json").write_text(Design().dumps(), encoding="utf-8")
    summary = {"grip_target_n": grip_target} if grip_target else {}
    (root / "r001" / "results.json").write_text(json.dumps({"rev": "r001", "parent": None, "summary": summary}),
                                                encoding="utf-8")
    return RobotProject(str(root))


def test_a_rooms_grip_target_stays_in_that_room(tmp_path, monkeypatch):
    home = tmp_path / "projects"
    st = server.Studio(_project(home, "egg", grip_target=5.0), str(home))
    _project(home, "arm")                                                     # its revision reports no grip target
    applied = []
    monkeypatch.setattr(server.Studio, "apply", lambda self, changes, *a, room=None, **k: applied.append((room.name, changes)))
    st.chat("Make the grip gentler", st.room("egg"))
    st.chat("Make the grip gentler", st.room("arm"))
    st.chat("Make the grip gentler", st.room("arm"))                         # the room remembers its own last one
    assert applied == [("egg", {"electronics.target_force_n": 3.5}), ("arm", {"electronics.target_force_n": 1.4}),
                       ("arm", {"electronics.target_force_n": 0.98})]
    assert st.room("egg").last_target == 3.5 and st.room("arm").last_target == 0.98
