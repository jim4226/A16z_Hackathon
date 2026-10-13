"""Derive a Morph robot profile (robots/<name>/morph_profile.json) from a normalized URDF, no hand-written file.

  prof, rep = derive("work/cand0/source.urdf", out_dir="work/cand0", bundle="work/repo")

Each derived value carries the reason it was chosen (rep["gripper"]["why"], rep["tcp"]["how"], ...) so the
person can confirm it before the BOM and firmware are trusted. Verified on SO-101, SO-100, Koch v1.1,
OpenManipulator-X and two MuJoCo Menagerie arms (research notes, section 1):

1. Main chain: the root-to-leaf path with the most movable, non-mimic joints (not document order).
2. Gripper: name (+3), mimic followers (+2), prismatic (+1), leaf (+1), last in the chain (+1), short range
   (+1); a full-turn joint is a wrist (-3). Accepted at 3 or more, otherwise Morph asks.
3. TCP: a tool frame from the URDF when its z axis agrees with the hand's geometry, else z along the wrist-roll
   axis (or the jaw's principal axis), 15 mm short of the fingertips, x = hinge x z.
4. Base frame: a morph_base link turns the arm to reach along +x, puts the pan axis on the origin and the
   base on z = 0.
5. Ready pose: damped least squares to about half the reach, 60 mm above the table, tool pointing down.
6. Actuators: the motor model in the mesh names (sts3215, xl430, ...). URDF effort values are simulation
   numbers, not ratings, so an unknown motor stays unknown and the person picks it (README mentions are
   offered as a suggestion).
"""

from __future__ import annotations

import json
import math
import os
import re

import numpy as np

from .common import ImportFailed
from .urdf import finalize, joints_of, mesh_names

GRIP_RX = re.compile(r"grip|jaw|finger|claw|pinch|hand", re.I)
TCP_RX = re.compile(r"(^|_)(tcp|tool0?|ee|eef|end_?effector|gripper_frame|grasp|grasp_frame|tip)(_|$|link)", re.I)
MOTOR_RX = [  # mesh/link name -> catalog key (most specific first)
    (re.compile(r"sts[\s_-]?3215|st[\s_-]?3215|feetech", re.I), "sts3215_7v4"),
    (re.compile(r"xl[\s_-]?430", re.I), "xl430_w250"),
    (re.compile(r"xl[\s_-]?330", re.I), "xl330_m288"),
    (re.compile(r"xm[\s_-]?430", re.I), "xm430_w350"),
    (re.compile(r"mg[\s_-]?996", re.I), "mg996r"),
    (re.compile(r"ds[\s_-]?3218", re.I), "ds3218"),
    (re.compile(r"sg[\s_-]?90", re.I), "sg90"),
]
TEXT_RX = [  # README / BOM / package.xml mentions -> catalog key
    (re.compile(r"sts[\s_-]?3215[^\n|]{0,40}?(7\.4|c001|c044|c046)", re.I), "sts3215_7v4"),
    (re.compile(r"sts[\s_-]?3215[^\n|]{0,40}?12\s?v", re.I), "sts3215"),
    (re.compile(r"sts[\s_-]?3215", re.I), "sts3215_7v4"),
    (re.compile(r"xl[\s_-]?430", re.I), "xl430_w250"),
    (re.compile(r"xl[\s_-]?330", re.I), "xl330_m288"),
    (re.compile(r"xm[\s_-]?430", re.I), "xm430_w350"),
    (re.compile(r"mg[\s_-]?996", re.I), "mg996r"),
    (re.compile(r"ds[\s_-]?3218", re.I), "ds3218"),
    (re.compile(r"\bsg[\s_-]?90\b", re.I), "sg90"),
]
MOTOR_PART_RX = r"^(?!.*(holder|mount|bracket|plate|cover|case|adapter|clip|cap))" \
                r".*(motor|servo|sts3215|st3215|xl430|xl330|xm430|dynamixel|mg996|ds3218|sg90)"
FAMILY_KEYS = {"hobby_pwm": ["sg90", "mg996r", "ds3218"], "smart_bus": ["sts3215_7v4", "sts3215"],
               "dynamixel": ["xl330_m288", "xl430_w250", "xm430_w350"]}
