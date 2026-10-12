"""Revision exports: a ZIP of one revision's engineering files with a manifest that says which stages completed
and that the firmware is unverified, or (full=True) the project's whole design history.

  rNNN export:  export-manifest.json, design.json, results.json, sourcing_darwin.json?, cad/ sim/ electronics/
                mfg/ manual/ gripper/ (allow-listed file types only)
  full export:  export-manifest.json, meta.json, <rev>/design.json and <rev>/results.json for every revision,
                plus the files above for `rev` when one is given (F2's project import rebuilds the rest)

Settings, API keys, chat history, the ledger, hidden files and anything outside the revision folders are never
read. Sizes and file counts are bounded and the archive is built in a temporary file, not in memory.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import zipfile

from ..robot.pipeline2 import ORDER, artifact_problem, stage_problem
from .security import contained_path

MAX_EXPORT_BYTES = 200 * 1024 * 1024
MAX_EXPORT_FILES = 5000
ARTIFACT_DIRS = {"cad", "sim", "electronics", "mfg", "manual", "gripper"}
ROOT_ARTIFACTS = {"design.json", "results.json", "sourcing_darwin.json"}
META_KEYS = ("kind", "source", "need", "origin", "demo", "created_utc")
PRIVATE_NAME = re.compile(r"(?:^|[._-])(?:env|key|keys|api.?key|credentials?|secrets?|tokens?|settings|chat|history|"
                          r"messages|ledger)(?:[._-]|$)", re.I)
PRIVATE_EXTENSIONS = {".pem", ".key", ".p12", ".pfx", ".env", ".log", ".jsonl"}
ARTIFACT_EXTENSIONS = {".stl", ".step", ".stp", ".glb", ".gltf", ".obj", ".mtl", ".bin", ".ply", ".urdf", ".xml", ".mjcf",
                       ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".pdf", ".html", ".json", ".csv", ".txt", ".md",
                       ".ino", ".h", ".hpp", ".cpp", ".c", ".nc", ".gcode", ".gbr", ".drl", ".dxf", ".3mf", ".zip",
                       ".kicad_pcb", ".kicad_pro", ".kicad_sch", ".kicad_dru", ".net", ".pos", ".yaml", ".yml"}
NOTE = ("Generated design files. Simulation is not physical validation and the firmware is not bench-tested: "
        "review the firmware and fabrication files before building or powering anything.")


class ExportError(ValueError):
    """An export the Studio refuses; `status` is the HTTP code and the message is safe to show."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _allowed(parts: tuple) -> bool:
    if any(p.startswith(".") or PRIVATE_NAME.search(p) for p in parts):
        return False
    ext = os.path.splitext(parts[-1])[1].lower()
    if ext in PRIVATE_EXTENSIONS or ext not in ARTIFACT_EXTENSIONS:
        return False
    return (len(parts) == 1 and parts[0] in ROOT_ARTIFACTS) or (len(parts) > 1 and parts[0] in ARTIFACT_DIRS)


def _revision(project, rev) -> tuple[str, dict]:
    """(folder, results) for a finished revision of `project`, or ExportError."""
    if not isinstance(rev, str) or not re.fullmatch(r"r[0-9]{1,6}", rev) or rev not in project.revs():
        raise ExportError("Unknown revision.", 404)
    folder = contained_path(project.root, rev, decode=False)
    if folder is None or not os.path.isdir(folder):
        raise ExportError("Unknown revision.", 404)
    path = contained_path(folder, "results.json", decode=False) or os.path.join(folder, "missing")
    try:
        if os.path.islink(os.path.join(folder, "results.json")):
            raise OSError("linked results")
        if os.path.getsize(path) > 16 * 1024 * 1024:
            raise ExportError("This revision's results are too large to export.", 413)
        with open(path, encoding="utf-8") as fh:
            results = json.load(fh)
    except OSError:
        raise ExportError(f"{rev} is still being built (or was cut off): it has no results to export yet.", 409) from None
    except (ValueError, RecursionError):
        raise ExportError(f"{rev} has unreadable results and can't be exported.", 409) from None
    if not isinstance(results, dict):
        raise ExportError(f"{rev} has unreadable results and can't be exported.", 409)
    return folder, results


def _status(results: dict, folder: str) -> tuple[dict, dict]:
    """Per stage: what happened ("computed", "reused", "error", "blocked", "preview", "pending", "unknown") and,
    for stages that can't be used, why (including output files that are missing from the folder)."""
    status, problems = {}, {}
    for node in ORDER:
        s = (results.get("stage_status") or {}).get(node) or (results.get("provenance") or {}).get(node) or "unknown"
        status[node] = "reused" if s.startswith("reused") else s
        if node in (results.get("provenance") or {}) or node in (results.get("stage_status") or {}):
            problem = stage_problem(results, node) or artifact_problem(results, node, folder)
            if problem:
                problems[node] = problem
    return status, problems


