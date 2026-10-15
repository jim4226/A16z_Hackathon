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

from . import agent, boardrepo, darwin, keewano, kicad, report, resources
from .explore import explore
from .pipeline import Project
from .spec import Design
from .studio.projects import EGG_ROBOT  # noqa: F401  (the default arm; tests and tools import it from here)

HOME = resources.projects_home()  # MORPH_HOME, else projects/ in the Morph folder (a wheel: the user's data folder)


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
        print(f"  x {issue}")


def _report(p: Project, revs, name="report.html") -> str:
    path = os.path.join(p.root, name)
    report.build(p, revs, path)
    print(f"\nreport: {path}")
    return path


def cmd_new(a):
    if a.spec:
        design, src = Design.from_dict(json.load(open(a.spec, encoding="utf-8"))), "spec file"
        rationale = ""
    else:
        design, need, src = agent.propose(a.need)
        rationale = need.rationale
        print(f"spec from {src}: {need.model_dump_json(indent=1)}")
    name = a.name = a.name or design.name
    p = _project(name)
    if p.revs():
        sys.exit(f"project {name} already exists at {p.root}")
    json.dump({"need": a.need, "source": src, "rationale": rationale}, open(os.path.join(p.root, "meta.json"), "w", encoding="utf-8"))
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


def _set_aside_cut_off(proj):
    """A build cut off mid-way (window closed, Ctrl+C) leaves rNNN/design.json without results.json,
    which would stop every later launch. Move it aside; the next build reuses the number."""
    for rev in reversed(proj.revs()):
        d = proj.rev_dir(rev)
        if os.path.isfile(os.path.join(d, "results.json")):
            break
        print(f"{os.path.basename(proj.root)}: {rev} was cut off mid-build; setting it aside")
        try:
            os.rename(d, f"{d}.incomplete-{int(time.time())}")
        except OSError:
            break


COMMENT_NOTE = "Ignoring the rest of the line from # on: it is a comment, not options for Morph."


def _studio_argv(argv: list) -> list:
    """`morph studio` options as a launcher passes them on. Launchers from before v0.2 pass everything they were given,
    a comment pasted along with the command included (Command Prompt and zsh have no # comments for them): a word
    starting with # ends the options, as in today's launchers."""
    if argv[:1] != ["studio"]:
        return argv
    for i, word in enumerate(argv):
        if word.startswith("#"):
            print(COMMENT_NOTE)
            return argv[:i]
    return argv


def cmd_studio(a):
    from .robot.pipeline2 import RobotProject
    from .studio.server import access_token, is_loopback, serve

    import re
    import socket
    if getattr(a, "update", False):  # the launchers' own option, which a launcher from before v0.2 passes on
        launcher = r".\run_studio.bat" if sys.platform == "win32" else "./run_studio.sh"
        print(f"--update is an option of the launcher: {launcher} --update switches this clone to the latest release, "
              "then starts it. Morph ignores it here (a launcher from before v0.2 passes it on once, after its last "
              "git pull) and starts the version that is here.")
    if getattr(a, "remote", False) and is_loopback(a.host):
        a.host = "0.0.0.0"  # bring your own compute: other computers may use this Studio, with its access link
    team = bool(getattr(a, "team", False))
    if team:  # Morph Teams: members sign in with invite and sign-in links; MORPH_TOKEN plays no part
        from .studio.teams.store import exists
        if not exists(HOME):
            from .resources import morph_command
            sys.exit(f"There is no team here yet. Make it first, in a terminal in the Morph folder: {morph_command()} "
                     'team init --owner "your name" (docs/TEAMS.md), then start the Studio with --team again.')
    fixed = "" if team else os.environ.get("MORPH_TOKEN", "").strip()
    if fixed and not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", fixed):
        sys.exit("MORPH_TOKEN must be 8 to 128 letters, digits, - or _ (it goes in the access link).")
    from .studio.security import project_name
    try:
        project_name(a.name)
    except ValueError as exc:
        sys.exit(f"{a.name}: {exc}")
    token = None if team else access_token(a.host, fixed or None)
    local = "127.0.0.1" if a.host in ("0.0.0.0", "") else a.host
    link = f"http://{local}:{a.port}/" + (f"?token={token}" if token else "")
    with socket.socket() as sock:  # a Studio left running in another window would share the port on Windows
        sock.settimeout(1)
        if sock.connect_ex((local, a.port)) == 0:
            if a.open:
                open_app_window(f"http://{local}:{a.port}")
            sys.exit(f"Morph Studio is already running at http://{local}:{a.port}. To restart it, stop it first "
                     "(Ctrl+C in its terminal, or close that window).")
    if os.environ.get("MUJOCO_GL") is None and sys.platform.startswith("linux"):
        os.environ["MUJOCO_GL"] = "egl"
    meta = os.path.join(HOME, a.name, "meta.json")
    if os.path.isfile(meta):
        try:
            with open(meta, encoding="utf-8") as fh:
                kind = json.load(fh).get("kind")
        except (OSError, ValueError, AttributeError):
            kind = "robot"  # unreadable: let the robot pipeline report what is wrong
        if kind != "robot":
            sys.exit(f"{a.name} is a gripper-only project from the earlier CLI prototype; open "
                     f"{os.path.join(HOME, a.name, 'report.html')} for it. Morph Studio opens whole robots: "
                     "run_studio.bat / ./run_studio.sh, or `python -m morph.cli studio egg-picker --open`.")
    from . import keys
    from .studio import memory, projects
    memory.limit_arenas()  # before the start-up builds below: they share two malloc arenas, as the Studio's builds do
    if keys.ensure_env_file():
        print("Created .env for your API keys (optional): use the Keys button in the Studio, or edit .env.")
    projects.backfill_demo(HOME)  # robots this launcher made before 0.2: Reset demo touches only demo projects
    for nm in (a.name, "so101" if a.name != "so101" else "egg-picker"):
        _set_aside_cut_off(RobotProject(os.path.join(HOME, nm)))
    p = RobotProject(os.path.join(HOME, a.name))
    if not p.revs():
        print(f"no project {a.name} yet: generating the first robot (about a minute)...")
        cmd_robot(argparse.Namespace(action="new", name=a.name, need=None, trials=4, fast=False, base=None, changes=[],
                                     source="so101" if a.name == "so101" else None, demo=a.name in projects.DEMOS))
    other = "so101" if a.name != "so101" else "egg-picker"
    if not RobotProject(os.path.join(HOME, other)).revs():
        print(f"also preparing the {other} project for the project switcher...")
        cmd_robot(argparse.Namespace(action="new", name=other, need=None, trials=4, fast=False, base=None, changes=[],
                                     source="so101" if other == "so101" else None, demo=True))
    from .robot.pipeline2 import pipeline_version
    for proj in (p, RobotProject(os.path.join(HOME, other))):
        last = proj.latest()
        if last and proj.load_results(last).get("pipeline_version") != pipeline_version(proj.load_design(last).arm.source):
            print(f"{os.path.basename(proj.root)}: rebuilding with the latest Morph code (about a minute)...")
            proj.run(proj.load_design(last), parent=None, label="rebuilt with latest Morph", verbose=False)
    if team:  # a fresh one-time sign-in link for the owner (made now: it lasts 15 minutes)
        from .studio.server import lan_address
        from .studio.teams import cli as teams_cli
        # with --remote, at the address teammates reach: the invite links Share makes start with the page's address
        lan = lan_address() if a.host in ("0.0.0.0", "") else None
        link = teams_cli.team_link(home=HOME, base_url=f"http://{lan or local}:{a.port}", out=None)["url"]
    headless = sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    if a.open and not headless:  # a server without a screen: the link printed below is opened from another computer
        import threading
        threading.Timer(1.5, open_app_window, args=(link,)).start()
    extra = {**({"team": True, "link": link} if team else {}),
             **({"allow_url_import": True} if getattr(a, "allow_url_import", False) else {})}
    print(f"Projects folder: {os.path.abspath(HOME)}{resources.projects_home_note(HOME)}")
    memory.trim()  # what the start-up builds freed goes back to the system before the Studio waits for work
    serve(p, a.host, a.port, home=HOME, token=token, **extra)




