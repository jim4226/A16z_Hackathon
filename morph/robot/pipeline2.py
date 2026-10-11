"""Whole-robot pipeline: sizing -> gripper -> electronics -> CAD -> simulation ->
wiring/PCB/firmware -> manufacturing -> BOM/sourcing -> assembly manual.

Same storage and change-propagation model as morph.pipeline.Project (one
directory per revision, results.json, ledger.jsonl), with a robot-level
dependency graph: a change reruns only the stages that read the changed
parameters, plus everything downstream of them.
"""

from __future__ import annotations

import fnmatch
import importlib
import json
import os
import shutil
import time
import traceback

from .. import analysis as grip_analysis
from .. import electronics as grip_electronics
from .. import sim as grip_sim
from ..design import build_profile
from ..pipeline import Project
from ..spec import Design, diff

# node: (parameter patterns it reads, upstream nodes, output directory)
GRAPH = {
    "sizing": (["arm.*", "object.mass_g", "electronics.servo"], [], None),
    "gripper": (["finger.*", "object.*", "gripper.*", "safety_factor", "electronics.target_force_n",
                 "electronics.servo", "electronics.supply_v", "electronics.pinion_radius_mm", "arm.actuator_family",
                 "arm.source", "arm.actuator_overrides"],
                [], "gripper"),
    "electronics": (["electronics.*"], ["sizing", "gripper"], None),
    "cad": (["finger.*", "object.shape", "object.diameter_mm", "object.length_mm", "build.*", "name", "arm.source"],
            ["sizing", "electronics"], "cad"),
    # sim reads the arm geometry, but electronics placed in the fixed base do not change its dynamics,
    # so it keys off the geometry-defining parameters instead of the cad node
    "sim": (["task.*", "object.*", "arm.*", "finger.*", "build.*"], ["sizing", "gripper"], "sim"),
    "wiring": ([], ["electronics", "cad"], "electronics"),
    "manufacturing": (["build.*", "fab.*"], ["cad", "wiring"], "mfg"),
    "bom": ([], ["manufacturing", "wiring"], None),
    "manual": ([], ["cad", "manufacturing", "wiring"], "manual"),
}
ORDER = ["sizing", "gripper", "electronics", "cad", "sim", "wiring", "manufacturing", "bom", "manual"]
LABELS = [("sizing", "Sizing"), ("gripper", "Gripper + grasp"), ("electronics", "Electronics plan"),
          ("cad", "Robot CAD"), ("sim", "Simulation"), ("wiring", "Schematic · PCB · FW"),
          ("manufacturing", "Manufacturing"), ("bom", "BOM + sourcing"), ("manual", "Assembly manual")]

PARAMS = [  # editor schema for the Studio (key, label, group, type, extra)
    ("object.name", "Object", "Task", "select", {"options": ["raw egg", "strawberry", "tomato", "grape", "ping pong ball",
                                                             "light bulb", "soda can", "marker", "water bottle"]}),
    ("object.mass_g", "Object mass", "Task", "number", {"min": 1, "max": 1500, "step": 5, "unit": "g"}),
    ("object.max_force_n", "Damage force", "Task", "number", {"min": 0, "max": 100, "step": 0.5, "unit": "N"}),
    ("task.pick_x_mm", "Pick x", "Task", "number", {"min": 80, "max": 400, "step": 10, "unit": "mm"}),
    ("task.pick_y_mm", "Pick y", "Task", "number", {"min": -300, "max": 300, "step": 10, "unit": "mm"}),
    ("task.place_x_mm", "Place x", "Task", "number", {"min": 80, "max": 400, "step": 10, "unit": "mm"}),
    ("task.place_y_mm", "Place y", "Task", "number", {"min": -300, "max": 300, "step": 10, "unit": "mm"}),
    ("arm.dof", "Degrees of freedom", "Arm", "select", {"options": [4, 5, 6]}),
    ("arm.reach_mm", "Reach", "Arm", "number", {"min": 200, "max": 500, "step": 10, "unit": "mm"}),
    ("arm.base_height_mm", "Shoulder height", "Arm", "number", {"min": 60, "max": 160, "step": 5, "unit": "mm"}),
    ("arm.actuator_family", "Actuators", "Arm", "select", {"options": ["hobby_pwm", "smart_bus", "dynamixel"]}),
    ("arm.payload_margin", "Torque margin", "Arm", "number", {"min": 1.0, "max": 4.0, "step": 0.1, "unit": "×"}),
    ("finger.pad", "Pad shape", "Gripper", "select", {"options": ["contour", "v", "flat"]}),
    ("finger.sensor", "Force sensor", "Gripper", "select", {"options": ["fsr402", "none"]}),
    ("finger.length_mm", "Finger length", "Gripper", "number", {"min": 30, "max": 120, "step": 1, "unit": "mm"}),
    ("finger.thickness_mm", "Finger beam", "Gripper", "number", {"min": 3, "max": 14, "step": 0.5, "unit": "mm"}),
    ("electronics.target_force_n", "Grip force target", "Gripper", "number", {"min": 0, "max": 20, "step": 0.1, "unit": "N"}),
    ("electronics.config", "Electronics", "Electronics", "select", {"options": ["modules", "custom_pcb"]}),
    ("electronics.mcu", "Controller", "Electronics", "select", {"options": ["auto", "esp32_devkit", "esp32_s3_devkit", "arduino_nano", "rp2040_pico"]}),
    ("electronics.extras", "Extras", "Electronics", "select", {"options": ["", "wrist_camera", "estop", "wrist_camera,estop", "imu,wrist_camera,estop"]}),
    ("build.config", "Build", "Manufacturing", "select", {"options": ["prototype", "pilot", "production"]}),
    ("build.quantity", "Quantity", "Manufacturing", "number", {"min": 1, "max": 10000, "step": 1, "unit": "units"}),
]