def _files(folder: str, prefix: str = "") -> list:
    """[(path on disk, path in the archive)] for the allow-listed files of one revision folder."""
    root = os.path.realpath(folder)
    out = []
    for directory, dirs, names in os.walk(root, followlinks=False):
        rel_dir = os.path.relpath(directory, root)
        parts_dir = () if rel_dir == "." else tuple(rel_dir.split(os.sep))
        dirs[:] = sorted(d for d in dirs if not os.path.islink(os.path.join(directory, d))
                         and (parts_dir or d in ARTIFACT_DIRS) and not d.startswith(".") and not PRIVATE_NAME.search(d))
        for name in sorted(names):
            path = os.path.join(directory, name)
            parts = parts_dir + (name,)
            if os.path.islink(path) or not _allowed(parts) or contained_path(root, "/".join(parts), decode=False) is None:
                continue
            out.append((path, prefix + "/".join(parts)))
            if len(out) > MAX_EXPORT_FILES:
                raise ExportError(f"The export has more than {MAX_EXPORT_FILES:,} files. Export one revision instead.", 413)
    return out


def _manifest_rev(rev: str, folder: str, results: dict) -> dict:
    status, problems = _status(results, folder)
    electronics = results.get("electronics") or {}
    return {"revision": rev, "label": results.get("label"), "parent": results.get("parent"),
            "pipeline_version": results.get("pipeline_version"), "stage_status": status, "stage_problems": problems,
            "feasible": bool((results.get("summary") or {}).get("feasible")) and not problems,
            "summary": results.get("summary") or {},
            "firmware_validation": electronics.get("firmware_validation") or
            {"status": "unverified", "board_core": "not_verified", "bench": "not_verified"}}


def _plan(project, rev, full: bool) -> tuple[dict, list, str]:
    """(manifest, [(disk path, archive path)], filename) without writing anything."""
    name = os.path.basename(os.path.normpath(project.root))
    safe = re.sub(r"[^A-Za-z0-9_-]", "-", name)[:64] or "project"
    if not full:
        folder, results = _revision(project, rev)
        files = _files(folder)
        manifest = {"format_version": 1, "kind": "revision", "project": name,
                    **_manifest_rev(rev, folder, results), "note": NOTE}
        return manifest, files, f"{safe}-{rev}.zip"
    revs = [r for r in project.revs() if os.path.isfile(os.path.join(project.rev_dir(r), "results.json"))]
    if not revs:
        raise ExportError("This project has no finished revisions to export.", 409)
    if rev is not None:
        _revision(project, rev)
    files, entries = [], []
    for r in revs:
        folder, results = _revision(project, r)
        entries.append(_manifest_rev(r, folder, results))
        own = _files(folder, f"{r}/")
        files += own if r == rev else [f for f in own if f[1] in (f"{r}/design.json", f"{r}/results.json")]
    meta = {}
    try:
        with open(os.path.join(project.root, "meta.json"), encoding="utf-8") as fh:
            raw = json.load(fh)
        meta = {k: raw[k] for k in META_KEYS if isinstance(raw, dict) and k in raw}
    except (OSError, ValueError):
        pass
    manifest = {"format_version": 1, "kind": "project", "project": name, "meta": meta, "files_of": rev,
                "revisions": entries, "note": NOTE}
    return manifest, files, f"{safe}-{'history' if rev is None else rev + '-history'}.zip"


def check(project, rev, full: bool = False):
    """Raise ExportError when build_export would refuse, without reading any file contents."""
    _, files, _ = _plan(project, rev, full)
    total = 0
    for path, _ in files:
        try:
            total += os.path.getsize(path)
        except OSError:
            raise ExportError("A file of this revision disappeared while exporting. Try again.", 409) from None
        if total > MAX_EXPORT_BYTES:
            raise ExportError(f"The export is larger than {MAX_EXPORT_BYTES >> 20} MiB. Export one revision, "
                              "or the files you need from its tabs.", 413)


def build_export(project, rev, full: bool = False):
    """(open temporary file positioned at 0, download name). The caller streams and closes it."""
    check(project, rev, full)
    manifest, files, filename = _plan(project, rev, full)
    out = tempfile.TemporaryFile(mode="w+b")
    try:
        written = 0
        listing = []
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=3) as archive:
            for path, arc in files:
                digest = hashlib.sha256()
                try:
                    with open(path, "rb") as src, archive.open(arc, "w") as dst:
                        while chunk := src.read(1 << 16):
                            written += len(chunk)
                            if written > MAX_EXPORT_BYTES:
                                raise ExportError(f"The export is larger than {MAX_EXPORT_BYTES >> 20} MiB.", 413)
                            digest.update(chunk)
                            dst.write(chunk)
                except OSError:
                    raise ExportError("A file of this revision disappeared while exporting. Try again.", 409) from None
                listing.append({"path": arc, "bytes": os.path.getsize(path), "sha256": digest.hexdigest()})
            manifest["files"] = listing
            if manifest.get("meta"):
                archive.writestr("meta.json", json.dumps(manifest["meta"], indent=2))
            archive.writestr("export-manifest.json", json.dumps(manifest, indent=2))
        out.seek(0)
        return out, filename
    except BaseException:
        out.close()
        raise
