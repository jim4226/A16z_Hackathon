"""Whole-robot MuJoCo simulation (morph/robot/simulate.py) on the generated arm with the raw-egg design."""

import json
import math
import os
import time

import numpy as np
import pytest

mujoco = pytest.importorskip("mujoco")

from morph.robot import arm, simulate  # noqa: E402
from morph.spec import Design  # noqa: E402

EGG = {"name": "raw-egg", "object.name": "raw egg", "object.shape": "cylinder", "object.diameter_mm": 44.0,
       "object.length_mm": 57.0, "object.mass_g": 60.0, "object.friction": 0.35, "finger.sensor": "fsr402",
       "finger.width_mm": 20.0}
TOP_KEYS = {"scenario", "title", "dt", "joints", "frames", "markers", "object", "targets", "metrics"}
FRAME_KEYS = {"t", "q", "obj", "tau", "grip_n", "phase"}
PICK_PHASES = ["approach", "grasp", "lift", "move", "place", "release", "retreat"]


def egg_design(**changes) -> Design:
    return Design().with_changes({**EGG, **changes})


def build(design):
    return arm.build(design, arm.size(design))


@pytest.fixture(scope="module")
def egg_run(tmp_path_factory):
    d = egg_design()
    asm = build(d)
    out = tmp_path_factory.mktemp("sim")
    t0 = time.time()
    res = simulate.run_all(asm, d, str(out))
    wall = time.time() - t0
    docs = {}
    for name in simulate.SCENARIOS:
        with open(out / f"sim_{name}.json") as fh:
            docs[name] = json.load(fh)
    return {"design": d, "asm": asm, "out": out, "res": res, "docs": docs, "wall": wall}


def check_schema(doc, asm):
    assert TOP_KEYS <= set(doc)
    arm_joints = list(asm.meta["arm_joints"])
    assert doc["joints"] == arm_joints + [asm.meta["gripper_joint"]]
    assert abs(doc["dt"] - 0.0333) < 1e-3
    ts = np.array([f["t"] for f in doc["frames"]])
    assert len(ts) > 30
    assert np.all(np.diff(ts) > 0), "frame times must increase"
    assert np.all(np.abs(np.diff(ts) - 1 / 30) < 0.004), "frames at 30 Hz"
    for f in doc["frames"]:
        assert FRAME_KEYS <= set(f)
        assert len(f["q"]) == len(doc["joints"])
        assert len(f["tau"]) == len(arm_joints)
        assert len(f["obj"]) == 7 and abs(np.linalg.norm(f["obj"][3:]) - 1.0) < 1e-3
        assert isinstance(f["phase"], str) and f["grip_n"] >= 0
    assert set(doc["targets"]) == {"pick", "place"} and all(len(v) == 3 for v in doc["targets"].values())
    assert {"shape", "diameter_mm", "length_mm", "color"} <= set(doc["object"])
    for mk in doc["markers"]:
        assert {"t", "label"} <= set(mk)
    m = doc["metrics"]
    assert isinstance(m["success"], bool)
    assert set(m["max_torque_pct"]) == set(arm_joints)
    assert all(0 <= v <= 100.0 + 1e-6 for v in m["max_torque_pct"].values())


# ------------------------------------------------------------------ model

def test_mjcf_matches_assembly_kinematics():
    d = egg_design()
    asm = build(d)
    xml = simulate.build_mjcf(asm, d)
    m = mujoco.MjModel.from_xml_string(xml)
    data = mujoco.MjData(m)
    rng = np.random.default_rng(1)
    names = asm.meta["arm_joints"]
    for _ in range(5):
        q = {n: float(rng.uniform(asm.joint(n).lower, asm.joint(n).upper)) * 0.8 for n in names}
        q["gripper"] = float(rng.uniform(0.0, 10.0))
        for n, v in q.items():
            data.qpos[m.jnt_qposadr[m.joint(n).id]] = v * (1e-3 if n == "gripper" else 1.0)
        data.qpos[m.jnt_qposadr[m.joint("gripper_right").id]] = q["gripper"] * 1e-3
        mujoco.mj_kinematics(m, data)
        T = asm.world_transforms(q)
        tcp = data.site_xpos[m.site("sim_tcp").id] * 1000.0
        assert np.allclose(tcp, T[asm.meta["tcp_link"]][:3, 3], atol=1e-3)
        for link in ("jaw_left", "jaw_right", "forearm"):
            assert np.allclose(data.xpos[m.body(link).id] * 1000.0, T[link][:3, 3], atol=1e-3)


