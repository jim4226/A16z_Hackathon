"""A mesh as the OBJECT to grasp: STL/OBJ/PLY/OFF/GLB/glTF/3MF (and STEP with the optional CAD extra) ->
Morph's object.* design keys, with an estimated mass.

  res = object_changes("egg.stl")
  res["changes"]  # {"object.name": "egg", "object.shape": "cylinder", "object.diameter_mm": 44.0,
                  #  "object.length_mm": 57.0, "object.mass_g": 55.0, "finger.length_mm": ..., ...}

Units: an object whose largest extent is under 2 is taken to be in metres (STL has no units); 2 to 1000 is
millimetres. The person can override with answers {"units": "mm"|"cm"|"m"|"in"}. Shape: the tightest of the
minimum enclosing cylinder, the oriented box and the sphere (fill ratio = mesh volume / primitive volume).
Mass: volume x density (1.0 g/cm3 unless given), always labelled an estimate.
"""

from __future__ import annotations

import os
import re

import numpy as np

from .common import ImportFailed

UNIT_MM = {"mm": 1.0, "cm": 10.0, "m": 1000.0, "in": 25.4}
WIDTH_MM = (26.0, 75.0)          # what the generated gripper holds (capability envelope, contracts section 6)


def load(path: str, fmt: str | None = None, bundle: str | None = None):
    from .detect import sniff
    from .urdf import load_mesh

    fmt = fmt or sniff(path)
    if fmt in ("step", "iges"):
        from .urdf import load_step
        return load_step(path), "mm (STEP)"
    if fmt not in ("stl", "obj", "ply", "off", "glb", "gltf", "3mf", "dae"):
        raise ImportFailed(f"{os.path.basename(path)} is not a mesh Morph can read ({fmt}).")
    import trimesh
    m = load_mesh(path, fmt, bundle or os.path.dirname(path))
    return trimesh.Trimesh(m.vertices, m.faces, process=True), None      # merged vertices: closed meshes read as closed


def guess_units(mesh, answers: dict | None = None) -> dict:
    ext = float(np.max(mesh.extents)) if len(mesh.vertices) else 0.0
    u = (answers or {}).get("units")
    if u:
        return {"unit": u, "scale": UNIT_MM[u], "why": "you picked it", "confident": True}
    if ext <= 0:
        raise ImportFailed("The mesh has no size.")
    if ext < 2.0:
        return {"unit": "m", "scale": 1000.0, "why": f"largest extent {ext:.3g}: metres", "confident": ext > 0.01}
    if ext <= 1000.0:
        return {"unit": "mm", "scale": 1.0, "why": f"largest extent {ext:.3g}: millimetres", "confident": 10.0 <= ext <= 400.0}
    return {"unit": "mm", "scale": 1.0, "why": f"largest extent {ext:.3g}: very large for an object to grasp; check the units",
            "confident": False}


def fit(mesh) -> dict:
    """Tightest primitive: {"shape", "diameter_mm", "length_mm", "volume_cm3", "fill_ratio", "extents_mm", "closed"}."""
    import trimesh

    m = mesh.copy()
    m.apply_translation(-m.bounding_box.centroid)
    try:
        hull = m.convex_hull
    except Exception:  # noqa: BLE001  (qhull: fewer than 4 points or everything in one plane)
        raise ImportFailed("The mesh encloses no volume (a flat or open surface?); use a closed solid.") from None
    closed = bool(m.is_watertight)
    vol = abs(float(m.volume)) if closed else float(hull.volume)
    if vol <= 0:
        raise ImportFailed("The mesh encloses no volume (a flat or open surface?); use a closed solid.")
    try:
        cyl = trimesh.bounds.minimum_cylinder(hull)
        cyl_v = np.pi * cyl["radius"] ** 2 * cyl["height"]
    except Exception:  # noqa: BLE001  (degenerate hulls)
        cyl, cyl_v = None, np.inf
    obb = np.sort(hull.bounding_box_oriented.primitive.extents)
    sph_r = float(np.linalg.norm(hull.vertices - hull.vertices.mean(0), axis=1).max())
    cand = {"cylinder": cyl_v, "box": float(np.prod(obb)), "sphere": 4.0 / 3.0 * np.pi * sph_r ** 3}
    shape = max(cand, key=lambda k: vol / cand[k] if cand[k] > 0 else 0)
    if shape == "cylinder":
        d, L = 2.0 * cyl["radius"], cyl["height"]
    elif shape == "box":
        d, L = obb[1], obb[2]
    else:
        d = L = 2.0 * sph_r
    return {"shape": shape, "diameter_mm": round(float(d), 1), "length_mm": round(float(L), 1),
            "volume_cm3": round(vol / 1000.0, 2), "fill_ratio": round(float(vol / cand[shape]), 2),
            "extents_mm": [round(float(x), 1) for x in obb], "closed": closed}