def _robot_summary(p, rev) -> list:
    """Print a revision's summary and issues; returns the stages that did not complete."""
    from .robot.pipeline2 import ORDER, stage_problem

    res = p.load_results(rev)
    s = res["summary"]
    keys = ["dof", "reach_mm", "actuators", "min_torque_margin", "parts", "robot_mass_kg", "pick_place_success",
            "cycle_time_s", "max_payload_g", "electronics_config", "pcb", "build_config", "unit_cost", "assembly_steps"]
    print(f"\n{rev}: " + "  ".join(f"{k}={s.get(k)}" for k in keys))
    for i in s.get("issues", []):
        print(f"  x {i}")
    return [n for n in ORDER if stage_problem(res, n)]


def _checked(design, changes: dict):
    """`design` with `changes`, each changed value inside the design limits (security.design_values, as in the
    Studio); SystemExit with the reason otherwise, before anything is built: one absurd value (a kilometre-wide
    object) would hold the computer for hours."""
    from .studio.projects import DEFAULTS
    from .studio.security import design_values

    for key, value in changes.items():
        try:
            design = design.with_changes({key: value})
        except KeyError:
            sys.exit(f"Couldn't apply that: unknown parameter: {key}")
        except (TypeError, ValueError):
            sys.exit(f"Couldn't apply that: {key} can't be {value!r}")
    try:
        design_values(design.flat(), DEFAULTS, keys=changes)
    except ValueError as exc:
        sys.exit(f"Couldn't apply that: {exc}")
    return design


def _one_liner(a) -> bool:
    """`morph robot new "help me sort socks"`: a request in words (it has a space, or --defaults or --name came with
    it) goes through the New robot brief; a bare name keeps building the default arm under that name."""
    return a.action == "new" and (any(c.isspace() for c in a.name or "") or bool(getattr(a, "defaults", False))
                                  or bool(getattr(a, "project_name", None)))


def _ask(prompt: str) -> str | None:
    try:
        return input(prompt).strip()
    except EOFError:
        return None


def _brief_questions(b) -> dict | None:
    """Ask the brief's open questions in the terminal: {slot: chip value}, {} for defaults, None to stop."""
    answers = {}
    for i, q in enumerate(b.questions, 1):
        print(f"\n{i}. {q['text']}" + ("" if q.get("required") else " (optional)"))
        if q.get("why"):
            print(f"   {q['why']}")
        default = next((n for n, c in enumerate(q["chips"], 1) if c.get("default")), 1)
        for n, c in enumerate(q["chips"], 1):
            print(f"   {n}) {c['label']}" + ("  [default]" if n == default else "") + (f"  ({c['note']})" if c.get("note") else ""))
        while True:
            got = _ask(f"   Choose 1-{len(q['chips'])}, Enter for the default, d for defaults everywhere, q to stop: ")
            if got is None or got.lower() == "q":
                return None
            if got.lower() == "d":
                return {}
            if got == "" or (got.isdigit() and 1 <= int(got) <= len(q["chips"])):
                answers[q["slot"]] = q["chips"][(int(got) if got else default) - 1]["value"]
                break
    return answers


