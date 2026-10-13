"""What the merged features rely on in the design and its records: world mode's room is an id (never a path) that
chat and models can't set, a fork takes the room along, a change request's author is recorded on its revision,
results.json appears whole, the feature modules' Claude calls follow the network switch, and their telemetry ids are
registered with Keewano in their ranges."""

import json
import os

import pytest

from morph import keewano
from morph.robot.pipeline2 import RobotProject, dirty_nodes
from morph.spec import Design
from morph.studio import agents, api_video, capability, projects, worlds
from morph.studio.intent import CHAT_FORBIDDEN
from morph.studio.security import design_values


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr(RobotProject, "_compute", lambda self, *a, **k: False)  # every stage "pending": instant


def test_world_mode_names_a_room_by_id_never_by_path(tmp_path):
    assert dirty_nodes(["world.id", "world.sha"]) == ["sim"]                 # a world edit reruns the simulation only
    assert {"world.id", "world.sha"} <= CHAT_FORBIDDEN
    from morph.studio.intent import _from_tool
    out = _from_tool({"changes": {"world.id": "../x", "world.sha": "ff", "arm.dof": 6}, "reply": "ok"})
    assert out["changes"] == {"arm.dof": 6} and "world" in out["reply"]   # Claude's tool call can't point at a room
    out, notes = capability.clamp({"world.id": "desk", "world.sha": "ab12", "arm.dof": 5})
    assert out == {"arm.dof": 5} and any("world.id" in n for n in notes)  # a model never picks the room
    design_values({"world.id": "desk", "world.sha": "0123456789abcdef"}, projects.DEFAULTS)
    for bad in ("../../etc", "/etc/passwd", "Desk", "desk/room", "a" * 41, "C:\\x"):
        with pytest.raises(ValueError, match="never a path"):
            design_values({"world.id": bad}, projects.DEFAULTS)
    with pytest.raises(ValueError):
        design_values({"world.sha": "../x"}, projects.DEFAULTS)
    p = RobotProject(str(tmp_path / "egg"))
    for bad in ("../world", "world/../../x", "..", "x" * 50):
        with pytest.raises(RuntimeError, match="never a path"):
            p._sim_world(Design().with_changes({"world.id": bad}), str(tmp_path))
    with pytest.raises(RuntimeError, match="isn't this project's world"):
        p._sim_world(Design().with_changes({"world.id": "desk"}), str(tmp_path))   # named but missing
    assert p._sim_world(Design(), str(tmp_path)) is None


def test_a_fork_takes_its_world_along(tmp_path, quick):
    home = str(tmp_path / "projects")
    info = projects.create(home, "egg", design=Design())
    p = RobotProject(info["root"])
    w, sha = worlds.save(p.root, worlds.starter("desk"))
    rev = p.run(Design().with_changes({"name": "egg", "world.id": w["id"], "world.sha": sha}), label="desk", verbose=False)
    fork = projects.fork(home, "egg", rev, "egg-copy")
    assert os.path.isfile(os.path.join(fork["root"], "world", "world.json"))
    assert worlds.load(fork["root"])["id"] == "desk"
    plain = projects.fork(home, "egg", rev, "egg-plain")
    assert plain["design"].world.id == "desk"                              # the copy keeps naming the same room


def test_a_change_request_revision_records_its_author_and_appears_whole(tmp_path, quick):
    p = RobotProject(str(tmp_path / "egg"))
    rev = p.run(Design(), label="first", verbose=False, by="m-ana", proposed_by="m-vic")
    res = p.load_results(rev)
    assert res["by"] == "m-ana" and res["proposed_by"] == "m-vic"
    with open(os.path.join(p.root, "ledger.jsonl"), encoding="utf-8") as fh:
        entry = [json.loads(x) for x in fh][-1]
    assert entry["by"] == "m-ana" and entry["proposed_by"] == "m-vic"
    assert sorted(os.listdir(p.rev_dir(rev))) == sorted(f for f in os.listdir(p.rev_dir(rev)) if not f.startswith("."))
    plain = p.run(Design(), label="again", verbose=False)
    assert "proposed_by" not in p.load_results(plain)


def test_feature_claude_calls_follow_the_network_switch(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-a-real-key")
    assert agents._claude() is not None and worlds._claude_ready()
    monkeypatch.setenv("MORPH_OFFLINE", "1")
    assert agents._claude() is None and not worlds._claude_ready()


def test_feature_telemetry_ids_are_registered_in_their_ranges():
    from morph.studio.teams import activity
    ids = {e["name"]: (e["eventTypeId"], e["dataType"]) for e in keewano.CUSTOM_EVENTS}
    for evs, lo, hi in ((agents.KEEWANO_EVENTS, 2100, 2129), (api_video.KEEWANO_EVENTS, 2160, 2179),
                        (activity.KEEWANO_EVENTS, 2180, 2229)):
        for e in evs:
            assert ids[e["name"]] == (e["eventTypeId"], e["dataType"]) and lo <= e["eventTypeId"] <= hi
    names = set(ids)
    for entry in ({"type": "video_analysis", "kind": "video", "frames": 8, "ms": 900, "source": "claude", "usable": True,
                   "questions": 1, "cost_usd": 0.06, "second_opinion": "none"},
                  {"type": "video_apply", "changes": 3}, {"type": "video_decode", "decoder": "ffmpeg", "ms": 40, "frames": 8}):
        evs = keewano.events_for(entry)
        assert evs and {n for n, _ in evs} <= names
    assert {n for n, _ in agents.memory_events(_brief(), "built", {"feasible": True})} <= names


def _brief():
    from morph.studio import brief as B
    return B.start("help me sort socks")
