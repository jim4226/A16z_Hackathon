"""Robot projects in MORPH_HOME: list, create, fork and delete them, pick their names, keep their meta.json.

One place for the Studio, the CLI and feature modules (a brief, an import) to make projects. Nothing here builds:
the Studio queues a new project's first build in its room, the CLI runs it directly.

    meta.json  {"kind": "robot", "source", "need", "origin": "default" | "brief" | "import" | "fork",
                "demo": bool, "created_utc", "created_by"?, "forked_from"?: {"project", "rev"}, ...}

Only projects marked demo (the launcher's egg-picker and so101) are touched by Reset demo. Deleting a project moves
it to MORPH_HOME/.trash/<name>-<UTC time>/; nothing is erased.
"""

from __future__ import annotations

import dataclasses
import json
import math
import os
import re
import secrets
import shutil
import threading
import time
import traceback
import unicodedata

from ..pipeline import REV, utc_now
from ..robot.pipeline2 import ORDER, PARAMS, RobotProject, artifact_problem, pipeline_version, stage_problem
from ..services import durable
from ..spec import Design
from .security import WINDOWS_RESERVED, contained_path, design_values, param_changes, project_name, robot_source

EGG_ROBOT = {"name": "egg-picker", "object.name": "raw egg", "object.shape": "cylinder", "object.diameter_mm": 44.0,
             "object.length_mm": 57.0, "object.mass_g": 60.0, "object.friction": 0.35, "object.max_force_n": 25.0,
             "finger.sensor": "fsr402", "finger.width_mm": 20.0, "finger.length_mm": 52.0, "finger.thickness_mm": 6.0,
             "finger.pad_depth_mm": 5.5, "finger.pad_height_mm": 19.8, "electronics.servo": "sts3215"}
NEEDS = {"": "Pick a raw egg and place it without cracking it.",
         "so101": "Imported open-source SO-101 arm: edit its electronics, sims and build."}
ORIGINS = ("default", "brief", "import", "fork")
DEMOS = ("egg-picker", "so101")
TRASH = ".trash"
UPLOADS = ".uploads"
OWN_META = ("kind", "created_utc", "created_by", "origin", "demo", "source")  # set from create()'s own arguments only
_LOCK = threading.Lock()  # two requests creating "egg-picker-2" at once get -2 and -3


def read_meta(root: str) -> dict:
    try:
        with open(os.path.join(root, "meta.json"), encoding="utf-8") as fh:
            meta = json.load(fh)
    except (OSError, ValueError):
        return {}
    return meta if isinstance(meta, dict) else {}


def write_meta(root: str, meta: dict):
    """meta.json, replaced atomically and synced to the disk (durable.write_json)."""
    durable.write_json(os.path.join(root, "meta.json"), meta)


def damaged_meta(root: str) -> bool:
    """meta.json is there but doesn't hold an object (a power cut while it was saved, a hand edit)."""
    path = os.path.join(root, "meta.json")
    return os.path.isfile(path) and not durable.readable_json(path)


def _robot_folder(root: str) -> bool:
    """Whether a folder whose meta.json can't be read is a Studio robot project: one of its revisions says so in its
    results (`kind: "robot"`), or the Studio keeps its chat or queue there (.morph). The earlier CLI prototype's gripper
    projects have neither."""
    if os.path.isdir(os.path.join(root, ".morph")):
        return True
    try:
        names = sorted((n for n in os.listdir(root) if REV.fullmatch(n)), key=lambda n: int(n[1:]), reverse=True)
    except OSError:
        return False
    for n in names[:20]:
        try:
            with open(os.path.join(root, n, "results.json"), encoding="utf-8") as fh:
                if (json.load(fh) or {}).get("kind") == "robot":
                    return True
        except (OSError, ValueError, AttributeError):
            continue
    return False


