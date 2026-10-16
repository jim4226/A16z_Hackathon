"""Capability envelope: what Morph's robot generator can build, and how far a request is from it.

The single source of truth for the brief engine's chips, the scope check, clamping of model output and the
text models see (RELEASE_CONTRACTS section 6). The numbers come from the generator itself: derive() sizes the
reference robot with arm.size() and the gripper geometry with arm._grip_plan() / analysis.analyze(); the
constants below are what derive() returns for this code (tests/test_capability.py fails when the generator
changes them), so envelope() never has to wait ~1.5 s for a sweep. warm() runs derive() in the background.

    from morph.studio import capability
    capability.envelope()["payload_g"]["smart_bus"][5]   # 200
    capability.check({"object.mass_g": 2000})             # {"status": "out_of_scope", ...}
"""

from __future__ import annotations

import math
import threading

FAMILIES = ("hobby_pwm", "smart_bus", "dynamixel")
DOFS = (4, 5, 6)
SHAPES = ("cylinder", "sphere", "box")
CONFIGS = ("prototype", "pilot", "production")
EXTRAS = ("wrist_camera", "estop", "imu", "wifi")
ROBOT_SOURCES = ("so101",)  # imported robots a brief may target (shipped robots/); models never set arm.source

# Payload (g) at the default 2x torque margin on the reference robot: the largest of PAYLOAD_GRID that arm.size()
# accepts with no issues (0 = not even 50 g). derive() recomputes it.
PAYLOAD_GRID = (50, 100, 200, 300, 500, 750, 1000, 1500)
PAYLOAD_G = {"hobby_pwm": {4: 100, 5: 100, 6: 0},   # the DS3218s run at 6 V (an SG90's top): 88 % of their torque
             "smart_bus": {4: 300, 5: 200, 6: 100},
             "dynamixel": {4: 500, 5: 300, 6: 200}}
SO101_PAYLOAD_G = 100  # the stock SO-101 (7.4 V STS3215s on its 6 V rail) by imported.size(); 150 g fails its shoulder
# Widest object (mm) the stock SO-101's jaws hold in Morph's own pick-and-place simulation (simulate.run, a 30 g object
# at its nominal size): cylinders of 30, 35, 40, 44 and 46 mm and spheres of 40, 44 and 46 mm are placed, cylinders of
# 47, 48, 50, 54, 56, 58 and 60 mm and spheres of 48 and 52 mm slip out; boxes of 30, 32 and 34 mm are placed, boxes of
# 36, 40, 44 and 48 mm slip out (38 mm is placed with 9.6 mm of slip)
SO101_MAX_WIDTH_MM = {"cylinder": 46.0, "sphere": 46.0, "box": 34.0}
# Narrowest object (mm, 0.5 mm steps) the rack-and-pinion gripper closes on: arm._grip_plan() reports no issue.
# The FSR 402 finger's thicker pad stack is what makes 26-38 mm round objects fit.
MIN_WIDTH_MM = {("cylinder", "fsr402"): 26.0, ("sphere", "fsr402"): 26.0, ("box", "fsr402"): 21.5,
                ("cylinder", "none"): 38.0, ("sphere", "none"): 38.0, ("box", "none"): 31.5}
MAX_WIDTH_MM = 75.0          # analysis.analyze: the 85 mm gripper stroke must clear the object by 10 mm
REACH_MM = (417, 617)        # effective arm length for top-down picks (sizing raises arm.reach_mm to this)
CYCLE_S = (4.0, 60.0)        # the simulated motion profile cannot go below about 3.5 s per pick and place
QUANTITY = (1, 10000)
FAMILY_LABEL = {"hobby_pwm": "hobby PWM", "smart_bus": "smart-bus (Feetech STS3215)", "dynamixel": "Dynamixel"}
MAX_MASS_G = max(max(v.values()) for v in PAYLOAD_G.values())

OUT_OF_SCOPE = ("mobile robots (wheels, legs, vacuums)", "drones", "humanoids and multi-finger hands",
                "suction grippers", "pouring liquids", "folding cloth", "cooking",
                "placing above table height (shelves, stacks, racks)")
FIRMWARE = ("joint-level firmware only: a serial command server for the servos and the gripper; "
            "no task program, no inverse kinematics on the robot and no vision code")

