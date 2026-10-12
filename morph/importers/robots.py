"""The user robot store: imported robot bundles in MORPH_HOME/.robots/<name>/, next to the shipped robots/.

  resolve("so101")          -> "<repo>/robots/so101"            (shipped robots win)
  resolve("koch-v1-1")      -> "<MORPH_HOME>/.robots/koch-v1-1"
  save("work/cand0", "Koch v1.1", ...) -> ("koch-v1-1", "<MORPH_HOME>/.robots/koch-v1-1")

A bundle holds morph_profile.json, robot.urdf (relative mesh paths), meshes/, the original LICENSE/NOTICE files
and import.json (where it came from, the licence found, a SHA-256 per file). Bundles are never overwritten: a
second import under the same name gets a numbered name, so projects built from a bundle stay reproducible.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil

from .common import ROBOT_RX, ImportFailed, contained, home, sha256_file, slug, utcnow

LICENSES = [  # (regex over LICENSE / package.xml / model card text, SPDX id)
    (r"GNU AFFERO GENERAL PUBLIC LICENSE|\bAGPL", "AGPL-3.0"),
    (r"GNU LESSER GENERAL PUBLIC LICENSE|\bLGPL", "LGPL"),
    (r"GNU GENERAL PUBLIC LICENSE\s+Version 3|\bGPL-?3", "GPL-3.0"),
    (r"GNU GENERAL PUBLIC LICENSE\s+Version 2|\bGPL-?2", "GPL-2.0"),
    (r"GNU GENERAL PUBLIC LICENSE|\bGPL\b", "GPL"),
    (r"Apache License,?\s+Version 2\.0|\bapache-?2(\.0)?\b", "Apache-2.0"),
    (r"Mozilla Public License|\bMPL-?2", "MPL-2.0"),
    (r"CERN Open Hardware Licen[cs]e|CERN-OHL", "CERN-OHL"),
    (r"Creative Commons Attribution-NonCommercial|\bCC[- ]BY[- ]NC", "CC-BY-NC"),
    (r"Creative Commons Attribution-ShareAlike|\bCC[- ]BY[- ]SA", "CC-BY-SA"),
    (r"Creative Commons Attribution|\bCC[- ]BY\b", "CC-BY"),
    (r"\bMIT License\b|Permission is hereby granted, free of charge|^\s*license:\s*mit\s*$|\bMIT\b", "MIT"),
    (r"BSD 3-Clause|Redistribution and use in source and binary forms|\bBSD", "BSD"),
    (r"Unlicense|public domain|\bCC0\b", "Public domain"),
]
COPYLEFT = {"AGPL-3.0", "GPL-3.0", "GPL-2.0", "GPL", "LGPL", "CC-BY-SA"}
LICENSE_FILE = re.compile(r"^(licen[sc]e|copying|notice)([._-].*)?$", re.I)


def shipped_root() -> str:
    try:
        from ..robot import imported
        return imported.ROOT
    except Exception:  # noqa: BLE001  (the pipeline package failed to import: fall back to the repo folder)
        return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "robots")


def robots_dir(home_dir: str | None = None) -> str:
    return os.path.join(home_dir or home(), ".robots")


def valid_source(source) -> bool:
    return isinstance(source, str) and bool(ROBOT_RX.match(source))


def _check_bundle(d: str):
    """The profile's URDF files must be relative and inside the bundle (a user bundle never points elsewhere)."""
    with open(os.path.join(d, "morph_profile.json"), encoding="utf-8") as fh:
        prof = json.load(fh)
    if not isinstance(prof, dict):
        raise ValueError("robot profile is not a JSON object")
    for key in ("urdf", "urdf_camera"):
        rel = prof.get(key)
        if rel is None and key == "urdf_camera":
            continue
        if not isinstance(rel, str) or os.path.isabs(rel) or ".." in rel.replace("\\", "/").split("/") or \
                contained(d, os.path.join(d, rel)) is None:
            raise ValueError(f"robot profile {key} must be a file inside the robot folder")


