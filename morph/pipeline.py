"""Change propagation + revision ledger.

Every revision is a directory holding the design and every artifact derived
from it. When a parameter changes, only the artifacts that depend on it are
recomputed; the rest are carried over from the parent revision and marked as
reused. The ledger (ledger.jsonl) is the dataset Morph builds over time:
design -> predictions -> fabrication -> measured outcome.
"""

from __future__ import annotations

import fnmatch
import json
import os
import shutil
import time

from . import analysis, board, design as cad, electronics, fab, keewano, mechanism, sim
from .spec import Design, diff

# node: (parameter patterns it reads, upstream nodes)
GRAPH = {
    "cad": (["finger.length_mm", "finger.thickness_mm", "finger.width_mm", "finger.pad*",
             "finger.mount_*", "object.shape", "object.diameter_mm"], []),
    "analysis": (["finger.*", "object.shape", "object.diameter_mm", "object.mass_g", "object.friction",
                  "gripper.*", "safety_factor", "fab.process"], ["cad"]),
    "sim": (["object.shape", "object.diameter_mm", "object.length_mm", "object.mass_g", "object.friction",
             "gripper.*"], ["cad"]),
    "mech": (["electronics.servo", "electronics.pinion_radius_mm"], ["cad", "analysis", "sim"]),
    "electronics": (["electronics.*", "object.max_force_n", "finger.sensor"], ["analysis", "sim", "mech"]),
    "fab": (["fab.*", "finger.material", "finger.width_mm"], ["cad", "mech"]),
    "bom": (["gripper.name", "fab.quantity"], ["fab", "electronics"]),
}
ORDER = ["cad", "analysis", "sim", "mech", "electronics", "fab", "bom"]


def dirty_nodes(changed_keys) -> list:
    dirty = set()
    for node, (patterns, _) in GRAPH.items():
        if any(fnmatch.fnmatch(k, p) for k in changed_keys for p in patterns):
            dirty.add(node)
    grew = True
    while grew:
        grew = False
        for node, (_, ups) in GRAPH.items():
            if node not in dirty and any(u in dirty for u in ups):
                dirty.add(node)
                grew = True
    return [n for n in ORDER if n in dirty]