def project_root(home: str, name) -> str | None:
    """The folder of an existing robot project, or None: a bad name, missing, not a robot project, or (through a
    link) outside MORPH_HOME. A robot project whose meta.json can't be read still is one (repair_meta mends it when a
    Studio starts): it never disappears from the menu while its name stays taken."""
    try:
        project_name(name)
    except ValueError:
        return None
    root = contained_path(home, name, decode=False)
    if not root or not os.path.isdir(root):
        return None
    if read_meta(root).get("kind") != "robot" and not (damaged_meta(root) and _robot_folder(root)):
        return None
    return root


def repair_meta(root: str, say=None) -> bool:
    """A robot project's meta.json that can't be read is kept as meta.json.damaged-<time> and replaced by one made from
    what the project shows: kind robot, the robot source of its newest design, and demo for the launcher's egg-picker
    and so101 (so Reset demo still finds them). Its need, origin and brief or fork links are lost; the damaged copy
    keeps whatever can still be read. True when it repaired one."""
    if not damaged_meta(root) or not _robot_folder(root):
        return False
    name = os.path.basename(os.path.normpath(root))
    p = RobotProject(root)
    source = ""
    for rev in reversed(p.finished() or p.revs()):
        try:
            source = p.load_design(rev).arm.source or ""
            break
        except (OSError, ValueError, KeyError, TypeError):
            continue
    kept = f"meta.json.damaged-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    try:
        os.replace(os.path.join(root, "meta.json"), os.path.join(root, kept))
    except OSError:
        return False
    revs = p.revs()
    first = os.path.getmtime(p.rev_dir(revs[0])) if revs else os.path.getmtime(root)
    write_meta(root, {"kind": "robot", "source": source, "need": "", "origin": "default", "demo": name in DEMOS,
                      "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(first)),
                      "repaired_utc": utc_now(), "repaired_from": kept})
    if say:
        say(f"{name}: meta.json couldn't be read (damaged, or edited by hand); Morph wrote a new one from the project's "
            f"revisions and kept the old one as {kept}")
    return True


def finished(p: RobotProject) -> list:
    """Revisions with readable results (Project.finished: a revision being built, cut off or damaged doesn't count)."""
    return p.finished()


SET_ASIDE = re.compile(r"(r[0-9]{1,9})\.(incomplete|deleted)-([0-9]+)")
TRASH_AFTER_S = 24 * 3600


def set_aside_unfinished(p: RobotProject, say=None) -> list:
    """The newest revisions that never finished (a stop mid-build left only some of their files) or whose results.json
    or design.json can't be read (a power cut, a hand edit) are renamed rNNN.incomplete-<time>: nothing reads them as
    finished and the next build takes their number. A revision another process is still building (its BuildLock is
    held) is left alone. `say(text)` is told about each. Returns the revisions set aside."""
    done, out = set(p.finished()), []
    for rev in reversed(p.revs()):
        if rev in done:
            break
        lock = p.build_lock(rev)
        if durable.BuildLock.held(lock):
            break  # still building, in another Studio or the CLI
        d = p.rev_dir(rev)
        why = ("was cut off mid-build" if not os.path.isfile(os.path.join(d, "results.json")) else
               "has saved files that can't be read (a power cut or a crash while they were written, or a hand edit)")
        aside = f"{d}.incomplete-{time.time_ns()}"  # ns: the same revision set aside twice in a second never collides
        try:
            os.rename(d, aside)
        except OSError:
            break  # Windows: a file in it is open; next start
        try:
            os.remove(lock)
        except OSError:
            pass
        out.append(rev)
        if say:
            say(f"{os.path.basename(p.root)}: {rev} {why}; setting it aside")
    return out


# A first build the Studio stopped during more than once (Studio._recover) isn't started again on its own: if something
# in the design stops the Studio (memory, a crash in a native library), it would stop it at every start. Neither is one
# that ended with an error (Studio._failed_first). It waits here until someone asks for it again (chat "build it
# again") or the project is deleted.
FIRST_BUILD = os.path.join(".morph", "first-build.json")