def resolve(source: str, shipped: str | None = None, home_dir: str | None = None) -> str:
    """Folder of robot `source`: the shipped robots/ first, then MORPH_HOME/.robots/. Raises ValueError for a
    name that isn't a robot id and FileNotFoundError when no such robot exists. Never returns a folder
    outside those two roots."""
    if not valid_source(source):
        raise ValueError("Choose a robot by its identifier, such as so101 (lowercase letters, digits, - and _).")
    for root, user in ((shipped or shipped_root(), False), (robots_dir(home_dir), True)):
        if not os.path.isdir(root):
            continue
        d = contained(root, os.path.join(root, source))
        if d is None or d == os.path.realpath(root) or not os.path.isfile(os.path.join(d, "morph_profile.json")):
            continue
        if user:
            _check_bundle(d)
        return d
    raise FileNotFoundError(f"No robot named {source!r}. Import it again, or pick a shipped robot such as so101.")


def list_robots(home_dir: str | None = None) -> list:
    out = []
    for root, user in ((shipped_root(), False), (robots_dir(home_dir), True)):
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            if not valid_source(name):
                continue
            prof = os.path.join(root, name, "morph_profile.json")
            if not os.path.isfile(prof):
                continue
            try:
                with open(prof, encoding="utf-8") as fh:
                    p = json.load(fh)
            except (OSError, ValueError):
                continue
            meta = {}
            imp = os.path.join(root, name, "import.json")
            if user and os.path.isfile(imp):
                try:
                    with open(imp, encoding="utf-8") as fh:
                        meta = json.load(fh)
                except (OSError, ValueError):
                    meta = {}
            out.append({"name": name, "title": p.get("title") or name, "license": p.get("license"),
                        "shipped": not user, "created_utc": meta.get("created_utc"),
                        "source_url": p.get("source_url") or meta.get("source_url") or ""})
    return out


def unique_name(name: str, home_dir: str | None = None) -> str:
    base = slug(name, sep="-", maxlen=40, rx=ROBOT_RX, fallback="robot")
    taken = {r["name"] for r in list_robots(home_dir)}
    out, k = base, 2
    while out in taken or os.path.exists(os.path.join(robots_dir(home_dir), out)):
        out = f"{base[:44]}-{k}"
        k += 1
    return out


def detect_license(bundle: str | None, files: list | None = None) -> dict:
    """{"spdx", "summary", "files", "warning"} from LICENSE/NOTICE files, package.xml <license> or a model card."""
    found, texts = [], []
    if bundle and os.path.isdir(bundle):
        from .common import walk_files
        for rel in files if files is not None else walk_files(bundle, 50000):
            base = os.path.basename(rel)
            p = os.path.join(bundle, *rel.split("/"))
            if not os.path.isfile(p) or os.path.getsize(p) > 512 * 1024:
                continue
            if LICENSE_FILE.match(base):
                found.append(rel)
                with open(p, encoding="utf-8", errors="replace") as fh:
                    texts.append((rel, fh.read(200_000)))
            elif base == "package.xml":
                with open(p, encoding="utf-8", errors="replace") as fh:
                    m = re.search(r"<license[^>]*>([^<]{1,200})</license>", fh.read(), re.I)
                if m:
                    texts.append((rel, m.group(1)))
            elif base.lower() == "readme.md" and rel.count("/") <= 1:
                with open(p, encoding="utf-8", errors="replace") as fh:
                    head = fh.read(4000)
                m = re.match(r"^---\s*\n(.*?)\n---", head, re.S)       # Hugging Face model card front matter
                if m:
                    lic = re.search(r"^license:\s*(\S+)", m.group(1), re.M)
                    if lic:
                        texts.append((rel, f"license: {lic.group(1)}"))
    spdx, where = None, None
    for rel, text in sorted(texts, key=lambda t: (not LICENSE_FILE.match(os.path.basename(t[0])), t[0].count("/"))):
        for rx, sid in LICENSES:
            if re.search(rx, text, re.I | re.M):
                spdx, where = sid, rel
                break
        if spdx:
            break
    warning = None
    if spdx is None:
        warning = ("No licence found: you may not have the right to share these files. Keep this robot private, "
                   "or ask the authors for a licence.")
    elif spdx in COPYLEFT:
        warning = (f"{spdx}: sharing a modified version of this robot means sharing it under the same licence.")
    elif spdx == "CC-BY-NC":
        warning = "CC-BY-NC: non-commercial use only."
    summary = f"{spdx} (from {where})" if spdx else "unknown (no licence file found)"
    return {"spdx": spdx, "summary": summary, "files": found, "warning": warning}