class Project:
    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)
        self.ledger_path = os.path.join(root, "ledger.jsonl")

    # -- storage helpers
    def revs(self) -> list:
        return sorted(d for d in os.listdir(self.root) if d.startswith("r") and d[1:].isdigit())

    def latest(self):
        revs = self.revs()
        return revs[-1] if revs else None

    def rev_dir(self, rev: str) -> str:
        return os.path.join(self.root, rev)

    def load_design(self, rev: str) -> Design:
        with open(os.path.join(self.rev_dir(rev), "design.json"), encoding="utf-8") as fh:
            return Design.from_dict(json.load(fh))

    def load_results(self, rev: str) -> dict:
        with open(os.path.join(self.rev_dir(rev), "results.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def log(self, entry: dict):
        entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **entry}
        with open(self.ledger_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
        design = None
        if entry.get("type") == "measurement":
            try:
                design = self.load_design(entry["rev"]).to_dict()
                design["electronics"]["servo"] = self.load_results(entry["rev"])["summary"].get("servo")
            except Exception:
                pass
        keewano.emit(os.path.basename(os.path.normpath(self.root)), entry, design)

    def entries(self) -> list:
        if not os.path.exists(self.ledger_path):
            return []
        with open(self.ledger_path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    # -- the pipeline
    def run(self, design: Design, parent: str | None = None, label: str = "", n_trials: int = 8,
            verbose: bool = True, on_event=None) -> str:
        emit = on_event or (lambda e: None)
        revs = self.revs()
        rev = f"r{len(revs) + 1:03d}"
        out = self.rev_dir(rev)
        os.makedirs(out)
        with open(os.path.join(out, "design.json"), "w", encoding="utf-8") as fh:
            fh.write(design.dumps())

        if parent:
            changes = diff(self.load_design(parent), design)
            todo = dirty_nodes(changes.keys())
            prev = self.load_results(parent)
        else:
            changes, todo, prev = {}, list(ORDER), {}

        results = {"rev": rev, "parent": parent, "label": label,
                   "changes": {k: list(v) for k, v in changes.items()},
                   "provenance": {}, "timings_s": {}}
        geom = cad.build_profile(design)  # cheap; needed by every downstream node
        emit({"type": "start", "rev": rev, "parent": parent, "changes": results["changes"], "dirty": todo})

        for node in ORDER:
            if node not in todo:
                results[node] = prev.get(node)
                results["provenance"][node] = f"reused from {parent}"
                src = os.path.join(self.rev_dir(parent), node)
                if os.path.isdir(src):
                    shutil.copytree(src, os.path.join(out, node))
                if verbose:
                    print(f"  {node:<9} reused  (unaffected by change)")
                emit({"type": "node", "node": node, "status": "reused"})
                continue
            emit({"type": "node", "node": node, "status": "computing"})
            t0 = time.time()
            nd = os.path.join(out, node)
            os.makedirs(nd, exist_ok=True)
            if node == "cad":
                mesh = cad.export_stl(geom, os.path.join(nd, "finger.stl"))
                results["cad"] = {"area_mm2": round(geom.area_mm2, 1), "volume_mm3": round(geom.volume_mm3, 1),
                                  "contact_half_angle_deg": round(geom.contact_half_angle_deg, 1),
                                  "mesh": mesh, "notes": geom.notes,
                                  "profile": [list(map(lambda c: [round(c[0], 3), round(c[1], 3)], r.coords))
                                              for r in [geom.profile.exterior, *geom.profile.interiors]]}
            elif node == "analysis":
                results["analysis"] = analysis.analyze(design, geom)
            elif node == "sim":
                with open(os.path.join(nd, "scene.xml"), "w", encoding="utf-8") as fh:
                    fh.write(sim.build_mjcf(design, geom))
                results["sim"] = sim.simulate(design, geom, n_trials)
            elif node == "mech":
                servo = electronics.choose_servo(design, electronics.target_force(design, results["analysis"], results["sim"]))
                results["mech"] = mechanism.build(design, geom, servo, nd)
            elif node == "electronics":
                results["electronics"] = electronics.plan(design, results["analysis"], results["sim"], results["mech"])
                if results["electronics"]["sensor"] != "none":
                    electronics.write_firmware(design, results["electronics"], nd)
                    results["electronics"]["firmware"] = "electronics/morph_grip/morph_grip.ino"
                    results["electronics"]["board"] = board.build(results["electronics"], os.path.join(nd, "board"))
            elif node == "fab":
                results["fab"] = fab.plan(design, geom, nd, mechanism.printed_profiles(design, geom, results["mech"]["servo"]))
            elif node == "bom":
                results["bom"] = fab.bom(design, results["fab"]) + results["electronics"]["parts"]
            results["provenance"][node] = "computed"
            results["timings_s"][node] = round(time.time() - t0, 2)
            if verbose:
                print(f"  {node:<9} computed ({results['timings_s'][node]:.2f}s)")
            emit({"type": "node", "node": node, "status": "computed", "seconds": results["timings_s"][node]})

        results["summary"] = summarize(results)
        with open(os.path.join(out, "results.json"), "w", encoding="utf-8") as fh:
            json.dump(results, fh, indent=2)
        self.log({"type": "revision", "rev": rev, "parent": parent, "label": label,
                  "changes": results["changes"], "recomputed": todo,
                  "design": design.to_dict(), "predicted": results["summary"]})
        return rev

    def measure(self, rev: str, actual: dict) -> dict:
        pred = self.load_results(rev)["summary"]
        errors = {}
        for key, pkey in (("fab_minutes", "fab_minutes"), ("grasp_success_rate", "sim_success_rate"),
                          ("min_hold_force_n", "min_hold_force_n"), ("hold_force_n", "target_force_n")):
            if key in actual and pred.get(pkey) not in (None, float("inf")):
                errors[key] = round(actual[key] - pred[pkey], 3)
        if "measured_length_mm" in actual:
            nominal = self.load_design(rev).finger.length_mm
            errors["length_mm"] = round(actual["measured_length_mm"] - nominal, 3)
        entry = {"type": "measurement", "rev": rev, "actual": actual, "error_vs_prediction": errors}
        self.log(entry)
        return entry


def summarize(r: dict) -> dict:
    a, s, f = r.get("analysis") or {}, r.get("sim") or {}, r.get("fab") or {}
    e = r.get("electronics") or {}
    feasible = bool(a.get("pass")) and s.get("success_rate", 0) >= 0.875 and e.get("pass", True)
    return {
        "feasible": feasible,
        "sim_success_rate": s.get("success_rate"),
        "min_hold_force_n": s.get("min_hold_force_n"),
        "mean_slip_mm": s.get("mean_slip_mm"),
        "required_grip_n": a.get("required_grip_n"),
        "grip_margin": a.get("grip_margin"),
        "tip_deflection_mm": a.get("tip_deflection_mm"),
        "bending_sf": a.get("bending_sf"),
        "finger_mass_g": a.get("finger_mass_g"),
        "fab_process": f.get("process"),
        "fab_minutes": f.get("minutes"),
        "fab_cost": f.get("total_cost"),
        "dfm_warnings": len(f.get("dfm_warnings", [])),
        "target_force_n": e.get("target_force_n"),
        "servo": e.get("servo"),
        "servo_stall_force_n": e.get("servo_stall_force_n"),
        "open_loop_crushes": e.get("open_loop_crushes"),
        "bom_cost": round(sum(i.get("cost", 0) for i in r.get("bom") or []), 2),
        "jaw_travel_mm": (r.get("mech") or {}).get("travel_mm"),
        "servo_sweep_deg": (r.get("mech") or {}).get("servo_sweep_deg"),
        "issues": a.get("issues", []) + (r.get("mech") or {}).get("issues", []) + e.get("issues", []),
    }
