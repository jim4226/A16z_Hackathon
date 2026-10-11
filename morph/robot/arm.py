"""Parametric robot arm + sensorized gripper generator.

STUB GEOMETRY: correct kinematics, naming and API (the contract); the
detailed-geometry pass replaces the part shapes with realistic ones.

Kinematic contract (every generator must follow it; sim + viewer rely on it)
---------------------------------------------------------------------------
Links:  base -> turret -> upper_arm -> forearm -> wrist [-> wrist_yaw] -> gripper_base
        gripper_base -> jaw_left, jaw_right, pinion, tcp
Joints (zero pose = arm pointing straight up, gripper fingers pointing +z):
  j1_base_yaw     base->turret          revolute, axis +z
  j2_shoulder     turret->upper_arm     revolute, axis +y
  j3_elbow        upper_arm->forearm    revolute, axis +y
  j4_wrist_pitch  forearm->wrist        revolute, axis +y
  j5_wrist_yaw    wrist->wrist_yaw      revolute, axis +y... only when arm.dof == 6 (axis +x)
  j5_wrist_roll   (dof>=5) wrist(_yaw)->gripper_base revolute, axis +z; dof==4 -> fixed joint "wrist_fixed"
  gripper         gripper_base->jaw_left    prismatic, axis -x, value = mm opened beyond contact with the
                                            design object (0 = touching it, negative = squeezing/overtravel)
  gripper_right   gripper_base->jaw_right   prismatic, axis +x, mimic gripper x1
  gripper_pinion  gripper_base->pinion      revolute, mimic gripper x(1/pitch_radius)
  tcp_fixed       gripper_base->tcp         fixed; tcp = object centre when grasped, +z = approach direction
assembly.meta: {"arm_joints": [...], "gripper_joint": "gripper", "tcp_link": "tcp",
                "home": {joint: value}, "ready": {joint: value}, "sizing": {...}}
"""

from __future__ import annotations

import math

import numpy as np
import trimesh

from . import catalog, components
from .assembly import Assembly, Cable, Joint, Part, Step, rot, trans
from ..design import build_profile
from ..mechanism import object_center_v

G = 9.81


def arm_joint_names(dof: int) -> list:
    names = ["j1_base_yaw", "j2_shoulder", "j3_elbow", "j4_wrist_pitch"]
    if dof >= 6:
        names.append("j5_wrist_yaw")
    if dof >= 5:
        names.append("j5_wrist_roll" if dof == 5 else "j6_wrist_roll")
    return names


def size(design) -> dict:
    """Link lengths from reach, then static torque at full stretch -> actuator per joint."""
    a = design.arm
    reach = a.reach_mm
    L1, L2 = 0.42 * reach, 0.36 * reach
    L3 = reach - L1 - L2                      # wrist pitch axis -> tcp
    payload_kg = design.object.mass_g / 1000.0
    family = catalog.FAMILY_ACTUATORS[a.actuator_family]
    overrides = dict(kv.split("=") for kv in a.actuator_overrides.split(",") if "=" in kv)
    act_mass = catalog.ACTUATORS[family[-1]]["mass_g"] / 1000.0
    grip_kg = 0.18
    link_kg_per_m = 0.35
    # moments about each pitch joint with the arm horizontal (worst case)
    def moment(dist_to_tcp, links_beyond):
        m = (payload_kg + grip_kg) * dist_to_tcp
        for length, start in links_beyond:   # distributed link mass + actuator at its far end
            m += link_kg_per_m * length / 1000.0 * (start + length / 2) + act_mass * (start + length)
        return m * G / 1000.0  # N*m (distances in mm)
    req = {
        "j1_base_yaw": 0.25 * moment(reach, [(L1, 0), (L2, L1)]),     # yaw: inertia/friction, not gravity
        "j2_shoulder": moment(reach, [(L1, 0), (L2, L1)]),
        "j3_elbow": moment(L2 + L3, [(L2, 0)]),
        "j4_wrist_pitch": moment(L3, []),
        "j5_wrist_yaw": 0.3 * moment(L3, []),
        "j5_wrist_roll": 0.15 * moment(L3, []), "j6_wrist_roll": 0.15 * moment(L3, []),
    }
    joints, issues = [], []
    for jn in arm_joint_names(a.dof):
        need = req[jn] * a.payload_margin
        key = overrides.get(jn.split("_")[0]) or overrides.get(jn)
        if not key:
            key = next((k for k in family if catalog.ACTUATORS[k]["torque_nm"] >= need), family[-1])
        avail = catalog.ACTUATORS[key]["torque_nm"]
        if avail < need:
            issues.append(f"{jn}: needs {need:.2f} N·m with margin, {key} gives {avail:.2f} N·m")
        joints.append({"name": jn, "actuator": key, "torque_req_nm": round(req[jn], 3),
                       "torque_with_margin_nm": round(need, 3), "torque_avail_nm": avail,
                       "margin": round(avail / max(req[jn], 1e-6), 2)})
    grip_act = family[0] if a.actuator_family != "hobby_pwm" else (design.electronics.servo if design.electronics.servo != "auto" else "mg996r")
    return {"links_mm": {"base_h": a.base_height_mm, "L1": round(L1, 1), "L2": round(L2, 1), "L3": round(L3, 1)},
            "reach_mm": reach, "payload_g": design.object.mass_g, "joints": joints, "gripper_actuator": grip_act,
            "issues": issues}


