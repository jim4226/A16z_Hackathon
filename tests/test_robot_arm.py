"""Tests for the parametric arm + gripper generator (morph/robot/arm.py)."""

import functools
import json
import math
import time

import numpy as np
import pytest

from morph.cli import EGG_ROBOT
from morph.robot import arm, catalog
from morph.robot.assembly import KINDS, PROCESSES
from morph.spec import Design

FAMILIES = ("smart_bus", "dynamixel", "hobby_pwm")
CONFIGS = ("prototype", "pilot", "production")


def _design(**ch):
    return Design().with_changes({**EGG_ROBOT, **{k.replace("__", "."): v for k, v in ch.items()}})


@functools.lru_cache(maxsize=None)
def _built(fam="smart_bus", dof=5, cfg="prototype", extras="", ecfg="modules"):
    d = _design(arm__actuator_family=fam, arm__dof=dof, build__config=cfg, electronics__extras=extras,
                electronics__config=ecfg)
    t = time.time()
    s = arm.size(d)
    a = arm.build(d, s)
    return d, s, a, time.time() - t


def _tf(a, q):
    return a.world_transforms(q)


# ------------------------------------------------------------------ sizing
@pytest.mark.parametrize("fam", FAMILIES)
@pytest.mark.parametrize("dof", (4, 5, 6))
def test_size_contract(fam, dof):
    d = _design(arm__actuator_family=fam, arm__dof=dof)
    s = arm.size(d)
    for k in ("links_mm", "reach_mm", "payload_g", "gripper_actuator", "joints", "issues"):
        assert k in s
    assert set(s["links_mm"]) >= {"base_h", "L1", "L2", "L3"}
    names = [j["name"] for j in s["joints"]]
    assert names == arm.arm_joint_names(dof)
    fam_keys = catalog.FAMILY_ACTUATORS[fam]
    for j in s["joints"]:
        assert {"name", "actuator", "torque_req_nm", "torque_with_margin_nm", "torque_avail_nm", "margin"} <= set(j)
        assert j["actuator"] in fam_keys
        assert j["torque_req_nm"] > 0 and j["torque_avail_nm"] > 0
        assert j["count"] in (1, 2)
        # issues are reported exactly when the margin is not met
        short = j["torque_avail_nm"] < j["torque_with_margin_nm"]
        assert short == any(i.startswith(j["name"]) for i in s["issues"])
    assert s["gripper_actuator"] in catalog.ACTUATORS
    L = s["links_mm"]
    assert abs(L["L1"] + L["L2"] + L["L3"] - s["reach_mm"]) < 0.5
    assert s["reach_mm"] >= d.arm.reach_mm - 0.5


def test_size_physics_monotonic():
    """Heavier payload or longer reach never lowers the shoulder torque requirement."""
    base = arm.size(_design())
    heavy = arm.size(_design(object__mass_g=300.0))
    long_ = arm.size(_design(arm__reach_mm=520.0))
    sh = lambda s: next(j for j in s["joints"] if j["name"] == "j2_shoulder")["torque_req_nm"]  # noqa: E731
    assert sh(heavy) > sh(base)
    assert sh(long_) > sh(base)
    assert long_["reach_mm"] >= 519.5


def test_size_overrides_and_dual_shoulder():
    s = arm.size(_design(arm__actuator_family="dynamixel", arm__actuator_overrides="j2=xl430_w250,j4=xm430_w350"))
    j = {x["name"]: x for x in s["joints"]}
    assert j["j2_shoulder"]["actuator"] == "xl430_w250"
    assert j["j4_wrist_pitch"]["actuator"] == "xm430_w350"
    # the default egg robot needs two STS3215 at the shoulder (one does not meet the 2x margin)
    d = _design()
    s = arm.size(d)
    sh = next(x for x in s["joints"] if x["name"] == "j2_shoulder")
    assert sh["count"] == 2 and sh["torque_avail_nm"] >= sh["torque_with_margin_nm"]
    assert s["issues"] == []


