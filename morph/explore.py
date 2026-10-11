"""Design-space exploration: screen many variants analytically, simulate the best few."""

from __future__ import annotations

from . import analysis, design as cad
from .fab import SHEET_THICKNESSES, choose_process
from .pipeline import Project
from .spec import Design


def _widths(design: Design):
    if choose_process(design) == "fdm_print":
        return [6.0, 8.0, 10.0, 12.0]
    return [w for w in SHEET_THICKNESSES if w >= 3.0]


def best_for_pad(base: Design, pad: str) -> tuple[Design, int]:
    """Smallest finger (by volume) that passes the closed-form checks for this pad style."""
    screened, best, best_key = 0, None, None
    for w in _widths(base):
        for t10 in range(30, 161, 5):
            d = base.with_changes({"finger.pad": pad, "finger.thickness_mm": t10 / 10.0, "finger.width_mm": w})
            g = cad.build_profile(d)
            a = analysis.analyze(d, g)
            screened += 1
            key = (not a["pass"], g.volume_mm3 if a["pass"] else -a["bending_sf"])
            if best_key is None or key < best_key:
                best, best_key = d, key
    return best, screened


def rank_key(summary: dict):
    mhf = summary.get("min_hold_force_n")
    return (not summary["feasible"], -(summary.get("sim_success_rate") or 0),
            mhf if mhf is not None else 1e9, summary.get("fab_minutes") or 1e9)


def explore(project: Project, base: Design, parent: str | None = None, n_trials: int = 8) -> dict:
    pads = ["flat", "v", "contour"] if base.object.shape in ("cylinder", "sphere") else ["flat", "v"]
    revs, screened = {}, 0
    for pad in pads:
        d, n = best_for_pad(base, pad)
        screened += n
        f = d.finger
        print(f"\n[{pad}] best of {n} screened: t={f.thickness_mm} mm, w={f.width_mm} mm -> simulating")
        revs[pad] = project.run(d, parent=parent, label=f"explore:{pad}", n_trials=n_trials)
    ranked = sorted(revs.items(), key=lambda kv: rank_key(project.load_results(kv[1])["summary"]))
    best_pad, best_rev = ranked[0]
    project.log({"type": "exploration", "screened": screened, "candidates": revs, "recommended": best_rev})
    return {"screened": screened, "candidates": revs, "ranked": [r for _, r in ranked], "recommended": best_rev}