def _part(asm, name, link, mesh, kind="structural", **kw):
    return asm.add_part(Part(name=name, link=link, mesh=mesh, kind=kind, **kw))


def _place_component(asm, comp, prefix, link, T, step, explode=(0, 0, 0), kind=None):
    for suffix, mesh, color, material in comp.parts:
        m = mesh.copy()
        m.apply_transform(T)
        _part(asm, f"{prefix}_{suffix}", link, m, kind or comp.kind, material=material, color=color,
              process="purchased", sku=comp.key, step=step, explode=explode, group=f"{comp.key}_{suffix}",
              meta={"mass_g": comp.spec.get("mass_g", 10.0) / max(len(comp.parts), 1)} if comp.kind in ("actuator", "electronics") else {})


def build(design, sizing: dict, electronics_plan: dict | None = None) -> Assembly:
    a = design.arm
    Lm = sizing["links_mm"]
    asm = Assembly(design.name.replace("-", "_") or "robot")
    acts = {j["name"]: j["actuator"] for j in sizing["joints"]}
    names = arm_joint_names(a.dof)
    printed = dict(material="PETG", color="#e9edf2", process="fdm_print",
                   process_options=("fdm_print", "mjf_print", "cnc_mill", "injection_mold"))

    # base + turret
    _part(asm, "base_plate", "base", components._cyl(70, 12, 0), step=1, **printed)
    asm.add_joint(Joint("j1_base_yaw", "revolute", "base", "turret", trans((0, 0, 30)), (0, 0, 1), -2.6, 2.6,
                        actuator=acts["j1_base_yaw"], effort=catalog.ACTUATORS[acts["j1_base_yaw"]]["torque_nm"]))
    _part(asm, "turret_shell", "turret", components._cyl(45, Lm["base_h"] - 30, 0), step=2, **printed)
    _place_component(asm, components.actuator(acts["j1_base_yaw"]), "act_j1", "base", rot((1, 0, 0), 0) @ trans((0, 0, 30)), 1)

    # pitch chain
    chain = [("j2_shoulder", "turret", "upper_arm", Lm["base_h"] - 30, Lm["L1"]),
             ("j3_elbow", "upper_arm", "forearm", Lm["L1"], Lm["L2"]),
             ("j4_wrist_pitch", "forearm", "wrist", Lm["L2"], 35.0)]
    home = {"j1_base_yaw": 0.0, "j2_shoulder": 0.25, "j3_elbow": 1.35, "j4_wrist_pitch": 1.2}
    for i, (jn, parent, child, offset, length) in enumerate(chain):
        act = acts[jn]
        asm.add_joint(Joint(jn, "revolute", parent, child, trans((0, 0, offset)), (0, 1, 0), -1.9, 1.9, actuator=act,
                            effort=catalog.ACTUATORS[act]["torque_nm"]))
        _place_component(asm, components.actuator(act), f"act_{jn.split('_')[0]}", child, rot((1, 0, 0), -math.pi / 2), 3 + i)
        _part(asm, f"{child}_link", child, components._box(22, 30, length, (0, 0, length / 2)), step=3 + i, **printed)

    last = "wrist"
    if a.dof >= 6:
        asm.add_joint(Joint("j5_wrist_yaw", "revolute", "wrist", "wrist_yaw", trans((0, 0, 35)), (1, 0, 0), -1.6, 1.6,
                            actuator=acts["j5_wrist_yaw"], effort=catalog.ACTUATORS[acts["j5_wrist_yaw"]]["torque_nm"]))
        _part(asm, "wrist_yaw_link", "wrist_yaw", components._box(30, 30, 30, (0, 0, 15)), step=6, **printed)
        last = "wrist_yaw"
    roll = [n for n in names if n.endswith("wrist_roll")]
    wrist_len = 35.0
    if roll:
        asm.add_joint(Joint(roll[0], "revolute", last, "gripper_base", trans((0, 0, wrist_len)), (0, 0, 1), -2.8, 2.8,
                            actuator=acts[roll[0]], effort=catalog.ACTUATORS[acts[roll[0]]]["torque_nm"]))
    else:
        asm.add_joint(Joint("wrist_fixed", "fixed", last, "gripper_base", trans((0, 0, wrist_len))))

    # gripper: fingers from the existing finger generator, at the contact pose
    geom = build_profile(design)
    cv = object_center_v(design, geom)
    tcp_z = Lm["L3"] - wrist_len
    _part(asm, "gripper_plate", "gripper_base", components._box(2 * cv + 20, 12, 40, (0, 14, 20)), step=7, **printed)
    asm.add_joint(Joint("gripper", "prismatic", "gripper_base", "jaw_left", np.eye(4), (-1, 0, 0), -3.0, 14.0,
                        actuator=sizing["gripper_actuator"], effort=design.gripper.force_n))
    asm.add_joint(Joint("gripper_right", "prismatic", "gripper_base", "jaw_right", np.eye(4), (1, 0, 0), -3.0, 14.0,
                        mimic={"joint": "gripper", "multiplier": 1.0}))
    r_p = design.electronics.pinion_radius_mm
    asm.add_joint(Joint("gripper_pinion", "revolute", "gripper_base", "pinion", trans((0, 14, 45)), (0, 1, 0), -3.0, 3.0,
                        mimic={"joint": "gripper", "multiplier": 1.0 / r_p}))
    _part(asm, "pinion_gear", "pinion", components._cyl(r_p, 6, -3), step=7, **printed)
    asm.add_joint(Joint("tcp_fixed", "fixed", "gripper_base", "tcp", trans((0, 0, tcp_z))))
    w = design.finger.width_mm
    for side, link, sgn in (("left", "jaw_left", -1), ("right", "jaw_right", 1)):
        for pname, poly in geom.part_polys().items():
            m = components.trimesh_from_profile(poly, w) if hasattr(components, "trimesh_from_profile") else None
            if m is None:
                from ..design import to_mesh
                m = to_mesh(poly, w)
            # profile (u along finger, v toward object, width) -> gripper frame: u -> +z, v -> -sgn*x, width -> y
            T = np.array([[0, -sgn, 0, sgn * cv], [0, 0, 1, -w / 2], [1, 0, 0, tcp_z - geom.pad_center_u], [0, 0, 0, 1.0]])
            m.apply_transform(T)
            _part(asm, f"finger_{side}_{pname}", link, m, kind="pad" if pname == "pad" else "structural",
                  material="PETG", color="#d9534f" if pname != "pad" else "#ff9a4a", process="fdm_print",
                  process_options=("fdm_print", "sla_print", "cnc_mill"), step=8, group=f"finger_{pname}",
                  profile=poly, profile_frame=T, thickness=w)

    # electronics bay (modules on the base plate)
    plan = electronics_plan or {"modules": [{"key": "esp32_devkit", "ref": "U1"}, {"key": "bus_servo_adapter", "ref": "U2"},
                                            {"key": "buck_5v", "ref": "U3"}]}
    x = -40.0
    for mod in plan["modules"]:
        if catalog.MODULES.get(mod["key"], {}).get("external"):
            continue
        comp = components.module(mod["key"])
        _place_component(asm, comp, f"mod_{mod['ref'].lower()}", "base", trans((x, -45, 12)) @ rot((0, 0, 1), math.pi / 2), 1)
        x += comp.dims["w"] + 8

    asm.add_joint  # noqa: B018  (cables reference links after all joints exist)
    asm.cables.append(Cable("bus_main", [("base", (-40, -45, 20)), ("turret", (0, -40, 40)), ("upper_arm", (0, -20, Lm["L1"] / 2)),
                                         ("forearm", (0, -20, Lm["L2"] / 2)), ("wrist", (0, -15, 20))],
                            diameter=3.0, color="#202020", src="U2.bus", dst="j4.bus"))

    asm.steps = [Step(1, "Base plate, base actuator and electronics", [p.name for p in asm.parts if p.step == 1]),
                 Step(2, "Turret", [p.name for p in asm.parts if p.step == 2]),
                 Step(3, "Shoulder", [p.name for p in asm.parts if p.step == 3]),
                 Step(4, "Upper arm + elbow", [p.name for p in asm.parts if p.step == 4]),
                 Step(5, "Forearm + wrist", [p.name for p in asm.parts if p.step == 5]),
                 Step(6, "Wrist yaw", [p.name for p in asm.parts if p.step == 6]),
                 Step(7, "Gripper body", [p.name for p in asm.parts if p.step == 7]),
                 Step(8, "Fingers and sensor pads", [p.name for p in asm.parts if p.step == 8])]
    asm.meta = {"arm_joints": names, "gripper_joint": "gripper", "tcp_link": "tcp",
                "home": {**{n: 0.0 for n in names}, **home, "gripper": 10.0},
                "ready": {**{n: 0.0 for n in names}, "j2_shoulder": 0.5, "j3_elbow": 1.2, "j4_wrist_pitch": 1.4, "gripper": 14.0},
                "sizing": sizing, "stub": True}
    return asm