def test_collision_model_pads_and_filtering():
    d = egg_design()
    asm = build(d)
    rob = simulate._Robot(asm, d, simulate.SimOptions(object_xy=(-400.0, 400.0)))
    info = rob.info
    pad_pieces = [g for g in info.geoms if g.kind == "pad" and g.link == "jaw_left"]
    assert len(pad_pieces) >= 6, "contour pad must be cut into convex strips"
    assert info.grip_kind == "prismatic" and -1.0 < info.grip_contact <= 0.0   # contour pad: 0.5 mm running clearance
    assert np.allclose(info.grasp_offset, 0.0)
    # at the ready pose nothing on the robot touches anything (no spurious self-collision)
    m, data = rob.model, rob.data
    data.qpos[rob.qadr] = rob.q_ready()
    rob.set_grip(data, info.grip_open)
    mujoco.mj_forward(m, data)
    robot_geoms = {m.geom(g.name).id for g in info.geoms}
    for i in range(data.ncon):
        c = data.contact[i]
        assert not (c.geom1 in robot_geoms and c.geom2 in robot_geoms), "arm collides with itself"


@pytest.mark.parametrize("dof", [4, 5, 6])
def test_ik_top_down_for_each_dof(dof):
    d = egg_design(**{"arm.dof": dof})
    rob = simulate._Robot(build(d), d, simulate.SimOptions())
    pick = np.array([d.task.pick_x_mm, d.task.pick_y_mm, 22.0])
    axis = simulate._tangent(pick[:2])
    q, ep, er = rob.ik_robust(pick, axis)
    assert ep < 0.5 and er < 5e-3
    p, R = rob.fk(q)
    assert np.linalg.norm(p - pick) < 0.5
    assert R[:, 2] @ np.array([0, 0, -1.0]) > 0.999          # top-down approach
    assert abs(R @ rob.info.obj_axis_tcp @ axis) > 0.99         # fingers across the egg's long axis


# ------------------------------------------------------------------ scenarios (full run on the egg)

def test_run_all_outputs(egg_run):
    res, out = egg_run["res"], egg_run["out"]
    assert res["mjcf"] == "robot.xml" and os.path.exists(out / "robot.xml")
    mujoco.MjModel.from_xml_path(str(out / "robot.xml"))
    assert set(res["scenarios"]) == set(simulate.SCENARIOS)
    for name, s in res["scenarios"].items():
        assert s["file"] == f"sim_{name}.json" and os.path.exists(out / s["file"])
        assert "success" in s["metrics"]
        check_schema(egg_run["docs"][name], egg_run["asm"])
    assert egg_run["wall"] < 60.0, f"run_all took {egg_run['wall']:.1f} s"


def test_pick_place_egg(egg_run):
    doc = egg_run["docs"]["pick_place"]
    m = doc["metrics"]
    assert m["success"], m
    assert m["place_error_mm"] < 15.0 and not m["dropped"]
    assert m["max_slip_mm"] < 3.0
    assert abs(m["grip_n_mean"] - m["grip_target_n"]) < 0.3 * m["grip_target_n"]
    assert m["cycle_time_s"] <= egg_run["design"].task.cycle_time_s + 0.5
    assert m["max_torque_pct"]["j2_shoulder"] > 5.0                # gravity is really carried by the shoulder
    phases = []
    for f in doc["frames"]:
        if not phases or phases[-1] != f["phase"]:
            phases.append(f["phase"])
    assert phases == PICK_PHASES
    # gripper value: open (mm beyond contact) before the grasp, at/just past contact while carrying
    carry = [f["q"][-1] for f in doc["frames"] if f["phase"] in ("lift", "move")]
    assert doc["frames"][0]["q"][-1] > 5.0 and max(abs(v) for v in carry) < 2.0
    # the object ends on the place target, resting on the table
    obj = np.array(doc["frames"][-1]["obj"][:3])
    assert np.linalg.norm(obj[:2] - np.array(doc["targets"]["place"][:2])) < 15.0
    assert abs(obj[2] - 22.0) < 3.0