def test_moving_accessories_count_in_the_hand_mass():
    """A wrist camera and an IMU ride on the gripper: sizing carries what the build mounts there."""
    _, s0, a0, _ = _built()
    _, s1, a1, _ = _built(extras="wrist_camera,estop,imu")
    hand = lambda a: 1000 * sum(a.link_mass(n)[0] for n in ("gripper_base", "jaw_left", "jaw_right", "pinion", "tcp"))  # noqa: E731
    grew = s1["hand_mass_g"] - s0["hand_mass_g"]
    assert grew > 15 and abs(grew - (hand(a1) - hand(a0))) < 2.0, (grew, hand(a1) - hand(a0))
    sh = lambda s: next(j for j in s["joints"] if j["name"] == "j2_shoulder")["torque_req_nm"]  # noqa: E731
    assert sh(s1) > sh(s0)
    assert arm.size(_design(electronics__extras="estop"))["hand_mass_g"] == s0["hand_mass_g"]   # the e-stop is in the base


# ------------------------------------------------------------------ build: structure + contract
@pytest.mark.parametrize("cfg", CONFIGS)
@pytest.mark.parametrize("fam", FAMILIES)
@pytest.mark.parametrize("dof", (4, 5, 6))
def test_build_matrix_valid(fam, dof, cfg):
    d, s, a, dt = _built(fam, dof, cfg)
    assert a.validate() == []
    assert dt < 10.0
    links = set(a.links)
    expect = {"base", "turret", "upper_arm", "forearm", "wrist", "gripper_base", "jaw_left", "jaw_right", "pinion", "tcp"}
    assert expect <= links
    assert ("wrist_yaw" in links) == (dof == 6)
    J = {j.name: j for j in a.joints}
    for n in arm.arm_joint_names(dof):
        assert J[n].type == "revolute" and J[n].actuator
    assert J["j1_base_yaw"].axis == (0, 0, 1)
    for n in ("j2_shoulder", "j3_elbow", "j4_wrist_pitch"):
        assert tuple(J[n].axis) == (0, 1, 0)
    if dof == 6:
        assert tuple(J["j5_wrist_yaw"].axis) == (1, 0, 0) and J["j6_wrist_roll"].parent == "wrist_yaw"
    if dof == 5:
        assert J["j5_wrist_roll"].parent == "wrist" and tuple(J["j5_wrist_roll"].axis) == (0, 0, 1)
    if dof == 4:
        assert J["wrist_fixed"].type == "fixed"
    g = J["gripper"]
    assert g.type == "prismatic" and g.parent == "gripper_base" and g.child == "jaw_left" and tuple(g.axis) == (-1, 0, 0)
    assert g.lower < 0 < g.upper
    gr = J["gripper_right"]
    assert gr.mimic == {"joint": "gripper", "multiplier": 1.0} and tuple(gr.axis) == (1, 0, 0)
    gp = J["gripper_pinion"]
    r = a.meta["gripper"]["pitch_radius_mm"]
    assert gp.mimic["joint"] == "gripper" and abs(gp.mimic["multiplier"] - 1.0 / r) < 1e-5
    assert J["tcp_fixed"].type == "fixed" and J["tcp_fixed"].child == "tcp"
    m = a.meta
    for k in ("arm_joints", "gripper_joint", "tcp_link", "home", "ready", "sizing"):
        assert k in m
    assert m["arm_joints"] == arm.arm_joint_names(dof) and m["gripper_joint"] == "gripper" and m["tcp_link"] == "tcp"
    steps = {st.index for st in a.steps}
    for p in a.parts:
        assert p.kind in KINDS and p.process in PROCESSES
        assert p.step in steps
        assert len(p.explode) == 3 and all(math.isfinite(v) for v in p.explode)
    # joint values of the named poses stay inside the limits
    for pose in ("home", "ready"):
        for n, v in m[pose].items():
            j = J[n]
            assert j.lower - 1e-6 <= v <= j.upper + 1e-6, (pose, n, v, j.lower, j.upper)


def test_zero_pose_is_straight_up():
    d, s, a, _ = _built()
    T = a.world_transforms({n: 0.0 for n in a.meta["arm_joints"]} | {"gripper": 0.0})
    tcp = T["tcp"][:3, 3]
    assert abs(tcp[0]) < 1e-6 and abs(tcp[1]) < 1e-6
    L = s["links_mm"]
    assert abs(tcp[2] - (L["base_h"] + L["L1"] + L["L2"] + L["L3"])) < 0.5
    assert np.allclose(T["tcp"][:3, 2], (0, 0, 1))          # fingers point +z at zero
    sh = T["upper_arm"][:3, 3]
    assert abs(sh[2] - L["base_h"]) < 1e-6


