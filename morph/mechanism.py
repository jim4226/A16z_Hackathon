"""Printable rack-and-pinion parallel gripper, sized to the servo and the object.

One servo turns a printed pinion; two slotted racks above and below it move in
opposite directions, and each finger bolts to a rack. Racks slide on M3 bolts
through slots in a back plate the servo screws to. All geometry is generated in
the assembled pose with the fingers touching the object (servo at 90 deg), in
the same world frame the Studio uses: x = jaw travel, y = up, z = toward the
back plate, object centred at the origin.

Servo dimensions are typical datasheet values; check yours with calipers.
"""

from __future__ import annotations

import math
import os

from shapely import affinity
from shapely.geometry import Point, Polygon, box
from shapely.ops import unary_union

from .design import mount_dims, to_mesh
from .spec import Design

MODULE = 1.5
PRESSURE_DEG = 20.0
BACKLASH = 0.2
RACK_H = 9.0          # rack body height (y)
RACK_T = 6.0          # rack / pinion thickness (z)
BOSS_T = 3.0          # standoff between finger and rack, clears the guide bolt heads
PLATE_T = 5.0
OVERTRAVEL = 3.0      # extra closing past contact before the slots bottom out
CLEAR = 1.0

SERVOS = {  # body l x w x h, ear-hole spacing along l, across w, shaft offset from body centre along l, horn dia
    "sg90": dict(l=22.5, w=12.2, h=22.7, ear_l=27.8, ear_w=0.0, shaft_off=5.9, horn_d=20.0, screw_d=2.0),
    "mg90s": dict(l=22.8, w=12.2, h=28.5, ear_l=27.8, ear_w=0.0, shaft_off=5.9, horn_d=20.0, screw_d=2.0),
    "mg996r": dict(l=40.7, w=19.7, h=42.9, ear_l=49.5, ear_w=10.0, shaft_off=10.2, horn_d=24.0, screw_d=2.8),
    "ds3218": dict(l=40.0, w=20.0, h=40.5, ear_l=49.5, ear_w=10.0, shaft_off=10.0, horn_d=24.0, screw_d=2.8),
}


def object_center_v(design: Design, geom) -> float:
    """Distance from the finger's outer face to the object's centre when gripped."""
    f, R = design.finger, design.object.diameter_mm / 2
    if f.pad == "contour" and design.object.shape in ("cylinder", "sphere"):
        return geom.pad_face_v - f.pad_depth_mm + R + 0.5
    if f.pad == "v":
        hw = min(f.pad_depth_mm, (geom.pad_height_mm or f.pad_height_mm) / 2 - 0.5)
        return geom.pad_face_v - hw + R * math.sqrt(2)
    return geom.pad_face_v + R


# ---------------------------------------------------------------- gears

def pinion_profile(n: int, m: float = MODULE) -> Polygon:
    rp = n * m / 2
    rb = rp * math.cos(math.radians(PRESSURE_DEG))
    ra, rf = rp + m, rp - 1.25 * m
    inv = lambda a: math.tan(a) - a  # noqa: E731
    half = (math.pi * m / 2 - BACKLASH) / (2 * rp) + inv(math.radians(PRESSURE_DEG))
    ta = math.sqrt(max((ra / rb) ** 2 - 1, 0))
    flank = []
    for i in range(12):
        t = ta * i / 11
        r = rb * math.sqrt(1 + t * t)
        flank.append((r, half - (t - math.atan(t))))
    pts = [(max(rf, 0.5), half)] if rf < rb else []
    tooth_r = [(r, a) for r, a in flank]
    left = pts + tooth_r
    right = [(r, -a) for r, a in reversed(left)]
    tooth = Polygon([(r * math.cos(a), r * math.sin(a)) for r, a in left + right] + [(0, 0)])
    teeth = [affinity.rotate(tooth, 360.0 * k / n, origin=(0, 0)) for k in range(n)]
    return unary_union([Point(0, 0).buffer(rf, quad_segs=32)] + teeth).buffer(0)


def rack_teeth(x0: float, x1: float, pitch_y: float, direction: int, phase_x: float, m: float = MODULE):
    """Trapezoid teeth between x0..x1 on a pitch line; direction +1 = teeth point up."""
    p = math.pi * m
    tan = math.tan(math.radians(PRESSURE_DEG))
    half_pitch = (p / 2 - BACKLASH) / 2
    polys = []
    k0 = math.floor((x0 - phase_x) / p) - 1
    k1 = math.ceil((x1 - phase_x) / p) + 1
    for k in range(k0, k1 + 1):
        c = phase_x + k * p
        hb = half_pitch + 1.25 * m * tan
        ht = max(half_pitch - m * tan, 0.15)
        yb, yt = pitch_y - direction * 1.25 * m, pitch_y + direction * m
        poly = Polygon([(c - hb, yb), (c + hb, yb), (c + ht, yt), (c - ht, yt)])
        clipped = poly.intersection(box(x0, min(yb, yt) - 1, x1, max(yb, yt) + 1))
        if not clipped.is_empty and clipped.area > 0.05:
            polys.append(clipped)
    return unary_union(polys)