def params_for(design: Design) -> list:
    flat = design.flat()
    out = []
    for key, label, group, typ, extra in PARAMS:
        out.append({"key": key, "label": label, "group": group, "type": typ, "value": flat.get(key), **extra})
    return out


def dirty_nodes(changed_keys) -> list:
    dirty = set()
    for node, (patterns, _, _) in GRAPH.items():
        if any(fnmatch.fnmatch(k, p) for k in changed_keys for p in patterns):
            dirty.add(node)
    grew = True
    while grew:
        grew = False
        for node, (_, ups, _) in GRAPH.items():
            if node not in dirty and any(u in dirty for u in ups):
                dirty.add(node)
                grew = True
    return [n for n in ORDER if n in dirty]


HOBBY = ("sg90", "mg90s", "mg996r", "ds3218")


def gripper_actuator(design: Design) -> str:
    """The gripper uses the arm's actuator family (one bus, one spare-parts list)."""
    fam, servo = design.arm.actuator_family, design.electronics.servo
    if fam == "hobby_pwm":
        return servo if servo in HOBBY else "mg996r"
    return "sts3215" if fam == "smart_bus" else "xl430_w250"


def pipeline_version() -> str:
    """Hash of the platform code: results built by older code are rebuilt when the Studio starts."""
    import hashlib

    h = hashlib.sha1()
    here = os.path.dirname(os.path.abspath(__file__))
    for root in (here, os.path.dirname(here)):
        for fn in sorted(os.listdir(root)):
            if fn.endswith(".py"):
                with open(os.path.join(root, fn), "rb") as fh:
                    h.update(fn.encode() + fh.read())
    return h.hexdigest()[:12]


def _mod(name):
    try:
        return importlib.import_module(f"morph.robot.{name}")
    except ImportError:
        return None


