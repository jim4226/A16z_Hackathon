"""Import an existing robot (URDF + meshes) into a Morph Assembly.

Open-source robots ship URDFs whose visual meshes are STL/OBJ in metres. This
importer turns every visual into a Part (mesh in the link frame, mm), every
URDF joint into a Joint (origin 4x4 in mm, axis, limits, mimic), and attaches
a profile describing the robot's electronics, materials and processes so the
rest of the platform (electronics editing, manufacturing, sourcing, manual,
viewer) works on it like on a generated robot.

  asm = load_urdf("so101.urdf", profile)
"""

from __future__ import annotations

import math
import os
import re
import xml.etree.ElementTree as ET

import numpy as np
import trimesh

from ..studio.security import inside
from .assembly import Assembly, Joint, Part, Step, rpy, trans


def _floats(s, n=3, default=0.0):
    if s is None:
        return [default] * n
    vals = [float(x) for x in s.split()]
    return (vals + [default] * n)[:n]


def _origin(el) -> np.ndarray:
    o = el.find("origin") if el is not None else None
    if o is None:
        return np.eye(4)
    xyz = np.array(_floats(o.get("xyz"))) * 1000.0
    r, p, y = _floats(o.get("rpy"))
    return trans(xyz) @ rpy(r, p, y)


