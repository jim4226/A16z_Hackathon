"""Import sessions: MORPH_HOME/.imports/<id>/ holds what was dropped or fetched, what Morph found in it, and the
staged robot bundles, until the person commits one (or the session expires after 24 hours).

  s = Session.create()
  s.add_file("koch/follower.urdf", fh, length)        # streamed, capped; or s.add_path(folder), s.fetch(url)
  det = s.detect(on_event)                             # {"candidates": [...], "checklist": [...], ...}
  png = s.preview(0, "png")                            # or "glb": the robot at its ready pose + TCP marker
  plan = s.commit(0, "Koch v1.1", {"family": "dynamixel"})
  out = build(plan, on_event=hub.send)                 # creates the project and runs the pipeline

Candidate shapes (all JSON):
  robot   {index, kind, file, format, title, name, ok, needs, scope, error, movable, arm_joints, joints, gripper,
           tcp, base, ready, actuators, family, hints, units, meshes, license, warnings}
  object  {index, kind, file, format, changes, fit, units, mass, warnings}
  bom     {index, kind, file, format, rows, count, total, currency, columns, warnings}
  board   {index, kind, file, format, document: pcb|schematic, version, board_mm, footprints, layers, rows, warnings}
  project {index, kind, file, project, revisions, source, robot, warnings}
commit() returns {kind: robot|object|bom|board|project, ...}; robots and projects come back as a plan that
build() turns into a new project (the latest design is rebuilt, never trusted from results.json).
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import threading
import time
import traceback

from . import detect as _detect
from .common import (LIMITS, MB, PROJECT_RX, SESSION_RX, ImportFailed, contained, home, ledger_entry, safe_relpath, set_home,
                     slug, utcnow)

MAX_AGE_S = 24 * 3600
_LOCK = threading.Lock()
_LOCKS: dict = {}
ROBOT_FORMATS = ("urdf", "xacro", "mjcf")
EGG = {"object.name": "raw egg", "object.shape": "cylinder", "object.diameter_mm": 44.0, "object.length_mm": 57.0,
       "object.mass_g": 60.0, "object.friction": 0.35, "object.max_force_n": 25.0, "finger.sensor": "fsr402",
       "finger.width_mm": 20.0, "finger.length_mm": 52.0, "finger.thickness_mm": 6.0, "finger.pad_depth_mm": 5.5,
       "finger.pad_height_mm": 19.8, "electronics.servo": "sts3215"}


def _lock(sid: str) -> threading.Lock:
    with _LOCK:
        return _LOCKS.setdefault(sid, threading.Lock())


def imports_dir(home_dir: str | None = None) -> str:
    return os.path.join(home_dir or home(), ".imports")


def cleanup(home_dir: str | None = None, max_age_s: float = MAX_AGE_S) -> int:
    """Delete import sessions older than max_age_s (the Studio calls this at startup) and tidy the others (Session.tidy:
    uploads a stop cut off). Returns how many were deleted."""
    root = imports_dir(home_dir)
    n = 0
    if not os.path.isdir(root):
        return 0
    now = time.time()
    for sid in os.listdir(root):
        d = os.path.join(root, sid)
        if not SESSION_RX.match(sid) or not os.path.isdir(d) or os.path.islink(d):
            if sid.startswith(".tmp") and os.path.isdir(d):
                shutil.rmtree(d, ignore_errors=True)
            continue
        try:
            if now - os.path.getmtime(os.path.join(d, "session.json")) > max_age_s:
                shutil.rmtree(d, ignore_errors=True)
                n += 1
                continue
        except OSError:
            shutil.rmtree(d, ignore_errors=True)
            n += 1
            continue
        try:
            Session(sid, home_dir).tidy()  # a session kept for later: nothing half-uploaded left in it
        except (OSError, ValueError, KeyError, ImportFailed):
            traceback.print_exc()
    return n


def _catalog() -> list:
    from ..robot import catalog, catalog_extra  # noqa: F401
    return [{"key": k, "name": v["name"], "family": v["family"], "torque_nm": v["torque_nm"]}
            for k, v in sorted(catalog.ACTUATORS.items(), key=lambda kv: (kv[1]["family"], kv[1]["torque_nm"]))]


class Session:
    def __init__(self, sid: str, home_dir: str | None = None):
        if not isinstance(sid, str) or not SESSION_RX.match(sid):
            raise ImportFailed("Unknown import session.")
        self.id = sid
        self.home = os.path.abspath(home_dir or home())
        self.root = os.path.join(imports_dir(self.home), sid)
        self.files = os.path.join(self.root, "files")
        self.work = os.path.join(self.root, "work")

    # ---------------------------------------------------------------- lifecycle
    @classmethod
    def create(cls, home_dir: str | None = None, url: str | None = None) -> "Session":
        s = cls(secrets.token_hex(8), home_dir)
        os.makedirs(s.files)
        os.makedirs(s.work)
        s._save({"id": s.id, "created_utc": utcnow(), "url": url, "files": {}, "bytes": 0, "status": "new"})
        return s

    @classmethod
    def open(cls, sid: str, home_dir: str | None = None) -> "Session":
        s = cls(sid, home_dir)
        if not os.path.isfile(os.path.join(s.root, "session.json")):
            raise ImportFailed("This import session has expired or was deleted; start the import again.")
        return s

    def state(self) -> dict:
        with open(os.path.join(self.root, "session.json"), encoding="utf-8") as fh:
            return json.load(fh)

    def _save(self, st: dict):
        from ..services.durable import write_json
        write_json(os.path.join(self.root, "session.json"), st)  # synced, then renamed over the old one

    def delete(self):
        shutil.rmtree(self.root, ignore_errors=True)

    # ---------------------------------------------------------------- inputs
    def add_file(self, rel: str, stream, length: int) -> dict:
        """Stream one file (a raw request body) into the session: Content-Length checked before reading,
        copied in 1 MiB chunks, the session's file count and total size capped."""
        rel = safe_relpath(rel)
        if not isinstance(length, int) or length < 0:
            raise ImportFailed("Send each file with a Content-Length.")
        if length > LIMITS["upload_file"]:
            raise ImportFailed(f"{os.path.basename(rel)} is {length / MB:.0f} MB; the limit is {LIMITS['upload_file'] // MB} MB per file.")
        tok = secrets.token_hex(4)
        from ..studio import rooms
        claim = rooms.claim(self.home)  # noqa: F841  (held while this upload streams: another Studio's tidy() keeps it)
        with _lock(self.id):
            # what is being streamed is reserved under `pending`, never counted as a file before it is all there: a
            # Studio stopped mid-upload never leaves a session claiming a file it doesn't have
            st = self.state()
            pending = st.setdefault("pending", {})
            old = st["files"].get(rel, 0)
            coming = [p for p in pending.values() if p[0] != rel]
            if rel not in st["files"] and len(st["files"]) + len(coming) >= LIMITS["session_files"]:
                raise ImportFailed(f"More than {LIMITS['session_files']:,} files; drop only the robot's folder.")
            total = st["bytes"] - old + sum(p[1] for p in coming) + length
            if total > LIMITS["upload_total"]:
                raise ImportFailed(f"{total / MB:.0f} MB is over the {LIMITS['upload_total'] // MB} MB limit for one import.")
            pending[tok] = [rel, length, rooms.OWNER]
            st["status"] = "new"
            self._save(st)
        target = os.path.join(self.files, *rel.split("/"))
        tmp = target + f".part-{tok}"
        got = 0
        try:
            if contained(self.files, os.path.dirname(target)) is None:
                raise ImportFailed("Refused a path outside the import folder.")
            try:
                os.makedirs(os.path.dirname(target), exist_ok=True)
            except OSError:
                raise ImportFailed(f"{rel[:80]} clashes with a file of the same name already in this import.") from None
            if os.path.isdir(target):
                raise ImportFailed(f"{rel[:80]} clashes with a folder of the same name already in this import.")
            with open(tmp, "wb") as out:
                while got < length:
                    chunk = stream.read(min(MB, length - got))
                    if not chunk:
                        break
                    out.write(chunk)
                    got += len(chunk)
            if got != length:
                raise ImportFailed(f"The upload of {os.path.basename(rel)} was cut off ({got:,} of {length:,} bytes); try again.")
            os.replace(tmp, target)
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            with _lock(self.id):
                st = self.state()
                (st.get("pending") or {}).pop(tok, None)
                self._save(st)
            raise
        with _lock(self.id):  # all there: now it counts
            st = self.state()
            (st.get("pending") or {}).pop(tok, None)
            st["files"][rel] = length
            st["bytes"] = sum(st["files"].values())
            self._save(st)
        return {"path": rel, "size": got}

    def tidy(self) -> bool:
        """What a stopped Studio left in this session: the half-written `*.part-*` files go, so do the reservations of
        uploads that never finished, and a file recorded before it arrived (sessions from before uploads were reserved)
        is forgotten. An upload another Studio on the same projects folder is still streaming (its lock, rooms.claim,
        is held) keeps its reservation and its part file. True when something changed."""
        from ..studio import rooms
        changed, cut = False, set()
        with _lock(self.id):
            pending = self.state().get("pending") or {}
        live = {tok for tok, p in pending.items() if isinstance(p, list) and len(p) > 2 and rooms.owner_live(self.home, p[2])}
        for folder, _, names in os.walk(self.files):
            for n in names:
                m = re.fullmatch(r"(.+)\.part-([0-9a-f]{8})", n)
                if m and m.group(2) not in live:
                    try:
                        os.remove(os.path.join(folder, n))
                        changed = True
                    except OSError:
                        pass
                    cut.add(os.path.relpath(os.path.join(folder, m.group(1)), self.files).replace(os.sep, "/"))
        with _lock(self.id):
            st = self.state()
            kept = {tok: p for tok, p in (st.get("pending") or {}).items() if tok in live}
            if (st.get("pending") or {}) != kept:
                st["pending"], changed = kept, True
            if not st.get("pending"):
                st.pop("pending", None)
            gone = [rel for rel in cut if rel in st["files"] and not os.path.exists(os.path.join(self.files, *rel.split("/")))]
            for rel in gone:
                st["files"].pop(rel)
            if gone:
                st["bytes"] = sum(st["files"].values())
                changed = True
            if changed:
                self._save(st)
        return changed

    def add_path(self, path: str) -> list:
        """Copy a local file or folder into the session (the CLI path), with the same limits as uploads."""
        path = os.path.abspath(os.path.expanduser(path))
        if os.path.isdir(path):
            from .common import walk_files
            base = re.sub(r"[^A-Za-z0-9._ -]+", "_", os.path.basename(path.rstrip(os.sep))) or "folder"
            out = []
            for rel in walk_files(path, LIMITS["session_files"] + 1):
                src = os.path.join(path, *rel.split("/"))
                with open(src, "rb") as fh:
                    out.append(self.add_file(f"{base}/{rel}", fh, os.path.getsize(src)))
            if not out:
                raise ImportFailed(f"{path} has no files to import.")
            return out
        if not os.path.isfile(path):
            raise ImportFailed(f"{path} doesn't exist.")
        with open(path, "rb") as fh:
            return [self.add_file(re.sub(r"[^A-Za-z0-9._ -]+", "_", os.path.basename(path)) or "file", fh,
                                  os.path.getsize(path))]

    def fetch(self, url: str, on_event=None, allow: bool = True) -> dict:
        from . import fetch as _fetch

        emit = self._emitter(on_event)
        emit("fetch", "start", f"Fetching {url[:120]}")
        info = _fetch.fetch(url, self.files, allow=allow, on_progress=lambda t: emit("fetch", "progress", t))
        with _lock(self.id):
            st = self.state()
            for rel, size in info["files"].items():
                st["files"][rel] = size
            st["bytes"] = sum(st["files"].values())
            st["url"] = info.get("url") or url
            st["source_url"] = info.get("source_url") or url
            self._save(st)
        emit("fetch", "done", f"Fetched {len(info['files'])} files ({info['bytes'] / MB:.1f} MB)")
        return info

    # ---------------------------------------------------------------- detect
    def _emitter(self, on_event):
        def emit(stage, status, text):
            if on_event:
                try:
                    on_event({"type": "import", "id": self.id, "stage": stage, "status": status, "text": text})
                except Exception:  # noqa: BLE001  (a closed SSE client must not break the import)
                    pass
        return emit

    def _unpack(self, emit, warnings: list, errors: list):
        from . import zipsafe

        st = self.state()
        for rel in sorted(st["files"]):
            p = os.path.join(self.files, *rel.split("/"))
            if not os.path.isfile(p):
                continue
            fmt = _detect.sniff(p, os.path.basename(rel))
            if fmt not in ("zip", "tar", "gzip", "bz2", "xz"):
                continue
            stem = re.sub(r"(\.tar)?\.(zip|tgz|gz|bz2|xz|tar)$", "", os.path.basename(rel), flags=re.I) or "archive"
            dest = os.path.join(os.path.dirname(p), stem)
            k = 2
            while os.path.exists(dest):
                dest = os.path.join(os.path.dirname(p), f"{stem}_{k}")
                k += 1
            emit("unpack", "start", f"Unpacking {os.path.basename(rel)}")
            try:
                files, warns = zipsafe.extract(p, dest, fmt)
            except ImportFailed as exc:
                shutil.rmtree(dest, ignore_errors=True)
                errors.append(self._why(exc))
                emit("unpack", "error", self._why(exc))
                continue
            os.remove(p)
            warnings += warns[:20]
            emit("unpack", "done", f"Unpacked {len(files)} files from {os.path.basename(rel)}")

    def detect(self, on_event=None, max_robots: int = 6) -> dict:
        emit = self._emitter(on_event)
        warnings, errors, refused = [], [], []
        t0 = time.time()
        shutil.rmtree(self.work, ignore_errors=True)
        os.makedirs(self.work)
        self._unpack(emit, warnings, errors)
        inv = _detect.scan(self.files, 20000)
        if not inv and not errors:  # an archive whose every entry was skipped says which, not "drop a file"
            raise ImportFailed(f"Nothing left to import: Morph {'; '.join(warnings[:3])}." if warnings else
                               "Nothing to import yet: drop a file or folder, or paste a link.")
        for f in inv:
            msg = _detect.refusal(f["format"], os.path.basename(f["path"]))
            if msg:
                refused.append(msg)
        by = {}
        for f in inv:
            by.setdefault(f["format"], []).append(f)
        cands = []
        robots = self._robot_files(by)
        if robots:
            emit("robot", "start", f"Found {len(robots)} robot description{'s' if len(robots) > 1 else ''}")
        used_meshes = set()
        for f in robots[:max_robots]:
            cands.append(self._robot_candidate(len(cands), f, emit, used_meshes))
        if len(robots) > max_robots:
            warnings.append(f"{len(robots) - max_robots} more robot files were not analysed")
        for f in by.get("morph_project", [])[:3]:
            cands.append(self._project_candidate(len(cands), f))
        meshes = [f for k in ("stl", "obj", "ply", "off", "glb", "gltf", "3mf", "dae", "step", "iges") for f in by.get(k, [])]
        standalone = [f for f in meshes if os.path.realpath(os.path.join(self.files, *f["path"].split("/"))) not in used_meshes]
        if standalone and (not robots or len(inv) <= 3):
            for f in sorted(standalone, key=lambda f: -f["size"])[:5]:
                cands.append(self._object_candidate(len(cands), f))
        for f in [x for k in ("csv", "tsv", "xlsx", "markdown") for x in by.get(k, [])][:8]:
            c = self._bom_candidate(len(cands), f)
            if c:
                cands.append(c)
        for f in (by.get("kicad_pcb") or by.get("kicad_sch") or [])[:3]:
            cands.append(self._board_candidate(len(cands), f))
        order = {"robot": 0, "project": 1, "object": 2, "board": 3, "bom": 4}
        def rank(c):
            ms = c.get("meshes") or {}
            found = ms["found"] / ms["total"] if ms.get("total") else 1.0
            return (order.get(c["kind"], 9), bool(c.get("error")), not c.get("ok"), "gripper" in (c.get("needs") or []),
                    -round(found, 2), c["index"])
        cands.sort(key=rank)
        for i, c in enumerate(cands):
            c["index"] = i
            wd = os.path.join(self.work, f"cand{c.pop('_slot')}") if "_slot" in c else None
            c["_dir"] = os.path.relpath(wd, self.root) if wd else None
        if not cands:
            if refused:
                raise ImportFailed(refused[0])
            raise ImportFailed(errors[0] if errors else "Morph found nothing it can import here: drop a URDF, xacro or "
                               "MuJoCo XML robot (with its meshes), an STL/OBJ/STEP of the object to grasp, a parts list "
                               "(CSV, XLSX, Markdown) or a KiCad board.")
        kinds = {c["kind"] for c in cands}
        out = {"id": self.id, "kind": cands[0]["kind"] if len(kinds) == 1 else "mixed", "candidates": cands,
               "checklist": self._checklist(cands), "warnings": warnings, "errors": errors, "refused": refused,
               "files": len(inv), "seconds": round(time.time() - t0, 2), "catalog": _catalog()}
        with _lock(self.id):
            with open(os.path.join(self.work, "detect.json"), "w", encoding="utf-8") as fh:
                json.dump(out, fh, indent=1)
            st = self.state()
            st["status"] = "detected"
            self._save(st)
        emit("detect", "done", f"Found {len(cands)} thing{'s' if len(cands) != 1 else ''} to import")
        return _public(out)

    def detection(self) -> dict:
        p = os.path.join(self.work, "detect.json")
        if not os.path.isfile(p):
            raise ImportFailed("Run detection first.")
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)

    def _robot_files(self, by: dict) -> list:
        from .mjcf import includes_of

        urdfs = by.get("urdf", [])
        xacros = [f for f in by.get("xacro", []) if not _included(self.files, f, by.get("xacro", []))]
        stems = {re.sub(r"\.urdf$", "", os.path.splitext(f["path"])[0]) for f in urdfs}
        xacros = [f for f in xacros if re.sub(r"(\.urdf)?\.xacro$", "", f["path"]) not in stems]
        mj = by.get("mjcf", [])
        bodies = {}
        for f in mj:
            p = os.path.join(self.files, *f["path"].split("/"))
            try:
                with open(p, encoding="utf-8", errors="replace") as fh:
                    bodies[os.path.realpath(p)] = "<body" in fh.read(2_000_000)
            except OSError:
                pass
        # a scene.xml that includes the robot's own file is the same robot plus a floor and lights
        mj = [f for f in mj if not any(bodies.get(r) for r in includes_of(os.path.join(self.files, *f["path"].split("/"))))]
        ranked = []
        for f in urdfs + xacros + mj:
            try:
                with open(os.path.join(self.files, *f["path"].split("/")), encoding="utf-8", errors="replace") as fh:
                    text = fh.read(2_000_000)
            except OSError:
                continue
            links = len(re.findall(r"<(link|body)\b", text))
            if f["format"] == "xacro" and not links and "<xacro:include" not in text:
                continue                      # a macro library (ros2_control, gazebo, materials): not a robot
            if f["format"] == "mjcf" and not links and "<include" not in text:
                continue                      # assets or defaults only
            side = bool(re.search(r"camera|gazebo|ros2_control|transmission|test|example", f["path"], re.I))
            ranked.append(((side, {"urdf": 0, "mjcf": 1, "xacro": 2}[f["format"]], -min(links, 50),
                            f["path"].count("/"), f["path"]), f))
        return [f for _, f in sorted(ranked, key=lambda t: t[0])]

    def _robot_candidate(self, index: int, f: dict, emit, used_meshes: set, answers: dict | None = None,
                         slot: int | None = None) -> dict:
        from . import autoprofile, mjcf, robots, urdf

        slot = index if slot is None else slot
        wd = os.path.join(self.work, f"cand{slot}")
        shutil.rmtree(wd, ignore_errors=True)
        os.makedirs(wd)
        src = os.path.join(self.files, *f["path"].split("/"))
        bundle = self._bundle_root(f["path"])
        title = _title(f["path"], self.state().get("source_url"))
        c = {"index": index, "_slot": slot, "kind": "robot", "file": f["path"], "format": f["format"], "title": title,
             "name": slug(title, maxlen=40), "ok": False, "needs": [], "scope": "ok", "error": None, "warnings": []}
        answers = answers or {}
        try:
            if f["format"] == "mjcf":
                rep0 = mjcf.to_urdf(src, bundle, wd)
            else:
                text = None
                if f["format"] == "xacro":
                    from . import xacro_sandbox
                    emit("xacro", "start", f"Expanding {os.path.basename(f['path'])}")
                    text = xacro_sandbox.expand(src, bundle)
                    emit("xacro", "done", f"Expanded {os.path.basename(f['path'])}")
                rep0 = urdf.normalize(src, bundle, wd, text=text, source_name=os.path.basename(f["path"]),
                                      mesh_scale=answers.get("mesh_scale"))
            ms = rep0["meshes"]
            emit("meshes", "warn" if ms["missing"] else "done", f"{os.path.basename(f['path'])}: meshes {ms['found']}/{ms['total']}")
            for real in _mesh_sources(rep0, src, bundle):
                used_meshes.add(real)
            lic = robots.detect_license(bundle)
            prof, rep = autoprofile.derive(rep0["urdf"], wd, bundle=bundle, answers=answers, title=title,
                                           license_info=lic, source_url=self.state().get("source_url") or "")
            g = rep.get("gripper") or {}
            if g.get("joint"):
                emit("gripper", "done", f"Gripper: {g['joint']} ({', '.join(g.get('why') or [])})")
            else:
                emit("gripper", "warn", "No gripper joint found: pick one")
            unknown = rep.get("unknown_actuators") or []
            if g.get("joint"):
                emit("actuators", "warn" if unknown else "done",
                     f"Actuators: pick a model for {len(unknown)} joints" if unknown else "Actuators found in the mesh names")
            c.update({
                "ok": bool(rep.get("ok")), "scope": rep.get("scope", "ok"), "movable": rep0["movable"],
                "joints": _joint_list(rep0["urdf"]), "arm_joints": rep.get("arm_joints"), "gripper": rep.get("gripper"),
                "tcp": rep.get("tcp"), "base": rep.get("base"), "ready": rep.get("ready"),
                "actuators": rep.get("actuators") or {}, "family": rep.get("family"), "hints": rep.get("hints") or {},
                "units": rep.get("units"), "license": lic, "parts": rep.get("parts"),
                "meshes": {"total": ms["total"], "found": ms["found"], "missing": ms["missing"][:20], "searched": ms["searched"]},
                "renamed": len(rep0.get("renamed") or {}), "actuated": rep0.get("actuated") or [],
                "warnings": (rep0.get("warnings") or []) + (rep.get("warnings") or []) + ([lic["warning"]] if lic.get("warning") else []),
            })
            if not g.get("joint"):
                c["needs"].append("gripper")
            if unknown:
                c["needs"].append("actuators")
            if ms["total"] and ms["found"] == 0:
                c["ok"] = False
                c["error"] = c["warnings"][0]
        except ImportFailed as exc:
            c.update(error=self._why(exc), scope="error")
            emit("robot", "error", f"{os.path.basename(f['path'])}: {self._why(exc)}")
        except Exception as exc:  # noqa: BLE001  (a broken file must not take the whole detection down)
            traceback.print_exc()
            why = f"{exc.__class__.__name__}: {self._why(exc)}"
            c.update(error=f"{os.path.basename(f['path'])} couldn't be analysed ({why})"[:300], scope="error")
            emit("robot", "error", c["error"])
        return c

    def _why(self, exc) -> str:
        """An error's text for the import dialog: files named from the projects folder, never the server's own path
        (durable.explain; an archive or robot file that can't be read says where it is)."""
        from ..services import durable
        return durable.explain(exc, self.home)

    def _bundle_root(self, rel: str) -> str:
        """The folder the robot's references may use: the top folder of the upload that holds the file."""
        parts = rel.split("/")
        return os.path.join(self.files, parts[0]) if len(parts) > 1 else self.files

    def _object_candidate(self, index: int, f: dict, answers: dict | None = None) -> dict:
        from . import mesh_object

        p = os.path.join(self.files, *f["path"].split("/"))
        c = {"index": index, "kind": "object", "file": f["path"], "format": f["format"], "ok": False, "error": None,
             "warnings": []}
        try:
            res = mesh_object.object_changes(p, f["format"], answers=answers or {}, bundle=self.files)
            c.update(ok=True, **res)
        except ImportFailed as exc:
            c["error"] = self._why(exc)
        return c

    def _bom_candidate(self, index: int, f: dict, answers: dict | None = None) -> dict | None:
        from . import bom

        p = os.path.join(self.files, *f["path"].split("/"))
        try:
            res = bom.parse(p, f["format"], table=(answers or {}).get("table"))
        except ImportFailed as exc:
            if f["format"] == "markdown":
                return None
            return {"index": index, "kind": "bom", "file": f["path"], "format": f["format"], "ok": False,
                    "error": self._why(exc), "warnings": []}
        if not res["rows"]:
            return None
        return {"index": index, "kind": "bom", "file": f["path"], "format": f["format"], "ok": True, "error": None,
                "rows": res["rows"][:200], "count": len(res["rows"]), "total": res.get("total"),
                "currency": res.get("currency"), "columns": res.get("columns"), "tables": res.get("tables"),
                "warnings": res.get("warnings") or []}

    def _board_candidate(self, index: int, f: dict) -> dict:
        from . import kicad

        p = os.path.join(self.files, *f["path"].split("/"))
        c = {"index": index, "kind": "board", "file": f["path"], "format": f["format"], "ok": False, "error": None,
             "warnings": []}
        try:
            res = kicad.read(p)
            c.update(ok=True, **res)
        except ImportFailed as exc:
            c["error"] = self._why(exc)
        return c

    def _project_candidate(self, index: int, f: dict) -> dict:
        from . import project_zip

        p = os.path.join(self.files, *f["path"].split("/"))
        c = {"index": index, "kind": "project", "file": f["path"], "format": f["format"], "ok": False, "error": None,
             "warnings": []}
        try:
            res = project_zip.inspect(p)
            c.update(ok=True, project=res["project"], revisions=len(res["designs"]), source=res.get("source"),
                     robot=bool(res.get("robot")), warnings=res.get("warnings") or [], title=res.get("title"))
        except ImportFailed as exc:
            c["error"] = self._why(exc)
        return c

    @staticmethod
    def _checklist(cands: list) -> list:
        """The dialog's live checklist for the best robot candidate (or the first candidate)."""
        c = next((x for x in cands if x["kind"] == "robot"), cands[0])
        if c["kind"] != "robot":
            return [{"stage": c["kind"], "status": "done" if c.get("ok") else "error",
                     "text": c.get("error") or f"{c['kind'].capitalize()} found in {os.path.basename(c['file'])}"}]
        out = [{"stage": "unpack", "status": "done", "text": f"Read {os.path.basename(c['file'])}"}]
        if c.get("format") == "xacro":
            out.append({"stage": "xacro", "status": "error" if c.get("error") and "xacro" in c["error"].lower() else "done",
                        "text": "Expanded the xacro macros"})
        if c.get("error"):
            out.append({"stage": "robot", "status": "error", "text": c["error"]})
            return out
        ms = c["meshes"]
        out.append({"stage": "meshes", "status": "warn" if ms["missing"] else "done", "text": f"Meshes {ms['found']}/{ms['total']}"})
        g = c.get("gripper") or {}
        out.append({"stage": "gripper", "status": "done" if g.get("joint") else "warn",
                    "text": f"Gripper: {g['joint']} ({', '.join(g.get('why') or [])})" if g.get("joint") else
                    "No gripper found: pick the joint that opens the jaws"})
        if g.get("joint"):
            unknown = [n for n, a in (c.get("actuators") or {}).items() if not a.get("key")]
            out.append({"stage": "actuators", "status": "warn" if unknown else "done",
                        "text": f"Actuators: pick a model for {len(unknown)} of {len(c['actuators'])} joints" if unknown
                        else "Actuators: " + ", ".join(sorted({a['key'] for a in c['actuators'].values()}))})
            t = c.get("tcp") or {}
            out.append({"stage": "tcp", "status": "done", "text": f"Tool point: {t.get('how', '')}"})
        return out

    # ---------------------------------------------------------------- preview
    def preview(self, index: int, fmt: str = "glb") -> tuple[bytes, str]:
        """(bytes, content type) for a candidate: robot at its ready pose with the TCP marker, or the object mesh."""
        from . import preview as pv

        det = self.detection()
        c = _cand(det, index)
        if fmt not in ("glb", "png"):
            raise ImportFailed("Preview format must be glb or png.")
        cache = os.path.join(self.work, f"preview{index}.{fmt}")
        if os.path.isfile(cache):
            with open(cache, "rb") as fh:
                return fh.read(), _ctype(fmt)
        if c["kind"] == "robot":
            if c.get("error") or not c.get("_dir"):
                raise ImportFailed(c.get("error") or "This robot has no preview.")
            from ..robot.urdf_import import load_urdf
            wd = os.path.join(self.root, c["_dir"])
            with open(os.path.join(wd, "morph_profile.json"), encoding="utf-8") as fh:
                prof = json.load(fh)
            asm = load_urdf(os.path.join(wd, "robot.urdf"), {k: v for k, v in prof.items() if k != "actuators"})
            meshes = pv.robot_meshes(asm, prof.get("ready") or {})
        elif c["kind"] == "object":
            from . import mesh_object
            meshes = mesh_object.preview_meshes(os.path.join(self.files, *c["file"].split("/")), c)
        else:
            raise ImportFailed(f"A {c['kind']} has no 3D preview.")
        data = pv.glb_bytes(meshes) if fmt == "glb" else pv.render_png(meshes)
        with open(cache, "wb") as fh:
            fh.write(data)
        return data, _ctype(fmt)

    # ---------------------------------------------------------------- commit
    def commit(self, index: int, name: str | None = None, answers: dict | None = None, on_event=None) -> dict:
        emit = self._emitter(on_event)
        det = self.detection()
        c = _cand(det, index)
        answers = _check_answers(answers or {})
        if c["kind"] == "robot":
            return self._commit_robot(c, name, answers, emit)
        if c.get("error"):
            raise ImportFailed(c["error"])
        if c["kind"] == "object":
            if answers:
                c = self._object_candidate(index, {"path": c["file"], "format": c["format"]}, answers)
                if c.get("error"):
                    raise ImportFailed(c["error"])
            emit("commit", "done", f"Object: {c['changes'].get('object.shape')} {c['changes'].get('object.diameter_mm')} mm")
            return {"kind": "object", "file": c["file"], "changes": c["changes"], "fit": c.get("fit"),
                    "mass": c.get("mass"), "units": c.get("units"), "warnings": c.get("warnings", []),
                    "label": f"object from {os.path.basename(c['file'])}"[:48], "format": c.get("format")}
        if c["kind"] == "bom":
            from . import bom
            res = bom.parse(os.path.join(self.files, *c["file"].split("/")), c["format"], table=answers.get("table"))
            return {"kind": "bom", "file": c["file"], "rows": res["rows"], "total": res.get("total"),
                    "currency": res.get("currency"), "darwin_parts": bom.darwin_parts(res["rows"]),
                    "actuators": bom.actuator_keys(res["rows"]), "warnings": res.get("warnings") or []}
        if c["kind"] == "board":
            from . import kicad
            res = kicad.read(os.path.join(self.files, *c["file"].split("/")))
            return {"kind": "board", "file": c["file"], **res}
        if c["kind"] == "project":
            from . import project_zip
            plan = project_zip.plan(os.path.join(self.files, *c["file"].split("/")), name=name, home_dir=self.home)
            emit("commit", "done", f"Project {plan['project']}: rebuilding its latest design")
            return {**plan, "format": c.get("format")}
        raise ImportFailed(f"Can't commit a {c['kind']}.")

    def _commit_robot(self, c: dict, name: str | None, answers: dict, emit) -> dict:
        from . import robots

        if c.get("error"):
            raise ImportFailed(c["error"])
        slot = int(c["_dir"].rsplit("cand", 1)[1])
        if answers:
            used = set()
            c2 = self._robot_candidate(c["index"], {"path": c["file"], "format": c["format"]}, emit, used, answers, slot)
            if c2.get("error"):
                raise ImportFailed(c2["error"])
            c = {**c2, "_dir": c["_dir"]}
            try:
                os.remove(os.path.join(self.work, f"preview{c['index']}.glb"))
                os.remove(os.path.join(self.work, f"preview{c['index']}.png"))
            except OSError:
                pass
        if "gripper" in c.get("needs", []):
            raise ImportFailed("Pick the gripper joint (the one that opens and closes the jaws) first.")
        unknown = [n for n, a in (c.get("actuators") or {}).items() if not a.get("key")]
        if unknown:
            raise ImportFailed(f"Pick the actuators for {', '.join(unknown[:4])}{'…' if len(unknown) > 4 else ''} "
                               "(or one family for all joints); Morph can't tell them from the files.")
        if c.get("scope") not in ("ok", None):
            raise ImportFailed(c.get("error") or "This robot can't be built.")
        wd = os.path.join(self.root, c["_dir"])
        title = str(answers.get("title") or c.get("title") or "Imported robot")[:80]
        bundle = self._bundle_root(c["file"])
        lic = c.get("license") or {}
        st = self.state()
        source, folder = robots.save(wd, name or c.get("name") or title, title=title, license_info=lic, bundle=bundle,
                                     provenance={"file": c["file"], "url": st.get("source_url") or st.get("url"),
                                                 "session": self.id, "answers": answers,
                                                 "files": {k: v for k, v in list(st["files"].items())[:50]}},
                                     home_dir=self.home)
        emit("commit", "done", f"Saved the robot as {source}")
        reach = float((c.get("ready") or {}).get("reach_mm") or 400.0)
        clamp = lambda v, lo, hi: float(min(max(round(v / 5.0) * 5.0, lo), hi))  # noqa: E731
        changes = {"arm.source": source, "arm.actuator_family": c.get("family") or "smart_bus", "finger.sensor": "none",
                   "task.pick_x_mm": clamp(0.46 * reach, 80, 400), "task.pick_y_mm": clamp(-0.17 * reach, -300, 300),
                   "task.place_x_mm": clamp(0.33 * reach, 80, 400), "task.place_y_mm": clamp(0.36 * reach, -300, 300)}
        return {"kind": "robot", "source": source, "title": title, "folder": os.path.relpath(folder, self.home),
                "project": slug(name or source, maxlen=40), "changes": changes,
                "meta": {"kind": "robot", "source": source, "origin": "import", "demo": False,
                         "need": f"Imported {title}: auto-calibrated, please confirm the gripper, tool point and actuators."},
                "label": f"imported {title}"[:48], "license": lic, "warnings": c.get("warnings") or [],
                "confirm": ["gripper", "tcp", "actuators"], "actuators": {n: a["key"] for n, a in c["actuators"].items()},
                "gripper": (c.get("gripper") or {}).get("joint"), "tcp": c.get("tcp"), "format": c.get("format"),
                "picked_motors": bool(answers.get("actuators") or answers.get("family"))}


