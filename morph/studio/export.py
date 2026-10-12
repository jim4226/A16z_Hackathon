"""Bounded revision exports; credentials and application state are never inputs."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile
import zipfile

from ..robot.pipeline2 import ORDER
from .security import contained_path

MAX_EXPORT_BYTES = 200 * 1024 * 1024
MAX_EXPORT_FILES = 5000
ARTIFACT_DIRS = {"cad", "sim", "electronics", "mfg", "manual", "gripper"}
ROOT_ARTIFACTS = {"design.json", "results.json", "bom.json", "report.html"}
PRIVATE_NAME = re.compile(r"(?:^|[._-])(?:env|key|keys|api.?key|credentials?|secrets?|tokens?|settings|chat|history|messages|ledger)(?:[._-]|$)", re.I)
PRIVATE_EXTENSIONS = {".pem", ".key", ".p12", ".pfx", ".env", ".log", ".jsonl"}
ARTIFACT_EXTENSIONS = {".stl", ".step", ".stp", ".glb", ".gltf", ".obj", ".mtl", ".bin", ".ply", ".urdf", ".xml", ".mjcf",
                       ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".pdf", ".html", ".json", ".csv", ".txt", ".md",
                       ".ino", ".h", ".hpp", ".cpp", ".c", ".nc", ".gcode", ".gbr", ".drl", ".dxf", ".3mf", ".zip",
                       ".kicad_pcb", ".kicad_pro", ".kicad_sch", ".kicad_dru", ".net", ".pos", ".yaml", ".yml"}


class ExportError(ValueError):
    """Fixed safe messages, suitable for an HTTP response."""


def _allowed(relative: Path) -> bool:
    parts = relative.parts
    if any(p.startswith(".") or PRIVATE_NAME.search(p) for p in parts):
        return False
    if relative.suffix.lower() in PRIVATE_EXTENSIONS:
        return False
    if relative.suffix.lower() not in ARTIFACT_EXTENSIONS:
        return False
    return (len(parts) == 1 and parts[0] in ROOT_ARTIFACTS) or (len(parts) > 1 and parts[0] in ARTIFACT_DIRS)


def build_export(project, rev: str):
    """Return a seekable temporary ZIP and filename; caller closes it after streaming.

    The Studio caller holds its operation lock throughout creation, so a revision
    cannot be modified or reset while it is copied. No settings, credentials,
    project ledger or chat transcript is opened by this function.
    """
    if not isinstance(rev, str) or not re.fullmatch(r"r[0-9]+", rev) or rev not in project.revs():
        raise ExportError("Unknown revision.")
    resolved = contained_path(project.root, rev)
    if resolved is None or not Path(resolved).is_dir():
        raise ExportError("Invalid revision path.")
    root = Path(resolved)
    result_path = contained_path(str(root), "results.json")
    if result_path is None or not Path(result_path).is_file():
        raise ExportError("This revision has no completed results to export.")
    try:
        if Path(result_path).stat().st_size > 16 * 1024 * 1024:
            raise ExportError("Revision metadata exceeds the export limit.")
        with open(result_path, encoding="utf-8") as fh:
            results = json.load(fh)
        if not isinstance(results, dict):
            raise ValueError()
    except (OSError, ValueError, RecursionError):
        raise ExportError("This revision has no readable results to export.") from None
    name = Path(project.root).name
    safe_name = re.sub(r"[^A-Za-z0-9_-]", "-", name)[:64] or "project"
    files, total, inspected = [], 0, 0
    for directory, folders, filenames in os.walk(root, followlinks=False):
        parent = Path(directory)
        kept = []
        for folder in sorted(folders):
            candidate = parent / folder
            relative = candidate.relative_to(root)
            inspected += 1
            if candidate.resolve() != candidate.absolute() or candidate.is_symlink():
                continue
            if relative.parts[0] not in ARTIFACT_DIRS or any(p.startswith(".") or PRIVATE_NAME.search(p) for p in relative.parts):
                continue
            kept.append(folder)
        folders[:] = kept
        for filename in sorted(filenames):
            inspected += 1
            if inspected > MAX_EXPORT_FILES * 2:
                raise ExportError("This revision contains too many entries to export.")
            path = parent / filename
            relative = path.relative_to(root)
            if not _allowed(relative) or path.is_symlink() or path.resolve() != path.absolute():
                continue
            try:
                path.resolve().relative_to(root)
                if not path.is_file():
                    continue
                total += path.stat().st_size
            except (OSError, ValueError):
                raise ExportError("A revision artifact is unavailable. Retry the export.") from None
            if total > MAX_EXPORT_BYTES or len(files) >= MAX_EXPORT_FILES:
                raise ExportError("Export exceeds 200 MiB or 5,000 files. Export individual artifacts instead.")
            files.append((path, relative.as_posix()))
        if inspected > MAX_EXPORT_FILES * 2:
            raise ExportError("This revision contains too many entries to export.")
    validation = (results.get("electronics") or {}).get("firmware_validation") or {
        "status": "unverified", "board_core": "not_verified", "bench": "not_verified"}
    manifest = {"format_version": 1, "project": name, "revision": rev,
                "pipeline_version": results.get("pipeline_version"),
                "stage_status": {node: (results.get("stage_status") or {}).get(node, "unknown") for node in ORDER},
                "summary": results.get("summary") or {},
                "firmware_validation": validation,
                "artifact_count": len(files),
                "note": "Generated design artifacts. Simulation is not physical validation. Review firmware and fabrication outputs before use."}
    metadata = json.dumps(manifest, indent=2).encode("utf-8")
    if total + len(metadata) > MAX_EXPORT_BYTES or len(files) + 1 > MAX_EXPORT_FILES:
        raise ExportError("Export exceeds 200 MiB or 5,000 files. Export individual artifacts instead.")
    # A real temporary file also supports ZipFile readers on Python 3.10 and
    # keeps memory bounded for large CAD/simulation bundles.
    output = tempfile.TemporaryFile(mode="w+b")
    try:
        written = len(metadata)
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=3) as archive:
            archive.writestr("export-manifest.json", metadata)
            for path, relative in files:
                with open(path, "rb") as src, archive.open(relative, "w") as target:
                    while chunk := src.read(64 * 1024):
                        written += len(chunk)
                        if written > MAX_EXPORT_BYTES:
                            raise ExportError("Export exceeds 200 MiB. Export individual artifacts instead.")
                        target.write(chunk)
        output.seek(0)
        return output, f"{safe_name}-{rev}.zip"
    except Exception:
        output.close()
        raise
