"""Design specification: the single source of truth every artifact derives from."""

from __future__ import annotations

import copy
import json
from dataclasses import asdict, dataclass, field


@dataclass
class ObjectSpec:
    name: str = "aluminum can"
    shape: str = "cylinder"          # cylinder (lying on its side) | box | sphere
    diameter_mm: float = 66.0        # cylinder/sphere diameter, or box height in grip plane
    length_mm: float = 120.0         # extent along finger width axis
    mass_g: float = 370.0
    friction: float = 0.5


@dataclass
class FingerParams:
    length_mm: float = 70.0          # mount face to fingertip
    thickness_mm: float = 6.0        # beam thickness in bending direction
    width_mm: float = 6.0            # extrusion depth == sheet/plate thickness
    pad: str = "contour"             # flat | v | contour
    pad_height_mm: float = 30.0
    pad_depth_mm: float = 8.0
    material: str = "PETG"
    mount_hole_d_mm: float = 3.4     # M3 clearance
    mount_hole_spacing_mm: float = 12.0


@dataclass
class GripperSpec:
    name: str = "generic parallel gripper"
    force_n: float = 25.0            # max grip force per finger
    stroke_mm: float = 85.0          # total opening


@dataclass
class FabSpec:
    process: str = "auto"            # auto | fdm_print | laser_cut | cnc_router
    tool_d_mm: float = 3.175
    quantity: int = 2


@dataclass
class Design:
    name: str = "can-gripper"
    object: ObjectSpec = field(default_factory=ObjectSpec)
    finger: FingerParams = field(default_factory=FingerParams)
    gripper: GripperSpec = field(default_factory=GripperSpec)
    fab: FabSpec = field(default_factory=FabSpec)
    safety_factor: float = 2.0

    def to_dict(self) -> dict:
        return asdict(self)

    def flat(self) -> dict:
        out = {}

        def walk(prefix, d):
            for k, v in d.items():
                key = f"{prefix}.{k}" if prefix else k
                if isinstance(v, dict):
                    walk(key, v)
                else:
                    out[key] = v

        walk("", self.to_dict())
        return out

    @classmethod
    def from_dict(cls, d: dict) -> "Design":
        return cls(
            name=d.get("name", "design"),
            object=ObjectSpec(**d.get("object", {})),
            finger=FingerParams(**d.get("finger", {})),
            gripper=GripperSpec(**d.get("gripper", {})),
            fab=FabSpec(**d.get("fab", {})),
            safety_factor=d.get("safety_factor", 2.0),
        )

    def with_changes(self, changes: dict) -> "Design":
        """Return a copy with dotted-key changes applied, e.g. {"finger.length_mm": 80}."""
        d = copy.deepcopy(self.to_dict())
        for key, value in changes.items():
            parts = key.split(".")
            node = d
            for p in parts[:-1]:
                if p not in node or not isinstance(node[p], dict):
                    raise KeyError(f"unknown parameter: {key}")
                node = node[p]
            if parts[-1] not in node:
                raise KeyError(f"unknown parameter: {key}")
            old = node[parts[-1]]
            node[parts[-1]] = coerce(value, old)
        return Design.from_dict(d)

    def dumps(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


def coerce(value, like):
    if isinstance(value, str) and not isinstance(like, str):
        if isinstance(like, bool):
            return value.lower() in ("1", "true", "yes")
        if isinstance(like, int):
            return int(float(value))
        if isinstance(like, float):
            return float(value)
    return value


def diff(a: Design, b: Design) -> dict:
    fa, fb = a.flat(), b.flat()
    return {k: (fa.get(k), fb.get(k)) for k in sorted(set(fa) | set(fb)) if fa.get(k) != fb.get(k)}
