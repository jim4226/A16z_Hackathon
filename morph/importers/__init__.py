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