def parked_first_build(root: str) -> dict | None:
    """{design, label, by, cut, utc, and error for one that ended with an error} of a first build that waits to be
    asked for again, or None."""
    try:
        with open(os.path.join(root, FIRST_BUILD), encoding="utf-8") as fh:
            rec = json.load(fh)
    except (OSError, ValueError):
        return None
    return rec if isinstance(rec, dict) and isinstance(rec.get("design"), dict) else None


def set_aside_design(root: str) -> dict | None:
    """The design of the newest revision of project folder `root` set aside unfinished (rNNN.incomplete-<time>, kept a
    day before it goes to the trash), or None: in a project with no finished revision, the first design a stop or an
    error cut off, when no journal or record kept it (a full disk, a power cut in between)."""
    best = None
    for entry in os.listdir(root) if os.path.isdir(root) else []:
        m = SET_ASIDE.fullmatch(entry)
        if not m or m.group(2) != "incomplete":
            continue
        try:
            with open(os.path.join(root, entry, "design.json"), encoding="utf-8") as fh:
                design = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(design, dict) and (best is None or int(m.group(3)) > best[0]):
            best = (int(m.group(3)), design)
    return best[1] if best else None


def park_first_build(root: str, record: dict):
    durable.write_json(os.path.join(root, FIRST_BUILD), {**record, "utc": utc_now()})


def unpark_first_build(root: str):
    try:
        os.remove(os.path.join(root, FIRST_BUILD))
    except OSError:
        pass


def _folder_size(path: str) -> int:
    total = 0
    for folder, _, names in os.walk(path):
        for n in names:
            try:
                total += os.lstat(os.path.join(folder, n)).st_size
            except OSError:
                pass
    return total


def tidy(home: str, say=None, *, now: float | None = None) -> dict:
    """What a Studio does for every robot project when it starts: repair_meta(), set_aside_unfinished(); revision folders
    set aside more than a day ago go to .trash/<project>-<rev>-<UTC time>/ (they hold only files a build can make again:
    empty .trash to get the space back); the leftovers of a deleted revision a locked file kept are deleted. Never
    raises: one odd project never stops the Studio. Returns {"set_aside": {project: [revs]}, "trashed": n, "bytes":
    size moved}."""
    now = time.time() if now is None else now
    out = {"set_aside": {}, "trashed": 0, "bytes": 0}
    for info in list_projects(home):
        name, root = info["name"], os.path.join(home, info["name"])
        try:
            repair_meta(root, say)
            p = RobotProject(root)
            gone = set_aside_unfinished(p, say)
            if gone:
                out["set_aside"][name] = gone
            for entry in sorted(os.listdir(root)):
                m = SET_ASIDE.fullmatch(entry)
                path = os.path.join(root, entry)
                if not m or not os.path.isdir(path) or os.path.islink(path):
                    continue
                if m.group(2) == "deleted":  # meant to be erased already
                    shutil.rmtree(path, ignore_errors=True)
                    continue
                stamp = int(m.group(3))
                stamp = stamp / 1e9 if stamp > 1e12 else stamp
                if now - stamp < TRASH_AFTER_S:
                    continue
                size = _folder_size(path)
                trash = os.path.join(home, TRASH)
                os.makedirs(trash, exist_ok=True)
                when = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(stamp))
                dst, n = os.path.join(trash, f"{name}-{m.group(1)}-{when}"), 1
                while os.path.lexists(dst):
                    n += 1
                    dst = os.path.join(trash, f"{name}-{m.group(1)}-{when}-{n}")
                os.rename(path, dst)
                out["trashed"] += 1
                out["bytes"] += size
            for lock in _stale_locks(p):
                try:
                    os.remove(lock)
                except OSError:
                    pass
        except Exception:  # noqa: BLE001  (one odd project never stops the Studio)
            traceback.print_exc()
    if out["trashed"] and say:
        size = f"{out['bytes'] / 1e6:.0f} MB" if out["bytes"] >= 0.5e6 else "<1 MB"
        say(f"Moved {out['trashed']} build{'s' if out['trashed'] != 1 else ''} the Studio had stopped part way, "
            f"{size}, to {os.path.join(home, TRASH)}: empty it to free the space.")
    return out


