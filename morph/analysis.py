"""Fast closed-form checks: grip force budget and finger bending.

These run in microseconds, so the explorer can screen hundreds of variants
before spending simulation time. MuJoCo then checks the survivors.
"""

from __future__ import annotations

import math

from .design import FingerGeometry
from .materials import MATERIALS
from .spec import Design

G = 9.81


def wedge_gain(pad: str, contact_half_angle_deg: float) -> float:
    """Friction gain from pad shape. A wedge of half-angle a boosts holding force by 1/sin(a)."""
    if pad == "flat" or contact_half_angle_deg <= 0:
        return 1.0
    if pad == "v":
        return 1.0 / math.sin(math.radians(45.0))
    alpha = max(90.0 - contact_half_angle_deg, 15.0)  # cap: past this it is form closure
    return 1.0 / math.sin(math.radians(alpha))


def analyze(design: Design, geom: FingerGeometry) -> dict:
    f, o = design.finger, design.object
    mat = MATERIALS[f.material]
    printed = "fdm_print" in mat.processes and design.fab.process in ("auto", "fdm_print")
    yield_mpa = mat.yield_mpa * (mat.print_derate if printed else 1.0)

    weight = o.mass_g / 1000.0 * G
    gain = wedge_gain(f.pad, geom.contact_half_angle_deg)
    n_required = design.safety_factor * weight / (2.0 * o.friction * gain)
    F = design.gripper.force_n

    # Cantilever from the mount face, load at pad centre.
    L = geom.pad_center_u
    I = f.width_mm * f.thickness_mm ** 3 / 12.0          # mm^4
    E = mat.E_gpa * 1000.0                                # N/mm^2
    deflection = F * L ** 3 / (3.0 * E * I)
    stress = F * L * (f.thickness_mm / 2.0) / I           # MPa
    stress_sf = yield_mpa / stress if stress > 0 else float("inf")

    mass_g = geom.volume_mm3 / 1000.0 * mat.density_g_cc
    closed_face_gap = o.diameter_mm - 2.0 * (f.pad_depth_mm if f.pad != "flat" else 0.0)

    issues = []
    if F < n_required:
        issues.append(f"grip force {F:.1f} N < required {n_required:.1f} N")
    if stress_sf < 2.0:
        issues.append(f"bending safety factor {stress_sf:.2f} < 2")
    if deflection > 1.0:
        issues.append(f"tip deflection {deflection:.2f} mm > 1 mm")
    if design.gripper.stroke_mm < o.diameter_mm + 10:
        issues.append("gripper stroke too small to open around object")
    if closed_face_gap < 1.0:
        issues.append("pads bottom out before squeezing the object")

    return {
        "material": f.material,
        "printed": printed,
        "object_weight_n": round(weight, 3),
        "wedge_gain": round(gain, 3),
        "required_grip_n": round(n_required, 2),
        "applied_grip_n": F,
        "grip_margin": round(F / n_required, 2),
        "tip_deflection_mm": round(deflection, 3),
        "max_stress_mpa": round(stress, 2),
        "bending_sf": round(stress_sf, 2),
        "finger_mass_g": round(mass_g, 1),
        "contact_half_angle_deg": round(geom.contact_half_angle_deg, 1),
        "pass": not issues,
        "issues": issues,
    }