@pytest.mark.parametrize("fam", FAMILIES)
@pytest.mark.parametrize("dof", (4, 5, 6))
def test_home_pose_natural(fam, dof):
    d, s, a, _ = _built(fam, dof)
    for pose in ("home", "ready"):
        T = a.world_transforms(a.meta[pose])
        tcp = T["tcp"]
        tip = a.meta["gripper"]["tip_mm"]
        assert tcp[2, 3] - tip > d.object.diameter_mm / 2, pose          # fingertips above the object top
        assert tcp[:3, 2] @ np.array([0, 0, -1.0]) > 0.85, pose          # gripper points down
        assert tcp[0, 3] > 60.0, pose                                    # in front of the base
        # elbow up: the elbow sits above the shoulder (and, with the compact 4/5-DOF wrist, the forearm is about level)
        assert T["forearm"][2, 3] > T["upper_arm"][2, 3] + 50.0, pose
        if dof < 6 and pose == "home":
            assert T["forearm"][2, 3] > T["wrist"][2, 3] - 15.0, pose
        for link in a.links:                                             # nothing below the table
            assert T[link][2, 3] > -1e-6
    assert all(abs(a.meta["home"][n]) < 1e-9 for n in a.meta["arm_joints"] if n.startswith("j1") or "roll" in n)


def test_gripper_geometry_matches_object():
    """At gripper = 0 the pads touch the design object; opening moves the jaws apart symmetrically."""
    d, s, a, _ = _built()
    R = d.object.diameter_mm / 2
    pads = {p.link: p for p in a.parts if p.kind == "pad"}
    assert set(pads) == {"jaw_left", "jaw_right"}
    T0 = a.world_transforms(a.meta["home"] | {"gripper": 0.0})
    tcp = T0["tcp"]
    inv = np.linalg.inv(tcp)
    for link, p in pads.items():
        Vw = (T0[link][:3, :3] @ p.mesh.vertices.T).T + T0[link][:3, 3]
        Vt = (inv[:3, :3] @ Vw.T).T + inv[:3, 3]
        near = np.min(np.linalg.norm(Vt[:, [0, 2]], axis=1))     # distance from the object axis (tcp y axis)
        assert R < near < R + 2.0, (link, near)
    T1 = a.world_transforms(a.meta["home"] | {"gripper": 8.0})
    dl = np.linalg.inv(T0["gripper_base"]) @ T1["jaw_left"]
    dr = np.linalg.inv(T0["gripper_base"]) @ T1["jaw_right"]
    assert abs(dl[0, 3] + 8.0) < 1e-6 and abs(dr[0, 3] - 8.0) < 1e-6
    # pinion turns by travel / pitch radius
    J = {j.name: j for j in a.joints}
    assert abs(a.joint_values({"gripper": 8.0})["gripper_pinion"] - 8.0 * J["gripper_pinion"].mimic["multiplier"]) < 1e-9
    assert J["gripper"].upper == pytest.approx(a.meta["gripper"]["travel_mm"], abs=0.01)
    assert J["gripper"].lower == pytest.approx(-a.meta["gripper"]["overtravel_mm"])


def test_fsr_and_pads_on_both_fingers():
    d, s, a, _ = _built()
    for side in ("left", "right"):
        link = f"jaw_{side}"
        assert any(p.kind == "sensor" and p.sku == "fsr402" and p.link == link for p in a.parts)
        fingers = [p for p in a.parts if p.link == link and p.name.startswith(f"finger_{side}") and p.kind != "fastener"]
        assert {p.kind for p in fingers} >= {"structural", "pad"}
        for p in fingers:
            assert p.profile is not None and p.profile_frame is not None and p.thickness == d.finger.width_mm
            # profile frame is a proper rotation (identical printed parts, not mirrored)
            assert np.linalg.det(np.asarray(p.profile_frame)[:3, :3]) == pytest.approx(1.0)