def _safe(name: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_") or "part"


def _resolve(path: str, urdf_dir: str, mesh_dirs: list, bundle: str) -> str | None:
    """A mesh file inside the robot's folder `bundle`, or None. Absolute paths, file:// and other URLs, drive
    letters and ../ that climbs out of the folder (symlinks included) are never followed."""
    p = (path or "").strip().replace("\\", "/")
    if p.startswith("package://"):
        p = p[len("package://"):]
    elif re.match(r"[A-Za-z][A-Za-z0-9+.-]*:", p) or p.startswith("/"):
        return None
    parts = [s for s in p.split("/") if s not in ("", ".")]
    if not parts:
        return None
    cands = [os.path.join(urdf_dir, *parts)] + [os.path.join(d, parts[-1]) for d in mesh_dirs]
    for i in range(1, len(parts)):  # package://pkg/meshes/x.stl -> try meshes/x.stl relative to the urdf
        cands.append(os.path.join(urdf_dir, *parts[i:]))
    return next((c for c in cands if os.path.isfile(c) and inside(bundle, c)), None)


def load_urdf(urdf_path: str, profile: dict | None = None, mesh_dirs: list | None = None, bundle: str | None = None) -> Assembly:
    """Meshes load only from inside `bundle` (the robot's folder; default: the URDF's folder).
       profile keys (all optional):
       name, root, color, material, process, part_overrides {regex: {kind, material, process, color, sku}},
       actuators {joint: catalog_key}, arm_joints [...], gripper_joint, tcp {link, xyz}, home {joint: value},
       ready {...}, steps [{title, parts_regex, instructions, fasteners, tools}], electronics {...}, meta {...}"""
    profile = profile or {}
    urdf_dir = os.path.dirname(os.path.abspath(urdf_path))
    bundle = bundle or urdf_dir
    mesh_dirs = mesh_dirs or [os.path.join(urdf_dir, "meshes"), os.path.join(urdf_dir, "assets")]
    root_el = ET.parse(urdf_path).getroot()
    materials = {}
    for m in root_el.findall("material"):
        c = m.find("color")
        if c is not None and m.get("name"):
            materials[m.get("name")] = _floats(c.get("rgba"), 4, 1.0)

    joints_el = root_el.findall("joint")
    children = {j.find("child").get("link") for j in joints_el}
    link_names = [l.get("name") for l in root_el.findall("link")]
    root = profile.get("root") or next(n for n in link_names if n not in children)
    asm = Assembly(_safe(profile.get("name") or root_el.get("name") or "robot"), root=_safe(root))
    name_map = {n: _safe(n) for n in link_names}

    for j in joints_el:
        jt = j.get("type")
        typ = {"revolute": "revolute", "continuous": "revolute", "prismatic": "prismatic"}.get(jt, "fixed")
        lim = j.find("limit")
        lo = float(lim.get("lower", -math.pi)) if lim is not None else -math.pi
        hi = float(lim.get("upper", math.pi)) if lim is not None else math.pi
        if jt == "continuous":
            lo, hi = -math.pi, math.pi
        if typ == "prismatic":
            lo, hi = lo * 1000.0, hi * 1000.0
        axis = _floats(j.find("axis").get("xyz") if j.find("axis") is not None else "1 0 0")
        mim = j.find("mimic")
        mimic = {"joint": _safe(mim.get("joint")), "multiplier": float(mim.get("multiplier", 1.0)),
                 "offset": float(mim.get("offset", 0.0))} if mim is not None else None
        name = _safe(j.get("name"))
        act = (profile.get("actuators") or {}).get(name, "")
        effort = float(lim.get("effort", 0.0)) if lim is not None else 0.0
        asm.add_joint(Joint(name, typ, name_map[j.find("parent").get("link")], name_map[j.find("child").get("link")],
                            _origin(j), tuple(axis), lo, hi, actuator=act, effort=effort,
                            velocity=float(lim.get("velocity", 3.0)) if lim is not None else 3.0, mimic=mimic))
    for n in link_names:
        if name_map[n] not in asm.links:
            asm.add_link(name_map[n])

    overrides = [(re.compile(k), v) for k, v in (profile.get("part_overrides") or {}).items()]
    for l in root_el.findall("link"):
        lname = name_map[l.get("name")]
        for i, vis in enumerate(l.findall("visual")):
            g = vis.find("geometry")
            mesh_el = g.find("mesh") if g is not None else None
            mesh = None
            if mesh_el is not None:
                path = _resolve(mesh_el.get("filename"), urdf_dir, mesh_dirs, bundle)
                if path is None:
                    continue
                mesh = trimesh.load(path, force="mesh")
                scale = np.array(_floats(mesh_el.get("scale"), 3, 1.0))
                mesh.apply_scale(scale * 1000.0)
            elif g is not None and g.find("box") is not None:
                mesh = trimesh.creation.box(np.array(_floats(g.find("box").get("size"))) * 1000.0)
            elif g is not None and g.find("cylinder") is not None:
                c = g.find("cylinder")
                mesh = trimesh.creation.cylinder(float(c.get("radius")) * 1000.0, float(c.get("length")) * 1000.0)
            elif g is not None and g.find("sphere") is not None:
                mesh = trimesh.creation.icosphere(radius=float(g.find("sphere").get("radius")) * 1000.0)
            if mesh is None or len(mesh.faces) == 0:
                continue
            T_vis = _origin(vis)
            mesh.apply_transform(T_vis)
            base = os.path.splitext(os.path.basename(mesh_el.get("filename")))[0] if mesh_el is not None else f"{lname}_{i}"
            pname = _safe(base)
            k = 2
            while any(p.name == pname for p in asm.parts):
                pname = f"{_safe(base)}_{k}"
                k += 1
            mat_el = vis.find("material")
            rgba = None
            if mat_el is not None:
                c = mat_el.find("color")
                rgba = _floats(c.get("rgba"), 4, 1.0) if c is not None else materials.get(mat_el.get("name"))
            color = profile.get("color", "#e8e8e8")
            if rgba:
                color = "#%02x%02x%02x" % tuple(int(max(0, min(1, v)) * 255) for v in rgba[:3])
            attrs = {"kind": "structural", "material": profile.get("material", "PLA"),
                     "process": profile.get("process", "fdm_print"), "color": color, "sku": ""}
            for rx, ov in overrides:
                if rx.search(pname):
                    attrs.update(ov)
            asm.add_part(Part(name=pname, link=lname, mesh=mesh, kind=attrs["kind"], material=attrs["material"],
                              color=attrs["color"], process=attrs["process"], sku=attrs.get("sku", ""),
                              group=attrs.get("group", pname),
                              meta={"source": "urdf", "visual_origin": T_vis.tolist(),
                                    "mesh_file": os.path.basename(mesh_el.get("filename")) if mesh_el is not None else "",
                                    **attrs.get("meta", {})}))

    asm.meta = {"source": os.path.basename(urdf_path), "imported": True,
                "arm_joints": profile.get("arm_joints") or [j.name for j in asm.joints if j.type != "fixed" and not j.mimic],
                "gripper_joint": profile.get("gripper_joint"), "tcp_link": None,
                "home": profile.get("home") or {}, "ready": profile.get("ready") or {}, **(profile.get("meta") or {})}
    tcp = profile.get("tcp")
    if "tcp" in asm.links and asm.parent_joint("tcp") is not None:
        asm.meta["tcp_link"] = "tcp"
    elif tcp:
        asm.add_joint(Joint("tcp_fixed", "fixed", _safe(tcp["link"]), "tcp", trans(np.array(tcp.get("xyz", (0, 0, 0)), float))))
        asm.meta["tcp_link"] = "tcp"

    # assembly steps from the profile (regex over part names), else one step per link
    steps = profile.get("steps")
    if steps:
        for idx, st in enumerate(steps, 1):
            rx = re.compile(st.get("parts_regex", "$^"))
            names = [p.name for p in asm.parts if p.step == 0 and rx.search(p.name)]
            for p in asm.parts:
                if p.name in names:
                    p.step = idx
            asm.steps.append(Step(idx, st["title"], names, st.get("fasteners", []), st.get("tools", []),
                                  st.get("instructions", ""), st.get("subassembly", "")))
        leftovers = [p for p in asm.parts if p.step == 0]
        if leftovers:
            idx = len(asm.steps) + 1
            for p in leftovers:
                p.step = idx
            asm.steps.append(Step(idx, "Remaining parts", [p.name for p in leftovers]))
    else:
        for idx, link in enumerate(asm.ordered_links(), 1):
            names = [p.name for p in asm.parts if p.link == link]
            if not names:
                continue
            for p in asm.parts:
                if p.link == link:
                    p.step = idx
            asm.steps.append(Step(idx, f"Install {link.replace('_', ' ')}", names))
    # explode: push each part away from its link origin along its centroid direction
    for p in asm.parts:
        c = p.mesh.bounds.mean(axis=0)
        n = np.linalg.norm(c)
        p.explode = tuple((c / n * 30.0) if n > 1e-6 else (0.0, 0.0, 30.0))
    return asm