def cmd_robot_brief(a) -> int:
    """`morph robot new "<one-liner>"`: the New robot conversation in a terminal (questions as numbered choices; with
    --defaults, or without a terminal, the sensible defaults), then the brief is checked, the project made through the
    project service (origin "brief") and the whole robot built. Exit status 1 when nothing was built or stages failed."""
    from . import keewano, settings
    from .robot.pipeline2 import ORDER, RobotProject
    from .spec import Design
    from .studio import agents, capability, projects
    from .studio import brief as B

    text = " ".join(a.name.split())
    if a.need or getattr(a, "source", None):
        sys.exit("Describe the robot in the one quoted request, or give a name with --need/--source; not both.")
    name = getattr(a, "project_name", None)
    if name and not B.NAME_RE.match(name):
        sys.exit(f"--name {name}: use lower-case letters, digits and dashes (up to 40).")
    if len(text) > 2000:
        sys.exit("Keep the request under 2,000 characters.")
    asking = not a.defaults and sys.stdin.isatty() and sys.stdout.isatty()

    def timeline(ev):  # the agents timeline, as the Studio's dialog shows it
        if ev.get("type") == "agent" and ev.get("status") != "start":
            prov = {"subq": "SubQ Snap (AI)", "cache": "SubQ, cached", "claude": "Claude", "rules": "built-in rules",
                    "code": "Morph's code"}.get(ev.get("provider"), ev.get("provider"))
            print(f"  {ev['role']:<11} {prov}: {ev.get('text', '')}")

    print(f"New robot: {text}")
    b = agents.run_turn(text=text, on_event=timeline, home=HOME)
    while b.status in ("asking", "out_of_scope"):
        if b.status == "out_of_scope":
            for n in (b.scope or {}).get("notes") or []:
                print(f"  out of scope: {n}")
            near = (b.scope or {}).get("nearest")
            if not near:
                return 1
            print(f"  nearest robot Morph can build: {near.get('summary')}")
            if not (a.nearest or (asking and (_ask("Build that instead? [y/N] ") or "").lower().startswith("y"))):
                print("Nothing built." + ("" if asking else " Add --nearest to build the nearest robot."))
                return 1
            b = agents.run_turn(b, action="nearest", on_event=timeline, home=HOME)
            continue
        answers = _brief_questions(b) if asking else {}
        if answers is None:
            print("Stopped; nothing was built.")
            return 1
        if not asking:
            n = len(b.questions)
            print(f"  defaults    {n} open question{'s' if n != 1 else ''} ({', '.join(q['slot'] for q in b.questions)}) take "
                  "Morph's default answers" + ("" if a.defaults else " (no terminal to ask in)"))
        b = agents.run_turn(b, answers=answers or None, action=None if answers else "defaults", on_event=timeline, home=HOME)
    if b.status == "refused":
        print("Refused: " + ((b.scope or {}).get("notes") or ["Morph doesn't design that."])[0])
        return 1
    if b.status != "ready":
        print(f"The brief isn't ready to build ({b.status}).")
        return 1
    pay = B.build_payload(b, name, HOME)
    pre = b.preflight or {}
    print(f"\n{b.summary or b.request}")
    for x in b.assumptions:
        if x.get("text"):
            print(f"  - {x['text']}")
    for issue in pre.get("issues") or []:
        print(f"  ! preflight: {issue}")
    if b.estimate:
        print(f"  parts cost: {b.estimate['label']}")
    if asking and (_ask(f"Build {pay['name']} now? [Y/n] ") or "y").lower().startswith("n"):
        print("Nothing built.")
        return 1
    changes, notes = capability.clamp(pay["changes"], allow=("name", "arm.source", "arm.actuator_overrides"))
    for n in notes:
        print(f"  adjusted: {n}")
    try:
        info = projects.create(HOME, pay["name"], design=Design().with_changes(changes), need=pay["meta"]["need"],
                               origin="brief", meta={"brief_id": b.id}, unique=not name)
    except FileExistsError:
        sys.exit(f"{os.path.join(HOME, name)} already exists: choose another --name.")
    except ValueError as exc:
        sys.exit(f"Couldn't create the project: {exc}")
    p = RobotProject(info["root"])
    print(f"\nBuilding {info['name']} (all nine stages, from scratch):")
    rev = p.run(info["design"], label="first design", n_trials=a.trials, fast_sim=a.fast)
    B.mark_built(b, info["name"], rev)
    B.write_final(b, info["root"])
    failed = _robot_summary(p, rev)
    if settings.telemetry_enabled() and os.environ.get("KEEWANO_API_KEY"):  # opt-in, as in the Studio: categories only
        try:
            summary = p.load_results(rev).get("summary")
        except (OSError, ValueError):
            summary = None
        keewano.send(info["name"], agents.memory_events(b, "ready") + agents.memory_events(b, "built", summary))
    if failed:
        print(f"\n{len(failed)} of {len(ORDER)} stages did not complete: {', '.join(failed)} (details above)")
    print(f"\nOpen it: morph studio {info['name']}   Suppliers: morph source {info['name']}   "
          f"Export: morph export {info['name']} -o {info['name']}.zip")
    return 1 if failed else 0


def cmd_robot(a) -> int:
    """Exit status: 1 when the new revision has stages that did not complete (show never fails)."""
    from .robot.pipeline2 import ORDER, RobotProject
    from .studio import projects
    from .studio.security import project_name

    if _one_liner(a):
        return cmd_robot_brief(a)
    root = os.path.join(HOME, a.name)
    source = getattr(a, "source", None) or ("so101" if a.name == "so101" else "")
    failed = []
    if a.action == "new":  # the name, the source and what --need asks for are checked before anything is created
        design = None
        if a.need:
            from .studio.intent import interpret
            try:
                design = projects.template(project_name(a.name), source)
            except ValueError as exc:
                sys.exit(f"{a.name}: {exc}")
            it = interpret(a.need, design)
            design = _checked(design, {k: v for k, v in it["changes"].items() if k not in ("name", "arm.source")})
            print(it["reply"])
        try:
            info = projects.create(HOME, a.name, source=source, design=design, need=a.need, demo=getattr(a, "demo", False),
                                   unique=False)
        except ValueError as exc:
            sys.exit(f"{a.name}: {exc}")
        except FileExistsError:
            sys.exit(f"{root} already exists")
        p, design = RobotProject(info["root"]), info["design"]
        failed = _robot_summary(p, p.run(design, label="first design", n_trials=a.trials, fast_sim=a.fast))
    else:
        if not os.path.isdir(root):
            sys.exit(f"no project {a.name} in {os.path.abspath(HOME)}")
        p = RobotProject(root)
        if a.action == "change":
            base = a.base or p.latest()
            if base not in p.revs():
                sys.exit(f"{a.name} has no revision {base}" if base else f"{a.name} has no revisions yet")
            bad = [kv for kv in a.changes if "=" not in kv]
            if bad or not a.changes:
                sys.exit("Give each change as dotted.key=value, for example arm.dof=6" + (f" (not {bad[0]})" if bad else "."))
            design = _checked(p.load_design(base), dict(kv.split("=", 1) for kv in a.changes))
            rev = p.run(design, parent=base, label=" ".join(a.changes)[:48], n_trials=a.trials, fast_sim=a.fast)
            failed = _robot_summary(p, rev)
        elif a.action == "show":
            for r in ([a.base] if a.base else p.revs()):
                _robot_summary(p, r)
    if failed:
        print(f"\n{len(failed)} of {len(ORDER)} stages did not complete: {', '.join(failed)} (details above)")
    return 1 if failed else 0