# ---------------------------------------------------------------- assembly

def plan(design: Design, geom, servo: str) -> dict:
    f = design.finger
    sv = SERVOS[servo]
    m = MODULE
    n = max(10, round(2 * design.electronics.pinion_radius_mm / m))
    r = n * m / 2
    ra = r + m
    cv = object_center_v(design, geom)
    mount_h, mount_w = mount_dims(f)
    u_c, w = geom.pad_center_u, f.width_mm
    issues, notes = [], []

    T = min(15.0, cv - mount_w - 2 * OVERTRAVEL - CLEAR - 2 * m - 1)
    if T < 4:
        issues.append(f"only {T:.1f} mm of jaw travel fits around a Ø{design.object.diameter_mm} mm object; "
                      f"use deeper pads or a smaller pinion")
        T = max(T, 1.0)
    if -cv + mount_w + OVERTRAVEL > -(ra + CLEAR):
        issues.append("left finger's rack arm would hit the pinion; object too small for this layout")

    y_mount_top = u_c + mount_h          # finger mount spans y in [u_c, u_c + mount_h]
    yB_bot = y_mount_top + 0.6
    yB_root = yB_bot + RACK_H
    y_p = yB_root + 1.25 * m + r          # pinion centre
    yA_root = y_p + r + 1.25 * m
    yA_top = yA_root + RACK_H

    # rack A: top, drives the LEFT finger (x < 0); rack B: bottom, drives the RIGHT finger
    xAr = T + 2 * m + 1
    xBl = -xAr
    ear_A = box(-cv, u_c, -cv + mount_w, yA_root + 0.5)
    ear_B = box(cv - mount_w, u_c, cv, yB_bot + 0.5)
    def rack_phase(contact_deg):
        """Rack tooth x-centre so rack teeth sit in the pinion's gaps at the contact point."""
        pitch_deg = 360.0 / n
        k = round(contact_deg / pitch_deg)
        eps = math.radians(k * pitch_deg - contact_deg)       # nearest pinion tooth, CCW offset
        x_tooth = -r * eps if contact_deg == 90 else r * eps  # top: CCW moves -x; bottom: CCW moves +x
        return x_tooth + math.pi * m / 2
    body_A = box(-cv, yA_root, xAr, yA_top)
    teeth_A = rack_teeth(-cv + mount_w + 0.8, xAr, y_p + r, -1, rack_phase(90))
    body_B = box(xBl, yB_bot, cv, yB_root)
    teeth_B = rack_teeth(xBl, cv - mount_w - 0.8, y_p - r, +1, rack_phase(270))

    b1 = -cv + 3 + OVERTRAVEL + 1.7
    b2 = max(xAr - 2 - T - 1.7, b1 + 6.0)
    slot_A = box(b1 - OVERTRAVEL - 1.7, (yA_root + yA_top) / 2 - 1.7, b2 + T + 1.7, (yA_root + yA_top) / 2 + 1.7)
    slot_B = affinity.scale(slot_A, xfact=-1, origin=(0, 0))
    slot_B = affinity.translate(slot_B, 0, ((yB_bot + yB_root) / 2) - ((yA_root + yA_top) / 2))
    if b2 + T + 1.7 > xAr - 1:
        issues.append("rack too short for its guide slot at this travel")

    holes_A = [Point(-cv + mount_w / 2, u_c + mount_h / 2 + s * f.mount_hole_spacing_mm / 2).buffer(1.7, quad_segs=12) for s in (-1, 1)]
    holes_B = [affinity.scale(h, xfact=-1, origin=(0, 0)) for h in holes_A]
    rack_A = unary_union([body_A, teeth_A, ear_A]).difference(slot_A).difference(unary_union(holes_A))
    rack_B = unary_union([body_B, teeth_B, ear_B]).difference(slot_B).difference(unary_union(holes_B))
    boss_A = box(-cv, u_c, -cv + mount_w, y_mount_top).difference(unary_union(holes_A))
    boss_B = affinity.scale(boss_A, xfact=-1, origin=(0, 0))

    pin = affinity.translate(pinion_profile(n), 0, y_p)
    pin = pin.difference(Point(0, y_p).buffer(3.2, quad_segs=16))
    for a in (0, 180):
        pin = pin.difference(Point(8 * math.cos(math.radians(a)), y_p + 8 * math.sin(math.radians(a))).buffer(1.0, quad_segs=8))

    # back plate with servo mount (servo long axis vertical, body above the shaft)
    sy = y_p + sv["shaft_off"]
    ears = [(sx, sy + s * sv["ear_l"] / 2) for s in (-1, 1) for sx in ((-sv["ear_w"] / 2, sv["ear_w"] / 2) if sv["ear_w"] else (0.0,))]
    px = cv + T + 4
    py0, py1 = yB_bot - 3, max(yA_top + 3, sy + sv["ear_l"] / 2 + 5, sy + sv["l"] / 2 + 3) + 8
    plate = box(-px, py0, px, py1).difference(Point(0, y_p).buffer(sv["horn_d"] / 2 + 1.0, quad_segs=32))
    bolt_pts = []
    for (yy, sign) in (((yA_root + yA_top) / 2, 1), ((yB_bot + yB_root) / 2, -1)):
        for b in (b1, b2):
            bolt_pts.append((sign * b, yy))
    for (bx, by) in bolt_pts:
        plate = plate.difference(Point(bx, by).buffer(1.7, quad_segs=12))
    for (ex, ey) in ears:
        plate = plate.difference(Point(ex, ey).buffer(sv["screw_d"] / 2, quad_segs=12))
    for sx in (-px + 6, px - 6):
        plate = plate.difference(Point(sx, py1 - 5).buffer(2.2, quad_segs=12))   # M4 to the robot arm

    z_f = w / 2
    layers = {  # name -> (profile, z0, z1, printed?)
        "rack_a": (rack_A, z_f + BOSS_T, z_f + BOSS_T + RACK_T, True),
        "rack_b": (rack_B, z_f + BOSS_T, z_f + BOSS_T + RACK_T, True),
        "boss_a": (boss_A, z_f, z_f + BOSS_T, True),
        "boss_b": (boss_B, z_f, z_f + BOSS_T, True),
        "pinion": (pin, z_f + BOSS_T, z_f + BOSS_T + RACK_T, True),
        "plate": (plate, z_f + BOSS_T + RACK_T + 0.3, z_f + BOSS_T + RACK_T + 0.3 + PLATE_T, True),
    }
    pz = z_f + BOSS_T + RACK_T + 0.3 + PLATE_T
    servo_box = box(-sv["w"] / 2, sy - sv["l"] / 2, sv["w"] / 2, sy + sv["l"] / 2)
    visual = {"servo": (servo_box, pz, pz + sv["h"], False),
              "horn": (Point(0, y_p).buffer(sv["horn_d"] / 2, quad_segs=32), pz - PLATE_T + 0.6, pz - 0.2, False)}

    deg_T, deg_ot = math.degrees(T / r), math.degrees(OVERTRAVEL / r)
    notes.append(f"assemble at servo 90°: center the servo, then mesh both racks with the pads touching a "
                 f"Ø{design.object.diameter_mm:g} mm reference (the object)")
    notes.append(f"bolts: 4× M3×16 guide bolts with washers + nylock nuts behind the plate; 4× M3×{int(w + BOSS_T + RACK_T + 4)} "
                 f"through finger + boss + rack; servo screws ×{len(ears)}")
    return {
        "servo": servo, "module": m, "teeth": n, "pitch_radius_mm": r, "travel_mm": round(T, 1),
        "opening_mm": round(2 * T, 1), "overtravel_mm": OVERTRAVEL,
        "servo_open_deg": round(90 - deg_T, 1), "servo_closed_deg": round(90 + deg_ot, 1),
        "servo_sweep_deg": round(deg_T + deg_ot, 1), "center_v_mm": round(cv, 2),
        "envelope_mm": [round(2 * px, 1), round(py1 - min(py0, u_c), 1)],
        "issues": issues, "notes": notes, "_layers": layers, "_visual": visual,
    }


