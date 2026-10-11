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
    max_force_n: float = 0.0         # per-finger force that damages the object; 0 = robust


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
    sensor: str = "none"             # none | fsr402 (force sensor seated under the pad)


@dataclass
class GripperSpec:
    name: str = "generic parallel gripper"
    force_n: float = 25.0            # max grip force per finger
    stroke_mm: float = 85.0          # total opening


@dataclass
class ElectronicsSpec:
    mcu: str = "auto"                # auto | arduino_nano | esp32_devkit | rp2040_pico
    servo: str = "auto"              # auto | sg90 | mg90s | mg996r | ds3218
    supply_v: float = 6.0
    pinion_radius_mm: float = 10.5   # pitch radius of the printed pinion (module 1.5, 14 teeth)
    target_force_n: float = 0.0      # 0 = derive from simulation + object fragility
    config: str = "modules"          # modules (off-the-shelf boards) | custom_pcb (one controller PCBA)
    extras: str = ""                 # comma list: wrist_camera, imu, estop, wifi


@dataclass
class ArmSpec:
    dof: int = 5                     # arm joints before the gripper: 4 | 5 | 6
    reach_mm: float = 320.0          # shoulder axis to tool centre point, fully stretched
    base_height_mm: float = 95.0     # table to shoulder axis
    actuator_family: str = "smart_bus"   # hobby_pwm | smart_bus | dynamixel
    actuator_overrides: str = ""     # "j2=xm430_w350,j3=sts3215" to force specific actuators
    payload_margin: float = 2.0      # torque safety factor at full stretch
    source: str = ""                 # "" = Morph-generated arm; "so101" = imported open-source robot (robots/<source>/)


@dataclass
class TaskSpec:
    pick_x_mm: float = 220.0         # object position on the table, robot base frame (z up)
    pick_y_mm: float = -80.0
    place_x_mm: float = 160.0
    place_y_mm: float = 170.0
    cycle_time_s: float = 6.0


@dataclass
class BuildSpec:
    config: str = "prototype"        # prototype (printed) | pilot (CNC + sheet metal + PCBA) | production (molded + PCBA)
    quantity: int = 1


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
    electronics: ElectronicsSpec = field(default_factory=ElectronicsSpec)
    arm: ArmSpec = field(default_factory=ArmSpec)
    task: TaskSpec = field(default_factory=TaskSpec)
    build: BuildSpec = field(default_factory=BuildSpec)
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
            electronics=ElectronicsSpec(**d.get("electronics", {})),
            arm=ArmSpec(**d.get("arm", {})),
            task=TaskSpec(**d.get("task", {})),
            build=BuildSpec(**d.get("build", {})),
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
