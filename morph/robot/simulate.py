"""Whole-robot MuJoCo simulation: pick-and-place, payload sweep, workspace, shake.

The model is generated from the Assembly alone (kinematic contract in
docs/internal/PLATFORM_CONTRACTS.md: links / joints / parts / meta, ``Part.profile`` for
pads), so it follows whatever the arm generator builds:

* one body per link, placed by the joint origins (mm -> m); revolute -> hinge,
  prismatic -> slide. The gripper's mimic jaw (``gripper_right``) is coupled to
  the leading jaw with a joint equality (the rack and pinion); decorative
  mimics (the pinion) are welded at the contact pose.
* inertials from ``asm.link_mass`` (mass + COM) with an inertia tensor summed
  from the part bounding boxes (parallel-axis about the link COM).
* collision geometry, convex pieces per significant part (fasteners, cables,
  visual and tiny parts skipped, the biggest few kept per link):
    - profiled jaw parts (finger, pad) are cut into thin convex strips of
      ``Part.profile`` / ``profile_frame``, merged while the hull stays within
      0.12 mm of the outline, so a contour pad cradles the object (the idea of
      morph/sim.py's ``_pad_pieces``);
    - other parts near the object (hand, jaws, wrist) are sliced into slabs
      across the approach axis, each slab hulled (a generic convex
      decomposition for meshes without a profile);
    - everything else is one convex hull.
* contact filtering by bit groups: the arm never collides with itself, the
  base/turret only meet the object, and hand pieces that sit within 1 mm of the
  object at the grasp pose (a palm block, a gear) are kept off it.
* one position servo per arm joint, force-limited to the joint effort (stall
  torque) with kp = effort / 0.03 rad, reflected-rotor armature and a velocity
  gain for a 0.9 damping ratio; the jaw servo is force limited to the firmware
  grip target (the FSR loop on the real gripper) and closes at servo speed.
* the design object (capsule for a cylinder lying on its side, sphere, box;
  mass, friction and a little rolling resistance from the design) on a table at
  ``design.task`` pick xy, long axis tangential to the base so any dof can grasp it.

Grippers: the contract gripper is prismatic (value = mm opened beyond contact,
the object centred on the tcp). Hinged jaws (an imported robot such as the
SO-101) are handled too: the object's seat between the moving and fixed jaw is
found geometrically and marked with a ``sim_grasp`` site, and the gripper value
stays in radians.

Control: a host controller streams joint set-points from a damped-least-squares
IK (MuJoCo Jacobians at the grasp site, top-down approach, finger axis aligned
with the object for dof >= 5) and adds gravity and velocity feed-forward plus a
slow bounded integral, the way a smart-servo arm is driven from a PC. Torques
reported per frame are the servo outputs (N*m).

Scenarios (each writes ``sim_<name>.json``, frames at 30 Hz, schema in the contract):
  pick_place     approach, descend, force-limited grasp, lift, arc move, place, release, retreat;
                 phase durations scaled to design.task.cycle_time_s
  payload_sweep  carry the object to a far pose, ramp extra mass at the grasp point until a servo saturates
                 (phases move / hold / overload / recover; frames carry ``payload_g``)
  workspace      Monte-Carlo joint sampling -> reachable TCP cloud (``points``), reach, envelope, top-down
                 reach; a kinematic tour for the viewer (phase ``sample``, torques by inverse dynamics)
  shake          grasp, lift, chirp 1.5 -> 3.5 Hz along the object's axis (~1 g), measure in-hand slip
                 (phase ``shake``)
Frame ``obj`` quaternions put the object's long axis on its local z (how the Studio viewer draws it).

Public API:
  build_mjcf(asm, design, options=None) -> str
  run(asm, design, scenario, outdir=None, fast=False, **options) -> dict   (the scenario document)
  run_all(asm, design, outdir, scenarios=None, fast=False, **options)
      -> {"scenarios": {name: {"file", "title", "metrics"}}, "mjcf": "robot.xml"}
  render_frames(asm, design, sim, times, path_pattern, ...) -> [png paths]   (MUJOCO_GL=egl/osmesa when headless)
  render_gif(asm, design, sim, path, fps=15, ...) -> path
``options`` are SimOptions fields (timestep, grip_force_n, object_xy, object_yaw, ...). ``fast`` uses a 2 ms
step, fewer workspace samples and shorter tails.

World mode (``world=``, morph.studio.worlds.sim_world: boxes in the robot base frame, mm): the mount surface replaces
the fixed table, obstacle boxes touch the robot and the object (never the table), and every move of pick_place is
checked against them every 10 ms (each plan sample, the object with the hand while it is carried). A move that comes
within 10 mm tries the alternatives in turn and takes the first that keeps 10 mm, else the one with the most room: the
carry arc from a family (pulled in, lifted mid-way); the approach straight, along the carry arc or from above (up,
across and down, the empty hand free to tilt); the release with the jaws opened less; the way back to rest straight,
with the hand closed, along the carry arc or straight up first. pick_place reports world_contacts (with the move),
min_clearance_mm (and where), clearance_ok, the arc, those detours and each move's clearance (path); any contact fails
it, and so does a planned move that still passes into a box (blocked), naming the move and the box. The workspace
drops samples that hit the room (usable_workspace_fraction); the payload sweep, a capacity test, ignores the room.
Without a world nothing changes.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import time
from dataclasses import dataclass, field

import numpy as np
from shapely.geometry import box as sbox

from ..electronics import SERVO_BAND_MM

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
B_WORLD = 64                                  # world mode: room obstacle boxes
GROUPS = {
    "table": (B_TABLE, B_OBJ),
    "object": (B_OBJ, B_TABLE | B_ARM | B_JAW | B_PROX),
    "arm": (B_ARM, B_TABLE | B_OBJ),          # moving links: table + object, never each other
    "jaw": (B_JAW, B_TABLE | B_OBJ),          # fingers / pads
    "prox": (B_PROX, B_OBJ),                  # base + turret: only the object
    "noobj": (B_NOOBJ, B_TABLE),              # hand parts that overlap the object at the grasp pose
    "world": (B_WORLD, B_OBJ | B_ARM | B_JAW | B_PROX | B_NOOBJ),   # the robot and the object, never the table or each other
}
CLEAR_MM = 10.0                    # world mode: the carry arc keeps this far from every obstacle box
CARRIED = ("lift", "move", "place")          # plan phases with the object in the hand
LIFTS_MM = (40.0, 80.0, 120.0)               # world mode: how much higher a detour may take the empty hand
OPEN_LESS_MM = 4.0                           # world mode: a release that opens the jaws only this far past the object
MOVES = {"approach": "the approach", "descent": "the descent to the object", "grasp": "the grasp", "lift": "the lift",
         "carry": "the carry", "set-down": "the set-down", "release": "the release",
         "rise": "the rise from the place spot", "return": "the way back to rest"}
SKIP_KINDS = ("fastener", "cable", "visual")

# servo model
STALL_ERR_RAD = 0.03               # position error at which a servo reaches stall torque (kp = effort / this)
ZETA = 0.9                         # damping ratio of each servo loop
KI = 6.0                           # host-side integral gain (1/s), bounded to half the stall torque
GRIP_SPEED_MM_S = 45.0             # jaw closing / opening speed (pinion at servo speed)
GRIP_SLOW_MM = 3.0                 # the firmware slows the jaws to 1/4 speed this close to the expected contact
GRIP_SAT_MM = SERVO_BAND_MM        # jaw position error at which the jaw servo reaches its force limit (1.5 mm; the
                                   # firmware closes a hobby servo with no torque limit only part of it past contact)
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


def _cross(a, b):
    """np.cross of two 3-vectors without its per-call overhead: the same products and differences, bit for bit."""
    return np.array([a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]])


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
    world: dict | None = None               # world mode: {surface, obstacles, floor_z, ...} in the robot base frame (mm)


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
    grip_open: float                # gripper command values in the joint's own unit (mm or rad)
    grip_close: float
    obj_axis_tcp: np.ndarray        # object long axis, tcp frame
    obj_close_tcp: np.ndarray       # jaw closing direction, tcp frame
    tip_mm: float                   # fingertip extent beyond the grasp point along the approach axis
    object: dict
    grip_kind: str = "prismatic"    # prismatic (contract: value = mm beyond contact) | revolute (rad) | none
    grip_contact: float = 0.0       # gripper value at first contact with the object
    grasp_offset: np.ndarray = field(default_factory=lambda: np.zeros(3))  # object centre in the tcp frame, mm
    lever_mm: float = 1.0           # revolute jaw: hinge axis -> object centre
    grip_speed: float = 45.0        # jaw speed in the joint's unit per second
    grip_effort: float = 0.0        # actuator limit on the gripper joint (N for a jaw pair, N*m for a hinge)
    grip_force_max: float = 0.0     # per-finger force at the gripper actuator's full effort (N)
    grip_open_loop: bool = False    # a hobby servo held grip_close past contact: its position loop sets the squeeze
    excluded: list = field(default_factory=list)
    dropped_parts: list = field(default_factory=list)
    geoms: list = field(default_factory=list)
    kp: dict = field(default_factory=dict)
    kv: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)


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
    g = max(-shrink, 0.0)
    xs = np.arange(-R - g, R + g + 1e-9, spacing)
    ys = np.arange(-L / 2 - g, L / 2 + g + 1e-9, spacing)
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


def _object_surface(design, spacing=1.0, offset=0.0) -> np.ndarray:
    """Points on the object's surface pushed out by ``offset`` mm (object frame: x closing, y long axis, z
    approach). Exact contact tests need surface samples; an interior grid misses up to its spacing."""
    shape, R, L = _object_dims(design)
    r = R + offset
    if shape == "box":
        hx, hy = r, L / 2.0 + offset
        out = []
        for ax, (a, b, c) in enumerate(((hx, hy, r), (hy, hx, r), (r, hx, hy))):
            u = np.arange(-b, b + 1e-9, spacing)
            v = np.arange(-c, c + 1e-9, spacing)
            U, V = np.meshgrid(u, v, indexing="ij")
            for sgn in (-1.0, 1.0):
                P = np.zeros((U.size, 3))
                P[:, ax] = sgn * a
                P[:, (ax + 1) % 3], P[:, (ax + 2) % 3] = U.ravel(), V.ravel()
                out.append(P)
        return np.vstack(out)
    half = max(L / 2.0 - R, 0.0) if shape == "cylinder" else 0.0
    n_t = max(int(2 * math.pi * r / spacing), 12)
    th = np.linspace(0, 2 * math.pi, n_t, endpoint=False)
    out = []
    ys = np.arange(-half, half + 1e-9, spacing) if half > 0 else np.array([])
    for y in ys:                                                    # cylinder band (axis = y)
        out.append(np.c_[r * np.cos(th), np.full(n_t, y), r * np.sin(th)])
    n_p = max(int(math.pi * r / spacing), 6)
    for k in range(n_p + 1):                                        # end caps / sphere
        ph = math.pi * k / n_p
        ring = r * math.sin(ph)
        m = max(int(2 * math.pi * ring / spacing), 1)
        t = np.linspace(0, 2 * math.pi, m, endpoint=False)
        y = r * math.cos(ph)
        y = y + half if y >= 0 else y - half
        out.append(np.c_[ring * np.cos(t), np.full(m, y), ring * np.sin(t)])
    return np.vstack(out)


def _grip_force_target(asm, design, override=None, generated_fingers=True, cap=None) -> float:
    """Per-finger clamp force the firmware regulates to (N): the one it ships (asm.meta["grip_target_n"], which the robot
    builders copy from the electronics plan, itself the gripper stage's force plan), else the Grip force target, else
    that force plan's closed form (morph.electronics.force_window). A sensorless hobby servo regulates nothing: its
    firmware closes it a set distance past the object (asm.meta["grip_squeeze_mm"]), where its own position loop squeezes
    about asm.meta["grip_squeeze_n"] (_describe holds it there with the servo's stall force as its limit, so the
    simulation squeezes what that travel gives). ``generated_fingers`` = Morph's own
    pads (contour / V wedge gain and the FSR range apply); False for a stock gripper (flat jaws). ``cap``: the jaws'
    own limit (the gripper joint's effort), else design.gripper.force_n."""
    o, f = design.object, design.finger
    cap = max(cap if cap else design.gripper.force_n, 0.5)
    for key in ("grip_squeeze_n", "grip_target_n", "grip_force_n"):
        if asm.meta.get(key):
            override = override or asm.meta[key]
    if override:
        return float(min(override, cap))
    if design.electronics.target_force_n > 0:
        return float(min(design.electronics.target_force_n, cap))
    gain = 1.0
    if generated_fingers:
        try:
            from ..analysis import wedge_gain
            from ..design import build_profile

            gain = wedge_gain(f.pad, build_profile(design).contact_half_angle_deg)
        except Exception:  # noqa: BLE001
            gain = 1.0
    from ..electronics import force_window, static_hold

    hold = static_hold(o, gain)
    target = force_window(o, hold, "fsr402" if f.sensor == "fsr402" and generated_fingers else None, floor=0.5)[2]
    return float(min(target, cap))


def grasp_lever(asm, design) -> float:
    """A hinged jaw's lever at the grasp the simulation makes (mm, hinge axis to the object's centre): where the object
    sits against the fixed jaw, inside the fingers, reachable straight down with the jaw open (_describe). The
    firmware's torque limit is set at this lever (imported.gripper_summary), so the simulated squeeze is the target.
    1.0 for a parallel jaw (no hinge)."""
    return float(_describe(asm, design, _opts(None), grasp_only=True)[0].lever_mm)


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


def _profile_pieces(p, opts: SimOptions) -> list:
    """Convex strips of a flat/profiled part (link frame, mm): pads finely, other jaw plates coarsely."""
    F = np.asarray(p.profile_frame, float)
    local = (np.linalg.inv(F) @ np.c_[np.asarray(p.mesh.vertices), np.ones(len(p.mesh.vertices))].T).T[:, :3]
    w0, w1 = float(local[:, 2].min()), float(local[:, 2].max())
    if w1 - w0 < 0.2:
        w0, w1 = 0.0, max(p.thickness, 1.0)
    fine = p.kind == "pad"
    pieces = _convex_pieces(p.profile, opts.pad_strip_mm if fine else 2.0 * opts.pad_strip_mm, 0.12 if fine else 0.6)
    out = []
    for pts in pieces:
        uvw = np.vstack([np.c_[pts, np.full(len(pts), w0)], np.c_[pts, np.full(len(pts), w1)]])
        out.append((F @ np.c_[uvw, np.ones(len(uvw))].T).T[:, :3])
    return out


def _slabs(mesh, axis, step: float, max_slabs: int = 14) -> list:
    """Cut a mesh into slabs across ``axis`` and return each slab's points (vertices inside + edge crossings),
    whose hulls approximate a concave part (a jaw with its housing, a palm with a slot) far better than one hull."""
    V = np.asarray(mesh.vertices, float)
    F = np.asarray(mesh.faces, int)
    a = np.asarray(axis, float) / max(np.linalg.norm(axis), 1e-9)
    sv = V @ a
    lo, hi = float(sv.min()), float(sv.max())
    n = int(np.clip(math.ceil((hi - lo) / step), 1, max_slabs))
    if n == 1 or len(F) == 0:
        return [V]
    cuts = np.linspace(lo, hi, n + 1)
    E = np.unique(np.sort(np.vstack([F[:, [0, 1]], F[:, [1, 2]], F[:, [2, 0]]]), axis=1), axis=0)
    si, sj = sv[E[:, 0]], sv[E[:, 1]]
    X = {}
    for k in range(1, n):
        c = cuts[k]
        msk = (si - c) * (sj - c) < 0
        t = (c - si[msk]) / (sj[msk] - si[msk])
        X[k] = V[E[msk, 0]] + t[:, None] * (V[E[msk, 1]] - V[E[msk, 0]])
    out = []
    for k in range(n):
        pts = [V[(sv >= cuts[k] - 1e-9) & (sv <= cuts[k + 1] + 1e-9)]]
        if k > 0:
            pts.append(X[k])
        if k < n - 1:
            pts.append(X[k + 1])
        P = np.vstack(pts)
        if len(P) >= 4:
            out.append(P)
    return out


@dataclass
class _Cand:
    link: str
    part: object
    k: int
    hv: np.ndarray            # hull vertices, link frame, mm
    hf: np.ndarray
    n: np.ndarray = None      # hull half-spaces (link frame): inside <=> n.x - d <= 0
    d: np.ndarray = None
    vol: float = 0.0
    group: str = "arm"


def _overlap(cands, T_tcp_link: dict, pts_tcp: np.ndarray, margin: float = 0.3) -> int:
    """Number of object sample points (tcp frame) inside any of the candidate hulls."""
    total = 0
    for c in cands:
        M = T_tcp_link[c.link]
        P = (pts_tcp - M[:3, 3]) @ M[:3, :3]                  # tcp -> link frame
        lo, hi = c.hv.min(0) - margin, c.hv.max(0) + margin
        box = np.all((P >= lo) & (P <= hi), axis=1)
        if not box.any():
            continue
        Pi = P[box]
        total += int(np.all(Pi @ c.n.T - c.d <= -margin, axis=1).sum())
    return total


def _contact_value(jaw_c, tl, gname, pts, g_open, g_closed, iters=14) -> float:
    """Gripper value where the jaws first touch the object (sample points in the tcp frame), closing from
    g_open towards g_closed; bisection on the (monotonic) overlap. Returns g_closed if they never touch."""
    if _overlap(jaw_c, tl({gname: g_closed}), pts, 0.0) == 0:
        return float(g_closed)
    if _overlap(jaw_c, tl({gname: g_open}), pts, 0.0) > 0:
        return float(g_open)
    a, b = g_open, g_closed
    for _ in range(iters):
        mid = 0.5 * (a + b)
        if _overlap(jaw_c, tl({gname: mid}), pts, 0.0) > 0:
            b = mid
        else:
            a = mid
    return float(a)


def _describe(asm, design, opts: SimOptions, grasp_only: bool = False):
    """Collision geoms, contact groups, joints and the gripper's grasp geometry for the MJCF. grasp_only: only the
    hand's and the jaws' pieces (all the grasp geometry reads), for grasp_lever; the result can't build a model."""
    meta = asm.meta or {}
    jnames = {j.name for j in asm.joints}
    gname = meta.get("gripper_joint", "gripper")
    gj = asm.joint(gname) if gname in jnames else None
    arm_joints = [n for n in meta.get("arm_joints", []) if n in jnames] or \
        [j.name for j in asm.joints if j.type == "revolute" and not j.mimic and j.name != gname]
    tcp_link = meta.get("tcp_link", "tcp") if meta.get("tcp_link", "tcp") in asm.links else asm.ordered_links()[-1]
    kind = "none" if gj is None else ("prismatic" if gj.type == "prismatic" else "revolute")
    jaw_joints = ([gj] if gj else []) + [j for j in asm.joints if j.mimic and gj and j.mimic.get("joint") == gname
                                         and j.type == gj.type and asm.parent_joint(j.child).parent == gj.parent]
    jaw_links = set()
    for j in jaw_joints:
        jaw_links |= _subtree(asm, j.child)
    hand_root = gj.parent if gj else asm.parent_joint(tcp_link).parent
    hand_links = _subtree(asm, hand_root) - jaw_links
    wrist_links = _wrist_links(asm, arm_joints, hand_root)
    near_links = hand_links | jaw_links | wrist_links | {tcp_link}
    first = asm.joint(arm_joints[0]).child if arm_joints else asm.root
    prox_links = {asm.root, first}

    ready = {}
    for key in ("ready", "home"):
        if isinstance(meta.get(key), dict) and not ready:
            ready = dict(meta[key])
    lower = {j.name: float(j.lower) for j in asm.joints}
    upper = {j.name: float(j.upper) for j in asm.joints}
    efforts = {n: _effort(asm, asm.joint(n)) for n in arm_joints}
    velocity = {n: _velocity(asm.joint(n)) for n in arm_joints}
    shape, R, L = _object_dims(design)
    notes = []

    # ---- reference pose: ready, gripper fully open
    g_hi = upper.get(gname, 0.0)
    q_ref = {**ready, **({gname: g_hi} if gj else {})}
    T = asm.world_transforms(q_ref)
    Ttcp_inv = np.linalg.inv(T[tcp_link])
    approach_link = {l: (np.linalg.inv(T[l])[:3, :3] @ T[tcp_link][:3, 2]) for l in near_links if l in T}

    # ---- candidate convex pieces per part
    masses = {p.name: _part_mass(asm, p) for p in asm.parts}
    reach = R + L / 2.0 + 60.0
    by_link = {}
    for p in asm.parts:
        if p.collide and p.kind not in SKIP_KINDS:
            by_link.setdefault(p.link, []).append(p)
    cands, dropped = [], []
    for link, parts in by_link.items():
        jaw = link in jaw_links
        near = link in near_links
        if grasp_only and not near:
            continue
        group = "jaw" if jaw else ("prox" if link in prox_links else "arm")
        cl = []
        for p in parts:
            if jaw and p.profile is not None and p.profile_frame is not None:
                sets = _profile_pieces(p, opts)
            elif near:
                Tl = Ttcp_inv @ T[link]
                b = p.mesh.bounds
                ctr = Tl[:3, :3] @ b.mean(0) + Tl[:3, 3]
                close = np.linalg.norm(ctr) - np.linalg.norm(b[1] - b[0]) / 2 < reach
                sets = _slabs(p.mesh, approach_link[link], 2.5 if p.kind == "pad" else 5.0) if close else \
                    [np.asarray(p.mesh.vertices, float)]
            else:
                sets = [np.asarray(p.mesh.vertices, float)]
            for k, pts in enumerate(sets):
                h = _hull(pts)
                if h is not None and len(h[0]) > opts.hull_verts:
                    h = _hull(_reduce(h[0], opts.hull_verts))     # the same point set MuJoCo will hull
                if h is None:
                    continue
                vol = _hull_volume(*h)
                if vol < 20.0 or np.ptp(h[0], axis=0).max() < 3.0:
                    continue
                n_, d_ = _halfspaces(*h)
                cl.append(_Cand(link, p, k, h[0], h[1], n_, d_, vol, group))
        limit = 120 if (jaw or near) else (min(opts.max_geoms_per_link, 6) if link == asm.root else opts.max_geoms_per_link)
        # drop pieces already enclosed by a bigger hull of the same link (screws, labels, pins on a servo case)
        kept = []
        for c in sorted(cl, key=lambda c: -c.vol):
            lo_c, hi_c = c.hv.min(0), c.hv.max(0)
            if c.part.kind != "pad" and any(
                    k.part.kind != "pad" and np.all(lo_c >= k.hv.min(0) - 1.0) and np.all(hi_c <= k.hv.max(0) + 1.0)
                    and np.all(c.hv @ k.n.T - k.d <= 1.0) for k in kept):
                continue
            kept.append(c)
        cl = kept
        cl.sort(key=lambda c: (c.part.kind != "pad", -c.vol))
        if len(cl) > limit:
            dropped += sorted({c.part.name for c in cl[limit:]} - {c.part.name for c in cl[:limit]})
            cl = cl[:limit]
        cands += cl

    # ---- grasp geometry
    jaw_c = [c for c in cands if c.link in jaw_links]
    hand_c = [c for c in cands if c.link in hand_links or c.link in wrist_links or c.link == tcp_link]

    def tl(q):
        Tq = asm.world_transforms({**ready, **q})
        inv = np.linalg.inv(Tq[tcp_link])
        return {l: inv @ Tq[l] for l in Tq}

    z = np.array([0.0, 0.0, 1.0])
    if kind == "prismatic":
        ax_w = T[gj.child][:3, :3] @ np.asarray(gj.axis, float)
        close_tcp = Ttcp_inv[:3, :3] @ ax_w
    elif kind == "revolute":
        hinge = Ttcp_inv[:3, :3] @ T[gj.child][:3, :3] @ np.asarray(gj.axis, float)
        hinge = hinge - z * hinge @ z
        close_tcp = np.cross(hinge, z)
    else:
        close_tcp = np.array([1.0, 0.0, 0.0])
    close_tcp = close_tcp - z * close_tcp @ z
    close_tcp = close_tcp / max(np.linalg.norm(close_tcp), 1e-9)
    axis_tcp = np.cross(z, close_tcp)
    R_obj = np.c_[close_tcp, axis_tcp, z]
    pts_obj = _object_points(design, spacing=3.0, shrink=0.5)
    pts0 = pts_obj @ R_obj.T

    offset = np.zeros(3)
    contact = 0.0
    g_lo = lower.get(gname, 0.0)
    lever = 1.0
    if kind == "prismatic":
        # contract: value 0 = jaws touching the object centred on the tcp. Check it against the real pad
        # geometry (closing from open) so a generator that is off by a few mm still squeezes properly.
        pts_s = _object_surface(design, 1.0) @ R_obj.T
        contact = _contact_value(jaw_c, tl, gname, pts_s, g_hi, g_lo - 6.0)
        if abs(contact) > 1.0:
            notes.append(f"pads meet the object at gripper={contact:+.1f} mm (contract: 0)")
        if contact < g_lo + 0.5:
            notes.append("jaws are within 0.5 mm of their closed stop when they meet the object")
    elif kind == "revolute":
        # no contract for hinged jaws: find where the object sits (against the fixed jaw, inside the fingers,
        # reachable by a straight top-down descent with the jaw open)
        open_T = tl({gname: g_hi})
        if _overlap(jaw_c, tl({gname: g_lo}), pts0) == 0 and _overlap(jaw_c, open_T, pts0) > 0:
            g_hi, g_lo = g_lo, g_hi                                 # this jaw opens towards negative values
            open_T = tl({gname: g_hi})
        tips = [float(((open_T[c.link][:3, :3] @ c.hv.T).T + open_T[c.link][:3, 3])[:, 2].max()) for c in hand_c]
        tip_z = max(tips) if tips else 0.0
        descent = (2.0, 5.0, 10.0, 18.0, 28.0, 40.0, 55.0)

        pts_fat = _object_surface(design, 1.5, 1.0) @ R_obj.T              # 1 mm clearance all round

        def clear(c, Tj, hs=descent):
            """No hand overlap at the seat or anywhere on the way down to it; moving jaw (at Tj) clear too."""
            for h in (0.0,) + tuple(hs):
                pts = pts_fat + c + z * h
                if _overlap(hand_c, open_T, pts, 0.0) or _overlap(jaw_c, Tj, pts, 0.0):
                    return False
            return True

        best = None
        # start with the fingertips 3 mm short of the object's far side (they must clear the table), go deeper
        for cz in np.arange(tip_z - R + 3.0, tip_z - R - 40.0, -2.5):
            row = []
            for cx in np.arange(-45.0, 45.01, 2.5):
                c = close_tcp * cx + z * cz
                ok = _overlap(hand_c, open_T, pts0 + c, 0.5) == 0 and _overlap(jaw_c, open_T, pts0 + c, 0.5) == 0
                row.append((cx, c, ok))
            for i, (cx, c, ok) in enumerate(row):
                if not ok:
                    continue
                touches = any(not r[2] and _overlap(hand_c, open_T, pts0 + r[1], 0.5) > 0
                              for r in row[max(i - 1, 0):i + 2] if r is not row[i])
                if not touches or _overlap(jaw_c, tl({gname: g_lo}), pts0 + c) == 0:
                    continue                      # not seated against the fixed jaw, or the closing jaw misses it
                for back in (0.0, 1.0, 2.0, 3.0, 4.0):   # a little room from the fixed jaw for the descent
                    cc = c + close_tcp * back * (1 if row[min(i + 1, len(row) - 1)][2] else -1)
                    if clear(cc, open_T):
                        best = cc
                        break
                if best is not None:
                    break
            if best is not None:
                break
        if best is None:
            notes.append("no pinch position found between the jaws; grasping at the tcp")
            best = np.zeros(3)
        offset = best
        # contact value: close from open until the moving jaw meets the object
        contact = _contact_value(jaw_c, tl, gname, _object_surface(design, 1.0) @ R_obj.T + offset, g_hi, g_lo)
        gs = np.linspace(g_hi, g_lo, 60)
        # open just enough for the descent to clear the moving jaw (plus a little), not to the hard stop
        g_clear = g_hi
        for g in gs[::-1]:
            if (g - contact) * (g_hi - contact) > 0 and clear(offset, tl({gname: g})):
                g_clear = g
                break
        hinge_p = tl({gname: contact})[gj.child][:3, 3]
        hinge_a = Ttcp_inv[:3, :3] @ T[gj.child][:3, :3] @ np.asarray(gj.axis, float)
        rvec = offset - hinge_p
        lever = float(np.linalg.norm(rvec - hinge_a * (rvec @ hinge_a))) or 1.0

    # ---- object contact filtering at the grasp (gripper at contact, object at the grasp point). Hand pieces
    # within ~1 mm of the object there would fight the grasp (a palm block, a gear): a parallel gripper's body
    # parts are dropped whole, hinged jaws keep their other slabs. Jaw pieces are the contact surfaces, so
    # only those that cut > 1 mm into the object at contact are dropped.
    Tg = tl({gname: contact} if gj else {})
    ptsg = np.vstack([_object_surface(design, 1.5, 1.0), _object_points(design, 3.0, 0.0)]) @ R_obj.T + offset
    ptsj = np.vstack([_object_surface(design, 1.5, -1.0), _object_points(design, 3.0, 1.0)]) @ R_obj.T + offset
    excluded, drop = [], set()
    for c in cands:
        if c.link not in near_links:
            continue
        if c.link in jaw_links:
            if _overlap([c], Tg, ptsj, 0.0) > 0:
                drop.add(id(c))
        elif _overlap([c], Tg, ptsg, 0.0) > 0:
            drop.add(id(c))
    whole = {c.part.name for c in cands if id(c) in drop and kind == "prismatic" and c.link not in jaw_links}
    for c in cands:
        if id(c) in drop or c.part.name in whole:
            c.group = "jaw_noobj" if c.link in jaw_links else "noobj"
            excluded.append(c.part.name)
    tip = 0.0
    for c in jaw_c + hand_c:
        if c.group in ("noobj", "jaw_noobj"):
            continue
        M = Tg[c.link]
        tip = max(tip, float(((M[:3, :3] @ c.hv.T).T + M[:3, 3])[:, 2].max() - offset[2]))

    specs = [_GeomSpec(f"{c.part.name}__{c.k}", c.link, c.part.name, c.hv, c.group,
                       _rgba(c.part.color), c.part.kind, (c.hv, c.hf)) for c in cands]

    # ---- gripper command values and force limits
    gf = _grip_force_target(asm, design, opts.grip_force_n, generated_fingers=kind == "prismatic",
                            cap=(gj.effort if kind == "prismatic" and gj is not None and gj.effort else None))
    # a hobby servo without force sensors or a torque limit: its firmware closes it only grip_squeeze_mm past the
    # object, and its own position loop (full force GRIP_SAT_MM past contact, up to its stall) sets the squeeze
    open_mm = float((asm.meta or {}).get("grip_squeeze_mm") or 0.0) if opts.grip_force_n is None else 0.0
    stall_n = float((asm.meta or {}).get("grip_stall_n") or gf)
    if kind == "prismatic":
        g_open = float(min(g_hi - 0.5, max(ready.get(gname, 12.0), contact + 8.0)))
        g_close = float(contact - open_mm) if open_mm else float(min(g_lo, contact - 2.5 * GRIP_SAT_MM))
        speed = GRIP_SPEED_MM_S
        effort = (min(stall_n, gj.effort or stall_n) if open_mm else gf) * (2.0 if len(jaw_joints) > 1 else 1.0)
        fmax = float(gj.effort or design.gripper.force_n)
    elif kind == "revolute":
        sgn = 1.0 if g_hi > g_lo else -1.0
        g_open = float(np.clip(max(g_clear * sgn, (contact + 12.0 / lever * sgn) * sgn) * sgn + sgn * 4.0 / lever,
                               min(g_lo, g_hi), max(g_lo, g_hi)))
        g_close = float(g_lo)
        if (contact - g_close) * sgn < 2.5 * GRIP_SAT_MM / lever:
            g_close = contact - sgn * 2.5 * GRIP_SAT_MM / lever
        if open_mm:
            g_close = contact - sgn * open_mm / lever
        speed = GRIP_SPEED_MM_S / lever
        cap = (gj.effort or 1.0) / (lever * MM)
        gf = min(gf, cap)
        effort = (min(stall_n, cap) if open_mm else gf) * lever * MM
        if (asm.meta or {}).get("grip_torque_nm") and opts.grip_force_n is None:
            effort = min(float(asm.meta["grip_torque_nm"]), gj.effort or 1.0)   # the firmware's torque limit, as set
        fmax = cap
    else:
        g_open = g_close = 0.0
        speed, effort, fmax = 1.0, 0.0, 0.0
    info = _Info(arm_joints=arm_joints, gripper_joint=gname if gj else "", jaw_joints=[j.name for j in jaw_joints],
                 tcp_link=tcp_link, jaw_links=jaw_links, hand_links=hand_links, efforts=efforts, lower=lower,
                 upper=upper, velocity=velocity, ready={k: float(v) for k, v in ready.items()},
                 grip_force_n=gf, grip_open=g_open, grip_close=g_close, obj_axis_tcp=axis_tcp,
                 obj_close_tcp=close_tcp, tip_mm=tip,
                 object={"shape": "egg" if "egg" in (design.object.name or "").lower() else shape,
                         "sim_shape": {"cylinder": "capsule"}.get(shape, shape), "name": design.object.name,
                         "diameter_mm": design.object.diameter_mm, "length_mm": L,
                         "mass_g": design.object.mass_g, "friction": design.object.friction,
                         "color": _object_color(design), "axis": "z"},
                 grip_kind=kind, grip_contact=float(contact), grasp_offset=offset, lever_mm=lever, grip_speed=speed,
                 grip_effort=effort, grip_force_max=fmax, grip_open_loop=bool(open_mm) and kind in ("prismatic", "revolute"),
                 excluded=sorted(set(excluded)), dropped_parts=dropped, geoms=specs, notes=notes)
    return info, masses


def _wrist_links(asm, arm_joints, hand_root) -> set:
    """Links between the last pitch joint and the hand (wrist housings that sit just above the object)."""
    out = set()
    pitch = [n for n in arm_joints if "pitch" in n or "flex" in n]
    if not pitch:
        return out
    start = asm.joint(pitch[-1]).child
    for j in asm.chain(hand_root):
        if j.child in _subtree(asm, start):
            out.add(j.child)
    return out - {hand_root}


# ====================================================================== MJCF

def _xml(asm, design, opts: SimOptions, info: _Info, masses, kv=None) -> str:
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
    # value at which every other joint is frozen (mimics follow their leader at the contact value)
    frozen = asm.joint_values({**info.ready, **({gname: info.grip_contact} if gname else {})})

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
            out.append(f'{pad}<site name="sim_grasp" pos="{_fmt(info.grasp_offset * MM)}" size="0.003" '
                       f'rgba="0.2 0.8 0.3 0.6" group="4"/>')
            out.append(f'{pad}<body name="sim_payload" pos="{_fmt(info.grasp_offset * MM)}"><inertial pos="0 0 0" '
                       f'mass="1e-6" diaginertia="1e-10 1e-10 1e-10"/></body>')
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
                    e = info.efforts.get(j.name, _effort(asm, j))
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
        F2 = info.grip_effort       # jaw pair: each finger presses F2 / 2; hinge: torque = force x lever
        if info.grip_kind == "prismatic":
            kpg = F2 / (GRIP_SAT_MM * MM)
            kvg = 2.0 * ZETA * math.sqrt(kpg * len(info.jaw_joints) * (JAW_ARMATURE_KG + 0.02))
            cr = f"{(min(j.lower, info.grip_close) - 2) * MM:.6g} {(j.upper + 2) * MM:.6g}"
        else:
            kpg = F2 / (GRIP_SAT_MM / max(info.lever_mm, 1.0))
            arm_ = float(np.clip(0.006 * _effort(asm, j), 0.002, 0.05))
            kvg = 2.0 * ZETA * math.sqrt(kpg * arm_ * 2)
            cr = f"{min(j.lower, info.grip_close) - 0.2:.6g} {max(j.upper, info.grip_close) + 0.2:.6g}"
        info.kp[gname], info.kv[gname] = kpg, kvg
        acts.append(f'    <position name="act_{gname}" joint="{gname}" kp="{kpg:.6g}" kv="{kvg:.6g}" forcelimited="true" '
                    f'forcerange="{-F2:.6g} {F2:.6g}" ctrlrange="{cr}"/>')
        for n in info.jaw_joints[1:]:
            jm = asm.joint(n)
            k = float(jm.mimic.get("multiplier", 1.0))
            c0 = float(jm.mimic.get("offset", 0.0)) * (MM if jm.type == "prismatic" else 1.0)
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
    ocontact = contact.replace('condim="4"', 'condim="6"')  # no backslashes inside f-strings: Python 3.11
    oz = R + 0.2
    if opts.world is not None:
        oz += _surface_top(opts.world)
    obj = (f'    <body name="sim_object" pos="{_fmt([x0 * MM, y0 * MM, oz * MM])}" quat="{_fmt(oq)}">\n'
           f'      <freejoint name="sim_object"/>\n'
           f'      <geom name="sim_object" {og} mass="{design.object.mass_g / 1000.0:.6g}" friction="{mu:.3f} 0.005 0.001" '
           f'{ocontact} contype="{ct}" conaffinity="{ca}" rgba="{ocol}"/>\n'
           f'    </body>')
    px, py = design.task.place_x_mm, design.task.place_y_mm
    tct, tca = GROUPS["table"]
    table = (f'    <geom name="sim_table" type="box" pos="0.1 0 -0.012" size="0.55 0.6 0.012" contype="{tct}" '
             f'conaffinity="{tca}" friction="0.6 0.005 0.0001" material="table"/>\n'
             f'    <site name="sim_place" pos="{_fmt([px * MM, py * MM, 0.0003])}" type="cylinder" '
             f'size="{(R + 4) * MM:.4g} 0.0003" rgba="0.2 0.75 0.45 0.55"/>\n'
             f'    <site name="sim_pick" pos="{_fmt([x0 * MM, y0 * MM, 0.0003])}" type="cylinder" '
             f'size="{(R + 4) * MM:.4g} 0.0003" rgba="0.95 0.6 0.2 0.35"/>') if opts.table else ""
    if opts.world is not None:
        table, mat = _world_xml(opts.world, R, (x0, y0), (px, py), opts.table)
        if mat:
            assets.insert(0, mat)

    name = (asm.name or "robot").replace('"', "")
    return f"""<mujoco model="{name}">
  <!-- generated by morph.robot.simulate from the robot assembly; units m, kg, s, rad -->
  <compiler angle="radian" autolimits="true" boundmass="1e-5" boundinertia="1e-10"/>
  <option timestep="{dt}" integrator="implicitfast" cone="elliptic" impratio="10"/>
  <size memory="64M"/>
  <visual>
    <global offwidth="1280" offheight="960" azimuth="150" elevation="-25"/>
    <quality shadowsize="4096" offsamples="8"/>
    <headlight ambient="0.22 0.22 0.24" diffuse="0.32 0.32 0.32" specular="0.05 0.05 0.05"/>
    <map znear="0.01"/>
  </visual>
  <asset>
    <texture name="sky" type="skybox" builtin="gradient" rgb1="0.82 0.85 0.9" rgb2="0.42 0.47 0.55" width="256" height="256"/>
    <texture name="grid" type="2d" builtin="checker" rgb1="0.56 0.58 0.62" rgb2="0.52 0.54 0.58" width="512" height="512" mark="edge" markrgb="0.46 0.48 0.52"/>
    <material name="table" texture="grid" texrepeat="11 12" reflectance="0.03"/>
{chr(10).join(assets)}
  </asset>
  <worldbody>
    <light name="key" pos="0.7 -0.5 1.1" dir="-0.5 0.4 -0.8" diffuse="0.62 0.6 0.57" specular="0.2 0.2 0.2" castshadow="true"/>
    <light name="fill" pos="-0.6 0.5 0.9" dir="0.5 -0.4 -0.8" diffuse="0.22 0.23 0.26" castshadow="false"/>
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


def _surface_top(world: dict) -> float:
    """Height (mm, robot base frame) of the world's mount surface top; 0 when it has none."""
    s = (world or {}).get("surface")
    return float(s["center"][2] + s["half"][2]) if s else 0.0


def _yaw_quat(deg: float) -> list:
    a = math.radians(float(deg or 0.0))
    return [math.cos(a / 2), 0.0, 0.0, math.sin(a / 2)]


def _world_xml(world: dict, R: float, pick, place, table_on: bool) -> tuple:
    """World mode: the mount surface as the table, a floor below it, the pick/place marks on the surface and the
    obstacle boxes (world_<i>, touching the robot and the object only). Returns (worldbody xml, material xml)."""
    s = world.get("surface")
    top = _surface_top(world)
    tct, tca = GROUPS["table"]
    wct, wca = GROUPS["world"]
    out, mat = [], ""
    if table_on:
        if s:
            rep = [max(2.0 * h / 100.0, 1.0) for h in s["half"][:2]]
            mat = f'    <material name="table_world" texture="grid" texrepeat="{_fmt(rep, 4)}" reflectance="0.03"/>'
            out.append(f'    <geom name="sim_table" type="box" pos="{_fmt(np.asarray(s["center"], float) * MM)}" '
                       f'size="{_fmt(np.asarray(s["half"], float) * MM)}" quat="{_fmt(_yaw_quat(s.get("yaw_deg")))}" '
                       f'contype="{tct}" conaffinity="{tca}" friction="0.6 0.005 0.0001" material="table_world"/>')
        else:
            out.append(f'    <geom name="sim_table" type="box" pos="0.1 0 {(top - 12) * MM:.6g}" size="0.55 0.6 0.012" '
                       f'contype="{tct}" conaffinity="{tca}" friction="0.6 0.005 0.0001" material="table"/>')
        fz = world.get("floor_z")
        if isinstance(fz, (int, float)) and fz < top - 30.0:
            out.append(f'    <geom name="sim_floor" type="plane" pos="0 0 {fz * MM:.6g}" size="0 0 0.05" contype="{tct}" '
                       f'conaffinity="{tca}" rgba="0.5 0.52 0.55 1"/>')
        for nm, (x, y), rgba in (("sim_place", place, "0.2 0.75 0.45 0.55"), ("sim_pick", pick, "0.95 0.6 0.2 0.35")):
            out.append(f'    <site name="{nm}" pos="{_fmt([x * MM, y * MM, (top + 0.3) * MM])}" type="cylinder" '
                       f'size="{(R + 4) * MM:.4g} 0.0003" rgba="{rgba}"/>')
    for i, o in enumerate(world.get("obstacles") or []):
        out.append(f'    <geom name="world_{i}" type="box" pos="{_fmt(np.asarray(o["center"], float) * MM)}" '
                   f'size="{_fmt(np.maximum(np.asarray(o["half"], float), 0.5) * MM)}" quat="{_fmt(_yaw_quat(o.get("yaw_deg")))}" '
                   f'contype="{wct}" conaffinity="{wca}" friction="0.6 0.005 0.0001" rgba="0.55 0.6 0.7 0.35" group="3"/>')
    return "\n".join(out), mat


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
        diag = np.diag(Mfull)[self.dadr]                    # includes the rotor armature
        kv = {n: 2.0 * ZETA * math.sqrt(self.info.kp[n] * max(diag[i], 1e-5))
              for i, n in enumerate(self.info.arm_joints)}
        self.xml = _xml(asm, design, opts, self.info, masses, kv=kv)
        self.model = mujoco.MjModel.from_xml_string(self.xml)
        self._index(self.model)
        self.data = mujoco.MjData(self.model)
        self.dik = mujoco.MjData(self.model)               # scratch data for IK / FK
        self.R = design.object.diameter_mm / 2.0

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
        self.site = m.site("sim_grasp").id          # object centre when grasped (= tcp for contract robots)
        self.tcp_site = m.site("sim_tcp").id
        self.obj = m.body("sim_object").id
        self.obj_q = m.jnt_qposadr[m.joint("sim_object").id]
        self.obj_geom = m.geom("sim_object").id
        self.payload = m.body("sim_payload").id
        self.tcp_body = m.body(inf.tcp_link).id
        if inf.gripper_joint:
            jid = m.joint(inf.gripper_joint).id
            self.g_q = [m.jnt_qposadr[m.joint(n).id] for n in inf.jaw_joints]
            self.g_act = m.actuator(f"act_{inf.gripper_joint}").id
            self.g_body = m.jnt_bodyid[jid]
            left = _subtree(self.asm, self.asm.joint(inf.gripper_joint).child)
            self.left_bodies = {m.body(n).id for n in left}
            self.g_axis_local = np.asarray(m.jnt_axis[jid], float)
            self.g_scale = MM if inf.grip_kind == "prismatic" else 1.0    # joint unit -> MuJoCo unit
            self.g_digits = 2 if inf.grip_kind == "prismatic" else 4
        else:
            self.g_q, self.g_act, self.left_bodies = [], None, set()
            self.g_scale, self.g_digits = MM, 2
        self.n = len(inf.arm_joints)
        self._mj = mujoco
        # world mode: obstacle geoms (world_<i>), the robot's own geoms (clearance) and the surface height
        W = self.opts.world
        self.world_geoms = np.array([i for i in range(m.ngeom) if W is not None and m.geom(i).name.startswith("world_")], int)
        self.world_names = {int(g): str((W["obstacles"][int(m.geom(g).name[6:])]).get("label") or m.geom(g).name)
                            for g in self.world_geoms}
        links = {m.body(n).id for n in self.asm.links}
        self.robot_geoms = np.array([i for i in range(m.ngeom) if m.geom_bodyid[i] in links], int)
        self.sz = _surface_top(W) if W is not None else 0.0
        self.arc = None
        self.detours = {}
        self.path, self.blocked = [], None

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

    def ik(self, p_mm, axis=None, q0=None, iters=80, tol_mm=0.2, approach=(0, 0, -1.0), free=0.0):
        """Damped least squares to a TCP position (mm) with top-down approach; ``axis`` = object long axis (world)
        to align the fingers with (dof >= 5). ``free`` (0 to 1) weakens the orientation terms by that share: 1 asks
        for the position only (an empty hand that may tilt on the way). Returns (q, pos_err_mm, rot_err_rad)."""
        mj, m, d = self._mj, self.model, self.dik
        q = (self.q_ready() if q0 is None else np.asarray(q0, float)).copy()
        p = np.asarray(p_mm, float) * MM
        zd = np.asarray(approach, float)
        zd = zd / np.linalg.norm(zd)
        jp = np.zeros((3, m.nv))
        jr = np.zeros((3, m.nv))
        ya = self.info.obj_axis_tcp
        use_yaw = axis is not None and self.n >= 5 and free < 1.0
        wr = 0.15 * (1.0 - free) if free else 0.15
        ep_n = er_n = 1e9
        for _ in range(iters):
            d.qpos[self.qadr] = q
            mj.mj_kinematics(m, d)
            mj.mj_comPos(m, d)
            x = d.site_xpos[self.site]
            Rm = d.site_xmat[self.site].reshape(3, 3)
            ep = p - x
            er = np.zeros(3) if free >= 1.0 else _cross(Rm[:, 2], zd)
            if use_yaw:
                y = Rm @ ya
                yd = np.asarray(axis, float)
                yd = yd - zd * np.dot(yd, zd)
                yd = yd / max(np.linalg.norm(yd), 1e-9)
                if np.dot(y, yd) < 0:
                    yd = -yd
                ey = _cross(y, yd)
                # a 6-DOF wrist controls tilt about the TCP x axis, which the z term already corrects:
                # count only the yaw part of the y term there, or each step overshoots ~2x and oscillates
                er = er + (zd * np.dot(ey, zd) if self.n >= 6 else ey)
            ep_n, er_n = math.sqrt(ep.dot(ep)), math.sqrt(er.dot(er))      # np.linalg.norm's arithmetic
            if ep_n < tol_mm * MM and er_n < 2e-3:
                break
            mj.mj_jacSite(m, d, jp, jr, self.site)
            J = np.vstack([jp[:, self.dadr], wr * jr[:, self.dadr]])
            e = np.concatenate((ep, wr * er))
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

    def set_grip(self, d, g):
        """Write a gripper value (joint unit) into qpos, including mimic jaws."""
        for n, a in zip(self.info.jaw_joints, self.g_q):
            j = self.asm.joint(n)
            v = g if not j.mimic else g * j.mimic.get("multiplier", 1.0) + j.mimic.get("offset", 0.0)
            d.qpos[a] = v * self.g_scale

    def grip_value(self, d) -> float:
        return float(d.qpos[self.g_q[0]]) / self.g_scale if self.g_q else 0.0

    # ------------------------------------------------------------------ sensing
    def grip_force(self, d) -> float:
        """Clamp force on the left jaw along its closing axis (what the FSR behind the pad reads), N."""
        mj, m = self._mj, self.model
        if self.g_act is None or d.ncon == 0:
            return 0.0
        f6 = np.zeros(6)
        R = d.xmat[self.g_body].reshape(3, 3)
        open_w = R @ self.g_axis_local
        hinge = self.info.grip_kind == "revolute"
        anchor = d.xanchor[m.joint(self.info.gripper_joint).id] if hinge else None
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
            if hinge:   # torque about the hinge in the opening sense, back to a force at the object centre
                total += float(np.dot(np.cross(c.pos - anchor, on_jaw), open_w)) / (self.info.lever_mm * MM)
            else:
                total += float(np.dot(on_jaw, open_w))
        return max(total, 0.0)

    def tcp_pose(self, d):
        return d.site_xpos[self.site].copy(), d.site_xmat[self.site].reshape(3, 3).copy()

    def obj_pose(self, d):
        return d.xpos[self.obj].copy(), d.xquat[self.obj].copy()

    # ------------------------------------------------------------------ world mode
    def clearance(self, d, with_object=False, distmax=0.5):
        """Smallest distance (mm) between the robot (plus the object when carried) and the world boxes at the pose
        already in ``d`` (kinematics done), and where: (mm, robot geom, world geom); ``distmax`` (m) when nothing is
        closer. Each robot geom's bounding sphere against the box itself (an exact lower bound, tight for walls and
        shelves) prunes the pairs so only the few that can be closest go to mj_geomDistance."""
        mj, m = self._mj, self.model
        if not hasattr(mj, "mj_geomDistance"):          # added in MuJoCo 3.1.6; only world mode needs it
            raise RuntimeError("World mode needs MuJoCo 3.1.6 or newer: pip install -U mujoco")
        W = self.world_geoms
        R = np.r_[self.robot_geoms, [self.obj_geom]] if with_object else self.robot_geoms
        loc = np.abs(np.matmul(d.geom_xpos[R][None, :, :] - d.geom_xpos[W][:, None, :], d.geom_xmat[W].reshape(-1, 3, 3)))
        loc -= m.geom_size[W][:, None, :]                  # each geom centre in each box's axes, outside its faces
        np.maximum(loc, 0.0, out=loc)
        lb = (np.sqrt((loc * loc).sum(axis=2)) - m.geom_rbound[R][None, :]).T
        order = np.argsort(lb, axis=None)
        best, pair = distmax, (int(R[0]), int(W[0]))
        ft = np.zeros(6)
        for k in order:
            i, j = divmod(int(k), len(W))
            if lb[i, j] >= best:
                break
            dist = mj.mj_geomDistance(m, d, int(W[j]), int(R[i]), best, ft)
            if dist < best:
                best, pair = dist, (int(R[i]), int(W[j]))
        return best / MM, pair[0], pair[1]

    def pose_carried(self, d, q, g, carried=True):
        """Arm at q, gripper at g, the object held in the grasp (as _simulate starts with it in hand; left out when not
        `carried`); kinematics."""
        mj, m = self._mj, self.model
        d.qpos[:] = 0
        d.qpos[self.qadr] = q
        if self.g_q:
            self.set_grip(d, g)
        mj.mj_kinematics(m, d)
        if not carried:
            return
        p, Rm = self.tcp_pose(d)
        info = self.info
        Ro = np.c_[Rm @ info.obj_close_tcp, Rm @ info.obj_axis_tcp, Rm @ np.array([0, 0, 1.0])]
        d.qpos[self.obj_q:self.obj_q + 3] = p
        d.qpos[self.obj_q + 3:self.obj_q + 7] = _mat2quat(Ro)
        mj.mj_kinematics(m, d)

    def where(self, rg) -> str:
        b = int(self.model.geom_bodyid[rg])
        return "object" if b == self.obj else self.model.body(b).name


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
        self.moves = []                 # [name, first sample] per move (MOVES), for world mode's clearance per move

    @property
    def T(self):
        return self.t[-1]

    def move(self, name):
        self.moves.append([name, len(self.t) - 1])

    def branch(self):
        """A scratch plan from where this one ends: a candidate move is built and checked there, and the chosen one
        appended with extend(), giving the samples building it here would."""
        b = _Plan(self.rob, self.qe, self.g[-1], self.ph[-1])
        b.t = [self.T]
        return b

    def extend(self, b):
        off = len(self.t) - 1
        self.t += b.t[1:]
        self.q += b.q[1:]
        self.g += b.g[1:]
        self.ph += b.ph[1:]
        self.markers += b.markers
        self.moves += [[nm, i + off] for nm, i in b.moves]
        self.ik_err_mm = max(self.ik_err_mm, b.ik_err_mm)

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

    def cart(self, fn, T, phase, grip=None, free=0.0):
        """fn(s in [0,1]) -> (p_mm, axis_world | None); s already eased by the caller if wanted. `free` (a share,
        or fn(s) -> share): how much of the hand's orientation is left to the IK (Robot.ik; an empty hand)."""
        n = max(1, int(round(T / PLAN_DT)))
        q = self.qe
        qs = []
        for i in range(1, n + 1):
            p, ax = fn(i / n)
            q, ep, _ = self.rob.ik(p, ax, q, iters=25, free=free(i / n) if callable(free) else free)
            self.ik_err_mm = max(self.ik_err_mm, ep)
            qs.append(q)
        self._push(qs, phase, grip)

    def mark(self, label, t=None):
        self.markers.append({"t": round(self.T if t is None else t, 3), "label": label})


def _tangent(xy):
    a = math.atan2(xy[1], xy[0])
    return np.array([-math.sin(a), math.cos(a), 0.0])


def _pick_axis(rob, pick):
    """The object's long axis on the table: SimOptions.object_yaw if given, else tangential to the base."""
    yaw = rob.opts.object_yaw
    if yaw is None:
        return _tangent(pick[:2])
    return np.array([-math.sin(yaw), math.cos(yaw), 0.0])


def _line(p0, p1):
    p0, p1 = np.asarray(p0, float), np.asarray(p1, float)
    return lambda s: p0 + (p1 - p0) * _minjerk(s)


# ====================================================================== simulation loop

@dataclass
class _Run:
    frames: list
    stats: dict


def _simulate(rob: _Robot, plan: _Plan, *, obj_xy=None, obj_yaw=None, obj_in_hand=False, payload_fn=None,
              on_step=None, record=True, scenario="", perturb=None) -> _Run:
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
    g_start = info.grip_contact if obj_in_hand else Gm[0]
    rob.set_grip(d, g_start)
    if obj_in_hand:
        mj.mj_kinematics(m, d)
        p, Rm = rob.tcp_pose(d)
        Ro = np.c_[Rm @ info.obj_close_tcp, Rm @ info.obj_axis_tcp, Rm @ np.array([0, 0, 1.0])]
        d.qpos[rob.obj_q:rob.obj_q + 3] = p
        d.qpos[rob.obj_q + 3:rob.obj_q + 7] = _mat2quat(Ro)
    else:
        x0, y0 = obj_xy
        yaw = obj_yaw if obj_yaw is not None else rob.opts.object_yaw
        yaw = yaw if yaw is not None else math.atan2(y0, x0)
        oz = rob.R + 0.2
        if perturb:
            x0, y0, yaw, oz = _perturb_start(rob, perturb, x0, y0, yaw)
        if rob.opts.world is not None:
            oz += rob.sz
        d.qpos[rob.obj_q:rob.obj_q + 3] = [x0 * MM, y0 * MM, oz * MM]
        d.qpos[rob.obj_q + 3:rob.obj_q + 7] = [math.cos(yaw / 2), 0, 0, math.sin(yaw / 2)]
    mj.mj_forward(m, d)
    d.ctrl[rob.aid] = Q[0] + d.qfrc_bias[rob.dadr] / rob.kp
    if rob.g_act is not None:
        d.ctrl[rob.g_act] = g_start * rob.g_scale
    mj.mj_forward(m, d)

    hits = {} if len(rob.world_geoms) else None   # world mode: first contact of each (link, obstacle) pair
    servo = _servo(perturb, dt, rob.n) if perturb else None
    integ = np.zeros(rob.n)
    imax = 0.5 * rob.eff
    g_ctrl = g_start
    g_rate = info.grip_speed * dt
    g_slow = GRIP_SLOW_MM * (1.0 if info.grip_kind == "prismatic" else 1.0 / max(info.lever_mm, 1.0))
    g_dir = 1.0 if info.grip_open >= info.grip_contact else -1.0       # sign of "opening"
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
        if servo is not None:
            integ = servo(step, rob, d, qr, qdr, integ, imax)
        else:
            q = d.qpos[rob.qadr]
            err = qr - q
            integ = np.clip(integ + KI * rob.kp * err * dt, -imax, imax)
            u = d.qfrc_bias[rob.dadr] + rob.kvv * qdr + integ
            d.ctrl[rob.aid] = qr + u / rob.kp
        if rob.g_act is not None:
            gt = Gm[k]
            closing = (gt - g_ctrl) * g_dir < 0
            near = (g_ctrl - info.grip_contact) * g_dir < g_slow
            rate = g_rate * (0.25 if closing and near else 1.0)
            g_ctrl = g_ctrl + float(np.clip(gt - g_ctrl, -rate, rate))
            d.ctrl[rob.g_act] = g_ctrl * rob.g_scale
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
        if step % 5 == 0 and d.ncon:
            stats["peak_grip"] = max(stats.get("peak_grip", 0.0), rob.grip_force(d))
        if hits is not None and step % 2 == 0 and d.ncon:
            _world_hits(rob, d, t + dt, hits)
    if hits is not None:
        stats["world_contacts"] = sorted(hits.values(), key=lambda h: h["t"])[:20]
    stats["max_tau"] = max_tau
    stats["max_torque_pct"] = {n: round(float(max_tau[i] / rob.eff[i] * 100.0), 1) for i, n in enumerate(info.arm_joints)}
    stats["max_speed_pct"] = {n: round(float(stats["max_speed"][i] / max(vmax[i], 1e-6) * 100.0), 1)
                              for i, n in enumerate(info.arm_joints)}
    return _Run(frames, stats)


# ====================================================================== robustness trials (perturb=, never by default)
# A trial executes the nominal plan in a world that differs from the design. Every key is optional and absent means
# nominal: off_across_mm / off_along_mm / yaw_deg (the object's start, relative to its long axis), size_scale,
# mass_g, friction_scale (pads and object), grip_scale (grip-force calibration), torque_scale (servo torque and gain),
# deadband_deg, encoder_cpr / encoder_noise (counts) / noise_seed, rate_hz (host set-points, held between updates),
# mount_dx_mm / mount_dy_mm / mount_yaw_deg (the room moves the other way), box_offsets_mm ([dx, dy] per box),
# timestep, impratio. The host still plans and computes with the nominal values.

def _perturb_start(rob, p, x0, y0, yaw):
    """The object's start in a trial: off its mark across and along its long axis, turned, resized."""
    ax = np.array([-math.sin(yaw), math.cos(yaw), 0.0])
    off = np.cross([0, 0, 1.0], ax) * p.get("off_across_mm", 0.0) + ax * p.get("off_along_mm", 0.0)
    return x0 + off[0], y0 + off[1], yaw + math.radians(p.get("yaw_deg", 0.0)), rob.R * p.get("size_scale", 1.0) + 0.2


def _servo(p, dt, n):
    """A trial's servo model, or None (the nominal loop): the host reads encoders of encoder_cpr counts per turn with
    encoder_noise counts of noise, sends set-points at rate_hz and holds them; each servo ignores position errors
    inside deadband_deg (backlash as a deadband) and drives with its own, possibly derated, gain."""
    if not any(k in p for k in ("rate_hz", "encoder_cpr", "encoder_noise", "deadband_deg")):
        return None
    res = 2 * math.pi / p["encoder_cpr"] if p.get("encoder_cpr") else 0.0
    sig = p.get("encoder_noise", 0.0) * (res or 2 * math.pi / 4096)
    rng = np.random.default_rng(p.get("noise_seed", 0))
    hold = max(1, int(round(1.0 / (p["rate_hz"] * dt)))) if p.get("rate_hz") else 1
    wdb = math.radians(p.get("deadband_deg", 0.0))
    cmd = [None]

    def step(i, rob, d, qr, qdr, integ, imax):
        q_true = d.qpos[rob.qadr]
        q_meas = q_true + (rng.normal(0, sig, n) if sig > 0 else 0.0)
        if res:
            q_meas = np.round(q_meas / res) * res
        if i % hold == 0:
            err = qr - q_meas
            integ = np.clip(integ + KI * rob.kp * err * dt * hold, -imax, imax)
            u = d.qfrc_bias[rob.dadr] + rob.kvv * qdr + integ
            cmd[0] = qr + u / rob.kp
        e = cmd[0] - q_meas
        if wdb > 0:
            e = np.sign(e) * np.maximum(np.abs(e) - wdb, 0.0)
        d.ctrl[rob.aid] = q_true + e
        return integ
    return step


PERTURBED = ("geom_size", "geom_rbound", "geom_aabb", "geom_friction", "geom_pos", "geom_quat", "body_mass",
             "body_inertia", "actuator_forcerange", "actuator_gainprm", "actuator_biasprm")


@contextlib.contextmanager
def _perturbed(rob, p):
    """A trial's changes to the compiled model (object size and mass, friction, grip-force calibration, servo torque
    and gain, the room's offsets, solver settings); the nominal model is put back bit for bit afterwards."""
    if not p:
        yield
        return
    m = rob.model
    keep = {k: getattr(m, k).copy() for k in PERTURBED}
    opt = (m.opt.timestep, m.opt.impratio, m.opt.disableflags)
    try:
        g, (shape, R, L) = rob.obj_geom, _object_dims(rob.design)
        s = p.get("size_scale")
        if s == 1.0:   # nominal: the formulas below don't give the compiled bounds bit for bit, so leave them alone
            s = None
        if s is not None:
            Ra, La = R * s, L * s
            if shape == "cylinder":
                half = max(La / 2 - Ra, 0.5)
                m.geom_size[g, :2] = [Ra * MM, half * MM]
                m.geom_rbound[g] = (Ra + half) * MM
                m.geom_aabb[g, 3:] = [Ra * MM, Ra * MM, (Ra + half) * MM]
            elif shape == "sphere":
                m.geom_size[g, 0] = Ra * MM
                m.geom_rbound[g] = Ra * MM
                m.geom_aabb[g, 3:] = Ra * MM
            else:
                m.geom_size[g] = [Ra * MM, La / 2 * MM, Ra * MM]
                m.geom_rbound[g] = math.sqrt(2 * Ra * Ra + La * La / 4) * MM
                m.geom_aabb[g, 3:] = m.geom_size[g]
        if s is not None or "mass_g" in p:
            mnom = rob.design.object.mass_g / 1000.0
            ma = p.get("mass_g", rob.design.object.mass_g) / 1000.0
            m.body_mass[rob.obj] = ma
            m.body_inertia[rob.obj] = keep["body_inertia"][rob.obj] * (ma / mnom) * (s or 1.0) * (s or 1.0)
        if "friction_scale" in p:
            for gi in [g, *np.nonzero(m.geom_contype == B_JAW)[0]]:
                m.geom_friction[gi, 0] = keep["geom_friction"][gi, 0] * p["friction_scale"]
        if "grip_scale" in p and rob.g_act is not None:
            m.actuator_forcerange[rob.g_act] *= p["grip_scale"]
            if rob.info.grip_open_loop:     # a weaker or stronger servo below its force limit: its position loop's gain
                m.actuator_gainprm[rob.g_act, 0] *= p["grip_scale"]
                m.actuator_biasprm[rob.g_act, 1] *= p["grip_scale"]
        if "torque_scale" in p:
            for a in rob.aid:
                m.actuator_forcerange[a] *= p["torque_scale"]
                m.actuator_gainprm[a, 0] *= p["torque_scale"]
                m.actuator_biasprm[a, 1] *= p["torque_scale"]
        if len(rob.world_geoms) and any(k in p for k in ("mount_dx_mm", "mount_dy_mm", "mount_yaw_deg", "box_offsets_mm")):
            yaw = -math.radians(p.get("mount_yaw_deg", 0.0))
            c, sn = math.cos(yaw), math.sin(yaw)
            Rz = np.array([[c, -sn, 0], [sn, c, 0], [0, 0, 1.0]])
            shift = -np.array([p.get("mount_dx_mm", 0.0), p.get("mount_dy_mm", 0.0), 0.0]) * MM
            offs = p.get("box_offsets_mm") or []
            for k, gid in enumerate(rob.world_geoms):
                o = offs[k] if k < len(offs) else (0.0, 0.0)
                m.geom_pos[gid] = Rz @ keep["geom_pos"][gid] + shift + np.array([o[0], o[1], 0.0]) * MM
                m.geom_quat[gid] = _qmul([math.cos(yaw / 2), 0.0, 0.0, math.sin(yaw / 2)], keep["geom_quat"][gid])
            if not np.array_equal(m.geom_pos, keep["geom_pos"]) or not np.array_equal(m.geom_quat, keep["geom_quat"]):
                # the compiled collision hierarchy still has the boxes where they were: without the midphase every pair
                # is tested where the boxes are now (a moved box was otherwise passed through without a contact)
                m.opt.disableflags |= int(rob._mj.mjtDisableBit.mjDSBL_MIDPHASE)
        if "timestep" in p:
            m.opt.timestep = float(p["timestep"])
        if "impratio" in p:
            m.opt.impratio = float(p["impratio"])
        yield
    finally:
        for k, v in keep.items():
            getattr(m, k)[:] = v
        m.opt.timestep, m.opt.impratio, m.opt.disableflags = opt


def _world_hits(rob: _Robot, d, t, hits: dict):
    """Record contacts between the world boxes and the robot or the object (first time per pair, deepest overlap)."""
    g = d.contact.geom[:d.ncon]
    w = np.isin(g, rob.world_geoms)
    for k in np.nonzero(w[:, 0] ^ w[:, 1])[0]:
        wg, other = (int(g[k, 0]), int(g[k, 1])) if w[k, 0] else (int(g[k, 1]), int(g[k, 0]))
        link = rob.where(other)
        depth = round(max(-float(d.contact.dist[k]), 0.0) / MM, 2)
        h = hits.get((link, wg))
        if h is None:
            hits[(link, wg)] = {"t": round(float(t), 3), "link": link, "obstacle": rob.world_names.get(wg, ""), "depth_mm": depth}
        elif depth > h["depth_mm"]:
            h["depth_mm"] = depth


def _frame(rob: _Robot, d, t, phase) -> dict:
    q = [round(float(v), 4) for v in d.qpos[rob.qadr]]
    if rob.g_q:
        q.append(round(rob.grip_value(d), rob.g_digits))
    p, qu = rob.obj_pose(d)
    qu = _q_json(qu)
    return {"t": round(t, 4), "q": q,
            "obj": [round(float(v) / MM, 2) for v in p] + [round(float(v), 5) for v in qu],
            "tau": [round(float(v), 4) for v in d.actuator_force[rob.aid]],
            "grip_n": round(rob.grip_force(d), 3), "phase": phase}


Q_AXIS = np.array([math.sqrt(0.5), -math.sqrt(0.5), 0.0, 0.0])   # body (long axis y) -> JSON frame (long axis z)


def _qmul(a, b):
    w1, x1, y1, z1 = a
    w2, x2, y2, z2 = b
    return np.array([w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2, w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
                     w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2, w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2])


def _q_json(q_body):
    return _qmul(np.asarray(q_body, float), Q_AXIS)


def _q_body(q_json):
    qc = Q_AXIS * np.array([1, -1, -1, -1])
    return _qmul(np.asarray(q_json, float), qc)


def _rel_obj(rob, d):
    """Object position in the tcp frame (mm)."""
    p, Rm = rob.tcp_pose(d)
    return Rm.T @ (d.xpos[rob.obj] - p) / MM


# ====================================================================== scenarios

# nominal durations (s) of the motion segments; scaled together to meet design.task.cycle_time_s
NOMINAL = {"to_hover": 1.4, "descend": 1.1, "lift": 1.0, "move": 1.8, "place": 1.1}


def _grip_times(rob):
    """(closing, opening) seconds: fast to just short of the contact, then a quarter speed until the jaw is
    GRIP_SAT_MM past it (the force limit is reached there), then a short settle."""
    info = rob.info
    k = 1.0 if info.grip_kind == "prismatic" else 1.0 / max(info.lever_mm, 1.0)
    slow, sat = GRIP_SLOW_MM * k, GRIP_SAT_MM * k
    v = info.grip_speed
    closing = max(abs(info.grip_open - info.grip_contact) - slow, 0.0) / v + (slow + 1.5 * sat) / (0.25 * v) + 0.1
    opening = (abs(info.grip_open - info.grip_contact) + abs(info.grip_contact - info.grip_close) * 0.5) / v
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
    if rob.opts.world is not None:
        R = rob.sz + R                   # world mode: the object rests on the mount surface (place height = surface + R)
    px, py = rob.opts.object_xy if rob.opts.object_xy is not None else (design.task.pick_x_mm, design.task.pick_y_mm)
    pick = np.array([px, py, R])
    place = np.array([design.task.place_x_mm, design.task.place_y_mm, R])
    return pick, place


def _rotz(v, a):
    c, s_ = math.cos(a), math.sin(a)
    return np.array([c * v[0] - s_ * v[1], s_ * v[0] + c * v[1], v[2]])


def _arc_angles(p0, p1):
    a0, a1 = math.atan2(p0[1], p0[0]), math.atan2(p1[1], p1[0])
    if a1 - a0 > math.pi:
        a1 -= 2 * math.pi
    elif a0 - a1 > math.pi:
        a1 += 2 * math.pi
    return a0, a1


def _arc(p0, p1, inward, bump, ax0=None):
    """Path around the base between two points: polar interpolation, pulled inward / lifted mid-way. The
    object's axis (``ax0`` at the start, tangential by default) turns with the base. fn(u) eases along it (minimum
    jerk); fn.at(e) is the same path by its eased parameter, for moving along part of it."""
    r0, r1 = np.linalg.norm(p0[:2]), np.linalg.norm(p1[:2])
    a0, a1 = _arc_angles(p0, p1)
    ax0 = _tangent(p0[:2]) if ax0 is None else np.asarray(ax0, float)

    def at(e):
        b = math.sin(math.pi * e)
        r = (r0 + (r1 - r0) * e) * (1.0 - inward * b)
        a = a0 + (a1 - a0) * e
        z = p0[2] + (p1[2] - p0[2]) * e + bump * b
        return np.array([r * math.cos(a), r * math.sin(a), z]), _rotz(ax0, a - a0)

    def fn(u):
        return at(_minjerk(u))
    fn.at = at
    return fn


ARCS = ((0.06, 20.0), (0.12, 12.0), (0.2, 0.0), (0.3, 0.0))                 # (inward, bump mm), tried in order
WIDE_ARCS = tuple((i, b) for i in (0.06, 0.12, 0.2, 0.3, 0.4, 0.5) for b in (20.0, 0.0, 40.0))   # world mode adds these


def _feasible_arc(rob, p0, p1, q0, ax0=None):
    if len(rob.world_geoms):
        return _clear_arc(rob, p0, p1, q0, ax0)
    return _default_arc(rob, p0, p1, q0, ax0)


def _arc_reachable(rob, fn, q0) -> bool:
    q = q0
    for u in np.linspace(0.05, 0.95, 10):
        p, ax = fn(u)
        pm = np.r_[p[:2] * 1.02, p[2]]
        q, ep, er = rob.ik(pm, ax, q, iters=60)
        if ep > 0.5 or er > 5e-3:
            return False
    return True


def _default_arc(rob, p0, p1, q0, ax0=None):
    for inward, bump in ARCS:
        fn = _arc(p0, p1, inward, bump, ax0)
        if _arc_reachable(rob, fn, q0):
            return fn
    return _arc(p0, p1, 0.3, 0.0, ax0)


def _clear_arc(rob, p0, p1, q0, ax0=None, verify=None):
    """World mode: the first arc, in the usual order and then a wider family (pulled in 6-50 %, lifted 0-40 mm), that
    is reachable like the usual one and whose IK sweep, object in hand, stays CLEAR_MM from every obstacle box; with a
    clear room that is the arc the plain table gets. `verify(fn)` (world mode's plan) then checks an arc that passed at
    the carry's own 10 ms samples: one that comes closer there is not clear after all. When none is clear, the reachable
    one with the most clearance (the path check then reports it). This chooses among arcs; it is not a motion planner.
    rob.arc["usual"] says whether the arc is the one the plain table gets (_default_arc's), so nobody is told it was
    changed for the room when it wasn't."""
    d = rob._mj.MjData(rob.model)
    g = rob.info.grip_contact
    best, tried, usual = None, 0, None
    for inward, bump in ARCS + tuple(c for c in WIDE_ARCS if c not in ARCS):
        tried += 1
        fn = _arc(p0, p1, inward, bump, ax0)
        if not _arc_reachable(rob, fn, q0):
            continue
        if usual is None and (inward, bump) in ARCS:
            usual = (inward, bump)
        q, worst = q0, math.inf
        for u in np.linspace(0.0, 1.0, 21):
            p, ax = fn(u)
            q, _, _ = rob.ik(p, ax, q, iters=60)
            rob.pose_carried(d, q, g)
            worst = min(worst, rob.clearance(d, with_object=True)[0])
            if worst < CLEAR_MM and best is not None and worst <= best[0]:
                break                          # not clear, and no better than the fallback so far: next arc
        if worst >= CLEAR_MM and verify is not None:
            worst = min(worst, verify(fn))
        if worst >= CLEAR_MM:
            rob.arc = {"inward": inward, "bump_mm": bump, "min_clearance_mm": round(float(worst), 1), "tried": tried,
                       "clear": True, "usual": (inward, bump) == (usual or (0.3, 0.0))}
            return fn
        if best is None or worst > best[0]:
            best = (worst, inward, bump, fn)
    if best is None:
        rob.arc = {"tried": tried, "clear": False, "usual": True,
                   "note": "no arc of the family is reachable; using the default"}
        return _arc(p0, p1, 0.3, 0.0, ax0)
    rob.arc = {"inward": best[1], "bump_mm": best[2], "min_clearance_mm": round(float(best[0]), 1), "tried": tried,
               "clear": False, "usual": (best[1], best[2]) == (usual or (0.3, 0.0)),
               "note": f"no arc keeps {CLEAR_MM:.0f} mm from the room; using the one with the most room"}
    return best[3]


def _held(rob, g) -> float:
    """The jaws' real opening for a commanded gripper value: told to close past the object (only ever while holding it),
    they stop on it."""
    c = rob.info.grip_contact
    return c if (g - c) * (1.0 if rob.info.grip_open >= c else -1.0) < 0 else g


def _along(rob, d, plan, i0=1, i1=None, stop=-math.inf):
    """World mode: the smallest clearance (mm) between the robot (and the object while it is carried) and the boxes over
    plan samples i0..i1-1, one every 10 ms as the simulation follows them: (mm, sample, robot geom, world geom); inf when
    there are none. Stops once it is below `stop` (a candidate that can no longer beat the best one)."""
    i1 = len(plan.t) if i1 is None else i1
    best, cut, last = (math.inf, i0, None, None), 0.5, None
    for k in range(i0, i1):
        held, g = plan.ph[k] in CARRIED, _held(rob, plan.g[k])
        if last is not None and held == last[0] and g == last[1] and np.array_equal(plan.q[k], last[2]):
            continue                           # a hold: the same pose as the sample before
        last = (held, g, plan.q[k])
        rob.pose_carried(d, plan.q[k], g, held)
        c, rg, wg = rob.clearance(d, with_object=held, distmax=cut)
        if c < best[0]:
            best, cut = (c, k, rg, wg), max(c * MM, 0.0)
            if c < stop:
                break
    return best


def _choose(rob, d, plan, cands):
    """World mode: build each candidate move, (via, extra, build(branch) -> False to skip), on a branch of the plan,
    check it every 10 ms and append the first that keeps CLEAR_MM, else the one with the most room (the earlier one on a
    tie). Returns the record for rob.detours: {via, min_clearance_mm, clear, **extra}."""
    best = None
    for via, extra, build in cands:
        b = plan.branch()
        if build(b) is False:
            continue
        c = _along(rob, d, b, stop=-math.inf if best is None else best[0])[0]
        if best is None or c > best[0]:
            best = (c, via, extra, b)
        if c >= CLEAR_MM:
            break
    c, via, extra, b = best
    plan.extend(b)
    return {"via": via, "min_clearance_mm": round(float(min(c, 500.0)), 1), "clear": bool(c >= CLEAR_MM), **extra}


def _arc_q(rob, at, e0, e1, q):
    """q at at(e1), following the arc (`at`: by its eased parameter) by IK from at(e0), and whether it stays in reach."""
    ok = True
    for e in np.linspace(e0, e1, max(9, int(abs(e1 - e0) * 40) + 1)):
        p, ax = at(e)
        q, ep, er = rob.ik(p, ax, q, iters=60)
        ok = ok and ep < 0.5 and er < 5e-3
    return q, ok


def _arc_e_at(rob, p0, p1, q_rest):
    """Where along the carry arc (0 above the pick spot, 1 above the place spot) it passes the direction of the hand at
    rest, seen from the base; None when the rest pose points outside the arc."""
    tcp, _ = rob.fk(q_rest)
    a0, a1 = _arc_angles(p0, p1)
    if abs(a1 - a0) < 1e-3 or math.hypot(tcp[0], tcp[1]) < 1.0:
        return None
    a = math.atan2(tcp[1], tcp[0])
    a += 2 * math.pi * round((0.5 * (a0 + a1) - a) / (2 * math.pi))       # the turn nearest the arc
    e = (a - a0) / (a1 - a0)
    return float(e) if 0.02 < e < 0.98 else None


def _clear_approach(rob, d, plan, above_pick, above_place, ax, q_above, s, arc, hover0):
    """World mode: from the rest pose to above the object, checked every 10 ms. The joint move straight there when it
    keeps CLEAR_MM from every box; else a short move to where the carry arc passes the rest pose's direction, then
    along the arc (chosen to keep clear of the boxes) to above the object; else from above: straight up from the rest
    pose to 40, 80 or 120 mm over the hover height, across at that height and straight down to above the object, the
    empty hand free to tilt on the way and vertical again at the bottom (a vertical hand that high is out of reach for
    most arms). The one with the most room when none is clear (the path check then reports it). Records
    rob.detours["approach"]."""
    q0 = plan.qe
    p_rest = rob.fk(q0)[0]

    def above(dh, top_rest, top_pick):
        def go(b):
            b.cart(lambda u: (_line(p_rest, top_rest)(u), None),
                   max(0.4, NOMINAL["descend"] * s * (top_rest[2] - p_rest[2]) / hover0), "approach", free=1.0)
            b.cart(lambda u: (_line(top_rest, top_pick)(u), None), NOMINAL["to_hover"] * s, "approach", free=1.0)
            b.cart(lambda u: (_line(top_pick, above_pick)(u), ax), max(0.4, NOMINAL["descend"] * s * dh / hover0),
                   "approach", free=lambda u: 1.0 - u)
        return go

    def cands():
        yield "direct", {}, lambda b: b.joint(q_above, NOMINAL["to_hover"] * s, "approach")
        e_rest = _arc_e_at(rob, above_pick, above_place, q0)
        if e_rest is not None:
            q_mid, ok = _arc_q(rob, arc.at, 0.0, e_rest, q_above)
            if ok:
                yield "carry path", {}, lambda b: (
                    b.joint(q_mid, 0.7 * NOMINAL["to_hover"] * s, "approach"),
                    b.cart(lambda v: arc.at(e_rest * (1.0 - _minjerk(v))), max(0.6, NOMINAL["move"] * e_rest) * s, "approach"))
        for dh in LIFTS_MM:
            top_pick = above_pick + [0, 0, dh]
            top_rest = np.r_[p_rest[:2], max(p_rest[2], top_pick[2])]
            q_t, e_t, _ = rob.ik(top_rest, None, q0, iters=100, free=1.0)
            if e_t > 0.5 or rob.ik(top_pick, None, q_t, iters=100, free=1.0)[1] > 0.5:
                break
            yield "from above", {"rise_mm": dh}, above(dh, top_rest, top_pick)
    rob.detours["approach"] = _choose(rob, d, plan, cands())


def _clear_release(rob, d, plan, place, above_place, ax, s):
    """World mode: let go of the object and rise straight back to the hover point, checked every 10 ms: the jaws open
    as usual, else (a box beside the place spot) only OPEN_LESS_MM past the object. Records rob.detours["release"];
    returns the time the object was released."""
    info = rob.info
    _, opening = _grip_times(rob)
    sgn = 1.0 if info.grip_open >= info.grip_contact else -1.0
    less = info.grip_contact + sgn * OPEN_LESS_MM * (1.0 if info.grip_kind == "prismatic" else 1.0 / max(info.lever_mm, 1.0))
    t_rel = {}

    def build(g, via):
        def go(b):
            b.move("release")
            b.hold(0.1, "release", grip=g)
            b.hold(0.5 * opening, "release")
            b.cart(lambda u: (_line(place + [0, 0, 1.5], place + [0, 0, 5.0])(u), ax), 0.5 * opening + 0.1, "release")
            t_rel[via] = b.T
            b.mark("released")
            b.move("rise")
            b.cart(lambda u: (_line(place + [0, 0, 5.0], above_place)(u), ax), 0.9 * s, "retreat")
        return go

    def cands():
        yield "full", {}, build(info.grip_open, "full")
        if info.grip_kind != "none" and (info.grip_open - less) * sgn > 0.5:
            yield "opened less", {"open_mm": OPEN_LESS_MM}, build(less, "opened less")
    rob.detours["release"] = _choose(rob, d, plan, cands())
    return t_rel[rob.detours["release"]["via"]]


def _clear_retreat(rob, d, plan, fn, above_pick, above_place, s, hover0):
    """World mode: from above the place spot back to the rest pose, checked every 10 ms. The joint move straight there
    when it keeps CLEAR_MM from every box; else the same with the hand first closed to the object's width; else (hand
    closed, as it was on the way over) back along the carry arc `fn` to where it passes the rest pose's direction, then
    to the rest pose; else back along the whole arc, then the way the approach came; else straight up 40, 80 or 120 mm
    first, then to the rest pose. The one with the most room when none is clear (the path check then reports it). The
    hand opens fully again at rest. The rise keeps the hand's position on the vertical but not its orientation (it is
    empty), so arms with no top-down room above the place spot can still rise. Records rob.detours["retreat"]."""
    q0, q_rest = plan.qe, rob.q_ready()
    g_open, g_hold, g_now = rob.info.grip_open, rob.info.grip_contact, plan.g[-1]
    closing, opening = _grip_times(rob)

    def build(g, e_end=None, up=0.0):
        def go(b):
            if g != g_now:
                b.hold(closing, "retreat", grip=g)
            if up:                             # an empty hand: it may tilt on the way up
                b.cart(lambda u: (_line(above_place, above_place + [0, 0, up])(u), None),
                       max(0.4, NOMINAL["descend"] * s * up / hover0), "retreat", free=1.0)
            if e_end is not None:              # back along the arc at the speed it was carried over
                span = 1.0 - e_end
                b.cart(lambda v: fn.at(1.0 - span * _minjerk(v)), max(0.6, NOMINAL["move"] * span) * s, "retreat")
            b.joint(q_rest, 1.2 * s, "retreat")
            if g != g_open:
                b.hold(opening, "retreat", grip=g_open)
        return go

    def cands():
        yield "direct", {}, build(g_now)
        yield "direct, hand closed", {}, build(g_hold)
        e_rest = _arc_e_at(rob, above_pick, above_place, q_rest)
        for e_end in [e for e in (e_rest, 0.0) if e is not None]:
            if _arc_q(rob, fn.at, 1.0, e_end, q0)[1]:
                yield "carry path" if e_end > 0 else "carry path and approach", {}, build(g_hold, e_end)
        for dh in LIFTS_MM:
            if rob.ik(above_place + [0, 0, dh], None, q0, iters=100, free=1.0)[1] > 0.5:
                break
            yield "straight up first", {"rise_mm": dh}, build(g_hold, up=dh)
    rob.detours["retreat"] = _choose(rob, d, plan, cands())


def _move_at(plan, t) -> str:
    """The move (MOVES) the plan is in at time t."""
    k = min(int(t / PLAN_DT + 1e-9), len(plan.t) - 1)
    name = plan.moves[0][0] if plan.moves else ""
    for nm, i in plan.moves:
        if i < k:
            name = nm
    return name


def _check_path(rob, plan):
    """World mode: every move of the finished plan checked against the boxes every 10 ms (the object with the hand while
    it is carried): rob.path, one {move, phase, min_clearance_mm, clear, at?} per move, and rob.blocked, the first move
    whose planned path still passes into a box, which fails the simulation however the run goes (no alternative kept
    clear of it)."""
    d = rob._mj.MjData(rob.model)
    ends = [i for _, i in plan.moves[1:]] + [len(plan.t)]
    rob.path, rob.blocked = [], None
    for (name, i0), i1 in zip(plan.moves, ends):
        c, k, rg, wg = _along(rob, d, plan, i0, i1)
        e = {"move": name, "phase": plan.ph[min(i0 + 1, i1 - 1)], "min_clearance_mm": round(float(min(c, 500.0)), 1),
             "clear": bool(c >= CLEAR_MM)}
        if rg is not None and c < 500.0:
            e["at"] = {"t": round(float(plan.t[k]), 2), "link": rob.where(rg), "obstacle": rob.world_names.get(wg, "")}
        rob.path.append(e)
        if c < 0.0 and rob.blocked is None:
            rob.blocked = e


def _world_metrics(rob, design, run, plan) -> dict:
    """World mode, pick_place: contacts recorded during the simulation (each with the move and plan phase it happened
    in), the smallest clearance between the robot (and the object while carried) and the room over the simulated frames,
    with where it happens, and the plan's own check of every move (path, blocked)."""
    W = rob.opts.world
    d = rob._mj.MjData(rob.model)
    best = (math.inf, None, None, 0.0)
    for f in run.frames:
        d.qpos[:] = 0
        d.qpos[rob.qadr] = f["q"][:rob.n]
        if rob.g_q:
            rob.set_grip(d, f["q"][-1])
        carried = f["phase"] in ("lift", "move", "place")
        d.qpos[rob.obj_q:rob.obj_q + 3] = np.asarray(f["obj"][:3]) * MM
        d.qpos[rob.obj_q + 3:rob.obj_q + 7] = _q_body(f["obj"][3:7])
        rob._mj.mj_kinematics(rob.model, d)
        mc, rg, wg = rob.clearance(d, with_object=carried)
        if mc < best[0]:
            best = (mc, rg, wg, f["t"])
    hits = run.stats.get("world_contacts") or []
    for h in hits:
        h["move"] = _move_at(plan, h["t"])
        h["phase"] = plan.ph[min(int(h["t"] / PLAN_DT + 1e-9), len(plan.ph) - 1)]
    mc = round(float(best[0]), 1) if math.isfinite(best[0]) else None
    out = {"world": W.get("id", ""), "world_sha": W.get("sha", ""), "world_contacts": hits, "min_clearance_mm": mc,
           "clearance_ok": bool(not hits and mc is not None and mc >= CLEAR_MM and rob.blocked is None), "arc": rob.arc,
           "detours": dict(rob.detours), "path": list(rob.path)}
    if rob.blocked is not None:
        out["blocked"] = dict(rob.blocked)
    if best[1] is not None:
        out["min_clearance_at"] = {"t": round(float(best[3]), 2), "link": rob.where(best[1]),
                                   "obstacle": rob.world_names.get(best[2], "")}
    return out


def _world_off(rob):
    """Take the world boxes out of contact (capacity tests); returns what _world_on puts back."""
    m = rob.model
    saved = (m.geom_contype[rob.world_geoms].copy(), m.geom_conaffinity[rob.world_geoms].copy())
    m.geom_contype[rob.world_geoms] = 0
    m.geom_conaffinity[rob.world_geoms] = 0
    return saved


def _world_on(rob, saved):
    rob.model.geom_contype[rob.world_geoms], rob.model.geom_conaffinity[rob.world_geoms] = saved


def _grasp_sequence(rob, plan, pick, s, hover):
    """approach (descend open), grasp, lift. Returns the q at the grasp."""
    ax = _pick_axis(rob, pick)
    above = pick + [0, 0, hover]
    plan.move("descent")
    plan.cart(lambda u: (_line(above, pick)(u), ax), NOMINAL["descend"] * s, "approach")
    plan.mark("at grasp height")
    q_grasp = plan.qe.copy()
    t_close = plan.T
    closing, _ = _grip_times(rob)
    info = rob.info
    k = 1.0 if info.grip_kind == "prismatic" else 1.0 / max(info.lever_mm, 1.0)
    t_touch = t_close + max(abs(info.grip_open - info.grip_contact) - GRIP_SLOW_MM * k, 0.0) / info.grip_speed \
        + GRIP_SLOW_MM * k / (0.25 * info.grip_speed)
    plan.move("grasp")
    plan.hold(0.15, "grasp", grip=info.grip_close)
    plan.hold(closing + 0.35, "grasp")
    plan.mark("contact", t_touch)
    plan.mark("{grip}", t_close + closing)
    plan.move("lift")
    plan.cart(lambda u: (_line(pick, above)(u), ax), NOMINAL["lift"] * s, "lift")
    return q_grasp


def _pick_place(rob: _Robot, design, fast=False, perturb=None):
    plan, at = _plan_pick_place(rob, design, fast)
    with _perturbed(rob, perturb):
        run, metrics = _run_pick_place(rob, design, plan, at, perturb)
    return plan, run, metrics


def _plan_pick_place(rob: _Robot, design, fast=False):
    """The pick-and-place plan, always for the nominal design (a robustness study plans once and runs it many times):
    (plan, at), `at` holding the targets and times the run and its metrics need. World mode checks the finished plan
    every 10 ms (rob.path, rob.blocked)."""
    s = _cycle_scale(rob, design)
    pick, place = _targets(rob, design)
    ax_pick = _pick_axis(rob, pick)
    a0, a1 = _arc_angles(pick, place)
    ax_place = _rotz(ax_pick, a1 - a0)               # the object turns with the base on the way over
    hover = min(_hover_mm(rob, pick, ax_pick), _hover_mm(rob, place, ax_place))
    plan, at = _plan_at(rob, design, fast, s, pick, place, ax_pick, ax_place, hover)
    if len(rob.world_geoms):
        _check_path(rob, plan)
    return plan, at


def _plan_at(rob, design, fast, s, pick, place, ax_pick, ax_place, hover):
    info = rob.info
    world = len(rob.world_geoms) > 0
    q_hover, e1, _ = rob.ik_robust(pick + [0, 0, hover], ax_pick)
    _, e2, _ = rob.ik_robust(place + [0, 0, 1.5], ax_place, seeds=[q_hover])
    plan = _Plan(rob, rob.q_ready(), info.grip_open, "approach")
    plan.move("approach")
    plan.hold(0.25, "approach")
    above_pick, above_place = pick + [0, 0, hover], place + [0, 0, hover]
    if world:                                         # world mode: around the boxes when the direct move isn't clear
        d = rob._mj.MjData(rob.model)

        def verify(fn):                               # the carry at its own 10 ms samples, object in hand
            b = _Plan(rob, q_hover, info.grip_close, "move")
            b.cart(fn, NOMINAL["move"] * s, "move")
            return _along(rob, d, b)[0]
        arc = _clear_arc(rob, above_pick, above_place, q_hover, ax_pick, verify)
        _clear_approach(rob, d, plan, above_pick, above_place, ax_pick, q_hover, s, arc, hover)
    else:
        plan.joint(q_hover, NOMINAL["to_hover"] * s, "approach")
    plan.mark("above the object")
    _grasp_sequence(rob, plan, pick, s, hover)
    plan.mark("lifted")
    t_move = plan.T
    if not world:
        arc = _feasible_arc(rob, above_pick, above_place, plan.qe, ax_pick)
    plan.move("carry")
    plan.cart(arc, NOMINAL["move"] * s, "move")
    plan.move("set-down")
    plan.cart(lambda u: (_line(above_place, place + [0, 0, 1.5])(u), ax_place), NOMINAL["place"] * s, "place")
    plan.mark("set down")
    t_release = plan.T
    if world:
        t_released = _clear_release(rob, d, plan, place, above_place, ax_place, s)
    else:
        _, opening = _grip_times(rob)
        plan.move("release")
        plan.hold(0.1, "release", grip=info.grip_open)
        plan.hold(0.5 * opening, "release")
        plan.cart(lambda u: (_line(place + [0, 0, 1.5], place + [0, 0, 5.0])(u), ax_place), 0.5 * opening + 0.1, "release")
        t_released = plan.T
        plan.mark("released")
        plan.move("rise")
        plan.cart(lambda u: (_line(place + [0, 0, 5.0], above_place)(u), ax_place), 0.9 * s, "retreat")
    if not fast:
        plan.move("return")
    if not fast and world:
        _clear_retreat(rob, d, plan, arc, above_pick, above_place, s, hover)
    elif not fast:
        plan.joint(rob.q_ready(), 1.2 * s, "retreat")
    plan.hold(0.3, "retreat")
    return plan, {"pick": pick, "place": place, "e1": e1, "e2": e2, "hover": hover, "t_move": t_move,
                  "t_release": t_release, "t_released": t_released}


def _run_pick_place(rob: _Robot, design, plan, at, perturb=None, on_step=None):
    """Simulate a pick-and-place plan and measure it: (run, metrics). `perturb`: a robustness trial (_simulate; the
    model changes come from _perturbed around this call); `on_step(t, data, phase)` also sees every step."""
    info = rob.info
    pick, place, e1, e2 = at["pick"], at["place"], at["e1"], at["e2"]
    t_move, t_release, t_released = at["t_move"], at["t_release"], at["t_released"]
    # monitor in-hand slip between the end of the grasp and the release
    st = {"rel0": None, "slip": 0.0, "grip": [], "carry": False}

    def monitor(t, d, ph):
        if ph in ("lift", "move", "place") and t < t_release:
            rel = _rel_obj(rob, d)
            if st["rel0"] is None:
                st["rel0"] = rel
            st["slip"] = max(st["slip"], float(np.linalg.norm(rel - st["rel0"])))
        return True if on_step is None else on_step(t, d, ph)

    run = _simulate(rob, plan, obj_xy=pick[:2], on_step=monitor, perturb=perturb)
    _label_grip(plan, run)
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
    notes = []
    if e1 >= 2:
        notes.append(f"pick target out of top-down reach (IK misses by {e1:.0f} mm)")
    if e2 >= 2:
        notes.append(f"place target out of top-down reach (IK misses by {e2:.0f} mm)")
    if notes:
        metrics["note"] = "; ".join(notes)
    metrics.update(_damage(design, run, metrics))
    metrics.update({"peak_grip_n": metrics["grip_n_max"], "target_grip_n": metrics["grip_target_n"],
                    "slip_mm": metrics["max_slip_mm"], "payload_g": design.object.mass_g, "reach_mm": _sized_reach(rob, design)})
    if not metrics["intact"]:
        metrics["success"] = False
    if not len(rob.world_geoms) and rob.opts.world:      # a world without boxes: simulated there, nothing to hit
        W = rob.opts.world
        metrics.update({"world": W.get("id", ""), "world_sha": W.get("sha", ""), "world_contacts": [], "clearance_ok": True})
    if len(rob.world_geoms):
        metrics.update(_world_metrics(rob, design, run, plan))
        b = rob.blocked
        if metrics["world_contacts"]:
            h = metrics["world_contacts"][0]
            metrics["success"] = False
            metrics["failure"] = (f"the {h['link'].replace('_', ' ')} touches {h['obstacle']} in the arm's starting pose"
                                  if h["t"] < 0.05 else
                                  f"the {h['link'].replace('_', ' ')} hits {h['obstacle']} during "
                                  f"{MOVES.get(h['move'], 'the run')} at {h['t']:.1f} s")
            plan.mark(f"hits {h['obstacle']}", h["t"])
        elif b is not None:                # the planned path itself passes into a box: fails however the run went
            metrics["success"] = False
            metrics["failure"] = (f"the {b['at']['link'].replace('_', ' ')} would pass {-b['min_clearance_mm']:.1f} mm "
                                  f"into {b['at']['obstacle']} during {MOVES.get(b['move'], 'the run')} at "
                                  f"{b['at']['t']:.1f} s, and no clear way around it was found")
            plan.mark(f"path into {b['at']['obstacle']}", b["at"]["t"])
    return run, metrics


def _sized_reach(rob, design):
    """Reach the robot was sized for (an imported robot reports its own), else the design parameter."""
    r = (rob.asm.meta.get("sizing") or {}).get("reach_mm")
    return float(r) if r else float(design.arm.reach_mm)


def _label_grip(plan, run):
    """Put the 'contact' marker on the first frame the pad sensor feels the object, and fill the '{grip}'
    marker with the force it reads once the grip has settled."""
    for mk in plan.markers:
        if mk["label"] == "contact":
            hit = next((f for f in run.frames if f["t"] >= mk["t"] - 0.6 and f["grip_n"] > 0.05), None)
            if hit is not None:
                mk["t"] = hit["t"]
        elif mk["label"] == "{grip}":
            f = min(run.frames, key=lambda fr: abs(fr["t"] - mk["t"]))
            mk["label"] = f"holding {f['grip_n']:.1f} N"


def _damage(design, run, metrics) -> dict:
    """Peak squeeze (sampled at 200 Hz) against the object's damage force."""
    peak = max(float(run.stats.get("peak_grip", 0.0)), metrics.get("grip_n_max", 0.0))
    lim = design.object.max_force_n or None
    intact = not (lim and peak > lim)
    out = {"grip_n_max": round(peak, 2), "damage_force_n": lim, "intact": bool(intact)}
    if "egg" in (design.object.name or "").lower():
        out["egg_intact"] = bool(intact)
    return out


def _far_pose(rob, design):
    """Top-down TCP pose at ~90 % of the reachable radius along the pick direction, held at mid height
    (object well clear of the table), where the shoulder moment is near its maximum."""
    info = rob.info
    a = math.atan2(design.task.pick_y_mm, design.task.pick_x_mm)
    u = np.array([math.cos(a), math.sin(a), 0.0])
    q0 = rob.q_ready()
    T = rob.asm.world_transforms({n: 0.0 for n in info.arm_joints})
    pitch = [n for n in info.arm_joints if n != info.arm_joints[0]]
    shoulder_z = T[rob.asm.joint(pitch[0]).child][2, 3] if pitch else 100.0
    wrist = [n for n in info.arm_joints if "wrist_pitch" in n]
    L3 = (T[info.tcp_link][2, 3] - T[rob.asm.joint(wrist[0]).child][2, 3]) if wrist else 60.0
    z = max(shoulder_z - L3, 2 * rob.R + 40.0)
    if rob.opts.world is not None:
        z = max(z, rob.sz + 2 * rob.R + 40.0)
    lo, hi, best = 50.0, 1500.0, None
    for _ in range(18):
        r = 0.5 * (lo + hi)
        q, ep, er = rob.ik(u * r + [0, 0, z], _tangent(u[:2]), best if best is not None else q0, iters=120)
        if ep < 0.5 and er < 5e-3:
            lo, best = r, q
        else:
            hi = r
    r = 0.9 * lo
    q, ep, er = rob.ik(u * r + [0, 0, z], _tangent(u[:2]), best if best is not None else q0, iters=150)
    return q, np.r_[u[:2] * r, z], lo


def _stretch_pose(rob, design):
    """The arm stretched straight out: the tool point at shoulder height, as far out along the pick direction as the
    arm reaches (position only), where every joint's lever is longest. (q, radius in mm), or (None, 0.0)."""
    info = rob.info
    pitch = [n for n in info.arm_joints if n != info.arm_joints[0]]
    if not pitch:
        return None, 0.0
    a = math.atan2(design.task.pick_y_mm, design.task.pick_x_mm)
    u = np.array([math.cos(a), math.sin(a), 0.0])
    z = rob.asm.world_transforms({n: 0.0 for n in info.arm_joints})[rob.asm.joint(pitch[0]).child][2, 3]
    lo, hi, best = 0.0, 1500.0, None
    for _ in range(16):
        r = 0.5 * (lo + hi)
        q, ep, _ = rob.ik_robust(u * r + [0, 0, z], None, seeds=[best] if best is not None else [], iters=200, free=1.0)
        if ep < 0.5:
            lo, best = r, q
        else:
            hi = r
    return best, lo


def _static_capacity(rob, q):
    """Payload (kg) at the TCP each joint can hold statically at q: (effort - arm gravity torque) / lever."""
    mj, m, d = rob._mj, rob.model, rob.dik
    d.qpos[:] = 0
    d.qpos[rob.qadr] = q
    d.qpos[rob.obj_q + 3] = 1.0
    d.qvel[:] = 0
    mj.mj_forward(m, d)
    tau_g = d.qfrc_bias[rob.dadr].copy()
    jp = np.zeros((3, m.nv))
    mj.mj_jacSite(m, d, jp, None, rob.site)
    lever = np.abs(jp[2, rob.dadr]) * G
    cap = np.where(lever > 1e-6, (rob.eff - np.abs(tau_g)) / np.maximum(lever, 1e-9), np.inf)
    return cap, tau_g


def _payload_sweep(rob: _Robot, design, fast=False):
    """Carry the object out to a far pose, then ramp an extra mass at the TCP until a servo saturates
    (or the TCP sags > SAG_MM); unload and let the arm recover."""
    info = rob.info
    q_far, p_far, _ = _far_pose(rob, design)
    cap, _ = _static_capacity(rob, q_far)
    q_str, r_str = _stretch_pose(rob, design)       # statics on the same model: nothing more is simulated
    cap_str = _static_capacity(rob, q_str)[0] if q_str is not None else None
    mobj = design.object.mass_g / 1000.0
    est = float(np.min(cap)) - mobj
    ramp_T = 3.0 if fast else 5.0
    top = max(1.7 * est, 0.3)
    q_r = rob.q_ready()
    p_r, _ = rob.fk(q_r)
    start_ready = p_r[2] > 2 * rob.R + 25.0
    plan = _Plan(rob, q_r if start_ready else q_far, info.grip_close, "move")
    plan.hold(0.3, "move")
    if start_ready:
        plan.joint(q_far, 1.6, "move")
    plan.hold(0.4, "hold")
    t0 = plan.T
    plan.mark("payload ramp starts", t0)
    plan.hold(ramp_T + (1.4 if not fast else 1.0), "hold")
    st = {"fail_t": None, "fail_joint": None, "sag": 0.0, "ref": None, "unload": None, "max_ok": 0.0, "log": []}

    def payload(t):
        if st["unload"] is not None and t >= st["unload"]:
            return 1e-6
        if t < t0:
            return 1e-6
        tt = t if st["fail_t"] is None else st["fail_t"]
        return top * min((tt - t0) / ramp_T, 1.0)

    def on_step(t, d, ph):
        if st["unload"] is not None and t > st["unload"] + 1.2:
            return False
        p, _ = rob.tcp_pose(d)
        if st["ref"] is None and t >= t0 - 0.02:
            st["ref"] = p.copy()
        if st["ref"] is not None and st["fail_t"] is None and t > t0:
            sag = float(np.linalg.norm(p - st["ref"])) / MM
            st["sag"] = sag
            tau = np.abs(d.actuator_force[rob.aid])
            satj = [info.arm_joints[i] for i in range(rob.n) if tau[i] >= 0.995 * rob.eff[i]]
            if satj and st.get("first_sat") is None:
                st["first_sat"] = float(payload(t))
            if sag > SAG_MM or (satj and sag > 1.0):
                st["fail_t"] = t
                st["fail_joint"] = satj[0] if satj else info.arm_joints[int(np.argmax(tau / rob.eff))]
                st["unload"] = t + 0.25
            else:
                st["max_ok"] = float(payload(t))
                if not st["log"] or t - st["log"][-1][0] >= 0.05:
                    st["log"].append((t, st["max_ok"], d.actuator_force[rob.aid].copy(), sag))
        return True

    saved = _world_off(rob) if len(rob.world_geoms) else None   # a capacity test: the room is ignored
    try:
        run = _simulate(rob, plan, obj_in_hand=True, payload_fn=payload, on_step=on_step)
    finally:
        if saved is not None:
            _world_on(rob, saved)
    for f in run.frames:
        if st["fail_t"] is not None and f["t"] >= st["fail_t"]:
            f["phase"] = "recover" if f["t"] >= st["unload"] else "overload"
    max_extra = (st.get("first_sat") or st["max_ok"]) if st["fail_t"] is not None else top
    max_payload_g = (max_extra + mobj) * 1000.0
    if st["fail_t"] is not None:
        plan.mark(f"{st['fail_joint']} saturates at {max_payload_g:.0f} g", st["fail_t"])
        plan.mark("payload removed", st["unload"])
    grip_cap = 2 * design.object.friction * info.grip_force_max / G * 1000.0
    j_lim = info.arm_joints[int(np.argmin(cap))]
    lim = st["fail_joint"] or j_lim
    sh = info.arm_joints[1] if len(info.arm_joints) > 1 else info.arm_joints[0]
    si = info.arm_joints.index(sh)
    curve = []
    if st["log"]:
        logs = st["log"]
        for k in np.linspace(0, len(logs) - 1, min(12, len(logs))).astype(int):
            t, mx, tau, sag = logs[k]
            curve.append({"payload_g": round((mx + mobj) * 1000.0, 0),
                          "torque_nm": {n: round(float(tau[i]), 3) for i, n in enumerate(info.arm_joints)},
                          "pct": {n: round(float(abs(tau[i]) / rob.eff[i] * 100), 1) for i, n in enumerate(info.arm_joints)},
                          "shoulder_torque_nm": round(float(abs(tau[si])), 3),
                          "shoulder_pct": round(float(abs(tau[si]) / rob.eff[si] * 100), 1),
                          "sag_mm": round(sag, 2), "ok": True})
        curve.append({"payload_g": round(max_payload_g + 10, 0), "ok": False, "limit": lim})
    metrics = {
        "success": bool(max_payload_g >= design.object.mass_g * 1.0),
        "max_payload_g": round(max_payload_g, 0), "limited_by": lim,
        "saturated": st["fail_t"] is not None, "sag_at_limit_mm": round(st["sag"], 2),
        "static_estimate_g": round((est + mobj) * 1000.0, 0), "static_limiting_joint": j_lim,
        "rated_payload_g": design.object.mass_g,
        "payload_margin": round(max_payload_g / max(design.object.mass_g, 1e-6), 2),
        "grip_limited_payload_g": round(grip_cap, 0),
        "far_pose_mm": [round(float(v), 1) for v in p_far], "far_radius_mm": round(float(np.linalg.norm(p_far[:2])), 1),
        "test_reach_mm": round(float(np.linalg.norm(p_far[:2])), 1),
        # stretched straight out (tool point at shoulder height, as far as it reaches): what holds at stall, statically
        "stretch_reach_mm": round(float(r_str), 1) if cap_str is not None else None,
        "stretch_payload_g": round(float(np.min(cap_str)) * 1000.0, 0) if cap_str is not None else None,
        "stretch_limited_by": info.arm_joints[int(np.argmin(cap_str))] if cap_str is not None else None,
        "torque_margin": round(max_payload_g / max(design.object.mass_g, 1e-6), 2),
        "curve": curve, "max_torque_pct": run.stats["max_torque_pct"],
    }
    if not metrics["saturated"]:
        metrics["note"] = f"no saturation up to {max_payload_g:.0f} g (ramp cap)"
    if saved is not None:
        metrics["world_ignored"] = True
    return plan, run, metrics


def _workspace(rob: _Robot, design, fast=False):
    """Monte-Carlo joint sampling (wrist roll fixed: it does not move the TCP) -> reachable TCP cloud with every
    link above the table; top-down reach at table grasp height by IK; a kinematic tour for the viewer."""
    mj, m = rob._mj, rob.model
    info = rob.info
    d = rob.dik
    rng = np.random.default_rng(7)
    N = 1500 if fast else 4000
    lo, hi = rob.lo.copy(), rob.hi.copy()
    Qs = rng.uniform(lo, hi, size=(N, rob.n))
    for i, n in enumerate(info.arm_joints):
        if "roll" in n:
            Qs[:, i] = 0.0
    link_ids = [m.body(n).id for n in rob.asm.ordered_links() if m.body(n).id > 0]
    pts = []
    world = len(rob.world_geoms) > 0
    floor, usable = (rob.sz * MM, []) if world else (0.0, None)
    for q in Qs:
        d.qpos[rob.qadr] = q
        mj.mj_kinematics(m, d)
        if d.xpos[link_ids, 2].min() < floor - 1e-3 or d.site_xpos[rob.tcp_site][2] < floor:
            continue
        pts.append(d.site_xpos[rob.tcp_site] / MM)
        if world:                       # world mode: does this pose put the robot into a box?
            mj.mj_collision(m, d)
            g = d.contact.geom[:d.ncon]
            w = np.isin(g, rob.world_geoms)
            r = np.isin(g, rob.robot_geoms)
            usable.append(not bool(((w[:, 0] & r[:, 1]) | (w[:, 1] & r[:, 0])).any()) if d.ncon else True)
    P = np.array(pts) if pts else np.zeros((1, 3))
    rad = np.linalg.norm(P[:, :2], axis=1)
    # top-down reach at table grasp height along the pick direction (inner and outer limits)
    a = math.atan2(design.task.pick_y_mm, design.task.pick_x_mm)
    u = np.array([math.cos(a), math.sin(a), 0.0])
    ok = []
    zg = rob.R + rob.sz if world else rob.R
    for r in np.arange(40.0, 1.15 * rad.max() + 1, 10.0):
        _, ep, er = rob.ik(u * r + [0, 0, zg], _tangent(u[:2]), rob.q_ready(), iters=120)
        if ep < 1.0 and er < 1e-2:
            ok.append(float(r))
    pick, place = _targets(rob, design)
    _, e1, r1 = rob.ik_robust(pick, _tangent(pick[:2]))
    _, e2, r2 = rob.ik_robust(place, _tangent(place[:2]))
    h = _hull(P)
    vol = _hull_volume(*h) / 1e6 if h is not None else 0.0
    rnd = lambda v: round(float(v), 1)  # noqa: E731
    metrics = {
        "success": bool(e1 < 1 and e2 < 1 and len(pts) > 0), "reach_mm": rnd(rad.max()),
        "max_height_mm": rnd(P[:, 2].max()), "reach_3d_mm": rnd(np.linalg.norm(P, axis=1).max()),
        "envelope_mm": {k: [rnd(P[:, i].min()), rnd(P[:, i].max())] for i, k in enumerate("xyz")},
        "volume_l": round(vol, 2), "samples": int(N), "valid_fraction": round(len(pts) / N, 3),
        "top_down_reach_mm": [min(ok), max(ok)] if ok else None,
        "pick_reachable": bool(e1 < 1 and r1 < 1e-2), "place_reachable": bool(e2 < 1 and r2 < 1e-2),
        "design_reach_mm": _sized_reach(rob, design), "min_reach_mm": min(ok) if ok else None,
        "height_mm": [rnd(P[:, 2].min()), rnd(P[:, 2].max())],
        "yaw_deg": rnd(math.degrees(float(hi[0] - lo[0]))), "points": int(len(pts)),
    }
    # viewer tour: reach out, sweep the base yaw across its range, stand up, back to ready
    sc = 0.7 if fast else 1.0
    q_r = rob.q_ready()
    plan = _Plan(rob, q_r, info.grip_open, "sample")
    p_out = u * (0.85 * rad.max()) + [0, 0, 120.0]
    q_out = rob.ik(p_out, _tangent(u[:2]), q_r, iters=150)[0]
    left, right = q_out.copy(), q_out.copy()
    left[0], right[0] = lo[0] * 0.8, hi[0] * 0.8
    for q1, T in ((q_out, 1.4), (left, 1.6), (right, 2.6), (np.clip(np.zeros(rob.n), lo, hi), 1.8), (q_r, 1.4)):
        plan.joint(q1, T * sc, "sample")
        plan.hold(0.15, "sample")
    plan.mark(f"reach {metrics['reach_mm']:.0f} mm", 1.4 * sc + 0.15)
    frames = _kinematic_frames(rob, plan, design)
    taus = np.abs(np.array([f["tau"] for f in frames]))
    metrics["max_torque_pct"] = {n: round(float(taus[:, i].max() / rob.eff[i] * 100.0), 1)
                                 for i, n in enumerate(info.arm_joints)}
    if world:                           # the cloud shows only the poses that stay clear of the room
        keep = np.asarray(usable, bool)
        metrics["usable_workspace_fraction"] = round(float(keep.mean()), 3) if len(keep) else 0.0
        metrics["world"] = rob.opts.world.get("id", "")
        P = P[keep] if keep.any() else np.zeros((0, 3))
        pts = P.tolist()
    sub = P[rng.choice(len(P), size=min(len(P), 1500), replace=False)] if pts else np.zeros((0, 3))
    return plan, _Run(frames, {}), metrics, [[round(float(v), 1) for v in p] for p in sub]


def _kinematic_frames(rob, plan, design):
    """Frames from a joint trajectory without dynamics: tau = inverse dynamics (gravity + inertia)."""
    mj, m, d = rob._mj, rob.model, rob.dik
    tq, Q = np.asarray(plan.t), np.asarray(plan.q)
    QD = np.gradient(Q, PLAN_DT, axis=0)
    QDD = np.gradient(QD, PLAN_DT, axis=0)
    pick, _ = _targets(rob, design)
    yaw = math.atan2(pick[1], pick[0])
    qb = [math.cos(yaw / 2), 0.0, 0.0, math.sin(yaw / 2)]
    oq = [round(float(v), 5) for v in _q_json(qb)]
    frames = []
    oz = float(pick[2]) if rob.opts.world is not None else rob.R
    for t in np.arange(0.0, tq[-1] + 1e-9, FRAME_DT):
        k = min(int(round(t / PLAN_DT)), len(tq) - 1)
        d.qpos[:] = 0
        d.qvel[:] = 0
        d.qacc[:] = 0
        d.qpos[rob.qadr] = Q[k]
        d.qvel[rob.dadr] = QD[k]
        d.qacc[rob.dadr] = QDD[k]
        rob.set_grip(d, plan.g[k])
        d.qpos[rob.obj_q:rob.obj_q + 3] = [pick[0] * MM, pick[1] * MM, oz * MM]
        d.qpos[rob.obj_q + 3:rob.obj_q + 7] = qb
        mj.mj_inverse(m, d)
        tau = np.clip(d.qfrc_inverse[rob.dadr], -rob.eff, rob.eff)
        q = [round(float(v), 4) for v in Q[k]] + ([round(plan.g[k], rob.g_digits)] if rob.g_q else [])
        frames.append({"t": round(float(t), 4), "q": q, "obj": [round(float(pick[0]), 2), round(float(pick[1]), 2),
                                                                  round(float(oz), 2)] + oq,
                       "tau": [round(float(v), 4) for v in tau], "grip_n": 0.0, "phase": plan.ph[k]})
    return frames


def _shake(rob: _Robot, design, fast=False):
    info = rob.info
    s = 0.85
    pick, _ = _targets(rob, design)
    ax = _pick_axis(rob, pick)
    hover = _hover_mm(rob, pick, ax)
    above = pick + [0, 0, hover]
    q_above, e1, _ = rob.ik_robust(above, ax)
    plan = _Plan(rob, q_above, info.grip_open, "approach")
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
    A_ax, A_z = 20.0, 4.0
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
    opening = _grip_times(rob)[1]
    plan.hold(0.1, "release", grip=info.grip_open)
    plan.hold(0.5 * opening, "release")
    plan.cart(lambda u: (_line(pick + [0, 0, 1.5], pick + [0, 0, 5.0])(u), ax), 0.5 * opening + 0.1, "release")
    plan.cart(lambda u: (_line(pick + [0, 0, 5.0], above)(u), ax), 0.8, "retreat")
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
    _label_grip(plan, run)
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
    if e1 >= 2:
        metrics["note"] = f"pick target out of top-down reach (IK misses by {e1:.0f} mm)"
    metrics.update(_damage(design, run, metrics))
    metrics.update({"max_accel_ms2": metrics["peak_accel_ms2"], "max_slip_mm": metrics["slip_mm"],
                    "min_grip_n": metrics["grip_n_min"], "max_grip_n": metrics["grip_n_max"]})
    if not metrics["intact"]:
        metrics["success"] = False
    if len(rob.world_geoms):
        metrics["world_contacts"] = run.stats.get("world_contacts") or []
        if metrics["world_contacts"]:
            metrics["success"] = False
    return plan, run, metrics


# ====================================================================== entry points

def _scenario_doc(rob, design, name, plan, run, metrics, points=None) -> dict:
    pick, place = _targets(rob, design)
    info = rob.info
    out = {
        "scenario": name, "title": TITLES[name], "dt": round(FRAME_DT, 4),
        "joints": list(info.arm_joints) + ([info.gripper_joint] if info.gripper_joint else []),
        "frames": run.frames, "markers": sorted(plan.markers, key=lambda mk: mk["t"]),
        "object": dict(info.object),
        "targets": {"pick": [round(float(v), 2) for v in pick], "place": [round(float(v), 2) for v in place]},
        "metrics": metrics,
        "info": {"timestep_s": rob.model.opt.timestep, "grip_force_n": round(info.grip_force_n, 3),
                 "efforts": {k: round(v, 3) for k, v in info.efforts.items()},
                 "collision_geoms": len(info.geoms), "object_excluded_parts": info.excluded,
                 "dropped_parts": info.dropped_parts[:40], "gripper": {
                     "kind": info.grip_kind, "contact": round(info.grip_contact, 4), "open": round(info.grip_open, 4),
                     "grasp_offset_mm": [round(float(v), 2) for v in info.grasp_offset],
                     "fingertip_beyond_grasp_mm": round(info.tip_mm, 1)},
                 "notes": list(info.notes),
                 "units": "mm, rad, N, N*m; gripper value in the joint's unit (mm prismatic, rad hinge); "
                          "obj = position mm + quaternion w,x,y,z with the object's long axis on local z"},
    }
    if points is not None:
        out["points"] = points
    if rob.opts.world is not None:
        out["world"] = rob.opts.world
    return out


def _run_one(rob, design, name, fast, perturb=None):
    if perturb and name != "pick_place":
        raise ValueError("perturb: robustness trials run the pick_place scenario")
    if name == "pick_place":
        plan, run, metrics = _pick_place(rob, design, fast, perturb)
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
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, separators=(",", ":"))
    return path


def run(asm, design, scenario: str, outdir: str | None = None, fast: bool = False, perturb: dict | None = None,
        **opts) -> dict:
    """Run one scenario; writes ``outdir/sim_<scenario>.json`` and ``outdir/robot.xml`` when outdir is given.
    Returns the scenario document (same content as the JSON). ``perturb``: one robustness trial of pick_place (see
    _perturb_start); the plan stays the nominal one."""
    rob = _make_robot(asm, design, fast, opts)
    t0 = time.time()
    doc = _run_one(rob, design, scenario, fast, perturb)
    doc["info"]["wall_s"] = round(time.time() - t0, 2)
    if outdir:
        os.makedirs(outdir, exist_ok=True)
        with open(os.path.join(outdir, "robot.xml"), "w", encoding="utf-8") as fh:
            fh.write(rob.xml)
        _write(outdir, scenario, doc)
    return doc


def run_all(asm, design, outdir: str, scenarios=None, fast: bool = False, **opts) -> dict:
    """Run the scenarios (default: all four), write sim_<name>.json + robot.xml into outdir."""
    os.makedirs(outdir, exist_ok=True)
    rob = _make_robot(asm, design, fast, opts)
    with open(os.path.join(outdir, "robot.xml"), "w", encoding="utf-8") as fh:
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

def _render_iter(asm, design, sim, times, size, camera, fast):
    """Yield (frame, RGB image) for the frames nearest to ``times`` (posed kinematically from the JSON)."""
    import mujoco

    if isinstance(sim, str):
        with open(sim, encoding="utf-8") as fh:
            sim = json.load(fh)
    rob = _make_robot(asm, design, fast, {"world": sim["world"]} if sim.get("world") else {})   # world mode: the boxes too
    m, d = rob.model, mujoco.MjData(rob.model)
    r = mujoco.Renderer(m, size[1], size[0])
    cam = mujoco.MjvCamera()
    camera = camera or {}
    cam.lookat[:] = camera.get("lookat", [0.12, 0.0, 0.12])
    cam.distance = camera.get("distance", 0.75)
    cam.azimuth = camera.get("azimuth", 215)
    cam.elevation = camera.get("elevation", -22)
    frames = sim["frames"]
    tt = np.array([f["t"] for f in frames])
    try:
        for t in times:
            f = frames[int(np.argmin(np.abs(tt - t)))]
            d.qpos[:] = 0
            d.qpos[rob.qadr] = f["q"][:rob.n]
            if rob.g_q:
                rob.set_grip(d, f["q"][-1])
            d.qpos[rob.obj_q:rob.obj_q + 3] = np.asarray(f["obj"][:3]) * MM
            d.qpos[rob.obj_q + 3:rob.obj_q + 7] = _q_body(f["obj"][3:7])
            mujoco.mj_forward(m, d)
            r.update_scene(d, cam)
            yield f, r.render()
    finally:
        r.close()


def render_frames(asm, design, sim: dict | str, times, path_pattern: str, size=(640, 480), camera=None,
                  fast=False) -> list:
    """Render scenario frames to PNG with mujoco.Renderer (set MUJOCO_GL=egl or osmesa when headless).
    ``sim`` is a scenario document or the path of a sim_<name>.json; ``path_pattern`` like 'out_{i}.png'
    (``{t}`` is also available); ``camera`` = {"lookat": [x, y, z] m, "distance", "azimuth", "elevation"}."""
    from PIL import Image

    outs = []
    for i, (f, img) in enumerate(_render_iter(asm, design, sim, times, size, camera, fast)):
        path = path_pattern.format(i=i, t=f["t"])
        Image.fromarray(img).save(path)
        outs.append(path)
    return outs


def render_gif(asm, design, sim: dict | str, path: str, fps: float = 15.0, size=(480, 360), camera=None) -> str:
    """Animated GIF of a scenario (every frame at ``fps``), for reports and quick previews."""
    from PIL import Image

    if isinstance(sim, str):
        with open(sim, encoding="utf-8") as fh:
            sim = json.load(fh)
    t_end = sim["frames"][-1]["t"]
    times = np.arange(0.0, t_end + 1e-9, 1.0 / fps)
    imgs = [Image.fromarray(img) for _, img in _render_iter(asm, design, sim, times, size, camera, False)]
    imgs[0].save(path, save_all=True, append_images=imgs[1:], duration=int(1000 / fps), loop=0)
    return path