# ------------------------------------------------------------------- helpers
def _public(det: dict) -> dict:
    """Detection result without internal fields."""
    out = dict(det)
    out["candidates"] = [{k: v for k, v in c.items() if not k.startswith("_")} for c in det["candidates"]]
    return out


def _cand(det: dict, index) -> dict:
    try:
        i = int(index)
    except (TypeError, ValueError):
        raise ImportFailed("Pick a candidate by its number.") from None
    if not 0 <= i < len(det["candidates"]):
        raise ImportFailed("No such candidate; run detection again.")
    return dict(det["candidates"][i])


def _ctype(fmt: str) -> str:
    return {"glb": "model/gltf-binary", "png": "image/png"}[fmt]


def _title(rel: str, url: str | None) -> str:
    stem = re.sub(r"(\.urdf)?(\.xacro)?(\.urdf|\.xml)?$", "", os.path.basename(rel), flags=re.I)
    if url:
        m = re.match(r"https://(?:github\.com|huggingface\.co(?:/(?:datasets|spaces))?)/[^/]+/([^/?#]+)", url)
        if m:
            return f"{m.group(1)} ({stem})"[:80]
    top = rel.split("/")[0] if "/" in rel else ""
    if top and top.lower() not in stem.lower():
        return f"{top} ({stem})"[:80]
    return stem.replace("_", " ")[:80] or "Imported robot"