# ------------------------------------------------------------------ build configs: processes / manufacturing data
def test_prototype_is_printed():
    _, _, a, _ = _built(cfg="prototype")
    st = [p for p in a.parts if p.kind == "structural" and p.process != "purchased"]
    assert st and {p.process for p in st} <= {"fdm_print", "cnc_turn"}
    assert all(p.meta.get("mass_g", 0) > 0 for p in st)


def test_pilot_cnc_and_sheet_metal():
    _, _, a, _ = _built(cfg="pilot")
    cnc = [p for p in a.parts if p.process == "cnc_mill" and p.profile is not None]
    sheet = [p for p in a.parts if p.process == "sheet_metal"]
    mjf = [p for p in a.parts if p.process == "mjf_print"]
    assert len(cnc) >= 6 and sheet and mjf
    assert any(p.name.endswith("plate_horn") for p in cnc)
    for p in cnc:
        assert p.thickness > 0 and np.asarray(p.profile_frame).shape == (4, 4) and p.profile.area > 50
    for p in sheet:
        sh = p.meta["sheet"]
        assert sh["thickness"] > 0 and "k_factor" in sh and isinstance(sh["bends"], list)
        assert p.profile is not None and p.profile.area > 0        # flat pattern
        for b in sh["bends"]:
            assert len(b["line"]) == 2 and b["angle_deg"] > 0 and b["radius"] > 0
    assert any(p.meta["sheet"]["bends"] for p in sheet)
    # same kinematics as the prototype
    _, _, b, _ = _built(cfg="prototype")
    for j in a.joints:
        assert np.allclose(j.origin, b.joint(j.name).origin)


def test_production_moulded():
    _, _, a, _ = _built(cfg="production")
    mould = [p for p in a.parts if p.process == "injection_mold"]
    assert len(mould) >= 8
    assert any(p.name.startswith("ua_shell_") for p in mould)
    assert any(p.process == "sheet_metal" for p in a.parts)
    # moulded shells are hollow: much lighter than the solid envelope
    sh = next(p for p in a.parts if p.name == "ua_shell_l")
    assert sh.mesh.volume < 0.5 * sh.mesh.convex_hull.volume


def test_structural_meshes_are_solid():
    for cfg in CONFIGS:
        _, _, a, _ = _built(cfg=cfg)
        for p in a.parts:
            if p.kind in ("structural", "cover") and p.process != "purchased":
                assert p.mesh.is_watertight, (cfg, p.name)
                assert p.mesh.volume > 0, (cfg, p.name)


# ------------------------------------------------------------------ fasteners, electronics, cables, steps
def test_fasteners():
    _, _, a, _ = _built()
    fs = [p for p in a.parts if p.kind == "fastener"]
    assert len(fs) >= 20
    for p in fs:
        assert p.meta["spec"].startswith("M") and p.meta["qty"] >= 1
        assert (p.meta["torque_nm"] or 0) > 0 or p.meta["fastener_kind"] in ("washer", "insert")
        assert np.linalg.norm(p.explode) > 1.0
    names = " ".join(p.name for p in fs)
    for j in ("j2_horn_screws", "j3_horn_screws", "j4_horn_screws", "j1_horn_screws", "grip_guide_bolts", "finger_left_screws"):
        assert j in names


def test_electronics_bay_and_panel():
    d, s, a, _ = _built()
    refs = {p.meta.get("ref") for p in a.parts if p.link == "base" and p.kind in ("electronics", "pcb")}
    assert {"U1", "U2", "U3", "J_USB", "J_PWR"} <= refs
    assert any(p.name == "base_feet" for p in a.parts)
    _, _, b, _ = _built(extras="wrist_camera,estop,imu")
    names = {p.name.split("_")[0] for p in b.parts}
    assert any(p.sku == "wrist_camera" and p.link == "gripper_base" for p in b.parts)
    assert any(p.sku == "estop" for p in b.parts) and any(p.sku == "imu" for p in b.parts)
    assert {"camera_usb", "imu_i2c", "estop"} <= {c.name for c in b.cables}
    _, _, c, _ = _built(ecfg="custom_pcb")
    pcb = [p for p in c.parts if p.kind == "pcb"]
    assert pcb and all(p.process == "pcb_assembly" for p in pcb)
    assert not any(p.meta.get("ref") == "U2" for p in c.parts)
    assert names


