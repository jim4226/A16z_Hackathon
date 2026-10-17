"""Import a Morph project export (.morph.zip) as a new project.

  info = inspect("egg-picker-r004.zip")    # what's inside, validated
  plan = plan("egg-picker-r004.zip")       # installs a bundled user robot, returns the design to rebuild
  session.build(plan)                      # new project; the latest design is re-run, results.json never trusted

Accepted layouts (optionally inside one top folder):
* the single-revision export (export-manifest.json + design.json + results.json + artifact folders);
* a project export: manifest.json + meta.json + rNNN/design.json (+ results.json, ledger.jsonl);
* a user robot bundle next to the designs (robot/ or robots/<name>/ with morph_profile.json + robot.urdf +
  meshes/): validated (relative mesh paths inside the bundle, valid meshes) and installed under a new name.
Refused: absolute, `..`, drive and symlink entries, archives over the zip limits, JSON that isn't an object,
designs that don't load, and an arm.source that is neither shipped, installed here, nor in the archive.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import shutil
import tempfile
import zipfile

from .common import LIMITS, MB, ROBOT_RX, ImportFailed, parse_xml, slug
from .zipsafe import _clean, is_symlink_zipinfo, read_member

JSON_CAP = 4 * MB


def _open(path: str) -> zipfile.ZipFile:
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        raise ImportFailed(f"{os.path.basename(path)} is not a readable zip.") from None
    infos = zf.infolist()
    if len(infos) > LIMITS["zip_entries"]:
        zf.close()
        raise ImportFailed(f"The export has {len(infos):,} entries; the limit is {LIMITS['zip_entries']:,}.")
    total = 0
    for i in infos:
        _clean(i.filename)                          # raises on absolute / .. / drive paths
        if is_symlink_zipinfo(i):
            zf.close()
            raise ImportFailed("The export contains a symlink; Morph refuses those.")
        total += i.file_size
        if i.file_size > MB and i.file_size / max(i.compress_size, 1) > LIMITS["zip_ratio"]:
            zf.close()
            raise ImportFailed(f"{i.filename} expands more than {LIMITS['zip_ratio']}x (a zip bomb guard).")
    if total > LIMITS["zip_total"]:
        zf.close()
        raise ImportFailed(f"The export unpacks to {total / MB:.0f} MB; the limit is {LIMITS['zip_total'] // MB} MB.")
    return zf


def _json(zf, name: str):
    try:
        data = json.loads(read_member(zf, name, JSON_CAP).decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ImportFailed(f"{name} in the export is not valid JSON.") from None
    if not isinstance(data, dict):
        raise ImportFailed(f"{name} in the export is not a JSON object.")
    return data


def _prefix(names: list) -> str:
    """'' or 'folder/' when every entry sits in one top folder."""
    tops = {n.split("/", 1)[0] for n in names if n.strip("/")}
    if len(tops) == 1 and all("/" in n for n in names if n.strip("/")):
        return tops.pop() + "/"
    return ""


def load_design(d: dict):
    """Design.from_dict that tolerates fields a newer Morph added (dropped with a warning) but nothing else."""
    from ..spec import Design

    dropped = []
    clean = {}
    for f in dataclasses.fields(Design):
        if f.name not in d:
            continue
        v = d[f.name]
        if f.default_factory is not dataclasses.MISSING:          # a section: object, finger, arm, ...
            if not isinstance(v, dict):
                raise ImportFailed(f"design.{f.name} must be an object.")
            known = {x.name for x in dataclasses.fields(f.default_factory())}
            dropped += [f"{f.name}.{k}" for k in v if k not in known]
            clean[f.name] = {k: x for k, x in v.items() if k in known}
        else:
            clean[f.name] = v
    dropped += [k for k in d if k not in {f.name for f in dataclasses.fields(Design)}]
    try:
        design = Design.from_dict(clean)
        design.with_changes({})
        json.dumps(design.to_dict())
    except (TypeError, ValueError, KeyError) as exc:
        raise ImportFailed(f"A design in the export doesn't load ({exc}).") from None
    for key, val in design.flat().items():
        if isinstance(val, str) and len(val) > 2000:
            raise ImportFailed(f"design value {key} is too long.")
    return design, dropped


def inspect(path: str) -> dict:
    with _open(path) as zf:
        names = [i.filename for i in zf.infolist() if not i.filename.endswith("/")]
        pre = _prefix(names)
        rel = [n[len(pre):] for n in names if n.startswith(pre)]
        man = None
        for m in ("export-manifest.json", "manifest.json", "morph_export.json"):
            if m in rel:
                man = _json(zf, pre + m)
                break
        designs = []
        if "design.json" in rel:
            rev = (man or {}).get("revision") if isinstance((man or {}).get("revision"), str) else "r001"
            designs.append((rev if re.fullmatch(r"r\d{1,5}", rev or "") else "r001", "design.json"))
        for n in rel:
            m = re.fullmatch(r"(r\d{1,5})/design\.json", n)
            if m:
                designs.append((m.group(1), n))
        if not designs:
            raise ImportFailed("This zip has no Morph design (design.json); export the project from Morph again.")
        designs.sort(key=lambda t: int(t[0][1:]))
        warnings = []
        loaded = []
        for rev, n in designs[-200:]:
            d, dropped = load_design(_json(zf, pre + n))
            if dropped:
                warnings.append(f"{rev}: ignored settings from a newer Morph ({', '.join(dropped[:3])})")
            loaded.append({"rev": rev, "design": d.to_dict()})
        meta = _json(zf, pre + "meta.json") if "meta.json" in rel else {}
        robot = None
        for n in rel:
            m = re.fullmatch(r"(robot|robots/([a-z0-9][a-z0-9_-]{0,47}))/morph_profile\.json", n)
            if m:
                robot = {"prefix": pre + m.group(1) + "/", "name": m.group(2) or None}
                break
        project = (man or {}).get("project") or meta.get("name") or os.path.splitext(os.path.basename(path))[0]
        source = loaded[-1]["design"]["arm"].get("source") or ""
        if source and not ROBOT_RX.match(source):
            raise ImportFailed(f"The export's robot id {source[:40]!r} is not a valid robot name.")
        return {"project": slug(str(project)[:60], maxlen=36, fallback="imported"), "designs": loaded, "meta": meta,
                "source": source, "robot": robot, "manifest": {k: (man or {}).get(k) for k in
                                                              ("format_version", "pipeline_version", "revision")},
                "warnings": warnings, "title": str((man or {}).get("project") or project)[:80]}


def _validate_bundle(d: str):
    """A robot folder from an archive: profile JSON, relative URDF inside it, every mesh inside meshes/ and valid."""
    from .urdf import load_mesh, stl_faces

    with open(os.path.join(d, "morph_profile.json"), encoding="utf-8") as fh:
        try:
            prof = json.load(fh)
        except ValueError:
            raise ImportFailed("The robot profile in the export is not valid JSON.") from None
    if not isinstance(prof, dict) or not isinstance(prof.get("urdf"), str):
        raise ImportFailed("The robot profile in the export has no URDF.")
    rel = prof["urdf"]
    if os.path.isabs(rel) or "/" in rel.replace("\\", "/") or not rel.endswith(".urdf") or \
            not os.path.isfile(os.path.join(d, rel)):
        raise ImportFailed("The robot profile's URDF must be a file in the robot folder.")
    if prof.get("urdf_camera"):
        prof.pop("urdf_camera")
    root = parse_xml(os.path.join(d, rel))
    for m in root.iter("mesh"):
        fn = m.get("filename") or ""
        if not re.fullmatch(r"meshes/[A-Za-z0-9_.-]{1,120}\.stl", fn) or ".." in fn:
            raise ImportFailed(f"The exported robot references {fn[:60]!r}; meshes must be meshes/<name>.stl.")
        p = os.path.join(d, *fn.split("/"))
        if not os.path.isfile(p):
            raise ImportFailed(f"The exported robot is missing {fn}.")
        n = stl_faces(p)
        if n is None:
            load_mesh(p, "stl", d)
        elif n > LIMITS["mesh_faces"]:
            raise ImportFailed(f"{fn} has {n:,} triangles; the limit is {LIMITS['mesh_faces']:,}.")
    for key in ("actuators", "arm_joints", "gripper_joint"):
        if key not in prof:
            raise ImportFailed(f"The exported robot profile has no {key}.")
    with open(os.path.join(d, "morph_profile.json"), "w", encoding="utf-8") as fh:
        json.dump(prof, fh, indent=1)
    return prof


def plan(path: str, name: str | None = None, home_dir: str | None = None) -> dict:
    """Validate, install a bundled robot if any, and return the build plan for session.build()."""
    from . import robots
    from .zipsafe import _copy

    info = inspect(path)
    latest = info["designs"][-1]["design"]
    source = info["source"]
    changes, warnings = {}, list(info["warnings"])
    installed = None
    if info["robot"]:
        tmp = tempfile.mkdtemp(prefix="morph-robot-")
        try:
            with _open(path) as zf:
                budget = {"total": 0}
                for i in zf.infolist():
                    if not i.filename.startswith(info["robot"]["prefix"]) or i.filename.endswith("/"):
                        continue
                    relp = _clean(i.filename[len(info["robot"]["prefix"]):])
                    if relp is None or relp.startswith("."):
                        continue
                    dst = os.path.join(tmp, *relp.split("/"))
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    with zf.open(i) as src:
                        _copy(src, dst, i.file_size, budget, {**LIMITS, "zip_file": LIMITS["zip_file"]}, relp)
            prof = _validate_bundle(tmp)
            lic = robots.detect_license(tmp)
            installed, _ = robots.save(tmp, info["robot"]["name"] or source or "imported-robot",
                                       title=prof.get("title") or "", license_info=lic, bundle=tmp,
                                       provenance={"export": os.path.basename(path)}, home_dir=home_dir)
            changes["arm.source"] = installed
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    elif source:
        try:
            robots.resolve(source, home_dir=home_dir)
        except (ValueError, FileNotFoundError):
            raise ImportFailed(f"This project uses the robot {source!r}, which isn't in the export or installed here. "
                               "Import that robot first, or export the project with its robot.") from None
    if (latest.get("world") or {}).get("id"):    # World mode: an export carries the revision's files, not the room
        changes.update({"world.id": "", "world.sha": ""})
        warnings.append("This project was simulated in a World mode room, which exports don't carry: the import is "
                        "simulated on the plain table. Set the room up again in World mode.")
    meta = info["meta"] if isinstance(info["meta"], dict) else {}
    need = str(meta.get("need") or "")[:500]
    return {"kind": "project", "project": slug(name or info["project"], maxlen=36, fallback="imported"),
            "design": latest, "history": [d["design"] for d in info["designs"][:-1]][-50:], "changes": changes,
            "meta": {"kind": "robot", "source": changes.get("arm.source") or source, "origin": "import", "demo": False,
                     "need": need or f"Imported from {os.path.basename(path)}"},
            "label": f"imported {info['designs'][-1]['rev']} from export"[:48], "robot": installed,
            "revisions": len(info["designs"]), "warnings": warnings}