def _extrude(poly, z0, z1):
    import trimesh

    polys = [poly] if poly.geom_type == "Polygon" else list(poly.geoms)
    meshes = []
    for p in polys:
        if p.area < 0.05:
            continue
        mesh = to_mesh(p, z1 - z0)
        mesh.apply_translation([0, 0, z0])
        meshes.append(mesh)
    return trimesh.util.concatenate(meshes)


def build(design: Design, geom, servo: str, outdir: str) -> dict:
    mech = plan(design, geom, servo)
    os.makedirs(outdir, exist_ok=True)
    files, printed = [], {}
    for name, (poly, z0, z1, is_printed) in {**mech["_layers"], **mech["_visual"]}.items():
        mesh = _extrude(poly, z0, z1)
        mesh.export(os.path.join(outdir, f"{name}.stl"))
        files.append(f"{name}.stl")
        if is_printed:
            printed[name] = {"volume_mm3": round(float(mesh.volume), 1), "watertight": bool(mesh.is_watertight),
                             "thickness_mm": round(z1 - z0, 2)}
    out = {k: v for k, v in mech.items() if not k.startswith("_")}
    out["files"] = files
    out["printed"] = printed
    return out


def printed_profiles(design: Design, geom, servo: str) -> dict:
    """name -> (polygon, thickness) for fab planning."""
    mech = plan(design, geom, servo)
    return {k: (v[0], v[2] - v[1]) for k, v in mech["_layers"].items()}
