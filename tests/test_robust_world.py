"""A robustness study in World mode (morph/services/robust.py): the revision's room comes along the way the pipeline
loads it, the mount and every box get their tolerances, a run whose arm or egg touches a box is grouped by the phase
it happened in, a run MuJoCo had to reset is not counted, and the study never changes the revision. Offline; the
built-in desk with a bin 20 mm outside the carry arc, 2 ms simulations, stress mode so boxes get hit."""

import hashlib
import json
import math
import os

import numpy as np
import pytest

pytest.importorskip("mujoco")

from morph.robot import arm, electronics2, simulate  # noqa: E402
from morph.robot.pipeline2 import RobotProject  # noqa: E402
from morph.services import robust  # noqa: E402
from morph.studio import projects, worlds  # noqa: E402


def make_world_revision(home, name="egg-in-room"):
    info = projects.create(str(home), name, origin="default", demo=False, unique=False)
    root = info["root"]
    d0 = projects.template(name)
    w = worlds.starter("desk")
    pick, place = np.array([d0.task.pick_x_mm, d0.task.pick_y_mm]), np.array([d0.task.place_x_mm, d0.task.place_y_mm])
    a0, a1 = simulate._arc_angles(np.r_[pick, 0], np.r_[place, 0])
    am, rm = (a0 + a1) / 2, 0.5 * (np.linalg.norm(pick) + np.linalg.norm(place)) + 20
    center = worlds.to_world(w, [rm * math.cos(am), rm * math.sin(am), 45.0])
    w["obstacles"].append({"id": "bin", "label": "Bin", "center": [round(v, 1) for v in center],
                           "half": [30.0, 30.0, 45.0], "yaw_deg": float(w["mount"]["yaw_deg"])})
    w, sha = worlds.save(root, w)
    d = d0.with_changes({"world.id": w["id"], "world.sha": sha})
    rev = os.path.join(root, "r001")
    os.makedirs(os.path.join(rev, "sim"))
    with open(os.path.join(rev, "design.json"), "w", encoding="utf-8") as fh:
        fh.write(d.dumps())
    sw = RobotProject(root)._sim_world(d, os.path.join(rev, "sim"))       # also sim/world.json, as the pipeline
    sz = arm.size(d)
    asm = arm.build(d, sz, electronics2.plan(d, {**sz, "grip_target_n": 2.34}))
    doc = simulate.run(asm, d, "pick_place", os.path.join(rev, "sim"), fast=True, world=sw)
    pp = {"file": "sim_pick_place.json", "title": "Pick and place", "metrics": doc["metrics"]}
    res = {"rev": "r001", "kind": "robot", "stage_status": {"sim": "computed"}, "provenance": {"sim": "computed"},
           "gripper": {"force": {"target_force_n": 2.34}}, "sims": {"mjcf": "robot.xml", "scenarios": {"pick_place": pp}}}
    with open(os.path.join(rev, "results.json"), "w", encoding="utf-8") as fh:
        json.dump(res, fh)
    return root, doc["metrics"]


def _tree_sha(folder):
    h = hashlib.sha256()
    for dp, dirs, fns in os.walk(folder):
        dirs.sort()
        for fn in sorted(fns):
            p = os.path.join(dp, fn)
            h.update(os.path.relpath(p, folder).encode() + open(p, "rb").read())
    return h.hexdigest()


def test_a_study_in_a_room(tmp_path):
    root, nominal = make_world_revision(tmp_path / "home")
    assert nominal["success"] and nominal["clearance_ok"] and nominal["arc"]["usual"] is False   # it goes round the bin
    spec = robust.tolerances(root)
    world = {e["name"]: e for e in spec["params"][-4:]}
    assert spec["world_preset"] == "standard" and list(world) == ["mount_dx_mm", "mount_dy_mm", "mount_yaw_deg",
                                                                   "box_error_mm"]
    assert (world["mount_dx_mm"]["lo"], world["box_error_mm"]["hi"]) == (-10.0, 15.0)
    before = _tree_sha(os.path.join(root, "r001")), _tree_sha(os.path.join(root, "world"))
    doc = robust.run_study(root, runs=8, seed=5, stress=True, workers=2, timestep=0.002, checks=False, nice=0)
    assert (_tree_sha(os.path.join(root, "r001")), _tree_sha(os.path.join(root, "world"))) == before
    assert doc["model"]["world"] == "desk" and doc["model"]["boxes"] == len(worlds.starter("desk")["obstacles"]) + 1
    assert doc["model"]["matches_revision"] is True
    rows = robust.load(root, doc["id"], rows=True)["runs"]
    assert len(rows) == 8 and all("mount_dx_mm" in r["params"] and "min_clearance_mm" in r for r in rows if r["outcome"] != "error")
    hit = [r for r in rows if r["outcome"] == "world_collision"]
    assert hit, [r["outcome"] for r in rows]
    for r in hit:
        c = r["world_contact"]
        assert r["group"] == f"world_collision:{c['phase']}" and c["phase"] in robust.PHASES and c["obstacle"]
    for r in rows:              # a box the arm overlaps by more than a millimetre at a frame was hit, or the run is void
        if r.get("min_clearance_mm") is not None and r["min_clearance_mm"] < -1.0:
            assert r["outcome"] in ("world_collision", "unstable"), r
    s = doc["summary"]
    assert sum(g["count"] for g in s["groups"]) == s["failures"] and s["n"] + len(s["unstable"]) == 8
    labels = [g["label"] for g in s["groups"] if g["id"].startswith("world_collision")]
    assert labels and all(lb.startswith("Hit a box in the room during the ") for lb in labels)
    text = robust.report(doc)
    assert "Robot mount off, x: -20 mm to 20 mm" in text and "Each box off its measured place: 0 mm to 30 mm" in text
