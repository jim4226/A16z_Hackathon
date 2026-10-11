"""Whole-robot MuJoCo simulation: pick-and-place, payload sweep, workspace, shake.

The model is generated from the Assembly alone (kinematic contract in
docs/PLATFORM_CONTRACTS.md), so it follows whatever the arm generator builds:

* one body per link, placed by the joint origins (mm -> m); revolute -> hinge,
  prismatic -> slide; the gripper's prismatic mimic (right jaw) is coupled to
  the left jaw with a joint equality, decorative mimics (pinion) are welded;
* inertials from ``asm.link_mass`` (mass + COM) with an inertia tensor built
  from the part bounding boxes (parallel-axis summed about the link COM);
* collision geometry = one convex hull per significant part (fasteners,
  cables, visual and tiny parts skipped, at most a few per link). Profiled jaw
  parts (finger, pad) are cut into thin convex strips from ``Part.profile`` so
  a concave contour pad cradles the object instead of being filled in by its
  hull (the same idea as morph/sim.py's ``_pad_pieces``);
* one position servo per arm joint, force-limited to the joint effort (stall
  torque), with reflected-rotor armature and velocity feedback; the jaw servo
  is force limited to the firmware grip target, like the FSR loop on the
  real gripper;
* the design object (capsule / sphere / box, friction + mass from the design)
  on a table at ``design.task`` pick xy;
* contact filtering by bit groups: no arm self-collision, the base/turret only
  meet the object, and hand parts that overlap the object at the grasp pose
  (a palm block, a gear) are kept away from it.

Control: a host controller streams joint set-points from a damped-least-squares
IK (MuJoCo Jacobians at the TCP site, top-down approach) and adds gravity and
velocity feed-forward plus a slow integral term, the way a smart-servo arm is
driven from a PC. Torques reported per frame are the servo outputs.

Scenarios (each writes ``sim_<name>.json``, frames at 30 Hz, see the contract):
  pick_place     approach, descend, force-limited grasp, lift, arc move, place, release, retreat
  payload_sweep  hold at a far pose and ramp a payload at the TCP until a servo saturates or the TCP sags
  workspace      Monte-Carlo joint sampling -> reachable TCP cloud, reach radius, envelope, top-down reach
  shake          grasp, lift, oscillate along the object's axis with rising frequency, measure in-hand slip

Public API:
  build_mjcf(asm, design, options=None) -> str
  run(asm, design, scenario, outdir=None, fast=False, **opts) -> dict   (the scenario JSON)
  run_all(asm, design, outdir, scenarios=None, fast=False, **opts) -> {"scenarios": {...}, "mjcf": "robot.xml"}
  render_frames(asm, design, sim, times, path_pattern, ...) -> [png paths]   (needs an offscreen GL backend)
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, field

import numpy as np
from shapely.geometry import box as sbox

MM = 1e-3
G = 9.81
FRAME_HZ = 30.0
FRAME_DT = 1.0 / FRAME_HZ
PLAN_DT = 0.01                     # trajectory sample period (s); the sim interpolates between samples
SCENARIOS = ("pick_place", "payload_sweep", "workspace", "shake")
TITLES = {"pick_place": "Pick and place", "payload_sweep": "Payload sweep at far reach",
          "workspace": "Reachable workspace", "shake": "Shake test (in-hand slip)"}

# contact groups: (contype, conaffinity). Two geoms touch when either one's type bit is in the other's affinity.
B_TABLE, B_OBJ, B_ARM, B_JAW, B_PROX, B_NOOBJ = 1, 2, 4, 8, 16, 32
GROUPS = {
    "table": (B_TABLE, B_OBJ),
    "object": (B_OBJ, B_TABLE | B_ARM | B_JAW | B_PROX),
    "arm": (B_ARM, B_TABLE | B_OBJ),          # moving links: table + object, never each other
    "jaw": (B_JAW, B_TABLE | B_OBJ),          # fingers / pads
    "prox": (B_PROX, B_OBJ),                  # base + turret: only the object
    "noobj": (B_NOOBJ, B_TABLE),              # hand parts that overlap the object at the grasp pose
}
SKIP_KINDS = ("fastener", "cable", "visual")

# servo model
STALL_ERR_RAD = 0.03               # position error at which a servo reaches stall torque (kp = effort / this)
ZETA = 0.9                         # damping ratio of each servo loop
KI = 6.0                           # host-side integral gain (1/s), bounded to half the stall torque
GRIP_SPEED_MM_S = 45.0             # jaw closing / opening speed (pinion at servo speed)
GRIP_SAT_MM = 1.5                  # jaw position error at which the jaw servo reaches its force limit
JAW_ARMATURE_KG = 0.08             # reflected servo inertia seen at each jaw

# hold / slip thresholds
DROP_MM = 12.0                     # object further than this from its in-hand pose = dropped
PLACE_TOL_MM = 15.0
SAG_MM = 5.0

OBJECT_COLORS = {"egg": "#efe3cf", "strawberry": "#d23c3c", "tomato": "#d9462b", "grape": "#6b8e23",
                 "ping pong": "#f6f6f2", "bulb": "#f4f1e6", "can": "#c4c9cf", "marker": "#2d5aa8",
                 "bottle": "#7fb7d9"}


# ====================================================================== small helpers

def _fmt(vals, nd=6) -> str:
    return " ".join(f"{float(v):.{nd}g}" for v in np.ravel(vals))


def _rgba(color: str, alpha: float = 1.0) -> str:
    c = (color or "#9aa3ad").lstrip("#")
    if len(c) < 6:
        c = "9aa3ad"
    return _fmt([int(c[0:2], 16) / 255, int(c[2:4], 16) / 255, int(c[4:6], 16) / 255, alpha], 4)


def _mat2quat(R) -> np.ndarray:
    import mujoco

    q = np.zeros(4)
    mujoco.mju_mat2Quat(q, np.ascontiguousarray(R, float).ravel())
    return q


def _minjerk(s):
    s = np.clip(s, 0.0, 1.0)
    return s * s * s * (10.0 - 15.0 * s + 6.0 * s * s)


def _hull(points: np.ndarray):
    """Convex hull (verts, faces) via manifold3d (no scipy here). Returns None when degenerate."""
    import manifold3d as m3d

    pts = np.asarray(points, float)
    if len(pts) < 4 or np.ptp(pts, axis=0).min() < 1e-3:
        return None
    try:
        h = m3d.Manifold.hull_points(pts)
        mesh = h.to_mesh()
        v = np.asarray(mesh.vert_properties, float)[:, :3]
        f = np.asarray(mesh.tri_verts, int)
        if len(f) < 4 or h.volume() <= 1e-6:
            return None
        return v, f
    except Exception:  # noqa: BLE001  degenerate input
        return None


def _hull_volume(v, f) -> float:
    a, b, c = v[f[:, 0]], v[f[:, 1]], v[f[:, 2]]
    return float(abs(np.einsum("ij,ij->i", a, np.cross(b, c)).sum()) / 6.0)


def _reduce(points: np.ndarray, n: int) -> np.ndarray:
    """Farthest-point subsample (keeps the extremes of a hull)."""
    if len(points) <= n:
        return points
    c = points.mean(0)
    idx = [int(np.argmax(np.linalg.norm(points - c, axis=1)))]
    d = np.linalg.norm(points - points[idx[0]], axis=1)
    for _ in range(n - 1):
        i = int(np.argmax(d))
        idx.append(i)
        d = np.minimum(d, np.linalg.norm(points - points[i], axis=1))
    return points[idx]


def _halfspaces(v, f):
    """Outward face normals and offsets of a convex hull: inside <=> n.x - d <= 0 for all faces."""
    a, b, c = v[f[:, 0]], v[f[:, 1]], v[f[:, 2]]
    n = np.cross(b - a, c - a)
    ln = np.linalg.norm(n, axis=1)
    keep = ln > 1e-9
    n = n[keep] / ln[keep, None]
    d = np.einsum("ij,ij->i", n, a[keep])
    # make outward: centroid must be inside
    cen = v.mean(0)
    flip = (n @ cen - d) > 0
    n[flip] *= -1
    d[flip] *= -1
    return n, d


def _polys(geom):
    if geom.is_empty:
        return []
    if geom.geom_type == "Polygon":
        return [geom]
    return [g for g in getattr(geom, "geoms", []) if g.geom_type == "Polygon"]


def _convex_pieces(poly, strip_mm: float, tol_mm: float) -> list:
    """Approximate convex decomposition of a 2D profile.

    Slices the polygon into strips across its long axis, then greedily merges
    neighbouring strips while the merged hull stays within ``tol_mm`` (mean gap)
    of the true outline, so straight beams become one piece and a curved pad
    face keeps many thin pieces.
    """
    minx, miny, maxx, maxy = poly.bounds
    along_x = (maxx - minx) >= (maxy - miny)
    lo, hi = (minx, maxx) if along_x else (miny, maxy)
    n = max(1, int(math.ceil((hi - lo) / strip_mm - 1e-9)))
    edges = np.linspace(lo, hi, n + 1)
    pieces, cur, cur_lo = [], None, None
    for a, b in zip(edges[:-1], edges[1:]):
        cut = sbox(a, miny - 1, b, maxy + 1) if along_x else sbox(minx - 1, a, maxx + 1, b)
        ps = [p for p in _polys(poly.intersection(cut)) if p.area > 0.02]
        if len(ps) != 1:
            if cur is not None:
                pieces.append(cur.convex_hull)
                cur = None
            pieces += [p.convex_hull for p in ps]
            continue
        p = ps[0]
        if cur is None:
            cur, cur_lo = p, a
            continue
        merged = cur.union(p)
        hull = merged.convex_hull
        if (hull.area - merged.area) / max(b - cur_lo, 1e-6) <= tol_mm and merged.geom_type == "Polygon":
            cur = merged
        else:
            pieces.append(cur.convex_hull)
            cur, cur_lo = p, a
    if cur is not None:
        pieces.append(cur.convex_hull)
    return [np.asarray(h.exterior.coords)[:-1] for h in pieces if h.geom_type == "Polygon" and h.area > 0.05]


# ====================================================================== model description

@dataclass
class SimOptions:
    timestep: float = 0.001
    grip_force_n: float | None = None       # per-finger clamp force; None -> design / closed form
    object_xy: tuple | None = None          # object start (mm, base frame); None -> design.task pick
    object_yaw: float | None = None         # rad; None -> long axis tangential to the base (graspable by any dof)
    max_geoms_per_link: int = 10
    pad_strip_mm: float = 1.0
    hull_verts: int = 48
    table: bool = True


@dataclass
class _GeomSpec:
    name: str
    link: str
    part: str
    verts: np.ndarray         # convex point set, link frame, mm
    group: str
    rgba: str
    kind: str
    hull: tuple = None        # (verts, faces) mm


@dataclass
class _Info:
    """Everything the controller needs to know about the generated MJCF."""
    arm_joints: list
    gripper_joint: str
    jaw_joints: list
    tcp_link: str
    jaw_links: set
    hand_links: set
    efforts: dict
    lower: dict
    upper: dict
    velocity: dict
    ready: dict
    grip_force_n: float
    grip_open_mm: float
    grip_close_mm: float
    obj_axis_tcp: np.ndarray        # object long axis, tcp frame
    obj_close_tcp: np.ndarray       # jaw closing axis, tcp frame
    tip_mm: float                   # fingertip extent beyond the tcp along the approach axis
    object: dict
    excluded: list = field(default_factory=list)
    dropped_parts: list = field(default_factory=list)
    geoms: list = field(default_factory=list)
    kp: dict = field(default_factory=dict)
    kv: dict = field(default_factory=dict)


def _object_dims(design):
    o = design.object
    R = o.diameter_mm / 2.0
    shape = o.shape if o.shape in ("cylinder", "sphere", "box") else "cylinder"
    L = max(o.length_mm, o.diameter_mm if shape == "cylinder" else 0.0)
    return shape, R, L


def _object_color(design) -> str:
    name = (design.object.name or "").lower()
    for k, c in OBJECT_COLORS.items():
        if k in name:
            return c
    return "#e07a5f"


def _object_points(design, spacing=2.0, shrink=1.0) -> np.ndarray:
    """Grid points inside the object (shrunk by ``shrink`` mm), object frame (x closing, y long axis, z approach)."""
    shape, R, L = _object_dims(design)
    r = R - shrink
    h = L / 2.0 - shrink
    xs = np.arange(-R, R + 1e-9, spacing)
    ys = np.arange(-L / 2, L / 2 + 1e-9, spacing)
    P = np.stack(np.meshgrid(xs, ys, xs, indexing="ij"), -1).reshape(-1, 3)
    if shape == "sphere":
        keep = np.linalg.norm(P, axis=1) <= r
    elif shape == "box":
        keep = (np.abs(P[:, 0]) <= r) & (np.abs(P[:, 1]) <= h) & (np.abs(P[:, 2]) <= r)
    else:
        half = max(L / 2.0 - R, 0.0)
        yy = np.clip(P[:, 1], -half, half)
        keep = np.linalg.norm(P - np.c_[np.zeros(len(P)), yy, np.zeros(len(P))], axis=1) <= r
    return P[keep]


def _grip_force_target(asm, design, override=None) -> float:
    """Per-finger clamp force the firmware regulates to (N)."""
    o, f = design.object, design.finger
    cap = max(design.gripper.force_n, 0.5)
    for key in ("grip_target_n", "grip_force_n"):
        if asm.meta.get(key):
            override = override or asm.meta[key]
    if override:
        return float(min(override, cap))
    if design.electronics.target_force_n > 0:
        return float(min(design.electronics.target_force_n, cap))
    # same closed form as morph.analysis / morph.electronics: hold x 1.5, below the crush limit
    try:
        from ..analysis import wedge_gain
        from ..design import build_profile

        gain = wedge_gain(f.pad, build_profile(design).contact_half_angle_deg)
    except Exception:  # noqa: BLE001
        gain = 1.0
    hold = o.mass_g / 1000.0 * G / (2.0 * max(o.friction, 0.05) * gain)
    lo = 1.5 * hold
    hi = o.max_force_n / 1.5 if o.max_force_n > 0 else float("inf")
    if f.sensor == "fsr402":
        hi = min(hi, 16.0)
    target = min(math.sqrt(lo * hi), 2.0 * lo) if hi != float("inf") else 1.3 * lo
    return float(min(max(target, 0.5), cap))


def _effort(asm, j) -> float:
    if j.effort and j.effort > 0:
        return float(j.effort)
    try:
        from . import catalog

        return float(catalog.get(j.actuator)["torque_nm"])
    except Exception:  # noqa: BLE001
        return 2.0


def _velocity(j) -> float:
    v = float(j.velocity or 0.0)
    if v in (0.0, 3.0):  # 3.0 = Joint dataclass default: prefer the actuator's datasheet speed
        try:
            from . import catalog

            return float(catalog.get(j.actuator)["speed_rad_s"])
        except Exception:  # noqa: BLE001
            return v or 3.0
    return v


def _subtree(asm, link) -> set:
    out, stack = set(), [link]
    while stack:
        n = stack.pop()
        out.add(n)
        stack += [j.child for j in asm.joints if j.parent == n]
    return out


def _part_mass(asm, p) -> float:
    try:
        return float(asm.part_mass_kg(p))
    except Exception:  # noqa: BLE001  (no scipy -> trimesh hull volume fails on open meshes)
        h = _hull(np.asarray(p.mesh.vertices))
        from .assembly import DENSITY_G_CC, INFILL

        vol = _hull_volume(*h) if h else 0.0
        return vol / 1000.0 * DENSITY_G_CC.get(p.material, 1.2) * INFILL.get(p.process, 1.0) / 1000.0


def _inertial(asm, link, masses):
    """(mass kg, com mm, full inertia kg m^2 [xx yy zz xy xz yz]) of a link."""
    parts = [p for p in asm.parts if p.link == link]
    L = asm.links[link]
    if not parts:
        m = max(L.mass_kg or 0.002, 1e-4)
        return m, np.zeros(3), np.array([1e-7, 1e-7, 1e-7, 0, 0, 0]) * max(m / 0.01, 1.0)
    ms = np.array([max(masses[p.name], 1e-6) for p in parts])
    cs = np.array([p.mesh.bounds.mean(axis=0) for p in parts])
    m_tot = float(ms.sum())
    com = (ms[:, None] * cs).sum(0) / m_tot
    m = m_tot if L.mass_kg is None else float(L.mass_kg)
    ms = ms * (m / m_tot)
    I = np.zeros((3, 3))
    for p, mi, ci in zip(parts, ms, cs):
        e = np.ptp(p.mesh.bounds, axis=0) * MM
        I += np.diag([mi / 12 * (e[1] ** 2 + e[2] ** 2), mi / 12 * (e[0] ** 2 + e[2] ** 2), mi / 12 * (e[0] ** 2 + e[1] ** 2)])
        dvec = (ci - com) * MM
        I += mi * (np.dot(dvec, dvec) * np.eye(3) - np.outer(dvec, dvec))
    I += np.eye(3) * max(m, 1e-4) * 1e-7
    return max(m, 1e-4), com, np.array([I[0, 0], I[1, 1], I[2, 2], I[0, 1], I[0, 2], I[1, 2]])


def _part_geoms(asm, p, jaw: bool, opts: SimOptions) -> list:
    """Convex point sets (link frame, mm) for one part."""
    if p.profile is not None and p.profile_frame is not None and jaw:
        F = np.asarray(p.profile_frame, float)
        local = (np.linalg.inv(F) @ np.c_[np.asarray(p.mesh.vertices), np.ones(len(p.mesh.vertices))].T).T[:, :3]
        w0, w1 = float(local[:, 2].min()), float(local[:, 2].max())
        if w1 - w0 < 0.2:
            w0, w1 = 0.0, max(p.thickness, 1.0)
        fine = p.kind == "pad"
        pieces = _convex_pieces(p.profile, opts.pad_strip_mm if fine else 2.0 * opts.pad_strip_mm,
                                0.12 if fine else 0.6)
        out = []
        for pts in pieces:
            uvw = np.vstack([np.c_[pts, np.full(len(pts), w0)], np.c_[pts, np.full(len(pts), w1)]])
            xyz = (F @ np.c_[uvw, np.ones(len(uvw))].T).T[:, :3]
            out.append(xyz)
        if out:
            return out
    return [np.asarray(p.mesh.vertices, float)]


def _describe(asm, design, opts: SimOptions):
    """Collision geoms, contact groups, joints and gripper facts for the MJCF."""
    meta = asm.meta or {}
    jnames = {j.name for j in asm.joints}
    arm_joints = [n for n in meta.get("arm_joints", []) if n in jnames] or \
        [j.name for j in asm.joints if j.type == "revolute" and not j.mimic and j.name != meta.get("gripper_joint")]
    gname = meta.get("gripper_joint", "gripper")
    gj = asm.joint(gname) if gname in jnames else None
    tcp_link = meta.get("tcp_link", "tcp") if meta.get("tcp_link", "tcp") in asm.links else asm.ordered_links()[-1]
    jaw_joints = ([gj] if gj else []) + [j for j in asm.joints if j.mimic and gj and j.mimic.get("joint") == gname
                                         and j.type == "prismatic"]
    jaw_links = set()
    for j in jaw_joints:
        jaw_links |= _subtree(asm, j.child)
    hand_root = gj.parent if gj else asm.parent_joint(tcp_link).parent
    hand_links = _subtree(asm, hand_root) - jaw_links
    first = asm.joint(arm_joints[0]).child if arm_joints else asm.root
    prox_links = {asm.root, first}

    ready = {}
    for key in ("ready", "home"):
        if isinstance(meta.get(key), dict):
            ready = {**meta[key], **ready} if not ready else ready
    lower = {j.name: float(j.lower) for j in asm.joints}
    upper = {j.name: float(j.upper) for j in asm.joints}
    efforts = {n: _effort(asm, asm.joint(n)) for n in arm_joints}
    velocity = {n: _velocity(asm.joint(n)) for n in arm_joints}
    grip_open = float(min(upper.get(gname, 14.0) - 0.5, max(ready.get(gname, 12.0), 8.0))) if gj else 0.0
    grip_close = float(lower.get(gname, -3.0)) if gj else 0.0

    # ---- grasp pose geometry: tcp frame axes and where every hand / jaw part sits relative to the object
    qg = dict(ready)
    if gj:
        qg[gname] = 0.0
    T = asm.world_transforms(qg)
    Ttcp_inv = np.linalg.inv(T[tcp_link])
    if gj:
        axis_w = T[gj.child][:3, :3] @ np.asarray(gj.axis, float)
        close_tcp = Ttcp_inv[:3, :3] @ axis_w
    else:
        close_tcp = np.array([1.0, 0, 0])
    close_tcp[2] = 0.0
    close_tcp = close_tcp / max(np.linalg.norm(close_tcp), 1e-9)
    axis_tcp = np.cross([0, 0, 1.0], close_tcp)
    R_obj = np.c_[close_tcp, axis_tcp, [0, 0, 1.0]]          # object frame -> tcp frame
    obj_pts_tcp = _object_points(design) @ R_obj.T

    masses = {p.name: _part_mass(asm, p) for p in asm.parts}
    specs, excluded, dropped = [], [], []
    tip = 0.0
    by_link = {}
    for p in asm.parts:
        if not p.collide or p.kind in SKIP_KINDS:
            continue
        by_link.setdefault(p.link, []).append(p)
    for link, parts in by_link.items():
        jaw = link in jaw_links
        group = "jaw" if jaw else ("prox" if link in prox_links else "arm")
        cand = []
        for p in parts:
            for k, pts in enumerate(_part_geoms(asm, p, jaw, opts)):
                h = _hull(pts)
                if h is None:
                    continue
                vol = _hull_volume(*h)
                ext = np.ptp(h[0], axis=0)
                if vol < 20.0 or ext.max() < 3.0:
                    continue
                cand.append((vol, p, k, h))
        cand.sort(key=lambda c: -c[0])
        limit = opts.max_geoms_per_link if not jaw else 400
        if link == asm.root:
            limit = min(limit, 6)
        keep = [c for c in cand if c[1].kind == "pad"] + [c for c in cand if c[1].kind != "pad"]
        if len(keep) > limit:
            dropped += [f"{c[1].name}" for c in keep[limit:]]
            keep = keep[:limit]
        for vol, p, k, (hv, hf) in keep:
            g = group
            in_hand = link in hand_links or link in jaw_links or link == tcp_link
            if in_hand or link in _wrist_links(asm, arm_joints, hand_root):
                Tl = Ttcp_inv @ T[link]
                v_tcp = (Tl[:3, :3] @ hv.T).T + Tl[:3, 3]
                if jaw:
                    tip = max(tip, float(v_tcp[:, 2].max()))
                if p.kind != "pad":
                    n, d = _halfspaces(v_tcp, hf)
                    inside = np.all(obj_pts_tcp @ n.T - d <= -0.3, axis=1)
                    if inside.any():
                        g = "noobj" if not jaw else "jaw_noobj"
                        excluded.append(p.name)
            pts = _reduce(hv, opts.hull_verts)
            specs.append(_GeomSpec(f"{p.name}__{k}", link, p.name, pts, g, _rgba(p.color), p.kind, (hv, hf)))

    shape, R, L = _object_dims(design)
    info = _Info(arm_joints=arm_joints, gripper_joint=gname if gj else "", jaw_joints=[j.name for j in jaw_joints],
                 tcp_link=tcp_link, jaw_links=jaw_links, hand_links=hand_links, efforts=efforts, lower=lower,
                 upper=upper, velocity=velocity, ready={k: float(v) for k, v in ready.items()},
                 grip_force_n=_grip_force_target(asm, design, opts.grip_force_n), grip_open_mm=grip_open,
                 grip_close_mm=grip_close, obj_axis_tcp=axis_tcp, obj_close_tcp=close_tcp, tip_mm=tip,
                 object={"shape": shape, "diameter_mm": design.object.diameter_mm, "length_mm": L,
                         "mass_g": design.object.mass_g, "friction": design.object.friction,
                         "color": _object_color(design), "axis": "y"},
                 excluded=excluded, dropped_parts=dropped, geoms=specs)
    return info, masses


def _wrist_links(asm, arm_joints, hand_root) -> set:
    """Links between the last pitch joint and the hand (wrist housings that sit just above the object)."""
    out = set()
    pitch = [n for n in arm_joints if "pitch" in n]
    if not pitch:
        return out
    start = asm.joint(pitch[-1]).child
    for j in asm.chain(hand_root):
        if j.child in _subtree(asm, start):
            out.add(j.child)
    return out - {hand_root}


# ====================================================================== MJCF

def _xml(asm, design, opts: SimOptions, info: _Info, masses, kv=None) -> str:
    meta = asm.meta or {}
    shape, R, L = _object_dims(design)
    mu = max(design.object.friction, 0.05)
    contact = 'condim="4" solref="0.004 1" solimp="0.95 0.99 0.001"'
    dt = opts.timestep
    kv = kv or {}

    assets, eq, acts = [], [], []
    geoms_by_link = {}
    for g in info.geoms:
        geoms_by_link.setdefault(g.link, []).append(g)

    # joints that become real MuJoCo joints
    live = set(info.arm_joints) | set(info.jaw_joints)
    gname = info.gripper_joint
    # value at which every other joint is frozen (mimics follow their leader at gripper = 0)
    frozen = asm.joint_values({**info.ready, **({gname: 0.0} if gname else {})})

    def body(link, indent):
        pad = " " * indent
        out = []
        m, com, I = _inertial(asm, link, masses)
        out.append(f'{pad}<inertial pos="{_fmt(com * MM)}" mass="{m:.6g}" fullinertia="{_fmt(I)}"/>')
        for g in geoms_by_link.get(link, []):
            mname = f"m_{g.name}"
            assets.append(f'    <mesh name="{mname}" vertex="{_fmt(g.verts * MM, 6)}"/>')
            grp = "jaw" if g.group == "jaw_noobj" else g.group
            ct, ca = GROUPS[grp]
            if g.group == "jaw_noobj":
                ct, ca = B_NOOBJ, B_TABLE
            fr = f'friction="{mu:.3f} 0.005 0.0001" {contact}' if grp == "jaw" else 'friction="0.6 0.005 0.0001"'
            out.append(f'{pad}<geom name="{g.name}" type="mesh" mesh="{mname}" contype="{ct}" conaffinity="{ca}" '
                       f'{fr} rgba="{g.rgba}" group="{2 if grp == "jaw" else 1}"/>')
        if link == info.tcp_link:
            out.append(f'{pad}<site name="sim_tcp" pos="0 0 0" size="0.004" rgba="1 0.5 0 0.6" group="4"/>')
            out.append(f'{pad}<body name="sim_payload" pos="0 0 0"><inertial pos="0 0 0" mass="1e-6" '
                       f'diaginertia="1e-10 1e-10 1e-10"/></body>')
        for j in [j for j in asm.joints if j.parent == link]:
            T = np.asarray(j.origin, float)
            if j.name not in live:
                T = T @ asm.joint_motion(j, frozen.get(j.name, 0.0))
            pos, quat = T[:3, 3] * MM, _mat2quat(T[:3, :3])
            out.append(f'{pad}<body name="{j.child}" pos="{_fmt(pos)}" quat="{_fmt(quat)}">')
            if j.name in live:
                ax = np.asarray(j.axis, float)
                ax = ax / max(np.linalg.norm(ax), 1e-9)
                if j.type == "prismatic":
                    out.append(f'{pad}  <joint name="{j.name}" type="slide" axis="{_fmt(ax)}" '
                               f'range="{j.lower * MM:.6g} {j.upper * MM:.6g}" armature="{JAW_ARMATURE_KG}" '
                               f'damping="2"/>')
                else:
                    e = info.efforts.get(j.name, 1.0)
                    arm_ = float(np.clip(0.006 * e, 0.002, 0.05))
                    out.append(f'{pad}  <joint name="{j.name}" type="hinge" axis="{_fmt(ax)}" '
                               f'range="{j.lower:.6g} {j.upper:.6g}" armature="{arm_:.4g}" damping="{0.01 * e:.4g}"/>')
            out += body(j.child, indent + 2)
            out.append(f"{pad}</body>")
        return out

    tree = body(asm.root, 6)

    # actuators
    for n in info.arm_joints:
        j = asm.joint(n)
        e = info.efforts[n]
        kp = e / STALL_ERR_RAD
        info.kp[n] = kp
        info.kv[n] = kv.get(n, 0.25 * e)
        acts.append(f'    <position name="act_{n}" joint="{n}" kp="{kp:.6g}" kv="{info.kv[n]:.6g}" forcelimited="true" '
                    f'forcerange="{-e:.6g} {e:.6g}" ctrlrange="{j.lower - 0.5:.4g} {j.upper + 0.5:.4g}"/>')
    if gname:
        j = asm.joint(gname)
        F2 = 2.0 * info.grip_force_n            # the coupled pair: each finger presses F2 / 2
        kpg = F2 / (GRIP_SAT_MM * MM)
        kvg = 2.0 * ZETA * math.sqrt(kpg * 2 * (JAW_ARMATURE_KG + 0.02))
        info.kp[gname], info.kv[gname] = kpg, kvg
        acts.append(f'    <position name="act_{gname}" joint="{gname}" kp="{kpg:.6g}" kv="{kvg:.6g}" forcelimited="true" '
                    f'forcerange="{-F2:.6g} {F2:.6g}" ctrlrange="{(j.lower - 2) * MM:.6g} {(j.upper + 2) * MM:.6g}"/>')
        for n in info.jaw_joints[1:]:
            jm = asm.joint(n)
            k = float(jm.mimic.get("multiplier", 1.0))
            c0 = float(jm.mimic.get("offset", 0.0)) * MM
            eq.append(f'    <joint name="eq_{n}" joint1="{n}" joint2="{gname}" polycoef="{c0:.6g} {k:.6g} 0 0 0" '
                      f'solref="{max(0.004, 2 * dt):.4g} 1" solimp="0.95 0.99 0.001"/>')

    # object + table
    x0, y0 = opts.object_xy if opts.object_xy is not None else (design.task.pick_x_mm, design.task.pick_y_mm)
    yaw = opts.object_yaw if opts.object_yaw is not None else math.atan2(y0, x0)
    oq = [math.cos(yaw / 2), 0, 0, math.sin(yaw / 2)]
    ct, ca = GROUPS["object"]
    ocol = _rgba(info.object["color"])
    if shape == "sphere":
        og = f'type="sphere" size="{R * MM:.6g}"'
    elif shape == "box":
        og = f'type="box" size="{_fmt([R * MM, L / 2 * MM, R * MM])}"'
    else:
        og = f'type="capsule" size="{R * MM:.6g} {max(L / 2 - R, 0.5) * MM:.6g}" euler="1.5707963 0 0"'
    obj = (f'    <body name="sim_object" pos="{_fmt([x0 * MM, y0 * MM, (R + 0.2) * MM])}" quat="{_fmt(oq)}">\n'
           f'      <freejoint name="sim_object"/>\n'
           f'      <geom name="sim_object" {og} mass="{design.object.mass_g / 1000.0:.6g}" friction="{mu:.3f} 0.005 0.001" '
           f'{contact.replace("condim=\"4\"", "condim=\"6\"")} contype="{ct}" conaffinity="{ca}" rgba="{ocol}"/>\n'
           f'    </body>')
    px, py = design.task.place_x_mm, design.task.place_y_mm
    tct, tca = GROUPS["table"]
    table = (f'    <geom name="sim_table" type="box" pos="0.1 0 -0.012" size="0.55 0.6 0.012" contype="{tct}" '
             f'conaffinity="{tca}" friction="0.6 0.005 0.0001" material="table"/>\n'
             f'    <site name="sim_place" pos="{_fmt([px * MM, py * MM, 0.0003])}" type="cylinder" '
             f'size="{(R + 4) * MM:.4g} 0.0003" rgba="0.2 0.75 0.45 0.55"/>\n'
             f'    <site name="sim_pick" pos="{_fmt([x0 * MM, y0 * MM, 0.0003])}" type="cylinder" '
             f'size="{(R + 4) * MM:.4g} 0.0003" rgba="0.95 0.6 0.2 0.35"/>') if opts.table else ""

    name = (asm.name or "robot").replace('"', "")
    return f"""<mujoco model="{name}">
  <!-- generated by morph.robot.simulate from the robot assembly; units m, kg, s, rad -->
  <compiler angle="radian" autolimits="true" boundmass="1e-5" boundinertia="1e-10"/>
  <option timestep="{dt}" integrator="implicitfast" cone="elliptic" impratio="10"/>
  <size memory="64M"/>
  <visual>
    <global offwidth="1280" offheight="960" azimuth="150" elevation="-25"/>
    <quality shadowsize="4096" offsamples="8"/>
    <headlight ambient="0.35 0.35 0.35" diffuse="0.45 0.45 0.45" specular="0.1 0.1 0.1"/>
    <map znear="0.01"/>
  </visual>
  <asset>
    <texture name="sky" type="skybox" builtin="gradient" rgb1="0.93 0.95 0.98" rgb2="0.70 0.75 0.82" width="256" height="256"/>
    <texture name="grid" type="2d" builtin="checker" rgb1="0.86 0.87 0.89" rgb2="0.82 0.83 0.85" width="512" height="512" mark="edge" markrgb="0.75 0.77 0.8"/>
    <material name="table" texture="grid" texrepeat="12 12" reflectance="0.08"/>
{chr(10).join(assets)}
  </asset>
  <worldbody>
    <light name="key" pos="0.6 -0.6 1.2" dir="-0.45 0.45 -0.8" diffuse="0.75 0.75 0.75" castshadow="true"/>
    <light name="fill" pos="-0.6 0.4 0.9" dir="0.5 -0.3 -0.8" diffuse="0.3 0.3 0.32" castshadow="false"/>
{table}
    <body name="{asm.root}" pos="0 0 0">
{chr(10).join(tree)}
    </body>
{obj}
  </worldbody>
  <equality>
{chr(10).join(eq)}
  </equality>
  <actuator>
{chr(10).join(acts)}
  </actuator>
