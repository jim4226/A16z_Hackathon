"""Before/after time study for whole-robot projects.

"By hand" = cited manual-engineering minutes (morph/robot/time_baseline.json,
built from published surveys and case studies) for every engineering step a
revision touched. "With Morph" = the pipeline's measured runtime for that
revision plus human minutes the team logged (`morph clock`). A first revision
costs every step in full. A later revision (one design change) costs the
cited per-change effort scaled by the share of engineering work the change
touches: a BOM-only edit costs minutes, a change that ripples through CAD,
simulation, electronics and manufacturing costs the full per-change figure.
This is deliberately conservative.
"""

from __future__ import annotations

import json
import os

BASELINE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "time_baseline.json")

# pipeline node -> baseline steps it replaces
NODE_STEPS = {
    "sizing": ["cad_arm_and_gripper"],
    "cad": ["cad_arm_and_gripper"],
    "gripper": ["simulation_setup_and_runs"],
    "sim": ["simulation_setup_and_runs"],
    "electronics": ["electronics_schematic_pcb"],
    "wiring": ["electronics_schematic_pcb", "firmware"],
    "manufacturing": ["manufacturing_prep_quotes"],
    "bom": ["bom_sourcing"],
    "manual": ["assembly_manual"],
}


def load_baseline() -> dict:
    with open(BASELINE_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def _steps(base: dict) -> dict:
    return {s["key"]: s for s in base["steps"]}


def revision(results: dict, base: dict | None = None, human_min: float = 0.0) -> dict:
    """Time card for one revision's results.json."""
    base = base or load_baseline()
    steps = _steps(base)
    prov = results.get("provenance") or {}
    first = not results.get("parent")
    touched = []
    for node, p in prov.items():
        if p == "computed":
            for k in NODE_STEPS.get(node, []):
                if k not in touched:
                    touched.append(k)
    if first and "spec_requirements" in steps:
        touched.insert(0, "spec_requirements")
    total_typ = sum(s["typical_min"] for s in steps.values()) or 1.0
    per_change = (base.get("per_change") or {}).get("typical_min", 1440.0)
    rows = []
    for k in touched:
        s = steps.get(k)
        if not s:
            continue
        full = s["typical_min"]
        before = full if first else per_change * full / total_typ
        rows.append({"key": k, "label": s["label"], "before_min": round(before, 1), "full_min": full,
                     "low_min": s.get("low_min"), "high_min": s.get("high_min"), "citations": s.get("citations", []),
                     "derivation": s.get("derivation", "")})
    frac = per_change / total_typ
    tim = results.get("timings_s") or {}
    compute_min = sum(v for n, v in tim.items() if prov.get(n) == "computed") / 60.0
    # the engineer still reads and checks what Morph produced: logged minutes if any, else a stated allowance
    review = base.get("review_allowance_min", {"first": 15.0, "change": 5.0})
    review_min = human_min if human_min > 0 else review["first" if first else "change"]
    after_min = compute_min + review_min
    before = sum(r["before_min"] for r in rows)
    return {"first": first, "rows": rows, "before_min": round(before, 1), "after_min": round(after_min, 2),
            "compute_min": round(compute_min, 2), "review_min": round(review_min, 1), "review_assumed": human_min <= 0,
            "saved_min": round(before - after_min, 1), "speedup": round(before / after_min, 0) if after_min > 0 else None,
            "per_change_share": round(frac, 4)}


def project(p, base: dict | None = None) -> dict:
    """Cumulative card across every revision of a project (each chat edit adds up)."""
    base = base or load_baseline()
    human = {}
    for e in p.entries():
        if e.get("type") == "human_time":
            human[e.get("rev") or "_"] = human.get(e.get("rev") or "_", 0.0) + e["minutes"]
    revs, tb, ta, designed = [], 0.0, 0.0, False
    for r in p.finished():  # a revision being built, cut off or damaged has no results to count
        try:
            res = p.load_results(r)
        except (OSError, ValueError):
            continue
        if not res.get("parent"):
            # a parentless revision after the first is the same design rebuilt by newer Morph code,
            # not new engineering work: count the design once
            if designed:
                continue
            designed = True
        card = revision(res, base, human.get(r, 0.0))
        tb += card["before_min"]
        ta += card["after_min"]
        revs.append({"rev": r, "label": res.get("label"), "before_min": card["before_min"], "after_min": card["after_min"]})
    ta += human.get("_", 0.0)
    return {"revisions": revs, "before_min": round(tb, 1), "after_min": round(ta, 2), "saved_min": round(tb - ta, 1),
            "before_days": round(tb / 60 / 8, 1), "speedup": round(tb / ta, 0) if ta > 0 else None,
            "headlines": base.get("headlines", []), "caveats": base.get("caveats", ""), "method": __doc__.strip()}
