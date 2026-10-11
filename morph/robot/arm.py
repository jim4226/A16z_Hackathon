"""Parametric robot arm + sensorized gripper generator.

`size(design)` picks actuators by static torque in the worst-case (horizontal) pose and lays out
the link lengths; `build(design, sizing, electronics_plan)` generates the whole robot as an
`Assembly`: printed / machined / sheet-metal / moulded structure (per `design.build.config`),
purchased actuators, horns, bearings, fasteners and electronics, a rack-and-pinion force-sensing
gripper, routed cables, and an assembly sequence written like a real manual.

Kinematic contract (every generator must follow it; sim + viewer rely on it)
---------------------------------------------------------------------------
Links:  base -> turret -> upper_arm -> forearm -> wrist [-> wrist_yaw] -> gripper_base
        gripper_base -> jaw_left, jaw_right, pinion, tcp
Joints (zero pose = arm pointing straight up, gripper fingers pointing +z):
  j1_base_yaw     base->turret          revolute, axis +z
  j2_shoulder     turret->upper_arm     revolute, axis +y
  j3_elbow        upper_arm->forearm    revolute, axis +y
  j4_wrist_pitch  forearm->wrist        revolute, axis +y
  j5_wrist_yaw    wrist->wrist_yaw      revolute, axis +x, only when arm.dof == 6
  j5_wrist_roll   (dof 5) / j6_wrist_roll (dof 6): wrist(_yaw)->gripper_base revolute, axis +z;
                  dof == 4 -> fixed joint "wrist_fixed"
  gripper         gripper_base->jaw_left    prismatic, axis -x, value = mm opened beyond contact with the
                                            design object (0 = touching it, negative = squeezing/overtravel)
  gripper_right   gripper_base->jaw_right   prismatic, axis +x, mimic gripper x1
  gripper_pinion  gripper_base->pinion      revolute, axis +y, mimic gripper x(1/pitch_radius)
  tcp_fixed       gripper_base->tcp         fixed; tcp = object centre when grasped, +z = approach direction
assembly.meta: {"arm_joints": [...], "gripper_joint": "gripper", "tcp_link": "tcp",
                "home": {joint: value}, "ready": {joint: value}, "sizing": {...}, ...}

Geometry conventions used here
------------------------------
Every actuated pitch joint is built in a canonical "joint frame": origin on the joint axis, axis +y,
child link towards +z, the actuator body hanging towards -z inside the parent link. The actuator's
output faces -y (so its printed label faces the front, +x), the child link is a clevis whose horn
cheek bolts to the horn (-y side) and whose idler cheek bolts to the idler / rides on a bearing (+y
side): dual-sided support at every pitch joint. A "dual" joint (two actuators back to back, both
horns driving the clevis) is used automatically when one actuator cannot hold the shoulder/elbow.
"""

from __future__ import annotations

import functools
import math
from dataclasses import dataclass, field

import manifold3d as m3d
import numpy as np
import trimesh
from shapely import affinity
from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import unary_union

from . import catalog, components
from .assembly import Assembly, Cable, Joint, Part, Step, rot, trans
from .. import mechanism as _mech
from ..design import FSR, build_profile, mount_dims
from ..mechanism import object_center_v, pinion_profile, rack_teeth

G = 9.81
MF = m3d.Manifold
CS = m3d.CrossSection

# ------------------------------------------------------------------ palette / build configs
C_WHITE = "#e9ebee"        # printed PETG, warm white
C_CHAR = "#2c3037"         # charcoal accents (fingers, trim)
C_ORANGE = "#ff6a1a"       # accent / TPU pads
C_ANOD = "#c5cad1"         # clear-anodised 6061
C_ANOD_DK = "#3f444c"      # dark-anodised 6061
C_SHEET = "#26292e"        # black powder-coated sheet metal
C_SHEET_RAW = "#a9afb7"    # brushed / zinc sheet (production internals)
C_MJF = "#4e535b"          # MJF PA12, dyed grey
C_PC = "#f3f4f6"           # moulded PC/ABS, gloss white
C_PC_DK = "#353940"        # moulded dark grey
C_RUBBER = "#1c1d20"
C_STEEL = "#b7bcc3"
C_BRASS = "#c9a25b"
C_FR4_BLACK = "#15171a"

CONFIGS = ("prototype", "pilot", "production")

T_CH = 4.0        # clevis cheek thickness
WALL = 3.0        # cradle wall
FLOOR = 3.5       # cradle floor under the actuator body
T_BR = 4.0        # clevis bridge thickness
T_FLANGE = 5.0    # hand flange
SHEET_T = 1.5     # sheet-metal brackets
PLATE_T = 4.0     # pilot CNC side plates
MOLD_T = 2.2      # moulded cover wall

STD_LEN = (3, 4, 5, 6, 8, 10, 12, 14, 16, 18, 20, 25, 30, 35, 40, 45, 50)


def _std_len(L):
    return next((s for s in STD_LEN if s >= L - 0.25), STD_LEN[-1])


# ------------------------------------------------------------------ manifold helpers
def _tri(man) -> trimesh.Trimesh:
    if man is None or man.is_empty():
        return trimesh.Trimesh()
    m = man.to_mesh()
    v = np.asarray(m.vert_properties, dtype=np.float64)[:, :3]
    f = np.asarray(m.tri_verts, dtype=np.int64)
    return trimesh.Trimesh(v, f, process=False)


def _man(mesh: trimesh.Trimesh):
    return MF(m3d.Mesh(vert_properties=np.ascontiguousarray(mesh.vertices, dtype=np.float32),
                       tri_verts=np.ascontiguousarray(mesh.faces, dtype=np.uint32)))


def _xf(man, T):
    return man.transform(np.asarray(T, dtype=float)[:3, :])


def _mv(man, x=0.0, y=0.0, z=0.0):
    return man.translate((float(x), float(y), float(z)))


def _flat(items):
    out = []
    for it in items:
        if it is None:
            continue
        if isinstance(it, (list, tuple)):
            out += _flat(it)
        elif not it.is_empty():
            out.append(it)
    return out


def _U(*items):
    ms = _flat(items)
    if not ms:
        return MF()
    return ms[0] if len(ms) == 1 else MF.batch_boolean(ms, m3d.OpType.Add)


def _D(base, *cutters):
    cs = _flat(cutters)
    if base is None or base.is_empty() or not cs:
        return base
    return MF.batch_boolean([base] + cs, m3d.OpType.Subtract)


def _I(a, b):
    return MF.batch_boolean([a, b], m3d.OpType.Intersect)


def _hull(*items):
    ms = _flat(items)
    return MF.batch_hull(ms) if ms else MF()


def _segs(r, lo=16, hi=72):
    n = int(round(2 * math.pi * max(float(r), 0.1) / 1.2))
    return int(math.ceil(max(lo, min(hi, n)) / 4) * 4)


def _cyl(r, h, z0=0.0, xy=(0.0, 0.0), segs=None, r2=None):
    m = MF.cylinder(float(h), float(r), -1.0 if r2 is None else float(r2), segs or _segs(max(r, r2 or 0)))
    return _mv(m, xy[0], xy[1], z0)


def _box(lx, ly, lz, c=(0.0, 0.0, 0.0)):
    return _mv(MF.cube((float(lx), float(ly), float(lz)), True), *c)


