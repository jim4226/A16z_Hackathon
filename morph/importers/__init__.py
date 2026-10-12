"""Import anything someone may have: robots (URDF, xacro, MuJoCo XML), meshes as the object to grasp, parts lists
(CSV, TSV, XLSX, Markdown), KiCad boards and Morph project exports.

  from morph import importers
  importers.cli_import("~/Downloads/koch-v1-1.zip", answers={"family": "dynamixel"})
  importers.cli_import("https://github.com/TheRobotStudio/SO-ARM100", dry_run=True)

Modules: detect (magic bytes), zipsafe (archive limits), fetch (GitHub / Hugging Face URLs with an SSRF guard),
urdf (bundle normalization), xacro_sandbox (optional extra), mjcf (MuJoCo XML), autoprofile (gripper, TCP, base
frame, ready pose, actuators), mesh_object, bom, kicad, project_zip, robots (the user robot store) and session
(the Studio's import sessions). Nothing here runs code from an imported repository.
"""

from __future__ import annotations

from .common import ImportFailed, home, set_home

__all__ = ["ImportFailed", "cli_import", "home", "set_home"]


def cli_import(path_or_url: str, name: str | None = None, as_: str | None = None, answers: dict | None = None,
               dry_run: bool = False, home_dir: str | None = None, on_event=None, fast_sim: bool = False,
               build: bool = True) -> dict:
    """One-shot import for `morph import <path|url>`: detect, pick the best candidate (of kind `as_` if given),
    then commit it and, for robots and projects, create the project and run the pipeline.

    Returns {"ok", "kind", "candidate", "candidates", "result"?, "project"?, "rev"?, "summary"?, "warnings"} or
    raises ImportFailed with an actionable message. dry_run detects and reports without saving anything."""
    from .session import cli_import as _run

    return _run(path_or_url, name=name, as_=as_, answers=answers, dry_run=dry_run, home_dir=home_dir,
                on_event=on_event, fast_sim=fast_sim, build_project=build)


def add_arguments(p):
    """`morph import` options (cli.py: s = sub.add_parser("import", ...); importers.add_arguments(s))."""
    p.add_argument("source", help="a file, a folder, a zip or a github.com / huggingface.co link")
    p.add_argument("--name", help="name for the new robot and project")
    p.add_argument("--as", dest="as_", choices=["robot", "object", "bom", "board", "project"], help="what to import it as")
    p.add_argument("--gripper", help="the joint that opens and closes the jaws (when Morph can't tell)")
    p.add_argument("--actuators", help="joint=key,... with catalog keys such as sts3215_7v4, xl430_w250, mg996r")
    p.add_argument("--family", choices=["hobby_pwm", "smart_bus", "dynamixel"], help="one actuator family; Morph sizes each joint")
    p.add_argument("--accept-suggestions", action="store_true", help="use the motor model the README or parts list names")
    p.add_argument("--units", choices=["mm", "cm", "m", "in"], help="units of a mesh imported as the object")
    p.add_argument("--project", help="with an object: apply it to this robot project (a new revision)")
    p.add_argument("--fast", action="store_true", help="fast simulations")
    p.add_argument("--dry-run", action="store_true", help="show what Morph found and change nothing")
    p.add_argument("--json", action="store_true", help="print the result as JSON")


def run_cli(a) -> int:
    """`morph import`; returns the exit code. Output is plain ASCII."""
    import json
    import os
    import sys

    answers = {k: v for k, v in (("gripper", a.gripper), ("family", a.family), ("units", a.units)) if v}
    if a.actuators:
        answers["actuators"] = dict(kv.split("=", 1) for kv in a.actuators.split(",") if "=" in kv)
    if a.accept_suggestions:
        answers["accept_suggestions"] = True

    def say(ev):
        if not a.json and ev.get("type") == "import" and ev.get("status") in ("done", "warn", "error"):
            print(f"  {ev['status']:<5} {ev.get('text', '')}")

    try:
        out = cli_import(a.source, name=a.name, as_=a.as_, answers=answers, dry_run=a.dry_run, on_event=say,
                         fast_sim=a.fast)
        if out["kind"] == "object" and a.project and not a.dry_run:
            from ..robot.pipeline2 import RobotProject
            p = RobotProject(os.path.join(home(), a.project))
            base = p.latest()
            if not base:
                raise ImportFailed(f"No robot project named {a.project!r}.")
            out["rev"] = p.run(p.load_design(base).with_changes(out["result"]["changes"]), parent=base,
                               label=out["result"]["label"], verbose=False, fast_sim=a.fast)
            out["project"] = a.project
    except ImportFailed as exc:
        print(f"Import failed: {exc}", file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps(out, indent=1, default=str))
        return 0
    c = out["candidate"]
    print(f"{c['kind']}: {c['file']}")
    if c["kind"] == "robot" and not c.get("error"):
        g = c.get("gripper") or {}
        print(f"  gripper {g.get('joint')} ({', '.join(g.get('why') or [])}); tool point: {(c.get('tcp') or {}).get('how')}")
        for j, info in (c.get("actuators") or {}).items():
            print(f"  {j:<16} {info.get('key') or 'unknown':<14} {info.get('basis', '')}")
    for w in out.get("warnings", [])[:8]:
        print(f"  note: {w}")
    if out.get("dry_run"):
        need = c.get("needs") or []
        print("Dry run: nothing saved." + (f" To build it, pass --family or --actuators ({', '.join(need)} needed)." if need else ""))
        return 0
    r = out.get("result") or {}
    if out.get("project"):
        s = out.get("summary") or {}
        print(f"Built {out['project']} {out.get('rev')}: feasible={s.get('feasible')} pick_place={s.get('pick_place_success')}")
        for i in (s.get("issues") or [])[:6]:
            print(f"  issue: {i}")
    elif r.get("kind") == "object":
        print("Object keys (apply with --project NAME, or `morph robot change NAME ...`):")
        print("  " + " ".join(f"{k}={v}" for k, v in r["changes"].items()))
    elif r.get("kind") in ("bom", "board"):
        rows = r.get("rows") or []
        print(f"{len(rows)} parts" + (f", total {r.get('total')} {r.get('currency') or ''}" if r.get("total") is not None else ""))
        for row in rows[:12]:
            print(f"  {row.get('qty', 1):>4} x {row['item'][:70]}")
    return 0
