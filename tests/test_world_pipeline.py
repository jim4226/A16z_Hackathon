"""World mode in the design and the pipeline: Design.world, the sim stage reads world.* and reruns alone when the
room changes, and passes <project>/world/world.json to the simulation. Offline; the real pipeline takes ~30 s."""

import json
import os

import pytest

from morph.cli import EGG_ROBOT
from morph.robot.pipeline2 import ORDER, RobotProject, dirty_nodes
from morph.spec import Design, diff
from morph.studio import worlds


def test_world_keys_dirty_only_the_simulation():
    assert dirty_nodes(["world.id"]) == ["sim"] and dirty_nodes(["world.sha"]) == ["sim"]
    assert dirty_nodes(["world.id", "world.sha"]) == ["sim"]


def test_design_world_section():
    d = Design()
    assert d.world.id == "" and d.world.sha == "" and d.flat()["world.id"] == ""
    old = d.to_dict()
    old.pop("world")                                             # design.json written before world mode
    assert Design.from_dict(old).to_dict() == d.to_dict()
    d2 = d.with_changes({"world.id": "desk", "world.sha": "0123456789abcdef"})
    assert set(diff(d, d2)) == {"world.id", "world.sha"}
    assert Design.from_dict(json.loads(d2.dumps())).world.sha == "0123456789abcdef"


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    pytest.importorskip("mujoco")
    root = tmp_path_factory.mktemp("proj") / "egg-picker"
    p = RobotProject(str(root))
    r1 = p.run(Design().with_changes(EGG_ROBOT), n_trials=1, verbose=False, fast_sim=True)
    return p, r1


def test_saving_a_world_reruns_only_the_simulation(built):
    p, r1 = built
    w, h = worlds.save(p.root, worlds.starter("desk"))
    d2 = p.load_design(r1).with_changes({"world.id": w["id"], "world.sha": h})
    events = []
    r2 = p.run(d2, parent=r1, verbose=False, fast_sim=True, on_event=events.append)
    res = p.load_results(r2)
    assert events[0]["dirty"] == ["sim"]
    assert {n: v for n, v in res["provenance"].items() if not v.startswith("reused")} == {"sim": "computed"}
    assert all(res["provenance"][n] == f"reused from {r1}" for n in ORDER if n != "sim")
    pp = res["sims"]["scenarios"]["pick_place"]["metrics"]
    assert pp["world"] == "desk" and pp["world_sha"] == h and pp["success"] and pp["clearance_ok"]
    with open(os.path.join(p.rev_dir(r2), "sim", "world.json"), encoding="utf-8") as fh:
        sw = json.load(fh)
    assert sw["sha"] == h and sw["design_sha"] == h and sw["obstacles"]
    with open(os.path.join(p.rev_dir(r2), "sim", "sim_pick_place.json"), encoding="utf-8") as fh:
        assert json.load(fh)["world"]["id"] == "desk"
    # move the mug next to the place spot: only the simulation reruns, and it sees the change
    w2 = json.loads(json.dumps(w))
    mug = next(o for o in w2["obstacles"] if o["id"] == "mug")
    mug["center"][:2] = worlds.to_world(w, [d2.task.place_x_mm, d2.task.place_y_mm + 70.0, 0.0])[:2]
    w2, h2 = worlds.save(p.root, w2)
    r3 = p.run(p.load_design(r2).with_changes({"world.sha": h2}), parent=r2, verbose=False, fast_sim=True)
    res3 = p.load_results(r3)
    assert [n for n, v in res3["provenance"].items() if not v.startswith("reused")] == ["sim"]
    m3 = res3["sims"]["scenarios"]["pick_place"]["metrics"]
    assert m3["world_sha"] == h2 and m3["min_clearance_mm"] < pp["min_clearance_mm"]
    # and the exact checks see this revision as simulated in this world
    with open(os.path.join(p.rev_dir(r3), "cad", "robot.json"), encoding="utf-8") as fh:
        robot = json.load(fh)
    out = worlds.checks(w2, res3, robot=robot, design=p.load_design(r3))
    assert {c["id"]: c["status"] for c in out["checks"]}["sim_world"] == "pass"


def test_a_missing_world_fails_the_sim_stage(built, tmp_path):
    p, r1 = built
    q = RobotProject(str(tmp_path / "no-world"))
    d = p.load_design(r1).with_changes({"world.id": "desk", "world.sha": "0123456789abcdef"})
    rev = q.run(d, verbose=False, fast_sim=True)
    res = q.load_results(rev)
    assert "world" in res["errors"]["sim"] and res["provenance"]["sim"] == "error"
