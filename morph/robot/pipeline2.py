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
from ..services.durable import write_text
from ..spec import Design, diff
from . import catalog_extra  # noqa: F401  (imported robots' parts, needed even when their sizing is reused)

# node: (parameter patterns it reads, upstream nodes, output directory)
GRAPH = {
    # arm.size() reads the build config (link materials set the masses), the task (reach) and the hand
    # (gripper/finger set the payload, a wrist camera or IMU in electronics.extras rides on it), so a pilot build
    # re-sizes the joints instead of reusing prototype torques
    "sizing": (["arm.*", "object.*", "electronics.servo", "electronics.extras", "build.*", "task.pick_*", "task.place_*",
                "finger.*", "gripper.*"], [], None),
    "gripper": (["finger.*", "object.*", "gripper.*", "safety_factor", "electronics.target_force_n",
                 "electronics.servo", "electronics.supply_v", "electronics.pinion_radius_mm", "arm.actuator_family",
                 "arm.source", "arm.actuator_overrides"],
                [], "gripper"),
    "electronics": (["electronics.*"], ["sizing", "gripper"], None),
    "cad": (["finger.*", "object.shape", "object.diameter_mm", "object.length_mm", "build.*", "name", "arm.source"],
            ["sizing", "electronics"], "cad"),
    # sim reads the arm geometry, but electronics placed in the fixed base do not change its dynamics,
    # so it keys off the geometry-defining parameters instead of the cad node (plus the extras that ride on the hand,
    # and the room it is simulated in: a world-only edit reruns the simulation and nothing else)
    "sim": (["task.*", "object.*", "arm.*", "finger.*", "build.*", "electronics.extras", "world.*"], ["sizing", "gripper"],
            "sim"),
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


# The morph/*.py modules the robot pipeline computes with (its import graph, without keewano.py and settings.py:
# Keewano's calibration is opt-in data, and features append event ids there). Every morph/robot/*.py is hashed too.
PIPELINE_MODULES = ("spec", "design", "analysis", "electronics", "sim", "mechanism", "fab", "materials", "pipeline")
_BUNDLES: dict = {}


def _code_files() -> list:
    here = os.path.dirname(os.path.abspath(__file__))
    top = os.path.dirname(here)
    robot = [os.path.join(here, fn) for fn in sorted(os.listdir(here)) if fn.endswith(".py")]
    return robot + [os.path.join(top, m + ".py") for m in PIPELINE_MODULES]


def _bundle_hash(source: str) -> str:
    """The files of an imported robot's bundle (URDF, meshes, profile), hashed again only when one changes."""
    import hashlib

    try:
        from .imported import robot_dir
        root = robot_dir(source)
    except (ValueError, OSError, ImportError):
        return "missing"
    try:
        files = []
        for directory, dirs, names in os.walk(root):
            dirs[:] = sorted(d for d in dirs if not d.startswith("."))
            for fn in sorted(names):
                path = os.path.join(directory, fn)
                if fn.startswith(".") or os.path.islink(path):
                    continue
                st = os.stat(path)
                files.append((os.path.relpath(path, root).replace(os.sep, "/"), st.st_size, st.st_mtime_ns))
        key = (root, tuple(files))
        if key not in _BUNDLES:
            h = hashlib.sha1()
            for rel, _, _ in files:
                with open(os.path.join(root, rel), "rb") as fh:
                    h.update(rel.encode() + b"\0" + fh.read())
            _BUNDLES[key] = h.hexdigest()
        return _BUNDLES[key]
    except OSError:
        return "unreadable"


def pipeline_version(source: str | None = None) -> str:
    """Hash of the code that computes a revision (morph/robot/*.py plus PIPELINE_MODULES) and, for an imported robot
    (`source` = arm.source), of its bundle: results built by older code or from an edited bundle are rebuilt when the
    Studio starts. Studio, CLI and service modules are not hashed, so editing them never rebuilds the demo robots."""
    import hashlib

    h = hashlib.sha1()
    for path in _code_files():
        with open(path, "rb") as fh:
            h.update(os.path.basename(path).encode() + fh.read())
    if source:
        h.update(b"bundle:" + _bundle_hash(source).encode())
    return h.hexdigest()[:12]


def _mod(name):
    try:
        return importlib.import_module(f"morph.robot.{name}")
    except ImportError:
        return None


OUTPUT_KEYS = {"sizing": "sizing", "gripper": "gripper", "electronics": "eplan", "cad": "cad", "sim": "sims",
               "wiring": "electronics", "manufacturing": "manufacturing", "bom": "bom", "manual": "manual"}
INVALID = {"pending": "not computed: its module is not available", "error": "failed", "blocked": "not built",
           "preview": "preview only"}


def _preview(results: dict) -> bool:
    return bool(results.get("preview_only") or (results.get("cad") or {}).get("preview_only"))


def stage_problem(results: dict, node: str) -> str | None:
    """Why this stage's result can't be used (an error, a missing output, a preview-only stand-in), or None.
    Reads the outputs themselves, so it also judges revisions saved before stage_status existed."""
    error = (results.get("errors") or {}).get(node)
    if error:
        return error
    status = (results.get("stage_status") or {}).get(node) or (results.get("provenance") or {}).get(node)
    if status in INVALID:
        return INVALID[status]
    if not results.get(OUTPUT_KEYS[node]):
        return "missing required output"
    if node == "cad" and _preview(results):
        return "the basic model is a preview only; manufacturing needs the detailed robot CAD"
    return None


def artifact_problem(results: dict, node: str, revision_dir: str) -> str | None:
    """A saved result can't stand in for files that are gone: every file the stage reports must exist
    inside the revision folder."""
    value = results.get(OUTPUT_KEYS[node]) or {}
    paths = []
    if node == "cad":
        paths = ["cad/robot.glb", "cad/robot.json", value.get("urdf")]
    elif node == "sim":
        scenarios = value.get("scenarios") or {}
        if not scenarios or any(not s.get("file") or (s.get("metrics") or {}).get("error") for s in scenarios.values()):
            return "a simulation scenario failed or wrote no output"
        paths = ["sim/" + str(value.get("mjcf") or "")] + ["sim/" + s["file"] for s in scenarios.values()]
    elif node == "wiring":
        paths = ["electronics/" + str(value.get(k) or "") for k in ("schematic_svg", "schematic_json", "harness_csv", "firmware")]
    elif node == "manufacturing":
        paths = ["mfg/manufacturing.json", "mfg/" + str(value.get("package") or "")]
        paths += ["mfg/" + f for part in value.get("parts", []) for f in part.get("files", [])]
    elif node == "manual":
        paths = ["manual/" + str(value.get(k) or "") for k in ("json", "html")]
    base = os.path.realpath(revision_dir)
    for path in paths:
        if not path or path.endswith("/"):
            return f"missing required artifact reference for {node}"
        target = os.path.realpath(os.path.join(base, path))
        try:
            ok = os.path.commonpath([base, target]) == base and os.path.isfile(target)
        except ValueError:
            ok = False
        if not ok:
            return f"missing required artifact for {node}: {path}"
    return None


class RobotProject(Project):
    def run(self, design: Design, parent: str | None = None, label: str = "", n_trials: int = 4,
            verbose: bool = True, on_event=None, fast_sim: bool = False, by: str | None = None,
            proposed_by: str | None = None) -> str:
        """Build a revision of `design` (reusing what `parent` already has) and return its name. `by` is the team
        member who asked for it (Teams), `proposed_by` the author of the change request it applies; both are recorded
        in results.json and the ledger."""
        emit = on_event or (lambda e: None)
        if parent:  # read before a revision is claimed: an unreadable parent never leaves a half-made revision
            try:
                base, prev = self.load_design(parent), self.load_results(parent)
                if not isinstance(prev, dict):
                    raise ValueError("it isn't a results file")
            except (OSError, ValueError, KeyError, TypeError) as exc:
                raise ValueError(f"{parent}'s saved files can't be read ({exc.__class__.__name__}: {exc}); Morph won't "
                                 "build on it.") from None
            changes = diff(base, design)
            todo = dirty_nodes(changes.keys())
        else:
            changes, todo, prev = {}, list(ORDER), {}
        rev, lock = self.claim_build()
        try:
            return self._build(rev, design, parent, label, n_trials, verbose, emit, fast_sim, by, proposed_by, changes,
                               todo, prev)
        except BaseException:
            # it never finished (an error, a full disk, Ctrl+C): its folder holds only files that can be made again, and
            # left behind it would keep the disk full and be read as a cut-off build
            if not os.path.isfile(os.path.join(self.rev_dir(rev), "results.json")):
                self.discard(rev)
            raise
        finally:
            lock.release()

    def _build(self, rev, design, parent, label, n_trials, verbose, emit, fast_sim, by, proposed_by, changes, todo, prev):
        from . import arm

        out = self.rev_dir(rev)
        write_text(os.path.join(out, "design.json"), design.dumps())
        res = {"rev": rev, "parent": parent, "label": label, "kind": "robot",
               "changes": {k: list(v) for k, v in changes.items()}, "provenance": {}, "stage_status": {}, "timings_s": {}}
        if by:
            res["by"] = by
        if proposed_by:
            res["proposed_by"] = proposed_by
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
                    except Exception as exc:  # show a basic model to look at, but never manufacture the stand-in
                        traceback.print_exc()
                        from . import arm_stub
                        res.setdefault("warnings", []).append(f"detailed arm generator failed ({exc}); showing the basic "
                                                              "model as a preview only, not for manufacturing")
                        res["preview_only"] = True
                        cache["asm"] = arm_stub.build(design, res["sizing"], res.get("eplan"))
            return cache["asm"]

        for node in ORDER:
            sub = GRAPH[node][2]
            if node not in todo:
                for key in self._keys(node):
                    res[key] = prev.get(key)
                # a failed, incomplete or preview-only stage stays that way: an unrelated edit never launders it
                problem = stage_problem(prev, node) or artifact_problem(prev, node, self.rev_dir(parent))
                preview = node == "cad" and _preview(prev)
                if preview:
                    res["preview_only"] = True
                elif problem:
                    res.setdefault("errors", {})[node] = f"{problem} (carried over from {parent})"
                status = "preview" if preview else "error" if problem else "reused"
                res["provenance"][node] = f"reused from {parent}" if status == "reused" else status
                res["stage_status"][node] = status
                if sub and os.path.isdir(os.path.join(self.rev_dir(parent), sub)):
                    shutil.copytree(os.path.join(self.rev_dir(parent), sub), os.path.join(out, sub))
                emit({"type": "node", "node": node, "status": status})
                if verbose:
                    print(f"  {node:<14} {status if status == 'reused' else status + ' (carried over)'}")
                continue
            blocked = [up for up in GRAPH[node][1] if stage_problem(res, up)]
            if blocked:  # never build on an invalid input
                why = ("needs the detailed robot CAD, not the basic preview" if "cad" in blocked and _preview(res)
                       else "needs " + ", ".join(blocked) + ", which did not complete")
                res.setdefault("errors", {})[node] = f"not built: {why}"
                res["provenance"][node] = res["stage_status"][node] = "blocked"
                emit({"type": "node", "node": node, "status": "blocked"})
                if verbose:
                    print(f"  {node:<14} blocked ({', '.join(blocked)})")
                continue
            emit({"type": "node", "node": node, "status": "computing"})
            t0 = time.time()
            nd = os.path.join(out, sub) if sub else out
            os.makedirs(nd, exist_ok=True)
            status = "computed"
            try:
                if self._compute(node, design, res, nd, asm, n_trials, fast_sim) is False:
                    status = "pending"          # module not available yet: say so, never fake "computed"
                elif node == "cad" and _preview(res):
                    status = "preview"          # the basic model: shown, never manufactured
                else:
                    problem = stage_problem(res, node) or artifact_problem(res, node, out)
                    if problem:
                        res.setdefault("errors", {})[node] = problem
                        status = "error"
            except Exception as exc:  # keep the rest of the pipeline alive; surface the error
                traceback.print_exc()
                res.setdefault("errors", {})[node] = f"{exc.__class__.__name__}: {exc}"
                status = "error"
            res["provenance"][node] = status
            res["stage_status"][node] = status
            res["timings_s"][node] = round(time.time() - t0, 2)
            emit({"type": "node", "node": node, "status": status, "seconds": res["timings_s"][node]})
            if verbose:
                print(f"  {node:<14} {status} ({res['timings_s'][node]:.2f}s)")

        res["summary"] = summarize(res)
        res["pipeline_version"] = pipeline_version(design.arm.source)
        # results.json marks a finished revision: it appears whole, and is on the disk before the ledger says so
        write_text(os.path.join(out, "results.json"), json.dumps(res, indent=1, default=_jsonable))
        self.log({"type": "revision", "rev": rev, "parent": parent, "label": label, "changes": res["changes"],
                  "recomputed": todo, "design": design.to_dict(), "predicted": res["summary"], **({"by": by} if by else {}),
                  **({"proposed_by": proposed_by} if proposed_by else {})})
        return rev

    @staticmethod
    def _keys(node):
        return {"sizing": ["sizing"], "gripper": ["gripper"], "electronics": ["eplan"], "cad": ["cad"],
                "sim": ["sims"], "wiring": ["electronics"], "manufacturing": ["manufacturing"], "bom": ["bom"],
                "manual": ["manual"]}[node]

    def _sim_world(self, design, nd):
        """World mode: the room design.world names, in robot base millimetres: the world saved under design.world.sha
        (world/saved/<sha>.json; the room may have been edited again since), else world/world.json; also kept as
        sim/world.json so the revision shows what it was simulated against. A named world that is missing or broken
        fails the stage rather than silently simulating the plain table."""
        if not design.world.id:
            return None
        from ..studio import worlds
        from ..studio.security import contained_path

        if not isinstance(design.world.id, str) or not worlds.SLUG.fullmatch(design.world.id):
            raise RuntimeError("world mode: world.id must be a world's id (lower-case letters, digits and -), never a path")
        kept = worlds.saved_path(self.root, design.world.sha)
        for path in (worlds.world_path(self.root), kept):
            rel = os.path.relpath(path, self.root).replace(os.sep, "/") if path else None
            if rel and os.path.lexists(path) and not contained_path(self.root, rel, decode=False):
                raise RuntimeError(f"world mode: {rel} must be a file inside the project folder")
        w = worlds.load_pinned(self.root, design.world.id, design.world.sha)
        if w is None:
            raise RuntimeError(f"world mode: {design.world.id} isn't this project's world (world/world.json); "
                               "save the world again or turn world mode off")
        sw = worlds.sim_world(w)
        sw["design_sha"] = design.world.sha
        with open(os.path.join(nd, "world.json"), "w", encoding="utf-8") as fh:
            json.dump(sw, fh, indent=1)
        return sw

    def _compute(self, node, design, res, nd, asm, n_trials, fast_sim):
        from . import arm

        if node == "sizing":
            if design.arm.source:
                from . import imported
                res["sizing"] = imported.size(design)
            else:
                try:
                    res["sizing"] = arm.size(design)
                except Exception as exc:  # basic numbers to show, but nothing downstream may build on them
                    from . import arm_stub
                    res["sizing"] = arm_stub.size(design)
                    res.setdefault("errors", {})["sizing"] = f"detailed sizing failed ({exc.__class__.__name__}: {exc}); " \
                                                             "the basic estimate is a preview only"
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
                          "preview_only": bool(res.get("preview_only")),
                          "license": a.meta.get("license"), "source_url": a.meta.get("source_url"),
                          "issues": a.meta.get("issues", []),
                          "redesign": [p.meta["needs_redesign"] for p in a.parts if p.meta.get("needs_redesign")]}
        elif node == "sim":
            simulate = _mod("simulate")
            world = self._sim_world(design, nd) if simulate else None
            res["sims"] = (simulate.run_all(asm(), design, nd, fast=fast_sim, **({"world": world} if world else {}))
                           if simulate else None)
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
        list((r.get("eplan") or {}).get("issues", []) or []) + list(el.get("issues", []) or []) + list(mfg.get("issues", []) or [])
    issues = list(dict.fromkeys(issues))   # the wiring stage repeats the plan's issues
    for node, err in (r.get("errors") or {}).items():
        issues.append(f"{node} stage error: {err}")
    if pp.get("success") is False:
        issues.append(f"pick-and-place failed in simulation ({pp.get('failure') or pp.get('reason') or 'object not delivered'})")
    pending = [n for n in ORDER if stage_problem(r, n)]
    if pending:
        issues.append("not computed: " + ", ".join(pending))
    if _preview(r):
        issues.append("preview only: the basic model stands in for the detailed robot CAD and is not valid for manufacturing")
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