</mujoco>
"""


def build_mjcf(asm, design, options=None) -> str:
    """MJCF for the generated robot + design object + table.

    ``options``: a SimOptions or a dict of its fields (timestep, grip_force_n,
    object_xy, object_yaw, max_geoms_per_link, pad_strip_mm, hull_verts, table).
    """
    return _Robot(asm, design, _opts(options)).xml


def _opts(options) -> SimOptions:
    if isinstance(options, SimOptions):
        return options
    o = SimOptions()
    for k, v in (options or {}).items():
        if hasattr(o, k):
            setattr(o, k, v)
    return o


# ====================================================================== runtime wrapper

class _Robot:
    def __init__(self, asm, design, opts: SimOptions):
        import mujoco

        self.asm, self.design, self.opts = asm, design, opts
        self.info, masses = _describe(asm, design, opts)
        xml = _xml(asm, design, opts, self.info, masses)
        model = mujoco.MjModel.from_xml_string(xml)
        # second pass: velocity gains for a damping ratio ZETA from the joint-space inertia at the ready pose
        data = mujoco.MjData(model)
        self._index(model)
        q = self.q_ready()
        data.qpos[self.qadr] = q
        mujoco.mj_forward(model, data)
        Mfull = np.zeros((model.nv, model.nv))
        mujoco.mj_fullM(model, data, Mfull)
        diag = np.array([Mfull[d, d] + model.dof_armature[d] * 0 for d in self.dadr])
        kv = {n: 2.0 * ZETA * math.sqrt(self.info.kp[n] * max(diag[i], 1e-5)) for i, n in enumerate(self.info.arm_joints)}
        self.xml = _xml(asm, design, opts, self.info, masses, kv=kv)
        self.model = mujoco.MjModel.from_xml_string(self.xml)
        self._index(self.model)
        self.data = mujoco.MjData(self.model)
        self.dik = mujoco.MjData(self.model)
        R = design.object.diameter_mm / 2.0
        self.R = R

    def _index(self, m):
        import mujoco

        inf = self.info
        self.qadr = np.array([m.jnt_qposadr[m.joint(n).id] for n in inf.arm_joints], int)
        self.dadr = np.array([m.jnt_dofadr[m.joint(n).id] for n in inf.arm_joints], int)
        self.aid = np.array([m.actuator(f"act_{n}").id for n in inf.arm_joints], int)
        self.lo = np.array([inf.lower[n] for n in inf.arm_joints])
        self.hi = np.array([inf.upper[n] for n in inf.arm_joints])
        self.eff = np.array([inf.efforts[n] for n in inf.arm_joints])
        self.kp = np.array([inf.kp[n] for n in inf.arm_joints])
        self.kvv = np.array([inf.kv[n] for n in inf.arm_joints])
        self.site = m.site("sim_tcp").id
        self.obj = m.body("sim_object").id
        self.obj_q = m.jnt_qposadr[m.joint("sim_object").id]
        self.obj_geom = m.geom("sim_object").id
        self.payload = m.body("sim_payload").id
        self.tcp_body = m.body(inf.tcp_link).id
        if inf.gripper_joint:
            self.g_q = [m.jnt_qposadr[m.joint(n).id] for n in inf.jaw_joints]
            self.g_act = m.actuator(f"act_{inf.gripper_joint}").id
            jl = m.body(m.joint(inf.gripper_joint).bodyid).id if False else m.jnt_bodyid[m.joint(inf.gripper_joint).id]
            self.jaw_left = jl
            left = _subtree(self.asm, self.asm.joint(inf.gripper_joint).child)
            self.left_bodies = {m.body(n).id for n in left}
            self.g_axis_local = np.asarray(m.jnt_axis[m.joint(inf.gripper_joint).id], float)
        else:
            self.g_q, self.g_act, self.left_bodies = [], None, set()
        self.n = len(inf.arm_joints)
        self.dof = self.n
        self._mj = mujoco

    # ------------------------------------------------------------------ kinematics
    def q_ready(self) -> np.ndarray:
        r = self.info.ready
        q = np.array([r.get(n, 0.0) for n in self.info.arm_joints], float)
        return np.clip(q, self.lo, self.hi)

    def fk(self, q):
        mj, m, d = self._mj, self.model, self.dik
        d.qpos[self.qadr] = q
        mj.mj_kinematics(m, d)
        return d.site_xpos[self.site].copy() / MM, d.site_xmat[self.site].reshape(3, 3).copy()

    def ik(self, p_mm, axis=None, q0=None, iters=80, tol_mm=0.2, approach=(0, 0, -1.0)):
        """Damped least squares to a TCP position (mm) with top-down approach; ``axis`` = object long axis (world)
        to align the fingers with (dof >= 5). Returns (q, pos_err_mm, rot_err_rad)."""
        mj, m, d = self._mj, self.model, self.dik
        q = (self.q_ready() if q0 is None else np.asarray(q0, float)).copy()
        p = np.asarray(p_mm, float) * MM
        zd = np.asarray(approach, float)
        zd = zd / np.linalg.norm(zd)
        jp = np.zeros((3, m.nv))
        jr = np.zeros((3, m.nv))
        ya = self.info.obj_axis_tcp
        use_yaw = axis is not None and self.n >= 5
        wr = 0.15
        ep_n = er_n = 1e9
        for it in range(iters):
            d.qpos[self.qadr] = q
            mj.mj_kinematics(m, d)
            mj.mj_comPos(m, d)
            x = d.site_xpos[self.site]
            Rm = d.site_xmat[self.site].reshape(3, 3)
            ep = p - x
            er = np.cross(Rm[:, 2], zd)
            if use_yaw:
                y = Rm @ ya
                yd = np.asarray(axis, float)
                yd = yd - zd * np.dot(yd, zd)
                yd = yd / max(np.linalg.norm(yd), 1e-9)
                if np.dot(y, yd) < 0:
                    yd = -yd
                er = er + np.cross(y, yd)
            ep_n, er_n = np.linalg.norm(ep), np.linalg.norm(er)
            if ep_n < tol_mm * MM and er_n < 2e-3:
                break
            mj.mj_jacSite(m, d, jp, jr, self.site)
            J = np.vstack([jp[:, self.dadr], wr * jr[:, self.dadr]])
            e = np.r_[ep, wr * er]
            lam = 1e-5 + 1e-3 * min(1.0, ep_n * 10)
            dq = J.T @ np.linalg.solve(J @ J.T + lam * np.eye(6), e)
            mx = np.abs(dq).max()
            if mx > 0.25:
                dq *= 0.25 / mx
            q = np.clip(q + dq, self.lo, self.hi)
        return q, ep_n / MM, er_n

    def ik_robust(self, p_mm, axis=None, seeds=(), **kw):
        best = None
        for s in list(seeds) + [self.q_ready()]:
            q, ep, er = self.ik(p_mm, axis, s, **kw)
            if best is None or ep + 50 * er < best[1] + 50 * best[2]:
                best = (q, ep, er)
            if ep < 0.5 and er < 5e-3:
                break
        return best

    # ------------------------------------------------------------------ sensing
    def grip_force(self, d) -> float:
        """Clamp force on the left jaw along its closing axis (what the FSR behind the pad reads), N."""
        mj, m = self._mj, self.model
        if self.g_act is None or d.ncon == 0:
            return 0.0
        f6 = np.zeros(6)
        jid = m.joint(self.info.gripper_joint).id
        R = d.xmat[m.jnt_bodyid[jid]].reshape(3, 3)
        open_w = R @ self.g_axis_local
        total = 0.0
        for i in range(d.ncon):
            c = d.contact[i]
            g1, g2 = c.geom1, c.geom2
            if self.obj_geom not in (g1, g2):
                continue
            other = g2 if g1 == self.obj_geom else g1
            if m.geom_bodyid[other] not in self.left_bodies:
                continue
            mj.mj_contactForce(m, d, i, f6)
            fw = c.frame.reshape(3, 3).T @ f6[:3]       # force on geom2 (world)
            on_jaw = fw if other == g2 else -fw
            total += float(np.dot(on_jaw, open_w))
        return max(total, 0.0)

    def tcp_pose(self, d):
        return d.site_xpos[self.site].copy(), d.site_xmat[self.site].reshape(3, 3).copy()

    def obj_pose(self, d):
        return d.xpos[self.obj].copy(), d.xquat[self.obj].copy()


# ====================================================================== trajectory planning

class _Plan:
    """Joint/gripper reference sampled every PLAN_DT, with phase labels and markers."""

    def __init__(self, rob: _Robot, q0, grip_mm: float, phase: str):
        self.rob = rob
        self.t = [0.0]
        self.q = [np.asarray(q0, float)]
        self.g = [float(grip_mm)]
        self.ph = [phase]
        self.markers = []
        self.ik_err_mm = 0.0
        self.notes = []

    @property
    def T(self):
        return self.t[-1]

    @property
    def qe(self):
        return self.q[-1]

    def _push(self, qs, phase, grip):
        g = self.g[-1] if grip is None else float(grip)
        for qq in qs:
            self.t.append(self.t[-1] + PLAN_DT)
            self.q.append(np.asarray(qq, float))
            self.g.append(g)
            self.ph.append(phase)

    def hold(self, T, phase, grip=None):
        n = max(1, int(round(T / PLAN_DT)))
        self._push([self.qe] * n, phase, grip)

    def joint(self, q1, T, phase, grip=None):
        n = max(1, int(round(T / PLAN_DT)))
        q0 = self.qe
        s = _minjerk(np.arange(1, n + 1) / n)
        self._push([q0 + (np.asarray(q1) - q0) * si for si in s], phase, grip)

    def cart(self, fn, T, phase, grip=None):
        """fn(s in [0,1]) -> (p_mm, axis_world | None); s already eased by the caller if wanted."""
        n = max(1, int(round(T / PLAN_DT)))
        q = self.qe
        qs = []
        for i in range(1, n + 1):
            p, ax = fn(i / n)
            q, ep, er = self.rob.ik(p, ax, q, iters=25)
            self.ik_err_mm = max(self.ik_err_mm, ep)
            qs.append(q)
        self._push(qs, phase, grip)

    def mark(self, label, t=None):
        self.markers.append({"t": round(self.T if t is None else t, 3), "label": label})


def _tangent(xy):
    a = math.atan2(xy[1], xy[0])
    return np.array([-math.sin(a), math.cos(a), 0.0])


def _line(p0, p1):
    p0, p1 = np.asarray(p0, float), np.asarray(p1, float)
    return lambda s: p0 + (p1 - p0) * _minjerk(s)


# ====================================================================== simulation loop

@dataclass
class _Run:
    frames: list
    stats: dict


def _simulate(rob: _Robot, plan: _Plan, *, obj_xy=None, obj_yaw=None, obj_in_hand=False, payload_fn=None,
              on_step=None, record=True, scenario="") -> _Run:
    mj, m = rob._mj, rob.model
    d = rob.data
    mj.mj_resetData(m, d)
    m.body_mass[rob.payload] = 1e-6
    m.body_inertia[rob.payload] = 1e-10
    info = rob.info
    dt = m.opt.timestep
    tq = np.asarray(plan.t)
    Q = np.asarray(plan.q)
    Gm = np.asarray(plan.g)
    QD = np.gradient(Q, PLAN_DT, axis=0)
    n_steps = int(round(tq[-1] / dt))

    # initial state
    d.qpos[rob.qadr] = Q[0]
    for a in rob.g_q:
        d.qpos[a] = Gm[0] * MM
    if obj_in_hand:
        mj.mj_kinematics(m, d)
        p, Rm = rob.tcp_pose(d)
        Ro = np.c_[Rm @ info.obj_close_tcp, Rm @ info.obj_axis_tcp, Rm @ np.array([0, 0, 1.0])]
        d.qpos[rob.obj_q:rob.obj_q + 3] = p
        d.qpos[rob.obj_q + 3:rob.obj_q + 7] = _mat2quat(Ro)
    else:
        x0, y0 = obj_xy
        yaw = obj_yaw if obj_yaw is not None else math.atan2(y0, x0)
        d.qpos[rob.obj_q:rob.obj_q + 3] = [x0 * MM, y0 * MM, (rob.R + 0.2) * MM]
        d.qpos[rob.obj_q + 3:rob.obj_q + 7] = [math.cos(yaw / 2), 0, 0, math.sin(yaw / 2)]
    mj.mj_forward(m, d)
    d.ctrl[rob.aid] = Q[0] + d.qfrc_bias[rob.dadr] / rob.kp
    if rob.g_act is not None:
        d.ctrl[rob.g_act] = Gm[0] * MM
    mj.mj_forward(m, d)

    integ = np.zeros(rob.n)
    imax = 0.5 * rob.eff
    g_ctrl = Gm[0]
    g_rate = GRIP_SPEED_MM_S * dt
    frames = []
    next_frame = 0.0
    max_tau = np.zeros(rob.n)
    sat_time = np.zeros(rob.n)
    stats = {"max_speed": np.zeros(rob.n), "n_steps": n_steps}
    vmax = np.array([info.velocity[nm] for nm in info.arm_joints])
    k_idx = np.minimum((np.arange(n_steps + 1) * dt / PLAN_DT).astype(int), len(tq) - 1)
    frac = np.clip(np.arange(n_steps + 1) * dt / PLAN_DT - k_idx, 0, 1)
    for step in range(n_steps + 1):
        t = step * dt
        k = k_idx[step]
        k2 = min(k + 1, len(tq) - 1)
        a = frac[step]
        qr = Q[k] * (1 - a) + Q[k2] * a
        qdr = QD[k] * (1 - a) + QD[k2] * a
        q = d.qpos[rob.qadr]
        err = qr - q
        integ = np.clip(integ + KI * rob.kp * err * dt, -imax, imax)
        u = d.qfrc_bias[rob.dadr] + rob.kvv * qdr + integ
        d.ctrl[rob.aid] = qr + u / rob.kp
        if rob.g_act is not None:
            gt = Gm[k]
            g_ctrl = g_ctrl + float(np.clip(gt - g_ctrl, -g_rate, g_rate))
            d.ctrl[rob.g_act] = g_ctrl * MM
        if payload_fn is not None:
            pm = payload_fn(t)
            m.body_mass[rob.payload] = max(pm, 1e-6)
            m.body_inertia[rob.payload] = max(pm, 1e-6) * 1e-4
        if record and t >= next_frame - 1e-9:
            frames.append(_frame(rob, d, t, plan.ph[k]))
            if payload_fn is not None:
                frames[-1]["payload_g"] = round(float(m.body_mass[rob.payload]) * 1000.0, 1)
            next_frame += FRAME_DT
        if on_step is not None and on_step(t, d, plan.ph[k]) is False:
            break
        if step == n_steps:
            break
        mj.mj_step(m, d)
        tau = np.abs(d.actuator_force[rob.aid])
        max_tau = np.maximum(max_tau, tau)
        sat_time = np.where(tau >= 0.995 * rob.eff, sat_time + dt, 0.0)
        stats["max_speed"] = np.maximum(stats["max_speed"], np.abs(d.qvel[rob.dadr]))
        stats["sat_time"] = sat_time
    stats["max_tau"] = max_tau
    stats["max_torque_pct"] = {n: round(float(max_tau[i] / rob.eff[i] * 100.0), 1) for i, n in enumerate(info.arm_joints)}
    stats["max_speed_pct"] = {n: round(float(stats["max_speed"][i] / max(vmax[i], 1e-6) * 100.0), 1)
                              for i, n in enumerate(info.arm_joints)}
    return _Run(frames, stats)


def _frame(rob: _Robot, d, t, phase) -> dict:
    q = [round(float(v), 4) for v in d.qpos[rob.qadr]]
    if rob.g_q:
        q.append(round(float(d.qpos[rob.g_q[0]]) / MM, 2))
    p, qu = rob.obj_pose(d)
    return {"t": round(t, 4), "q": q,
            "obj": [round(float(v) / MM, 2) for v in p] + [round(float(v), 5) for v in qu],
            "tau": [round(float(v), 4) for v in d.actuator_force[rob.aid]],
            "grip_n": round(rob.grip_force(d), 3), "phase": phase}


def _rel_obj(rob, d):
    """Object position in the tcp frame (mm)."""
    p, Rm = rob.tcp_pose(d)
    return Rm.T @ (d.xpos[rob.obj] - p) / MM


# ====================================================================== scenarios

# nominal durations (s) of the motion segments; scaled together to meet design.task.cycle_time_s
NOMINAL = {"to_hover": 1.4, "descend": 1.1, "lift": 1.0, "move": 1.8, "place": 1.1}


def _grip_times(rob):
    info = rob.info
    closing = (info.grip_open_mm - info.grip_close_mm) / GRIP_SPEED_MM_S
    opening = (info.grip_open_mm + 2.0) / GRIP_SPEED_MM_S
    return closing, opening


def _cycle_scale(rob, design):
    closing, opening = _grip_times(rob)
    fixed = 0.25 + 0.15 + closing + 0.35 + 0.1 + opening + 0.2
    return float(np.clip((design.task.cycle_time_s - fixed) / sum(NOMINAL.values()), 0.55, 2.0))


def _reachable(rob, p, ax, q0=None, margin=1.03):
    """IK-reachable with a radial margin (keeps the elbow away from full stretch)."""
    q, ep, er = rob.ik(p, ax, q0, iters=100)
    if ep > 0.5 or er > 5e-3:
        return None
    pm = np.r_[np.asarray(p[:2]) * margin, p[2]]
    _, ep2, er2 = rob.ik(pm, ax, q, iters=100)
    return q if (ep2 < 0.5 and er2 < 5e-3) else None


def _hover_mm(rob, p=None, ax=None):
    """TCP height above the object centre for the approach: fingertips clear the object's top by 30 mm,
    lowered (down to 8 mm clearance) when the hover point would be out of reach."""
    want = rob.R + rob.info.tip_mm + 30.0
    if p is None:
        return want
    lo = rob.R + rob.info.tip_mm + 8.0
    for h in np.linspace(want, lo, 6):
        if _reachable(rob, np.asarray(p, float) + [0, 0, h], ax) is not None:
            return float(h)
    return float(lo)


def _targets(rob, design):
    R = rob.R
    pick = np.array([design.task.pick_x_mm, design.task.pick_y_mm, R])
    place = np.array([design.task.place_x_mm, design.task.place_y_mm, R])
    return pick, place


def _arc(p0, p1, inward, bump):
    """Path around the base between two points: polar interpolation, pulled inward / lifted mid-way."""
    r0, r1 = np.linalg.norm(p0[:2]), np.linalg.norm(p1[:2])
    a0, a1 = math.atan2(p0[1], p0[0]), math.atan2(p1[1], p1[0])
    if a1 - a0 > math.pi:
        a1 -= 2 * math.pi
    elif a0 - a1 > math.pi:
        a1 += 2 * math.pi

    def fn(u):
        e = _minjerk(u)
        b = math.sin(math.pi * e)
        r = (r0 + (r1 - r0) * e) * (1.0 - inward * b)
        a = a0 + (a1 - a0) * e
        z = p0[2] + (p1[2] - p0[2]) * e + bump * b
        return np.array([r * math.cos(a), r * math.sin(a), z]), np.array([-math.sin(a), math.cos(a), 0.0])
    return fn


def _feasible_arc(rob, p0, p1, q0):
    for inward, bump in ((0.06, 20.0), (0.12, 12.0), (0.2, 0.0), (0.3, 0.0)):
        fn = _arc(p0, p1, inward, bump)
        q, ok = q0, True
        for u in np.linspace(0.05, 0.95, 10):
            p, ax = fn(u)
            pm = np.r_[p[:2] * 1.02, p[2]]
            q, ep, er = rob.ik(pm, ax, q, iters=60)
            if ep > 0.5 or er > 5e-3:
                ok = False
                break
        if ok:
            return fn
    return _arc(p0, p1, 0.3, 0.0)


def _grasp_sequence(rob, plan, pick, s, hover):
    """approach (descend open), grasp, lift. Returns the q at the grasp."""
    ax = _tangent(pick[:2])
    above = pick + [0, 0, hover]
    plan.cart(lambda u: (_line(above, pick)(u), ax), NOMINAL["descend"] * s, "approach")
    plan.mark("at grasp height")
    q_grasp = plan.qe.copy()
    t_close = plan.T
    closing, _ = _grip_times(rob)
    plan.hold(0.15, "grasp", grip=rob.info.grip_close_mm)
    plan.hold(closing + 0.35, "grasp")
    plan.mark("gripping", t_close + 0.15 + (rob.info.grip_open_mm / GRIP_SPEED_MM_S))
    plan.cart(lambda u: (_line(pick, above)(u), ax), NOMINAL["lift"] * s, "lift")
    return q_grasp


def _pick_place(rob: _Robot, design, fast=False):
    info = rob.info
    s = _cycle_scale(rob, design)
    pick, place = _targets(rob, design)
    ax_pick, ax_place = _tangent(pick[:2]), _tangent(place[:2])
    hover = min(_hover_mm(rob, pick, ax_pick), _hover_mm(rob, place, ax_place))
    q_hover, e1, _ = rob.ik_robust(pick + [0, 0, hover], ax_pick)
    _, e2, _ = rob.ik_robust(place + [0, 0, 1.5], ax_place, seeds=[q_hover])
    plan = _Plan(rob, rob.q_ready(), info.grip_open_mm, "approach")
    plan.hold(0.25, "approach")
    plan.joint(q_hover, NOMINAL["to_hover"] * s, "approach")
    plan.mark("above the object")
    _grasp_sequence(rob, plan, pick, s, hover)
    plan.mark("lifted")
    above_pick, above_place = pick + [0, 0, hover], place + [0, 0, hover]
    t_move = plan.T
    plan.cart(_feasible_arc(rob, above_pick, above_place, plan.qe), NOMINAL["move"] * s, "move")
    plan.cart(lambda u: (_line(above_place, place + [0, 0, 1.5])(u), ax_place), NOMINAL["place"] * s, "place")
    plan.mark("set down")
    t_release = plan.T
    _, opening = _grip_times(rob)
    plan.hold(0.1, "release", grip=info.grip_open_mm)
    plan.hold(opening + 0.1, "release")
    t_released = plan.T
    plan.mark("released")
    plan.cart(lambda u: (_line(place + [0, 0, 1.5], above_place)(u), ax_place), 0.9 * s, "retreat")
    if not fast:
        plan.joint(rob.q_ready(), 1.2 * s, "retreat")
    plan.hold(0.3, "retreat")

    # monitor in-hand slip between the end of the grasp and the release
    st = {"rel0": None, "slip": 0.0, "grip": [], "carry": False}

    def on_step(t, d, ph):
        if ph in ("lift", "move", "place") and t < t_release:
            rel = _rel_obj(rob, d)
            if st["rel0"] is None:
                st["rel0"] = rel
            st["slip"] = max(st["slip"], float(np.linalg.norm(rel - st["rel0"])))
        return True

    run = _simulate(rob, plan, obj_xy=pick[:2], on_step=on_step)
    fr = run.frames
    carry = [f["grip_n"] for f in fr if f["phase"] in ("lift", "move")]
    final = np.array(fr[-1]["obj"][:3])
    err = float(np.linalg.norm(final - place))
    err_xy = float(np.linalg.norm(final[:2] - place[:2]))
    dropped = st["slip"] > DROP_MM
    success = bool(err <= PLACE_TOL_MM and not dropped and e1 < 2 and e2 < 2)
    plan.mark(f"placed {err_xy:.1f} mm from target" if not dropped else "object dropped", t_released)
    metrics = {
        "success": success, "place_error_mm": round(err, 2), "place_error_xy_mm": round(err_xy, 2),
        "dropped": bool(dropped), "max_slip_mm": round(st["slip"], 2),
        "cycle_time_s": round(t_released, 2), "cycle_target_s": design.task.cycle_time_s,
        "cycle_ok": bool(t_released <= design.task.cycle_time_s + 0.25),
        "grip_target_n": round(info.grip_force_n, 2),
        "grip_n_mean": round(float(np.mean(carry)), 2) if carry else 0.0,
        "grip_n_max": round(float(max(f["grip_n"] for f in fr)), 2),
        "object_limit_n": design.object.max_force_n or None,
        "ik_error_mm": round(max(e1, e2, plan.ik_err_mm), 2), "reachable": bool(e1 < 2 and e2 < 2),
        "max_torque_pct": run.stats["max_torque_pct"], "max_speed_pct": run.stats["max_speed_pct"],
        "move_time_s": round(t_released - t_move, 2),
    }
    if design.object.max_force_n and metrics["grip_n_max"] > design.object.max_force_n:
        metrics["crushed"] = True
        metrics["success"] = False
    return plan, run, metrics


def _far_pose(rob, design):
    """Top-down TCP pose at ~90 % of the reachable radius along the pick direction, at the height that
    puts the wrist level with the shoulder (worst static moment)."""
    info = rob.info
    a = math.atan2(design.task.pick_y_mm, design.task.pick_x_mm)
    u = np.array([math.cos(a), math.sin(a), 0.0])
    q0 = rob.q_ready()
    T = rob.asm.world_transforms({n: 0.0 for n in info.arm_joints})
    pitch = [n for n in info.arm_joints if n not in ("j1_base_yaw",)]
    shoulder_z = T[rob.asm.joint(pitch[0]).child][2, 3] if pitch else 100.0
    wrist = [n for n in info.arm_joints if "wrist_pitch" in n]
    L3 = (T[info.tcp_link][2, 3] - T[rob.asm.joint(wrist[0]).child][2, 3]) if wrist else 60.0
    z = max(shoulder_z - L3, rob.R + 10.0)
    lo, hi, best = 50.0, 1500.0, None
    for _ in range(18):
        r = 0.5 * (lo + hi)
        q, ep, er = rob.ik(u * r + [0, 0, z], _tangent(u[:2]), q0, iters=120)
        if ep < 0.5 and er < 5e-3:
            lo, best = r, q
        else:
            hi = r
    r = 0.9 * lo
    q, ep, er = rob.ik(u * r + [0, 0, z], _tangent(u[:2]), best if best is not None else q0, iters=150)
    return q, np.r_[u[:2] * r, z], lo


def _static_capacity(rob, q):
    """Payload (kg) at the TCP each joint can hold statically at q: (effort - arm gravity torque) / lever."""
    mj, m, d = rob._mj, rob.model, rob.dik
    d.qpos[:] = 0
    d.qpos[rob.qadr] = q
    d.qvel[:] = 0
    mj.mj_forward(m, d)
    tau_g = d.qfrc_bias[rob.dadr].copy()
    jp = np.zeros((3, m.nv))
    mj.mj_jacSite(m, d, jp, None, rob.site)
    lever = np.abs(jp[2, rob.dadr]) * G
    cap = np.where(lever > 1e-6, (rob.eff - np.abs(tau_g)) / np.maximum(lever, 1e-9), np.inf)
    return cap, tau_g


def _payload_sweep(rob: _Robot, design, fast=False):
    info = rob.info
    q_far, p_far, r_max = _far_pose(rob, design)
    cap, tau_g = _static_capacity(rob, q_far)
    mobj = design.object.mass_g / 1000.0
    est = float(np.min(cap)) - mobj
    ramp_T = 3.0 if fast else 5.0
    top = max(1.7 * est, 0.3)
    plan = _Plan(rob, q_far, info.grip_close_mm, "hold")
    t0 = 0.5
    plan.hold(t0 + ramp_T + (1.4 if not fast else 1.0), "hold")
    st = {"fail_t": None, "fail_joint": None, "sag": 0.0, "ref": None, "unload": None, "max_ok": 0.0}

    def payload(t):
        if st["unload"] is not None and t >= st["unload"]:
            return mobj * 0 + 1e-6
        if t < t0:
            return 1e-6
        tt = t if st["fail_t"] is None else st["fail_t"]
        return top * min((tt - t0) / ramp_T, 1.0)

    def on_step(t, d, ph):
        p, _ = rob.tcp_pose(d)
        if st["ref"] is None and t >= t0 - 0.05:
            st["ref"] = p.copy()
        if st["ref"] is not None and st["fail_t"] is None and t > t0:
            sag = float(np.linalg.norm(p - st["ref"])) / MM
            st["sag"] = sag
            tau = np.abs(d.actuator_force[rob.aid])
            satj = [info.arm_joints[i] for i in range(rob.n) if tau[i] >= 0.995 * rob.eff[i]]
            if sag > SAG_MM or (satj and sag > 1.0):
                st["fail_t"] = t
                st["fail_joint"] = satj[0] if satj else info.arm_joints[int(np.argmax(tau / rob.eff))]
                st["unload"] = t + 0.6
            else:
                st["max_ok"] = float(m_payload(t))
        return True

    m_payload = payload
    run = _simulate(rob, plan, obj_in_hand=True, payload_fn=payload, on_step=on_step)
    for f in run.frames:
        if f["t"] < t0:
            f["phase"] = "hold"
        elif st["fail_t"] is None or f["t"] < st["fail_t"]:
            f["phase"] = "hold"
        else:
            f["phase"] = "release" if st["unload"] and f["t"] >= st["unload"] else "hold"
    max_extra = st["max_ok"] if st["fail_t"] is not None else top
    max_payload_g = (max_extra + mobj) * 1000.0
    plan.mark("start payload ramp", t0)
    if st["fail_t"] is not None:
        plan.mark(f"{st['fail_joint']} saturates at {max_payload_g:.0f} g", st["fail_t"])
        plan.mark("payload removed", st["unload"])
    grip_cap = 2 * design.object.friction * design.gripper.force_n / G * 1000.0
    j_lim = info.arm_joints[int(np.argmin(cap))]
    metrics = {
        "success": bool(max_payload_g >= design.object.mass_g * 1.0),
        "max_payload_g": round(max_payload_g, 0), "limited_by": st["fail_joint"] or j_lim,
        "saturated": st["fail_t"] is not None, "sag_at_limit_mm": round(st["sag"], 2),
        "static_estimate_g": round((est + mobj) * 1000.0, 0), "static_limiting_joint": j_lim,
        "rated_payload_g": design.object.mass_g,
        "payload_margin": round(max_payload_g / max(design.object.mass_g, 1e-6), 2),
        "grip_limited_payload_g": round(grip_cap, 0),
        "far_pose_mm": [round(float(v), 1) for v in p_far], "far_radius_mm": round(float(np.linalg.norm(p_far[:2])), 1),
        "max_torque_pct": run.stats["max_torque_pct"],
    }
    if not metrics["saturated"]:
        metrics["note"] = f"no saturation up to {max_payload_g:.0f} g (ramp cap)"
    return plan, run, metrics


def _workspace(rob: _Robot, design, fast=False):
    mj, m = rob._mj, rob.model
    info = rob.info
    d = rob.dik
    rng = np.random.default_rng(7)
    N = 1500 if fast else 4000
    lo, hi = rob.lo.copy(), rob.hi.copy()
    roll = [i for i, n in enumerate(info.arm_joints) if "roll" in n]
    Qs = rng.uniform(lo, hi, size=(N, rob.n))
    for i in roll:
        Qs[:, i] = 0.0
    link_ids = [m.body(n).id for n in rob.asm.ordered_links() if n in rob.asm.links and m.body(n).id > 0]
    pts = []
    for q in Qs:
        d.qpos[rob.qadr] = q
        mj.mj_kinematics(m, d)
        if d.xpos[link_ids, 2].min() < -1e-3 or d.site_xpos[rob.site][2] < 0:
            continue
        pts.append(d.site_xpos[rob.site] / MM)
    P = np.array(pts)
    rad = np.linalg.norm(P[:, :2], axis=1)
    # top-down reach at table grasp height along the pick direction (inner and outer limits)
    a = math.atan2(design.task.pick_y_mm, design.task.pick_x_mm)
    u = np.array([math.cos(a), math.sin(a), 0.0])
    z = rob.R
    ok = []
    for r in np.arange(40.0, 1.15 * rad.max() + 1, 10.0):
        q, ep, er = rob.ik(u * r + [0, 0, z], _tangent(u[:2]), rob.q_ready(), iters=120)
        if ep < 1.0 and er < 1e-2:
            ok.append(r)
    pick, place = _targets(rob, design)
    _, e1, r1 = rob.ik_robust(pick, _tangent(pick[:2]))
    _, e2, r2 = rob.ik_robust(place, _tangent(place[:2]))
    vol = 0.0
    h = _hull(P)
    if h is not None:
        vol = _hull_volume(*h) / 1e6
    metrics = {
        "success": bool(e1 < 1 and e2 < 1), "reach_mm": round(float(rad.max()), 1),
        "max_height_mm": round(float(P[:, 2].max()), 1),
        "reach_3d_mm": round(float(np.linalg.norm(P, axis=1).max()), 1),
        "envelope_mm": {"x": [round(float(P[:, 0].min()), 1), round(float(P[:, 0].max()), 1)],
                        "y": [round(float(P[:, 1].min()), 1), round(float(P[:, 1].max()), 1)],
                        "z": [round(float(P[:, 2].min()), 1), round(float(P[:, 2].max()), 1)]},
        "volume_l": round(vol, 2), "samples": int(N), "valid_fraction": round(len(P) / N, 3),
        "top_down_reach_mm": [round(min(ok), 0), round(max(ok), 0)] if ok else None,
        "pick_reachable": bool(e1 < 1 and r1 < 1e-2), "place_reachable": bool(e2 < 1 and r2 < 1e-2),
        "design_reach_mm": design.arm.reach_mm,
    }
    # viewer tour: sweep the envelope kinematically (torques from inverse dynamics)
    q_r = rob.q_ready()
    plan = _Plan(rob, q_r, info.grip_open_mm, "sample")
    far = int(np.argmax(rad))
    top = int(np.argmax(P[:, 2]))
    qs = [q for q in Qs]
    valid_q = [q for q in qs if True]
    q_far_dir = rob.ik(u * (0.92 * rad.max()) + [0, 0, 120.0], None, q_r, iters=150)[0]
    j1 = 0
    sweep = []
    for ang in (lo[j1] * 0.8, hi[j1] * 0.8):
        q = q_far_dir.copy()
        q[j1] = ang
        sweep.append(q)
    up = np.zeros(rob.n)
    for q1, T in ((q_far_dir, 1.4), (sweep[0], 1.6), (sweep[1], 2.6), (up, 1.8), (q_r, 1.4)):
        plan.joint(q1, T * (0.7 if fast else 1.0), "sample")
        plan.hold(0.15, "sample")
    plan.mark(f"reach {metrics['reach_mm']:.0f} mm", 1.4 * (0.7 if fast else 1.0) + 0.15)
    frames = _kinematic_frames(rob, plan, design)
    sub = P[rng.choice(len(P), size=min(len(P), 1500), replace=False)] if len(P) else P
    _ = (far, top, valid_q)
    return plan, _Run(frames, {}), metrics, [[round(float(v), 1) for v in p] for p in sub]


def _kinematic_frames(rob, plan, design):
    """Frames from a joint trajectory without dynamics: tau = inverse dynamics (gravity + inertia)."""
    mj, m, d = rob._mj, rob.model, rob.dik
    tq, Q = np.asarray(plan.t), np.asarray(plan.q)
    QD = np.gradient(Q, PLAN_DT, axis=0)
    QDD = np.gradient(QD, PLAN_DT, axis=0)
    pick, _ = _targets(rob, design)
    yaw = math.atan2(pick[1], pick[0])
    oq = [round(math.cos(yaw / 2), 5), 0.0, 0.0, round(math.sin(yaw / 2), 5)]
    frames = []
    for t in np.arange(0.0, tq[-1] + 1e-9, FRAME_DT):
        k = min(int(round(t / PLAN_DT)), len(tq) - 1)
        d.qpos[:] = 0
        d.qvel[:] = 0
        d.qacc[:] = 0
        d.qpos[rob.qadr] = Q[k]
        d.qvel[rob.dadr] = QD[k]
        d.qacc[rob.dadr] = QDD[k]
        for a in rob.g_q:
            d.qpos[a] = plan.g[k] * MM
        d.qpos[rob.obj_q:rob.obj_q + 3] = [pick[0] * MM, pick[1] * MM, rob.R * MM]
        d.qpos[rob.obj_q + 3:rob.obj_q + 7] = oq
        mj.mj_inverse(m, d)
        tau = np.clip(d.qfrc_inverse[rob.dadr], -rob.eff, rob.eff)
        q = [round(float(v), 4) for v in Q[k]] + ([round(plan.g[k], 2)] if rob.g_q else [])
        frames.append({"t": round(float(t), 4), "q": q, "obj": [round(float(pick[0]), 2), round(float(pick[1]), 2),
                                                                  round(float(rob.R), 2)] + oq,
                       "tau": [round(float(v), 4) for v in tau], "grip_n": 0.0, "phase": plan.ph[k]})
    return frames


def _shake(rob: _Robot, design, fast=False):
    info = rob.info
    s = 0.85
    pick, _ = _targets(rob, design)
    ax = _tangent(pick[:2])
    hover = _hover_mm(rob, pick, ax)
    above = pick + [0, 0, hover]
    q_above, e1, _ = rob.ik_robust(above, ax)
    plan = _Plan(rob, q_above, info.grip_open_mm, "approach")
    plan.hold(0.2, "approach")
    _grasp_sequence(rob, plan, pick, s, hover)
    # shake pose: lifted 40 mm and pulled in so the oscillation stays inside the reach with margin
    hold_pt = above + [0, 0, 40.0]
    for pull in (0.0, 0.08, 0.16, 0.25, 0.35):
        cand = np.r_[above[:2] * (1 - pull), above[2] + 40.0]
        if _reachable(rob, cand + np.r_[ax[:2] * 25.0, 10.0], ax, plan.qe, margin=1.04) is not None and \
                _reachable(rob, cand - np.r_[ax[:2] * 25.0, 10.0], ax, plan.qe, margin=1.04) is not None:
            hold_pt = cand
            break
    plan.cart(lambda u: (_line(above, hold_pt)(u), ax), 0.6, "lift")
    plan.hold(0.2, "lift")
    t_shake = plan.T
    T = 2.0 if fast else 3.0
    A_ax, A_z = 22.0, 8.0
    f0, f1 = 1.5, 3.5
    # phase of a linear chirp, eased in/out over 0.35 s
    ts = np.arange(1, int(round(T / PLAN_DT)) + 1) * PLAN_DT
    ph = 2 * math.pi * (f0 * ts + 0.5 * (f1 - f0) / T * ts ** 2)
    env = np.minimum(1.0, np.minimum(ts / 0.35, (T - ts) / 0.35).clip(0, 1))
    env = _minjerk(env)
    offs = np.c_[A_ax * env * np.sin(ph), A_z * env * np.sin(2 * ph)]
    q = plan.qe
    qs = []
    for o in offs:
        p = hold_pt + ax * o[0] + [0, 0, o[1]]
        q, ep, _ = rob.ik(p, ax, q, iters=25)
        plan.ik_err_mm = max(plan.ik_err_mm, ep)
        qs.append(q)
    plan._push(qs, "shake", None)
    acc = np.gradient(np.gradient(offs, PLAN_DT, axis=0), PLAN_DT, axis=0)
    peak_acc = float(np.linalg.norm(acc, axis=1).max()) * MM
    t_end_shake = plan.T
    plan.mark("shake start", t_shake)
    plan.hold(0.3, "shake")
    plan.cart(lambda u: (_line(hold_pt, pick + [0, 0, 1.5])(u), ax), 1.2, "place")
    plan.hold(0.1, "release", grip=info.grip_open_mm)
    plan.hold((info.grip_open_mm + 2) / GRIP_SPEED_MM_S + 0.2, "release")
    plan.cart(lambda u: (_line(pick + [0, 0, 1.5], above)(u), ax), 0.8, "retreat")
    plan.hold(0.2, "retreat")

    st = {"rel0": None, "slip": 0.0, "rot": 0.0, "q0": None, "grip": []}

    def on_step(t, d, phs):
        if t_shake - 0.05 <= t <= t_end_shake + 0.3:
            rel = _rel_obj(rob, d)
            _, Rt = rob.tcp_pose(d)
            Ro = d.xmat[rob.obj].reshape(3, 3)
            Rrel = Rt.T @ Ro
            if st["rel0"] is None:
                st["rel0"], st["q0"] = rel, Rrel
            st["slip"] = max(st["slip"], float(np.linalg.norm(rel - st["rel0"])))
            c = (np.trace(st["q0"].T @ Rrel) - 1) / 2
            st["rot"] = max(st["rot"], math.degrees(math.acos(float(np.clip(c, -1, 1)))))
        return True

    run = _simulate(rob, plan, obj_xy=pick[:2], on_step=on_step)
    shake_grip = [f["grip_n"] for f in run.frames if f["phase"] == "shake"]
    dropped = st["slip"] > DROP_MM
    plan.mark(f"slip {st['slip']:.1f} mm at {peak_acc:.1f} m/s²", t_end_shake)
    metrics = {
        "success": bool(not dropped and st["slip"] < 5.0 and e1 < 2),
        "slip_mm": round(st["slip"], 2), "rot_slip_deg": round(st["rot"], 2), "dropped": bool(dropped),
        "peak_accel_ms2": round(peak_acc, 2), "freq_hz": [f0, f1], "amplitude_mm": [A_ax, A_z],
        "grip_target_n": round(info.grip_force_n, 2),
        "grip_n_min": round(float(min(shake_grip)), 2) if shake_grip else 0.0,
        "grip_n_max": round(float(max(shake_grip)), 2) if shake_grip else 0.0,
        "max_torque_pct": run.stats["max_torque_pct"], "max_speed_pct": run.stats["max_speed_pct"],
    }
    return plan, run, metrics


# ====================================================================== entry points

def _scenario_doc(rob, design, name, plan, run, metrics, points=None) -> dict:
    pick, place = _targets(rob, design)
    info = rob.info
    out = {
        "scenario": name, "title": TITLES[name], "dt": round(FRAME_DT, 4),
        "joints": list(info.arm_joints) + ([info.gripper_joint] if info.gripper_joint else []),
        "frames": run.frames, "markers": sorted(plan.markers, key=lambda mk: mk["t"]),
        "object": {k: info.object[k] for k in ("shape", "diameter_mm", "length_mm", "color", "mass_g", "axis")},
        "targets": {"pick": [round(float(v), 2) for v in pick], "place": [round(float(v), 2) for v in place]},
        "metrics": metrics,
        "info": {"timestep_s": rob.model.opt.timestep, "grip_force_n": round(info.grip_force_n, 3),
                 "efforts": {k: round(v, 3) for k, v in info.efforts.items()},
                 "collision_geoms": len(info.geoms), "object_excluded_parts": info.excluded,
                 "dropped_parts": info.dropped_parts[:40], "units": "mm, rad, N, N*m; obj quat w,x,y,z"},
    }
    if points is not None:
        out["points"] = points
    return out


def _run_one(rob, design, name, fast):
    if name == "pick_place":
        plan, run, metrics = _pick_place(rob, design, fast)
        return _scenario_doc(rob, design, name, plan, run, metrics)
    if name == "payload_sweep":
        plan, run, metrics = _payload_sweep(rob, design, fast)
        return _scenario_doc(rob, design, name, plan, run, metrics)
    if name == "workspace":
        plan, run, metrics, pts = _workspace(rob, design, fast)
        return _scenario_doc(rob, design, name, plan, run, metrics, pts)
    if name == "shake":
        plan, run, metrics = _shake(rob, design, fast)
        return _scenario_doc(rob, design, name, plan, run, metrics)
    raise ValueError(f"unknown scenario {name!r}; choose from {SCENARIOS}")


def _make_robot(asm, design, fast, opts):
    o = _opts(opts)
    if fast and "timestep" not in (opts or {}):
        o.timestep = 0.002
    return _Robot(asm, design, o)


def _write(outdir, name, doc):
    path = os.path.join(outdir, f"sim_{name}.json")
    with open(path, "w") as fh:
        json.dump(doc, fh, separators=(",", ":"))
    return path


def run(asm, design, scenario: str, outdir: str | None = None, fast: bool = False, **opts) -> dict:
    """Run one scenario; writes ``outdir/sim_<scenario>.json`` and ``outdir/robot.xml`` when outdir is given.
    Returns the scenario document (same content as the JSON)."""
    rob = _make_robot(asm, design, fast, opts)
    t0 = time.time()
    doc = _run_one(rob, design, scenario, fast)
    doc["info"]["wall_s"] = round(time.time() - t0, 2)
    if outdir:
        os.makedirs(outdir, exist_ok=True)
        with open(os.path.join(outdir, "robot.xml"), "w") as fh:
            fh.write(rob.xml)
        _write(outdir, scenario, doc)
    return doc


def run_all(asm, design, outdir: str, scenarios=None, fast: bool = False, **opts) -> dict:
    """Run the scenarios (default: all four), write sim_<name>.json + robot.xml into outdir."""
    os.makedirs(outdir, exist_ok=True)
    rob = _make_robot(asm, design, fast, opts)
    with open(os.path.join(outdir, "robot.xml"), "w") as fh:
        fh.write(rob.xml)
    out = {"scenarios": {}, "mjcf": "robot.xml"}
    for name in scenarios or SCENARIOS:
        t0 = time.time()
        try:
            doc = _run_one(rob, design, name, fast)
        except Exception as exc:  # noqa: BLE001  keep the other scenarios alive; surface the error
            out["scenarios"][name] = {"file": None, "title": TITLES.get(name, name),
                                      "metrics": {"success": False, "error": f"{exc.__class__.__name__}: {exc}"}}
            continue
        doc["info"]["wall_s"] = round(time.time() - t0, 2)
        _write(outdir, name, doc)
        out["scenarios"][name] = {"file": f"sim_{name}.json", "title": doc["title"], "metrics": doc["metrics"]}
    return out


# ====================================================================== rendering (for checks and stills)

def render_frames(asm, design, sim: dict | str, times, path_pattern: str, size=(640, 480), camera=None,
                  fast=False) -> list:
    """Render scenario frames to PNG with mujoco.Renderer (set MUJOCO_GL=egl or osmesa when headless).
    ``sim`` is a scenario document or the path of a sim_<name>.json; ``path_pattern`` like 'out_{i}.png'."""
    import mujoco
    from PIL import Image

    if isinstance(sim, str):
        with open(sim) as fh:
            sim = json.load(fh)
    rob = _make_robot(asm, design, fast, {})
    m, d = rob.model, mujoco.MjData(rob.model)
    r = mujoco.Renderer(m, size[1], size[0])
    cam = mujoco.MjvCamera()
    cam.lookat[:] = camera.get("lookat", [0.12, 0.0, 0.12]) if camera else [0.12, 0.0, 0.12]
    cam.distance = camera.get("distance", 0.75) if camera else 0.75
    cam.azimuth = camera.get("azimuth", 215) if camera else 215
    cam.elevation = camera.get("elevation", -22) if camera else -22
    frames = sim["frames"]
    tt = np.array([f["t"] for f in frames])
    outs = []
    for i, t in enumerate(times):
        f = frames[int(np.argmin(np.abs(tt - t)))]
        d.qpos[:] = 0
        d.qpos[rob.qadr] = f["q"][:rob.n]
        for a in rob.g_q:
            d.qpos[a] = f["q"][-1] * MM
        d.qpos[rob.obj_q:rob.obj_q + 3] = np.asarray(f["obj"][:3]) * MM
        d.qpos[rob.obj_q + 3:rob.obj_q + 7] = f["obj"][3:]
        mujoco.mj_forward(m, d)
        r.update_scene(d, cam)
        path = path_pattern.format(i=i, t=f["t"])
        Image.fromarray(r.render()).save(path)
        outs.append(path)
    r.close()
    return outs