def save(staged: str, name: str, *, title: str = "", license_info: dict | None = None, bundle: str | None = None,
         provenance: dict | None = None, home_dir: str | None = None) -> tuple[str, str]:
    """Copy a staged candidate (robot.urdf, meshes/, morph_profile.json) plus the licence files into a new
    .robots/<name>/ folder. Returns (source, folder)."""
    root = robots_dir(home_dir)
    os.makedirs(root, exist_ok=True)
    source = unique_name(name or title or "robot", home_dir)
    tmp = os.path.join(root, f".tmp-{secrets.token_hex(6)}")
    os.makedirs(tmp)
    try:
        for fn in ("robot.urdf", "source.urdf", "morph_profile.json"):
            if os.path.isfile(os.path.join(staged, fn)):
                shutil.copyfile(os.path.join(staged, fn), os.path.join(tmp, fn))
        if os.path.isdir(os.path.join(staged, "meshes")):
            shutil.copytree(os.path.join(staged, "meshes"), os.path.join(tmp, "meshes"))
        lic = license_info or {}
        os.makedirs(os.path.join(tmp, "licenses"), exist_ok=True)
        for rel in lic.get("files") or []:
            src = contained(bundle, os.path.join(bundle, *rel.split("/"))) if bundle else None
            if src and os.path.isfile(src):
                dst = os.path.join(tmp, "licenses", re.sub(r"[^A-Za-z0-9._-]+", "_", rel)[:120])
                shutil.copyfile(src, dst)
        with open(os.path.join(tmp, "morph_profile.json"), encoding="utf-8") as fh:
            prof = json.load(fh)
        prof.update({"name": source, "urdf": "robot.urdf"})
        if title:
            prof["title"] = title
        if lic.get("summary"):
            prof["license"] = lic["summary"]
        with open(os.path.join(tmp, "morph_profile.json"), "w", encoding="utf-8") as fh:
            json.dump(prof, fh, indent=1)
        files = {}
        for dp, _, fns in os.walk(tmp):
            for f in fns:
                p = os.path.join(dp, f)
                files[os.path.relpath(p, tmp).replace(os.sep, "/")] = sha256_file(p)
        meta = {"source": source, "title": prof.get("title"), "created_utc": utcnow(), "license": lic,
                "provenance": provenance or {}, "files": files}
        with open(os.path.join(tmp, "import.json"), "w", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=1)
        final = os.path.join(root, source)
        os.replace(tmp, final)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return source, final


def bundle_hash(source: str, home_dir: str | None = None) -> str | None:
    """SHA-256 over a user bundle's import.json file list (None for shipped robots): rebuild checks can
    include it so a changed bundle never mixes with old results."""
    import hashlib
    try:
        d = resolve(source, home_dir=home_dir)
    except (ValueError, FileNotFoundError):
        return None
    p = os.path.join(d, "import.json")
    if not os.path.isfile(p):
        return None
    with open(p, encoding="utf-8") as fh:
        files = json.load(fh).get("files") or {}
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()[:12]


def delete(source: str, home_dir: str | None = None) -> bool:
    if not valid_source(source):
        raise ImportFailed("Not a robot id.")
    d = contained(robots_dir(home_dir), os.path.join(robots_dir(home_dir), source))
    if not d or not os.path.isdir(d):
        return False
    shutil.rmtree(d)
    return True
