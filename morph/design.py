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


@dataclass
class FingerGeometry:
    profile: Polygon             # (u, v) polygon with mount holes
    width_mm: float
    pad_center_u: float
    pad_face_v: float
    contact_half_angle_deg: float  # how far the pad wraps the object (0 = flat)
    notes: list

    @property
    def area_mm2(self) -> float:
        return self.profile.area

    @property
    def volume_mm3(self) -> float:
        return self.profile.area * self.width_mm


def object_radius(design: Design) -> float:
    return design.object.diameter_mm / 2.0


def build_profile(design: Design) -> FingerGeometry:
    f = design.finger
    L, t, d, ph = f.length_mm, f.thickness_mm, f.pad_depth_mm, f.pad_height_mm
    notes = []
    if ph > L:
        ph = L
        notes.append("pad height clipped to finger length")
    mount_w = max(t, f.mount_hole_spacing_mm + 2 * f.mount_hole_d_mm + 2.0)
    v_face = t + d
    u_c = L - ph / 2.0

    shape = box(-MOUNT_H, 0, 0, mount_w).union(box(0, 0, L, t)).union(box(L - ph, 0, L, v_face))

    half_angle = 0.0
    if f.pad == "contour" and design.object.shape in ("cylinder", "sphere"):
        R = object_radius(design) + 0.5  # running clearance
        cut = Point(u_c, t + R).buffer(R, quad_segs=48)
        shape = shape.difference(cut)
        half_chord = min(math.sqrt(max(R * R - (R - d) ** 2, 0.0)), ph / 2.0)
        half_angle = math.degrees(math.asin(min(half_chord / R, 1.0)))
    elif f.pad == "v":
        hw = min(d, ph / 2.0 - 0.5)
        notch = Polygon([(u_c - hw, v_face + 0.01), (u_c + hw, v_face + 0.01), (u_c, v_face - hw)])
        shape = shape.difference(notch)
        half_angle = 45.0
    elif f.pad == "contour":
        notes.append(f"contour pad ignored for {design.object.shape}; using flat face")

    for s in (-0.5, 0.5):
        hole = Point(-MOUNT_H / 2.0, mount_w / 2.0 + s * f.mount_hole_spacing_mm).buffer(
            f.mount_hole_d_mm / 2.0, quad_segs=16)
        shape = shape.difference(hole)

    if shape.geom_type != "Polygon":  # keep the largest piece if a cut split it
        shape = max(shape.geoms, key=lambda g: g.area)
        notes.append("profile split by pad cut; kept largest piece")
    shape = orient(shapely.make_valid(shape).buffer(0), 1.0)
    return FingerGeometry(shape, f.width_mm, u_c, v_face, half_angle, notes)


def to_mesh(geom: FingerGeometry):
    """Extrude the profile into a watertight trimesh (x=u, y=v, z=width)."""
    import manifold3d as m3d
    import trimesh

    rings = [np.asarray(geom.profile.exterior.coords)[:-1]]
    rings += [np.asarray(r.coords)[:-1] for r in geom.profile.interiors]
    cs = m3d.CrossSection([r.tolist() for r in rings], m3d.FillRule.EvenOdd)
    solid = cs.extrude(geom.width_mm)
    mesh = solid.to_mesh()
    verts = np.asarray(mesh.vert_properties)[:, :3]
    faces = np.asarray(mesh.tri_verts)
    return trimesh.Trimesh(verts, faces, process=True)


def export_stl(geom: FingerGeometry, path: str) -> dict:
    mesh = to_mesh(geom)
    mesh.export(path)
    return {"watertight": bool(mesh.is_watertight), "volume_mm3": float(mesh.volume),
            "bbox_mm": [round(float(x), 2) for x in mesh.extents]}


def profile_svg_path(poly: Polygon, scale=1.0, du=0.0, dv=0.0) -> str:
    def ring(coords):
        pts = [f"{(u + du) * scale:.2f},{(v + dv) * scale:.2f}" for u, v in coords]
        return "M" + " L".join(pts) + " Z"

    parts = [ring(poly.exterior.coords)] + [ring(r.coords) for r in poly.interiors]
    return " ".join(parts)