def _robot_project(name: str):
    """The robot project `name` in HOME, or SystemExit saying why (checked before anything is read)."""
    from .robot.pipeline2 import RobotProject
    from .studio.security import project_name

    try:
        project_name(name)
    except ValueError as exc:
        sys.exit(f"{name}: {exc}")
    root = os.path.join(HOME, name)
    if not os.path.isdir(root):
        sys.exit(f"no project {name} in {os.path.abspath(HOME)}")
    return RobotProject(root)


def cmd_export(a) -> int:
    """`morph export NAME [--rev rNNN] [--full] -o FILE`: what the Studio's Export button downloads. In a team's projects
    folder the export also carries team.json, the team's record (names, never member ids)."""
    import shutil

    from .studio import export

    p = _robot_project(a.name)
    rev = a.rev or (None if a.full else p.latest())
    if not a.full and not rev:
        sys.exit(f"{a.name} has no finished revision to export yet.")
    extra = None
    from .studio.teams import store as team_store
    if team_store.exists(HOME):
        from .studio.teams import collab
        try:
            t = team_store.TeamStore(HOME)
            members = t.read().get("members") or {}
            extra = {"team.json": collab.export_bundle(p.root, members) if a.full
                     else collab.revision_record(p.root, rev, members, t.policy())}
        except collab.CollabError as exc:
            print(f"note: the team's record is left out ({exc})")
    try:
        archive, filename = export.build_export(p, rev, full=a.full, extra=extra)
    except export.ExportError as exc:
        sys.exit(f"{a.name}: {exc}")
    out = a.out or filename
    if os.path.isdir(out):
        out = os.path.join(out, filename)
    with archive, open(out, "wb") as fh:
        shutil.copyfileobj(archive, fh, 1 << 16)
    import zipfile
    with zipfile.ZipFile(out) as z:
        manifest = json.loads(z.read("export-manifest.json"))
    what = f"{a.name} history" + (f" with {rev}'s files" if rev else "") if a.full else f"{a.name} {rev}"
    print(f"{out}: {what}, {len(manifest.get('files') or [])} files, {os.path.getsize(out) / 1e6:.1f} MB")
    if manifest.get("left_out"):
        print(f"  left out: {len(manifest['left_out'])} order files ({manifest.get('left_out_note')})")
    print(f"  {manifest.get('note')}")
    return 0


def cmd_world(a) -> int:
    """`morph world NAME [--starter desk|counter|workbench | --off]`: World mode (beta) from a terminal. Without an
    option it shows the project's room and how the newest simulation went in it; --starter saves a built-in room and
    reruns the simulation in it; --off simulates on the plain table again. Rooms of your own: World in the Studio."""
    from .studio import worlds

    p = _robot_project(a.name)
    base = p.latest()
    if not base:
        sys.exit(f"{a.name} has no finished revision yet.")
    design = p.load_design(base)
    if a.starter or a.off:
        if a.off:
            if not design.world.id:
                print(f"{a.name} {base} already simulates on the plain table.")
                return 0
            changes, label, w = {"world.id": "", "world.sha": ""}, "world: off", None
        else:
            try:
                w, sha = worlds.save(p.root, worlds.starter(a.starter))
            except worlds.WorldError as exc:
                sys.exit(str(exc))
            if design.world.id == w["id"] and design.world.sha == sha:
                print(f"{a.name} {base} already simulates in {w['name']}.")
                return 0
            changes, label = {"world.id": w["id"], "world.sha": sha}, f"world: {w['name']}"
        design = _checked(design, changes)
        p.log(worlds.ledger_entry(w))  # the kind of room and its counts (Keewano, when its events are on)
        rev = p.run(design, parent=base, label=label, n_trials=a.trials, fast_sim=a.fast)
        base, design = rev, p.load_design(rev)
    try:
        w = worlds.load(p.root)
    except worlds.WorldError as exc:
        w = None
        print(f"world/world.json can't be read: {exc}")
    if w:
        print(f"room: {w['name']} ({w['source'].get('kind')}), {len(w.get('obstacles') or [])} grey boxes, "
              f"{len(w.get('zones') or [])} people areas")
    pp = ((((p.load_results(base).get("sims") or {}).get("scenarios") or {}).get("pick_place") or {}).get("metrics") or {})
    if not design.world.id:
        print(f"{a.name} {base} simulates on the plain table" + (" (World mode is off)." if w else "."))
        return 0
    clear = pp.get("min_clearance_mm")
    print(f"{a.name} {base} simulates in {design.world.id} ({design.world.sha}): pick and place "
          f"{'succeeds' if pp.get('success') else 'fails'}"
          + (f", closest {clear:.0f} mm to a box" if isinstance(clear, (int, float)) else "")
          + (f", {len(pp['world_contacts'])} contacts" if pp.get("world_contacts") else ""))
    print("  " + worlds.NOTES["world"])
    return 0 if pp.get("success") else 1


