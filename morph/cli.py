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
import time

from . import agent, boardrepo, darwin, keewano, kicad, report
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
    name = a.name = a.name or design.name
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


def cmd_clock(a):
    p = _project(a.name)
    p.log({"type": "human_time", "step": a.step, "minutes": a.minutes, "note": a.note or ""})
    print(f"logged {a.minutes} min of human time for '{a.step}'")


def cmd_time(a):
    from .timestudy import study

    t = study(_project(a.name), a.rev)
    print(f"{'step':<48}{'before (min)':>14}{'after (min)':>13}")
    for r in t["rows"]:
        print(f"{r['step']:<48}{r['before_min']:>14}{r['after_min']:>13}")
    mult = f"{t['speedup']}x, " if t["human_logged"] else ""
    print(f"{'total':<48}{t['before_min']:>14}{t['after_min']:>13}   -> {mult}"
          f"{t['saved_min']} min saved over {t['variants_evaluated']} evaluated variants")
    if not t["human_logged"]:
        print("note: no human time logged yet; use `morph clock NAME review 5` etc. so the claim counts it")


def cmd_bench(a):
    from . import bench

    p = _project(a.name)
    if not a.port and not a.simulate:
        sys.exit("give --port COM5 (or /dev/ttyUSB0), or --simulate for a rehearsal")
    bench.run(p, a.rev, a.port, a.seconds, a.simulate)
    _report(p, [a.rev], f"{a.rev}.html")