def _name(path: str, answers: dict) -> str:
    if answers.get("name"):
        return str(answers["name"])[:40]
    stem = os.path.splitext(os.path.basename(path))[0]
    s = re.sub(r"[_\-.]+", " ", stem).strip()
    s = re.sub(r"\s+v?\d+(\.\d+)*$", "", s, flags=re.I).strip()
    return (s or "imported object")[:40]


FSR_FINGER = {"finger.sensor": "fsr402", "finger.width_mm": 20.0, "fab.process": "fdm_print", "finger.material": "PETG"}


def handling(name: str, shape: str, diameter_mm: float) -> dict:
    """How the new object is handled, so nothing carries over from the old one: friction and the squeeze that damages it
    from Morph's preset when the name is one ("egg" is a raw egg: 25 N), else a robust object (0 N); and the force-
    sensing finger when the object is fragile or narrower than the plain finger closes on (capability.min_width)."""
    from ..studio.capability import min_width
    from ..studio.intent import OBJECTS, object_alias

    preset = object_alias(str(name).lower())
    p = OBJECTS.get(preset) or {}
    out = {"object.friction": float(p.get("friction", 0.5)), "object.max_force_n": float(p.get("max_force_n", 0.0))}
    if out["object.max_force_n"] > 0 or diameter_mm < min_width(shape, "none"):
        out.update(FSR_FINGER)
    return {"changes": out, "preset": preset}


def object_changes(path: str, fmt: str | None = None, answers: dict | None = None, bundle: str | None = None) -> dict:
    from ..studio.intent import fit_finger

    answers = answers or {}
    mesh, note = load(path, fmt, bundle)
    units = guess_units(mesh, answers) if note is None else {"unit": "mm", "scale": 1.0, "why": "STEP files are in mm",
                                                              "confident": True}
    mesh.apply_scale(units["scale"])
    f = fit(mesh)
    rho = float(answers.get("density_g_cc") or 1.0)
    mass = round(f["volume_cm3"] * rho, 1)
    warnings = []
    if not units["confident"]:
        warnings.append(f"Check the units: {units['why']}. Pick mm, cm, m or inches if the size looks wrong.")
    if not WIDTH_MM[0] <= f["diameter_mm"] <= WIDTH_MM[1]:
        warnings.append(f"The gripper holds objects about {WIDTH_MM[0]:.0f} to {WIDTH_MM[1]:.0f} mm across; this one is "
                        f"{f['diameter_mm']:.0f} mm, so the build may report it can't grasp it.")
    if mass > 500:
        warnings.append(f"About {mass:.0f} g (estimated): heavier than most tabletop arms lift.")
    if not f["closed"]:
        warnings.append("The mesh isn't watertight; its volume (and mass) come from its convex hull.")
    obj = {"shape": f["shape"], "diameter_mm": f["diameter_mm"], "length_mm": f["length_mm"]}
    name = _name(path, answers)
    hand = handling(name, f["shape"], f["diameter_mm"])
    changes = {"object.name": name, "object.shape": f["shape"],
               "object.diameter_mm": f["diameter_mm"], "object.length_mm": f["length_mm"],
               "object.mass_g": max(mass, 1.0), **fit_finger(obj), **hand["changes"]}
    return {"changes": changes, "fit": f, "units": units, "warnings": warnings, "preset": hand["preset"],
            "mass": {"g": max(mass, 1.0), "density_g_cc": rho, "basis": "estimated: volume x density"
                     + (" (you set the density)" if answers.get("density_g_cc") else " (1.0 g/cm3, like water)")},
            "faces": int(len(mesh.faces))}


def primitive(cand: dict):
    import trimesh

    ch = cand["changes"]
    d, L = ch["object.diameter_mm"], ch["object.length_mm"]
    if ch["object.shape"] == "sphere":
        return trimesh.creation.icosphere(subdivisions=3, radius=d / 2)
    if ch["object.shape"] == "box":
        e = (cand.get("fit") or {}).get("extents_mm") or [d, d, L]
        return trimesh.creation.box(e)
    return trimesh.creation.cylinder(radius=d / 2, height=L, sections=48)


def preview_meshes(path: str, cand: dict) -> list:
    """[(object mesh centred on the table, colour)] in mm, z-up."""
    mesh, note = load(path, cand.get("format"))
    scale = (cand.get("units") or {}).get("scale") or 1.0
    if note is None:
        mesh.apply_scale(scale)
    mesh.apply_translation(-mesh.bounding_box.centroid)
    mesh.apply_translation([0, 0, -mesh.bounds[0][2]])
    return [(mesh, "#c9ced6")]
