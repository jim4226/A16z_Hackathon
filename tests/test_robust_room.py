"""The "room" world-tolerance preset for robustness studies (morph/services/robust.py, through register_world_preset):
the mount +/-10 mm and +/-2 deg and every box 0-15 mm off, with the object and the hardware held at their nominal
values, so a study's failures are the room's. A carton wall 12 mm from the open jaws at the place spot (the research
case) is hit in some runs: the study reports that collision rate with its interval. Offline, 2 ms simulations;
MORPH_SLOW=1 adds the 200-run study at 1 ms."""

import json
import math
import os

import numpy as np
import pytest

pytest.importorskip("mujoco")

from morph.robot import arm, electronics2, simulate  # noqa: E402
from morph.robot.pipeline2 import RobotProject  # noqa: E402
from morph.services import robust  # noqa: E402
from morph.studio import api_study, projects, worlds  # noqa: E402

SLOW = os.environ.get("MORPH_SLOW") == "1"


def make_carton_revision(home, name="carton", off=66.0, fast=True):
    """The egg-picker on a bench with a carton wall `off` mm past its place spot (66: 12 mm from the open jaws)."""
    info = projects.create(str(home), name, origin="default", demo=False, unique=False)
    root = info["root"]
    d0 = projects.template(name)
    place = np.array([d0.task.place_x_mm, d0.task.place_y_mm])
    a, r = math.atan2(place[1], place[0]), float(np.linalg.norm(place)) + off
    w = {"id": "carton", "name": "Bench with a carton", "mount": {"position": [0.0, 0.0, 0.0], "yaw_deg": 0.0},
         "surface": {"label": "Bench top", "center": [100.0, 0.0, -12.5], "half": [550.0, 600.0, 12.5], "yaw_deg": 0.0},
         "floor_z": -900.0, "obstacles": [{"id": "carton", "label": "Carton wall", "center": [r * math.cos(a), r * math.sin(a), 20.0],
                                           "half": [6.0, 40.0, 20.0], "yaw_deg": math.degrees(a)}], "zones": []}
    w, sha = worlds.save(root, w)
    d = d0.with_changes({"world.id": w["id"], "world.sha": sha})
    rev = os.path.join(root, "r001")
    os.makedirs(os.path.join(rev, "sim"))
    with open(os.path.join(rev, "design.json"), "w", encoding="utf-8") as fh:
        fh.write(d.dumps())
    sw = RobotProject(root)._sim_world(d, os.path.join(rev, "sim"))
    sz = arm.size(d)
    asm = arm.build(d, sz, electronics2.plan(d, {**sz, "grip_target_n": 2.34}))
    doc = simulate.run(asm, d, "pick_place", os.path.join(rev, "sim"), fast=fast, world=sw)
    pp = {"file": "sim_pick_place.json", "title": "Pick and place", "metrics": doc["metrics"]}
    res = {"rev": "r001", "kind": "robot", "stage_status": {"sim": "computed"}, "provenance": {"sim": "computed"},
           "gripper": {"force": {"target_force_n": 2.34}}, "sims": {"mjcf": "robot.xml", "scenarios": {"pick_place": pp}}}
    with open(os.path.join(rev, "results.json"), "w", encoding="utf-8") as fh:
        json.dump(res, fh)
    return root, doc["metrics"]


def test_the_room_preset_holds_everything_else_nominal():
    d = projects.template("egg-picker")
    w = {"obstacles": [{}, {}]}
    std, room = robust.default_spec(d, w), robust.default_spec(d, w, "room")
    assert [e["name"] for e in room["params"]] == [e["name"] for e in std["params"]] == robust.NAMES
    world = {e["name"]: e for e in room["params"][-4:]}
    assert all(not e["on"] for e in room["params"][:-4]) and all(e["on"] for e in room["params"][-4:])
    assert (world["mount_dx_mm"]["lo"], world["mount_dx_mm"]["hi"], world["mount_yaw_deg"]["hi"],
            world["box_error_mm"]["lo"], world["box_error_mm"]["hi"]) == (-10.0, 10.0, 2.0, 0.0, 15.0)
    assert room["world_preset"] == "room" and robust.spec_sha(room) != robust.spec_sha(std)
    for i in range(5):                   # every entry still draws: the room's values match a standard study's
        a, da, _ = robust.sample(std["params"], 3, i)
        b, db, _ = robust.sample(room["params"], 3, i)
        assert all(a[n] == b[n] for n, _, _ in robust.WORLD) and np.array_equal(da, db)
        assert all(b[e["name"]] == e["nominal"] for e in room["params"][:-4])
    assert "room" in robust.WORLD_PRESETS and "room" in robust.ROOM_ONLY and "standard" not in robust.ROOM_ONLY
    assert robust.PRESET_LABELS["room"].startswith("Only the room varies")
    assert robust.default_spec(d, None, "room")["world_preset"] is None      # no room, no preset


