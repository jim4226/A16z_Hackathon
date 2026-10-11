"""Before/after time study: the judged claim ("time saved") as data.

After = what the ledger measured: Morph's own command wall-clock, machine time,
and the human minutes the team logs with `morph clock` (setup, review,
corrections). Before = the same steps done by hand in CAD + a simulator + a
slicer/CAM. Defaults below are estimates; time one step manually at the venue
and override them in <project>/baseline.json.
"""

from __future__ import annotations

import json
import os

# minutes for an engineer fluent in CAD, but setting up this grasp test for the first time
BASELINE = {
    "spec": (15, "measure object, write requirements"),
    "cad_first": (40, "sketch + extrude + mount holes + pad contour in Fusion/Onshape"),
    "calc_first": (30, "hand calc or FEA setup for deflection/stress and grip force"),
    "sim_setup": (180, "build MuJoCo/Isaac scene, import meshes, script close-lift-shake, debug contacts"),
    "per_variant": (40, "edit CAD, re-export, re-run calc and sim, record results"),
    "machine_files": (20, "slice / CAM, check DFM, export, per process tried"),
    "electronics": (90, "pick servo/sensor/MCU, size the divider, wire, write + debug force-limited firmware"),
}

STEPS = [
    ("spec", "Spec the need"),
    ("design", "CAD + checks + simulate every evaluated variant"),
    ("machine_files", "Machine files + DFM"),
    ("electronics", "Electronics: parts, wiring, firmware"),
    ("review", "Engineer reviews results"),
    ("fab", "Fabricate (machine time)"),
    ("test", "Assemble + physical test"),
    ("corrections", "Corrections (reprints, fixes)"),
]


def load_baseline(project_root: str) -> dict:
    base = {k: v[0] for k, v in BASELINE.items()}
    path = os.path.join(project_root, "baseline.json")
    if os.path.exists(path):
        base.update(json.load(open(path, encoding="utf-8")))
    return base


def study(project, fab_rev: str | None = None) -> dict:
    entries = project.entries()
    revs = [e for e in entries if e["type"] == "revision"]
    tool = [e for e in entries if e["type"] == "tool_time"]
    human, events = {}, {}
    for e in entries:
        if e["type"] == "human_time":
            human[e["step"]] = human.get(e["step"], 0.0) + e["minutes"]
            events[e["step"]] = events.get(e["step"], 0) + 1
    meas = [e for e in entries if e["type"] == "measurement" and (fab_rev is None or e["rev"] == fab_rev)]
    fab_rev = fab_rev or (revs[-1]["rev"] if revs else None)
    processes = {e["predicted"].get("fab_process") for e in revs}

    b = load_baseline(project.root)
    n = max(len(revs), 1)
    fab_pred = project.load_results(fab_rev)["summary"]["fab_minutes"] if fab_rev else 0
    fab_meas = next((m["actual"]["fab_minutes"] for m in reversed(meas) if "fab_minutes" in m["actual"]), None)
    fab = fab_meas if fab_meas is not None else fab_pred
    tool_min = sum(e["seconds"] for e in tool) / 60.0
    has_sensor = bool(fab_rev) and project.load_design(fab_rev).finger.sensor != "none"

    before = {
        "spec": b["spec"],
        "design": b["cad_first"] + b["calc_first"] + b["sim_setup"] + b["per_variant"] * (n - 1),
        "machine_files": b["machine_files"] * max(len(processes), 1),
        "electronics": b["electronics"] if has_sensor else 0.0,
        "review": human.get("review", 0.0),
        "fab": fab,
        "test": human.get("test", 0.0),
        # same hands-on fix, plus the manual CAD -> calc -> sim loop each correction re-enters
        "corrections": human.get("corrections", 0.0) + b["per_variant"] * events.get("corrections", 0),
    }
    after = {
        "spec": human.get("spec", 0.0),
        "design": tool_min,
        "machine_files": 0.0,  # produced inside the same commands
        "electronics": 0.0,    # likewise; flashing and wiring time goes in `morph clock ... test`
        "review": human.get("review", 0.0),
        "fab": fab,
        "test": human.get("test", 0.0),
        "corrections": human.get("corrections", 0.0),
    }
    rows = [{"step": label, "key": k, "before_min": round(before[k], 1), "after_min": round(after[k], 2)}
            for k, label in STEPS]
    tb, ta = sum(before.values()), sum(after.values())
    return {"rows": rows, "before_min": round(tb, 1), "after_min": round(ta, 1),
            "saved_min": round(tb - ta, 1), "speedup": round(tb / ta, 1) if ta > 0 else None,
            "variants_evaluated": n, "fab_measured": fab_meas is not None,
            "human_logged": sorted(human), "tool_minutes": round(tool_min, 2),
            "baseline_overridden": os.path.exists(os.path.join(project.root, "baseline.json"))}
