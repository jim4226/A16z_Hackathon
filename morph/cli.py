"""morph CLI.

  morph new can --need "pick up a 66 mm soda can lying on its side, 370 g, print in PETG"
  morph explore can
  morph change can object.mass_g=500
  morph compare can r001 r004
  morph measure can r004 --fab-minutes 18 --grasp 10/10 --length 63.2
  morph send can r004 --grbl /dev/ttyUSB0
  morph research "M3 heat-set inserts for PETG, sizes and suppliers"
  morph demo
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import agent, report
from .explore import explore
from .pipeline import Project
from .spec import Design

HOME = os.environ.get("MORPH_HOME", "projects")


def _project(name: str) -> Project:
    return Project(os.path.join(HOME, name))


def _print_summary(p: Project, rev: str):
    s = p.load_results(rev)["summary"]
    d = p.load_design(rev)
    f = d.finger
    print(f"\n{rev}: pad={f.pad} t={f.thickness_mm} w={f.width_mm} L={f.length_mm} {f.material}")
    print(f"  feasible={s['feasible']}  sim success={s['sim_success_rate']}  min hold force={s['min_hold_force_n']} N"
          f"  (closed-form needs {s['required_grip_n']} N incl. SF)")
    print(f"  deflection={s['tip_deflection_mm']} mm  bending SF={s['bending_sf']}  "
          f"fab={s['fab_process']} {s['fab_minutes']} min ${s['fab_cost']}  DFM warnings={s['dfm_warnings']}")
    for issue in s["issues"]:
        print(f"  ✗ {issue}")


def _report(p: Project, revs, name="report.html") -> str:
    path = os.path.join(p.root, name)
    report.build(p, revs, path)
    print(f"\nreport: {path}")
    return path


def cmd_new(a):
    if a.spec:
        design, src = Design.from_dict(json.load(open(a.spec))), "spec file"
        rationale = ""
    else:
        design, need, src = agent.propose(a.need)
        rationale = need.rationale
        print(f"spec from {src}: {need.model_dump_json(indent=1)}")
    name = a.name or design.name
    p = _project(name)
    if p.revs():
        sys.exit(f"project {name} already exists at {p.root}")
    json.dump({"need": a.need, "source": src, "rationale": rationale}, open(os.path.join(p.root, "meta.json"), "w"))
    rev = p.run(design, label="baseline proposal", n_trials=a.trials)
    _print_summary(p, rev)
    _report(p, [rev], f"{rev}.html")


def cmd_explore(a):
    p = _project(a.name)
    base_rev = a.base or p.latest()
    out = explore(p, p.load_design(base_rev), parent=base_rev, n_trials=a.trials)
    for r in out["ranked"]:
        _print_summary(p, r)
    print(f"\nscreened {out['screened']} variants analytically, simulated {len(out['candidates'])}; "
          f"recommended: {out['recommended']}")
    _report(p, [base_rev] + out["ranked"], "explore.html")


def cmd_change(a):
    p = _project(a.name)
    base = a.base or p.latest()
    changes = dict(kv.split("=", 1) for kv in a.changes)
    design = p.load_design(base).with_changes(changes)
    print(f"{base} -> new revision, changing {changes}")
    rev = p.run(design, parent=base, label=a.label or "change", n_trials=a.trials)
    _print_summary(p, rev)
    _report(p, [base, rev], f"{base}_vs_{rev}.html")


def cmd_compare(a):
    _report(_project(a.name), a.revs, a.out)


def cmd_show(a):
    p = _project(a.name)
    for r in ([a.rev] if a.rev else p.revs()):
        _print_summary(p, r)


def cmd_measure(a):
    p = _project(a.name)
    actual = {}
    if a.fab_minutes is not None:
        actual["fab_minutes"] = a.fab_minutes
    if a.grasp:
        ok, n = map(int, a.grasp.split("/"))
        actual["grasp_success_rate"] = round(ok / n, 3)
        actual["grasp_trials"] = n
    if a.length is not None:
        actual["measured_length_mm"] = a.length
    if a.min_force is not None:
        actual["min_hold_force_n"] = a.min_force
    if a.notes:
        actual["notes"] = a.notes
    entry = p.measure(a.rev, actual)
    print(json.dumps(entry, indent=2))
    _report(p, [a.rev], f"{a.rev}.html")


def cmd_send(a):
    from . import machine

    p = _project(a.name)
    fab_dir = os.path.join(p.rev_dir(a.rev), "fab")
    if a.grbl:
        print(machine.stream_grbl(a.grbl, os.path.join(fab_dir, "job.nc"), dry_run=a.dry_run))
    elif a.octoprint:
        gcode = os.path.join(fab_dir, "plate.gcode")
        if not os.path.exists(gcode):
            sys.exit("no plate.gcode: slice plate.stl in your slicer (or install prusa-slicer) first")
        print(machine.octoprint_upload(a.octoprint, os.environ.get("OCTOPRINT_API_KEY", ""), gcode,
                                       start=a.start, dry_run=a.dry_run))
    else:
        print(f"machine files in {fab_dir}: {os.listdir(fab_dir)}")


def cmd_research(a):
    print(agent.research(a.query))


def cmd_gif(a):
    from . import design as cad, sim

    p = _project(a.name)
    d = p.load_design(a.rev)
    out = os.path.join(p.rev_dir(a.rev), "sim", "grasp.gif")
    if sim.render_gif(d, cad.build_profile(d), out):
        print(out)


def cmd_demo(a):
    need = a.need
    name = a.name
    p = _project(name)
    if p.revs():
        sys.exit(f"{p.root} exists; pick another --name or delete it")
    print(f"== 1. need -> spec\n{need}")
    a2 = argparse.Namespace(need=need, spec=None, name=name, trials=a.trials)
    cmd_new(a2)
    print("\n== 2. explore pad styles and section sizes")
    cmd_explore(argparse.Namespace(name=name, base="r001", trials=a.trials))
    best = [e for e in p.entries() if e["type"] == "exploration"][-1]["recommended"]
    print(f"\n== 3. what if the can is full and heavier? (only analysis + sim should rerun)")
    cmd_change(argparse.Namespace(name=name, base=best, changes=["object.mass_g=520"], label="heavier object", trials=a.trials))
    print(f"\n== 4. what if we laser cut it from Delrin instead? (geometry and sim reused)")
    cmd_change(argparse.Namespace(name=name, base=best, changes=["finger.material=Delrin", "fab.process=laser_cut"],
                                  label="laser cut Delrin", trials=a.trials))
    revs = p.revs()
    _report(p, ["r001", best] + revs[-2:], "report.html")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="morph", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("new", help="describe a need; Morph proposes and evaluates a first design")
    s.add_argument("name", nargs="?")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--need")
    g.add_argument("--spec", help="design JSON instead of natural language")
    s.add_argument("--trials", type=int, default=8)
    s.set_defaults(fn=cmd_new)

    s = sub.add_parser("explore", help="search pad styles/sections, simulate the best, rank")
    s.add_argument("name")
    s.add_argument("--from", dest="base")
    s.add_argument("--trials", type=int, default=8)
    s.set_defaults(fn=cmd_explore)

    s = sub.add_parser("change", help="change parameters; recompute only what is affected")
    s.add_argument("name")
    s.add_argument("changes", nargs="+", help="dotted.key=value")
    s.add_argument("--from", dest="base")
    s.add_argument("--label")
    s.add_argument("--trials", type=int, default=8)
    s.set_defaults(fn=cmd_change)

    s = sub.add_parser("compare", help="HTML side-by-side of revisions")
    s.add_argument("name")
    s.add_argument("revs", nargs="+")
    s.add_argument("-o", "--out", default="compare.html")
    s.set_defaults(fn=cmd_compare)

    s = sub.add_parser("show")
    s.add_argument("name")
    s.add_argument("rev", nargs="?")
    s.set_defaults(fn=cmd_show)

    s = sub.add_parser("measure", help="record what really happened after fabrication")
    s.add_argument("name")
    s.add_argument("rev")
    s.add_argument("--fab-minutes", type=float)
    s.add_argument("--grasp", help="successes/trials, e.g. 9/10")
    s.add_argument("--length", type=float, help="measured finger length, mm")
    s.add_argument("--min-force", type=float, help="measured min holding force, N")
    s.add_argument("--notes")
    s.set_defaults(fn=cmd_measure)

    s = sub.add_parser("send", help="send machine files to a connected machine")
    s.add_argument("name")
    s.add_argument("rev")
    s.add_argument("--grbl", help="serial port of a GRBL CNC/laser, e.g. /dev/ttyUSB0 or COM3")
    s.add_argument("--octoprint", help="OctoPrint base URL (API key in OCTOPRINT_API_KEY)")
    s.add_argument("--start", action="store_true")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(fn=cmd_send)

    s = sub.add_parser("research", help="web research via Claude (components, tooling, suppliers)")
    s.add_argument("query")
    s.set_defaults(fn=cmd_research)

    s = sub.add_parser("gif", help="render the grasp to a GIF (needs MUJOCO_GL=egl/osmesa)")
    s.add_argument("name")
    s.add_argument("rev")
    s.set_defaults(fn=cmd_gif)

    s = sub.add_parser("demo", help="scripted end-to-end run")
    s.add_argument("--name", default="soda-can")
    s.add_argument("--need", default="Our robot arm needs to pick up a 66 mm soda can lying on its side, "
                                      "370 g, and we want to 3D print the fingers in PETG today.")
    s.add_argument("--trials", type=int, default=8)
    s.set_defaults(fn=cmd_demo)

    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