def open_app_window(url: str):
    """Open the Studio in its own app-style window (Edge/Chrome --app), falling back to a browser tab."""
    import shutil
    import subprocess
    import webbrowser

    cands = []
    if sys.platform.startswith("win"):
        pf = [os.environ.get(k, "") for k in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
        cands = [os.path.join(b, *rel) for b in pf if b for rel in
                 (("Microsoft", "Edge", "Application", "msedge.exe"), ("Google", "Chrome", "Application", "chrome.exe"))]
    elif sys.platform == "darwin":
        cands = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                 "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]
    else:
        cands = [shutil.which(x) or "" for x in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge")]
    for exe in cands:
        if exe and os.path.exists(exe):
            subprocess.Popen([exe, f"--app={url}", "--window-size=1600,900"])
            return
    webbrowser.open(url)


def cmd_studio(a):
    from .robot.pipeline2 import RobotProject
    from .studio.server import serve

    if os.environ.get("MUJOCO_GL") is None and sys.platform.startswith("linux"):
        os.environ["MUJOCO_GL"] = "egl"
    p = RobotProject(os.path.join(HOME, a.name))
    if not p.revs():
        print(f"no project {a.name} yet: generating the first robot (about a minute)...")
        cmd_robot(argparse.Namespace(action="new", name=a.name, need=None, trials=4, fast=False, base=None, changes=[],
                                     source="so101" if a.name == "so101" else None))
    other = "so101" if a.name != "so101" else "egg-picker"
    if not RobotProject(os.path.join(HOME, other)).revs():
        print(f"also preparing the {other} project for the project switcher...")
        cmd_robot(argparse.Namespace(action="new", name=other, need=None, trials=4, fast=False, base=None, changes=[],
                                     source="so101" if other == "so101" else None))
    if a.open:
        import threading
        threading.Timer(1.5, open_app_window, args=(f"http://{a.host}:{a.port}",)).start()
    serve(p, a.host, a.port, home=HOME)


EGG_ROBOT = {"name": "egg-picker", "object.name": "raw egg", "object.shape": "cylinder", "object.diameter_mm": 44.0,
             "object.length_mm": 57.0, "object.mass_g": 60.0, "object.friction": 0.35, "object.max_force_n": 25.0,
             "finger.sensor": "fsr402", "finger.width_mm": 20.0, "finger.length_mm": 52.0, "finger.thickness_mm": 6.0,
             "finger.pad_depth_mm": 5.5, "finger.pad_height_mm": 19.8, "electronics.servo": "sts3215"}


def _robot_summary(p, rev):
    s = p.load_results(rev)["summary"]
    keys = ["dof", "reach_mm", "actuators", "min_torque_margin", "parts", "robot_mass_kg", "pick_place_success",
            "cycle_time_s", "max_payload_g", "electronics_config", "pcb", "build_config", "unit_cost", "assembly_steps"]
    print(f"\n{rev}: " + "  ".join(f"{k}={s.get(k)}" for k in keys))
    for i in s.get("issues", []):
        print(f"  ✗ {i}")


def cmd_robot(a):
    from .robot.pipeline2 import RobotProject

    p = RobotProject(os.path.join(HOME, a.name))
    if a.action == "new":
        if p.revs():
            sys.exit(f"{p.root} already exists")
        source = getattr(a, "source", None) or ("so101" if a.name == "so101" else "")
        design = Design().with_changes({**EGG_ROBOT, "name": a.name, "arm.source": source})
        if a.need:
            from .studio.intent import interpret
            it = interpret(a.need, design)
            design = design.with_changes(it["changes"])
            print(it["reply"])
        json.dump({"kind": "robot", "source": source,
                   "need": a.need or ("Imported open-source SO-101 arm: edit its electronics, sims and build."
                                      if source else "Pick a raw egg and place it without cracking it.")},
                  open(os.path.join(p.root, "meta.json"), "w"))
        rev = p.run(design, label="first design", n_trials=a.trials, fast_sim=a.fast)
        _robot_summary(p, rev)
    elif a.action == "change":
        base = a.base or p.latest()
        changes = dict(kv.split("=", 1) for kv in a.changes)
        rev = p.run(p.load_design(base).with_changes(changes), parent=base, label=" ".join(a.changes)[:48],
                    n_trials=a.trials, fast_sim=a.fast)
        _robot_summary(p, rev)
    elif a.action == "show":
        for r in ([a.base] if a.base else p.revs()):
            _robot_summary(p, r)


def cmd_source(a):
    p = _project(a.name)
    rev = a.rev or p.latest()
    print(f"asking Darwin for agents that can supply / build {a.name} {rev} ...")
    rep = darwin.source(p, rev)
    for r in rep["rows"]:
        top = r["agents"][0] if r["agents"] else None
        tag = " (cached)" if r["cached"] else ""
        print(f"\n[{r['kind']}] {r['item']}{tag}")
        if top:
            show = r["picked"] or r["agents"][:1]
            for ag in show:
                mark = "*" if ag in r["picked"] else " "
                print(f" {mark}{ag['name']:<40} startable={str(ag['can_start']):<5}  {ag['url']}")
            if len(r["agents"]) > len(show):
                print(f"   (+{len(r['agents']) - len(show)} other agents in the JSON)")
        print(f"  gap: {r['gap']}" if r["gap"] else ("" if r["readiness_verified"] else
              "  found; readiness checked only after sign-in (Darwin Act)"), end="")
    print(f"\n\n{len(rep['gaps'])} gap(s) of {len(rep['rows'])} lookups -> {p.rev_dir(rev)}/sourcing_darwin.json")
    print("* = an agent we route this work to (darwin.PREFERRED)")
    print(rep["note"])


def cmd_darwin(a):
    print(json.dumps(darwin.search(a.query, a.n), indent=2))


def cmd_ask(a):
    rows = keewano.query(a.sql)
    print("Keewano not configured (KEEWANO_PAT, KEEWANO_PROJECT_ID)" if rows is None
          else json.dumps(rows, indent=2, default=str))


def cmd_kicad(a):
    if not os.path.isdir(os.path.join(HOME, a.name)):
        sys.exit(f"no project {a.name} in {HOME}")
    p = _project(a.name)
    rev = a.rev or p.latest()
    if not rev or rev not in p.revs():
        sys.exit(f"{a.name} has no revision {rev or '(none yet)'}")
    src = os.path.join(p.rev_dir(rev), "electronics", "board", "board.json")
    if not os.path.exists(src):
        sys.exit(f"{a.name} {rev} has no carrier board (Morph builds one for sensor fingers, finger.sensor=fsr402)")
    with open(src) as fh:
        brd = json.load(fh)
    results = p.load_results(rev)
    design, summary = p.load_design(rev).to_dict(), results.get("summary") or {}
    o = design.get("object") or {}
    about = {"Morph project": f"{a.name} {rev}", "Object": o.get("name"), "Servo": summary.get("servo"),
             "Grip target": f"{summary['target_force_n']} N" if summary.get("target_force_n") else None}
    refs = boardrepo.related(boardrepo.keys_for(design, summary, robot="sizing" in results))
    name = f"{os.path.basename(os.path.normpath(p.root))}-{rev}"
    out = os.path.join(p.rev_dir(rev), "boardrepo", name)
    kc = kicad.export(brd, out, name, about, boardrepo.notes_md(refs))
    chk = kicad.check(out, name)
    zpath = os.path.join(out, kc["zip"])
    print(f"KiCad project: {out}/{name}.kicad_pro (+ .kicad_pcb, .kicad_sch, README.md)")
    if chk is None:
        print("kicad-cli not found: open the project in KiCad 8/9 to run DRC")
    elif "error" in chk:
        print(f"kicad-cli failed: {chk['error']}")
    else:
        print(f"KiCad {chk['kicad']}: DRC {chk['violations']} violations, {chk['unconnected']} unconnected, "
              f"{chk['parity']} schematic-parity issues; ERC {chk['erc_errors']} errors")
    if refs:
        print("\nrelated open designs (in the README):")
        for r in refs:
            print(f"  {boardrepo.page(r['board'])}  ({r['license'] or 'no licence recognised'})")
    print(f"\npublish on BoardRepo: {zpath}")
    print("\n".join("  " + s for s in boardrepo.publish_steps(zpath)))
    p.log({"type": "board_export", "rev": rev, "zip": os.path.relpath(zpath, p.root).replace(os.sep, "/"),
           "kicad_check": chk})


def cmd_boardrepo(a):
    if a.board:
        print(json.dumps(boardrepo.fetch(a.board), indent=2))
        return
    for r in boardrepo.related({k for ref in boardrepo.REFERENCES for k in ref["for"]}, live=True):
        info = r["live"]
        state = "cached" if info.get("cached") else ("live" if info.get("ok") else f"offline ({info.get('error')})")
        lic = info.get("license") if info.get("ok") else r["license"]   # the snapshot only when offline, no cache
        print(f"{r['board']}  [{lic or 'no licence recognised'}; {state}]")
        print(f"  {r['why']}")
        print(f"  for: {', '.join(r['for'])}  views: {', '.join(info.get('contents') or []) or '?'}  {r.get('live', {}).get('url')}")


def cmd_research(a):
    print(agent.research(a.query))


def cmd_gif(a):
    from . import design as cad, sim

    p = _project(a.name)
    d = p.load_design(a.rev)
    out = os.path.join(p.rev_dir(a.rev), "sim", "grasp.gif")
    if sim.render_gif(d, cad.build_profile(d), out):
        print(out)


def _timed(name, cmd, fn, ns):
    t0 = time.time()
    fn(ns)
    _project(name).log({"type": "tool_time", "cmd": cmd, "seconds": round(time.time() - t0, 2)})


def cmd_demo(a):
    need = a.need
    name = a.name
    p = _project(name)
    if p.revs():
        sys.exit(f"{p.root} exists; pick another --name or delete it")
    print(f"== 1. need -> spec\n{need}")
    a2 = argparse.Namespace(need=need, spec=None, name=name, trials=a.trials)
    _timed(name, "new", cmd_new, a2)
    print("\n== 2. explore pad styles and section sizes")
    _timed(name, "explore", cmd_explore, argparse.Namespace(name=name, base="r001", trials=a.trials))
    best = [e for e in p.entries() if e["type"] == "exploration"][-1]["recommended"]
    print(f"\n== 3. what if we use a smaller SG90 servo? (only electronics + BOM should rerun)")
    _timed(name, "change", cmd_change, argparse.Namespace(name=name, base=best, changes=["electronics.servo=sg90"],
                                                          label="SG90 servo", trials=a.trials))
    print(f"\n== 4. what if the object is a strawberry-soft 3 N limit? (only electronics + BOM rerun)")
    _timed(name, "change", cmd_change, argparse.Namespace(name=name, base=best, changes=["object.max_force_n=3"],
                                                          label="3 N damage limit", trials=a.trials))
    print(f"\n== 5. bench rehearsal (SIMULATED trace; use --port on real hardware)")
    from . import bench
    bench.run(p, best, None, 6.0, simulate=True)
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

    s = sub.add_parser("bench", help="run the force-limited grip on real hardware and log the trace")
    s.add_argument("name")
    s.add_argument("rev")
    s.add_argument("--port", help="serial port of the board running morph_grip.ino")
    s.add_argument("--seconds", type=float, default=8.0)
    s.add_argument("--simulate", action="store_true", help="synthetic trace for rehearsal (labelled SIMULATED)")
    s.set_defaults(fn=cmd_bench)

    s = sub.add_parser("clock", help="log human minutes: spec | review | test | corrections")
    s.add_argument("name")
    s.add_argument("step", choices=["spec", "review", "test", "corrections"])
    s.add_argument("minutes", type=float)
    s.add_argument("--note")
    s.set_defaults(fn=cmd_clock)

    s = sub.add_parser("time", help="before/after time study for the project")
    s.add_argument("name")
    s.add_argument("rev", nargs="?", help="revision that was fabricated")
    s.set_defaults(fn=cmd_time)

    s = sub.add_parser("robot", help="whole-robot projects: new | change | show")
    s.add_argument("action", choices=["new", "change", "show"])
    s.add_argument("name", nargs="?", default="egg-picker")
    s.add_argument("changes", nargs="*", help="dotted.key=value (change)")
    s.add_argument("--need", help="natural-language request applied to the default robot (new)")
    s.add_argument("--source", help="start from an imported open-source robot in robots/<source>/ (e.g. so101)")
    s.add_argument("--from", dest="base")
    s.add_argument("--trials", type=int, default=4)
    s.add_argument("--fast", action="store_true", help="shorter simulations")
    s.set_defaults(fn=cmd_robot)

    s = sub.add_parser("studio", help="web UI: chat with / edit the robot, watch everything regenerate")
    s.add_argument("name", nargs="?", default="egg-picker")
    s.add_argument("--open", action="store_true", help="open the browser when the server is up")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.set_defaults(fn=cmd_studio)

    s = sub.add_parser("source", help="Darwin: shortlist agents that sell each part / build the PCB and prints")
    s.add_argument("name")
    s.add_argument("rev", nargs="?")
    s.set_defaults(fn=cmd_source)

    s = sub.add_parser("darwin", help="one Darwin Search query")
    s.add_argument("query")
    s.add_argument("-n", type=int, default=5)
    s.set_defaults(fn=cmd_darwin)

    s = sub.add_parser("ask", help="run SQL against the project's Keewano events")
    s.add_argument("sql")
    s.set_defaults(fn=cmd_ask)

    s = sub.add_parser("kicad", help="KiCad project of the carrier board + BoardRepo-ready zip (DRC with kicad-cli if present)")
    s.add_argument("name")
    s.add_argument("rev", nargs="?")
    s.set_defaults(fn=cmd_kicad)

    s = sub.add_parser("boardrepo", help="open reference boards on BoardRepo (live licence/views), or one board's page")
    s.add_argument("board", nargs="?", help="owner/slug, e.g. evroc/robotic-gripper")
    s.set_defaults(fn=cmd_boardrepo)

    s = sub.add_parser("research", help="web research via Claude (components, tooling, suppliers)")
    s.add_argument("query")
    s.set_defaults(fn=cmd_research)

    s = sub.add_parser("gif", help="render the grasp to a GIF (needs MUJOCO_GL=egl/osmesa)")
    s.add_argument("name")
    s.add_argument("rev")
    s.set_defaults(fn=cmd_gif)

    s = sub.add_parser("demo", help="scripted end-to-end run")
    s.add_argument("--name", default="raw-egg")
    s.add_argument("--need", default="Our robot needs to pick up a raw egg (44 mm wide, 57 mm long, 60 g) lying on "
                                      "its side without cracking it, using our MG996R gripper. Print the fingers "
                                      "in PETG today.")
    s.add_argument("--trials", type=int, default=8)
    s.set_defaults(fn=cmd_demo)

    a = ap.parse_args(argv)
    t0 = time.time()
    a.fn(a)
    # every pipeline command's wall-clock goes into the time study
    if a.cmd in ("new", "explore", "change") and getattr(a, "name", None):
        _project(a.name).log({"type": "tool_time", "cmd": a.cmd, "seconds": round(time.time() - t0, 2)})


if __name__ == "__main__":
    main()