# The egg-picker demo robot: the reference design the payload table was measured on.
REFERENCE = {"object.name": "raw egg", "object.shape": "cylinder", "object.diameter_mm": 44.0, "object.length_mm": 57.0,
             "object.mass_g": 60.0, "object.friction": 0.35, "object.max_force_n": 25.0, "finger.sensor": "fsr402",
             "finger.width_mm": 20.0, "finger.length_mm": 52.0, "finger.thickness_mm": 6.0, "finger.pad_depth_mm": 5.5,
             "finger.pad_height_mm": 19.8, "electronics.servo": "sts3215"}

_derived: dict | None = None
_lock = threading.Lock()


def _params() -> dict:
    from ..robot.pipeline2 import PARAMS

    return {k: {"label": label, "group": group, "type": typ, **extra} for k, label, group, typ, extra in PARAMS}


def min_width(shape: str, sensor: str = "fsr402") -> float:
    return MIN_WIDTH_MM.get((shape if shape in SHAPES else "cylinder", "fsr402" if sensor == "fsr402" else "none"), 26.0)


def payload_g(family: str, dof: int) -> int:
    return (PAYLOAD_G.get(family) or PAYLOAD_G["smart_bus"]).get(int(dof) if int(dof) in DOFS else 5, 0)


def envelope() -> dict:
    """What the generator builds (RELEASE_CONTRACTS section 6), as data for chips, checks and models."""
    d = _derived or {}
    pay = d.get("payload_g") or PAYLOAD_G
    widths = d.get("min_width_mm") or MIN_WIDTH_MM
    params = _params()
    return {
        "robot": "one stationary tabletop arm", "dof": list(DOFS), "families": list(FAMILIES),
        "grasp": "two-finger parallel gripper (rack and pinion); top-down pick and place on the table plane",
        "object": {"shapes": list(SHAPES), "note": "cylinders are gripped lying on their side",
                   "width_mm": {"min": min(widths.values()), "max": MAX_WIDTH_MM,
                                "min_by_shape": {s: widths[(s, "fsr402")] for s in SHAPES},
                                "min_without_sensor": {s: widths[(s, "none")] for s in SHAPES}},
                   "mass_g": {"max": max(max(v.values()) for v in pay.values())}},
        "payload_g": {f: {str(k): v for k, v in t.items()} for f, t in pay.items()},
        "so101_payload_g": SO101_PAYLOAD_G,
        "so101_max_width_mm": dict(SO101_MAX_WIDTH_MM),
        "reach_mm": {"effective": list(REACH_MM),
                     "param": [params["arm.reach_mm"]["min"], params["arm.reach_mm"]["max"]]},
        "table_mm": {"x": [params["task.pick_x_mm"]["min"], params["task.pick_x_mm"]["max"]],
                     "y": [params["task.pick_y_mm"]["min"], params["task.pick_y_mm"]["max"]]},
        "cycle_time_s": list(CYCLE_S), "quantity": list(QUANTITY), "build_configs": list(CONFIGS),
        "extras": list(EXTRAS), "mcus": [o for o in params["electronics.mcu"]["options"] if o != "auto"],
        "firmware": FIRMWARE, "out_of_scope": list(OUT_OF_SCOPE),
        "params": params, "source": "code" if d else "table",
    }


def describe_for_models() -> str:
    """The envelope as plain text for the SubQ state and the Claude system prompt."""
    e = envelope()
    w, p = e["object"]["width_mm"], e["payload_g"]
    pay = "; ".join(f"{FAMILY_LABEL[f]} servos {p[f]['4']} / {p[f]['5']} / {p[f]['6'] or 'none'} g" for f in FAMILIES)
    return (
        "Morph designs one kind of robot: a stationary tabletop arm with 4, 5 or 6 joints and a two-finger parallel "
        "gripper that picks one object top-down from the table and places it elsewhere on the table plane. "
        f"Objects are cylinders (gripped lying on their side), spheres or boxes {w['min_by_shape']['cylinder']:g} to "
        f"{w['max']:g} mm wide where the fingers close (boxes from {w['min_by_shape']['box']:g} mm; round objects under "
        f"{w['min_without_sensor']['cylinder']:g} mm need the FSR 402 force-sensing finger). "
        f"Payload at a 2x torque margin with 4 / 5 / 6 joints: {pay}; nothing heavier than {e['object']['mass_g']['max']} g. "
        f"An existing SO-101 arm can be used instead (about {SO101_PAYLOAD_G} g). "
        f"Pick and place points lie on the table, x {e['table_mm']['x'][0]} to {e['table_mm']['x'][1]} mm and "
        f"y {e['table_mm']['y'][0]} to {e['table_mm']['y'][1]} mm from the base; the arm comes out "
        f"{e['reach_mm']['effective'][0]} to {e['reach_mm']['effective'][1]} mm long. Each robot gets CAD, simulations, "
        "electronics (off-the-shelf modules or one custom PCB; optional wrist camera, e-stop, IMU, Wi-Fi), "
        f"{FIRMWARE}, a manufacturing plan (printed prototype, CNC pilot or molded production), a BOM and an assembly "
        "manual. Out of scope: " + ", ".join(OUT_OF_SCOPE) + "."
    )


