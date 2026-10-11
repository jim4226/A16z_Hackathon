"""Robot assembly model: the shared contract between geometry, simulation,
electronics, manufacturing, the assembly manual and the Studio viewer.

Conventions
-----------
* Units: millimetres, radians, kilograms (mass), newton-metres (torque).
* Frames: robot base frame is z-up, x forward, origin on the table under the
  base axis. Every Part mesh is expressed in the frame of the Link it belongs
  to. A Joint's `origin` is the 4x4 transform from the parent link frame to the
  child link frame when the joint value is 0; the joint then rotates (revolute)
  about / translates (prismatic) along `axis`, expressed in the child frame.
* Names: link and part names are ASCII identifiers ([a-z0-9_]); they become
  glTF node names `link__<name>` and `part__<name>`.

Exports
-------
* export_glb(path):  one glTF binary, node tree base -> ... -> links, each
  link node's local matrix = joint origin (joint at 0), parts as mesh children.
* to_robot_json(path): links, joints, parts, cables, steps for the viewer.
* export_urdf(dir):  URDF + one STL per link (metres), for ROS / other tools.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field

import numpy as np
import trimesh

PROCESSES = ("fdm_print", "sla_print", "mjf_print", "cnc_mill", "cnc_turn", "laser_cut", "sheet_metal",
             "injection_mold", "pcb_assembly", "cable_assembly", "purchased")
KINDS = ("structural", "actuator", "horn", "bearing", "fastener", "electronics", "pcb", "sensor", "pad",
         "cable", "cover", "visual")

# PBR look per material key (base colour comes from Part.color)
MATERIAL_PBR = {
    "PLA": (0.0, 0.55), "PETG": (0.0, 0.45), "Nylon-CF": (0.0, 0.7), "PA12": (0.0, 0.75), "Resin": (0.0, 0.35),
    "ABS": (0.0, 0.5), "PC": (0.0, 0.3), "TPU": (0.0, 0.85), "Delrin": (0.0, 0.4), "Acrylic": (0.0, 0.1),
    "Al6061": (0.85, 0.35), "Al5052": (0.85, 0.4), "Steel": (0.9, 0.3), "Stainless": (0.9, 0.25),
    "FR4": (0.0, 0.5), "Copper": (1.0, 0.3), "Rubber": (0.0, 0.9), "purchased": (0.2, 0.5),
}


def rot(axis, angle) -> np.ndarray:
    return trimesh.transformations.rotation_matrix(angle, np.asarray(axis, float))


def trans(xyz) -> np.ndarray:
    T = np.eye(4)
    T[:3, 3] = xyz
    return T


def rpy(r, p, y) -> np.ndarray:
    return trimesh.transformations.euler_matrix(r, p, y, "sxyz")


def hex_rgba(color: str, alpha: int = 255) -> list:
    c = color.lstrip("#")
    return [int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16), alpha]


@dataclass
class Part:
    name: str
    link: str
    mesh: trimesh.Trimesh                  # in LINK frame, mm
    kind: str = "structural"
    material: str = "PETG"
    color: str = "#c9ced6"
    process: str = "fdm_print"             # current process for the active build config
    process_options: tuple = ()            # alternatives other build configs may pick
    sku: str = ""                          # catalog key for purchased parts (catalog.get(sku))
    group: str = ""                        # identical parts share a group (manufactured once, qty n)
    step: int = 0                          # assembly step that installs this part
    explode: tuple = (0.0, 0.0, 0.0)       # exploded-view offset in LINK frame, mm
    profile: object = None                 # shapely Polygon for flat/2.5D parts (laser, CNC, sheet flat pattern)
    profile_frame: object = None           # 4x4: profile (u, v, w in [0, thickness]) -> link frame
    thickness: float = 0.0
    collide: bool = True                   # include in the physics model
    meta: dict = field(default_factory=dict)

    @property
    def volume_mm3(self) -> float:
        return float(abs(self.mesh.volume)) if self.mesh.is_volume else float(self.mesh.convex_hull.volume)


@dataclass
class Link:
    name: str
    mass_kg: float | None = None           # None -> computed from parts and material densities
    meta: dict = field(default_factory=dict)


@dataclass
class Joint:
    name: str
    type: str                              # revolute | prismatic | fixed
    parent: str
    child: str
    origin: np.ndarray                     # 4x4 parent->child at q = 0 (mm)
    axis: tuple = (0.0, 0.0, 1.0)          # in child frame
    lower: float = -math.pi
    upper: float = math.pi
    home: float = 0.0
    actuator: str = ""                     # catalog key of the driving actuator ("" = passive / mimic)
    effort: float = 0.0                    # N*m (revolute) or N (prismatic) available
    velocity: float = 3.0                  # rad/s or mm/s
    mimic: dict | None = None              # {"joint": name, "multiplier": k, "offset": c}
    meta: dict = field(default_factory=dict)


@dataclass
class Cable:
    name: str
    points: list                           # [(link, (x, y, z)), ...] route through link frames
    diameter: float = 3.0
    color: str = "#222222"
    src: str = ""                          # "U1.J3" style connector refs, free text
    dst: str = ""
    conductors: int = 3
    meta: dict = field(default_factory=dict)


@dataclass
class Step:
    index: int
    title: str
    parts: list                            # part names installed in this step
    fasteners: list = field(default_factory=list)   # [{"spec": "M3x8 SHCS", "qty": 4, "torque_nm": 0.8}]
    tools: list = field(default_factory=list)
    instructions: str = ""
    subassembly: str = ""
    camera: dict = field(default_factory=dict)       # optional viewer hint {"target": [x,y,z], "dist": mm}


DENSITY_G_CC = {"PLA": 1.24, "PETG": 1.27, "Nylon-CF": 1.15, "PA12": 1.01, "Resin": 1.15, "ABS": 1.04, "PC": 1.2,
                "TPU": 1.21, "Delrin": 1.41, "Acrylic": 1.18, "Al6061": 2.70, "Al5052": 2.68, "Steel": 7.85,
                "Stainless": 8.0, "FR4": 1.85, "Copper": 8.96, "Rubber": 1.1}
INFILL = {"fdm_print": 0.45, "mjf_print": 1.0, "sla_print": 1.0}


class Assembly:
    def __init__(self, name: str, root: str = "base"):
        self.name = name
        self.root = root
        self.links: dict[str, Link] = {root: Link(root)}
        self.joints: list[Joint] = []
        self.parts: list[Part] = []
        self.cables: list[Cable] = []
        self.steps: list[Step] = []
        self.meta: dict = {}

    # -------------------------------------------------- building
    def add_link(self, name: str, **kw) -> Link:
        if name in self.links:
            raise ValueError(f"duplicate link {name}")
        self.links[name] = Link(name, **kw)
        return self.links[name]

    def add_joint(self, joint: Joint) -> Joint:
        for n in (joint.parent, joint.child):
            if n not in self.links:
                self.add_link(n)
        if any(j.child == joint.child for j in self.joints):
            raise ValueError(f"link {joint.child} already has a parent joint")
        joint.origin = np.asarray(joint.origin, float)
        self.joints.append(joint)
        return joint

    def add_part(self, part: Part) -> Part:
        if part.link not in self.links:
            raise ValueError(f"part {part.name}: unknown link {part.link}")
        if any(p.name == part.name for p in self.parts):
            raise ValueError(f"duplicate part {part.name}")
        self.parts.append(part)
        return part

    def part(self, name: str) -> Part:
        return next(p for p in self.parts if p.name == name)

    def joint(self, name: str) -> Joint:
        return next(j for j in self.joints if j.name == name)

    # -------------------------------------------------- kinematics
    def parent_joint(self, link: str) -> Joint | None:
        return next((j for j in self.joints if j.child == link), None)

    def ordered_links(self) -> list:
        out, frontier = [], [self.root]
        while frontier:
            n = frontier.pop(0)
            out.append(n)
            frontier += [j.child for j in self.joints if j.parent == n]
        return out

    def joint_values(self, q: dict | None = None) -> dict:
        q = dict(q or {})
        vals = {}
        for j in self.joints:
            if j.mimic:
                continue
            vals[j.name] = q.get(j.name, j.home)
        for j in self.joints:
            if j.mimic:
                m = j.mimic
                vals[j.name] = vals.get(m["joint"], 0.0) * m.get("multiplier", 1.0) + m.get("offset", 0.0)
        return vals

    def joint_motion(self, j: Joint, value: float) -> np.ndarray:
        if j.type == "revolute":
            return rot(j.axis, value)
        if j.type == "prismatic":
            return trans(np.asarray(j.axis, float) * value)
        return np.eye(4)

    def world_transforms(self, q: dict | None = None) -> dict:
        vals = self.joint_values(q)
        T = {self.root: np.eye(4)}
        for name in self.ordered_links()[1:]:
            j = self.parent_joint(name)
            T[name] = T[j.parent] @ j.origin @ self.joint_motion(j, vals.get(j.name, 0.0))
        return T

    def chain(self, tip: str) -> list:
        """Joints from the root to `tip`, in order."""
        out, link = [], tip
        while link != self.root:
            j = self.parent_joint(link)
            out.append(j)
            link = j.parent
        return out[::-1]

    # -------------------------------------------------- mass properties
    def part_mass_kg(self, p: Part) -> float:
        if "mass_g" in p.meta:
            return p.meta["mass_g"] / 1000.0
        rho = DENSITY_G_CC.get(p.material, 1.2)
        fill = INFILL.get(p.process, 1.0)
        return p.volume_mm3 / 1000.0 * rho * fill / 1000.0

    def link_mass(self, link: str) -> tuple[float, np.ndarray]:
        L = self.links[link]
        parts = [p for p in self.parts if p.link == link]
        if not parts:
            return (L.mass_kg or 0.001), np.zeros(3)
        ms = np.array([self.part_mass_kg(p) for p in parts])
        cs = np.array([p.mesh.bounds.mean(axis=0) for p in parts])
        m = float(ms.sum()) if L.mass_kg is None else L.mass_kg
        com = (ms[:, None] * cs).sum(0) / max(ms.sum(), 1e-9)
        return m, com

    def link_mesh(self, link: str) -> trimesh.Trimesh | None:
        ms = [p.mesh for p in self.parts if p.link == link]
        return trimesh.util.concatenate(ms) if ms else None

    # -------------------------------------------------- exports
    def export_glb(self, path: str) -> str:
        from trimesh.visual.material import PBRMaterial

        scene = trimesh.Scene()
        base = scene.graph.base_frame
        scene.graph.update(frame_from=base, frame_to=f"link__{self.root}", matrix=np.eye(4))
        for name in self.ordered_links()[1:]:
            j = self.parent_joint(name)
            scene.graph.update(frame_from=f"link__{j.parent}", frame_to=f"link__{name}", matrix=j.origin)
        for p in self.parts:
            m = p.mesh.copy()
            metal, rough = MATERIAL_PBR.get(p.material, (0.0, 0.5))
            m.visual = trimesh.visual.TextureVisuals(material=PBRMaterial(
                name=f"mat_{p.material}_{p.color.lstrip('#')}", baseColorFactor=hex_rgba(p.color),
                metallicFactor=metal, roughnessFactor=rough))
            scene.add_geometry(m, node_name=f"part__{p.name}", geom_name=f"geom__{p.name}",
                               parent_node_name=f"link__{p.link}")
        data = scene.export(file_type="glb")
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def to_dict(self) -> dict:
        joints = []
        for j in self.joints:
            joints.append({"name": j.name, "type": j.type, "parent": j.parent, "child": j.child,
                           "origin": np.round(j.origin, 6).flatten().tolist(), "axis": list(map(float, j.axis)),
                           "lower": j.lower, "upper": j.upper, "home": j.home, "actuator": j.actuator,
                           "effort": j.effort, "velocity": j.velocity, "mimic": j.mimic, "meta": j.meta})
        links = []
        for name in self.ordered_links():
            m, com = self.link_mass(name)
            pj = self.parent_joint(name)
            links.append({"name": name, "parent_joint": pj.name if pj else None, "mass_kg": round(m, 4),
                          "com": np.round(com, 3).tolist(), "meta": self.links[name].meta})
        parts = []
        for p in self.parts:
            b = p.mesh.bounds
            parts.append({"name": p.name, "link": p.link, "kind": p.kind, "material": p.material, "color": p.color,
                          "process": p.process, "process_options": list(p.process_options), "sku": p.sku,
                          "group": p.group or p.name, "step": p.step, "explode": list(map(float, p.explode)),
                          "thickness": p.thickness, "mass_g": round(self.part_mass_kg(p) * 1000, 2),
                          "bbox": np.round(b, 3).tolist(), "meta": p.meta})
        cables = [{"name": c.name, "points": [{"link": l, "xyz": list(map(float, xyz))} for l, xyz in c.points],
                   "diameter": c.diameter, "color": c.color, "src": c.src, "dst": c.dst, "conductors": c.conductors,
                   "length_mm": round(self.cable_length(c), 1), "meta": c.meta} for c in self.cables]
        steps = [{"index": s.index, "title": s.title, "parts": s.parts, "fasteners": s.fasteners, "tools": s.tools,
                  "instructions": s.instructions, "subassembly": s.subassembly, "camera": s.camera} for s in self.steps]
        return {"name": self.name, "units": "mm", "up": "z", "root": self.root, "links": links, "joints": joints,
                "parts": parts, "cables": cables, "steps": steps, "meta": self.meta}

    def to_robot_json(self, path: str, glb_name: str = "robot.glb") -> str:
        d = self.to_dict()
        d["glb"] = glb_name
        with open(path, "w") as fh:
            json.dump(d, fh)
        return path

    def cable_length(self, c: Cable, q: dict | None = None) -> float:
        T = self.world_transforms(q)
        pts = [(T[l] @ np.r_[np.asarray(x, float), 1.0])[:3] for l, x in c.points]
        L = sum(float(np.linalg.norm(b - a)) for a, b in zip(pts, pts[1:]))
        return L * 1.15 + 40.0  # slack for joint travel + connector service loops

    def export_urdf(self, outdir: str) -> str:
        os.makedirs(os.path.join(outdir, "meshes"), exist_ok=True)
        L = []
        L.append(f'<?xml version="1.0"?>\n<robot name="{self.name}">')
        for name in self.ordered_links():
            m, com = self.link_mass(name)
            mesh = self.link_mesh(name)
            L.append(f'  <link name="{name}">')
            L.append(f'    <inertial><origin xyz="{com[0]/1000:.5f} {com[1]/1000:.5f} {com[2]/1000:.5f}"/><mass value="{max(m, 1e-4):.5f}"/>'
                     f'<inertia ixx="1e-5" iyy="1e-5" izz="1e-5" ixy="0" ixz="0" iyz="0"/></inertial>')
            if mesh is not None:
                fn = f"meshes/{name}.stl"
                mesh.export(os.path.join(outdir, fn))
                geo = f'<geometry><mesh filename="{fn}" scale="0.001 0.001 0.001"/></geometry>'
                L.append(f"    <visual>{geo}</visual>\n    <collision>{geo}</collision>")
            L.append("  </link>")
        for j in self.joints:
            xyz = j.origin[:3, 3] / 1000.0
            r, p_, y = trimesh.transformations.euler_from_matrix(j.origin, "sxyz")
            jt = "fixed" if j.type == "fixed" else j.type
            L.append(f'  <joint name="{j.name}" type="{jt}"><parent link="{j.parent}"/><child link="{j.child}"/>'
                     f'<origin xyz="{xyz[0]:.5f} {xyz[1]:.5f} {xyz[2]:.5f}" rpy="{r:.5f} {p_:.5f} {y:.5f}"/>'
                     + (f'<axis xyz="{j.axis[0]} {j.axis[1]} {j.axis[2]}"/>' if jt != "fixed" else "")
                     + (f'<limit lower="{j.lower if j.type == "revolute" else j.lower / 1000:.4f}" '
                        f'upper="{j.upper if j.type == "revolute" else j.upper / 1000:.4f}" effort="{j.effort:.3f}" '
                        f'velocity="{j.velocity:.3f}"/>' if jt != "fixed" else "")
                     + (f'<mimic joint="{j.mimic["joint"]}" multiplier="{j.mimic.get("multiplier", 1.0)}"/>' if j.mimic else "")
                     + "</joint>")
        L.append("</robot>")
        path = os.path.join(outdir, f"{self.name}.urdf")
        with open(path, "w") as fh:
            fh.write("\n".join(L))
        return path

    def validate(self) -> list:
        """Structural checks every generator must pass (tests call this)."""
        errs = []
        names = set(self.links)
        for j in self.joints:
            if j.parent not in names or j.child not in names:
                errs.append(f"joint {j.name} references unknown link")
            if j.type not in ("revolute", "prismatic", "fixed"):
                errs.append(f"joint {j.name} bad type {j.type}")
            if j.mimic and not any(k.name == j.mimic["joint"] for k in self.joints):
                errs.append(f"joint {j.name} mimics unknown joint")
        if len(self.ordered_links()) != len(self.links):
            errs.append("links not all connected to the root")
        for p in self.parts:
            if p.kind not in KINDS:
                errs.append(f"part {p.name} bad kind {p.kind}")
            if p.process not in PROCESSES:
                errs.append(f"part {p.name} bad process {p.process}")
            if len(p.mesh.faces) == 0:
                errs.append(f"part {p.name} has an empty mesh")
            if not all(c.isalnum() or c == "_" for c in p.name):
                errs.append(f"part name {p.name!r} must be [a-z0-9_]")
        step_ids = {s.index for s in self.steps}
        for p in self.parts:
            if self.steps and p.step not in step_ids:
                errs.append(f"part {p.name} in unknown step {p.step}")
        return errs