def cmd_study(a) -> int:
    """`morph study NAME [--rev rNNN] [--runs N] [--workers N] [--stress] [--seed S]`: a robustness study (beta) of a
    revision's pick and place, run here and printed as text. Ctrl+C stops it and keeps the finished runs; the same
    command again carries on from them (a larger --runs deepens a finished study)."""
    import threading

    from .services import robust

    p = _robot_project(a.name)
    cores = robust.cores()
    if a.workers is not None and not 1 <= a.workers <= cores:
        print(f"{a.name}: use 1 to {cores} worker processes (this computer has {cores} CPU core{'s' if cores != 1 else ''}).",
              file=sys.stderr)
        return 1
    cancel, out, last = threading.Event(), {}, {"t": 0.0, "phase": None}

    def progress(ev):
        now = time.time()
        if ev["phase"] != last["phase"] or now - last["t"] >= 15 or ev["status"] in ("done", "cancelled"):
            last.update(t=now, phase=ev["phase"])
            rate = f", {_pct(ev['p'])} succeed so far" if ev.get("p") is not None else ""
            eta = f", about {ev['eta_s']:.0f} s left" if ev.get("eta_s") else ""
            what = {"prepare": "rebuilding the robot and its plan", "runs": "runs", "checks": "checks with other simulator settings",
                    "done": "done"}.get(ev["phase"], ev["phase"])
            print(f"  {ev['elapsed_s']:6.1f} s  {what}: {ev['done']} of {ev['n']}{rate}{eta}", flush=True)

    def work():
        try:
            out["doc"] = robust.run_study(p.root, a.rev, runs=a.runs, seed=a.seed, stress=a.stress, workers=a.workers,
                                          timestep=a.timestep / 1000.0, checks=not a.no_checks, cancel=cancel,
                                          progress=progress)
        except robust.StudyError as exc:
            out["error"] = str(exc)
        except Exception as exc:  # noqa: BLE001
            out["error"] = f"{exc.__class__.__name__}: {exc}"

    n = a.workers or robust.default_workers()
    print(f"{a.name}: robustness study (beta), {a.runs} randomized run{'s' if a.runs != 1 else ''} on {n} worker "
          f"process{'es' if n != 1 else ''}. "
          "Ctrl+C stops it and keeps the finished runs.")
    t = threading.Thread(target=work, daemon=True, name="morph-study")
    t.start()
    try:
        while t.is_alive():
            t.join(0.2)
    except KeyboardInterrupt:
        print("Stopping; finished runs are kept (run the same command again to carry on).", flush=True)
        cancel.set()
        t.join()
    if "error" in out:
        print(f"{a.name}: {out['error']}", file=sys.stderr)
        return 1
    doc = out["doc"]
    print(robust.report(doc))
    print(f"Saved in {os.path.join(p.root, robust.STUDIES, doc['id'])}")
    return 0 if doc["status"] == "done" else 130


def _pct(v) -> str:
    return f"{v * 100:.1f}%"


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
    try:
        lic = kicad.license_text(a.license) if a.license else None
    except (OSError, ValueError) as exc:
        sys.exit(str(exc))
    with open(src, encoding="utf-8") as fh:
        brd = json.load(fh)
    results = p.load_results(rev)
    design, summary = p.load_design(rev).to_dict(), results.get("summary") or {}
    o = design.get("object") or {}
    about = {"Morph project": f"{a.name} {rev}", "Object": o.get("name"), "Servo": summary.get("servo"),
             "Grip target": f"{summary['target_force_n']} N" if summary.get("target_force_n") else None}
    refs = boardrepo.related(boardrepo.keys_for(design, summary, robot="sizing" in results))
    name = f"{os.path.basename(os.path.normpath(p.root))}-{rev}"
    out = os.path.join(p.rev_dir(rev), "boardrepo", name)
    kc = kicad.export(brd, out, name, about, boardrepo.notes_md(refs), license=lic)
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
    if not lic:
        print("\nNo licence file in the export: the board is your design. Add one with --license apache-2.0 or "
              "--license <your licence file> (BoardRepo shows a board without a licence as all rights reserved).")
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
    print("\n== 3. what if we use a smaller SG90 servo? (only electronics + BOM should rerun)")
    _timed(name, "change", cmd_change, argparse.Namespace(name=name, base=best, changes=["electronics.servo=sg90"],
                                                          label="SG90 servo", trials=a.trials))
    print("\n== 4. what if the object is a strawberry-soft 3 N limit? (only electronics + BOM rerun)")
    _timed(name, "change", cmd_change, argparse.Namespace(name=name, base=best, changes=["object.max_force_n=3"],
                                                          label="3 N damage limit", trials=a.trials))
    print("\n== 5. bench rehearsal (SIMULATED trace; use --port on real hardware)")
    from . import bench
    bench.run(p, best, None, 6.0, simulate=True)
    revs = p.revs()
    _report(p, ["r001", best] + revs[-2:], "report.html")


def load_env(path=None):
    """KEY=value lines from the repo's .env (git-ignored) into the environment, without overriding
    anything already set (see morph/keys.py: any Windows encoding, comments, quotes)."""
    from . import keys
    return keys.load_env(path)


def _git_sha() -> str | None:
    """The checkout's commit, noting local changes; None outside a git checkout or without git."""
    import shutil
    import subprocess

    from . import resources
    repo, git = resources.checkout(), shutil.which("git")
    if not git or not repo or not os.path.exists(os.path.join(repo, ".git")):
        return None
    try:
        run = lambda *args: subprocess.run([git, "-C", repo, *args], capture_output=True, text=True, timeout=10)  # noqa: E731
        head = run("rev-parse", "--short=12", "HEAD")
        if head.returncode != 0 or not head.stdout.strip():
            return None
        dirty = run("status", "--porcelain", "--untracked-files=no")
        return head.stdout.strip() + (" with local changes" if dirty.returncode == 0 and dirty.stdout.strip() else "")
    except (OSError, subprocess.SubprocessError):
        return None