def _stale_locks(p: RobotProject) -> list:
    """Build locks left by builds that ended with their process (none holds them, and their revision is finished or
    gone)."""
    folder = os.path.join(p.root, ".morph")
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    revs = set(p.revs())
    out = []
    for n in names:
        m = re.fullmatch(r"build-(r[0-9]{1,9})\.lock", n)
        if not m:
            continue
        path = os.path.join(folder, n)
        if m.group(1) in revs and not os.path.isfile(os.path.join(p.rev_dir(m.group(1)), "results.json")):
            continue  # an unfinished revision: its lock says whether it still builds
        if not durable.BuildLock.held(path):
            out.append(path)
    return out


def list_projects(home: str) -> list:
    """Every robot project, by name: {name, source, need, origin, demo, created_utc, revs}. `revs` counts finished
    revisions, so a project whose first build is still running shows 0."""
    out = []
    for name in sorted(os.listdir(home)) if os.path.isdir(home) else []:
        root = project_root(home, name)  # skips .folders, odd names and links out of the projects folder
        if not root:
            continue
        m = read_meta(root)
        out.append({"name": name, "source": m.get("source") or "", "need": m.get("need") or "",
                    "origin": m.get("origin") or "default", "demo": m.get("demo") is True,
                    "created_utc": m.get("created_utc"), "revs": len(finished(RobotProject(root)))})
    return out


def slug(text, fallback: str = "robot") -> str:
    """A project name from any text: "Strawberry Picker!" -> "strawberry-picker" (at most 40 characters)."""
    text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40].strip("-")
    if not s:
        s = fallback
    if s in WINDOWS_RESERVED or REV.fullmatch(s):  # "con" can't be a folder on Windows; "r001" looks like a revision
        s = f"{s}-robot"
    return project_name(s)


def unique_name(home: str, base: str) -> str:
    """`base`, or base-2, base-3, ...: the first name nothing in MORPH_HOME uses yet."""
    name, n = base, 1
    while os.path.lexists(os.path.join(home, name)):
        n += 1
        suffix = f"-{n}"
        name = base[:40 - len(suffix)].rstrip("-") + suffix
    return name


def _check_source(source: str):
    robot_source(source)
    from ..importers.robots import robot_dir
    robot_dir(source)  # ValueError: no robot bundle of that name


def as_design(design) -> Design:
    """A Design from a Design or a design dict; ValueError for anything Morph can't read, including keys a Design
    doesn't have (a typo would otherwise build the default arm)."""
    if isinstance(design, Design):
        return design
    if not isinstance(design, dict):
        raise ValueError("Send the design as an object.")
    unknown = sorted(str(k) for k in set(design) - {f.name for f in dataclasses.fields(Design)})
    if unknown:
        raise ValueError(f"That isn't a Morph design: it has no {unknown[0]}.")
    try:
        return Design.from_dict(json.loads(json.dumps(design)))
    except (TypeError, ValueError, KeyError, RecursionError) as exc:
        raise ValueError(f"That isn't a Morph design: {exc}") from None


CHECKED = {key for key, *_ in PARAMS} - {"object.name", "electronics.extras"}  # custom objects and extras are allowed
DEFAULTS = Design().flat()  # each value's type


def check_design(design: Design):
    """ValueError when a design from outside (an API request) sets an editor parameter out of its range, or any value to
    something no robot of Morph's can have (security.design_values)."""
    flat = design.flat()
    design_values(flat, DEFAULTS)
    for key in sorted(CHECKED):
        value = flat.get(key)
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"{key}: send a finite number.")
        param_changes({key: value}, PARAMS)