def test_electronics_plan_integration():
    from morph.robot import electronics2

    d = _design(electronics__config="custom_pcb")
    s = arm.size(d)
    ep = electronics2.plan(d, s)
    a = arm.build(d, s, ep)
    assert a.validate() == []
    ids = [i["id"] for i in a.meta["servo_ids"]]
    assert ids == sorted(ids) and len(ids) == sum(j["count"] for j in s["joints"]) + 1


def test_cables_routed():
    for fam in FAMILIES:
        _, _, a, _ = _built(fam)
        servos = [p for p in a.parts if p.kind == "actuator" and p.name.endswith("_case")]
        kinds = [c.meta.get("kind") for c in a.cables]
        if fam == "hobby_pwm":
            assert kinds.count("pwm") == len(servos)            # star from the PWM driver
        else:
            assert kinds.count("servo_bus") == len(servos)      # daisy chain driver -> ID1 -> ... -> gripper
            bus = [c for c in a.cables if c.meta.get("kind") == "servo_bus"]
            assert bus[0].points[0][0] == "base" and bus[-1].points[-1][0] == "gripper_base"
        assert {"fsr_left", "fsr_right"} <= {c.name for c in a.cables}
        for c in a.cables:
            assert len(c.points) >= 2 and all(l in a.links for l, _ in c.points)
            assert 30 < a.cable_length(c) < 3000
        # cables crossing a pitch joint pass near the axis (service loop) and stay attached in any pose
        fsr = next(c for c in a.cables if c.name == "fsr_left")
        links = [l for l, _ in fsr.points]
        assert {"jaw_left", "gripper_base", "wrist", "forearm", "upper_arm", "turret", "base"} <= set(links)


def test_steps_read_like_a_manual():
    d, s, a, _ = _built()
    idx = [st.index for st in a.steps]
    assert idx == list(range(1, len(idx) + 1))
    titles = " | ".join(st.title.lower() for st in a.steps)
    for w in ("base", "electronics", "turret", "shoulder", "upper arm", "elbow", "forearm", "wrist", "gripper",
              "fingers", "wiring", "calibration"):
        assert w in titles, w
    seen = set()
    for st in a.steps:
        assert st.instructions and len(st.instructions) > 60
        for n in st.parts:
            assert n not in seen
            seen.add(n)
        if any(a.part(n).kind == "fastener" for n in st.parts):
            assert st.fasteners and st.tools
            for f in st.fasteners:
                assert {"spec", "qty", "torque_nm"} <= set(f) and f["qty"] > 0
    assert seen == {p.name for p in a.parts}


# ------------------------------------------------------------------ exports + preview helper
def test_exports(tmp_path):
    _, _, a, _ = _built(dof=6, cfg="pilot")
    a.export_glb(str(tmp_path / "r.glb"))
    a.to_robot_json(str(tmp_path / "r.json"))
    js = json.loads((tmp_path / "r.json").read_text())
    assert len(js["parts"]) == len(a.parts) and js["meta"]["tcp_link"] == "tcp"
    urdf = a.export_urdf(str(tmp_path / "urdf"))
    assert "<robot" in open(urdf).read()
    arm.export_pose_glb(a, str(tmp_path / "pose.glb"), a.meta["home"])
    arm.export_pose_glb(a, str(tmp_path / "exploded.glb"), a.meta["home"], explode=1.0)
    assert (tmp_path / "pose.glb").stat().st_size > 1e5


def test_mass_and_link_lengths_reasonable():
    for cfg in CONFIGS:
        _, s, a, _ = _built(cfg=cfg)
        total = sum(a.part_mass_kg(p) for p in a.parts)
        assert 0.8 < total < 3.5, (cfg, total)
        L = s["links_mm"]
        assert 0.7 < L["L1"] / L["L2"] < 1.4


def test_stub_signatures_kept():
    d = _design()
    s = arm.size(d)
    a = arm.build(d, s, None)          # electronics plan is optional
    assert a.validate() == []
    a2 = arm.build(d, s, {"modules": [{"ref": "U1", "key": "esp32_devkit", "role": "controller"},
                                      {"ref": "PS1", "key": "psu_12v"}]})
    assert a2.validate() == []