def _joint_list(urdf_path: str) -> list:
    from .urdf import joints_of
    return [{"name": n, "type": j["type"], "lower": round(j["lower"], 4), "upper": round(j["upper"], 4)}
            for n, j in joints_of(urdf_path).items() if j["type"] != "fixed" and not j["mimic"]]


def _mesh_sources(rep0: dict, src: str, bundle: str) -> list:
    """Real paths of the meshes a robot uses (so they aren't offered again as objects to grasp)."""
    from .urdf import packages, resolve_mesh
    from .common import parse_xml
    out = []
    if src.lower().endswith(".xml"):
        return out
    try:
        root = parse_xml(src)
    except ImportFailed:
        return out
    pk = packages(bundle)
    for m in root.iter("mesh"):
        real, _ = resolve_mesh(m.get("filename", ""), os.path.dirname(os.path.realpath(src)), bundle, pk)
        if real:
            out.append(real)
    return out


def _included(root: str, f: dict, xacros: list) -> bool:
    """A xacro included by another xacro is a macro library, not a robot to build."""
    base = os.path.basename(f["path"])
    for o in xacros:
        if o is f:
            continue
        try:
            with open(os.path.join(root, *o["path"].split("/")), encoding="utf-8", errors="replace") as fh:
                text = fh.read(2_000_000)
        except OSError:
            continue
        if re.search(r"<xacro:include[^>]*filename=\"[^\"]*" + re.escape(base) + "\"", text):
            return True
    return False