def template(name: str, source: str = "") -> Design:
    """The default egg-picking arm, on an imported robot's body when `source` names one (so101)."""
    if source:
        _check_source(source)
    return Design().with_changes({**EGG_ROBOT, "name": name, "arm.source": source})


def create(home: str, name=None, *, source: str = "", design=None, need: str | None = None, origin: str = "default",
           demo: bool = False, by: str | None = None, unique: bool = True, rename: bool = True,
           meta: dict | None = None) -> dict:
    """Make a project folder and its meta.json; build nothing. Returns {"name", "root", "design", "meta"}.

    `design` (a Design or a design dict) replaces the default arm; its arm.source must name a known robot. The design
    takes the project's name unless rename=False. Names: with unique=True `name` (any text) is slugged and gets
    -2, -3, ... when taken; with unique=False it must already be a valid name, and a taken name is an error unless
    that folder has no revisions yet (a first build that failed can be tried again). `meta` adds keys to meta.json but
    never sets those in OWN_META. ValueError for a bad name, design (security.design_values) or source;
    FileExistsError for a taken name."""
    if origin not in ORIGINS:
        raise ValueError(f"origin must be one of {', '.join(ORIGINS)}")
    if design is not None:
        design = as_design(design)
        design_values(design.flat(), DEFAULTS)
        source = design.arm.source
    if source:
        _check_source(source)
    default = design.name if design is not None else (source or "egg-picker")
    with _LOCK:
        os.makedirs(home, exist_ok=True)
        name = unique_name(home, slug(name or default)) if unique else project_name(name)
        root = contained_path(home, name, decode=False)
        if root is None:
            raise ValueError(f"{name} can't be a project folder here.")
        if os.path.lexists(root) and (unique or not os.path.isdir(root) or RobotProject(root).revs()):
            raise FileExistsError(f"{root} already exists")
        os.makedirs(root, exist_ok=True)
        if design is None:
            design = template(name, source)
        elif rename:
            design = design.with_changes({"name": name})
        info = {"kind": "robot", "source": source, "need": need if need is not None else NEEDS.get(source, ""),
                "origin": origin, "demo": bool(demo), "created_utc": utc_now(), **({"created_by": by} if by else {})}
        info.update({k: v for k, v in (meta or {}).items() if k not in OWN_META})
        write_meta(root, info)
    return {"name": name, "root": root, "design": design, "meta": info}


def _copy_tree(src: str, dst: str):
    """A revision folder, without hidden files and without links (a link could point outside the projects)."""
    skip = lambda d, names: [n for n in names if n.startswith(".") or os.path.islink(os.path.join(d, n))]  # noqa: E731
    shutil.copytree(src, dst, symlinks=False, ignore=skip)