class RobotProject(Project):
    def run(self, design: Design, parent: str | None = None, label: str = "", n_trials: int = 4,
            verbose: bool = True, on_event=None, fast_sim: bool = False) -> str:
        from . import arm

        emit = on_event or (lambda e: None)
        rev = f"r{len(self.revs()) + 1:03d}"
        out = self.rev_dir(rev)
        os.makedirs(out)
        with open(os.path.join(out, "design.json"), "w") as fh:
            fh.write(design.dumps())
        if parent:
            changes = diff(self.load_design(parent), design)
            todo = dirty_nodes(changes.keys())
            prev = self.load_results(parent)
        else:
            changes, todo, prev = {}, list(ORDER), {}
        res = {"rev": rev, "parent": parent, "label": label, "kind": "robot",
               "changes": {k: list(v) for k, v in changes.items()}, "provenance": {}, "timings_s": {}}
        emit({"type": "start", "rev": rev, "parent": parent, "changes": res["changes"], "dirty": todo})
        cache = {}

        def asm():
            if "asm" not in cache:
                if design.arm.source:
                    from . import imported
                    cache["asm"] = imported.build(design, res["sizing"], res.get("eplan"))
                else:
                    try:
                        cache["asm"] = arm.build(design, res["sizing"], res.get("eplan"))
                    except Exception as exc:  # never leave the user without a robot: fall back to the stub generator
                        traceback.print_exc()
                        from . import arm_stub
                        res.setdefault("warnings", []).append(f"detailed arm generator failed ({exc}); showing the basic model")
                        cache["asm"] = arm_stub.build(design, res["sizing"], res.get("eplan"))
            return cache["asm"]

        for node in ORDER:
            sub = GRAPH[node][2]
            if node not in todo:
                for key in self._keys(node):
                    res[key] = prev.get(key)
                res["provenance"][node] = f"reused from {parent}"
                if sub and os.path.isdir(os.path.join(self.rev_dir(parent), sub)):
                    shutil.copytree(os.path.join(self.rev_dir(parent), sub), os.path.join(out, sub))
                emit({"type": "node", "node": node, "status": "reused"})
                if verbose:
                    print(f"  {node:<14} reused")
                continue
            emit({"type": "node", "node": node, "status": "computing"})
            t0 = time.time()
            nd = os.path.join(out, sub) if sub else out
            os.makedirs(nd, exist_ok=True)
            status = "computed"
            try:
                if self._compute(node, design, res, nd, asm, n_trials, fast_sim) is False:
                    status = "pending"          # module not available yet: say so, never fake "computed"
            except Exception as exc:  # keep the rest of the pipeline alive; surface the error
                traceback.print_exc()
                res.setdefault("errors", {})[node] = f"{exc.__class__.__name__}: {exc}"
                status = "error"
            res["provenance"][node] = status
            res["timings_s"][node] = round(time.time() - t0, 2)
            emit({"type": "node", "node": node, "status": status, "seconds": res["timings_s"][node]})
            if verbose:
                print(f"  {node:<14} {status} ({res['timings_s'][node]:.2f}s)")

        res["summary"] = summarize(res)
        res["pipeline_version"] = pipeline_version()
        with open(os.path.join(out, "results.json"), "w") as fh:
            json.dump(res, fh, indent=1, default=_jsonable)
        self.log({"type": "revision", "rev": rev, "parent": parent, "label": label, "changes": res["changes"],
                  "recomputed": todo, "design": design.to_dict(), "predicted": res["summary"]})
        return rev

    @staticmethod
    def _keys(node):
        return {"sizing": ["sizing"], "gripper": ["gripper"], "electronics": ["eplan"], "cad": ["cad"],
                "sim": ["sims"], "wiring": ["electronics"], "manufacturing": ["manufacturing"], "bom": ["bom"],
                "manual": ["manual"]}[node]

    def _compute(self, node, design, res, nd, asm, n_trials, fast_sim):
        from . import arm

        if node == "sizing":
            if design.arm.source:
                from . import imported
                res["sizing"] = imported.size(design)
            else:
                try:
                    res["sizing"] = arm.size(design)
                except Exception:
                    from . import arm_stub
                    res["sizing"] = arm_stub.size(design)
        elif node == "gripper" and design.arm.source:
            from . import imported
            res["gripper"] = imported.gripper_summary(design, imported.size(design))
        elif node == "gripper":
            from .catalog import ACTUATORS

            ga = gripper_actuator(design)
            gd = design.with_changes({"electronics.servo": ga, "electronics.supply_v": ACTUATORS[ga]["nominal_v"]})
            geom = build_profile(gd)
            a = grip_analysis.analyze(gd, geom)
            s = grip_sim.simulate(gd, geom, n_trials)
            res["gripper"] = {"actuator": ga, "analysis": a, "sim": {k: v for k, v in s.items() if k != "trials"},
                              "force": grip_electronics.plan(gd, a, s)}
        elif node == "electronics":
            e2 = _mod("electronics2")
            grip_t = ((res.get("gripper") or {}).get("force") or {}).get("target_force_n")
            sz = {**res["sizing"], **({"grip_target_n": grip_t} if grip_t else {})}
            res["eplan"] = e2.plan(design, sz) if e2 else None
            return e2 is not None
        elif node == "cad":
            a = asm()
            errs = a.validate()
            a.export_glb(os.path.join(nd, "robot.glb"))
            a.to_robot_json(os.path.join(nd, "robot.json"))
            urdf = a.export_urdf(os.path.join(nd, "urdf"))
            mass = sum(a.link_mass(l)[0] for l in a.links)
            res["cad"] = {"parts": len(a.parts), "links": len(a.links), "joints": len(a.joints), "mass_kg": round(mass, 3),
                          "validate": errs, "urdf": os.path.relpath(urdf, os.path.dirname(nd)),
                          "groups": len({p.group or p.name for p in a.parts}),
                          "source": a.meta.get("source") or "generated", "title": a.meta.get("title"),
                          "license": a.meta.get("license"), "source_url": a.meta.get("source_url"),
                          "issues": a.meta.get("issues", []),
                          "redesign": [p.meta["needs_redesign"] for p in a.parts if p.meta.get("needs_redesign")]}
        elif node == "sim":
            simulate = _mod("simulate")
            res["sims"] = simulate.run_all(asm(), design, nd, fast=fast_sim) if simulate else None
            return simulate is not None
        elif node == "wiring":
            e2 = _mod("electronics2")
            res["electronics"] = e2.finalize(design, res["sizing"], res["eplan"], asm(), nd) if e2 and res.get("eplan") else None
            return res["electronics"] is not None
        elif node == "manufacturing":
            mfg = _mod("manufacturing")
            res["manufacturing"] = mfg.plan(design, asm(), nd, res.get("electronics")) if mfg else None
            return mfg is not None
        elif node == "bom":
            mfg = _mod("manufacturing")
            res["bom"] = mfg.bom(design, asm(), res["manufacturing"], res.get("electronics")) if mfg and res.get("manufacturing") else None
            return res["bom"] is not None
        elif node == "manual":
            man = _mod("manual")
            res["manual"] = man.build(design, asm(), res.get("manufacturing"), res.get("electronics"), nd) if man else None
            return man is not None


