"""Imported open-source robots (robots/<source>/morph_profile.json) as editable Morph assemblies.

The robot's own URDF and meshes are the starting point; Morph then applies the
edits the user asks for: actuator family swaps (re-skins the servo bodies,
re-sizes torque margins, flags the printed holders that no longer fit),
electronics configuration (controller board on the robot's own mount, custom
PCB, onboard computer + battery), the wrist-camera variant, and cabling.
"""

from __future__ import annotations

import json
import os

import numpy as np

from .. import resources
from ..studio.security import contained_path, robot_source
from . import catalog, catalog_extra  # noqa: F401  (registers extra catalog keys)
from . import components
from .assembly import Cable, Part, trans
from .urdf_import import load_urdf

ROOT = resources.path("robots")  # the repo's robots/ folder, or its copy inside an installed wheel
G = np.array([0.0, 0.0, -9.81])
# Feetech STS3215 STL frame -> Morph actuator frame (shaft axis at the horn face, body towards -z)
STS_STL_TO_COMPONENT = trans((12.5, 0.0, 20.2))

def roots() -> list:
    """Where an arm.source name is looked up, in order: the robots shipped with Morph, then robots the user
    imported (MORPH_HOME/.robots, the Studio's projects folder when it set one). Nowhere else: a design names a robot,
    never a folder."""
    from ..importers.robots import robots_dir

    user = robots_dir()
    return [ROOT] + ([user] if os.path.isdir(user) else [])


def robot_dir(source: str) -> str:
    """The bundle folder for arm.source; ValueError for anything but the name of a robot in roots(). The lookup is
    importers.robots.resolve(): shipped robots win, and an imported bundle's profile must name files inside it."""
    from ..importers.robots import resolve

    name = robot_source(source)
    try:
        return resolve(name, ROOT)
    except FileNotFoundError:
        raise ValueError(f"no robot called {name}: arm.source must name a folder in robots/ (or an imported robot)") from None


def bundle_file(d: str, rel) -> str:
    """A file the profile names (its URDF), inside the robot's own folder."""
    path = contained_path(d, rel, decode=False) if isinstance(rel, str) else None
    if not path or not os.path.isfile(path):
        raise ValueError(f"{rel!r} is not a file inside the robot folder {os.path.basename(d)}")
    return path


def load_profile(source: str) -> tuple[dict, str]:
    d = robot_dir(source)  # shipped robots/ first, then MORPH_HOME/.robots/ (user imports)
    with open(os.path.join(d, "morph_profile.json"), encoding="utf-8") as fh:
        return json.load(fh), d


def _extras(design) -> set:
    return {e.strip() for e in design.electronics.extras.split(",") if e.strip()}


def _urdf(design, prof) -> str:
    """The robot's URDF, or its wrist-camera variant: sizing and the build read the same one, so the camera and its
    mount (they ride on the gripper) load every joint."""
    return prof["urdf_camera"] if "wrist_camera" in _extras(design) and prof.get("urdf_camera") else prof["urdf"]


def actuator_plan(design, prof, need: dict | None = None) -> dict:
    """joint -> catalog key after the requested family/voltage edits. Swapping to another family (not the robot's own:
    an OMX keeps its XM430s) picks, for each joint, the weakest actuator that gives `need[joint]` (N·m, margin
    included) from one voltage class of that family (catalog.pick_in_groups): six XL430s on 12 V for the SO-101, never
    XL430s and XL330s on one rail."""
    stock = dict(prof["actuators"])
    joints = list(stock)
    fam = design.arm.actuator_family
    overrides = dict(kv.split("=") for kv in design.arm.actuator_overrides.split(",") if "=" in kv)
    stock_families = {catalog.ACTUATORS.get(k, {}).get("family") for k in stock.values()}
    if fam in catalog.FAMILY_GROUPS and fam not in stock_families:   # a swap, not the robot's own family
        if need is None:
            raise TypeError("actuator_plan needs each joint's torque (need) to swap the actuator family")
        out = catalog.pick_in_groups({j: need[j] for j in joints}, fam)
    else:
        out = dict(stock)
    if "all" in overrides:
        out = {j: overrides["all"] for j in joints}
    for j, k in overrides.items():
        if j in out:
            out[j] = k
    return out