def fork(home: str, project: str, rev: str | None = None, name=None, *, by: str | None = None) -> dict:
    """A new project from `project`'s revision `rev` (default: its newest finished one), with the same design and meta
    and origin "fork". When that revision was built by this version of Morph and every stage is valid, its files are
    copied as the fork's r001 (about a second); otherwise the caller builds the design. Returns create()'s dict plus
    "copied" and "rev" (r001 when copied, else None). LookupError: no such project or revision."""
    src = project_root(home, project)
    if not src:
        raise LookupError(f"There is no robot project called {project}.")
    p = RobotProject(src)
    revs = finished(p)
    rev = rev or (revs[-1] if revs else None)
    if rev not in revs:
        raise LookupError(f"{project} has no finished revision {rev}.")
    design, results = p.load_design(rev), p.load_results(rev)
    source = read_meta(src)
    info = create(home, name or project, design=design, need=source.get("need"), origin="fork", by=by, rename=False,
                  meta={"forked_from": {"project": project, "rev": rev}})
    room = os.path.join(src, "world")  # world mode: the room the design names (world.id) comes along
    if design.world.id and os.path.isdir(room) and not os.path.islink(room):
        _copy_tree(room, os.path.join(info["root"], "world"))
    folder = p.rev_dir(rev)
    valid = results.get("pipeline_version") == pipeline_version(design.arm.source) and not any(
        stage_problem(results, n) or artifact_problem(results, n, folder) for n in ORDER)
    info.update(copied=False, rev=None)
    if not valid:
        return info
    new = RobotProject(info["root"])
    _copy_tree(folder, new.rev_dir("r001"))
    label = f"fork of {project} {rev}"
    origin = f"copied from {project} {rev}"
    results.update(rev="r001", parent=None, label=label, changes={}, timings_s={}, forked_from={"project": project, "rev": rev},
                   provenance={n: origin for n in ORDER}, stage_status={n: "reused" for n in ORDER})
    results.pop("by", None)
    if by:
        results["by"] = by
    durable.write_json(os.path.join(new.rev_dir("r001"), "results.json"), results)
    new.log({"type": "revision", "rev": "r001", "parent": None, "label": label, "changes": {}, "recomputed": [],
             "design": design.to_dict(), "predicted": results.get("summary") or {},
             "forked_from": {"project": project, "rev": rev}, **({"by": by} if by else {})})
    info.update(copied=True, rev="r001")
    return info


def delete(home: str, name: str) -> str:
    """Move a robot project to MORPH_HOME/.trash/<name>-<UTC time>/ and return that folder. LookupError: no such
    project; OSError: the move failed (on Windows, a file in it is open)."""
    root = project_root(home, name)
    if not root:
        raise LookupError(f"There is no robot project called {name}.")
    trash = os.path.join(home, TRASH)
    os.makedirs(trash, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    dst, n = os.path.join(trash, f"{name}-{stamp}"), 1
    while os.path.lexists(dst):
        n += 1
        dst = os.path.join(trash, f"{name}-{stamp}-{n}")
    os.rename(root, dst)
    return dst


def upload_path(home: str, suffix: str = "") -> str:
    """A new, unused file path in MORPH_HOME/.uploads/ for a raw upload (Request.save writes it). Move the file where it
    belongs, or delete it, when done: the Studio clears what is left (clear_uploads) when it starts."""
    folder = os.path.join(home, UPLOADS)
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, secrets.token_hex(12) + re.sub(r"[^A-Za-z0-9.]", "", str(suffix))[:16])


def clear_uploads(home: str, older_than: float = 3600) -> int:
    """Remove what earlier runs left in MORPH_HOME/.uploads/ (anything older than `older_than` seconds, so an upload
    another Studio on the same folder is receiving right now survives). Returns how many entries went."""
    folder = os.path.join(home, UPLOADS)
    gone = 0
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return 0
    for e in entries:
        try:
            if time.time() - e.stat(follow_symlinks=False).st_mtime < older_than:
                continue
            if e.is_dir(follow_symlinks=False):
                shutil.rmtree(e.path)
            else:
                os.remove(e.path)
            gone += 1
        except OSError:
            pass  # open elsewhere (Windows): next start
    return gone


def backfill_demo(home: str, names=DEMOS) -> list:
    """Projects the launcher made before 0.2 have no demo flag: mark egg-picker and so101 as demo projects (with the
    origin and creation time they lack), so Reset demo keeps working for them and never touches anything else.
    Returns the names it marked."""
    done = []
    for name in names:
        root = project_root(home, name)
        if not root:
            continue
        repair_meta(root, print)  # never written over unread: the damaged one is kept, and the new one has demo set
        m = read_meta(root)
        if "demo" in m:
            continue
        m.setdefault("origin", "default")
        m["demo"] = True
        m.setdefault("created_utc", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(os.path.getmtime(root))))
        write_meta(root, m)
        done.append(name)
    return done