def test_object_pose_convention(egg_run):
    """obj quaternion: long axis on local z, lying flat and tangential to the base at the pick."""
    f0 = egg_run["docs"]["pick_place"]["frames"][0]
    w, x, y, z = f0["obj"][3:]
    zaxis = np.array([2 * (x * z + w * y), 2 * (y * z - w * x), 1 - 2 * (x * x + y * y)])
    radial = np.array(f0["obj"][:2]) / np.linalg.norm(f0["obj"][:2])
    assert abs(zaxis[2]) < 0.05 and abs(zaxis[:2] @ radial) < 0.05


def test_payload_sweep(egg_run):
    doc = egg_run["docs"]["payload_sweep"]
    m = doc["metrics"]
    arm_joints = egg_run["asm"].meta["arm_joints"]
    assert m["success"] and m["saturated"]
    assert m["max_payload_g"] > 2 * egg_run["design"].object.mass_g
    assert m["limited_by"] in arm_joints
    assert abs(m["max_payload_g"] - m["static_estimate_g"]) < 0.15 * m["static_estimate_g"]
    ramp = [f["payload_g"] for f in doc["frames"] if f["phase"] == "hold"]
    assert ramp[-1] > ramp[0] and all(b >= a - 1e-6 for a, b in zip(ramp, ramp[1:]))
    assert any(f["phase"] == "recover" for f in doc["frames"])
    pct = [c["pct"][m["limited_by"]] for c in m["curve"] if c.get("ok")]
    assert pct[-1] > pct[0]


def test_workspace(egg_run):
    doc = egg_run["docs"]["workspace"]
    m = doc["metrics"]
    d = egg_run["design"]
    assert m["success"] and m["pick_reachable"] and m["place_reachable"]
    assert len(doc["points"]) >= 500 and all(len(p) == 3 for p in doc["points"])
    assert 0.85 * m["design_reach_mm"] < m["reach_mm"] < 1.15 * m["design_reach_mm"]
    lo, hi = m["top_down_reach_mm"]
    assert lo < math.hypot(d.task.pick_x_mm, d.task.pick_y_mm) < hi
    assert m["volume_l"] > 1.0


def test_shake(egg_run):
    m = egg_run["docs"]["shake"]["metrics"]
    assert m["success"] and not m["dropped"]
    assert m["slip_mm"] < 5.0
    assert m["peak_accel_ms2"] > 5.0
    assert m["grip_n_min"] > 0.5 * m["grip_target_n"]
    assert any(f["phase"] == "shake" for f in egg_run["docs"]["shake"]["frames"])


# ------------------------------------------------------------------ other configurations

@pytest.mark.parametrize("dof", [4, 6])
def test_fast_pick_place_other_dofs(dof, tmp_path):
    d = egg_design(**{"arm.dof": dof})
    asm = build(d)
    t0 = time.time()
    doc = simulate.run(asm, d, "pick_place", str(tmp_path), fast=True)
    assert time.time() - t0 < 20.0
    assert doc["metrics"]["success"], doc["metrics"]
    check_schema(doc, asm)
    assert os.path.exists(tmp_path / "sim_pick_place.json") and os.path.exists(tmp_path / "robot.xml")


def test_imported_robot_with_hinged_jaw(tmp_path):
    """The same simulator drives an imported open-source arm (SO-101, revolute jaw against a fixed jaw)."""
    imported = pytest.importorskip("morph.robot.imported")
    d = egg_design(**{"arm.source": "so101"})
    try:
        asm = imported.build(d, imported.size(d))
    except (FileNotFoundError, KeyError) as exc:
        pytest.skip(f"SO-101 profile not available: {exc}")
    doc = simulate.run(asm, d, "pick_place", str(tmp_path), fast=True)
    assert doc["metrics"]["success"], doc["metrics"]
    check_schema(doc, asm)