def _subtree(asm, link) -> list:
    out, frontier = [], [link]
    while frontier:
        n = frontier.pop()
        out.append(n)
        frontier += [j.child for j in asm.joints if j.parent == n]
    return out


def _pose_grid(asm, arm_joints) -> list:
    """Poses over the range of the joints that tilt the arm (axis not vertical at q = 0; rolls skipped)."""
    import itertools

    T = asm.world_transforms({})
    tilt = []
    for n in arm_joints:
        j = asm.joint(n)
        ax = T[j.child][:3, :3] @ np.asarray(j.axis, float)
        if "roll" not in n and abs(ax[2]) < 0.9 * np.linalg.norm(ax):
            tilt.append(j)
    tilt = tilt[:4]
    k = {1: 13, 2: 13, 3: 9}.get(len(tilt), 5)
    axes = [np.linspace(j.lower, j.upper, k) for j in tilt]
    return [dict(zip([j.name for j in tilt], vals)) for vals in itertools.product(*axes)] if tilt else []


def worst_case_torques(asm, arm_joints, gripper_joint, payload_kg, grid=True) -> tuple[dict, float | None]:
    """|gravity torque| (N·m) per joint, worst case over q = 0 and (grid=True) a pose grid; and the reach: the
    farthest tool position (mm, horizontal) over the same poses, above the table."""
    return _worst(_gravity(asm, arm_joints, gripper_joint, payload_kg, [{}] + (_pose_grid(asm, arm_joints) if grid else [])),
                  grid)


def _gravity(asm, arm_joints, gripper_joint, payload_kg, poses) -> list:
    """[(|gravity torque| per joint, tool position or None)] for each pose."""
    tcp = asm.meta.get("tcp_link")
    names = [n for n in list(arm_joints) + [gripper_joint] if n]
    subs = {n: _subtree(asm, asm.joint(n).child) for n in names}
    mass = {link: asm.link_mass(link) for link in asm.links}
    out = []
    for q in poses:
        T = asm.world_transforms(q)
        tp = T[tcp][:3, 3] if tcp else None
        taus = {}
        for name in names:
            j = asm.joint(name)
            Tc = T[j.child]
            axis_w, p = Tc[:3, :3] @ np.asarray(j.axis, float), Tc[:3, 3]
            tau = 0.0
            for link in subs[name]:
                m, com = mass[link]
                c = (T[link] @ np.r_[com, 1.0])[:3]
                tau += float(np.cross((c - p) / 1000.0, m * G) @ axis_w)
            if tp is not None and name != gripper_joint:
                tau += float(np.cross((tp - p) / 1000.0, payload_kg * G) @ axis_w)
            taus[name] = abs(tau)
        out.append((taus, tp))
    return out


def _worst(rows, grid=True) -> tuple[dict, float | None]:
    out, reach = {n: 0.0 for n in (rows[0][0] if rows else {})}, None
    for taus, tp in rows:
        if tp is not None and (not grid or tp[2] >= 0.0):
            reach = max(reach or 0.0, float(np.linalg.norm(tp[:2])))
        for n, t in taus.items():
            out[n] = max(out[n], t)
    return out, reach


def _required(reqs: dict, prof) -> dict:
    """Torque each joint is sized for: its gravity torque, a squeezing floor of 0.15 N·m for the jaw, and a fifth of the
    heaviest load for the yaw and roll axes (no gravity load: acceleration and friction instead)."""
    reqs = dict(reqs)
    reqs[prof["gripper_joint"]] = max(reqs[prof["gripper_joint"]], 0.15)
    pitch_max = max(reqs.values())
    return {n: max(reqs[n], 0.2 * pitch_max) for n in prof["arm_joints"] + [prof["gripper_joint"]]}