ANSWER_KEYS = {"gripper", "arm_joints", "actuators", "family", "accept_suggestions", "mesh_scale", "yaw_deg", "title",
               "units", "density_g_cc", "name", "table", "shape"}


def _check_answers(a: dict) -> dict:
    """Answers come from the browser or the CLI: plain values only, checked again where they are used."""
    if not isinstance(a, dict):
        raise ImportFailed("Answers must be an object.")
    bad = set(a) - ANSWER_KEYS
    if bad:
        raise ImportFailed(f"Unknown answer {sorted(bad)[0]!r}.")
    out = {}
    for k, v in a.items():
        if v is None or v == "":
            continue
        if k == "actuators":
            if not isinstance(v, dict) or not all(isinstance(x, str) and isinstance(y, str) for x, y in v.items()):
                raise ImportFailed("actuators must map joint names to actuator keys.")
            v = {x[:64]: y[:32] for x, y in v.items()}
        elif k == "arm_joints":
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v) or len(v) > 12:
                raise ImportFailed("arm_joints must be a list of joint names.")
        elif k in ("mesh_scale", "yaw_deg", "density_g_cc"):
            try:
                v = float(v)
            except (TypeError, ValueError):
                raise ImportFailed(f"{k} must be a number.") from None
            if k == "mesh_scale" and not 1e-4 <= v <= 1e4 or k == "density_g_cc" and not 0.01 <= v <= 25 or \
                    k == "yaw_deg" and not -360 <= v <= 360:
                raise ImportFailed(f"{k} is out of range.")
        elif k == "accept_suggestions":
            v = bool(v)
        elif k == "table":
            try:
                v = int(v)
            except (TypeError, ValueError):
                raise ImportFailed("table must be a number.") from None
        elif k == "family":
            if v not in ("hobby_pwm", "smart_bus", "dynamixel"):
                raise ImportFailed("family must be hobby_pwm, smart_bus or dynamixel.")
        elif k == "units":
            if v not in ("mm", "cm", "m", "in"):
                raise ImportFailed("units must be mm, cm, m or in.")
        else:
            if not isinstance(v, str) or len(v) > 80:
                raise ImportFailed(f"{k} must be a short text.")
        out[k] = v
    return out