def _jsonable(o):
    try:
        import numpy as np

        if isinstance(o, np.ndarray):
            return o.tolist()
        if isinstance(o, (np.floating, np.integer)):
            return o.item()
    except ImportError:
        pass
    return str(o)


def summarize(r: dict) -> dict:
    sz = r.get("sizing") or {}
    g = r.get("gripper") or {}
    cad = r.get("cad") or {}
    sims = (r.get("sims") or {}).get("scenarios", {})
    pp = (sims.get("pick_place") or {}).get("metrics", {})
    pay = (sims.get("payload_sweep") or {}).get("metrics", {})
    ws = (sims.get("workspace") or {}).get("metrics", {})
    el = r.get("electronics") or {}
    mfg = r.get("manufacturing") or {}
    bom = (r.get("bom") or {}).get("totals", {})
    man = r.get("manual") or {}
    joints = sz.get("joints", [])
    issues = list(sz.get("issues", [])) + list(cad.get("issues", []) or []) + list((g.get("analysis") or {}).get("issues", [])) + \
        list((g.get("force") or {}).get("issues", [])) + list(cad.get("validate", []) or []) + \
        list(el.get("issues", []) or []) + list(mfg.get("issues", []) or [])
    for node, err in (r.get("errors") or {}).items():
        issues.append(f"{node} stage error: {err}")
    if pp.get("success") is False:
        issues.append(f"pick-and-place failed in simulation ({pp.get('failure') or pp.get('reason') or 'object not delivered'})")
    pending = [n for n, v in (r.get("provenance") or {}).items() if v in ("pending", "error")]
    if pending:
        issues.append("not computed: " + ", ".join(pending))
    return {
        "dof": len(joints), "reach_mm": sz.get("reach_mm"), "payload_g": sz.get("payload_g"),
        "actuators": ", ".join(sorted({j["actuator"] for j in joints})),
        "min_torque_margin": min((j["margin"] for j in joints), default=None),
        "parts": cad.get("parts"), "robot_mass_kg": cad.get("mass_kg"),
        "grasp_hold_force_n": (g.get("sim") or {}).get("min_hold_force_n"),
        "grip_target_n": (g.get("force") or {}).get("target_force_n"),
        "pick_place_success": pp.get("success"), "cycle_time_s": pp.get("cycle_time_s"),
        "max_payload_g": pay.get("max_payload_g"), "workspace_reach_mm": ws.get("reach_mm"),
        "max_torque_pct": max((pp.get("max_torque_pct") or {}).values(), default=None),
        "electronics_config": el.get("config"), "bus": el.get("bus"),
        "pcb": bool(el.get("board")), "harness_wires": len(el.get("harness", []) or []),
        "build_config": mfg.get("config"),
        "unit_cost": bom.get("total"), "fab_lead_days": (mfg.get("totals") or {}).get("lead_days"),
        "tooling_cost": (mfg.get("totals") or {}).get("tooling_cost"),
        "assembly_steps": len(man.get("steps", []) or []), "assembly_time_min": man.get("time_min_total"),
        "feasible": not issues, "issues": issues,
    }