def test_a_room_study_of_a_carton_reports_the_collision_rate(tmp_path):
    root, nominal = make_carton_revision(tmp_path / "home")
    assert nominal["success"] and nominal["min_clearance_mm"] == pytest.approx(12.0, abs=0.3)
    with pytest.raises(robust.StudyError, match="no world tolerance preset called hall"):
        robust.save_tolerances(root, {"world_preset": "hall"})
    spec = robust.save_tolerances(root, {"world_preset": "room"})
    assert spec["world_preset"] == "room" and [e["on"] for e in spec["params"]] == [False] * 11 + [True] * 4
    # a stress study doubles the room's ranges (mount +/-20 mm, boxes 0-30 mm): some runs hit the carton
    doc = robust.run_study(root, runs=12, seed=3, stress=True, workers=2, timestep=0.002, checks=False, nice=0)
    s = doc["summary"]
    hits = [g for g in s["groups"] if g["id"].startswith("world_collision:")]
    assert hits and s["n"] + len(s["unstable"]) == 12, s
    for g in hits:
        assert g["rate"] == round(g["count"] / s["n"], 4) and g["label"].startswith("Hit a box in the room during the ")
        assert [round(v, 4) for v in robust.wilson(g["count"], s["n"])] == g["wilson95"]
    assert all(g["id"].startswith("world_collision:") for g in s["groups"])    # nothing else varies, nothing else fails
    rows = robust.load(root, doc["id"], rows=True)["runs"]
    assert all(r["params"]["object_yaw_deg"] == 0.0 and r["params"]["pad_friction"] == 1.0 for r in rows)
    card = api_study.card(robust.load(root, doc["id"]))
    assert api_study.NOTES["room_only"] in card["flags"]
    g = card["groups"][0]
    assert g["share"].startswith(f"{hits[0]['count']} of {s['n']} runs (likely between ") and g["share"].endswith("%)")


@pytest.mark.skipif(not SLOW, reason="MORPH_SLOW=1: 200 runs at 1 ms (about 2-4 minutes on 3 workers)")
def test_acceptance_a_carton_at_12_mm(tmp_path):
    """Acceptance (research: 185 of 200, the jaw hits the wall in 15): 200 runs at 1 ms, only the room varying."""
    root, nominal = make_carton_revision(tmp_path / "home", fast=False)
    assert nominal["success"] and nominal["min_clearance_mm"] == pytest.approx(12.0, abs=0.3)
    robust.save_tolerances(root, {"world_preset": "room"})
    doc = robust.run_study(root, runs=200, seed=3, workers=3, timestep=0.001, checks=True, nice=0)
    s = doc["summary"]
    hits = [g for g in s["groups"] if g["id"].startswith("world_collision:")]
    count = sum(g["count"] for g in hits)
    assert 1 <= count <= 60 and s["n"] - s["successes"] == count, s
    lo, hi = robust.wilson(count, s["n"])
    assert lo < count / s["n"] < hi
    print(f"\ncarton 12 mm, room only, 200 runs at 1 ms: {s['successes']} of {s['n']} succeed "
          f"(Wilson {s['wilson95']}), collisions {count} ({count / s['n']:.1%}, Wilson [{lo:.3f}, {hi:.3f}]): "
          + "; ".join(f"{g['id']} {g['count']} {g['wilson95']} model_sensitive={g.get('model_sensitive')}" for g in hits)
          + f"; {doc['timing']['runs_per_s']} runs/s")
