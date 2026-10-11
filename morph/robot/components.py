"""Component geometry library (purchased parts): actuators, horns, bearings,
fasteners, electronics modules.

STUB GEOMETRY: the API below is the contract; the detailed-geometry pass
replaces the bodies of these functions with realistic models but must keep the
signatures, frame conventions and return types.

Frame conventions (component frame, mm):
* actuator(key): origin on the output-shaft axis at the horn mounting face,
  +z out of the shaft; the body extends towards -z; the body's long axis is +x
  (shaft sits `shaft_off` from the body centre towards -x). frames:
    "output": identity (where a horn / bracket attaches; rotates with the joint)
    "mount_bottom": centre of the body's bottom face (-z end), z pointing out of the bottom
    "connector": cable exit point
* module(key): origin at the board's bottom-face centre, +z up through the
  components, long axis +x. frames: "mount" = identity, "connector_*" = cable
  entry points.
* bearing(key): origin at the centre of the bearing, axis +z.
* fastener(spec): origin at the underside of the head, shank along -z.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import trimesh

from . import catalog
from .assembly import trans


@dataclass
class Component:
    key: str
    name: str
    kind: str                                   # actuator | horn | bearing | fastener | electronics | sensor
    parts: list = field(default_factory=list)   # [(suffix, mesh, color, material)]
    frames: dict = field(default_factory=dict)  # name -> 4x4 in component frame
    dims: dict = field(default_factory=dict)
    spec: dict = field(default_factory=dict)

    def mesh(self) -> trimesh.Trimesh:
        return trimesh.util.concatenate([m for _, m, _, _ in self.parts])


def _box(l, w, h, center=(0, 0, 0)):
    m = trimesh.creation.box((l, w, h))
    m.apply_translation(center)
    return m


def _cyl(r, h, z0=0.0, sections=48):
    m = trimesh.creation.cylinder(radius=r, height=h, sections=sections)
    m.apply_translation((0, 0, z0 + h / 2))
    return m


def actuator(key: str) -> Component:
    spec = catalog.get(key)
    d = spec["dims"]
    body_c = (-d["shaft_off"], 0.0, -d["h"] / 2 - 0.5)
    parts = [("body", _box(d["l"], d["w"], d["h"], body_c), "#1d1f23", "purchased"),
             ("spline", _cyl(2.5, d.get("spline_h", 3.0), -d.get("spline_h", 3.0) + 0.5), "#c0c0c0", "purchased")]
    frames = {"output": np.eye(4), "mount_bottom": trans((-d["shaft_off"], 0, -d["h"] - 0.5)),
              "connector": trans((-d["shaft_off"] - d["l"] / 2, 0, -d["h"] * 0.7))}
    return Component(key, spec["name"], "actuator", parts, frames, dict(d), spec)


def horn(key: str) -> Component:
    d = catalog.get(key)["dims"]
    return Component(f"{key}_horn", "round horn", "horn", [("horn", _cyl(d["horn_d"] / 2, 2.5, 0.0), "#e8e8e8", "purchased")],
                     {"face": trans((0, 0, 2.5))}, {"d": d["horn_d"], "t": 2.5})


def bearing(key: str) -> Component:
    b = catalog.BEARINGS[key]
    ring = trimesh.creation.annulus(r_min=b["d"] / 2, r_max=b["D"] / 2, height=b["B"], sections=64)
    return Component(key, b["name"], "bearing", [("ring", ring, "#b9bec6", "Steel")], {"center": np.eye(4)}, dict(b), b)


def fastener(spec: str = "M3x8", kind: str = "shcs") -> trimesh.Trimesh:
    size, length = spec.split("x")
    hd, hh, _ = catalog.FASTENER_SIZES[size]
    d = float(size[1:])
    head = _cyl(hd / 2, hh, 0.0, 24)
    shank = _cyl(d / 2, float(length), -float(length), 16)
    return trimesh.util.concatenate([head, shank])


def module(key: str) -> Component:
    spec = catalog.get(key)
    d = spec["dims"]
    pcb = _box(d["l"], d["w"], 1.6, (0, 0, 0.8))
    comp = _box(d["l"] * 0.6, d["w"] * 0.6, max(d["h"] - 1.6, 0.5), (0, 0, 1.6 + max(d["h"] - 1.6, 0.5) / 2))
    color = {"controller": "#0b5e8a", "servo_driver": "#1a3d7c", "power": "#2b2b2b", "sensor": "#3a3a3a"}.get(spec["role"], "#225522")
    return Component(key, spec["name"], "electronics", [("pcb", pcb, color, "FR4"), ("parts", comp, "#202020", "purchased")],
                     {"mount": np.eye(4), "connector_a": trans((d["l"] / 2, 0, d["h"] / 2))}, dict(d), spec)