def cmd_doctor(a):
    """Check that this machine can run Morph Studio, and say exactly what to fix if not."""
    import importlib
    import platform
    import socket
    from . import keys, resources

    ok_all = True
    checkout = resources.checkout()

    def line(ok, text, fix=""):
        nonlocal ok_all
        ok_all &= ok in (True, None)
        mark = {True: "  OK  ", False: " FIX  ", None: " info "}[ok]
        print(f"[{mark}] {text}" + (f"\n         -> {fix}" if fix and ok is False else ""))

    from . import __version__
    print(f"Morph doctor ({platform.system()} {platform.release()}, {platform.machine()})\n")
    sha = _git_sha()
    line(None, f"Morph {__version__}" + (f", git {sha}" if sha else " (not a git checkout)"))
    line(None, f"this computer: {socket.gethostname()}, {os.cpu_count()} CPU cores (Morph's pipeline runs here; on a slow "
               "laptop, run it on a stronger computer: README, 'Bring your own compute')")
    v = sys.version_info
    line((3, 10) <= v[:2] <= (3, 14), f"Python {platform.python_version()}",
         "install Python 3.10 to 3.14 from python.org, then run the launcher again")
    for mod in ("numpy", "scipy", "shapely", "manifold3d", "trimesh", "mujoco", "pydantic", "anthropic", "PIL"):
        try:
            m = importlib.import_module(mod)
            line(True, f"{mod} {getattr(m, '__version__', '')}".strip())
        except Exception as exc:  # noqa: BLE001
            line(False, f"{mod} does not import ({exc.__class__.__name__}: {exc})",
                 "delete the .venv folder, then run the launcher again: it reinstalls the packages" if checkout else
                 f"reinstall it: python -m pip install --force-reinstall {'pillow' if mod == 'PIL' else mod}")
    try:
        os.makedirs(HOME, exist_ok=True)
        probe = os.path.join(HOME, ".write-test")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("\u2192 \u2248 ok")  # characters the generators write; cp1252 cannot encode them
        os.remove(probe)
        line(True, f"projects folder {os.path.abspath(HOME)}{resources.projects_home_note(HOME)}: can write it")
    except OSError as exc:
        line(False, f"projects folder {os.path.abspath(HOME)}: cannot write it ({exc})",
             "move the Morph folder out of a read-only or synced folder" if checkout else
             "set MORPH_HOME to a folder you can write")
    if keys.ensure_env_file():
        line(None, f"created {keys.ENV_PATH} from env.example (add your keys there, or with the Keys button in the Studio)")
    for k in keys.status():
        line(None, f"{k['service']}: {k['label']} {'is set' if k['set'] else 'not set (optional)'}"
                   + (" (no tool that is on uses it: remove it in Keys)" if k.get("unused") else ""))
    line(None, "Darwin: no key needed")
    from . import settings
    line(None, f"settings ({settings.path()}): {settings.describe()}"
               + (" (MORPH_OFFLINE=1: network off whatever the settings say)" if settings.offline() else ""))
    _doctor_features(line)
    for name in ("egg-picker", "so101"):
        root = os.path.join(HOME, name)
        from .robot.pipeline2 import RobotProject
        p = RobotProject(root) if os.path.isdir(root) else None
        last = p.latest() if p else None
        if not last:
            line(None, f"{name}: not built yet (starting the Studio builds it, about a minute)")
            continue
        try:
            res = p.load_results(last)
        except (OSError, ValueError):
            line(False, f"{name} {last}: results missing (a build was cut off)", "start the launcher again; it sets cut-off builds aside")
            continue
        bad = {n: st for n, st in (res.get("provenance") or {}).items() if st in ("error", "pending", "blocked", "preview")}
        errs = res.get("errors") or {}
        line(not bad, f"{name} {last}: " + ("all stages built" if not bad else
             "; ".join(f"{n} {st}" + (f" ({errs[n][:120]})" if n in errs else "") for n, st in bad.items())),
             f"send this output to the team; delete {os.path.abspath(HOME)} and start again to rebuild")
    if not getattr(a, "offline", False) and not settings.network_allowed():
        line(None, "network checks skipped: network lookups are off (Keys, Privacy and network)")
    elif not getattr(a, "offline", False):
        try:
            r = darwin.search("PCB fabrication quote from Gerber files", 2, retries=0, timeout=15)
            line(None, "Darwin Search: " + ("reachable" if r["ok"] and not r.get("cached") else
                 f"not reachable right now ({r.get('error')}); the Studio uses its cached answers"))
        except Exception as exc:  # noqa: BLE001
            line(None, f"Darwin Search: check failed ({exc})")
        from .studio import subq
        h = subq.health()
        line(None, "SubQ: api.subq.ai " + ("is reachable" if h["ok"] else f"{h['detail']}; Morph uses its own rules without it"))
        for service, c in keys.check().items():  # each key that is set, tried once (read-only)
            line(c["ok"], f"{service}: {c['detail']}", "replace the key with the Keys button in the Studio, or in .env")
    with socket.socket() as sock:
        sock.settimeout(1)
        busy = sock.connect_ex(("127.0.0.1", 8765)) == 0
    line(None, "port 8765: " + ("in use (a Studio is running, or another app)" if busy else "free"))
    start = ("double-click run_studio.bat (Windows) or run ./run_studio.sh (macOS/Linux)" if checkout
             else "run `morph studio --open`")
    print(f"\nAll good: {start}." if ok_all else "\nFix the lines marked FIX, then run this again.")