# ------------------------------------------------------------------ checks
def _design(x):
    """A Design from a Design, a full design dict, or dotted changes over the reference robot."""
    from ..spec import Design

    if isinstance(x, Design):
        return x, []
    x = dict(x or {})
    if isinstance(x.get("object"), dict):  # nested design dict
        return Design.from_dict(x), []
    flat = Design().flat()
    good = {k: v for k, v in x.items() if k in flat}
    notes = [f"ignored unknown parameter {k}" for k in x if k not in flat]
    base = Design().with_changes(REFERENCE)
    try:
        return base.with_changes(good), notes
    except (TypeError, ValueError) as exc:
        return base, notes + [f"could not apply changes ({exc})"]


def check(x) -> dict:
    """Envelope check of a design or dotted changes: {status: ok|adjusted|out_of_scope, notes, nearest}.

    `nearest` is a dict of dotted changes that brings the design back inside (None when already inside).
    'adjusted' means buildable after a change Morph can make on its own (stronger servos, the FSR finger,
    a clamped value); 'out_of_scope' means the object or task itself is beyond what the generator builds."""
    d, notes = _design(x)
    o, a = d.object, d.arm
    out, adj, nearest = [], [], {}
    shape = o.shape if o.shape in SHAPES else "cylinder"
    if o.shape not in SHAPES:
        adj.append(f"unknown object shape '{o.shape}'; treated as a cylinder")
        nearest["object.shape"] = "cylinder"
    lo_s, lo_n = min_width(shape, "fsr402"), min_width(shape, "none")
    if o.diameter_mm < lo_s:
        out.append(f"{o.diameter_mm:g} mm is narrower than the {lo_s:g} mm the gripper can close on")
        nearest.update({"object.diameter_mm": lo_s if shape == "box" else max(lo_s, 30.0), "finger.sensor": "fsr402"})
    elif o.diameter_mm < lo_n and d.finger.sensor != "fsr402":
        adj.append(f"a {o.diameter_mm:g} mm {shape} needs the FSR 402 finger (its pad stack closes on objects under "
                   f"{lo_n:g} mm)")
        nearest["finger.sensor"] = "fsr402"
    if o.diameter_mm > MAX_WIDTH_MM:
        out.append(f"{o.diameter_mm:g} mm is wider than the {MAX_WIDTH_MM:g} mm the gripper opens around")
        nearest["object.diameter_mm"] = MAX_WIDTH_MM - 5
    if a.source:
        if a.source not in ROBOT_SOURCES:
            out.append(f"unknown robot source '{a.source}'")
            nearest["arm.source"] = ""
        elif o.mass_g > SO101_PAYLOAD_G:
            adj.append(f"the SO-101 lifts about {SO101_PAYLOAD_G} g with a 2x torque margin; {o.mass_g:g} g needs a "
                       "Morph-generated arm")
            nearest["arm.source"] = ""
    else:
        fam = a.actuator_family if a.actuator_family in FAMILIES else "smart_bus"
        if a.actuator_family not in FAMILIES:
            adj.append(f"unknown actuator family '{a.actuator_family}'; using smart bus servos")
            nearest["arm.actuator_family"] = "smart_bus"
        dof = int(a.dof) if int(a.dof) in DOFS else 5
        if int(a.dof) not in DOFS:
            adj.append(f"{a.dof} joints is not an option (4, 5 or 6); using {min(DOFS, key=lambda n: abs(n - int(a.dof)))}")
            nearest["arm.dof"] = min(DOFS, key=lambda n: abs(n - int(a.dof)))
        if o.mass_g > MAX_MASS_G:
            out.append(f"{o.mass_g:g} g is more than any desk arm here lifts ({MAX_MASS_G} g with Dynamixel servos and 4 joints)")
            nearest.update({"object.mass_g": float(MAX_MASS_G), "arm.actuator_family": "dynamixel", "arm.dof": 4})
        elif o.mass_g > payload_g(fam, dof):
            opts = [(f, n) for f in FAMILIES for n in DOFS if payload_g(f, n) >= o.mass_g]
            # fewest changes first, then cheaper servos, then more joints
            f2, n2 = min(opts, key=lambda c: ((c[0] != fam) + (c[1] != dof), FAMILIES.index(c[0]), -c[1]))
            adj.append(f"{o.mass_g:g} g is above the {payload_g(fam, dof)} g a {dof}-joint {fam.replace('_', ' ')} arm "
                       f"lifts; a {n2}-joint {f2.replace('_', ' ')} arm lifts {payload_g(f2, n2)} g")
            nearest.update({"arm.actuator_family": f2, "arm.dof": n2})
    params = _params()
    for k in ("task.pick_x_mm", "task.pick_y_mm", "task.place_x_mm", "task.place_y_mm"):
        v, p = d.flat()[k], params[k]
        if not p["min"] <= v <= p["max"]:
            adj.append(f"{k} = {v:g} mm is off the table area ({p['min']} to {p['max']} mm)")
            nearest[k] = float(min(max(v, p["min"]), p["max"]))
    if d.task.cycle_time_s < CYCLE_S[0]:
        adj.append(f"one pick and place takes at least about {CYCLE_S[0]:g} s on this arm")
        nearest["task.cycle_time_s"] = CYCLE_S[0]
    if not QUANTITY[0] <= d.build.quantity <= QUANTITY[1]:
        adj.append(f"quantity {d.build.quantity} is outside {QUANTITY[0]} to {QUANTITY[1]}")
        nearest["build.quantity"] = int(min(max(d.build.quantity, QUANTITY[0]), QUANTITY[1]))
    status = "out_of_scope" if out else "adjusted" if adj else "ok"
    return {"status": status, "notes": notes + out + adj, "nearest": nearest or None}