def unique_project(name: str, home_dir: str | None = None) -> str:
    root = home_dir or home()
    base = slug(name, maxlen=36)
    out, k = base, 2
    while os.path.exists(os.path.join(root, out)):
        out = f"{base}-{k}"
        k += 1
    if not PROJECT_RX.match(out):
        raise ImportFailed("Couldn't make a valid project name; type a shorter one.")
    return out


def plan_design(plan: dict, name: str):
    """The Design a robot or project plan builds, named `name`: an imported robot carries the default egg-picking task
    on its own body; a Morph export rebuilds its latest design."""
    from ..spec import Design

    if plan["kind"] == "robot":
        try:
            from ..cli import EGG_ROBOT
            base = {k: v for k, v in EGG_ROBOT.items() if k != "name"}
        except Exception:  # noqa: BLE001
            base = dict(EGG)
        return Design().with_changes({**base, **plan["changes"], "name": name})
    return Design.from_dict(plan["design"]).with_changes({"name": name, **(plan.get("changes") or {})})


def build(plan: dict, home_dir: str | None = None, on_event=None, fast_sim: bool = False, n_trials: int = 2,
          verbose: bool = False, session_id: str | None = None, created_by: str | None = None) -> dict:
    """Create the project for a robot or project plan and run the pipeline once. Returns
    {"project", "rev", "summary"}. The CLI's path; in the Studio, api_import queues the build in the new project's room."""
    from ..robot.pipeline2 import RobotProject

    if plan.get("kind") not in ("robot", "project"):
        raise ImportFailed("Only robots and projects become new projects.")
    root_home = os.path.abspath(home_dir or home())
    if home_dir and os.path.abspath(home_dir) != home():
        set_home(home_dir)                 # the pipeline resolves arm.source against MORPH_HOME/.robots
    os.makedirs(root_home, exist_ok=True)
    for _ in range(20):                    # two imports may race for the same name: the first makedirs wins
        name = unique_project(plan.get("project") or plan.get("source") or "robot", root_home)
        root = os.path.join(root_home, name)
        try:
            os.makedirs(root)
            break
        except FileExistsError:
            continue
    else:
        raise ImportFailed("Couldn't create the project folder; try another name.")
    design = plan_design(plan, name)
    if on_event:                           # contracts section 3: every event carries its project
        raw_event = on_event

        def on_event(ev, _send=raw_event):
            ev = {**ev, "project": ev.get("project") or name}
            if session_id and ev.get("type") == "import":
                ev.setdefault("id", session_id)
            _send(ev)
    meta = {**plan.get("meta", {}), "kind": "robot", "created_utc": utcnow()}
    if created_by:
        meta["created_by"] = str(created_by)[:64]
    with open(os.path.join(root, "meta.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=1)
    if plan["kind"] == "project":
        for i, d in enumerate(plan.get("history") or []):
            with open(os.path.join(root, f"imported_design_{i + 1:03d}.json"), "w", encoding="utf-8") as fh:
                json.dump(d, fh, indent=1)
    if on_event:
        on_event({"type": "import", "stage": "build", "status": "start", "text": f"Building {name}", "project": name})
    p = RobotProject(root)
    try:  # categories and counts only (keewano.events_for, when Keewano events are on)
        p.log(ledger_entry(plan, "cli"))
    except OSError:
        pass
    rev = p.run(design, label=plan.get("label") or "imported", n_trials=n_trials, verbose=verbose,
                on_event=on_event, fast_sim=fast_sim)
    res = p.load_results(rev)
    if on_event:
        on_event({"type": "project_created", "project": name, "origin": "import"})
    return {"project": name, "rev": rev, "summary": res.get("summary") or {}, "provenance": res.get("provenance")}


def cli_import(path_or_url: str, name=None, as_=None, answers=None, dry_run=False, home_dir=None, on_event=None,
               fast_sim=False, build_project=True) -> dict:
    if as_ == "part":
        raise ImportFailed("Importing a mesh as a custom printed part isn't in this release; import it as the object "
                           "to grasp (--as object) instead.")
    if as_ not in (None, "robot", "object", "bom", "board", "project"):
        raise ImportFailed("--as must be robot, object, bom, board or project.")
    h = os.path.abspath(home_dir or home())
    if home_dir:
        set_home(h)
    cleanup(h)
    s = Session.create(h, url=path_or_url if re.match(r"^[a-z]+://", path_or_url or "") else None)
    try:
        if re.match(r"^[a-z]+://", path_or_url or ""):
            s.fetch(path_or_url, on_event)
        else:
            s.add_path(path_or_url)
        det = s.detect(on_event)
        cands = [c for c in det["candidates"] if as_ is None or c["kind"] == as_]
        if not cands:
            raise ImportFailed(f"No {as_} found here." if as_ else "Nothing to import.")
        c = next((x for x in cands if not x.get("error")), cands[0])   # robots first, even when they need answers
        out = {"ok": True, "kind": c["kind"], "candidate": c, "candidates": det["candidates"],
               "warnings": det["warnings"] + c.get("warnings", []), "refused": det["refused"]}
        if dry_run:
            out["dry_run"] = True
            return out
        res = s.commit(c["index"], name, answers or {}, on_event)
        out["result"] = res
        if res["kind"] in ("robot", "project") and build_project:
            out.update(build(res, h, on_event=on_event, fast_sim=fast_sim))
        return out
    finally:
        s.delete()