def _doctor_features(line):
    """One line per 0.2 feature: whether it loads here, and the optional tools it uses (ffmpeg, xacro, cascadio)."""
    import importlib
    import importlib.util

    def loads(*mods):
        for m in mods:
            try:
                importlib.import_module(m)
            except Exception as exc:  # noqa: BLE001
                return f"{m} does not import ({exc.__class__.__name__}: {exc})"
        return None

    fix = "send this output to the team; reinstall with the launcher (delete .venv first) if packages are broken"
    bad = loads("morph.studio.brief", "morph.studio.agents", "morph.studio.capability", "morph.studio.api_brief")
    line(not bad, "New robot (one-sentence brief): " + (bad or "ready; built-in rules, plus SubQ and Claude when "
                                                                       "their keys are set"), fix)
    bad = loads("morph.importers", "morph.studio.api_import")
    if bad:
        line(False, f"Import: {bad}", fix)
    else:
        from .importers import xacro_sandbox
        from .resources import pip_install_command
        xacro = xacro_sandbox.available()
        cad = importlib.util.find_spec("cascadio") is not None
        line(True, "Import: ready (URDF, MuJoCo XML, STL/OBJ/GLB meshes, parts lists, KiCad boards, Morph exports); "
                   f"xacro {'installed' if xacro else 'not installed (' + pip_install_command('xacro PyYAML') + ')'}; "
                   f"STEP files {'readable (cascadio)' if cad else 'need cascadio (' + pip_install_command('cascadio') + ')'}")
    bad = loads("morph.studio.vision", "morph.studio.api_video", "morph.studio.ffmpeg")
    if bad:
        line(False, f"Show Morph the job: {bad}", fix)
    else:
        from .resources import pip_install_command
        from .studio import ffmpeg
        f = ffmpeg.status()
        line(True, "Show Morph the job: ready; " + (f"ffmpeg {f.get('version') or ''} at {f['path']}".replace("  ", " ")
             if f["available"] else "no ffmpeg (clips the browser plays still work; for others, e.g. iPhone HEVC, "
             f"install ffmpeg, or run {pip_install_command('imageio-ffmpeg')})") +
             (f"; {f['warning']}" if f.get("warning") else ""))
    bad = loads("morph.studio.teams.store", "morph.studio.api_team")
    if bad:
        line(False, f"Teams (preview): {bad}", fix)
    else:
        from .resources import checkout, morph_command
        from .studio.teams import store as team_store
        start = ((r".\run_studio.bat" if os.name == "nt" else "./run_studio.sh") if checkout() else "morph studio") + " --team"
        if team_store.exists(HOME):
            t = team_store.TeamStore(HOME).read()
            n = sum(1 for m in (t.get("members") or {}).values() if not m.get("revoked"))
            line(True, f"Teams (preview): team \"{t.get('name') or 'unnamed'}\" with {n} member{'s' if n != 1 else ''}; "
                       f"start it with `{start}`")
        else:
            line(None, f"Teams (preview): no team here (`{morph_command()} team init --owner \"your name\"`, then "
                       f"`{start}`)")
    bad = loads("morph.studio.worlds", "morph.studio.api_world")
    if bad:
        line(False, f"World mode (beta): {bad}", fix)
    else:
        import mujoco
        ok = hasattr(mujoco, "mj_geomDistance")
        line(ok, f"World mode (beta): {'ready' if ok else 'needs MuJoCo 3.1.6 or newer'} (MuJoCo {mujoco.__version__})",
             "python -m pip install -U mujoco")


SUBQ_ROUTES = {"new_robot": "Design a new robot for a task or an object",
               "edit_current": "Change the open project, e.g. make it cheaper, add a camera, use 6 joints",
               "import_robot": "Bring in an existing robot design: a URDF, CAD files, a repo or a named open-source arm",
               "ask_about_design": "A question about the open project with no change asked",
               "out_of_scope": "Anything else: small talk, or a task that is not about designing a robot"}


def subq_triage(text: str) -> tuple:
    """(state, questions) for `morph subq`: a sample triage of a one-line request, for trying a SubQ key by hand."""
    from .studio import subq

    state = {"request": text, "morph_builds": "One stationary tabletop robot arm with 4 to 6 joints and a two-finger "
             "gripper that picks and places small objects, with its CAD, electronics, firmware, simulations, bill of "
             "materials and assembly manual; or an imported open-source arm (SO-101)."}
    questions = {
        "route": subq.choice("What does the user want Morph to do with `request`?", SUBQ_ROUTES),
        "harm": subq.noul("Is the robot in `request` meant to hurt, threaten, restrain or target people or animals, "
                          "or to work as a weapon?", true="Weapons, launching things at someone, injuring or restraining",
                          false="Handling, sorting, assembly, lab, kitchen, hobby or teaching tasks"),
        "fragility": subq.score("How easily would the object in `request` be damaged by squeezing it?",
                                ["Robust: a can, a tool, a wooden block", "Needs care: a tomato, a light bulb",
                                 "Very fragile: a raw egg, a grape, a strawberry"]),
        "payload": subq.score("How heavy is the heaviest thing the robot must lift?",
                              ["Under 50 g", "50 to 200 g", "200 to 500 g", "Over 500 g"]),
        "says_object": subq.noul("Does `request` name or describe the object the robot must handle?"),
    }
    return state, questions


def cmd_subq(a) -> int:
    """One SubQ Snap triage of a request, printed: for trying a SubQ key (about 1,000 input tokens per run)."""
    from .studio import subq

    state, questions = subq_triage(a.text)
    if a.lint:  # no key: SubQ checks the request's shape before the key
        ok, msg = subq.lint({"model": subq.SNAP, "state": state, "questions": questions})
        print(("valid request: " if ok else "invalid request: ") + msg)
        return 0 if ok else 1
    if not os.environ.get("SUBQ_API_KEY"):
        print("SUBQ_API_KEY is not set: add it with the Keys button in the Studio, or in .env (key from console.subq.ai).")
        return 1
    res = subq.decide(state, questions, timeout=a.timeout, cache=not a.no_cache, purpose="cli")
    if res is None:
        st = subq.status()
        why = st["disabled"] or (st["last_error"] or {}).get("message") or ("paused for a minute after errors" if st["cooling"]
                                                                           else "network lookups are off")
        print(f"SubQ didn't answer: {why}")
        return 1
    if a.json:
        print(json.dumps(res, indent=1))
        return 0
    print(f"{subq.LABEL}, {res['source']}, {res['ms']} ms, {res.get('usage', {}).get('input_tokens', '?')} input tokens")
    for name, q in questions.items():
        ans = res["answers"].get(name) or {}
        if q["type"] == "noul":
            p = subq.p_yes(res, name)
            print(f"  {name}: p(yes) = {p:.2f}" if p is not None else f"  {name}: no answer")
        elif q["type"] == "choice":
            conf = ans.get("confidence")
            print(f"  {name}: {ans.get('choice')} (confidence {conf:.2f})" if isinstance(conf, (int, float)) else f"  {name}: no answer")
        else:
            s, label = subq.level(res, name)
            print(f"  {name}: {s:.2f} ({label})" if s is not None else f"  {name}: no answer")
    return 0