def _num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(v) else None
    try:
        f = float(str(v).strip())
        return f if math.isfinite(f) else None
    except ValueError:
        return None


# sanity bounds for numeric keys PARAMS does not cover (inputs the generator accepts; the envelope check decides scope)
_BOUNDS = {"object.diameter_mm": (5.0, 300.0), "object.length_mm": (5.0, 500.0), "object.friction": (0.1, 1.2),
           "finger.width_mm": (6.0, 20.0), "finger.pad_height_mm": (10.0, 60.0), "finger.pad_depth_mm": (2.0, 15.0),
           "task.cycle_time_s": CYCLE_S, "electronics.supply_v": (5.0, 14.8),
           "electronics.servo_v": (0.0, 14.8), "gripper.force_n": (5.0, 60.0)}
_ENUMS = {"object.shape": SHAPES, "finger.pad": ("contour", "v", "flat"), "finger.sensor": ("none", "fsr402"),
          "finger.material": ("PLA", "PETG", "Nylon-CF", "Acrylic", "Delrin", "HDPE", "Al6061"),
          "fab.process": ("auto", "fdm_print", "laser_cut", "cnc_router"), "electronics.config": ("modules", "custom_pcb"),
          "electronics.servo": ("auto", "sg90", "mg90s", "mg996r", "ds3218", "sts3215"),
          "arm.actuator_family": FAMILIES, "build.config": CONFIGS}
_LOCKED = ("name", "arm.source", "arm.actuator_overrides", "world.id", "world.sha")  # never from a model