def _box2(x0, x1, y0, y1, z0, z1):
    return _box(x1 - x0, y1 - y0, z1 - z0, ((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2))


def _rr_pts(lx, ly, r, n=6):
    r = max(0.0, min(r, lx / 2 - 1e-3, ly / 2 - 1e-3))
    pts = []
    for cx, cy, a0 in ((lx / 2 - r, -ly / 2 + r, -90), (lx / 2 - r, ly / 2 - r, 0), (-lx / 2 + r, ly / 2 - r, 90),
                       (-lx / 2 + r, -ly / 2 + r, 180)):
        if r <= 1e-3:
            pts.append((cx, cy))
            continue
        for k in range(n + 1):
            a = math.radians(a0 + 90.0 * k / n)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return np.array(pts, dtype=float)


def _edge_layers(h, z0, f, steps):
    if f <= 0:
        return [(z0, 0.0), (z0 + h, 0.0)]
    out = []
    for k in range(steps + 1):
        th = (math.pi / 2) * k / steps
        out.append((z0 + f * (1 - math.cos(th)), f * (1 - math.sin(th))))
    top = [(z0 + h - (z - z0), i) for z, i in out][::-1]
    return out + top


def _rbox(lx, ly, lz, c=(0.0, 0.0, 0.0), r=2.0, f=0.8, steps=3, n=5):
    """Box centred at c: vertical edges rounded (r), top / bottom edges filleted (f)."""
    f = min(f, lz / 2 - 1e-3, lx / 2 - 1e-3, ly / 2 - 1e-3) if f > 0 else 0.0
    pts = []
    for z, i in _edge_layers(lz, c[2] - lz / 2, f, steps):
        p = _rr_pts(lx - 2 * i, ly - 2 * i, max(r - i, 0.0), n) + np.asarray(c[:2], float)
        pts.append(np.column_stack([p, np.full(len(p), z)]))
    return MF.hull_points(np.vstack(pts))


def _rbox2(x0, x1, y0, y1, z0, z1, r=2.0, f=0.8, steps=3):
    return _rbox(x1 - x0, y1 - y0, z1 - z0, ((x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2), r, f, steps)


def _rev(pts, segs=48):
    poly = Polygon(pts).buffer(0)
    return MF.revolve(_cs(poly), segs)


def _fcyl(r, h, f=1.0, z0=0.0, segs=None, steps=3):
    """Cylinder along z (z0..z0+h) with filleted rims."""
    f = max(0.0, min(f, r * 0.45, h * 0.45))
    pts = [(0.0, 0.0)]
    for z, i in _edge_layers(h, 0.0, f, steps):
        pts.append((r - i, z))
    pts.append((0.0, h))
    return _mv(_rev(pts, segs or _segs(r)), 0, 0, z0)


R_ZY = rot((1, 0, 0), -math.pi / 2)    # +z -> +y
R_ZX = rot((0, 1, 0), math.pi / 2)     # +z -> +x


def _ycyl(r, y0, y1, f=1.0, x=0.0, z=0.0, segs=None):
    """Cylinder along y from y0 to y1 (axis through (x, ., z)), filleted rims."""
    return _xf(_fcyl(r, y1 - y0, f, 0.0, segs), trans((x, y0, z)) @ R_ZY)


def _cs(geom):
    if isinstance(geom, CS):
        return geom
    polys = [geom] if geom.geom_type == "Polygon" else [g for g in getattr(geom, "geoms", []) if g.geom_type == "Polygon"]
    rings = []
    for p in polys:
        if p.is_empty or p.area < 1e-8:
            continue
        from shapely.geometry.polygon import orient
        p = orient(p, 1.0)
        rings.append(np.asarray(p.exterior.coords)[:-1])
        rings += [np.asarray(r.coords)[:-1] for r in p.interiors]
    return CS(rings, m3d.FillRule.EvenOdd)


def _ext(poly, z0, z1):
    return _mv(MF.extrude(_cs(poly), float(z1 - z0)), 0, 0, z0)


def _plate(poly, t, fillet=0.0, chamfer=0.0):
    """Extruded profile (z in [0, t]) with filleted / chamfered edges (components.trimesh_from_profile)."""
    try:
        m = components.trimesh_from_profile(poly, t, chamfer=chamfer or 0.0, fillet=fillet or 0.0)
        man = _man(m)
        if not man.is_empty():
            return man
    except Exception:  # noqa: BLE001
        pass
    return _ext(poly, 0.0, t)


# profile (u, v, w in [0, t]) -> link: u -> x, v -> z, w -> -y  (det +1);  y range [y1 - t, y1]
def _xz_frame(y1):
    return np.array([[1, 0, 0, 0], [0, 0, -1, y1], [0, 1, 0, 0], [0, 0, 0, 1.0]])


def _xz_ext(poly, y0, y1, fillet=0.0):
    man = _plate(poly, y1 - y0, fillet=fillet) if fillet else _ext(poly, 0.0, y1 - y0)
    return _xf(man, _xz_frame(y1))


def _frame(origin, z, x):
    """4x4 with +z along `z`, +x along `x` (orthogonalised)."""
    z = np.asarray(z, float)
    z = z / np.linalg.norm(z)
    x = np.asarray(x, float) - np.dot(x, z) * z
    x = x / np.linalg.norm(x)
    T = np.eye(4)
    T[:3, 0], T[:3, 1], T[:3, 2], T[:3, 3] = x, np.cross(z, x), z, origin
    return T


def _pt(T, p):
    return (np.asarray(T, float) @ np.r_[np.asarray(p, float), 1.0])[:3]


def _vec(T, v):
    return np.asarray(T, float)[:3, :3] @ np.asarray(v, float)


# ------------------------------------------------------------------ actuator geometry
@dataclass
class _Act:
    key: str
    spec: dict
    L: float
    W: float
    H: float
    so: float
    zt: float          # case top (component z, < 0)
    zb: float          # case bottom
    zi: float          # idler face (= zb for hobby)
    ht: float          # horn thickness (seat -> face)
    horn_d: float
    hobby: bool
    frames: dict
    holes: dict
    bounds: dict       # part suffix -> bounds (component frame)
    horn_holes: list   # [(x, y, d)] in the horn face plane
    horn_pcd: float

    @property
    def overall(self):
        return -min(self.zb, self.zi)

    @property
    def mass_g(self):
        return float(self.spec.get("mass_g", 50.0))

    @property
    def torque(self):
        return float(self.spec.get("torque_nm", 1.0))


@functools.lru_cache(maxsize=None)
def _act(key: str) -> _Act:
    c = components.actuator(key, horn=False)
    spec = catalog.ACTUATORS[key]
    d = c.dims
    hd = components.horn(key).dims
    return _Act(key=key, spec=spec, L=float(d["l"]), W=float(d["w"]), H=float(d["h"]), so=float(d["shaft_off"]),
                zt=float(d["case_top_z"]), zb=float(d["case_bottom_z"]), zi=float(c.frames["idler"][2, 3]),
                ht=float(d["horn_t"]), horn_d=float(d["horn_d"]), hobby=spec.get("family") == "hobby_pwm",
                frames={k: np.array(v) for k, v in c.frames.items()}, holes=d.get("holes", {}),
                bounds={s: m.bounds.copy() for s, m, _, _ in c.parts},
                horn_holes=[(h[0], h[1], h[3]) for h in hd["holes"]], horn_pcd=float(hd["pcd"]))


# component (x, y, z) -> canonical joint frame (-y, y_h - z, x): output -y, body towards -z
_R_CAN = np.array([[0, -1, 0, 0], [0, 0, -1, 0], [1, 0, 0, 0], [0, 0, 0, 1.0]])


def _t_can(y_h):
    return trans((0, y_h, 0)) @ _R_CAN


def _screw_size(hole_d):
    return "M2" if hole_d <= 1.75 else ("M2.5" if hole_d <= 2.45 else "M3")


# ------------------------------------------------------------------ joint geometry (canonical frame)
@dataclass
class _JG:
    name: str
    key: str
    count: int
    A: _Act
    T_acts: list
    y_h: float          # horn face (first actuator; < 0)
    y_i: float          # inner face of the +y cheek (idler side, or 2nd horn face for dual)
    D2: float           # outer half width: cheeks at [-D2, y_h] and [y_i, D2]
    case_y: tuple
    r_ch: float
    r_cut: float
    z_b: float
    hw: float
    z_floor: float
    z_body: tuple
    z_fing: float
    neck: float
    tongue: tuple | None
    ear: dict | None
    conn_box: list = field(default_factory=list)   # canonical bboxes of connector regions
    lim: tuple = (-1.8, 1.8)

    @property
    def z_cup(self):
        return -self.r_cut


def _canon_box(Tc, b):
    corners = np.array([[x, y, z] for x in b[:, 0] for y in b[:, 1] for z in b[:, 2]])
    P = (Tc[:3, :3] @ corners.T).T + Tc[:3, 3]
    return np.array([P.min(0), P.max(0)])


def _jg(name, key, count=1) -> _JG:
    A = _act(key)
    if count == 2:
        prot = max(0.0, A.zb - A.zi)
        gap = 2 * prot + 2.0
        y_h = -gap / 2 + A.zi
        T_acts = [_t_can(y_h), rot((0, 0, 1), math.pi) @ _t_can(y_h)]
        y_i = -y_h
        case_y = (y_h - A.zt, -(y_h - A.zt))
        D2 = T_CH - y_h
        tongue = None
    else:
        y_h = (A.zt + A.zb) / 2
        T_acts = [_t_can(y_h)]
        case_y = (y_h - A.zt, y_h - A.zb)
        if A.hobby:
            tongue = (case_y[1] + 0.4, case_y[1] + 4.4)
            y_i = tongue[1] + 0.6
            D2 = max(T_CH - y_h, y_i + 8.5)
        else:
            tongue = None
            y_i = y_h - A.zi
            D2 = max(T_CH - y_h, y_i + T_CH)
    r_ch = A.horn_d / 2 + 5.0
    r_cut = r_ch + 1.5
    hw = A.W / 2 + WALL
    zb0, zb1 = -A.so - A.L / 2, -A.so + A.L / 2
    ear = None
    z_low = zb0
    r_top = math.hypot(A.W / 2 + 0.4, zb1 + 0.4)
    z_fing = zb1 - 4.0
    if A.hobby and "ears" in A.bounds:
        eb = A.bounds["ears"]
        # ear flange: component z in [eb_z0, eb_z1] -> canonical y in [y_h - z1, y_h - z0]; along x -> canonical z
        ear = dict(z0=float(eb[0][0]), z1=float(eb[1][0]), hw=float(eb[1][1]), y0=y_h - float(eb[1][2]),
                   y1=y_h - float(eb[0][2]))
        z_low = min(z_low, ear["z0"])
        r_top = max(r_top, math.hypot(hw, ear["z1"] + 1.5))
        z_fing = ear["z1"] + 1.5
    else:
        r_top = max(r_top, math.hypot(hw, z_fing))
    z_b = max(r_top + 2.5, r_ch + 2.0)
    conn = []
    for Tc in T_acts:
        for s in ("connectors", "grommet"):
            if s in A.bounds:
                conn.append(_canon_box(Tc, A.bounds[s]))
    return _JG(name=name, key=key, count=count, A=A, T_acts=T_acts, y_h=y_h, y_i=y_i, D2=D2, case_y=case_y, r_ch=r_ch,
               r_cut=r_cut, z_b=z_b, hw=hw, z_floor=z_low - FLOOR, z_body=(zb0, zb1), z_fing=z_fing,
               neck=max(r_ch - 1.0, hw - 2.0), tongue=tongue, ear=ear, conn_box=conn)


# ------------------------------------------------------------------ gripper (rack and pinion) plan
def _grip_plan(design, gkey: str) -> dict:
    """mechanism.plan generalised to any actuator: same racks / pinion / travel / overtravel, servo laid flat
    behind the back plate (shaft on the pinion axis), hand flange on top. Mechanism frame: x = jaw travel,
    y = up (towards the wrist), z = towards the back plate, object centre at the origin."""
    M = _mech
    geom = build_profile(design)
    f = design.finger
    m = M.MODULE
    n = max(10, round(2 * design.electronics.pinion_radius_mm / m))
    r = n * m / 2
    ra = r + m
    cv = object_center_v(design, geom)
    mount_h, mount_w = mount_dims(f)
    u_c, w = geom.pad_center_u, f.width_mm
    issues = []
    T = min(15.0, cv - mount_w - 2 * M.OVERTRAVEL - M.CLEAR - 2 * m - 1)
    if T < 4:
        issues.append(f"only {T:.1f} mm of jaw travel fits around a Ø{design.object.diameter_mm:g} mm object")
        T = max(T, 1.0)
    if -cv + mount_w + M.OVERTRAVEL > -(ra + M.CLEAR):
        issues.append("left finger's rack arm would hit the pinion; object too small for this layout")
    y_mount_top = u_c + mount_h
    yB_bot = y_mount_top + 0.6
    yB_root = yB_bot + M.RACK_H
    y_p = yB_root + 1.25 * m + r
    yA_root = y_p + r + 1.25 * m
    yA_top = yA_root + M.RACK_H
    xAr = T + 2 * m + 1
    xBl = -xAr

    def rack_phase(contact_deg):
        pitch_deg = 360.0 / n
        k = round(contact_deg / pitch_deg)
        eps = math.radians(k * pitch_deg - contact_deg)
        x_tooth = -r * eps if contact_deg == 90 else r * eps
        return x_tooth + math.pi * m / 2

    ear_A = box(-cv, u_c, -cv + mount_w, yA_root + 0.5)
    ear_B = box(cv - mount_w, u_c, cv, yB_bot + 0.5)
    body_A = box(-cv, yA_root, xAr, yA_top)
    teeth_A = rack_teeth(-cv + mount_w + 0.8, xAr, y_p + r, -1, rack_phase(90))
    body_B = box(xBl, yB_bot, cv, yB_root)
    teeth_B = rack_teeth(xBl, cv - mount_w - 0.8, y_p - r, +1, rack_phase(270))
    b1 = -cv + 3 + M.OVERTRAVEL + 1.7
    b2 = max(xAr - 2 - T - 1.7, b1 + 6.0)
    yA_mid, yB_mid = (yA_root + yA_top) / 2, (yB_bot + yB_root) / 2
    slot_A = box(b1 - M.OVERTRAVEL - 1.7, yA_mid - 1.7, b2 + T + 1.7, yA_mid + 1.7)
    slot_B = affinity.translate(affinity.scale(slot_A, xfact=-1, origin=(0, 0)), 0, yB_mid - yA_mid)
    holes_A = [Point(-cv + mount_w / 2, u_c + mount_h / 2 + s * f.mount_hole_spacing_mm / 2).buffer(1.7, quad_segs=12)
               for s in (-1, 1)]
    holes_B = [affinity.scale(h, xfact=-1, origin=(0, 0)) for h in holes_A]
    rack_A = unary_union([body_A, teeth_A, ear_A]).difference(slot_A).difference(unary_union(holes_A))
    rack_B = unary_union([body_B, teeth_B, ear_B]).difference(slot_B).difference(unary_union(holes_B))
    boss_A = box(-cv, u_c, -cv + mount_w, y_mount_top).difference(unary_union(holes_A))
    boss_B = affinity.scale(boss_A, xfact=-1, origin=(0, 0))
    A = _act(gkey)
    pin = pinion_profile(n).difference(Point(0, 0).buffer(1.7, quad_segs=12))
    hub_r = min(r - 1.25 * m - 1.0, A.horn_pcd / 2 + 3.2)
    for hx, hy, hd in A.horn_holes:
        if math.hypot(hx, hy) + hd / 2 + 0.8 < r - 1.25 * m:
            pin = pin.difference(Point(hx, hy).buffer(max(hd / 2, 1.1) + 0.15, quad_segs=10))
    zf = w / 2
    z_boss = (zf, zf + M.BOSS_T)
    z_rack = (zf + M.BOSS_T, zf + M.BOSS_T + M.RACK_T)
    z_plate = (z_rack[1] + 0.3, z_rack[1] + 0.3 + M.PLATE_T)
    # servo: output towards -z (the pinion), body laid along -x, case top against the plate back if the horn reaches
    z_hf = z_plate[1] + A.zt
    if z_hf < z_rack[1] + 0.1:
        z_hf = z_rack[1] + 0.1
    z_case_top = z_hf - A.zt
    servo_frame = z_case_top > z_plate[1] + 0.05          # needs a housing behind the plate (hobby servos)
    px = max(cv + T + 4.0, A.so + A.L / 2 + 5.0, A.L / 2 - A.so + 5.0)
    y_top = max(yA_top + 1.5, y_p + A.W / 2 + 3.0) + T_FLANGE
    if A.hobby and "ears" in A.bounds:
        eb = A.bounds["ears"]
        y_top = max(y_top, y_p + float(eb[1][1]) + 3.0 + T_FLANGE)
    py0 = yB_bot - 3.0
    return dict(geom=geom, servo=gkey, A=A, n=n, r=r, ra=ra, cv=cv, T=T, overtravel=M.OVERTRAVEL, u_c=u_c, w=w,
                mount_h=mount_h, mount_w=mount_w, y_mount_top=y_mount_top, yB_bot=yB_bot, yB_root=yB_root, y_p=y_p,
                yA_root=yA_root, yA_top=yA_top, xAr=xAr, b1=b1, b2=b2, yA_mid=yA_mid, yB_mid=yB_mid,
                rack_A=rack_A, rack_B=rack_B, boss_A=boss_A, boss_B=boss_B, pinion=pin, hub_r=hub_r,
                z_boss=z_boss, z_rack=z_rack, z_plate=z_plate, z_hf=z_hf, z_case_top=z_case_top,
                servo_frame=servo_frame, px=px, y_top=y_top, py0=py0, tcp_z=y_top,
                tip=f.length_mm - u_c, issues=issues)


def _mech_to_gb(tcp_z):
    """mechanism frame -> gripper_base frame: x -> x, y (up) -> -z, z (to plate) -> +y, origin -> (0, 0, tcp_z)."""
    return np.array([[1, 0, 0, 0], [0, 0, 1, 0], [0, -1, 0, tcp_z], [0, 0, 0, 1.0]])


# ------------------------------------------------------------------ layout
def arm_joint_names(dof: int) -> list:
    names = ["j1_base_yaw", "j2_shoulder", "j3_elbow", "j4_wrist_pitch"]
    if dof >= 6:
        names.append("j5_wrist_yaw")
    if dof >= 5:
        names.append("j5_wrist_roll" if dof == 5 else "j6_wrist_roll")
    return names


def _extras(design) -> set:
    return {e.strip() for e in (design.electronics.extras or "").split(",") if e.strip()}


def _config(design) -> str:
    c = (getattr(design.build, "config", "prototype") or "prototype").lower()
    return c if c in CONFIGS else "prototype"


def _family(design) -> list:
    fam = design.arm.actuator_family
    keys = catalog.FAMILY_ACTUATORS.get(fam) or catalog.FAMILY_ACTUATORS["smart_bus"]
    return [k for k in keys if k in catalog.ACTUATORS]


def _overrides(design) -> dict:
    out = {}
    for kv in (design.arm.actuator_overrides or "").split(","):
        if "=" in kv:
            k, v = (s.strip() for s in kv.split("=", 1))
            if v in catalog.ACTUATORS:
                out[k] = v
    return out


def _override_for(ov, jn):
    short = jn.split("_")[0]
    return ov.get(jn) or ov.get(short) or ov.get("all")


def gripper_actuator(design) -> str:
    """Same rule as the pipeline's gripper node: the gripper uses the arm's actuator family."""
    ov = _overrides(design)
    if ov.get("gripper"):
        return ov["gripper"]
    fam = design.arm.actuator_family
    if fam == "hobby_pwm":
        s = design.electronics.servo
        return s if s in catalog.ACTUATORS and catalog.ACTUATORS[s].get("family") == "hobby_pwm" else "mg996r"
    if fam == "smart_bus":
        return "sts3215"
    if fam == "dynamixel":
        return "xl430_w250"
    return _family(design)[0]


@dataclass
class _Layout:
    dof: int
    names: list
    keys: dict
    counts: dict
    gkey: str
    grip: dict
    jg: dict             # joint name -> _JG (pitch / yaw joints)
    A1: _Act             # base yaw actuator
    Ar: _Act | None      # roll actuator
    z_deck: float
    z_j1: float
    h_s: float
    phi_s: float         # shoulder actuator orientation about y (0 = vertical body, pi/2 = body towards -x)
    base_h: float
    L1: float
    L2: float
    wl: float            # wrist pitch axis -> roll output (dof 4: -> gripper flange)
    d45: float           # dof 6: wrist pitch -> wrist yaw axis
    wl5: float           # dof 6: wrist yaw axis -> roll output
    tcp_z: float
    L3: float
    notes: list
    issues: list
    lim: dict = field(default_factory=dict)

    @property
    def reach(self):
        return self.L1 + self.L2 + self.L3


def _bay_height(design, eplan):
    mods = [m["key"] for m in ((eplan or {}).get("modules") or [])]
    hs = [float(catalog.MODULES[k]["dims"]["h"]) for k in mods if k in catalog.MODULES
          and not catalog.MODULES[k].get("external") and catalog.MODULES[k].get("role") not in ("sensor", "safety")]
    return max(hs + [22.0])


def _layout(design, keys: dict, counts: dict, gkey: str, L12=None, base_h=None, eplan=None) -> _Layout:
    dof = int(design.arm.dof) if int(design.arm.dof) in (4, 5, 6) else 5
    names = arm_joint_names(dof)
    notes, issues = [], []
    grip = _grip_plan(design, gkey)
    issues += grip["issues"]
    A1 = _act(keys["j1_base_yaw"])
    roll = next((n for n in names if n.endswith("roll")), None)
    Ar = _act(keys[roll]) if roll else None
    jg = {}
    for jn in ("j2_shoulder", "j3_elbow", "j4_wrist_pitch") + (("j5_wrist_yaw",) if dof >= 6 else ()):
        jg[jn] = _jg(jn, keys[jn], counts.get(jn, 1))
    # base: yaw actuator hangs below the turntable, electronics bay height
    foot, floor = 2.5, 3.5
    bay_h = _bay_height(design, eplan)
    B = float(catalog.BEARINGS["lazy_susan_100"]["B"])
    z_j1_min = foot + floor + 6.0 + A1.overall
    z_deck = max(38.0, z_j1_min - B, foot + floor + 8.0 + bay_h + 4.0)
    z_j1 = z_deck + B
    # shoulder: vertical body if the turret is tall enough, else laid back (body towards -x)
    s = jg["j2_shoulder"]
    req_base = base_h if base_h is not None else float(design.arm.base_height_mm)
    need_v = -s.z_floor + 7.0
    need_h = s.hw + FLOOR + 7.0 + 2.0
    h_s = req_base - z_j1
    phi = 0.0
    if h_s < need_v:
        if h_s >= need_h:
            phi = math.pi / 2
        else:
            h_s = need_h
            phi = math.pi / 2
            notes.append(f"shoulder height raised to {z_j1 + h_s:.0f} mm (minimum for the {A1.key} base and "
                         f"{s.key} shoulder)")
    base_h_act = z_j1 + h_s
    # wrist stack
    w4 = jg["j4_wrist_pitch"]
    if dof == 4:
        wl, d45, wl5 = w4.z_b + T_BR + 2.0, 0.0, 0.0
        wrist_len = wl
    elif dof == 5:
        wl = w4.z_b + T_BR + Ar.overall + 0.4
        d45, wl5 = 0.0, 0.0
        wrist_len = wl
    else:
        y5 = jg["j5_wrist_yaw"]
        d45 = w4.z_b + T_BR - y5.z_floor + 0.4
        wl5 = y5.z_b + T_BR + Ar.overall + 0.4
        wl = d45
        wrist_len = d45 + wl5
    tcp_z = grip["tcp_z"]
    L3 = wrist_len + tcp_z
    # link minimums: child clevis bridge + parent cup
    e = jg["j3_elbow"]
    L1_min = s.z_b + T_BR + (-e.z_floor) + 8.0
    L2_min = e.z_b + T_BR + (-w4.z_floor) + 8.0
    if L12 is None:
        L12 = max(float(design.arm.reach_mm) - L3, L1_min + L2_min)
    L1 = max(round(0.51 * L12, 1), L1_min)
    L2 = max(round(L12 - L1, 1), L2_min)
    return _Layout(dof=dof, names=names, keys=dict(keys), counts=dict(counts), gkey=gkey, grip=grip, jg=jg, A1=A1, Ar=Ar,
                   z_deck=z_deck, z_j1=z_j1, h_s=h_s, phi_s=phi, base_h=base_h_act, L1=L1, L2=L2, wl=wl, d45=d45,
                   wl5=wl5, tcp_z=tcp_z, L3=L3, notes=notes, issues=issues)


def _task_L12(design, lay: _Layout) -> float:
    """Shoulder->wrist length (L1 + L2) needed to reach the pick and place targets top-down with the hand vertical
    and the fingertips 10 mm above the object (6 % margin so the elbow is never fully stretched)."""
    R = design.object.diameter_mm / 2
    hover = R + lay.grip["tip"] + 10.0
    need = 0.0
    for x, y in ((design.task.pick_x_mm, design.task.pick_y_mm), (design.task.place_x_mm, design.task.place_y_mm)):
        r = math.hypot(x, y)
        dz = R + hover + lay.L3 - lay.base_h
        need = max(need, math.hypot(r, dz))
    return need / 0.94


# ------------------------------------------------------------------ mass + static torque model
def _struct_mass_g(config, L, D2, hw):
    """Structural mass of one link (calibrated against the generated parts)."""
    k = {"prototype": 0.0105, "pilot": 0.0135, "production": 0.0085}.get(config, 0.0105)
    return k * L * (2 * D2) * (2 * hw) / 10.0 + 12.0


def _hand_mass_g(lay: _Layout, config) -> float:
    g = lay.grip
    A = g["A"]
    area = sum(p.area for p in (g["rack_A"], g["rack_B"], g["pinion"])) * 6.0 + g["geom"].area_mm2 * g["w"] * 2
    rho = 1.27e-3 * (0.55 if config == "prototype" else 1.0)
    plate = (2 * g["px"]) * (g["y_top"] - g["py0"]) * 5.0 * 0.7 + (2 * g["px"]) * 40 * T_FLANGE
    return A.mass_g + rho * (area + plate) + 14.0      # + fasteners, FSR, cabling


def _torques(design, lay: _Layout, config) -> dict:
    """Static torque per joint (N*m), worst case: arm stretched horizontally."""
    g_kg = lambda grams: grams / 1000.0  # noqa: E731
    payload = g_kg(design.object.mass_g)
    hand = g_kg(_hand_mass_g(lay, config))
    jg = lay.jg
    acts = {jn: _act(k) for jn, k in lay.keys.items()}
    cnt = lambda jn: lay.counts.get(jn, 1)  # noqa: E731
    # positions along the stretched arm from the shoulder axis (mm)
    s_el, s_wr = lay.L1, lay.L1 + lay.L2
    wrist_len = lay.wl if lay.dof != 6 else lay.d45 + lay.wl5
    s_gb = s_wr + wrist_len
    hand_com = s_gb + lay.tcp_z * 0.55
    masses = []   # (s, kg, tag)
    masses.append((0.45 * lay.L1, g_kg(_struct_mass_g(config, lay.L1, jg["j2_shoulder"].D2, jg["j3_elbow"].hw)), "ua"))
    masses.append((s_el - jg["j3_elbow"].A.so, cnt("j3_elbow") * g_kg(acts["j3_elbow"].mass_g), "act_el"))
    masses.append((s_el + 0.5 * lay.L2, g_kg(_struct_mass_g(config, lay.L2, jg["j3_elbow"].D2, jg["j4_wrist_pitch"].hw)), "fa"))
    masses.append((s_wr - jg["j4_wrist_pitch"].A.so, g_kg(acts["j4_wrist_pitch"].mass_g), "act_wr"))
    masses.append((s_wr + 0.45 * lay.wl, g_kg(_struct_mass_g(config, max(lay.wl, 20), jg["j4_wrist_pitch"].D2, 15.0)), "wr"))
    if lay.dof == 6:
        masses.append((s_wr + lay.d45 - jg["j5_wrist_yaw"].A.so, g_kg(acts["j5_wrist_yaw"].mass_g), "act_yaw"))
        masses.append((s_wr + lay.d45 + 0.45 * lay.wl5, g_kg(_struct_mass_g(config, lay.wl5, jg["j5_wrist_yaw"].D2, 15.0)), "wy"))
    roll = next((n for n in lay.names if n.endswith("roll")), None)
    if roll:
        masses.append((s_gb - lay.Ar.overall / 2, g_kg(lay.Ar.mass_g), "act_roll"))
    masses.append((hand_com, hand, "hand"))
    masses.append((s_gb + lay.tcp_z, payload, "payload"))

    def moment(s0, skip=()):
        return sum(m * (s - s0) for s, m, tag in masses if s > s0 and tag not in skip) * G / 1000.0

    tau = {}
    tau["j2_shoulder"] = moment(0.0, skip=())
    tau["j3_elbow"] = moment(s_el, skip=("ua", "act_el"))
    tau["j4_wrist_pitch"] = moment(s_wr, skip=("ua", "act_el", "fa", "act_wr"))
    if lay.dof == 6:
        tau["j5_wrist_yaw"] = moment(s_wr + lay.d45, skip=("ua", "act_el", "fa", "act_wr", "wr", "act_yaw"))
    # roll: hand centre of mass sits behind the roll axis (servo behind the back plate) + object offset
    g = lay.grip
    off = (g["z_plate"][1] + lay.grip["A"].overall / 2) * 0.6
    if roll:
        tau[roll] = max(hand * off * G / 1000.0, 0.12 * tau["j4_wrist_pitch"])
    # base yaw: no gravity load; accelerate the stretched arm at 4 rad/s^2 + bearing / cable friction
    Iz = sum(m * (s / 1000.0) ** 2 for s, m, _ in masses)
    tau["j1_base_yaw"] = Iz * 4.0 + 0.06
    return tau


# ------------------------------------------------------------------ joint limits from link silhouettes
def _limits(parent_inner, parent_outer, child_inner, child_outer, hi_cap=2.4, lo_cap=-2.4, margin_deg=4.0):
    """Largest +/- rotation about the joint (canonical frame) before the child's xz silhouette hits the parent's
    (inner layer = between the cheeks, outer = cheek layers)."""
    def hits(th):
        ci = affinity.rotate(child_inner, th, origin=(0, 0), use_radians=True)
        co = affinity.rotate(child_outer, th, origin=(0, 0), use_radians=True)
        return ci.intersection(parent_inner).area > 0.8 or co.intersection(parent_outer).area > 0.8

    out = []
    for sgn, cap in ((1, hi_cap), (-1, lo_cap)):
        lim = abs(cap)
        for deg in np.arange(2.0, math.degrees(abs(cap)) + 0.1, 2.0):
            # shapely rotates CCW in its (x, y) = (x, z) plane; a joint angle +q about +y moves +z towards +x,
            # i.e. clockwise in the (x, z) plane
            if hits(-sgn * math.radians(deg)):
                lim = math.radians(max(deg - margin_deg, 10.0))
                break
        out.append(sgn * lim)
    return out[1], out[0]


def _rect(x0, x1, z0, z1):
    return box(min(x0, x1), min(z0, z1), max(x0, x1), max(z0, z1))


def _jg_parent_regions(j: _JG, beam_hw, beam_len):
    """Parent-side silhouettes of a pitch joint in its canonical frame (x, z)."""
    zb0, zb1 = j.z_body
    A = j.A
    inner = unary_union([_rect(-A.W / 2, A.W / 2, zb0, zb1), _rect(-j.hw, j.hw, j.z_floor, j.z_cup),
                         _rect(-j.hw, -A.W / 2, j.z_cup - 2, j.z_fing),
                         _rect(-beam_hw, beam_hw, j.z_floor - beam_len, j.z_floor)])
    if j.ear:
        inner = inner.union(_rect(-j.hw, j.hw, j.ear["z0"] - 1.5, j.ear["z1"] + 1.5).difference(
            _rect(-A.W / 2 - 0.3, A.W / 2 + 0.3, zb0 - 1, zb1 + 30)))
    outer = unary_union([_rect(-j.hw, j.hw, j.z_floor, j.z_cup), _rect(-beam_hw, beam_hw, j.z_floor - beam_len, j.z_floor)])
    return inner, outer


def _child_regions(j: _JG, child_sil):
    """Child silhouette split into the inner layer (above the bridge) and the cheek layers."""
    inner = child_sil.intersection(_rect(-500, 500, j.z_b, 2000))
    outer = child_sil.union(Point(0, 0).buffer(j.r_ch, quad_segs=16))
    return inner, outer


# ------------------------------------------------------------------ sizing
def size(design) -> dict:
    """Actuator per joint by static torque in the worst-case horizontal pose (link + actuator masses,
    payload = object + gripper), link lengths from reach and the task's top-down reach."""
    a = design.arm
    config = _config(design)
    fam = _family(design)
    ov = _overrides(design)
    dof = int(a.dof) if int(a.dof) in (4, 5, 6) else 5
    names = arm_joint_names(dof)
    gkey = gripper_actuator(design)
    keys = {jn: (_override_for(ov, jn) or fam[0]) for jn in names}
    counts = {jn: 1 for jn in names}
    margin_f = max(float(a.payload_margin), 1.0)
    L12 = None
    lay = None
    for _ in range(6):
        lay = _layout(design, keys, counts, gkey, L12=None)
        L12_req = lay.L1 + lay.L2
        L12_task = _task_L12(design, lay)
        L12 = max(L12_req, L12_task)
        lay = _layout(design, keys, counts, gkey, L12=L12)
        tau = _torques(design, lay, config)
        new_keys, new_counts = dict(keys), dict(counts)
        for jn in reversed(names):           # distal first: their masses load the proximal joints
            if _override_for(ov, jn):
                continue
            need = tau[jn] * margin_f
            pick = next((k for k in fam if catalog.ACTUATORS[k]["torque_nm"] >= need), None)
            n_act = 1
            if pick is None and jn in ("j2_shoulder", "j3_elbow"):
                pick = next((k for k in fam if 2 * 0.95 * catalog.ACTUATORS[k]["torque_nm"] >= need), None)
                n_act = 2 if pick else 1
            new_keys[jn] = pick or fam[-1]
            new_counts[jn] = n_act if pick else (2 if jn in ("j2_shoulder", "j3_elbow") else 1)
        if new_keys == keys and new_counts == counts:
            break
        keys, counts = new_keys, new_counts
    tau = _torques(design, lay, config)
    joints, issues = [], list(lay.issues)
    for jn in names:
        key, n_act = keys[jn], counts.get(jn, 1)
        avail = catalog.ACTUATORS[key]["torque_nm"] * (n_act * 0.95 if n_act > 1 else 1.0)
        need = tau[jn] * margin_f
        if avail < need:
            issues.append(f"{jn}: needs {need:.2f} N·m with margin, {n_act}× {key} gives {avail:.2f} N·m")
        joints.append({"name": jn, "actuator": key, "count": n_act, "torque_req_nm": round(tau[jn], 3),
                       "torque_with_margin_nm": round(need, 3), "torque_avail_nm": round(avail, 3),
                       "margin": round(avail / max(tau[jn], 1e-6), 2),
                       "kind": "yaw" if jn.startswith("j1") else ("roll" if jn.endswith("roll") else "pitch")})
    notes = list(lay.notes)
    req = float(a.reach_mm)
    if lay.reach > req + 1.0:
        why = []
        if lay.L1 + lay.L2 > req - lay.L3 + 1.0:
            why.append(f"top-down pick/place at {math.hypot(design.task.pick_x_mm, design.task.pick_y_mm):.0f} / "
                       f"{math.hypot(design.task.place_x_mm, design.task.place_y_mm):.0f} mm with a "
                       f"{lay.L3:.0f} mm wrist + hand")
        notes.append(f"reach raised from {req:.0f} to {lay.reach:.0f} mm: " + ("; ".join(why) or "minimum link lengths"))
    gA = _act(gkey)
    grip_force = gA.torque * 0.8 / (lay.grip["r"] / 1000.0)
    return {"links_mm": {"base_h": round(lay.base_h, 1), "L1": round(lay.L1, 1), "L2": round(lay.L2, 1),
                         "L3": round(lay.L3, 1), "wrist": round(lay.wl if dof != 6 else lay.d45 + lay.wl5, 1),
                         "hand": round(lay.tcp_z, 1)},
            "reach_mm": round(lay.reach, 1), "reach_requested_mm": req, "payload_g": design.object.mass_g,
            "joints": joints, "gripper_actuator": gkey,
            "gripper": {"actuator": gkey, "pinion_teeth": lay.grip["n"], "pitch_radius_mm": lay.grip["r"],
                        "travel_mm": round(lay.grip["T"], 1), "overtravel_mm": lay.grip["overtravel"],
                        "stall_force_n": round(grip_force, 1)},
            "hand_mass_g": round(_hand_mass_g(lay, config), 1), "build_config": config,
            "issues": issues, "notes": notes}


# ================================================================== build
DENS = {"PLA": 1.24, "PETG": 1.27, "PA12": 1.01, "Resin": 1.15, "ABS": 1.04, "PC": 1.2, "TPU": 1.21, "Al6061": 2.70,
        "Al5052": 2.68, "Steel": 7.85, "Stainless": 8.0, "FR4": 1.85, "Copper": 8.96, "Rubber": 1.1, "Acrylic": 1.18}

PRINT_OPTS = ("fdm_print", "mjf_print", "sla_print", "injection_mold")

# role -> (material, colour, process, process options) per build config
LOOK = {
    "prototype": {
        "shell": ("PETG", C_WHITE, "fdm_print", PRINT_OPTS),
        "dark": ("PETG", C_CHAR, "fdm_print", PRINT_OPTS),
        "accent": ("PETG", C_ORANGE, "fdm_print", PRINT_OPTS),
        "plate": ("PETG", C_WHITE, "fdm_print", PRINT_OPTS),
        "sheet": ("PETG", C_CHAR, "fdm_print", PRINT_OPTS),
        "cover": ("PETG", C_CHAR, "fdm_print", PRINT_OPTS),
        "finger": ("PETG", C_CHAR, "fdm_print", ("fdm_print", "sla_print", "cnc_mill")),
        "gear": ("PETG", C_WHITE, "fdm_print", ("fdm_print", "sla_print", "mjf_print")),
        "pad": ("TPU", C_ORANGE, "fdm_print", ("fdm_print", "injection_mold")),
    },
    "pilot": {
        "shell": ("PA12", C_MJF, "mjf_print", ("mjf_print", "sla_print", "fdm_print")),
        "dark": ("PA12", C_MJF, "mjf_print", ("mjf_print", "sla_print", "fdm_print")),
        "accent": ("PA12", C_ORANGE, "mjf_print", ("mjf_print", "sla_print")),
        "plate": ("Al6061", C_ANOD, "cnc_mill", ("cnc_mill", "laser_cut")),
        "sheet": ("Al5052", C_SHEET, "sheet_metal", ("sheet_metal",)),
        "cover": ("PA12", C_MJF, "mjf_print", ("mjf_print", "sla_print")),
        "finger": ("Al6061", C_ANOD_DK, "cnc_mill", ("cnc_mill", "fdm_print")),
        "gear": ("Delrin", "#f2efe6", "cnc_mill", ("cnc_mill", "sla_print")),
        "pad": ("TPU", C_ORANGE, "injection_mold", ("injection_mold", "fdm_print")),
    },
    "production": {
        "shell": ("PC", C_PC, "injection_mold", ("injection_mold",)),
        "dark": ("PC", C_PC_DK, "injection_mold", ("injection_mold",)),
        "accent": ("PC", C_ORANGE, "injection_mold", ("injection_mold",)),
        "plate": ("Al5052", C_SHEET_RAW, "sheet_metal", ("sheet_metal", "laser_cut")),
        "sheet": ("Al5052", C_SHEET_RAW, "sheet_metal", ("sheet_metal",)),
        "cover": ("PC", C_PC_DK, "injection_mold", ("injection_mold",)),
        "finger": ("PC", C_PC_DK, "injection_mold", ("injection_mold", "cnc_mill")),
        "gear": ("Delrin", "#f2efe6", "injection_mold", ("injection_mold",)),
        "pad": ("TPU", C_ORANGE, "injection_mold", ("injection_mold",)),
    },
}
DENS["Delrin"] = 1.41


def _est_mass_g(mesh, material, process):
    rho = DENS.get(material, 1.2)
    V = abs(float(mesh.volume))
    if process == "fdm_print":
        shell = min(V, float(mesh.area) * 1.0)       # ~3 perimeters / top-bottom layers
        return rho * (shell + 0.18 * (V - shell)) / 1000.0
    return rho * V / 1000.0


@functools.lru_cache(maxsize=256)
def _bolt_cached(spec, kind):
    """Fastener with its real head (components.fastener) and a plain shank (threads are hidden in the part)."""
    full = components.fastener(spec, kind)
    if kind in ("nut", "washer", "insert"):
        return full
    size, L = components._parse_spec(spec) if hasattr(components, "_parse_spec") else (spec.split("x")[0], 8.0)
    d = float(size[1:])
    try:
        man = _man(full)
        z_cut = -0.25 if kind != "fhcs" else -0.25 - {"M2": 1.2, "M2.5": 1.5, "M3": 1.7, "M4": 2.3, "M5": 2.8}.get(size, 1.7)
        head = _I(man, _box2(-20, 20, -20, 20, z_cut, 20))
        L = L or 8.0
        shank = _mv(_rev([(0, 0), (d / 2 * 0.94 - 0.25, 0), (d / 2 * 0.94, 0.25), (d / 2 * 0.94, L + z_cut + 0.3),
                          (0, L + z_cut + 0.3)], 14), 0, 0, -L)
        return _tri(_U(head, shank))
    except Exception:  # noqa: BLE001
        return full


def _bolt_mesh(spec, kind):
    return _bolt_cached(str(spec), str(kind)).copy()


def _seat(p, n, up=(0, 0, 1)):
    """Fastener frame: origin at the seating surface p, +z = n (out of the hole, towards the head)."""
    n = np.asarray(n, float)
    u = np.asarray(up, float)
    if abs(np.dot(u, n) / np.linalg.norm(n)) > 0.95:
        u = np.array([1.0, 0, 0]) if abs(n[0]) < 0.9 else np.array([0, 1.0, 0])
    return _frame(p, n, np.cross(u, n))


class _Builder:
    def __init__(self, design, sizing, eplan):
        self.d = design
        self.cfg = _config(design)
        self.look = LOOK[self.cfg]
        self.sz = sizing
        self.extras = _extras(design)
        self.eplan = eplan or _default_eplan(design)
        joints = {j["name"]: j for j in sizing["joints"]}
        dof = int(design.arm.dof) if int(design.arm.dof) in (4, 5, 6) else 5
        names = arm_joint_names(dof)
        fam = _family(design)
        keys = {jn: (joints[jn]["actuator"] if jn in joints else fam[0]) for jn in names}
        counts = {jn: int(joints[jn].get("count", 1)) if jn in joints else 1 for jn in names}
        gkey = sizing.get("gripper_actuator") or gripper_actuator(design)
        Lm = sizing.get("links_mm", {})
        L1, L2 = float(Lm.get("L1", 150.0)), float(Lm.get("L2", 140.0))
        self.lay = _layout(design, keys, counts, gkey, L12=L1 + L2, base_h=float(Lm.get("base_h", design.arm.base_height_mm)),
                           eplan=self.eplan)
        self.lay.L1, self.lay.L2 = L1, L2
        self.asm = Assembly((design.name or "robot").replace("-", "_").replace(" ", "_").lower() or "robot")
        self.lex = {}
        self.issues = []

    # ------------------------------------------------------------ parts
    def add(self, name, link, geom, kind="structural", step="final", explode=(0, 0, 0), material="PETG", color=C_WHITE,
            process="fdm_print", process_options=(), sku="", group="", mass_g=None, collide=True, profile=None,
            profile_frame=None, thickness=0.0, **meta):
        mesh = geom if isinstance(geom, trimesh.Trimesh) else _tri(geom)
        if mesh is None or len(mesh.faces) == 0:
            return None
        base, k = name, 2
        names = {p.name for p in self.asm.parts}
        while name in names:
            name = f"{base}_{k}"
            k += 1
        if mass_g is None and process not in ("purchased",):
            mass_g = _est_mass_g(mesh, material, process)
        ex = np.asarray(self.lex.get(link, (0, 0, 0)), float) + np.asarray(explode, float)
        p = Part(name=name, link=link, mesh=mesh, kind=kind, material=material, color=color, process=process,
                 process_options=tuple(process_options), sku=sku, group=group or name, step=0,
                 explode=tuple(round(float(v), 2) for v in ex), profile=profile, profile_frame=profile_frame,
                 thickness=float(thickness), collide=collide, meta=dict(meta))
        p.meta["step_key"] = step
        if mass_g is not None:
            p.meta["mass_g"] = round(float(mass_g), 2)
        return self.asm.add_part(p)

    def struct(self, name, link, geom, step, explode=(0, 0, 0), role="shell", kind="structural", **kw):
        mat, col, proc, opts = self.look[role]
        kw.setdefault("material", mat)
        kw.setdefault("color", col)
        kw.setdefault("process", proc)
        kw.setdefault("process_options", opts)
        return self.add(name, link, geom, kind, step, explode, **kw)

    def comp(self, comp, prefix, link, T, step, explode=(0, 0, 0), kind=None, skip=(), sku=None, meta=None):
        parts = [(s, m, c, mt) for s, m, c, mt in comp.parts if not any(s.startswith(k) for k in skip)]
        vols = [max(abs(float(m.volume)), 1e-3) for _, m, _, _ in parts]
        tot = sum(vols) or 1.0
        mass = float(comp.spec.get("mass_g", 5.0)) if isinstance(comp.spec, dict) else 5.0
        out = []
        for (s, m, c, mt), v in zip(parts, vols):
            mm = m.copy()
            mm.apply_transform(T)
            mat = mt if mt in DENS or mt == "purchased" else "purchased"
            p = self.add(f"{prefix}_{s}", link, mm, kind or comp.kind, step, explode, material=mat, color=c,
                         process="purchased", sku=sku or comp.key, group=f"{sku or comp.key}_{s}", mass_g=mass * v / tot,
                         **(meta or {}))
            out.append(p)
        return out

    def bolts(self, name, link, spec, kind, frames, step, explode_len=None, torque=None, color=None, insert=False):
        if not frames:
            return None
        ms = []
        for F in frames:
            m = _bolt_mesh(spec, kind)
            m.apply_transform(F)
            ms.append(m)
        mesh = trimesh.util.concatenate(ms)
        ax = np.mean([F[:3, 2] for F in frames], axis=0)
        nrm = np.linalg.norm(ax)
        ax = ax / nrm if nrm > 1e-6 else np.array([0, 0, 1.0])
        size, L = (spec.split("x") + ["0"])[:2] if "x" in spec else (spec, "0")
        L = float(L or 0)
        ex = ax * (explode_len if explode_len is not None else 2.0 * L + 14.0)
        col, mat = components.FASTENER_LOOK.get(kind, ("#2b2c30", "Steel"))
        mass = DENS["Steel"] * abs(float(mesh.volume)) / 1000.0
        tq = torque if torque is not None else catalog.FASTENER_TORQUE_NM.get(size, 0.5)
        names = {"shcs": "SHCS", "bhcs": "BHCS", "fhcs": "FHCS", "nut": "nut", "washer": "washer", "insert": "heat-set insert"}
        return self.add(name, link, mesh, "fastener", step, ex, material=mat, color=color or col, process="purchased",
                        sku=f"{spec}_{kind}", group=f"{spec}_{kind}", mass_g=mass, spec=spec, fastener_kind=kind,
                        qty=len(frames), torque_nm=tq, label=f"{spec} {names.get(kind, kind)}")

    # ------------------------------------------------------------ frames
    def frames(self):
        L = self.lay
        self.T = {}
        self.T["j1_act"] = trans((0, 0, L.z_j1))                                    # base
        self.T["j2_par"] = trans((0, 0, L.h_s)) @ rot((0, 1, 0), L.phi_s)           # turret
        self.T["j3_par"] = trans((0, 0, L.L1))                                      # upper arm
        self.T["j4_par"] = trans((0, 0, L.L2))                                      # forearm
        if L.dof == 6:
            self.T["j5_par"] = trans((0, 0, L.d45)) @ rot((0, 0, 1), -math.pi / 2)  # wrist
            self.T["roll_par"] = trans((0, 0, L.wl5)) @ rot((0, 0, 1), math.pi / 2)  # wrist_yaw
        elif L.dof == 5:
            self.T["roll_par"] = trans((0, 0, L.wl)) @ rot((0, 0, 1), math.pi / 2)  # wrist
        self.T["yaw_child"] = rot((0, 0, 1), -math.pi / 2)
        self.T["roll_child"] = rot((0, 0, 1), math.pi / 2)
        chain = ["base", "turret", "upper_arm", "forearm", "wrist"] + (["wrist_yaw"] if L.dof == 6 else []) + \
            ["gripper_base"]
        for i, ln in enumerate(chain):
            self.lex[ln] = (0.0, 0.0, 26.0 * i) if i else (0.0, 0.0, 0.0)
        self.lex["turret"] = (0, 0, 34.0)
        for ln, v in (("jaw_left", (-24.0, 0, 0)), ("jaw_right", (24.0, 0, 0)), ("pinion", (0, -12.0, 0)), ("tcp", (0, 0, 0))):
            self.lex[ln] = tuple(np.asarray(self.lex["gripper_base"]) + v)

    # ------------------------------------------------------------ joint hardware (actuators, horns, bolts)
    def joint_hw(self, j: _JG, par, Tp, child, Tc, prefix, step_par, step_child):
        A = j.A
        cfg = self.cfg
        for i, Ta in enumerate(j.T_acts):
            sfx = "" if j.count == 1 else ("_a", "_b")[i]
            comp = components.actuator(j.key, horn=False)
            ex_dir = _vec(Tp @ Ta, (0, 0, 1)) * 0.0 + _vec(Tp, (0, 0, -1))
            self.comp(comp, f"{prefix}_servo{sfx}", par, Tp @ Ta, step_par, explode=ex_dir * 30.0,
                      kind="actuator", skip=("wire_",), meta={"drives": j.name, "servo_index": i})
            hc = components.horn(j.key)
            Th = Tc @ Ta @ trans((0, 0, -A.ht))
            hdir = _vec(Tc @ Ta, (0, 0, 1))
            self.comp(hc, f"{prefix}_horn{sfx}", child, Th, step_child, explode=hdir * 18.0, kind="horn")
            # horn screws through the cheek
            fr = []
            d_h = A.horn_holes[0][2] if A.horn_holes else 2.0
            size = _screw_size(d_h)
            Lb = _std_len(T_CH - 1.4 + min(A.ht, 3.0) * 0.8)
            for hx, hy, _ in A.horn_holes[:8]:
                p_face = _pt(Tc @ Ta, (hx, hy, 0.0))
                n_out = _vec(Tc @ Ta, (0, 0, 1))
                fr.append(_seat(p_face + n_out * (T_CH - 1.4), n_out))
            if len(fr) > 4:
                fr = fr[::2]
            self.bolts(f"{prefix}_horn_screws{sfx}", child, f"{size}x{Lb}", "shcs", fr, step_child)
        if j.count == 1:
            Ta = j.T_acts[0]
            n_id = _vec(Tc @ Ta, (0, 0, -1))
            if A.hobby:
                # idler: steel pin on the parent tongue, 608 bearing pressed into the child's idler cheek
                b = components.bearing("608")
                yb = j.y_i + 3.5 + 0.5
                Tb = Tc @ trans((0, yb, 0)) @ R_ZY
                self.comp(b, f"{prefix}_idler_bearing", child, Tb, step_child, explode=_vec(Tc, (0, 1, 0)) * 22.0,
                          kind="bearing")
                pin = _ycyl(4.0, j.tongue[0] + 0.2, j.y_i + 8.0, f=0.4, segs=28)
                self.add(f"{prefix}_idler_pin", par, _xf(pin, Tp), "structural", step_par, _vec(Tp, (0, 1, 0)) * 16,
                         material="Steel", color=C_STEEL, process="cnc_turn", process_options=("cnc_turn", "purchased"))
            else:
                hl = A.holes.get("idler") or []
                fr = []
                if hl:
                    for hx, hy, hz in hl[:4]:
                        p = _pt(Tc @ Ta, (hx, hy, A.zi))
                        fr.append(_seat(p + n_id * (j.D2 - j.y_i - 1.2), n_id))
                    self.bolts(f"{prefix}_idler_screws", child, f"M2x{_std_len(j.D2 - j.y_i - 1.2 + 2.5)}", "shcs", fr,
                               step_child)
                else:
                    p = _pt(Tc @ Ta, (0, 0, A.zi))
                    fr.append(_seat(p + n_id * (j.D2 - j.y_i - 1.4), n_id))
                    self.bolts(f"{prefix}_idler_screw", child, f"M2.5x{_std_len(j.D2 - j.y_i + 3)}", "shcs", fr, step_child)
        # actuator retaining screws in the parent cradle
        fr = []
        for i, Ta in enumerate(j.T_acts):
            if A.hobby and A.holes.get("ears"):
                for hx, hy, hz, hd in A.holes["ears"]:
                    p = _pt(Tp @ Ta, (hx, hy, hz + float(A.spec["dims"].get("ear_t", 2.5))))
                    fr.append(_seat(p, _vec(Tp @ Ta, (0, 0, 1))))
                spec = "M3x12" if A.W > 15 else "M2x8"
            else:
                sh = A.holes.get("side") or []
                # back face (canonical -x): component +y face
                back = [h for h in sh if h[1] > A.W / 2 - 0.5]
                zs = sorted({round(h[0], 2) for h in back})
                pick = [h for h in back if round(h[0], 2) in (zs[0], zs[-1])] if zs else []
                for hx, hy, hz in pick:
                    p = _pt(Tp @ Ta, (hx, hy + WALL - 1.0, hz))
                    fr.append(_seat(p, _vec(Tp @ Ta, (0, 1, 0))))
                spec = "M2.5x6" if A.W < 26 else "M2.5x8"
        if fr:
            self.bolts(f"{prefix}_mount_screws", par, spec, "bhcs" if not A.hobby else "shcs", fr, step_par)

    # ------------------------------------------------------------ cradle / clevis geometry (canonical frame)
    def cup_block(self, j: _JG):
        return _rbox2(-j.hw, j.hw, -j.D2, j.D2, j.z_floor, j.z_cup, r=4.0, f=2.0)

    def cup_adds(self, j: _JG):
        A = j.A
        adds = []
        if j.ear:
            e = j.ear
            col = _rbox2(-j.hw, j.hw, e["y1"] + 0.05, e["y1"] + 3.6, j.z_cup - 3.0, e["z1"] + 1.5, r=2.0, f=1.0)
            col = _D(col, _box2(-A.W / 2 - 0.3, A.W / 2 + 0.3, -200, 200, -200, j.z_body[1] + 0.3))
            adds.append(col)
        else:
            for Ta in j.T_acts:
                # back finger (canonical -x for actuator a, +x for the mirrored b) holding the side screws
                sgn = -1.0 if Ta[0, 1] < 0 else 1.0       # where component +y points
                y0, y1 = sorted((_pt(Ta, (0, 0, A.zt))[1], _pt(Ta, (0, 0, A.zb))[1]))
                x0, x1 = sorted((sgn * (A.W / 2 + 0.25), sgn * j.hw))
                adds.append(_rbox2(x0, x1, y0 + 1.0, y1 - 1.0, j.z_cup - 3.0, j.z_fing, r=1.5, f=1.2))
        if j.tongue:
            t0, t1 = j.tongue
            tg = _hull(_box2(-min(j.hw, 9.0), min(j.hw, 9.0), t0, t1, j.z_cup - 2.0, j.z_cup - 1.0),
                       _ycyl(8.0, t0, t1, f=0.6))
            adds.append(tg)
        return adds

    def cup_cuts(self, j: _JG):
        A = j.A
        cuts = []
        for Ta in j.T_acts:
            bx0 = -A.so - A.L / 2 - 0.3
            pk = _xf(_box2(bx0, 200, -A.W / 2 - 0.3, A.W / 2 + 0.3, A.zb - 0.3, A.zt + 0.3), Ta)
            cuts.append(pk)
            if A.hobby and "ears" in A.bounds:
                eb = A.bounds["ears"]
                cuts.append(_xf(_box2(eb[0][0] - 0.4, 200, eb[0][1] - 0.4, eb[1][1] + 0.4, eb[0][2] - 0.2, eb[1][2] + 0.3), Ta))
                # access from the horn side to the lower ear screws
                cuts.append(_xf(_box2(eb[0][0] - 0.4, -A.so - A.L / 2 + 0.5, eb[0][1] - 0.4, eb[1][1] + 0.4, eb[0][2], 60), Ta))
            if not A.hobby and "idler" in A.bounds:
                cuts.append(_xf(_cyl(A.horn_d / 2 * 0.0 + 11.0, 3.0, A.zi - 1.5, segs=40), Ta))
        for b in j.conn_box:
            x0, y0, z0 = b[0] - 1.2
            x1, y1, z1 = b[1] + 1.2
            if y1 > 0:
                cuts.append(_rbox2(x0, x1, y0, j.D2 + 5, z0, max(z1, z0 + 4), r=1.0, f=0.0))
            else:
                cuts.append(_rbox2(x0, x1, -j.D2 - 5, y1, z0, max(z1, z0 + 4), r=1.0, f=0.0))
        if j.count == 1 and "label" in A.bounds or (j.count == 1 and "print" in A.bounds):
            lb = _canon_box(j.T_acts[0], A.bounds.get("label", A.bounds.get("print")))
            if lb[1][0] > 0:
                z1 = min(lb[1][2] + 1.0, j.z_cup + 2.0)
                z0 = lb[0][2] - 0.5
                if z1 - z0 > 4:
                    cuts.append(_rbox2(A.W / 2 - 0.6, j.hw + 2, lb[0][1] - 0.8, lb[1][1] + 0.8, z0, z1 + 6, r=2.5, f=0.0))
        # screw holes for the back-side retaining screws
        if not A.hobby:
            for Ta in j.T_acts:
                for hx, hy, hz in (A.holes.get("side") or []):
                    if hy > A.W / 2 - 0.5:
                        p0 = _pt(Ta, (hx, hy, hz))
                        n = _vec(Ta, (0, 1, 0))
                        F = _frame(p0, n, (0, 0, 1) if abs(n[2]) < 0.9 else (1, 0, 0))
                        cuts.append(_xf(_U(_cyl(1.45, WALL + 2, -0.5, segs=16), _cyl(2.9, 3.0, WALL - 1.0, segs=20)), F))
        return cuts

    def clevis_cuts(self, j: _JG):
        """Child side, canonical frame: slot between the cheeks, horn / idler bolt holes."""
        A = j.A
        cuts = [_box2(-300, 300, j.y_h, j.y_i, -300, j.z_b)]
        for Ta in j.T_acts:
            n_out = _vec(Ta, (0, 0, 1))
            for hx, hy, hd in A.horn_holes:
                p = _pt(Ta, (hx, hy, 0.0))
                F = _frame(p, n_out, (1, 0, 0))
                cuts.append(_xf(_U(_cyl(hd / 2 + 0.25, T_CH + 2, -1, segs=14), _cyl(hd / 2 + 1.55, 3.0, T_CH - 1.4, segs=20)), F))
            F = _frame(_pt(Ta, (0, 0, 0)), n_out, (1, 0, 0))
            cuts.append(_xf(_cyl(4.2, T_CH + 3, -1, segs=28), F))
        if j.count == 1:
            Ta = j.T_acts[0]
            n_id = _vec(Ta, (0, 0, -1))
            depth = j.D2 - j.y_i
            if A.hobby:
                c = _pt(Ta, (0, 0, 0))
                cuts.append(_ycyl(11.05, j.y_i - 0.5, j.y_i + 7.6, f=0.0, x=c[0], z=c[2]))
                cuts.append(_ycyl(6.0, j.y_i, j.D2 + 1, f=0.0, x=c[0], z=c[2]))
            else:
                hl = A.holes.get("idler") or []
                pts = [(_pt(Ta, (hx, hy, A.zi)), 1.0) for hx, hy, hz in hl[:4]] or [(_pt(Ta, (0, 0, A.zi)), 1.35)]
                for p, r in pts:
                    F = _frame(p, n_id, (1, 0, 0))
                    cuts.append(_xf(_U(_cyl(r + 0.2, depth + 2, -1, segs=14), _cyl(r + 1.4, 3.0, depth - 1.2, segs=18)), F))
        return cuts

    def clevis_hull(self, j: _JG, T):
        return _xf(_ycyl(j.r_ch, -j.D2, j.D2, f=1.6, segs=72), T)

    def clevis_adds(self, j: _JG, T):
        if j.count == 1 and j.A.hobby:     # bearing boss on the idler cheek
            return [_xf(_ycyl(13.5, j.y_i, j.D2, f=1.0), T)]
        return []

    # ------------------------------------------------------------ roll cup (component frame of the roll actuator)
    def roll_cup(self, A: _Act):
        x0, x1 = -A.so - A.L / 2, -A.so + A.L / 2
        zbot = min(A.zb, A.zi)
        cuts, adds = [], []
        if A.hobby and "ears" in A.bounds:
            eb = A.bounds["ears"]
            ztop = float(eb[0][2]) - 0.05
            block = _rbox2(min(x0, eb[0][0]) - 2.5, max(x1, eb[1][0]) + 2.5, -A.W / 2 - WALL, A.W / 2 + WALL,
                           zbot - FLOOR, ztop, r=4.0, f=1.5)
        else:
            block = _rbox2(x0 - WALL, x1 + WALL, -A.W / 2 - WALL, A.W / 2 + WALL, zbot - FLOOR, A.zt - 1.6, r=4.0, f=1.8)
        cuts.append(_box2(x0 - 0.3, x1 + 0.3, -A.W / 2 - 0.3, A.W / 2 + 0.3, A.zb - 0.3, 60))
        if not A.hobby:
            cuts.append(_cyl(11.2, (A.zb - A.zi) + 2.0, A.zi - 1.0, segs=40))
        for s in ("connectors", "grommet"):
            if s in A.bounds:
                b = A.bounds[s]
                cuts.append(_box2(b[0][0] - 1.5, b[1][0] + 1.5, b[0][1] - 1.5, b[1][1] + 1.5, zbot - FLOOR - 4, b[1][2] + 1))
        lb = A.bounds.get("label", A.bounds.get("print"))
        if lb is not None and not A.hobby:
            cuts.append(_rbox2(lb[0][0] - 0.8, lb[1][0] + 0.8, -A.W / 2 - WALL - 2, -A.W / 2 + 0.6, lb[0][2] - 0.5, 40,
                               r=2.5, f=0.0))
        for hx, hy, hz in (A.holes.get("side") or []):
            if hy > A.W / 2 - 0.5:
                F = _frame((hx, hy, hz), (0, 1, 0), (1, 0, 0))
                cuts.append(_xf(_U(_cyl(1.45, WALL + 2, -0.5, segs=16), _cyl(2.9, 3.0, WALL - 1.0, segs=20)), F))
        return block, adds, cuts

    def roll_hw(self, A: _Act, par, Tp, child, Tc, prefix, step_par, step_child):
        comp = components.actuator(A.key, horn=False)
        self.comp(comp, f"{prefix}_servo", par, Tp, step_par, explode=_vec(Tp, (0, 0, 1)) * 26.0, kind="actuator",
                  skip=("wire_",), meta={"drives": "roll"})
        hc = components.horn(A.key)
        self.comp(hc, f"{prefix}_horn", child, Tc @ trans((0, 0, -A.ht)), step_child, explode=(0, 0, -14.0), kind="horn")
        fr = []
        hd = A.horn_holes[0][2] if A.horn_holes else 2.0
        for hx, hy, _ in A.horn_holes[:8]:
            fr.append(_seat(_pt(Tc, (hx, hy, T_FLANGE - 1.4)), _vec(Tc, (0, 0, 1))))
        if len(fr) > 4:
            fr = fr[::2]
        self.bolts(f"{prefix}_horn_screws", child, f"{_screw_size(hd)}x{_std_len(T_FLANGE - 1.4 + 2.5)}", "shcs", fr,
                   step_child, explode_len=-22.0)
        fr = []
        if A.hobby and A.holes.get("ears"):
            for hx, hy, hz, hd2 in A.holes["ears"]:
                fr.append(_seat(_pt(Tp, (hx, hy, hz + float(A.spec["dims"].get("ear_t", 2.5)))), _vec(Tp, (0, 0, 1))))
            self.bolts(f"{prefix}_mount_screws", par, "M3x10" if A.W > 15 else "M2x8", "shcs", fr, step_par)
        else:
            for hx, hy, hz in (A.holes.get("side") or []):
                if hy > A.W / 2 - 0.5:
                    fr.append(_seat(_pt(Tp, (hx, hy + WALL - 1.0, hz)), _vec(Tp, (0, 1, 0))))
            fr = fr[:2] if len(fr) <= 2 else [fr[0], fr[-1]]
            self.bolts(f"{prefix}_mount_screws", par, "M2.5x6", "bhcs", fr, step_par)

    # ------------------------------------------------------------ link bodies
    def link_body(self, prox, dists, extra=(), pocket=True):
        """Printed / moulded link body. prox = (jg, T) child-side clevis or None; dists = [("jg", jg, T) |
        ("roll", A, T) | ("flange", r, z0, z1, pcd)]. Returns (manifold, xz silhouette polygon, info)."""
        hull_parts, adds, cuts = [], [], []
        sil = []
        info = {"z0": None, "z1": None}
        if prox:
            jp, Tp = prox
            hull_parts.append(self.clevis_hull(jp, Tp))
            adds += self.clevis_adds(jp, Tp)
            cuts += [_xf(c, Tp) for c in self.clevis_cuts(jp)]
            info["z0"] = _pt(Tp, (0, 0, jp.z_b + T_BR))[2]
        for dd in dists:
            if dd[0] == "jg":
                _, j, T = dd
                hull_parts.append(_xf(self.cup_block(j), T))
                adds += [_xf(a, T) for a in self.cup_adds(j)]
                cuts += [_xf(c, T) for c in self.cup_cuts(j)]
                info["z1"] = min(_pt(T, (x, 0, j.z_floor))[2] for x in (-j.hw, j.hw))
            elif dd[0] == "roll":
                _, A, T = dd
                blk, a2, c2 = self.roll_cup(A)
                hull_parts.append(_xf(blk, T))
                adds += [_xf(a, T) for a in a2]
                cuts += [_xf(c, T) for c in c2]
                info["z1"] = _pt(T, (0, 0, min(A.zb, A.zi) - FLOOR))[2]
            elif dd[0] == "flange":
                _, r, z0, z1, pcd = dd
                hull_parts.append(_fcyl(r, z1 - z0, f=1.5, z0=z0))
                for k in range(4):
                    a = math.pi / 4 + k * math.pi / 2
                    cuts.append(_cyl(1.7, 30, z1 - 12, xy=(pcd / 2 * math.cos(a), pcd / 2 * math.sin(a)), segs=16))
                cuts.append(_cyl(5.0, 30, z1 - 12, segs=24))
                info["z1"] = z0
        hull_parts += list(extra)
        body = _hull(hull_parts)
        if adds:
            body = _U(body, adds)
        # decorative pocket on both flat side faces of the beam
        if pocket and prox and info["z0"] is not None and info["z1"] is not None and info["z1"] - info["z0"] > 26:
            sil2 = self.silhouette(body)
            jp = prox[0]
            D2s = [jp.D2] + [dd[1].D2 for dd in dists if dd[0] == "jg"]
            if max(D2s) - min(D2s) < 0.6 and not np.allclose(prox[1][:3, :3], self.T["yaw_child"][:3, :3]):
                reg = sil2.buffer(-6.0, join_style=1).intersection(box(-500, info["z0"] + 5, 500, info["z1"] - 3))
                if not reg.is_empty and reg.area > 120:
                    reg = reg.buffer(-2.0, join_style=1).buffer(2.0, join_style=1)
                    D2 = min(D2s)
                    for y1 in (-D2 + 1.0, D2 + 1.0):
                        cuts.append(_xz_ext(reg, y1 - 2.0, y1))
        body = _D(body, cuts)
        return body

    def silhouette(self, man):
        """xz silhouette (convex hull of the projected vertices)."""
        m = man.to_mesh() if not isinstance(man, trimesh.Trimesh) else None
        V = np.asarray(m.vert_properties)[:, :3] if m is not None else man.vertices
        from shapely.geometry import MultiPoint
        return MultiPoint(V[:, [0, 2]]).convex_hull

    # ------------------------------------------------------------ joints + links (all configs)
    def arm(self):
        L = self.lay
        cfg = self.cfg
        T = self.T
        acts = L.keys
        dof = L.dof
        roll = next((n for n in L.names if n.endswith("roll")), None)
        js, je, jw = L.jg["j2_shoulder"], L.jg["j3_elbow"], L.jg["j4_wrist_pitch"]
        jy = L.jg.get("j5_wrist_yaw")
        I4 = np.eye(4)
        # ---- joints (kinematic contract)
        lim = self.limits()
        a = self.asm
        def J(name, typ, par, ch, origin, axis, lo, hi, act=""):
            A = catalog.ACTUATORS.get(act, {})
            cnt = L.counts.get(name, 1)
            eff = float(A.get("torque_nm", 0.0)) * (cnt * 0.95 if cnt > 1 else 1.0)
            return a.add_joint(Joint(name, typ, par, ch, origin, axis, lo, hi, actuator=act, effort=round(eff, 3),
                                     velocity=float(A.get("speed_rad_s", 3.0)) if act else 3.0,
                                     meta={"count": cnt, "servo_sign": -1 if typ == "revolute" and name not in (
                                         "j1_base_yaw",) and not name.endswith("roll") else 1}))
        J("j1_base_yaw", "revolute", "base", "turret", trans((0, 0, L.z_j1)), (0, 0, 1), -2.7, 2.7, acts["j1_base_yaw"])
        J("j2_shoulder", "revolute", "turret", "upper_arm", trans((0, 0, L.h_s)), (0, 1, 0), *lim["j2_shoulder"],
          acts["j2_shoulder"])
        J("j3_elbow", "revolute", "upper_arm", "forearm", trans((0, 0, L.L1)), (0, 1, 0), *lim["j3_elbow"], acts["j3_elbow"])
        J("j4_wrist_pitch", "revolute", "forearm", "wrist", trans((0, 0, L.L2)), (0, 1, 0), *lim["j4_wrist_pitch"],
          acts["j4_wrist_pitch"])
        last = "wrist"
        if dof == 6:
            J("j5_wrist_yaw", "revolute", "wrist", "wrist_yaw", trans((0, 0, L.d45)), (1, 0, 0), *lim["j5_wrist_yaw"],
              acts["j5_wrist_yaw"])
            last = "wrist_yaw"
        if roll:
            J(roll, "revolute", last, "gripper_base", trans((0, 0, L.wl5 if dof == 6 else L.wl)), (0, 0, 1), -2.8, 2.8,
              acts[roll])
        else:
            a.add_joint(Joint("wrist_fixed", "fixed", last, "gripper_base", trans((0, 0, L.wl))))
        # ---- hardware at each joint
        self.joint_hw(js, "turret", T["j2_par"], "upper_arm", I4, "j2", "shoulder", "upper_arm")
        self.joint_hw(je, "upper_arm", T["j3_par"], "forearm", I4, "j3", "elbow", "forearm")
        self.joint_hw(jw, "forearm", T["j4_par"], "wrist", I4, "j4", "wrist", "wrist_link")
        if dof == 6:
            self.joint_hw(jy, "wrist", T["j5_par"], "wrist_yaw", T["yaw_child"], "j5", "wrist_yaw", "wrist_yaw")
        if roll:
            self.roll_hw(L.Ar, last, T["roll_par"], "gripper_base", T["roll_child"], "jr", "roll", "hand_mount")
        # ---- structure
        builder = {"prototype": self.links_printed, "production": self.links_printed, "pilot": self.links_pilot}[cfg]
        builder()

    def link_specs(self):
        """(link, prox (jg, T) | None, dists, step, label) for every arm link."""
        L, T = self.lay, self.T
        I4 = np.eye(4)
        js, je, jw = L.jg["j2_shoulder"], L.jg["j3_elbow"], L.jg["j4_wrist_pitch"]
        out = [("upper_arm", (js, I4), [("jg", je, T["j3_par"])], "upper_arm", "upper arm"),
               ("forearm", (je, I4), [("jg", jw, T["j4_par"])], "forearm", "forearm")]
        if L.dof == 6:
            jy = L.jg["j5_wrist_yaw"]
            out.append(("wrist", (jw, I4), [("jg", jy, T["j5_par"])], "wrist_link", "wrist"))
            out.append(("wrist_yaw", (jy, T["yaw_child"]), [("roll", L.Ar, T["roll_par"])], "wrist_yaw", "wrist yaw"))
        elif L.dof == 5:
            out.append(("wrist", (jw, I4), [("roll", L.Ar, T["roll_par"])], "wrist_link", "wrist"))
        else:
            out.append(("wrist", (jw, I4), [("flange", 21.0, L.wl - 5.0, L.wl, 30.0)], "wrist_link", "wrist"))
        return out

    def links_printed(self):
        """Prototype: one FDM part per link. Production: two injection-moulded half shells per link (uniform wall,
        split on the arm's mid plane), a stamped internal stiffener and a snap-on joint cap."""
        prod = self.cfg == "production"
        for link, prox, dists, step, label in self.link_specs():
            body = self.link_body(prox, dists)
            short = {"upper_arm": "ua", "forearm": "fa", "wrist": "wr", "wrist_yaw": "wy"}[link]
            if not prod:
                self.struct(f"{short}_body", link, body, step, (0, 0, 0), role="shell", label=f"{label} (printed)")
            else:
                inner = self.shrink_body(prox, dists, MOLD_T)
                shell = _D(body, inner) if inner is not None else body
                jp, Tp = prox
                yax = _vec(Tp, (0, 1, 0))
                for side, sg in (("l", -1), ("r", 1)):
                    half = _I(shell, _xf(_box2(-500, 500, 0.2 if sg > 0 else -500, 500 if sg > 0 else -0.2, -500, 900), Tp))
                    self.struct(f"{short}_shell_{side}", link, half, step, yax * 14.0 * sg, role="shell",
                                label=f"{label} shell, {'left' if sg < 0 else 'right'} half (moulded PC)")
                self.stiffener(link, prox, dists, step, short)
            self.joint_cap(link, prox, step, short)

    def shrink_body(self, prox, dists, t):
        """Inner cavity for moulded shells: the same hull with every primitive shrunk by the wall thickness."""
        parts = []
        jp, Tp = prox
        parts.append(_xf(_ycyl(max(jp.r_ch - t, 2), -jp.D2 + t, jp.D2 - t, f=0.5), Tp))
        for dd in dists:
            if dd[0] == "jg":
                _, j, T = dd
                parts.append(_xf(_box2(-j.hw + t, j.hw - t, -j.D2 + t, j.D2 - t, j.z_floor + t, j.z_cup - t), T))
            elif dd[0] == "roll":
                _, A, T = dd
                x0, x1 = -A.so - A.L / 2 - WALL + t, -A.so + A.L / 2 + WALL - t
                parts.append(_xf(_box2(x0, x1, -A.W / 2 - WALL + t, A.W / 2 + WALL - t, min(A.zb, A.zi) - FLOOR + t,
                                       A.zt - 1.6 - t), T))
            elif dd[0] == "flange":
                _, r, z0, z1, pcd = dd
                parts.append(_cyl(r - t, z1 - z0 - 2 * t, z0 + t))
        inner = _hull(parts)
        # keep the clevis cheeks and bridge solid
        inner = _D(inner, _xf(_box2(-500, 500, -500, 500, -500, jp.z_b + T_BR + t), Tp))
        return inner

    def stiffener(self, link, prox, dists, step, short):
        """Stamped sheet-metal channel inside the moulded shells (production)."""
        jp, Tp = prox
        z0 = _pt(Tp, (0, 0, jp.z_b + T_BR + MOLD_T + 1))[2]
        z1 = None
        for dd in dists:
            if dd[0] == "jg":
                z1 = min(_pt(dd[2], (0, 0, dd[1].z_floor + MOLD_T + 1))[2], z1 or 1e9)
            elif dd[0] == "roll":
                z1 = _pt(dd[2], (0, 0, min(dd[1].zb, dd[1].zi) - FLOOR + MOLD_T + 1))[2]
        if z1 is None or z1 - z0 < 15 or abs(_vec(Tp, (0, 1, 0))[1]) < 0.5:
            return
        w = min(jp.hw, 12.0) * 2 - 2
        hgt = min(jp.D2 - MOLD_T - 2.5, 14.0)
        self.sheet_channel(f"{short}_stiffener", link, z0, z1, w, hgt, step, role="sheet", explode=(-30, 0, 0))

    def joint_cap(self, link, prox, step, short, y_face=None):
        """Snap-on cap over the idler-side bolts (accent colour)."""
        jp, Tp = prox
        yf = jp.D2 if y_face is None else y_face
        r = jp.r_ch - 2.6
        cap = _ycyl(r, yf - 0.6, yf + 1.0, f=0.7, segs=64)
        cap = _D(cap, _ycyl(r - 1.6, yf + 0.6, yf + 2, f=0.0, segs=48))
        self.struct(f"{short}_joint_cap", link, _xf(cap, Tp), step, _vec(Tp, (0, 1, 0)) * 26.0, role="accent",
                    kind="cover", label="joint cap")

    def sheet_channel(self, name, link, z0, z1, w, h, step, role="sheet", explode=(0, 0, 0), T=None):
        """U channel along z (base in the x = -w/2 plane... flanges towards +x), real flat pattern + bends."""
        t = SHEET_T
        Rb = 1.5
        L = z1 - z0
        prof = Polygon([(-w / 2, -h), (-w / 2 + t, -h), (-w / 2 + t, h - t), (w / 2 - t, h - t), (w / 2 - t, -h),
                        (w / 2, -h), (w / 2, h), (-w / 2, h)])
        # profile in (x, y), extruded along z
        man = _mv(_ext(prof, 0, L), 0, 0, z0)
        for zz in np.linspace(z0 + 12, z1 - 12, max(int(L // 40), 1)):
            man = _D(man, _xf(_cyl(2.0, 10, -5, segs=16), trans((0, h - t / 2, zz)) @ R_ZY))
        if T is not None:
            man = _xf(man, T)
        k = 0.44
        ba = math.pi / 2 * (Rb + k * t)
        legs = [2 * h - Rb - t, w - 2 * (Rb + t), 2 * h - Rb - t]
        flat_w = sum(legs) + 2 * ba
        u = 0.0
        bends = []
        for i, lg in enumerate(legs[:-1]):
            u += lg + ba / 2
            bends.append({"line": [[round(u, 2), 0.0], [round(u, 2), round(L, 2)]], "angle_deg": 90.0, "radius": Rb})
            u += ba / 2
        flat = box(0, 0, flat_w, L)
        mat, col, proc, opts = self.look[role]
        if proc != "sheet_metal":
            mat, col, proc, opts = "Al5052", C_SHEET_RAW, "sheet_metal", ("sheet_metal",)
        self.add(name, link, man, "structural", step, explode, material=mat, color=col, process=proc,
                 process_options=opts, profile=flat, profile_frame=np.eye(4), thickness=t,
                 sheet={"thickness": t, "bends": bends, "k_factor": k}, label="sheet-metal channel")

    # ------------------------------------------------------------ pilot: CNC side plates + sheet-metal brackets
    def plate_holes(self, j: _JG, side):
        """Holes (canonical xz polygons) a side plate needs at its proximal joint: horn or idler pattern."""
        A = j.A
        hs = []
        if side == "horn" or j.count == 2:
            Ta = j.T_acts[0] if side == "horn" else j.T_acts[-1]
            for hx, hy, hd in A.horn_holes:
                p = _pt(Ta, (hx, hy, 0))
                hs.append(Point(p[0], p[2]).buffer(hd / 2 + 0.25, quad_segs=8))
            hs.append(Point(0, 0).buffer(4.2, quad_segs=16))
        else:
            Ta = j.T_acts[0]
            if A.hobby:
                hs.append(Point(0, 0).buffer(11.05, quad_segs=24))
            else:
                hl = A.holes.get("idler") or []
                for hx, hy, hz in hl[:4]:
                    p = _pt(Ta, (hx, hy, A.zi))
                    hs.append(Point(p[0], p[2]).buffer(1.2, quad_segs=8))
                hs.append(Point(0, 0).buffer(1.6 if hl else 1.4, quad_segs=10))
        return hs

    def links_pilot(self):
        L = self.lay
        for link, prox, dists, step, label in self.link_specs():
            jp, Tp = prox
            short = {"upper_arm": "ua", "forearm": "fa", "wrist": "wr", "wrist_yaw": "wy"}[link]
            Tinv = np.linalg.inv(Tp)
            # envelope in the proximal canonical frame
            prim = [Point(0, 0).buffer(jp.r_ch, quad_segs=24)]
            z_end = None
            bodies = []
            for dd in dists:
                if dd[0] == "jg":
                    _, j, T = dd
                    C = Tinv @ T
                    bx = np.array([_pt(C, (x, y, z)) for x in (-j.hw, j.hw) for y in (-1, 1) for z in (j.z_floor, j.z_cup)])
                    prim.append(_rect(bx[:, 0].min(), bx[:, 0].max(), bx[:, 2].min(), bx[:, 2].max()))
                    z_end = bx[:, 2].min()
                    bodies.append(("jg", j, T))
                elif dd[0] == "roll":
                    _, A, T = dd
                    C = Tinv @ T
                    z_end = _pt(C, (0, 0, min(A.zb, A.zi) - FLOOR))[2]
                    prim.append(_rect(-jp.neck, jp.neck, jp.z_b, z_end + 2))
                    bodies.append(dd)
                elif dd[0] == "flange":
                    _, r, z0, z1, pcd = dd
                    z_end = z0
                    prim.append(_rect(-jp.neck, jp.neck, jp.z_b, z0 + 1))
                    bodies.append(dd)
            sil = unary_union(prim).convex_hull
            D2m = max([jp.D2] + [dd[1].D2 for dd in dists if dd[0] == "jg" and abs(_vec(Tinv @ dd[2], (0, 1, 0))[1]) > 0.5])
            z0b = jp.z_b
            # lightening windows between the bridge and the distal bracket
            win = []
            zA, zB = z0b + T_BR + 14.0, (z_end if z_end is not None else z0b + 40) - 10.0
            if zB - zA > 22:
                core = sil.buffer(-8.0, join_style=1).intersection(box(-500, zA, 500, zB))
                nwin = max(1, int((zB - zA) // 48))
                edges = np.linspace(zA, zB, nwin + 1)
                for a0, a1 in zip(edges[:-1], edges[1:]):
                    w_ = core.intersection(box(-500, a0 + 3.0, 500, a1 - 3.0))
                    if not w_.is_empty and w_.area > 60:
                        win.append(w_.buffer(-3.0, join_style=1).buffer(3.0, join_style=1))
            br_z = z0b + SHEET_T / 2
            br_holes = [Point(x, z0b + 9.0).buffer(1.7, quad_segs=8) for x in (-jp.neck * 0.55, jp.neck * 0.55)]
            so_pts = []
            if zB - zA > 30:
                zm = (zA + zB) / 2
                xf_ = sil.intersection(box(-500, zm - 0.5, 500, zm + 0.5)).bounds
                so_pts = [(xf_[2] - 6.5, zA - 6.0), (xf_[2] - 6.5, zB + 4.0)]
            so_holes = [Point(x, z).buffer(1.7, quad_segs=8) for x, z in so_pts]
            cr_holes = []
            if z_end is not None:
                cr_holes = [Point(x, z_end + 7.0).buffer(1.7, quad_segs=8) for x in (-8.0, 8.0)]
            for side, y0, y1 in (("horn", -D2m, -D2m + PLATE_T), ("idler", D2m - PLATE_T, D2m)):
                poly = sil
                for h in self.plate_holes(jp, side) + br_holes + so_holes + cr_holes + win:
                    poly = poly.difference(h)
                poly = poly.buffer(0)
                man = _xf(_xf(_plate(poly, PLATE_T, chamfer=0.45), _xz_frame(y1)), Tp)
                mat, col, proc, opts = self.look["plate"]
                self.add(f"{short}_plate_{side}", link, man, "structural", step, _vec(Tp, (0, -1 if side == "horn" else 1, 0)) * 22,
                         material=mat, color=col, process=proc, process_options=opts, profile=poly,
                         profile_frame=Tp @ _xz_frame(y1), thickness=PLATE_T, label=f"{label} side plate ({side} side)")
            # spacers between plate and horn / idler where the plates sit outside the cheek faces
            gap_h = jp.y_h - (-D2m + PLATE_T)
            gap_i = (D2m - PLATE_T) - jp.y_i
            for nm, ya, yb in (("horn", -D2m + PLATE_T, jp.y_h), ("idler", jp.y_i, D2m - PLATE_T)):
                if yb - ya > 0.4:
                    sp = _D(_ycyl(jp.r_ch - 3.0, ya, yb, f=0.3), _ycyl(4.2, ya - 1, yb + 1, f=0.0))
                    self.add(f"{short}_{nm}_spacer", link, _xf(sp, Tp), "structural", step, _vec(Tp, (0, 1, 0)) * 12,
                             material="Al6061", color=C_ANOD, process="cnc_turn", process_options=("cnc_turn",),
                             label="spacer disc")
            # bridge bracket: U with flanges up along the plates
            self.u_bracket(f"{short}_bridge", link, Tp, (-jp.neck * 0.9, jp.neck * 0.9), (-D2m + PLATE_T, D2m - PLATE_T),
                           z0b, 16.0, +1, step, holes_z=z0b + 9.0)
            # hex standoffs
            if so_pts:
                for k, (x, z) in enumerate(so_pts):
                    hp = _man(components.hex_prism(5.5, 2 * (D2m - PLATE_T), 0.0, 0.3, hole=2.5))
                    so = _xf(hp, trans((x, -(D2m - PLATE_T), z)) @ R_ZY)
                    self.add(f"{short}_standoff_{k}", link, _xf(so, Tp), "structural", step, (0, 0, 0), material="Al6061",
                             color="#9aa0a8", process="purchased", sku=f"M3_hex_standoff_{2 * (D2m - PLATE_T):.0f}mm",
                             group=f"standoff_{2 * (D2m - PLATE_T):.0f}", label="M3 hex standoff")
                fr = []
                for x, z in so_pts:
                    for sg in (-1, 1):
                        fr.append(Tp @ _seat((x, sg * D2m, z), (0, sg, 0)))
                self.bolts(f"{short}_standoff_screws", link, "M3x6", "bhcs", fr, step)
            # distal: sheet cradle (jg) or machined roll / flange block
            for dd in bodies:
                if dd[0] == "jg":
                    _, j, T = dd
                    C = Tinv @ T
                    bx = np.array([_pt(C, (x, y, z)) for x in (-j.A.W / 2, j.A.W / 2) for y in j.case_y for z in j.z_body])
                    self.u_bracket(f"{short}_cradle", link, Tp, (bx[:, 0].min() - 1.0, bx[:, 0].max() + 1.0),
                                   (-D2m + PLATE_T, D2m - PLATE_T), bx[:, 2].min() - 0.2 - SHEET_T, z_end + 14.0 - (bx[:, 2].min() - SHEET_T),
                                   +1, step, holes_z=z_end + 7.0, base_holes=True)
                else:
                    if dd[0] == "roll":
                        _, A, T = dd
                        blk, a2, c2 = self.roll_cup(A)
                        base = _xf(_rbox2(-jp.neck, jp.neck, -D2m + PLATE_T + 0.2, D2m - PLATE_T - 0.2,
                                          jp.z_b + SHEET_T + 0.1, jp.z_b + SHEET_T + 4.0, r=2.0, f=0.6), Tp)
                        geo = _D(_U(_hull(_xf(blk, T), base), [_xf(a, T) for a in a2]), [_xf(c, T) for c in c2])
                    else:
                        _, r, z0, z1, pcd = dd
                        geo = _U(_fcyl(r, z1 - z0, f=1.0, z0=z0), _xf(_rbox2(-jp.neck, jp.neck, -D2m + PLATE_T + 0.2,
                                                                             D2m - PLATE_T - 0.2, jp.z_b + SHEET_T + 0.1,
                                                                             z0 + 0.5, r=2.0, f=0.6), Tp))
                        geo = _D(geo, [_cyl(1.7, 30, z1 - 12, xy=(pcd / 2 * math.cos(a), pcd / 2 * math.sin(a)), segs=16)
                                       for a in (math.pi / 4 + k * math.pi / 2 for k in range(4))] + [_cyl(5.0, 30, z1 - 12)])
                    mat, col = ("Al6061", C_ANOD_DK)
                    self.add(f"{short}_mount_block", link, geo, "structural", step, (0, 0, 18), material=mat, color=col,
                             process="cnc_mill", process_options=("cnc_mill", "mjf_print"), label=f"{label} machined mount")
            # MJF cable cover on the back between the plates
            if zB - zA > 24:
                zc = (zA + zB) / 2
                xs = sil.intersection(box(-500, zA, 500, zB)).bounds
                xb = xs[0]
                cov = _rbox2(xb + 0.5, xb + 4.0, -D2m + PLATE_T + 0.3, D2m - PLATE_T - 0.3, zA - 4, zB + 2, r=1.2, f=0.8)
                cov = _U(cov, _rbox2(xb + 3.0, xb + 9.0, -D2m + PLATE_T + 0.3, -D2m + PLATE_T + 2.3, zA - 4, zB + 2, r=0.8, f=0.4),
                         _rbox2(xb + 3.0, xb + 9.0, D2m - PLATE_T - 2.3, D2m - PLATE_T - 0.3, zA - 4, zB + 2, r=0.8, f=0.4))
                self.struct(f"{short}_cable_cover", link, _xf(cov, Tp), "wiring", _vec(Tp, (-1, 0, 0)) * 20, role="cover",
                            kind="cover", label="cable cover (MJF PA12)")
            self.joint_cap(link, prox, step, short, y_face=D2m)

    def u_bracket(self, name, link, Tp, xr, yr, z0, h, up, step, holes_z=None, base_holes=False, role="sheet"):
        """Sheet-metal U bracket in the canonical frame Tp: base plate at z0 (thickness SHEET_T) spanning x in xr,
        y in yr (outer faces against the side plates), flanges rising +z by h along the plates."""
        t, Rb, k = SHEET_T, 1.2, 0.44
        x0, x1 = xr
        y0, y1 = yr
        prof = Polygon([(y0, z0 + h), (y0, z0), (y1, z0), (y1, z0 + h), (y1 - t, z0 + h), (y1 - t, z0 + t),
                        (y0 + t, z0 + t), (y0 + t, z0 + h)])
        # extrude the (y, z) profile along x
        man = _xf(_ext(prof, 0, x1 - x0), np.array([[0, 0, 1, x0], [1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 0, 1.0]]))
        cuts = []
        if holes_z is not None:
            for xx in (x0 + (x1 - x0) * 0.25, x0 + (x1 - x0) * 0.75):
                cuts.append(_xf(_cyl(1.7, (y1 - y0) + 4, -2, segs=16), trans((xx, y0 - 1, holes_z)) @ R_ZY))
        if base_holes:
            cuts.append(_cyl(5.0, 6, z0 - 2, xy=((x0 + x1) / 2, 0.0), segs=24))
        man = _D(man, cuts)
        ba = math.pi / 2 * (Rb + k * t)
        legs = [h - Rb - t, (y1 - y0) - 2 * (Rb + t), h - Rb - t]
        flat_w = sum(legs) + 2 * ba
        u, bends = 0.0, []
        for lg in legs[:-1]:
            u += lg + ba / 2
            bends.append({"line": [[round(u, 2), 0.0], [round(u, 2), round(x1 - x0, 2)]], "angle_deg": 90.0, "radius": Rb})
            u += ba / 2
        flat = box(0, 0, flat_w, x1 - x0)
        hl = []
        if holes_z is not None:
            for xx in (0.25, 0.75):
                for uu in (holes_z - z0 - t / 2 * 0, flat_w - (holes_z - z0)):
                    hl.append(Point(uu, (x1 - x0) * xx).buffer(1.7, quad_segs=8))
        if base_holes:
            hl.append(Point(flat_w / 2, (x1 - x0) / 2).buffer(5.0, quad_segs=12))
        if hl:
            flat = flat.difference(unary_union(hl))
        mat, col, proc, opts = self.look[role]
        if proc != "sheet_metal":
            mat, col, proc, opts = "Al5052", C_SHEET, "sheet_metal", ("sheet_metal",)
        self.add(name, link, _xf(man, Tp), "structural", step, _vec(Tp, (0, 0, -1)) * 16, material=mat, color=col,
                 process=proc, process_options=opts, profile=flat, profile_frame=np.eye(4), thickness=t,
                 sheet={"thickness": t, "bends": bends, "k_factor": k}, label="sheet-metal U bracket")
        if holes_z is not None:
            fr = []
            for xx in (x0 + (x1 - x0) * 0.25, x0 + (x1 - x0) * 0.75):
                for yy, sg in ((y0 - PLATE_T, -1), (y1 + PLATE_T, 1)):
                    fr.append(Tp @ _seat((xx, yy, holes_z), (0, sg, 0)))
            self.bolts(f"{name}_screws", link, "M3x8", "bhcs", fr, step)

    # ------------------------------------------------------------ joint limits
    def limits(self):
        L = self.lay
        g = L.grip
        out = {}

        def sil_pts(points):
            from shapely.geometry import MultiPoint
            return MultiPoint([(p[0], p[2]) for p in points]).convex_hull

        def box_pts(T, x0, x1, y0, y1, z0, z1):
            return [_pt(T, (x, y, z)) for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)]

        def disc_pts(T, r, n=20):
            return [_pt(T, (r * math.cos(a), 0, r * math.sin(a))) for a in np.linspace(0, 2 * math.pi, n, endpoint=False)]

        hand = lambda T, z0: box_pts(T, -g["px"], g["px"], -15, 15, z0, z0 + g["tcp_z"] + g["tip"])  # noqa: E731
        js, je, jw = L.jg["j2_shoulder"], L.jg["j3_elbow"], L.jg["j4_wrist_pitch"]
        T3, T4 = trans((0, 0, L.L1)), trans((0, 0, L.L2))
        # children (in the joint's canonical frame)
        ua = sil_pts(disc_pts(np.eye(4), js.r_ch) + box_pts(T3, -je.hw, je.hw, -1, 1, je.z_floor, je.z_cup)
                     + box_pts(T3, -je.A.W / 2, je.A.W / 2, -1, 1, je.z_body[0], je.z_body[1]))
        fa = sil_pts(disc_pts(np.eye(4), je.r_ch) + box_pts(T4, -jw.hw, jw.hw, -1, 1, jw.z_floor, jw.z_cup)
                     + box_pts(T4, -jw.A.W / 2, jw.A.W / 2, -1, 1, jw.z_body[0], jw.z_body[1]))
        if L.dof == 6:
            jy = L.jg["j5_wrist_yaw"]
            wr_pts = disc_pts(np.eye(4), jw.r_ch) + box_pts(self.T["j5_par"], -jy.hw, jy.hw, -jy.D2, jy.D2, jy.z_floor, jy.z_cup) \
                + hand(np.eye(4), L.d45 + L.wl5)
        else:
            Ar = L.Ar
            wr_pts = disc_pts(np.eye(4), jw.r_ch) + hand(np.eye(4), L.wl)
            if Ar is not None:
                wr_pts += box_pts(self.T["roll_par"], -Ar.so - Ar.L / 2 - WALL, -Ar.so + Ar.L / 2 + WALL, -Ar.W / 2 - WALL,
                                  Ar.W / 2 + WALL, min(Ar.zb, Ar.zi) - FLOOR, 0)
        wr = sil_pts(wr_pts)
        # parents
        R_t = 54.0
        Rphi = affinity.rotate
        pi_s, po_s = _jg_parent_regions(js, js.hw, 10.0)
        pi_s = Rphi(pi_s, -L.phi_s, origin=(0, 0), use_radians=True)
        po_s = Rphi(po_s, -L.phi_s, origin=(0, 0), use_radians=True)
        base_reg = unary_union([_rect(-R_t, R_t, -L.h_s - 2, -L.h_s + 7), _rect(-260, 75, -L.base_h - 5, -L.h_s - 8)])
        pi_s, po_s = pi_s.union(base_reg), po_s.union(base_reg)
        ci, co = _child_regions(js, ua)
        out["j2_shoulder"] = _limits(pi_s, po_s, ci, co, hi_cap=2.2, lo_cap=-2.0)
        pi_e, po_e = _jg_parent_regions(je, js.r_ch, L.L1 + je.z_floor)
        ci, co = _child_regions(je, fa)
        out["j3_elbow"] = _limits(pi_e, po_e, ci, co, hi_cap=2.5, lo_cap=-2.5)
        pi_w, po_w = _jg_parent_regions(jw, je.r_ch, L.L2 + jw.z_floor)
        ci, co = _child_regions(jw, wr)
        out["j4_wrist_pitch"] = _limits(pi_w, po_w, ci, co, hi_cap=2.3, lo_cap=-2.3)
        if L.dof == 6:
            jy = L.jg["j5_wrist_yaw"]
            pi_y, po_y = _jg_parent_regions(jy, jw.r_ch, L.d45 + jy.z_floor)
            wy = sil_pts(disc_pts(np.eye(4), jy.r_ch) + hand(np.eye(4), L.wl5)
                         + box_pts(np.eye(4), -L.Ar.W / 2 - WALL, L.Ar.W / 2 + WALL, -40, 15, L.wl5 - L.Ar.overall - FLOOR, L.wl5))
            ci, co = _child_regions(jy, wy)
            out["j5_wrist_yaw"] = _limits(pi_y, po_y, ci, co, hi_cap=1.7, lo_cap=-1.7)
        out = {k: (round(lo, 3), round(hi, 3)) for k, (lo, hi) in out.items()}
        self.lay.lim = out
        return out