def _console_safe():
    """A console that can't show a character (cp1252 on Windows, output redirected without PYTHONUTF8) prints
    "?" instead of stopping the command with UnicodeEncodeError."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def main(argv=None) -> int:
    _console_safe()
    load_env()
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

    s = sub.add_parser("robot", help="whole-robot projects: new | change | show",
                       description='new "<what it should do>": the New robot brief (questions, then a whole robot); '
                                   "new NAME: the default arm under that name; change NAME key=value ...; show NAME")
    s.add_argument("action", choices=["new", "change", "show"])
    s.add_argument("name", nargs="?", default="egg-picker",
                   help='a project name, or for new a request in quotes: "help me sort socks"')
    s.add_argument("changes", nargs="*", help="dotted.key=value (change)")
    s.add_argument("--need", help="natural-language request applied to the default robot (new NAME)")
    s.add_argument("--source", help="start from an imported open-source robot in robots/<source>/ (e.g. so101)")
    s.add_argument("--defaults", action="store_true",
                   help="new from a request: no questions, Morph's sensible defaults (also without a terminal)")
    s.add_argument("--nearest", action="store_true",
                   help="new from a request Morph can't build as asked: build the nearest robot it can")
    s.add_argument("--name", dest="project_name", help="new from a request: the project's name (default: Morph suggests one)")
    s.add_argument("--from", dest="base")
    s.add_argument("--trials", type=int, default=4)
    s.add_argument("--fast", action="store_true", help="shorter simulations")
    s.set_defaults(fn=cmd_robot)

    s = sub.add_parser("doctor", help="check this machine can run Morph Studio, and what to fix if not")
    s.add_argument("--offline", action="store_true", help="skip the Darwin and Keewano network checks")
    s.set_defaults(fn=cmd_doctor)

    s = sub.add_parser("studio", help="web UI: chat with / edit the robot, watch everything regenerate")
    s.add_argument("name", nargs="?", default="egg-picker")
    s.add_argument("--open", action="store_true", help="open the browser when the server is up")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--remote", action="store_true",
                   help="bring your own compute: let other computers use this Studio through a private access link")
    s.add_argument("--team", action="store_true",
                   help="Morph Teams (preview): members sign in with invite links; run `morph team init` first")
    s.add_argument("--allow-url-import", action="store_true",
                   help="let people import from GitHub or Hugging Face links even when other computers use this Studio")
    s.add_argument("--update", action="store_true", help=argparse.SUPPRESS)  # the launchers' option (cmd_studio)
    s.set_defaults(fn=cmd_studio)

    from .studio.teams import cli as teams_cli
    teams_cli.add_parser(sub).set_defaults(fn=lambda a: teams_cli.run(a))

    s = sub.add_parser("import", help="import a robot (URDF, xacro, MuJoCo XML), an object mesh, a parts list, a KiCad "
                                      "board or a Morph export")
    from . import importers
    importers.add_arguments(s)
    s.set_defaults(fn=lambda a: importers.run_cli(a))

    s = sub.add_parser("export", help="a revision as a ZIP (CAD, simulations, electronics and firmware, manufacturing "
                                      "files, manual and a manifest), or with --full the project's whole design history")
    s.add_argument("name")
    s.add_argument("--rev", help="the revision (default: the newest)")
    s.add_argument("--full", action="store_true", help="every revision's design and results (plus --rev's files)")
    s.add_argument("-o", "--out", help="the ZIP to write, or a folder (default: <project>-<rev>.zip here)")
    s.set_defaults(fn=cmd_export)

    s = sub.add_parser("world", help="World mode (beta): simulate a project's robot in a built-in room, or show how it went")
    s.add_argument("name")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--starter", choices=["desk", "counter", "workbench"], help="save this built-in room and simulate in it")
    g.add_argument("--off", action="store_true", help="simulate on the plain table again")
    s.add_argument("--trials", type=int, default=4)
    s.add_argument("--fast", action="store_true", help="shorter simulations")
    s.set_defaults(fn=cmd_world)

    s = sub.add_parser("study", help="robustness study (beta): a revision's pick and place, many times, in randomized "
                                     "worlds within the project's tolerances")
    s.add_argument("name")
    s.add_argument("--rev", help="the revision (default: the newest)")
    s.add_argument("--runs", type=int, default=200, help="randomized runs (default 200)")
    s.add_argument("--workers", type=int, help="worker processes (default: MORPH_STUDY_WORKERS, else cores minus one)")
    s.add_argument("--stress", action="store_true", help="double every tolerance range")
    s.add_argument("--seed", type=int, default=1, help="the same seed gives the same runs (default 1)")
    s.add_argument("--timestep", type=float, default=1.0, help="simulation step in ms (default 1, as the Studio)")
    s.add_argument("--no-checks", action="store_true",
                   help="skip re-running each failure group at half the timestep and with impratio=1")
    s.set_defaults(fn=cmd_study)

    s = sub.add_parser("subq", help="SubQ Snap: triage one request with your SUBQ_API_KEY and print the answers")
    s.add_argument("text")
    s.add_argument("--lint", action="store_true", help="only check the request's shape (no key needed)")
    s.add_argument("--json", action="store_true", help="print SubQ's raw answers")
    s.add_argument("--no-cache", action="store_true", help="ask SubQ even if the same request was answered this week")
    s.add_argument("--timeout", type=float, default=15.0)
    s.set_defaults(fn=cmd_subq)

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
    s.add_argument("--license", metavar="apache-2.0|FILE",
                   help="put a licence in the export: apache-2.0, or your own licence file (none by default)")
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

    a = ap.parse_args(_studio_argv(sys.argv[1:] if argv is None else list(argv)))
    t0 = time.time()
    rc = a.fn(a)
    # every pipeline command's wall-clock goes into the time study
    if a.cmd in ("new", "explore", "change") and getattr(a, "name", None):
        _project(a.name).log({"type": "tool_time", "cmd": a.cmd, "seconds": round(time.time() - t0, 2)})
    return rc if isinstance(rc, int) else 0


if __name__ == "__main__":
    sys.exit(main())