def clamp(changes: dict, *, allow=()) -> tuple[dict, list]:
    """Validate dotted changes against the generator's input domain: unknown keys and bad enums are dropped, numbers
    are clamped to PARAMS / sane ranges, DOF snaps to 4-6, extras keep known tokens. name, arm.source and
    arm.actuator_overrides pass only when listed in `allow` (code, never a model, sets them)."""
    from ..spec import Design

    flat, params = Design().flat(), _params()
    out, notes = {}, []
    for k, v in (changes or {}).items():
        if k not in flat:
            notes.append(f"ignored unknown parameter {k}")
            continue
        if k in _LOCKED and k not in allow:
            notes.append(f"ignored {k}: only Morph sets it")
            continue
        if k == "arm.source":
            if v not in ("",) + ROBOT_SOURCES:
                notes.append(f"ignored unknown robot source {v!r}")
                continue
            out[k] = v
        elif k == "arm.dof":
            n = _num(v)
            if n is None:
                notes.append(f"ignored arm.dof = {v!r}")
                continue
            snap = min(DOFS, key=lambda d: abs(d - n))
            if snap != n:
                notes.append(f"arm.dof {v} -> {snap} (4, 5 or 6 joints)")
            out[k] = snap
        elif k == "electronics.extras":
            toks = [t.strip() for t in str(v or "").split(",") if t.strip()]
            keep = [t for t in dict.fromkeys(toks) if t in EXTRAS]
            if len(keep) != len(toks):
                notes.append(f"electronics.extras: kept {', '.join(keep) or 'none'}")
            out[k] = ",".join(keep)
        elif k == "electronics.mcu":
            if v in ["auto"] + list(params["electronics.mcu"]["options"]):
                out[k] = v
            else:
                notes.append(f"ignored electronics.mcu = {v!r}")
        elif k in _ENUMS:
            if v in _ENUMS[k]:
                out[k] = v
            else:
                notes.append(f"ignored {k} = {v!r}")
        elif isinstance(flat[k], str):
            out[k] = str(v)[:80]
        else:
            n = _num(v)
            if n is None:
                notes.append(f"ignored {k} = {v!r}")
                continue
            lo, hi = ((params[k]["min"], params[k]["max"]) if k in params and "min" in params[k] else _BOUNDS.get(k, (None, None)))
            c = n if lo is None else min(max(n, lo), hi)
            if c != n:
                notes.append(f"{k} {n:g} -> {c:g}")
            out[k] = int(round(c)) if isinstance(flat[k], int) and not isinstance(flat[k], bool) else float(c)
    return out, notes


# ------------------------------------------------------------------ derive from the generator
def _ref(changes: dict):
    from ..spec import Design

    return Design().with_changes({**REFERENCE, **changes})


def _object(shape: str, dia: float, sensor: str) -> dict:
    from .intent import fit_finger

    o = dict(shape=shape, diameter_mm=dia, length_mm=max(dia, 40.0), mass_g=50.0, friction=0.5, max_force_n=0.0)
    return {**{f"object.{k}": v for k, v in o.items()}, **fit_finger(o), "finger.pad": "flat" if shape == "box" else "contour",
            "finger.sensor": sensor, "finger.width_mm": 20.0}


def _closes_on(shape: str, dia: float, sensor: str) -> bool:
    from ..robot import arm

    d = _ref(_object(shape, dia, sensor))
    return not arm._grip_plan(d, arm.gripper_actuator(d))["issues"]


def derive(so101: bool = False) -> dict:
    """Recompute the envelope numbers from the generator (about 1.5 s; SO-101 adds about 5 s)."""
    from ..robot import arm

    out = {"min_width_mm": {}, "payload_g": {}}
    for shape in SHAPES:
        for sensor in ("fsr402", "none"):
            lo, hi = 10.0, MAX_WIDTH_MM  # fails at 10 mm, closes at 75 mm; bisect on a 0.5 mm grid
            while hi - lo > 0.5:
                mid = round((lo + hi), 0) / 2.0
                mid = mid if lo < mid < hi else lo + 0.5
                if _closes_on(shape, mid, sensor):
                    hi = mid
                else:
                    lo = mid
            out["min_width_mm"][(shape, sensor)] = hi
    reach = []
    for fam in FAMILIES:
        out["payload_g"][fam] = {}
        for dof in DOFS:
            best = 0
            for m in PAYLOAD_GRID:
                s = arm.size(_ref({"arm.actuator_family": fam, "arm.dof": dof, "object.mass_g": float(m),
                                   "object.max_force_n": 0.0}))
                if s["issues"]:
                    break
                best = m
                reach.append(s["reach_mm"])
            out["payload_g"][fam][dof] = best
    out["reach_mm"] = (math.floor(min(reach)), math.ceil(max(reach))) if reach else REACH_MM
    if so101:
        from ..robot import imported

        best = 0
        for m in (50, 100, 150, 200, 250, 300):
            if imported.size(_ref({"arm.source": "so101", "object.mass_g": float(m)}))["issues"]:
                break
            best = m
        out["so101_payload_g"] = best
    return out


def warm(block: bool = True):
    """Fill the envelope from the generator once per process (call at Studio start with block=False)."""
    def run():
        global _derived
        with _lock:
            if _derived is None:
                try:
                    _derived = derive()
                except Exception:  # noqa: BLE001  never block the Studio on a sweep; the constants still hold
                    pass

    if block:
        run()
    else:
        threading.Thread(target=run, daemon=True, name="capability-warm").start()