STOCK_ELECTRONICS = {
    "smart_bus": {"bus": "ttl_half_duplex", "driver": "bus_servo_adapter", "host": "laptop over USB"},
    "dynamixel": {"bus": "ttl_half_duplex", "driver": "u2d2", "host": "laptop over USB"},
    "hobby_pwm": {"bus": "pwm", "driver": "pca9685_board", "host": "microcontroller"},
}
TCP_SHORT_MM = 15.0


def _movable(j) -> bool:
    return j["type"] in ("revolute", "continuous", "prismatic") and not j["mimic"]


def _range(j) -> float:
    return float(j["upper"] - j["lower"])


def tree(J: dict) -> tuple[str, dict]:
    kids, children = {}, set()
    for j in J.values():
        kids.setdefault(j["parent"], []).append(j)
        children.add(j["child"])
    roots = {j["parent"] for j in J.values()} - children
    root = sorted(roots)[0] if roots else None
    return root, kids


def main_chain(J: dict) -> list:
    """Joints on the root-to-leaf path with the most movable joints (ties: the longer path)."""
    root, kids = tree(J)
    memo = {}

    def best(link, depth=0):
        if link in memo:
            return memo[link]
        if depth > 500:
            raise ImportFailed("The joint tree is too deep to be an arm.")
        opts = [[j] + best(j["child"], depth + 1) for j in kids.get(link, [])]
        memo[link] = max(opts, key=lambda p: (sum(_movable(x) for x in p), len(p)), default=[])
        return memo[link]

    return best(root) if root else []


def limbs(J: dict) -> list:
    """Movable-joint counts of the branches that leave a link with several children (legs, second arms)."""
    root, kids = tree(J)

    def count(link):
        return sum(_movable(j) + count(j["child"]) for j in kids.get(link, []))

    out = []
    for link, js in kids.items():
        if len(js) > 1:
            out += [count(j["child"]) + _movable(j) for j in js]
    return sorted([c for c in out if c >= 2], reverse=True)


def score_gripper(J: dict, chain: list) -> list:
    """[(score, joint, why)] best first, for movable joints in the chain or branching off its last links."""
    root, kids = tree(J)
    mov = [j for j in chain if _movable(j)]
    if not mov:
        return []
    followers = {}
    for j in J.values():
        if j["mimic"]:
            followers.setdefault(j["mimic"], []).append(j["name"])
    tail = {j["child"] for j in mov[-2:]} | {mov[-1]["parent"]}
    cands = list(mov) + [j for l in tail for j in kids.get(l, []) if _movable(j) and j not in mov]

    def sub_movable(link):
        return any(_movable(j) or sub_movable(j["child"]) for j in kids.get(link, []))

    out = []
    for j in cands:
        s, why = 0, []
        if GRIP_RX.search(j["name"]) or GRIP_RX.search(j["child"]):
            s += 3
            why.append("named like a gripper")
        if j["name"] in followers:
            s += 2
            why.append("drives mimic joints")
        if j["type"] == "prismatic":
            s += 1
            why.append("slides")
        if not sub_movable(j["child"]):
            s += 1
            why.append("leaf")
        if j is mov[-1]:
            s += 1
            why.append("last joint")
        if j["type"] == "continuous" or _range(j) >= 2 * math.pi - 0.1:
            s -= 3
            why.append("turns fully (a wrist)")
        elif j["type"] == "revolute" and _range(j) < 2.7:
            s += 1
            why.append("short range")
        out.append((s, j["name"], why))
    out.sort(key=lambda t: -t[0])
    return out


def _subtree(asm, link) -> list:
    out, todo = [], [link]
    while todo:
        n = todo.pop()
        out.append(n)
        todo += [j.child for j in asm.joints if j.parent == n]
    return out


