"""Parametric finger CAD.

The finger is a 2.5D profile (so the same geometry can be printed, laser cut,
or CNC routed from plate) extruded by ``width_mm``.

Profile frame (u, v), millimetres:
  u: along the finger, 0 at the mount face, +L at the fingertip
  v: across the finger, 0 at the outer face, increasing toward the object
The object is gripped lying on its side, so its cross-section lies in this
plane and the pad contour can wrap it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import shapely
from shapely.geometry import Point, Polygon, box
from shapely.geometry.polygon import orient

from .spec import Design

MOUNT_H = 10.0

# FSR 402 (Interlink): 18.3 mm disc, 12.7 mm active area, 0.45 mm thick
FSR = {"fsr402": {"disc_d": 18.3, "active_d": 12.7, "thick": 0.45, "min_n": 0.2, "max_n": 20.0}}
SOCKET = 4.0        # sensor socket block on the finger face
SLOT_FLOOR = 1.5    # material between beam and slot bottom
DOVETAIL_DEG = 30.0
PRELOAD = 0.15      # pad tail presses into the sensor by this much
PAD_GAP = 0.4       # pad body clears the socket face so all load goes through the sensor
PAD_BACKING = 1.5   # minimum pad material behind the contact surface


@dataclass
class FingerGeometry:
    profile: Polygon             # (u, v) polygon with mount holes
    width_mm: float
    pad_center_u: float
    pad_face_v: float
    contact_half_angle_deg: float  # how far the pad wraps the object (0 = flat)
    notes: list
    parts: dict = None           # sensorized fingers are two parts: {"finger": ..., "pad": ...}
    sensor: str = "none"
    pad_height_mm: float = 0.0   # as built (may be raised to seat a sensor)

    def part_polys(self) -> dict:
        return self.parts or {"finger": self.profile}

    @property
    def area_mm2(self) -> float:
        return sum(p.area for p in self.part_polys().values())

    @property
    def volume_mm3(self) -> float:
        return self.area_mm2 * self.width_mm


def object_radius(design: Design) -> float:
    return design.object.diameter_mm / 2.0


def _pad_cut(design: Design, shape, u_c, cut_floor_v, v_face, ph, d, notes):
    """Cut the contact surface into the pad. Returns (shape, contact half-angle)."""
    f = design.finger
    if f.pad == "contour" and design.object.shape in ("cylinder", "sphere"):
        R = object_radius(design) + 0.5  # running clearance
        shape = shape.difference(Point(u_c, cut_floor_v + R).buffer(R, quad_segs=48))
        half_chord = min(math.sqrt(max(R * R - (R - d) ** 2, 0.0)), ph / 2.0)
        return shape, math.degrees(math.asin(min(half_chord / R, 1.0)))
    if f.pad == "v":
        hw = min(d, ph / 2.0 - 0.5)
        notch = Polygon([(u_c - hw, v_face + 0.01), (u_c + hw, v_face + 0.01), (u_c, v_face - hw)])
        return shape.difference(notch), 45.0
    if f.pad == "contour":
        notes.append(f"contour pad ignored for {design.object.shape}; using flat face")
    return shape, 0.0


def _clean(shape, notes, what):
    shape = shapely.make_valid(shape).buffer(0)
    if shape.geom_type != "Polygon":  # keep the largest piece if a cut split it
        shape = max(shape.geoms, key=lambda g: g.area)
        notes.append(f"{what} split by pad cut; kept largest piece")
    return orient(shape, 1.0)


def build_profile(design: Design) -> FingerGeometry:
    f = design.finger
    L, t, d, ph = f.length_mm, f.thickness_mm, f.pad_depth_mm, f.pad_height_mm
    notes = []
    sensor = getattr(f, "sensor", "none")
    if sensor in FSR and ph < FSR[sensor]["disc_d"] + 4.6:
        ph = FSR[sensor]["disc_d"] + 4.6
        notes.append(f"pad height raised to {ph:.1f} mm to seat the {sensor}")
    if ph > L:
        ph = L
        notes.append("pad height clipped to finger length")
    mount_w = max(t, f.mount_hole_spacing_mm + 2 * f.mount_hole_d_mm + 2.0)
    u_c = L - ph / 2.0
    holes = [Point(-MOUNT_H / 2.0, mount_w / 2.0 + s * f.mount_hole_spacing_mm).buffer(
        f.mount_hole_d_mm / 2.0, quad_segs=16) for s in (-0.5, 0.5)]
    base = box(-MOUNT_H, 0, 0, mount_w).union(box(0, 0, L, t))

    if sensor not in FSR:
        v_face = t + d
        shape = base.union(box(L - ph, 0, L, v_face))
        shape, half_angle = _pad_cut(design, shape, u_c, t, v_face, ph, d, notes)
        for h in holes:
            shape = shape.difference(h)
        shape = _clean(shape, notes, "profile")
        return FingerGeometry(shape, f.width_mm, u_c, v_face, half_angle, notes, pad_height_mm=ph)

    # Sensorized: finger with a dovetail socket, separate pad whose tail bears on the FSR.
    fsr = FSR[sensor]
    b = fsr["disc_d"] + 0.6                                   # slot bottom width
    depth = SOCKET - SLOT_FLOOR
    a = b - 2 * depth * math.tan(math.radians(DOVETAIL_DEG))  # slot mouth width
    v_floor, v_sock = t + SLOT_FLOOR, t + SOCKET
    slot = Polygon([(u_c - b / 2, v_floor), (u_c + b / 2, v_floor),
                    (u_c + a / 2, v_sock + 0.01), (u_c - a / 2, v_sock + 0.01)])
    finger = base.union(box(L - ph, 0, L, v_sock)).difference(slot)
    for h in holes:
        finger = finger.difference(h)
    finger = _clean(finger, notes, "finger")

    v0 = v_sock + PAD_GAP
    v_face = v0 + PAD_BACKING + d
    c = 0.2  # flank clearance
    tail_bot = v_floor + fsr["thick"] - PRELOAD
    tail = Polygon([(u_c - b / 2 + c, tail_bot), (u_c + b / 2 - c, tail_bot),
                    (u_c + a / 2 - c, v_sock), (u_c - a / 2 + c, v_sock)])
    neck = box(u_c - a / 2 + c, v_sock - 0.01, u_c + a / 2 - c, v0 + 0.01)
    pad = box(L - ph, v0, L, v_face).union(tail).union(neck)
    pad, half_angle = _pad_cut(design, pad, u_c, v0 + PAD_BACKING, v_face, ph, d, notes)
    pad = _clean(pad, notes, "pad")

    assembled = _clean(finger.union(pad).buffer(0.3, quad_segs=8).buffer(-0.3, quad_segs=8), notes, "assembly")
    for h in holes:
        assembled = orient(assembled.difference(h), 1.0)
    notes.append(f"{sensor} sits in a dovetail socket; pad slides in along the width axis")
    return FingerGeometry(assembled, f.width_mm, u_c, v_face, half_angle, notes,
                          parts={"finger": finger, "pad": pad}, sensor=sensor, pad_height_mm=ph)


def to_mesh(geom, width_mm: float | None = None):
    """Extrude a profile (FingerGeometry or polygon) into a watertight trimesh (x=u, y=v, z=width)."""
    import manifold3d as m3d
    import trimesh

    poly = geom.profile if isinstance(geom, FingerGeometry) else geom
    width = geom.width_mm if isinstance(geom, FingerGeometry) else width_mm
    rings = [np.asarray(poly.exterior.coords)[:-1]]
    rings += [np.asarray(r.coords)[:-1] for r in poly.interiors]
    cs = m3d.CrossSection([r.tolist() for r in rings], m3d.FillRule.EvenOdd)
    solid = cs.extrude(width)
    mesh = solid.to_mesh()
    verts = np.asarray(mesh.vert_properties)[:, :3]
    faces = np.asarray(mesh.tri_verts)
    return trimesh.Trimesh(verts, faces, process=True)


def export_stl(geom: FingerGeometry, path: str) -> dict:
    """Export the assembled finger; sensorized designs also get one STL per part next to it."""
    mesh = to_mesh(geom)
    mesh.export(path)
    out = {"watertight": bool(mesh.is_watertight), "volume_mm3": float(mesh.volume),
           "bbox_mm": [round(float(x), 2) for x in mesh.extents]}
    if geom.parts:
        out["parts"] = {}
        for name, poly in geom.parts.items():
            m = to_mesh(poly, geom.width_mm)
            m.export(path.replace(".stl", f"_{name}.stl"))
            out["parts"][name] = {"watertight": bool(m.is_watertight), "volume_mm3": round(float(m.volume), 1)}
    return out


def profile_svg_path(poly: Polygon, scale=1.0, du=0.0, dv=0.0) -> str:
    def ring(coords):
        pts = [f"{(u + du) * scale:.2f},{(v + dv) * scale:.2f}" for u, v in coords]
        return "M" + " L".join(pts) + " Z"

    parts = [ring(poly.exterior.coords)] + [ring(r.coords) for r in poly.interiors]
    return " ".join(parts)