def size(design) -> dict:
    from .electronics2 import servo_rail

    prof, d = load_profile(design.arm.source)
    asm = load_urdf(bundle_file(d, _urdf(design, prof)), prof, bundle=d)
    payload = design.object.mass_g / 1000.0
    margin_f = design.arm.payload_margin
    # hand-written profiles size at q = 0 (SO-101: the pose its makers quote), and say so; auto-imported arms have no
    # such convention, so they size for the worst case over a pose grid of their tilting joints
    grid = bool((prof.get("imported") or {}).get("auto")) or prof.get("sizing") == "grid"
    rows = _gravity(asm, prof["arm_joints"], prof["gripper_joint"], payload, [{}] + _pose_grid(asm, prof["arm_joints"]))
    reqs, reach = _worst(rows if grid else rows[:1], grid)
    req = _required(reqs, prof)
    names = prof["arm_joints"] + [prof["gripper_joint"]]
    acts = actuator_plan(design, prof, {n: req[n] * margin_f for n in names})
    rail = servo_rail([{"key": acts[n], "joint": n, "torque_with_margin_nm": req[n] * margin_f} for n in names],
                      float(design.electronics.servo_v or 0.0))
    rv = rail["v"]
    joints, issues, warnings = [], [], []
    for name in names:
        key = acts[name]
        avail = round(catalog.torque_at(key, rv), 3)
        if avail < req[name] * margin_f:
            issues.append(f"{name}: needs {req[name] * margin_f:.2f} N·m with margin, {key} gives {avail:.2f} N·m at {rv:g} V")
        joints.append({"name": name, "actuator": key, "torque_req_nm": round(req[name], 3),
                       "torque_with_margin_nm": round(req[name] * margin_f, 3), "torque_avail_nm": avail,
                       "margin": round(avail / max(req[name], 1e-6), 2), "servo_id": prof.get("servo_ids", {}).get(name)})
    out = {"source": design.arm.source, "title": prof.get("title"), "links_mm": {}, "reach_mm": round(reach or 0, 1),
           "payload_g": design.object.mass_g, "joints": [j for j in joints if j["name"] != prof["gripper_joint"]],
           "gripper_actuator": acts[prof["gripper_joint"]],
           "gripper": next(j for j in joints if j["name"] == prof["gripper_joint"]), "servo_rail": rail,
           "issues": issues, "warnings": warnings, "stock_actuators": prof["actuators"]}
    if not grid:
        out["basis"] = "q = 0"
        worst = _required(_worst(rows)[0], prof)
        low = [(j, worst[j["name"]]) for j in joints if worst[j["name"]] > req[j["name"]] * 1.05]
        for j, w in low:
            m = j["torque_avail_nm"] / max(w, 1e-6)
            if m < margin_f:
                warnings.append(f"{j['name']}: the torque margins are at q = 0, the pose this robot's profile is sized in; "
                                f"over its whole joint range it needs up to {w:.2f} N·m, a {m:.2f}x margin at {rv:g} V "
                                f"(below {margin_f:g}x)")
    return out


JAW_LEVER_MM = 70.0   # a stock jaw's squeeze is quoted this far from its pivot (mid-jaw on the SO-101)


def gripper_summary(design, sizing) -> dict:
    """The gripper stage of an imported robot: its stock jaw's force plan, the grip's one source of truth (the electronics
    plan, the firmware, the simulation and the manual all use these numbers). The window comes from the object, with the
    same closed form the simulation uses for flat stock jaws: it slips below 1.5 x m g / (2 mu) and is damaged above its
    damage force / 1.5. The target is the Grip force target when one is set, else the window's own (at least 0.5 N), and
    never more than 80 % of what the gripper servo squeezes at the jaw; the firmware's F command stops at limit_n."""
    from .. import electronics as grip_electronics
    from ..design import FSR

    key = sizing["gripper"]["actuator"]
    o = design.object
    stall = catalog.ACTUATORS[key]["torque_nm"] / (JAW_LEVER_MM / 1000.0)
    sensor = design.finger.sensor if design.finger.sensor in FSR else None
    hold = o.mass_g / 1000.0 * float(-G[2]) / (2.0 * max(o.friction, 0.05))
    lo, hi, target = grip_electronics.force_window(o, hold, sensor, design.electronics.target_force_n, floor=0.5)
    target = min(target, 0.8 * stall, max(design.gripper.force_n, 0.5))
    limit = min(o.max_force_n / 1.2 if o.max_force_n > 0 else stall, FSR[sensor]["max_n"] * 0.95 if sensor else stall, stall)
    return {"actuator": key, "type": "revolute_jaw", "jaw_length_mm": JAW_LEVER_MM,
            "force": {"servo_stall_force_n": round(stall, 1), "hold_force_n": round(hold, 3),
                      "window_n": [round(lo, 2), round(hi, 2) if hi != float("inf") else None],
                      "target_force_n": round(target, 2), "limit_n": round(limit, 2), "object_limit_n": o.max_force_n or None,
                      "sensor": sensor or "none", "issues": [], "warnings": []}}