def _verts(asm, links, T, frame) -> np.ndarray:
    import trimesh
    inv = np.linalg.inv(T[frame])
    vs = [trimesh.transform_points(p.mesh.vertices[:: max(1, len(p.mesh.vertices) // 4000)], inv @ T[p.link])
          for p in asm.parts if p.link in links]
    return np.vstack(vs) if vs else np.zeros((0, 3))


def _closed(gj) -> float:
    return gj.lower if abs(gj.lower) < abs(gj.upper) else gj.upper


def _open(gj) -> float:
    return gj.upper if abs(gj.upper) > abs(gj.lower) else gj.lower


def tcp_frame(asm, gripper: str, arm: list) -> tuple[str, np.ndarray, str]:
    """(hand link, 4x4 hand->tcp in mm, how). z = approach axis, x = hinge x z."""
    gj = asm.joint(gripper)
    hand = gj.parent
    T = asm.world_transforms({gripper: _closed(gj)})
    inv = np.linalg.inv(T[hand])
    jaw_links = set(_subtree(asm, gj.child))
    hand_links = set(_subtree(asm, hand))
    jaw_v = _verts(asm, jaw_links, T, hand)
    all_v = _verts(asm, hand_links, T, hand)
    if len(all_v) == 0:
        raise ImportFailed(f"The gripper ({gripper}) has no meshes to place a tool point on.")
    hinge_o = (inv @ T[gj.child])[:3, 3]
    hinge_a = (inv @ T[gj.child])[:3, :3] @ np.asarray(gj.axis, float)
    hinge_a /= max(np.linalg.norm(hinge_a), 1e-9)
    # principal axis of the jaw (perpendicular to the hinge), pointing away from the hinge
    src = jaw_v if len(jaw_v) >= 10 else all_v
    c = src - src.mean(0)
    if gj.type == "revolute":
        c = c - np.outer(c @ hinge_a, hinge_a)
    a = np.linalg.eigh(c.T @ c)[1][:, -1]
    if (src.mean(0) - hinge_o) @ a < 0:
        a = -a
    how = "jaw's long axis"
    origin = hinge_o
    # wrist roll: the last arm joint turning about the jaw's long axis defines the approach line
    last = asm.joint(arm[-1]) if arm else None
    if last is not None and last.type == "revolute" and last.child in hand_links | {hand} or \
            (last is not None and hand in _subtree(asm, last.child)):
        Tl = inv @ T[last.child]
        roll = Tl[:3, :3] @ np.asarray(last.axis, float)
        roll /= max(np.linalg.norm(roll), 1e-9)
        if abs(roll @ a) > 0.7:
            a = roll if roll @ a > 0 else -roll
            origin = Tl[:3, 3]
            how = f"wrist-roll axis ({arm[-1]})"
    t = (all_v - origin) @ a
    tip = float(t.max())
    p = origin + a * (tip - TCP_SHORT_MM)
    if how.startswith("jaw"):
        slab = all_v[t > tip - 25.0]
        lat = slab.mean(0) - origin
        p = p + (lat - a * (lat @ a))
    z = a / np.linalg.norm(a)
    if gj.type == "prismatic":
        x = hinge_a - z * (hinge_a @ z)
    else:
        x = np.cross(hinge_a, z)
    if np.linalg.norm(x) < 1e-6:
        x = np.cross([0.0, 0.0, 1.0] if abs(z[2]) < 0.9 else [1.0, 0.0, 0.0], z)
    x /= np.linalg.norm(x)
    y = np.cross(z, x)
    M = np.eye(4)
    M[:3, :3] = np.c_[x, y, z]
    M[:3, 3] = p
    return hand, M, f"{how}, {TCP_SHORT_MM:.0f} mm short of the fingertips"


def tool_frame(asm, J: dict, gripper: str, geo: np.ndarray, hand: str) -> tuple[str, np.ndarray] | None:
    """A URDF tool frame (tcp, tool0, gripper_frame, ...) on the hand whose z agrees with the geometry."""
    hand_links = set(_subtree(asm, hand))
    T = asm.world_transforms({gripper: _closed(asm.joint(gripper))})
    for j in J.values():
        if j["type"] == "fixed" and TCP_RX.search(j["child"]) and j["child"] in hand_links and j["child"] != hand:
            M = np.linalg.inv(T[hand]) @ T[j["child"]]
            if M[:3, 2] @ geo[:3, 2] > math.cos(math.radians(25)) and \
                    np.linalg.norm(M[:3, 3] - geo[:3, 3]) < 60.0:
                return j["child"], M
    return None


def base_frame(asm, arm: list) -> dict:
    """yaw (rad) so the arm reaches along +x, xy offset putting the pan axis on the origin, lift to z = 0."""
    T = asm.world_transforms({})
    root = asm.root
    fixed = [root]
    todo = [root]
    while todo:
        n = todo.pop()
        for j in asm.joints:
            if j.parent == n and j.type == "fixed" and j.child != "tcp":
                fixed.append(j.child)
                todo.append(j.child)
    base_parts = [p for p in asm.parts if p.link in fixed]
    vs = np.vstack([(T[p.link] @ np.c_[p.mesh.vertices, np.ones(len(p.mesh.vertices))].T).T[:, :3] for p in base_parts]) \
        if base_parts else asm.posed_mesh().vertices
    zmin = float(vs[:, 2].min())
    # pan axis: the first arm joint, when it turns about a vertical axis
    axis_xy = np.zeros(2)
    if arm:
        j0 = asm.joint(arm[0])
        Tc = T[j0.child]
        ax = Tc[:3, :3] @ np.asarray(j0.axis, float)
        if abs(ax[2]) > 0.9 * np.linalg.norm(ax):
            axis_xy = Tc[:3, 3][:2].copy()
    # forward: where the tool is at the zero pose, or the distal mesh mass when the arm points up
    if "tcp" in T:
        p = T["tcp"][:3, 3]
    else:
        p = asm.posed_mesh().vertices.mean(0)
    d = p[:2] - axis_xy
    if np.linalg.norm(d) < 30.0:
        v = asm.posed_mesh().vertices
        hi = v[v[:, 2] > np.percentile(v[:, 2], 50)]
        d = hi.mean(0)[:2] - axis_xy
    yaw = math.atan2(d[1], d[0]) if np.linalg.norm(d) > 1.0 else 0.0
    c, s = math.cos(-yaw), math.sin(-yaw)
    off = -np.array([c * axis_xy[0] - s * axis_xy[1], s * axis_xy[0] + c * axis_xy[1]])
    return {"yaw": yaw, "xy_mm": [float(off[0]), float(off[1])], "lift_mm": -zmin}


def _jacobian_ik(asm, arm, target, q0, iters=120, tcp="tcp", w_rot=60.0):
    """Damped least squares on [position (mm), tool z vs straight down] with analytic joint Jacobians."""
    lo = np.array([asm.joint(n).lower for n in arm])
    hi = np.array([asm.joint(n).upper for n in arm])
    x = np.clip(np.array([q0.get(n, 0.0) for n in arm], float), lo, hi)
    down = np.array([0.0, 0.0, -1.0])
    tgt = np.asarray(target, float)
    joints = [asm.joint(n) for n in arm]
    for _ in range(iters):
        T = asm.world_transforms(dict(zip(arm, x)))
        Tt = T[tcp]
        p, z = Tt[:3, 3], Tt[:3, 2]
        e = np.r_[p - tgt, w_rot * (z - down)]
        if np.linalg.norm(e[:3]) < 0.3 and np.linalg.norm(z - down) < 0.01:
            break
        Jm = np.zeros((6, len(arm)))
        for i, j in enumerate(joints):
            Tc = T[j.child]
            ax = Tc[:3, :3] @ np.asarray(j.axis, float)
            if j.type == "prismatic":
                Jm[:3, i] = ax
            else:
                Jm[:3, i] = np.cross(ax, p - Tc[:3, 3])
                Jm[3:, i] = w_rot * np.cross(ax, z)
        lam = 1e-1 + 1e-3 * float(e @ e)
        step = -np.linalg.solve(Jm.T @ Jm + lam * np.eye(len(arm)), Jm.T @ e)
        n = np.linalg.norm(step)
        if n > 0.3:
            step *= 0.3 / n
        x = np.clip(x + step, lo, hi)
    T = asm.world_transforms(dict(zip(arm, x)))[tcp]
    return dict(zip(arm, map(float, x))), float(np.linalg.norm(T[:3, 3] - tgt)), float(T[:3, 2] @ down)


def reach_estimate(asm, arm) -> float:
    """Horizontal reach when stretched: the joint-to-joint lengths from the shoulder to the tool."""
    if not arm:
        return 0.0
    chain = asm.chain("tcp") if "tcp" in asm.links else []
    first = next((i for i, j in enumerate(chain) if j.name == arm[0]), 0)
    return float(sum(np.linalg.norm(j.origin[:3, 3]) for j in chain[first + 1:]))


def ready_pose(asm, arm: list) -> dict:
    """IK to a top-down grasp at about half the reach; tries a few targets and keeps the best."""
    reach = max(reach_estimate(asm, arm), 120.0)
    mid = {n: 0.5 * (asm.joint(n).lower + asm.joint(n).upper) if (asm.joint(n).upper - asm.joint(n).lower) < 6.0 else 0.0
           for n in arm}
    seeds = [{n: 0.0 for n in arm}, mid]
    best = None
    for frac in (0.5, 0.45, 0.55, 0.4, 0.6, 0.35):
        for zt in (60.0, 90.0, 40.0, 120.0):
            tgt = (frac * reach, 0.0, zt)
            for s in seeds:
                q, perr, down = _jacobian_ik(asm, arm, tgt, s)
                score = perr + 60.0 * (1.0 - down)
                if best is None or score < best["score"]:
                    best = {"q": q, "err_mm": perr, "down": down, "target_mm": [round(tgt[0], 1), 0.0, zt], "score": score}
                if perr < 2.0 and down > 0.97:
                    break
            if best["err_mm"] < 2.0 and best["down"] > 0.97:
                break
        if best["err_mm"] < 2.0 and best["down"] > 0.97:
            break
    best["reach_mm"] = round(reach, 1)
    return best


def motor_text(J: dict, names: dict, joint: str) -> str:
    j = J[joint]
    return " ".join([names.get(j["parent"], ""), names.get(j["child"], ""), j["parent"], j["child"]])


def actuator_hints(bundle: str | None, max_bytes: int = 2_000_000) -> dict:
    """{catalog key: [file, ...]} for motor models a README, BOM or package.xml in the bundle mentions."""
    out = {}
    if not bundle or not os.path.isdir(bundle):
        return out
    from .common import walk_files
    n = 0
    for rel in walk_files(bundle, 50000):
        base = os.path.basename(rel).lower()
        if not (base.startswith("readme") or base.endswith((".md", ".csv", ".tsv")) or base in ("package.xml", "bom.txt")):
            continue
        p = os.path.join(bundle, *rel.split("/"))
        if os.path.getsize(p) > max_bytes:
            continue
        n += 1
        if n > 200:
            break
        with open(p, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        for rx, key in TEXT_RX:
            if rx.search(text):
                out.setdefault(key, []).append(rel)
                if key.startswith("sts3215"):
                    break                       # one STS3215 variant per file (the most specific wins)
    return out


def actuators(J: dict, names: dict, joints: list, hints: dict, answers: dict) -> dict:
    """{joint: {"key", "basis", "suggest"?}}; key None means the person has to pick one."""
    from ..robot import catalog, catalog_extra  # noqa: F401

    out = {}
    explicit = answers.get("actuators") or {}
    one_hint = next(iter(hints)) if len(hints) == 1 else None
    for n in joints:
        if n in explicit:
            out[n] = {"key": explicit[n], "basis": "you picked it"}
            continue
        text = motor_text(J, names, n)
        key = next((k for rx, k in MOTOR_RX if rx.search(text)), None)
        if key:
            m = next(rx.search(text).group(0) for rx, k in MOTOR_RX if rx.search(text))
            out[n] = {"key": key, "basis": f"mesh name mentions {m}" + (" (assumed the 7.4 V follower variant)"
                                                                        if key == "sts3215_7v4" else "")}
            continue
        eff = J[n]["effort"]
        basis = "the URDF names no motor model" + (f" (its effort {eff:g} is a simulation value, not a rating)" if eff else "")
        out[n] = {"key": None, "basis": basis}
        if one_hint:
            out[n]["suggest"] = one_hint
            out[n]["suggest_basis"] = f"{catalog.ACTUATORS[one_hint]['name']} is mentioned in {hints[one_hint][0]}"
        elif hints:
            out[n]["options"] = sorted(hints)
    return out


def size_for_family(asm, arm: list, gripper: str, family: str, payload_kg: float = 0.06, margin: float = 2.0) -> dict:
    """Weakest actuator of the family meeting the worst-case gravity torque x margin, per joint."""
    from ..robot import catalog, catalog_extra  # noqa: F401
    from ..robot.imported import worst_case_torques

    keys = FAMILY_KEYS.get(family)
    if not keys:
        raise ImportFailed(f"Unknown actuator family {family!r}; pick hobby_pwm, smart_bus or dynamixel.")
    need, _ = worst_case_torques(asm, arm, gripper, payload_kg)
    out = {}
    for n in arm + [gripper]:
        req = max(need.get(n, 0.0), 0.15 if n == gripper else 0.2 * max(need.values() or [0.0]))
        key = next((k for k in keys if catalog.ACTUATORS[k]["torque_nm"] >= req * margin), keys[-1])
        out[n] = key
    return out


def steps_for(asm) -> list:
    steps = []
    for link in asm.ordered_links():
        names = [p.name for p in asm.parts if p.link == link]
        if names:
            steps.append({"title": f"Install {link.replace('_', ' ')}",
                          "parts_regex": "^(" + "|".join(re.escape(n) for n in names) + ")$"})
    return steps


def units_check(asm, arm: list) -> dict:
    """Meshes against the kinematics: an arm whose meshes are 1000x the joint chain was exported in mm."""
    bb = asm.posed_mesh().bounds if asm.parts else np.zeros((2, 3))
    size = float(np.max(bb[1] - bb[0]))
    kin = sum(float(np.linalg.norm(j.origin[:3, 3])) for j in asm.joints if j.child != "tcp")
    out = {"bbox_mm": np.round(bb, 1).tolist(), "size_mm": round(size, 1), "chain_mm": round(kin, 1), "ok": True,
           "suggest_scale": None}
    if kin > 1.0 and size > 40 * kin:
        out.update(ok=False, suggest_scale=0.001, note="the meshes look like millimetres without a 0.001 scale")
    elif kin > 1.0 and size < kin / 40:
        out.update(ok=False, suggest_scale=1000.0, note="the meshes look 1000x too small")
    elif size > 3000:
        out.update(ok=False, note=f"the robot is {size / 1000:.1f} m across; check the units")
    return out


def derive(src_urdf: str, out_dir: str, bundle: str | None = None, answers: dict | None = None,
           title: str | None = None, license_info: dict | None = None, source_url: str = "") -> tuple[dict, dict]:
    """Write out_dir/robot.urdf + return (profile, report). Raises ImportFailed for robots Morph can't build."""
    from ..robot.urdf_import import load_urdf

    answers = dict(answers or {})
    J = joints_of(src_urdf)
    names = mesh_names(src_urdf)
    warnings, scope = [], "ok"
    movable = [j for j in J.values() if _movable(j)]
    if not movable:
        raise ImportFailed("This robot has no movable joints; Morph builds arms. Import a mesh as the object "
                           "to grasp instead.")
    legs = limbs(J)
    if len(legs) >= 3 or (len(movable) > 12 and len(legs) >= 2):
        raise ImportFailed(f"This robot has {len(movable)} joints in {len(legs)} limbs (legged or multi-arm); "
                           "Morph builds single tabletop arms.")
    chain = main_chain(J)
    scores = score_gripper(J, chain)
    gripper = answers.get("gripper")
    if gripper is not None and (gripper not in J or not _movable(J[gripper])):
        raise ImportFailed(f"{gripper!r} is not a movable joint of this robot.")
    if gripper is None and scores and scores[0][0] >= 3:
        gripper = scores[0][1]
    chain_mov = [j["name"] for j in chain if _movable(j)]
    if gripper in chain_mov:
        arm = chain_mov[:chain_mov.index(gripper)]
    else:
        arm = chain_mov
    if answers.get("arm_joints"):
        bad = [n for n in answers["arm_joints"] if n not in J or not _movable(J[n])]
        if bad:
            raise ImportFailed(f"{', '.join(bad)} is not a movable joint of this robot.")
        arm = list(answers["arm_joints"])
    if len(arm) < 3:
        raise ImportFailed(f"Only {len(arm)} arm joints before the gripper; Morph needs at least 3 to pick top-down.")
    if not 4 <= len(arm) <= 6:
        warnings.append(f"{len(arm)} arm joints; Morph's generator is tuned for 4 to 6")
    report = {"chain": [j["name"] for j in chain], "arm_joints": arm, "movable": [j["name"] for j in movable],
              "gripper": {"joint": gripper, "score": None, "why": [], "alternatives": []}}
    sc = {n: (s, w) for s, n, w in scores}
    if gripper:
        picked = bool(answers.get("gripper"))
        report["gripper"].update(score=sc.get(gripper, (None, []))[0],
                                 why=["you picked it"] if picked or gripper not in sc else sc[gripper][1])
    report["gripper"]["alternatives"] = [{"joint": n, "score": s, "why": w} for s, n, w in scores if n != gripper][:4]
    if gripper is None:
        report.update(scope="needs_answers", warnings=warnings + ["No gripper joint found; pick the joint that opens "
                                                                  "and closes the jaws."], ok=False)
        return {}, report

    # ---- geometry: tcp, base frame, ready pose
    base_prof = {"arm_joints": arm, "gripper_joint": gripper}
    asm0 = load_urdf(src_urdf, base_prof)
    if not asm0.parts:
        raise ImportFailed("None of this robot's meshes could be loaded; drop the folder with its meshes.")
    hand, M, how = tcp_frame(asm0, gripper, arm)
    tf = tool_frame(asm0, J, gripper, M, hand)
    if tf:
        M = tf[1]
        how = f"URDF tool frame {tf[0]}"
    from ..robot.assembly import Joint
    asm0.add_joint(Joint("tcp_fixed", "fixed", hand, "tcp", M))
    asm0.meta["tcp_link"] = "tcp"
    bf = base_frame(asm0, arm)
    if answers.get("yaw_deg") is not None:
        bf["yaw"] = math.radians(float(answers["yaw_deg"]))
    urdf_out = os.path.join(out_dir, "robot.urdf")
    finalize_xy(src_urdf, urdf_out, bf, {"link": hand, "T": M})
    asm = load_urdf(urdf_out, base_prof)
    rp = ready_pose(asm, arm)
    if rp["err_mm"] > 10.0 or rp["down"] < 0.9:
        warnings.append(f"no top-down ready pose found (best: {rp['err_mm']:.0f} mm off, tool {math.degrees(math.acos(min(1.0, max(-1.0, rp['down'])))):.0f} deg from vertical); "
                        "the pick-and-place simulation may fail")
    gj = asm.joint(gripper)
    g_open = _open(gj)
    ready = {**rp["q"], gripper: 0.5 * g_open}
    home = {**rp["q"], gripper: float(min(max(0.0, gj.lower), gj.upper))}
    units = units_check(asm, arm)
    if not units["ok"]:
        warnings.append(f"Units: {units.get('note')}; check the preview before building")

    # ---- actuators
    hints = actuator_hints(bundle)
    acts = actuators(J, names, arm + [gripper], hints, answers)
    fam = answers.get("family")
    if fam:
        picked = size_for_family(asm, arm, gripper, fam)
        for n in arm + [gripper]:
            if acts[n]["key"] is None or acts[n]["basis"] != "you picked it":
                acts[n] = {"key": picked[n], "basis": f"{fam} family, sized by Morph for the worst-case pose"}
    if answers.get("accept_suggestions"):
        for n, a in acts.items():
            if a["key"] is None and a.get("suggest"):
                acts[n] = {"key": a["suggest"], "basis": a["suggest_basis"] + " (accepted)"}
    from ..robot import catalog, catalog_extra  # noqa: F401
    for n, a in acts.items():
        if a["key"] is not None and a["key"] not in catalog.ACTUATORS:
            raise ImportFailed(f"{a['key']!r} is not an actuator Morph knows; pick one of "
                               f"{', '.join(sorted(catalog.ACTUATORS))}.")
    unknown = [n for n, a in acts.items() if a["key"] is None]
    keys = [a["key"] for a in acts.values() if a["key"]]
    families = sorted({catalog.ACTUATORS[k]["family"] for k in keys})
    family = families[0] if len(families) == 1 else ("smart_bus" if not families else families[0])
    if len(families) > 1:
        warnings.append(f"mixed actuator families ({', '.join(families)}); they need separate buses")

    names_all = " ".join(names.values())
    overrides = {}
    if re.search(MOTOR_PART_RX, names_all, re.I | re.M) or any(re.search(MOTOR_PART_RX, v, re.I) for v in names.values()):
        overrides[MOTOR_PART_RX] = {"kind": "actuator", "material": "purchased", "process": "purchased", "color": "#1d1f23"}
    profile = {
        "name": os.path.basename(out_dir), "title": title or "Imported robot", "source_url": source_url,
        "license": (license_info or {}).get("summary") or "unknown (imported)", "urdf": "robot.urdf",
        "material": "PLA", "process": "fdm_print", "color": "#d9d9d9", "part_overrides": overrides,
        "actuators": {n: a["key"] for n, a in acts.items()},
        "servo_ids": {n: i + 1 for i, n in enumerate(arm + [gripper])},
        "arm_joints": arm, "gripper_joint": gripper, "gripper_type": f"{gj.type}_jaw",
        "home": home, "ready": ready, "electronics_stock": STOCK_ELECTRONICS.get(family, {}),
        "imported": {"auto": True, "family": family, "confirm": ["gripper", "actuators", "tcp"]},
    }
    profile["steps"] = steps_for(asm)
    asm_prof = load_urdf(urdf_out, {**profile, "actuators": {k: v for k, v in profile["actuators"].items() if v}})
    errs = asm_prof.validate()
    if errs:
        raise ImportFailed(f"The imported robot doesn't validate: {errs[0]}")
    T = asm.world_transforms(ready)
    report.update({
        "ok": not unknown, "scope": "needs_answers" if unknown else scope,
        "tcp": {"how": how, "link": hand, "xyz_mm": np.round(M[:3, 3], 1).tolist(),
                "ready_xyz_mm": np.round(T["tcp"][:3, 3], 1).tolist()},
        "base": {"yaw_deg": round(math.degrees(bf["yaw"]), 1), "xy_mm": [round(v, 1) for v in bf["xy_mm"]],
                 "lift_mm": round(bf["lift_mm"], 1)},
        "ready": {"err_mm": round(rp["err_mm"], 1), "tool_down": round(rp["down"], 3), "target_mm": rp["target_mm"],
                  "reach_mm": rp["reach_mm"]},
        "actuators": acts, "unknown_actuators": unknown, "family": family, "hints": hints,
        "units": units, "parts": len(asm_prof.parts), "warnings": warnings,
    })
    with open(os.path.join(out_dir, "morph_profile.json"), "w", encoding="utf-8") as fh:
        json.dump(profile, fh, indent=1)
    return profile, report


def finalize_xy(src: str, out: str, bf: dict, tcp: dict) -> str:
    """finalize() with the pan-axis offset: morph_base -> old root = trans(xy, lift) @ rotz(-yaw)."""
    from .common import parse_xml
    from .urdf import _write

    finalize(src, out, -bf["yaw"], bf["lift_mm"], tcp)
    root_el = parse_xml(out)
    for j in root_el.findall("joint"):
        if j.get("name") == "morph_base_fixed":
            o = j.find("origin")
            x, y = bf["xy_mm"]
            o.set("xyz", f"{x / 1000.0:.6f} {y / 1000.0:.6f} {bf['lift_mm'] / 1000.0:.6f}")
    _write(root_el, out)
    return out