def _place(asm, comp, prefix, link, T, step, kind=None, explode=(0, 0, 25)):
    for suffix, mesh, color, material in comp.parts:
        m = mesh.copy()
        m.apply_transform(T)
        asm.add_part(Part(name=f"{prefix}_{suffix}", link=link, mesh=m, kind=kind or comp.kind, material=material,
                          color=color, process="purchased", sku=comp.key, step=step, explode=explode,
                          group=f"{comp.key}_{suffix}", meta={"mass_g": comp.spec.get("mass_g", 10.0) / max(len(comp.parts), 1)}))


def build(design, sizing, eplan=None):
    prof, d = load_profile(design.arm.source)
    extras = _extras(design)
    asm = load_urdf(bundle_file(d, _urdf(design, prof)), prof, bundle=d)
    issues = []
    acts = {j["name"]: j["actuator"] for j in sizing["joints"]}
    acts[prof["gripper_joint"]] = sizing["gripper_actuator"]
    rail_v = (sizing.get("servo_rail") or {}).get("v")
    for j in asm.joints:
        if j.name in acts:
            j.actuator = acts[j.name]
            j.effort = catalog.torque_at(acts[j.name], rail_v) if rail_v else catalog.ACTUATORS[acts[j.name]]["torque_nm"]

    # actuator swaps: re-skin the servo body that drives each joint, flag holders that no longer fit
    stock_dims = None
    for p in [p for p in asm.parts if p.kind == "actuator"]:
        drives = next((j for j in asm.joints if j.parent == p.link and j.type != "fixed" and not j.mimic), None)
        if drives is None:
            continue
        key = acts.get(drives.name, prof["actuators"].get(drives.name))
        stock = prof["actuators"].get(drives.name)
        stock_dims = catalog.ACTUATORS[stock]["dims"]
        p.sku = key
        p.meta["drives"] = drives.name
        p.meta["servo_id"] = prof.get("servo_ids", {}).get(drives.name)
        if key != stock:
            comp = components.actuator(key)
            m = comp.mesh()
            m.apply_transform(np.asarray(p.meta["visual_origin"]) @ STS_STL_TO_COMPONENT)
            p.mesh = m
            p.color = "#2b2f36" if comp.spec.get("family") == "dynamixel" else "#1a1c20"
            nd = catalog.ACTUATORS[key]["dims"]
            if any(abs(nd[k] - stock_dims[k]) > 1.0 for k in ("l", "w", "h")):
                for h in asm.parts:
                    if h.link == p.link and "holder" in h.name and h.kind == "structural":
                        h.color = "#ff8a3d"
                        h.meta["needs_redesign"] = f"{h.name} fits {stock} ({stock_dims['l']}x{stock_dims['w']}x{stock_dims['h']} mm), " \
                                                   f"{key} is {nd['l']}x{nd['w']}x{nd['h']} mm"
                issues.append(f"{drives.name}: {key} body {nd['l']}x{nd['w']}x{nd['h']} mm vs stock {stock} "
                              f"{stock_dims['l']}x{stock_dims['w']}x{stock_dims['h']} mm: printed holder must be resized")

    # electronics on the robot's own controller mount (Waveshare plate on the SO-101 base)
    mount = prof.get("controller_mount") or {}
    plate = next((p for p in asm.parts if p.name.startswith(mount.get("part", "~"))), None)
    link = mount.get("link", asm.root)
    if plate is not None:
        b = plate.mesh.bounds
        c = b.mean(axis=0)
        normal_axis = int(np.argmin(b[1] - b[0]))          # thinnest axis = plate normal
        top = b[1].copy()
        origin = c.copy()
        origin[normal_axis] = top[normal_axis] + 1.0
    else:
        normal_axis, origin = 2, np.array([0.0, -60.0, 10.0])
    # orient module +z along the plate normal
    R = np.eye(4)
    if normal_axis == 0:
        R[:3, :3] = [[0, 0, 1], [0, 1, 0], [-1, 0, 0]]
    elif normal_axis == 1:
        R[:3, :3] = [[1, 0, 0], [0, 0, 1], [0, -1, 0]]
    mods = [m for m in (eplan or {}).get("modules", []) if not catalog.MODULES.get(m["key"], {}).get("external")]
    if (eplan or {}).get("custom_pcb"):
        w, h = eplan["custom_pcb"].get("size_mm", [50, 40])
        pcb = components.module("bus_servo_adapter")
        _place(asm, pcb, "custom_pcb", link, trans(origin) @ R, 1, kind="pcb")
        for p in asm.parts:
            if p.name.startswith("custom_pcb_"):
                p.process, p.sku = "pcb_assembly", "custom_pcb"
                p.color = "#0d5c3a" if p.name.endswith("pcb") else p.color
                p.meta["size_mm"] = [w, h]
    else:
        off = 0.0
        for m in mods[:2]:   # the plate holds the bus board; more boards stack beside it
            comp = components.module(m["key"])
            _place(asm, comp, f"mod_{m['ref'].lower()}", link, trans(origin + np.r_[0, 0, 0] + off * np.eye(3)[(normal_axis + 1) % 3]) @ R, 1)
            off += comp.dims["l"] + 6
    if "raspberry_pi" in extras or "onboard_computer" in extras:   # LeKiwi-style: Pi 5 + battery behind the base
        for key, xyz, step in (("raspberry_pi_5", (-110, 0, 4), 1), ("battery_12v", (-110, 0, -30), 1), ("dc_dc_12v_5v_usbc", (-60, 55, 0), 1)):
            _place(asm, components.module(key), f"lekiwi_{key}", asm.root, trans(xyz), step)

    # bus cable: controller -> servo 1 -> ... -> servo 6 (3-pin daisy chain), routed through each link
    servos = sorted([p for p in asm.parts if p.kind == "actuator" and p.meta.get("servo_id")], key=lambda p: p.meta["servo_id"])
    pts = [(link, tuple(origin + np.r_[0, 0, 4.0]))]
    for p in servos:
        b = p.mesh.bounds
        pts.append((p.link, tuple(np.r_[b[0][0], (b[0][1] + b[1][1]) / 2, (b[0][2] + b[1][2]) / 2])))
    bus = "pwm" if design.arm.actuator_family == "hobby_pwm" else "bus"
    asm.cables.append(Cable("servo_bus", pts, diameter=2.6, color="#1b1b1b", src="U1.bus", dst=f"ID{len(servos)}",
                            conductors=3, meta={"kind": bus}))
    if "wrist_camera" in extras:
        cam = next((p for p in asm.parts if p.name.startswith("wrist_camera_so101")), None)
        if cam is not None:
            asm.cables.append(Cable("camera_usb", [(cam.link, tuple(cam.mesh.bounds.mean(axis=0))), (link, tuple(origin))],
                                    diameter=3.5, color="#3a3f47", src="camera", dst="host USB", conductors=4))

    from .arm import grip_meta

    asm.meta.update({"source": design.arm.source, "title": prof.get("title"), "license": prof.get("license"),
                     "source_url": prof.get("source_url"), "gripper_type": prof.get("gripper_type"),
                     "sizing": sizing, "issues": issues, "stock_electronics": prof.get("electronics_stock"),
                     **grip_meta(eplan)})
    return asm
