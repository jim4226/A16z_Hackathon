"""Robustness studies (beta): how often a revision's pick and place succeeds when the world is not exactly as designed.

One nominal simulation says whether the design works once, exactly as drawn. A study runs the revision's own nominal
plan (planned once, as the Studio's simulation plans it) many times in worlds drawn from the project's tolerance spec:
the object off its mark and turned, its size and mass, pad friction, grip-force calibration, servo torque and gain,
deadband, encoder noise, the host's set-point rate and, in World mode, the mount and every box. Each run is labelled
(placed; jam, drag, missed grasp, dropped, over-force, out of reach, hit the room during a phase; numerically unstable
runs are listed and never counted), the success rate gets a Wilson 95 % interval (Clopper-Pearson when no run, or
every run, fails), each failure group gets the one-parameter rule that best isolates it (Youden's J, against what
shuffled outcomes reach by chance) and is re-run at half the timestep and at impratio=1: a group whose outcome changes
there is model-sensitive, a property of the simulator as much as of the design. This is simulation, for the tolerances
shown; never a field failure rate.

Runs are reproducible: run i of a study draws its world from SeedSequence([seed, i]) alone, so results do not depend
on the number of workers or the batch size, and a deeper study extends a shallower one. Workers are spawned processes
(every OS) that load the robot and plan once (packed by a short-lived preparing process) and then run many trials.
Studies only read a revision; they never change it or any stage hash. Results live in <project>/.studies/<id>/:
study.json (definition, status, timing, summary), runs.jsonl and checks.jsonl (one row per run); the project's edited
tolerances in <project>/.studies/tolerances.json.

  default_spec(design, world=None) / tolerances(root, rev) / save_tolerances(root, changes, rev=None, by=None)
  run_study(root, rev=None, *, runs, seed, stress, workers, timestep, checks, cancel, progress, by) -> study
  load(root, sid, rows=False) / studies(root) / replay(root, sid, run) / report(study) -> ASCII text
  wilson(k, n) / clopper_pearson(k, n) / analyze(rows, params, checks=None)
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import multiprocessing as mp
import os
import pickle
import re
import signal
import socket
import sys
import tempfile
import threading
import time
import traceback
from collections import deque
from multiprocessing.connection import wait as _wait
from types import SimpleNamespace

import numpy as np

VERSION = 1
SCENARIO = "pick_place"
STUDIES = ".studies"
ID = re.compile(r"[0-9a-f]{12}")
REV = re.compile(r"r\d{3,6}")
Z95 = 1.959963984540054
STALE_S = 30.0                      # a running study whose heartbeat is older than this was interrupted
RISE_MM = 10.0                      # the object rose this far after the release: the jaws stayed shut on it,
CARRIED_MM = 45.0                   # or it ended this far off the target (only closed jaws carry it that far)
DEFAULT_SEED = 1                    # the seed of the published egg-picker study (150 of 200)
CHECK_RUNS, CHECK_SUCCESSES = 20, 15
CACHED = 4                          # packed robots a long-running process keeps (about 5 MB each)
NOTE = "Simulation, for the tolerances shown; not a field failure rate."
SENSITIVE = "This result changes with the simulator's friction or timestep settings. Test it on the real robot."
SUCCESSES_SENSITIVE = ("Some runs that succeed here fail when re-run at half the timestep or with impratio=1: the success "
                       "rate depends on the simulator's settings as well as the design. Test it on the real robot.")
# A bad position, velocity, acceleration or control makes MuJoCo reset the run to its start (and its warning counters,
# all but that one), and a full contact or constraint buffer drops contacts: either way the run is not counted.
UNSTABLE = ("mjWARN_BADQPOS", "mjWARN_BADQVEL", "mjWARN_BADQACC", "mjWARN_BADCTRL", "mjWARN_CONTACTFULL",
            "mjWARN_CNSTRFULL")
WARNINGS = UNSTABLE + ("mjWARN_INERTIA",)


class StudyError(ValueError):
    """A study can't run (no such revision, no pick-and-place simulation, bad tolerances)."""


class StudyBusy(StudyError):
    """This project already has a study running."""


class StudyConflict(StudyError):
    """The revision or the study can't be used as asked (nothing to study in it, replaced since, other Morph code)."""


REPLACED = ("This study's revision was replaced (Reset demo or a deleted revision): the name now holds another design, so "
            "its runs can't be carried on or replayed. Run a new study.")
CODE_CHANGED = ("Morph's simulation code changed since this study started: carrying it on would mix two versions in one "
                "study. Run a new study.")
MAX_WORKERS = 256


# ====================================================================== tolerances

# name, label, unit. The order is the sampling order: never reorder or insert (a study's runs depend on it).
BASE = (("object_across_mm", "Object off its mark, across the jaws", "mm"),
        ("object_along_mm", "Object off its mark, along its length", "mm"),
        ("object_yaw_deg", "Object turned on the table", "deg"),
        ("object_size", "Object size", "x"),
        ("object_mass_g", "Object mass", "g"),
        ("pad_friction", "Pad friction", "x"),
        ("grip_force", "Grip-force calibration", "x"),
        ("servo_torque", "Servo torque and gain", "x"),
        ("servo_deadband_deg", "Servo deadband and backlash (half-width)", "deg"),
        ("encoder_noise", "Encoder noise", "counts"),
        ("setpoint_hz", "Host set-point rate", "Hz"))
WORLD = (("mount_dx_mm", "Robot mount off, x", "mm"),
         ("mount_dy_mm", "Robot mount off, y", "mm"),
         ("mount_yaw_deg", "Robot mount turned", "deg"),
         ("box_error_mm", "Each box off its measured place", "mm"))
NAMES = [n for n, _, _ in BASE + WORLD]
# what an edit may ask for (mass: times the nominal mass)
LIMITS = {"object_across_mm": (-100, 100), "object_along_mm": (-100, 100), "object_yaw_deg": (-180, 180),
          "object_size": (0.3, 3.0), "object_mass_g": (0.1, 10.0), "pad_friction": (0.05, 5.0), "grip_force": (0.05, 5.0),
          "servo_torque": (0.05, 2.0), "servo_deadband_deg": (0.0, 10.0), "encoder_noise": (0.0, 100.0),
          "setpoint_hz": (1.0, 1000.0), "mount_dx_mm": (-200, 200), "mount_dy_mm": (-200, 200), "mount_yaw_deg": (-45, 45),
          "box_error_mm": (0.0, 200.0)}
# stress mode doubles every range about its nominal, never below these (the mass: a tenth of the nominal)
FLOOR = {"object_size": 0.5, "pad_friction": 0.1, "grip_force": 0.1, "servo_torque": 0.1, "servo_deadband_deg": 0.0,
         "encoder_noise": 0.0, "setpoint_hz": 5.0, "box_error_mm": 0.0}
ENCODER = {"smart_bus": 4096, "dynamixel": 4096, "hobby_pwm": 1024}


def _p(name, lo, hi, nominal, source) -> dict:
    label, unit = next((lb, u) for n, lb, u in BASE + WORLD if n == name)
    return {"name": name, "label": label, "unit": unit, "lo": float(lo), "hi": float(hi), "nominal": float(nominal),
            "source": source, "on": True}


def _standard_world(world, design) -> list:
    return [_p("mount_dx_mm", -10, 10, 0, "assumed"), _p("mount_dy_mm", -10, 10, 0, "assumed"),
            _p("mount_yaw_deg", -2, 2, 0, "assumed"),
            _p("box_error_mm", 0, 15, 0, "assumed: how well each box's place was measured")]


# World-tolerance presets: name -> fn(sim world, design) -> the four world entries; "standard" is the default. A preset
# in ROOM_ONLY holds every other tolerance at its nominal value (switched off: it still draws, so runs stay comparable),
# so a study's failures are the room's: "room" is the standard ranges that way.
WORLD_PRESETS = {"standard": _standard_world}
ROOM_ONLY = set()
PRESET_LABELS = {"standard": "Everything varies: the object, the hardware, the mount and every box"}


def register_world_preset(name: str, fn, label: str | None = None, room_only: bool = False):
    WORLD_PRESETS[name] = fn
    if label:
        PRESET_LABELS[name] = label
    (ROOM_ONLY.add if room_only else ROOM_ONLY.discard)(name)


register_world_preset("room", _standard_world, "Only the room varies: the mount and every box; the object and the "
                      "hardware stay nominal", room_only=True)


def default_spec(design, world=None, preset: str = "standard") -> dict:
    """The default tolerances for a design (and its room), each with where its range comes from."""
    o, fam = design.object, design.arm.actuator_family
    egg = "egg" in (o.name or "").lower() and 40.0 <= o.mass_g <= 80.0
    sts = fam == "smart_bus"
    m = float(o.mass_g)
    params = [
        _p("object_across_mm", -8, 8, 0, "assumed: the object is set down by hand"),
        _p("object_along_mm", -8, 8, 0, "assumed: the object is set down by hand"),
        _p("object_yaw_deg", -15, 15, 0, "assumed"),
        _p("object_size", 0.92, 1.08, 1.0, "USDA egg weight classes, medium to jumbo" if egg else "assumed"),
        _p("object_mass_g", 50 if egg else round(0.85 * m, 1), 72 if egg else round(1.15 * m, 1), m,
           "USDA egg weight classes, medium (50 g) to jumbo (72 g)" if egg else "assumed: +/-15 %"),
        _p("pad_friction", 0.7, 1.3, 1.0, "assumed"),
        _p("grip_force", 0.75, 1.25, 1.0, "FSR 400 datasheet: +/-6 % part to part, +10 % hysteresis"
           if str(design.finger.sensor).startswith("fsr") else "assumed"),
        _p("servo_torque", 0.7, 1.0, 1.0, "ST3215: 16.5 kg-cm at 6 V vs 19.5 at 7.4 V" if sts
           else "assumed: supply sag and heat"),
        _p("servo_deadband_deg", 0, 0.5, 0, "ST3215: backlash 0.5 deg or less" if sts else "assumed"),
        _p("encoder_noise", 0, 1.5, 0, f"{ENCODER.get(fam, 4096)} counts per turn" + (" (ST3215)" if sts else "")),
        _p("setpoint_hz", 30, 200, 1000, "assumed: the host streams set-points and each servo holds the last one"),
    ]
    if world is not None:
        params += WORLD_PRESETS.get(preset, _standard_world)(world, design)
        if preset in ROOM_ONLY:
            for e in params[:len(BASE)]:
                e["on"] = False
    return {"version": VERSION, "params": params, "world_preset": preset if world is not None else None,
            "encoder_cpr": ENCODER.get(fam, 4096), "object": (o.name or "object")}


def _checked(e: dict, lo, hi, on) -> dict:
    """An edited entry, or StudyError: finite numbers inside LIMITS, lo <= hi."""
    a, b = LIMITS[e["name"]]
    if e["name"] == "object_mass_g":
        a, b = a * e["nominal"], b * e["nominal"]
    out = dict(e)
    for key, v in (("lo", lo), ("hi", hi)):
        if v is None:
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not a <= v <= b:
            raise StudyError(f"{e['label']}: {key} must be a number from {a:g} to {b:g} {e['unit']}.")
        out[key] = float(v)
    if out["lo"] > out["hi"]:
        raise StudyError(f"{e['label']}: the low end is above the high end.")
    if on is not None:
        if not isinstance(on, bool):
            raise StudyError(f"{e['label']}: on is true or false.")
        out["on"] = on
    out["edited"] = (out["lo"], out["hi"], out["on"]) != (e["lo"], e["hi"], e["on"])
    return out


def merge(spec: dict, edits: dict | None) -> dict:
    """The default spec with saved edits ({name: {lo, hi, on}}) on top; an edit that no longer fits is left out."""
    out = copy.deepcopy(spec)
    for k, e in enumerate(out["params"]):
        ed = (edits or {}).get(e["name"])
        if isinstance(ed, dict):
            try:
                out["params"][k] = _checked(e, ed.get("lo"), ed.get("hi"), ed.get("on"))
            except StudyError:
                pass
    return out


def spec_sha(spec: dict) -> str:
    key = [[e["name"], e["lo"], e["hi"], e["nominal"], bool(e["on"])] for e in spec["params"]]
    return hashlib.sha256(json.dumps([key, spec.get("world_preset"), spec.get("encoder_cpr")]).encode()).hexdigest()[:16]


def stressed(spec: dict, scale: float = 2.0) -> dict:
    """Stress mode: every range widened `scale` times about its nominal (never below FLOOR)."""
    out = copy.deepcopy(spec)
    for e in out["params"]:
        lo, hi, nom = e["lo"], e["hi"], e["nominal"]
        if e["name"] == "setpoint_hz":
            lo = lo / scale
        elif lo <= nom <= hi:
            lo, hi = nom - scale * (nom - lo), nom + scale * (hi - nom)
        else:
            hi = lo + scale * (hi - lo)
        floor = 0.1 * nom if e["name"] == "object_mass_g" else FLOOR.get(e["name"], lo)
        e["lo"], e["hi"] = max(lo, floor), hi
    return out


def _tolerance_path(root) -> str:
    return os.path.join(root, STUDIES, "tolerances.json")


class TolerancesUnreadable(StudyConflict):
    """The project's tolerances.json is there but can't be read: studies would run on the defaults without a word, and
    the next edit would write over the edits it holds."""


def _edits(root) -> dict:
    """The project's tolerance edits; {} when there are none. TolerancesUnreadable when the file is there but can't be
    read: never silently the defaults (a reset writes a new file)."""
    try:
        with open(_tolerance_path(root), encoding="utf-8") as fh:
            doc = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        doc = None
    if not isinstance(doc, dict):
        raise TolerancesUnreadable("This project's tolerances (.studies/tolerances.json) can't be read (damaged, or "
                                   "edited by hand), so no study runs on them and no edit writes over them. Fix the file, "
                                   "or reset the tolerances to the defaults.")
    return doc


def _revision(root, rev=None):
    """(RobotProject, rev, design) for a finished revision of the project at `root`; StudyError otherwise."""
    from ..robot.pipeline2 import RobotProject

    if not os.path.isdir(root):
        raise StudyError("There is no such project.")
    meta = _read_json(os.path.join(root, "meta.json"))
    if isinstance(meta, dict) and meta.get("kind") != "robot":     # as `morph studio` tells them apart
        raise StudyConflict(f"{os.path.basename(os.path.normpath(root))} is a gripper-only project from the earlier CLI "
                            "prototype; robustness studies need a whole robot (a Morph Studio project).")
    p = RobotProject(root)
    done = p.finished()
    rev = rev or (done[-1] if done else None)
    if not isinstance(rev, str) or not REV.fullmatch(rev) or rev not in done:
        raise StudyError("This project has no finished revision to study yet." if not rev else
                         f"This project has no finished revision {rev}." if isinstance(rev, str) and REV.fullmatch(rev)
                         else "There is no such revision.")
    return p, rev, p.load_design(rev)


def _sim_world(p, rev) -> dict | None:
    try:
        with open(os.path.join(p.rev_dir(rev), "sim", "world.json"), encoding="utf-8") as fh:
            w = json.load(fh)
        return w if isinstance(w, dict) else None
    except (OSError, ValueError):
        return None


def tolerances(root: str, rev: str | None = None) -> dict:
    """The project's tolerance spec for a revision: the defaults for its design and room, with the project's edits."""
    p, rev, design = _revision(root, rev)
    world = _sim_world(p, rev) if design.world.id else None
    ed = _edits(root)
    spec = merge(default_spec(design, world, ed.get("world_preset") or "standard"), ed.get("params"))
    spec["sha"] = spec_sha(spec)
    return spec


def save_tolerances(root: str, changes: dict, rev: str | None = None, by: str | None = None) -> dict:
    """Edit the project's tolerances: {name: {lo?, hi?, on?}}, "world_preset": name, or {"reset": true}. Returns the new
    spec for `rev`. An edit marks studies made with the old spec outdated (their spec_sha no longer matches)."""
    if not isinstance(changes, dict):
        raise StudyError("Send the tolerance changes as an object.")
    if "reset" in changes and changes["reset"] is not True:
        raise StudyError("Send {reset: true} to go back to the default tolerances.")
    p, rev, design = _revision(root, rev)
    world = _sim_world(p, rev) if design.world.id else None
    ed = {} if changes.get("reset") else _edits(root)
    edits = dict(ed.get("params") or {})
    preset = changes.get("world_preset", ed.get("world_preset") or "standard")
    if not isinstance(preset, str) or preset not in WORLD_PRESETS:
        raise StudyError(f"There is no world tolerance preset called {preset}.")
    spec = default_spec(design, world, preset)
    by_name = {e["name"]: e for e in spec["params"]}
    for name, ch in changes.items():
        if name in ("reset", "world_preset"):
            continue
        if name not in NAMES:
            raise StudyError(f"There is no tolerance called {name}.")
        if not isinstance(ch, dict):
            raise StudyError(f"Send the change to {name} as {{lo, hi, on}}.")
        if name not in by_name:
            raise StudyError(f"{name} only applies in World mode.")
        e = _checked(merge(spec, {name: edits.get(name, {})})["params"][[x["name"] for x in spec["params"]].index(name)],
                     ch.get("lo"), ch.get("hi"), ch.get("on"))
        edits[name] = {"lo": e["lo"], "hi": e["hi"], "on": e["on"]}
    doc = {"version": VERSION, "params": edits, "world_preset": preset, "updated_utc": _utc(), "by": by}
    _write_json(_tolerance_path(root), doc)
    return tolerances(root, rev)


# ====================================================================== runs: sampling and statistics

def sample(params: list, seed: int, i: int):
    """Run i's world: (values by name, 32 box directions, the encoder-noise seed). Depends on (seed, i) and the spec's
    ranges only; every entry draws even when switched off (it then keeps its nominal), so an edit to one range never
    moves the others."""
    rng = np.random.default_rng(np.random.SeedSequence([int(seed), int(i)]))
    vals = {}
    for e in params:
        v = float(rng.uniform(e["lo"], e["hi"]))
        vals[e["name"]] = v if e["on"] else float(e["nominal"])
    dirs = rng.uniform(0, 2 * math.pi, size=32)
    return vals, dirs, int(rng.integers(0, 2 ** 31 - 1))


def perturbation(vals: dict, dirs, noise_seed: int, cpr: int, boxes: int) -> dict:
    """The simulate.py perturbation (see simulate._perturb_start) for one run's values."""
    p = {"off_across_mm": vals["object_across_mm"], "off_along_mm": vals["object_along_mm"],
         "yaw_deg": vals["object_yaw_deg"], "size_scale": vals["object_size"], "mass_g": vals["object_mass_g"],
         "friction_scale": vals["pad_friction"], "grip_scale": vals["grip_force"], "torque_scale": vals["servo_torque"],
         "deadband_deg": vals["servo_deadband_deg"], "encoder_cpr": int(cpr), "encoder_noise": vals["encoder_noise"],
         "noise_seed": int(noise_seed), "rate_hz": vals["setpoint_hz"]}
    if "mount_dx_mm" in vals:
        err = vals["box_error_mm"]
        p.update({"mount_dx_mm": vals["mount_dx_mm"], "mount_dy_mm": vals["mount_dy_mm"],
                  "mount_yaw_deg": vals["mount_yaw_deg"],
                  "box_offsets_mm": [[math.cos(dirs[k % 32]) * err, math.sin(dirs[k % 32]) * err] for k in range(boxes)]})
    return p


def wilson(k: int, n: int, z: float = Z95) -> tuple:
    """Wilson score interval for k successes in n (Brown, Cai & DasGupta 2001)."""
    if n <= 0:
        return (0.0, 1.0)
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z / den * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, c - h), min(1.0, c + h))


def clopper_pearson(k: int, n: int, alpha: float = 0.05) -> tuple:
    """Exact (Clopper-Pearson) interval; closed form when k is 0 or n."""
    if n <= 0:
        return (0.0, 1.0)
    if k == n:
        return ((alpha / 2) ** (1.0 / n), 1.0)
    if k == 0:
        return (0.0, 1.0 - (alpha / 2) ** (1.0 / n))
    from scipy.stats import beta
    return (float(beta.ppf(alpha / 2, k, n - k + 1)), float(beta.ppf(1 - alpha / 2, k + 1, n - k)))


class _Feature:
    """One parameter (or its distance from nominal) cut at 26 quantiles; scores every interval between two cuts."""

    def __init__(self, name, kind, x):
        self.name, self.kind, self.x = name, kind, x
        self.cuts = np.unique(np.quantile(x, np.linspace(0.0, 1.0, 26)))
        m = len(self.cuts)
        self.lo = np.searchsorted(self.cuts, x, side="right") - 1        # largest cut <= x
        self.hi = np.searchsorted(self.cuts, x, side="left")             # smallest cut >= x
        a, b = np.indices((m, m))
        self.bad = (a > b) | ((a == 0) & (b == m - 1))                   # empty, or the whole range

    def scores(self, y):
        """(J, tp, fp) for every [cuts[a], cuts[b]]: Youden's J = TPR - FPR of "x in the interval" for indicator y."""
        m = len(self.cuts)
        P, N = np.zeros((m, m)), np.zeros((m, m))
        np.add.at(P, (self.lo, self.hi), y)
        np.add.at(N, (self.lo, self.hi), 1 - y)
        tp = P[::-1].cumsum(0)[::-1].cumsum(1)
        fp = N[::-1].cumsum(0)[::-1].cumsum(1)
        npos = float(y.sum())
        J = tp / max(npos, 1.0) - fp / max(len(y) - npos, 1.0)
        J[self.bad] = -np.inf
        return J, tp, fp


def _features(rows, params) -> list:
    on = [e for e in params if e["on"] and e["hi"] > e["lo"]]
    X = {e["name"]: np.array([float(r["params"][e["name"]]) for r in rows]) for e in on}
    feats = [_Feature(e["name"], "value", X[e["name"]]) for e in on]
    feats += [_Feature(e["name"], "deviation", np.abs(X[e["name"]] - e["nominal"])) for e in on
              if e["lo"] < e["nominal"] < e["hi"]]
    return [f for f in feats if len(f.cuts) > 1]


def _best(feats, y):
    best = None
    for f in feats:
        J, tp, fp = f.scores(y)
        k = int(np.argmax(J))
        a, b = divmod(k, len(f.cuts))
        if np.isfinite(J[a, b]) and (best is None or J[a, b] > best[0] + 1e-12):
            best = (float(J[a, b]), f, float(f.cuts[a]), float(f.cuts[b]), int(tp[a, b]), int(fp[a, b]))
    return best


def _chance(feats, y, seed, rounds=60) -> float:
    """95th percentile of the best J over every feature when the outcomes are shuffled: an interval search finds
    some J on pure noise, so a rule only means something above this."""
    rng = np.random.default_rng(np.random.SeedSequence([int(seed), 12345]))
    top = [max(float(f.scores(yp)[0].max()) for f in feats) for yp in (rng.permutation(y) for _ in range(rounds))]
    return float(np.quantile(top, 0.95)) if top else 1.0


PHRASES = {"object_across_mm": ("{obj} {lo} to {hi} mm across the jaws from its mark", "{obj} {lo}-{hi} mm off its mark across the jaws"),
           "object_along_mm": ("{obj} {lo} to {hi} mm along its length from its mark", "{obj} {lo}-{hi} mm off its mark along its length"),
           "object_yaw_deg": ("{obj} turned {lo} to {hi} deg", "{obj} turned {lo}-{hi} deg"),
           "object_size": ("{obj} at {lo}-{hi} of its nominal size", "{obj} {lo}-{hi} off its nominal size"),
           "object_mass_g": ("{obj} weighing {lo}-{hi} g", "{obj} {lo}-{hi} g off its nominal mass"),
           "pad_friction": ("pad friction at {lo}-{hi} of nominal", "pad friction {lo}-{hi} off nominal"),
           "grip_force": ("grip force calibrated at {lo}-{hi} of its target", "grip force calibration {lo}-{hi} off"),
           "servo_torque": ("servos at {lo}-{hi} of their torque", "servo torque {lo}-{hi} off"),
           "servo_deadband_deg": ("servo deadband {lo}-{hi} deg", "servo deadband {lo}-{hi} deg"),
           "encoder_noise": ("encoder noise {lo}-{hi} counts", "encoder noise {lo}-{hi} counts"),
           "setpoint_hz": ("set-points at {lo}-{hi} Hz", "set-points at {lo}-{hi} Hz"),
           "mount_dx_mm": ("mount {lo} to {hi} mm off in x", "mount {lo}-{hi} mm off in x"),
           "mount_dy_mm": ("mount {lo} to {hi} mm off in y", "mount {lo}-{hi} mm off in y"),
           "mount_yaw_deg": ("mount turned {lo} to {hi} deg", "mount turned {lo}-{hi} deg"),
           "box_error_mm": ("boxes {lo}-{hi} mm off their measured place", "boxes {lo}-{hi} mm off their measured place")}
PHASES = {"approach": "the approach", "grasp": "the grasp", "lift": "the lift", "move": "the carry",
          "place": "the set-down", "release": "the release", "retreat": "the way back"}
LABELS = {"jam": "Jaws stayed shut at release: the {obj} was carried away", "drag": "Dragged off the target",
          "missed_grasp": "Missed the grasp", "dropped": "Dropped on the way",
          "over_force": "Squeezed harder than the {obj} can take", "unreachable": "Out of reach",
          "world_collision": "Hit a box in the room during {phase}", "error": "The simulation stopped with an error",
          "unstable": "Numerically unstable (not counted)"}


def noun(name: str) -> str:
    words = re.findall(r"[a-z]+", (name or "").lower())
    return words[-1] if words else "object"


def group_label(group: str, obj: str = "object") -> str:
    cls, _, phase = group.partition(":")
    return LABELS.get(cls, cls).format(obj=obj, phase=PHASES.get(phase, phase or "the run"))


def _fmt(v, unit) -> str:
    if unit == "x":
        return f"{v * 100:.0f}%"
    return f"{v:.0f}" if unit in ("Hz", "counts") or abs(v) >= 100 else f"{v:.1f}"


def rule_text(rule: dict, obj: str = "object") -> str:
    tpl = PHRASES.get(rule["param"], ("{lo} to {hi}", "{lo}-{hi} off nominal"))[rule["on"] == "deviation"]
    region = tpl.format(obj=obj, lo=_fmt(rule["lo"], rule["unit"]), hi=_fmt(rule["hi"], rule["unit"]))
    return f"{region}: {rule['rate_in']:.0%} fail this way vs {rule['rate_out']:.0%} otherwise"


def _rule(feats, y, params, seed, obj) -> dict | None:
    best = _best(feats, y)
    if best is None:
        return None
    j, f, lo, hi, tp, fp = best
    e = next(e for e in params if e["name"] == f.name)
    n, pos = len(y), int(y.sum())
    chance = _chance(feats, y, seed)
    out = {"param": f.name, "label": e["label"], "unit": e["unit"], "on": f.kind, "lo": round(lo, 4), "hi": round(hi, 4),
           "J": round(j, 3), "runs_in": tp + fp, "covers": tp, "of": pos, "rate_in": round(tp / max(tp + fp, 1), 3),
           "rate_out": round((pos - tp) / max(n - tp - fp, 1), 3), "chance_J": round(chance, 3),
           "significant": bool(j > chance)}
    out["text"] = rule_text(out, obj)
    return out


# ====================================================================== analysis

VARIANTS = ("half_timestep", "impratio_1")


def variant_perturb(variant: str, timestep: float) -> dict:
    return {"half_timestep": {"timestep": timestep / 2}, "impratio_1": {"impratio": 1.0}}[variant]


def _counted(rows) -> list:
    return [r for r in rows if r["outcome"] not in ("unstable", "error")]


def check_selection(rows, seed, runs_per_group=CHECK_RUNS, successes=CHECK_SUCCESSES) -> tuple:
    """The runs each failure group is re-run with (at most runs_per_group of its own) and one sample of successes,
    drawn from the study's seed: ({group: [run]}, [run])."""
    counted = _counted(rows)
    rng = np.random.default_rng(np.random.SeedSequence([int(seed), 31337]))
    groups = {}
    for r in counted:
        if not r["success"]:
            groups.setdefault(r["group"], []).append(r["i"])
    pick = {}
    for g in sorted(groups):
        ids = sorted(groups[g])
        pick[g] = ids if len(ids) <= runs_per_group else sorted(int(i) for i in rng.choice(ids, runs_per_group, replace=False))
    ok = sorted(r["i"] for r in counted if r["success"])
    sample_ok = ok if len(ok) <= successes else sorted(int(i) for i in rng.choice(ok, successes, replace=False))
    return pick, sample_ok


def _sensitivity(group, ids, ok_ids, checks) -> dict:
    """How a failure group fares at half the timestep and with impratio=1: model-sensitive when, under either, fewer
    than 80 % of its re-run runs still fail the same way, or at least 20 % (and 2) of the re-run successes now do."""
    by = {(c["variant"], c["i"]): c for c in checks}
    out, sensitive = {}, False
    for v in VARIANTS:
        own = [by[(v, i)] for i in ids if (v, i) in by]
        oks = [by[(v, i)] for i in ok_ids if (v, i) in by]
        same = sum(c["group"] == group for c in own)
        new = sum(c["group"] == group for c in oks)
        out[v] = {"runs": len(own), "still_fail": same, "successes": len(oks), "now_fail": new}
        if (own and same < 0.8 * len(own)) or (oks and new >= max(2, 0.2 * len(oks))):
            sensitive = True
    done = all(out[v]["runs"] == len(ids) for v in VARIANTS)
    return {"model_sensitive": sensitive if done or sensitive else None, "checks": out}


def analyze(rows: list, params: list, checks: list | None = None, seed: int = DEFAULT_SEED, obj: str = "object") -> dict:
    """Success rate with its interval, failure groups (rate, interval, best rule, example run, model sensitivity) and
    each parameter's influence, from per-run rows ({i, params, outcome, group, success}) and the spec's params."""
    rows = sorted(rows, key=lambda r: r["i"])
    counted = _counted(rows)
    n, k = len(counted), sum(r["success"] for r in counted)
    out = {"n": n, "successes": k, "failures": n - k, "p": round(k / n, 4) if n else None,
           "wilson95": [round(v, 4) for v in wilson(k, n)] if n else None,
           "unstable": [r["i"] for r in rows if r["outcome"] == "unstable"],
           "errors": [r["i"] for r in rows if r["outcome"] == "error"], "groups": [], "sensitivity": []}
    if n and k in (0, n):
        out["clopper_pearson95"] = [round(v, 4) for v in clopper_pearson(k, n)]
    if not n:
        return out
    feats = _features(counted, params)
    on = [e for e in params if e["on"] and e["hi"] > e["lo"]]
    lo = np.array([e["lo"] for e in on])
    span = np.array([e["hi"] - e["lo"] for e in on])
    nom = np.clip([e["nominal"] for e in on], lo, lo + span)
    pick, ok_ids = check_selection(rows, seed) if checks is not None else ({}, [])
    groups = {}
    for r in counted:
        if not r["success"]:
            groups.setdefault(r["group"], []).append(r)
    for g, rs in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        y = np.array([r["group"] == g for r in counted], float)
        Xi = np.array([[r["params"][e["name"]] for e in on] for r in rs]) if on else np.zeros((len(rs), 0))
        ex = rs[int(np.argmin(np.linalg.norm((Xi - nom) / span, axis=1)))] if on else rs[0]
        item = {"id": g, "label": group_label(g, obj), "count": len(rs), "rate": round(len(rs) / n, 4),
                "wilson95": [round(v, 4) for v in wilson(len(rs), n)],
                "rule": _rule(feats, y, params, seed, obj) if feats and len(rs) < n else None,   # every run: nothing to isolate
                "example_run": ex["i"], "runs": [r["i"] for r in rs][:500]}
        if checks is not None:
            item.update(_sensitivity(g, pick.get(g, []), ok_ids, checks))
        out["groups"].append(item)
    if 0 < k < n and feats:
        yfail = np.array([not r["success"] for r in counted], float)
        chance = _chance(feats, yfail, seed)
        out["chance_J"] = round(chance, 3)
        for e in on:
            own = [f for f in feats if f.name == e["name"]]
            best = _best(own, yfail) if own else None
            x = np.array([r["params"][e["name"]] for r in counted])
            q1, q2 = np.quantile(x, [1 / 3, 2 / 3])
            bins = [x <= q1, (x > q1) & (x <= q2), x > q2]
            out["sensitivity"].append({"param": e["name"], "label": e["label"], "J": round(best[0], 3) if best else 0.0,
                                       "significant": bool(best and best[0] > chance),
                                       "fail_rate_terciles": [round(float(yfail[b].mean()), 3) if b.any() else None for b in bins]})
        out["sensitivity"].sort(key=lambda s: -s["J"])
    if checks is not None:
        by = {(c["variant"], c["i"]): c for c in checks}
        out["checks"] = {v: {"successes": len([i for i in ok_ids if (v, i) in by]),
                             "still_succeed": sum(by[(v, i)]["success"] for i in ok_ids if (v, i) in by)} for v in VARIANTS}
        # the success rate itself is model-sensitive when, under either setting, at least 20 % (and 2) of the counted
        # re-runs of successful runs fail: a study where nothing fails can depend on the solver as much as one that does
        flip = None
        for v in VARIANTS:
            cs = [by[(v, i)] for i in ok_ids if (v, i) in by and by[(v, i)]["outcome"] not in ("unstable", "error")]
            if cs:
                bad = sum(not c["success"] for c in cs)
                flip = bool(flip) or bad >= max(2, 0.2 * len(cs))
        out["successes_sensitive"] = flip
    return out


# ====================================================================== storage

def _utc(t: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if t is None else t))


def _write_json(path: str, doc: dict):
    from .durable import write_json
    write_json(path, doc)                    # synced, and retried while Windows holds the old file open for a moment


def _read_json(path: str):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def study_dir(root: str, sid: str) -> str:
    if not isinstance(sid, str) or not ID.fullmatch(sid):
        raise StudyError("There is no such study.")
    return os.path.join(root, STUDIES, sid)


def _append(path: str, row: dict):
    from .durable import append_line
    append_line(path, json.dumps(row, separators=(",", ":")))  # a row cut off by a crash never swallows the next one


def read_rows(path: str) -> list:
    """Rows of runs.jsonl / checks.jsonl; a line cut off by a crash is skipped, a run written twice counts once."""
    out, seen = [], set()
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                key = (r.get("variant"), r.get("i")) if isinstance(r, dict) else None
                if key is None or not isinstance(r.get("i"), int) or key in seen:
                    continue
                seen.add(key)
                out.append(r)
    except OSError:
        pass
    return out


def _active_path(root) -> str:
    return os.path.join(root, STUDIES, "active.json")


def _alive(pid) -> bool:
    """Whether process `pid` of this computer still runs; True when that can't be told (the heartbeat decides then)."""
    from .durable import process_alive
    return process_alive(pid)


def active(root: str) -> dict | None:
    """The study running in this project (any process), from its lock and heartbeat; None when none is. A lock whose
    process is gone from this computer (the Studio or the terminal was killed) is stale at once."""
    path = _active_path(root)
    try:
        age = time.time() - os.path.getmtime(path)
    except OSError:
        return None
    doc = _read_json(path)
    if not isinstance(doc, dict) or age >= STALE_S:
        return None
    if doc.get("host") == socket.gethostname() and not _alive(doc.get("pid")):
        return None
    return doc


HOLD = "hold-"  # the study lock's id when no study holds it but Delete project or Reset demo (holding)


def held(doc: dict | None) -> bool:
    """Whether the study lock `doc` (active()) is Delete project's or Reset demo's (holding), not a study's."""
    return isinstance(doc, dict) and str(doc.get("id") or "").startswith(HOLD)


def busy(doc: dict) -> StudyBusy:
    """Why no study can start while the study lock `doc` (active()) is taken."""
    if held(doc):
        return StudyBusy("This project is being deleted or reset right now: try again in a moment.")
    return StudyBusy("A robustness study is already running for this project.")


def _lock(root: str, sid: str):
    path = _active_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    for _ in range(3):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            doc = active(root)
            if doc is not None:
                raise busy(doc) from None
            try:
                os.unlink(path)           # stale: its process stopped
            except OSError:
                pass
            continue
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"id": sid, "pid": os.getpid(), "host": socket.gethostname(), "started_utc": _utc()}, fh)
        except BaseException:  # a full disk: no lock half written stays behind (it is ours: O_EXCL made it)
            try:
                os.unlink(path)
            except OSError:
                pass
            raise
        return
    raise StudyBusy("A robustness study is already running for this project.")


class holding:
    """`with holding(root) as moved:` no study starts in project folder `root` meanwhile: Delete project moves it to the
    trash, Reset demo removes revisions. It takes the study lock the way a study does (StudyBusy while a study, or
    another holding, has it), so a `morph study` started in between is told the project is being deleted or reset
    instead of running on in a folder that moves. `moved(new_root)`: the folder moved there, and the lock with it.
    On a full disk (the lock can't be written) it goes on without the lock, as before there was one: a study can't
    write its lock either until space is freed, and the caller has looked for a study running elsewhere already."""

    def __init__(self, root: str):
        import secrets
        self.root, self.sid = root, HOLD + secrets.token_hex(6)
        self.made = not os.path.isdir(os.path.join(root, STUDIES))  # a .studies folder made only for the lock goes too
        self.locked = False

    def __enter__(self):
        from .durable import disk_full
        try:
            _lock(self.root, self.sid)
            self.locked = True
        except OSError as exc:  # never StudyBusy: that is a ValueError
            if not disk_full(exc):
                raise
            self._tidy()
        return self.moved

    def moved(self, new_root: str):
        self.root = new_root

    def __exit__(self, *exc):
        if self.locked:
            _unlock(self.root, self.sid)
        self._tidy()
        return False

    def _tidy(self):
        if self.made:
            try:
                os.rmdir(os.path.join(self.root, STUDIES))
            except OSError:
                pass


def _unlock(root: str, sid: str):
    doc = _read_json(_active_path(root))
    if isinstance(doc, dict) and doc.get("id") == sid and doc.get("pid") == os.getpid():
        try:
            os.unlink(_active_path(root))
        except OSError:
            pass


def _beat(root: str):
    try:
        os.utime(_active_path(root))
    except OSError:
        pass


LIVE = ("queued", "preparing", "running", "checking")


def _rev_state(root: str, rev: str) -> tuple:
    """(tolerance spec sha, design.json sha, pipeline version) of a revision as it is now."""
    from ..robot.pipeline2 import pipeline_version

    p, rev, design = _revision(root, rev)
    try:
        spec_sha_ = tolerances(root, rev)["sha"]
    except TolerancesUnreadable:  # whether a study is outdated can't be told; its revision is still there
        spec_sha_ = None
    return (spec_sha_, _sha_file(os.path.join(p.rev_dir(rev), "design.json")), pipeline_version(design.arm.source))


_PARTIAL: dict = {}                 # runs.jsonl (path, size, mtime) -> (runs on disk, their summary): stopped studies


def _partial(doc: dict, d: str):
    """A study that stopped part way (its process was killed, or it failed): `done` and `summary` from the runs it
    saved, which its record may not have caught up with."""
    path = os.path.join(d, "runs.jsonl")
    try:
        st = os.stat(path)
    except OSError:
        return
    key = (path, st.st_size, st.st_mtime_ns)
    got = _PARTIAL.get(key)
    if got is None:
        rows = read_rows(path)
        s = doc.get("summary") or {}
        if (s.get("n") or 0) + len(s.get("unstable") or []) + len(s.get("errors") or []) != len(rows):
            try:
                s = analyze(rows, doc["spec"]["params"], None, doc.get("seed", DEFAULT_SEED), doc.get("object") or "object")
            except (KeyError, TypeError, ValueError):
                s = doc.get("summary")
        got = (len(rows), s)
        for old in list(_PARTIAL)[:max(0, len(_PARTIAL) - 31)]:     # the Studio's threads share it: never read back
            _PARTIAL.pop(old, None)
        _PARTIAL[key] = got
    doc["done"], doc["summary"] = got


def load(root: str, sid: str, rows: bool = False, _revs: dict | None = None) -> dict | None:
    """A study's record (study.json), its status corrected to "interrupted" when its process stopped (with `done` and
    `summary` from the runs it saved), with `outdated` (the project's tolerances changed since), `rev_missing`,
    `rev_changed` (a revision of that name now holds another design: Reset demo deleted it and a later edit was numbered
    the same) and `code_changed` (Morph's pipeline code is not the study's any more); `rows=True` adds runs and checks."""
    d = study_dir(root, sid)
    doc = _read_json(os.path.join(d, "study.json"))
    if not isinstance(doc, dict):
        return None
    if doc.get("status") in LIVE and (active(root) or {}).get("id") != sid and not (
            doc["status"] == "queued" and doc.get("queued_by") == _PROCESS):  # waiting for its turn in this process
        doc["status"] = "interrupted"
    if doc.get("status") in ("interrupted", "error"):
        _partial(doc, d)
    try:
        rev = doc["rev"]
        if _revs is None or rev not in _revs:     # studies() reads each revision's spec and design once
            got = _rev_state(root, rev)
            if _revs is not None:
                _revs[rev] = got
        spec_sha_, design_sha, pv = (_revs or {}).get(rev) or got
        doc["outdated"] = None if spec_sha_ is None else spec_sha_ != doc.get("base_spec_sha")
        doc["rev_missing"] = False
        doc["rev_changed"] = design_sha != doc.get("design_sha")
        doc["code_changed"] = pv != doc.get("pipeline_version")
    except (StudyError, OSError, ValueError, KeyError):
        doc["outdated"], doc["rev_missing"], doc["rev_changed"], doc["code_changed"] = None, True, None, None
    if rows:
        doc["runs"] = sorted(read_rows(os.path.join(d, "runs.jsonl")), key=lambda r: r["i"])
        doc["checks"] = read_rows(os.path.join(d, "checks.jsonl"))
    return doc


def studies(root: str) -> list:
    """The project's studies, newest first: {id, rev, status, n, done, p, wilson95, stress, seed, created_utc, outdated,
    rev_missing, rev_changed, code_changed}."""
    out, revs = [], {}
    base = os.path.join(root, STUDIES)
    for sid in os.listdir(base) if os.path.isdir(base) else []:
        if not ID.fullmatch(sid):
            continue
        doc = load(root, sid, _revs=revs)
        if doc:
            s = doc.get("summary") or {}
            out.append({k: doc.get(k) for k in ("id", "rev", "status", "n", "done", "stress", "seed", "created_utc",
                                                "updated_utc", "outdated", "rev_missing", "rev_changed", "code_changed")}
                       | {"p": s.get("p"), "wilson95": s.get("wilson95")})
    return sorted(out, key=lambda s: s.get("updated_utc") or "", reverse=True)


# ====================================================================== the robot and plan, packed for workers

def _assembly(design, res: dict):
    """The revision's assembly, built the way the pipeline built it (sizing, the gripper stage's grip target, the
    electronics plan)."""
    from ..robot import arm, electronics2, imported

    sz = imported.size(design) if design.arm.source else arm.size(design)
    ep = electronics2.plan(design, sz, grip=(res.get("gripper") or {}).get("force"))
    return (imported.build if design.arm.source else arm.build)(design, sz, ep)


def studyable(p, rev: str) -> dict:
    """The revision's results when its pick and place can be studied; StudyConflict saying why not (the simulation
    stage failed or never ran, a preview-only build). Checked before a study is queued, not in its worker processes."""
    from ..robot.pipeline2 import stage_problem

    try:
        res = p.load_results(rev) or {}
    except (OSError, ValueError) as exc:
        raise StudyConflict(f"{rev}'s results can't be read ({exc.__class__.__name__}).") from None
    if res.get("preview_only"):
        raise StudyConflict(f"{rev} is a preview built from the basic model; studies need the detailed robot.")
    problem = stage_problem(res, "sim")
    if problem or not (((res.get("sims") or {}).get("scenarios") or {}).get(SCENARIO) or {}).get("file"):
        raise StudyConflict(f"{rev} has no pick-and-place simulation to study" + (f" ({problem})." if problem else "."))
    return res


def _norm_xml(xml: str) -> str:
    """What of a robot.xml the motion depends on: the time step is left out (a study picks its own), and so is the base
    welded to the table (a body on the world with no joint): its mass and shapes change with the controller, supply
    and boards inside it, which move nothing, and the pipeline reuses the simulation across those edits."""
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return re.sub(r'timestep="[^"]*"', 'timestep=""', xml)
    for opt in root.iter("option"):
        opt.attrib.pop("timestep", None)
    world, meshes = root.find("worldbody"), set()
    for body in (world.findall("body") if world is not None else []):
        if body.find("joint") is not None or body.find("freejoint") is not None:
            continue
        for el in list(body):
            if el.tag in ("inertial", "geom", "site"):
                meshes.add(el.get("mesh"))
                body.remove(el)
    asset = root.find("asset")
    for m in (list(asset) if asset is not None else []):
        if m.tag == "mesh" and m.get("name") in meshes:
            asset.remove(m)
    return ET.tostring(root, encoding="unicode")


def _explain(exc, root: str) -> str:
    """An error's text for the Robustness card: files named from the projects folder (the folder holding project
    `root`), never the server's own path (durable.explain)."""
    from .durable import explain
    return explain(exc, os.path.dirname(os.path.abspath(root)))


def prepare(root: str, rev: str, timestep: float = 0.001) -> dict:
    """The revision's robot and its nominal pick-and-place plan, packed for the workers (pickled: it only ever travels
    between this study's own processes, never through a file). Checks the rebuilt model against the revision's
    sim/robot.xml, the moving robot only (_norm_xml): `matches_revision` False means the revision was simulated with
    other Morph code."""
    from ..robot import simulate as S

    t0 = time.time()
    p, rev, design = _revision(root, rev)
    res = studyable(p, rev)
    try:
        asm = _assembly(design, res)
        world = None
        if design.world.id:
            with tempfile.TemporaryDirectory() as tmp:
                world = p._sim_world(design, tmp)
        rob = S._make_robot(asm, design, False, {"timestep": timestep, **({"world": world} if world else {})})
    except StudyError:
        raise
    except Exception as exc:  # noqa: BLE001  (a robot the generator can't rebuild: say so)
        raise StudyError(f"{rev}'s robot couldn't be rebuilt for the study: {exc.__class__.__name__}: "
                         f"{_explain(exc, root)}") from exc
    try:
        with open(os.path.join(p.rev_dir(rev), "sim", "robot.xml"), encoding="utf-8") as fh:
            saved = fh.read()
    except OSError:
        saved = None
    m = re.search(r'<option timestep="([^"]*)"', saved or "")
    plan, at = S._plan_pick_place(rob, design, False)
    nominal = ((res["sims"]["scenarios"][SCENARIO] or {}).get("metrics") or {})
    out = {"blob": _pack(rob, plan, at), "boxes": int(len(rob.world_geoms)), "world": (world or {}).get("id"),
           "matches_revision": None if saved is None else _norm_xml(rob.xml) == _norm_xml(saved),
           "revision_timestep": float(m.group(1)) if m else None, "hover_mm": round(float(at["hover"]), 2),
           "nominal": {k: nominal.get(k) for k in ("success", "place_error_mm", "grip_n_max", "max_slip_mm", "intact")},
           "prepare_s": round(time.time() - t0, 2)}
    return out


class Skeleton:
    """What a run reads of the assembly (joints with their mimics, link names, meta): no meshes, so a worker never
    imports trimesh (100 MB and half a second per process)."""

    KEYS = ("name", "type", "parent", "child", "axis", "lower", "upper", "home", "actuator", "effort", "velocity", "mimic")

    def __init__(self, asm):
        self.name, self.root, self.meta = asm.name, asm.root, dict(asm.meta)
        self.links = {k: None for k in asm.links}
        self.joints = [SimpleNamespace(origin=np.asarray(j.origin, float), **{k: getattr(j, k) for k in self.KEYS})
                       for j in asm.joints]

    def joint(self, name):
        return next(j for j in self.joints if j.name == name)


def _pack(rob, plan, at) -> bytes:
    st = {k: v for k, v in vars(rob).items() if k not in ("asm", "_mj", "data", "dik")}
    st["asm"] = Skeleton(rob.asm)
    return pickle.dumps({"robot": st, "plan": {k: v for k, v in vars(plan).items() if k != "rob"}, "at": at},
                        protocol=pickle.HIGHEST_PROTOCOL)


def _unpack(blob: bytes):
    import mujoco

    from ..robot import simulate as S

    b = pickle.loads(blob)  # noqa: S301  (packed by this study's own preparing process, sent over its own pipe)
    rob = S._Robot.__new__(S._Robot)
    vars(rob).update(b["robot"])
    rob._mj, rob.data, rob.dik = mujoco, mujoco.MjData(rob.model), mujoco.MjData(rob.model)
    plan = S._Plan.__new__(S._Plan)
    vars(plan).update(b["plan"])
    plan.rob = rob
    return rob, plan, b["at"]


class _Monitor:
    """Per-step measurements a trial needs beyond the scenario's own: how far the object was pushed before the pads
    touched it, how high it was at the end of the lift, and how far it rose after the release (a jam)."""

    def __init__(self, rob, at, dt, t_lift_end, t_grasp0):
        self.rob, self.dt, self.t_lift_end, self.t_grasp0 = rob, dt, t_lift_end, t_grasp0
        self.t_released = at["t_released"]
        self.obj0, self.contact, self.lift, self.push, self.rise = None, None, None, 0.0, 0.0

    def __call__(self, t, d, ph):
        rob, MM = self.rob, 1e-3
        x = d.xpos[rob.obj]
        if self.obj0 is None:
            self.obj0 = x.copy()
        step = int(round(t / self.dt))
        if self.contact is None and step >= 1 and (step - 1) % 5 == 0 and d.ncon and (step - 1) * self.dt >= self.t_grasp0:
            if rob.grip_force(d) > 0.05:          # as felt after the previous step
                self.contact = (step - 1) * self.dt
        if self.contact is None:
            self.push = max(self.push, float(np.linalg.norm((x - self.obj0)[:2])) / MM)
        if self.lift is None and t >= self.t_lift_end:
            self.lift = float(x[2] - self.obj0[2]) / MM
        if t >= self.t_released:
            self.rise = max(self.rise, float(x[2] - self.obj0[2]) / MM)
        return True


class Runner:
    """A worker's robot and plan: runs trials (one perturbation each) and labels them."""

    def __init__(self, blob: bytes):
        import mujoco

        from ..robot import simulate as S

        self.S, self.mj = S, mujoco
        self.rob, self.plan, self.at = _unpack(blob)
        ph, t = np.asarray(self.plan.ph), np.asarray(self.plan.t)
        self.t_lift_end, self.t_grasp0 = float(t[ph == "lift"].max()), float(t[ph == "grasp"].min())
        self.warn = {w: int(getattr(mujoco.mjtWarning, w)) for w in WARNINGS}

    def run(self, job: dict, doc: bool = False) -> dict:
        """One run: {i, params, perturb, variant?} -> its row (and the full scenario document with `doc`)."""
        S, rob, at = self.S, self.rob, self.at
        p = job["perturb"]
        plan = copy.copy(self.plan)
        plan.markers = [dict(mk) for mk in self.plan.markers]          # the run labels its own markers
        dt = float(p.get("timestep", rob.model.opt.timestep))
        mon = _Monitor(rob, at, dt, self.t_lift_end, self.t_grasp0)
        t0, c0 = time.time(), time.process_time()
        row = {"i": job["i"], **({"variant": job["variant"]} if job.get("variant") else {}),
               "params": {k: round(v, 5) for k, v in job["params"].items()}}
        try:
            with S._perturbed(rob, p):
                run, met = S._run_pick_place(rob, rob.design, plan, at, p, mon)
            final = rob.data.xpos[rob.obj] / S.MM
        except Exception as exc:  # noqa: BLE001  (one run's failure is that run's outcome)
            row.update({"outcome": "error", "group": "error", "success": False,
                        "error": f"{exc.__class__.__name__}: {exc}"[:300]})
            return row
        warns = {w: int(rob.data.warning[k].number) for w, k in self.warn.items()}
        ra = rob.R * p.get("size_scale", 1.0)
        err = float(np.linalg.norm(final - (np.asarray(at["place"], float) + [0.0, 0.0, ra - rob.R])))
        contacts = met.get("world_contacts") or []
        ok = bool(err <= S.PLACE_TOL_MM and not met["dropped"] and met["intact"] and not contacts and met["reachable"])
        group = phase = None
        if any(warns[w] for w in UNSTABLE) or not np.all(np.isfinite(final)):
            outcome = "unstable"
        elif ok:
            outcome = "placed"
        elif contacts:
            outcome = "world_collision"
            k = min(int(contacts[0]["t"] / S.PLAN_DT), len(self.plan.ph) - 1)
            phase = self.plan.ph[k]
            group = f"world_collision:{phase}"
        elif not met["intact"]:
            outcome = "over_force"
        elif not met["reachable"]:
            outcome = "unreachable"
        elif mon.lift is None or mon.lift <= 0.5 * at["hover"]:
            outcome = "missed_grasp"
        elif met["dropped"]:
            outcome = "dropped"
        elif mon.rise > RISE_MM or err > CARRIED_MM:
            outcome = "jam"
        else:
            outcome = "drag"
        row.update({"outcome": outcome, "group": group or outcome, "success": outcome == "placed",
                    "place_error_mm": round(err, 2), "slip_mm": met["max_slip_mm"], "lift_mm": round(mon.lift or 0.0, 1),
                    "push_mm": round(mon.push, 2), "rise_mm": round(mon.rise, 1), "peak_grip_n": met["grip_n_max"],
                    "max_torque_pct": max(met["max_torque_pct"].values()) if met["max_torque_pct"] else None})
        if contacts:
            row["world_contact"] = {**contacts[0], "phase": phase}
        if "min_clearance_mm" in met:
            row["min_clearance_mm"] = met["min_clearance_mm"]
        if any(warns.values()):
            row["warnings"] = {w: v for w, v in warns.items() if v}
        row.update({"wall_s": round(time.time() - t0, 3), "cpu_s": round(time.process_time() - c0, 3)})
        if doc:
            d = S._scenario_doc(rob, rob.design, SCENARIO, plan, run, met)
            d["robustness"] = {k: row[k] for k in row if k not in ("wall_s", "cpu_s")}
            return d
        return row


# ====================================================================== worker processes (spawn, every OS)

def _quiet(nice: int):
    """A worker leaves Ctrl+C to its parent (which cancels and stops it) and runs below the Studio's priority."""
    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    except (ValueError, OSError, AttributeError):
        pass
    if nice <= 0:
        return
    try:
        if hasattr(os, "nice"):
            os.nice(nice)
        elif sys.platform == "win32":
            import ctypes
            ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)  # below normal
    except (OSError, AttributeError):
        pass


def _rss_mb() -> float | None:
    try:
        with open("/proc/self/status", encoding="utf-8") as fh:
            kb = next(int(ln.split()[1]) for ln in fh if ln.startswith("VmRSS:"))
        return round(kb / 1024, 1)
    except (OSError, StopIteration, ValueError):
        pass
    try:
        import resource
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return round(peak / (1024 * 1024 if sys.platform == "darwin" else 1024), 1)
    except (ImportError, OSError, AttributeError):
        return None


def _mute_mujoco():
    """MuJoCo's own warning handler writes MUJOCO_LOG.TXT into the current folder and prints each first warning: study
    processes read the warning counters instead (the Studio's own process keeps the handler)."""
    try:
        import mujoco
        mujoco.set_mju_user_warning(lambda msg: None)
    except (ImportError, AttributeError):
        pass


def _worker(conn, nice: int = 0):
    """A study worker process: loads the packed robot once, then runs the batches of trials it is sent."""
    _quiet(nice)
    runner = None
    try:
        conn.send(("up", os.getpid()))
        while True:
            msg = conn.recv()
            if msg[0] == "load":
                t0 = time.time()
                _mute_mujoco()
                runner = Runner(msg[1])
                conn.send(("loaded", round(time.time() - t0, 3), _rss_mb()))
            elif msg[0] == "runs":
                for job in msg[1]:
                    conn.send(("row", runner.run(job)))
            elif msg[0] == "doc":            # one run with its frames (a replay)
                conn.send(("doc", runner.run(msg[1], doc=True)))
            else:
                return
    except (EOFError, OSError, KeyboardInterrupt):
        return


def _preparer(conn, nice, root, rev, timestep):
    """The short-lived process that rebuilds the robot and plans (its memory goes when it exits)."""
    _quiet(nice)
    _mute_mujoco()
    try:
        msg = ("prepared", prepare(root, rev, timestep))
    except StudyError as exc:
        msg = ("failed", str(exc))
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        msg = ("failed", f"{exc.__class__.__name__}: {_explain(exc, root)}")
    try:
        conn.send(msg)
    except (OSError, EOFError):      # the study was cancelled, or the process that started it ended, meanwhile
        pass


def _single_threaded():
    for k in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
        os.environ.setdefault(k, "1")             # workers start numpy single-threaded (inherited at spawn)


def cores() -> int:
    """The CPU cores this process may use (a container's or taskset's share, not the whole machine's)."""
    try:
        n = os.process_cpu_count() if hasattr(os, "process_cpu_count") else len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        n = None
    return max(1, int(n or os.cpu_count() or 1))


def default_workers() -> int:
    """MORPH_STUDY_WORKERS, else the cores minus one; never more than the cores (runs are CPU-bound)."""
    try:
        n = int(os.environ.get("MORPH_STUDY_WORKERS") or cores() - 1)
    except ValueError:
        n = cores() - 1
    return max(1, min(n, cores()))


def default_nice() -> int:
    try:
        return max(0, int(os.environ.get("MORPH_STUDY_NICE") or 5))
    except ValueError:
        return 5


class Pool:
    """Worker processes from the spawn start method. Each loads the packed robot once (sent when it is up, or as soon
    as the preparing process delivers it) and asks for the next batch when idle; rows stream back one run at a
    time. A worker that dies has its unfinished runs tried once more elsewhere. close(kill=True) stops at once."""

    def __init__(self, n: int, nice: int | None = None, blob: bytes | None = None, prepare_args: tuple | None = None):
        _single_threaded()
        self.ctx = mp.get_context("spawn")
        self.nice = default_nice() if nice is None else nice
        self.blob, self.prep, self.prepared, self.workers, self.started = blob, None, None, [], 0
        self.t0 = time.time()
        if blob is None:
            a, b = self.ctx.Pipe()
            proc = self.ctx.Process(target=_preparer, args=(b, self.nice, *prepare_args), name="morph-study-prepare",
                                    daemon=True)
            proc.start()
            b.close()
            self.prep = {"p": proc, "c": a}
        for _ in range(max(1, int(n))):
            self._start()

    def _start(self):
        a, b = self.ctx.Pipe()
        proc = self.ctx.Process(target=_worker, args=(b, self.nice), name=f"morph-study-{self.started}", daemon=True)
        w = {"p": proc, "c": a, "t0": time.time(), "up": None, "load_s": None, "ready": None, "rss": None, "jobs": [],
             "dead": False, "loading": False}
        proc.start()
        b.close()
        self.workers.append(w)
        self.started += 1

    def _send_blob(self, w):
        if self.blob is not None and w["up"] and not w["loading"] and not w["dead"]:
            w["c"].send(("load", self.blob))
            w["loading"] = True

    def info(self) -> list:
        return [{"pid": w["p"].pid, "up_s": round(w["up"] - w["t0"], 3) if w["up"] else None, "load_s": w["load_s"],
                 "start_s": round(w["up"] - w["t0"] + w["load_s"], 3) if w["up"] and w["load_s"] is not None else None,
                 "rss_mb": w["rss"]} for w in self.workers]

    def drive(self, jobs: list, batch: int, on_row, cancel=None, tick=None) -> bool:
        """Run every job; False when `cancel` was set first (rows already received are kept). `tick()` runs about
        five times a second."""
        todo = deque(jobs[k:k + max(1, batch)] for k in range(0, len(jobs), max(1, batch)))
        tries = {}
        while True:
            if tick is not None:
                tick()
            live = [w for w in self.workers if not w["dead"]]
            if not todo and not any(w["jobs"] for w in live):
                return True
            if cancel is not None and cancel.is_set():
                self._drain(on_row)
                return False
            if not live:
                raise RuntimeError("the study's worker processes stopped")
            conns = {w["c"]: w for w in live}
            if self.prep is not None:
                conns[self.prep["c"]] = None
            for c in _wait(list(conns), timeout=0.2):
                w = conns[c]
                if w is None:
                    self._prepared(c)
                    continue
                try:
                    msg = c.recv()
                except (EOFError, OSError):
                    self._died(w, todo, tries, on_row)
                    continue
                if msg[0] == "up":
                    w["up"] = time.time()
                    self._send_blob(w)
                elif msg[0] == "loaded":
                    w["ready"], w["load_s"], w["rss"] = time.time(), msg[1], msg[2]
                elif msg[0] == "row":           # a worker is idle once every run it was sent came back
                    key = (msg[1].get("variant"), msg[1]["i"])
                    w["jobs"] = [j for j in w["jobs"] if (j.get("variant"), j["i"]) != key]
                    on_row(msg[1])
            for w in self.workers:
                if not w["dead"] and w["ready"] and not w["jobs"] and todo:
                    w["jobs"] = list(todo.popleft())
                    w["c"].send(("runs", w["jobs"]))

    def _prepared(self, c):
        try:
            msg = c.recv()
        except (EOFError, OSError):
            msg = ("failed", "the preparing process stopped")
        self.prep["p"].join(timeout=5)
        self.prep = None
        if msg[0] != "prepared":
            raise StudyError(msg[1])
        self.prepared = msg[1]
        self.blob = msg[1]["blob"]
        for w in self.workers:
            self._send_blob(w)

    def wait_prepared(self, cancel=None, tick=None) -> dict | None:
        """The preparing process's result (once it has delivered it), or None when cancelled first."""
        while self.prep is not None:
            if tick is not None:
                tick()
            if cancel is not None and cancel.is_set():
                return None
            for c in _wait([self.prep["c"]] + [w["c"] for w in self.workers if not w["dead"]], timeout=0.2):
                if c is self.prep["c"]:
                    self._prepared(c)
                    break
                w = next(w for w in self.workers if w["c"] is c)
                try:
                    msg = c.recv()
                except (EOFError, OSError):
                    w["dead"] = True
                    continue
                if msg[0] == "up":
                    w["up"] = time.time()
                elif msg[0] == "loaded":
                    w["ready"], w["load_s"], w["rss"] = time.time(), msg[1], msg[2]
        return self.prepared

    def one(self, job: dict, timeout: float = 300.0) -> dict:
        """One run with its frames (the scenario document), on the first worker once it has loaded the robot."""
        end = time.time() + timeout
        sent = None
        while time.time() < end:
            live = [w for w in self.workers if not w["dead"]]
            if not live:
                raise StudyError("The replay's simulation process stopped.")
            if sent is None:
                sent = next((w for w in live if w["ready"]), None)
                if sent is not None:
                    sent["c"].send(("doc", job))
            for c in _wait([w["c"] for w in live], timeout=0.2):
                w = next(x for x in live if x["c"] is c)
                try:
                    msg = c.recv()
                except (EOFError, OSError):
                    w["dead"] = True
                    if w is sent:
                        raise StudyError("The replay's simulation process stopped.") from None
                    continue
                if msg[0] == "up":
                    w["up"] = time.time()
                    self._send_blob(w)
                elif msg[0] == "loaded":
                    w["ready"], w["load_s"], w["rss"] = time.time(), msg[1], msg[2]
                elif msg[0] == "doc" and w is sent:
                    return msg[1]
        raise StudyError("The replay took too long; try again when the computer is less busy.")

    def _died(self, w, todo, tries, on_row):
        w["dead"] = True
        w["p"].join(timeout=1)
        for job in w["jobs"]:
            key = (job.get("variant"), job["i"])
            tries[key] = tries.get(key, 0) + 1
            if tries[key] < 2:
                todo.appendleft([job])
            else:
                on_row({"i": job["i"], **({"variant": job["variant"]} if job.get("variant") else {}),
                        "params": {k: round(v, 5) for k, v in job["params"].items()}, "outcome": "error",
                        "group": "error", "success": False, "error": "the simulation process stopped"})
        w["jobs"] = []
        if self.started < 2 * len(self.workers) + 2:
            self._start()

    def _drain(self, on_row):
        for w in self.workers:
            try:
                while not w["dead"] and w["c"].poll(0):
                    msg = w["c"].recv()
                    if msg[0] == "row":
                        on_row(msg[1])
            except (EOFError, OSError):
                pass

    def close(self, kill: bool = False):
        procs = [w for w in self.workers] + ([self.prep] if self.prep else [])
        for w in procs:
            if not kill:
                try:
                    w["c"].send(("stop",))
                except (OSError, ValueError):
                    pass
        for w in procs:
            if kill:
                w["p"].terminate()
            w["p"].join(timeout=0.5 if kill else 5)
            if w["p"].is_alive():
                w["p"].kill()
                w["p"].join(timeout=2)
            try:
                w["c"].close()
            except OSError:
                pass


# ====================================================================== a study

def _sha_file(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()[:16]


def cache_key(root: str, rev: str, timestep: float) -> tuple:
    """What a packed robot depends on: the project, the revision's design, the pipeline code and the timestep."""
    from ..robot.pipeline2 import pipeline_version

    p, rev, design = _revision(root, rev)
    return (os.path.realpath(root), rev, _sha_file(os.path.join(p.rev_dir(rev), "design.json")),
            pipeline_version(design.arm.source), float(timestep))


def _keep(cache: dict, key, prep: dict):
    """Keep a packed robot, dropping the oldest past CACHED (studies and replays share one cache across threads:
    list() of a dict is one step under the GIL, so another thread's insert can't break the loop)."""
    for old in list(cache)[:max(0, len(cache) - CACHED + 1)]:
        cache.pop(old, None)
    cache[key] = prep


def study_key(rev, design_sha, pipeline, seed, timestep, stress, spec_sha_) -> str:
    raw = json.dumps([VERSION, SCENARIO, rev, design_sha, pipeline, int(seed), float(timestep), bool(stress), spec_sha_])
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def new_study(root: str, rev: str | None = None, *, runs: int = 200, seed: int = DEFAULT_SEED, stress: bool = False,
              timestep: float = 0.001, by: str | None = None, study_id: str | None = None) -> dict:
    """The study record for these settings: the saved one (same revision, design, code, tolerances, seed, timestep
    and mode) when there is one, its run count raised to `runs`; else a new one (not saved yet). `study_id`: that
    saved study as it was defined (its own tolerances, even if the project's changed since), to resume or deepen."""
    from ..robot.pipeline2 import pipeline_version

    if isinstance(runs, bool) or not isinstance(runs, int) or not 1 <= runs <= 100_000:
        raise StudyError("Ask for 1 to 100,000 runs.")
    if study_id is not None:
        old = _read_json(os.path.join(study_dir(root, study_id), "study.json"))
        if not isinstance(old, dict) or old.get("version") != VERSION:
            raise StudyError("There is no such study.")
        p, rev, design = _revision(root, old.get("rev"))
        if _sha_file(os.path.join(p.rev_dir(rev), "design.json")) != old.get("design_sha"):
            raise StudyConflict(REPLACED)       # its rows would be of another design
        if pipeline_version(design.arm.source) != old.get("pipeline_version"):
            raise StudyConflict(CODE_CHANGED)   # its rows would be of two versions of the simulation
        studyable(p, rev)
        old["n"] = max(int(old.get("n") or 0), runs)
        return old
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2 ** 63:
        raise StudyError("The seed must be a whole number from 0.")
    if not isinstance(timestep, (int, float)) or not 0.0001 <= float(timestep) <= 0.005:
        raise StudyError("The timestep must be 0.1 to 5 ms.")
    p, rev, design = _revision(root, rev)
    studyable(p, rev)
    base = tolerances(root, rev)
    spec = stressed(base) if stress else base
    spec["sha"] = spec_sha(spec)
    dsha = _sha_file(os.path.join(p.rev_dir(rev), "design.json"))
    pv = pipeline_version(design.arm.source)
    sid = study_key(rev, dsha, pv, seed, timestep, stress, base["sha"])
    old = _read_json(os.path.join(study_dir(root, sid), "study.json"))
    if isinstance(old, dict) and old.get("version") == VERSION:
        old["n"] = max(int(old.get("n") or 0), runs)
        return old
    return {"version": VERSION, "id": sid, "project": os.path.basename(os.path.normpath(root)), "rev": rev,
            "scenario": SCENARIO, "n": runs, "seed": seed, "stress": bool(stress), "timestep": float(timestep),
            "spec": spec, "base_spec_sha": base["sha"], "design_sha": dsha, "pipeline_version": pv,
            "world": design.world.id or None, "object": noun(design.object.name), "created_utc": _utc(),
            "by": by, "status": "queued", "done": 0}


_PROCESS = f"{os.getpid()}-{os.urandom(4).hex()}"   # this process, also once a later one reuses its pid


def save_queued(root: str, doc: dict) -> bool:
    """Write a new study's record as "queued" while it waits for its turn (HeavyJobs), so a Studio that stops before it
    starts lists it as interrupted, to carry on, instead of losing it. A study that already has a record keeps it.
    True when written."""
    path = os.path.join(study_dir(root, doc["id"]), "study.json")
    if os.path.exists(path):
        return False
    _write_json(path, {**doc, "status": "queued", "done": 0, "queued_by": _PROCESS, "updated_utc": _utc()})
    return True


def drop_queued(root: str, sid: str) -> bool:
    """Remove the record save_queued() wrote for a study that never ran (cancelled while it waited, or replaced by one
    with the newest tolerances when its turn came). A study with saved runs, or that started, is kept."""
    d = study_dir(root, sid)
    doc = _read_json(os.path.join(d, "study.json"))
    if not isinstance(doc, dict) or doc.get("status") != "queued" or os.path.exists(os.path.join(d, "runs.jsonl")):
        return False
    import shutil
    shutil.rmtree(d, ignore_errors=True)
    return True


def _job(doc: dict, i: int, boxes: int, variant: str | None = None) -> dict:
    vals, dirs, ns = sample(doc["spec"]["params"], doc["seed"], i)
    p = perturbation(vals, dirs, ns, doc["spec"]["encoder_cpr"], boxes)
    if variant:                        # the packed model already runs at the study's timestep
        p.update(variant_perturb(variant, doc["timestep"]))
    return {"i": i, "params": vals, "perturb": p, **({"variant": variant} if variant else {})}


def _event(doc: dict, phase: str, rows: list, done: int, total: int, clock: dict) -> dict:
    """A `study` event: where the study is, its success rate so far with the Wilson interval, the failures so far by
    group, and an estimate of the time left (from this phase's pace)."""
    k = n = unstable = 0
    groups = {}
    for r in rows:                     # one pass: this runs after every run of a study
        if r["outcome"] in ("unstable", "error"):
            unstable += r["outcome"] == "unstable"
            continue
        n += 1
        if r["success"]:
            k += 1
        else:
            groups[r["group"]] = groups.get(r["group"], 0) + 1
    now = time.time()
    rate = (done - clock["phase_done"]) / max(now - clock["phase_t"], 1e-6) if done > clock["phase_done"] else None
    return {"type": "study", "id": doc["id"], "rev": doc["rev"], "status": doc["status"], "phase": phase, "done": done,
            "n": total, "successes": k, "failures": n - k, "unstable": unstable, "groups": groups,
            "p": round(k / n, 4) if n else None, "ci": [round(v, 4) for v in wilson(k, n)] if n else None,
            "elapsed_s": round(now - clock["t0"], 1), "eta_s": round((total - done) / rate) if rate else None,
            "first_result_s": clock["first"], "stress": doc["stress"]}


def run_study(root: str, rev: str | None = None, *, runs: int = 200, seed: int = DEFAULT_SEED, stress: bool = False,
              workers: int | None = None, timestep: float = 0.001, batch: int = 4, checks: bool = True,
              cancel: threading.Event | None = None, progress=None, by: str | None = None, cache: dict | None = None,
              nice: int | None = None, study_id: str | None = None) -> dict:
    """Run (or resume, or deepen) a study of `rev` (default: the newest finished revision) and return its record.
    `progress(event)` gets a `study` event when it starts and after every run; `cancel` (a threading.Event) stops it
    within a couple of seconds, keeping every finished run (status "cancelled"; running it again resumes). `cache`: a
    dict that keeps the packed robot between studies in one process. `study_id`: resume that study as it was defined.
    StudyError / StudyBusy before anything runs."""
    if workers is not None and (isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= MAX_WORKERS):
        raise StudyError(f"Use 1 to {MAX_WORKERS} worker processes.")
    doc = new_study(root, rev, runs=runs, seed=seed, stress=stress, timestep=timestep, by=by, study_id=study_id)
    sid, rev = doc["id"], doc["rev"]
    d = study_dir(root, sid)
    _lock(root, sid)
    emit = progress or (lambda ev: None)
    clock = {"t0": time.time(), "first": None, "beat": time.time(), "phase_t": time.time(), "phase_done": 0}
    timing = {"workers": 0, "load_avg": _load()}
    pool = None

    def save():
        doc["updated_utc"] = _utc()
        _write_json(os.path.join(d, "study.json"), doc)

    def tick():                       # the heartbeat, and the record's run count (a killed process leaves it right)
        if time.time() - clock["beat"] > 5:
            clock["beat"] = time.time()
            _beat(root)
            if doc.get("done") != len(rows):
                doc["done"] = len(rows)
                try:
                    save()
                except OSError:
                    pass

    def stop(status):
        pool.close(kill=True)
        doc.update({"status": status, "done": len(rows), "timing": {**timing, "first_result_s": clock["first"]},
                    "summary": analyze(rows, doc["spec"]["params"], None, doc["seed"], doc["object"]) if rows else None})
        save()
        emit({**_event(doc, "runs", rows, len(rows), doc["n"], clock), "summary": _headline(doc)})
        return doc

    try:
        os.makedirs(d, exist_ok=True)
        rows = read_rows(os.path.join(d, "runs.jsonl"))
        crow = read_rows(os.path.join(d, "checks.jsonl"))
        have = {r["i"] for r in rows}
        todo = [i for i in range(doc["n"]) if i not in have]
        need_checks = bool(checks) and doc.get("checks_done") != doc["n"]
        doc.update({"status": "preparing", "done": len(rows), "error": None})
        save()
        if todo or need_checks:
            key = (os.path.realpath(root), rev, doc["design_sha"], doc["pipeline_version"], float(doc["timestep"]))
            hit = (cache or {}).get(key)
            n_workers = max(1, int(workers or default_workers()))
            if not need_checks:
                n_workers = min(n_workers, len(todo))
            pool = Pool(n_workers, nice, blob=hit["blob"] if hit else None, prepare_args=(root, rev, doc["timestep"]))
            emit(_event(doc, "prepare", rows, len(rows), doc["n"], clock))
            prep = hit or pool.wait_prepared(cancel, tick)
            if prep is None:
                return stop("cancelled")
            if cache is not None and not hit:
                _keep(cache, key, prep)
            doc["model"] = {k: prep[k] for k in ("matches_revision", "revision_timestep", "nominal", "hover_mm", "world",
                                                 "boxes")}
            timing.update({"workers": n_workers, "prepare_s": None if hit else prep["prepare_s"], "prepare_cached": bool(hit)})
            doc["status"] = "running"
            save()

            def on_row(r):
                rows.append(r)
                _append(os.path.join(d, "runs.jsonl"), r)
                if clock["first"] is None:
                    clock["first"] = round(time.time() - clock["t0"], 2)
                emit(_event(doc, "runs", rows, len(rows), doc["n"], clock))

            clock.update(phase_t=time.time(), phase_done=len(rows))
            if todo and not pool.drive([_job(doc, i, prep["boxes"]) for i in todo], batch, on_row, cancel, tick):
                return stop("cancelled")
            el = time.time() - clock["phase_t"]
            timing.update({"first_result_s": clock["first"], "runs": len(todo), "runs_wall_s": round(el, 2),
                           "runs_per_s": round(len(todo) / max(el, 1e-6), 2) if todo else None})
            if need_checks:
                pick, ok_ids = check_selection(rows, doc["seed"])
                done_c = {(c["variant"], c["i"]) for c in crow}
                chosen = sorted({i for ids in pick.values() for i in ids} | set(ok_ids))
                cjobs = [_job(doc, i, prep["boxes"], v) for v in VARIANTS for i in chosen if (v, i) not in done_c]
                total = len(cjobs) + len(done_c)
                doc.update({"status": "checking", "checks_n": total})
                save()

                def on_check(r):
                    crow.append(r)
                    _append(os.path.join(d, "checks.jsonl"), r)
                    emit(_event(doc, "checks", rows, len(crow), total, clock))

                clock.update(phase_t=time.time(), phase_done=len(crow))
                if cjobs and not pool.drive(cjobs, batch, on_check, cancel, tick):
                    return stop("cancelled")
                doc["checks_done"] = doc["n"]
                timing.update({"checks_runs": len(cjobs), "checks_wall_s": round(time.time() - clock["phase_t"], 2)})
            timing["worker"] = pool.info()
            pool.close()
            pool = None
        doc["summary"] = analyze(rows, doc["spec"]["params"], crow if checks or crow else None, doc["seed"], doc["object"])
        doc.update({"status": "done", "done": len(rows), "finished_utc": _utc()})
        if timing["workers"]:
            doc["timing"] = {**timing, "total_s": round(time.time() - clock["t0"], 2)}
        save()
        emit({**_event(doc, "done", rows, len(rows), doc["n"], clock), "summary": _headline(doc)})
        return doc
    except BaseException as exc:
        if pool is not None:
            pool.close(kill=True)
            pool = None
        cut = isinstance(exc, KeyboardInterrupt)
        doc.update({"status": "cancelled" if cut else "error", "error": None if cut else _explain(exc, root)[:500]})
        try:
            save()
        except OSError:
            pass
        raise
    finally:
        if pool is not None:
            pool.close(kill=True)
        _unlock(root, sid)


def _load() -> list | None:
    try:
        return [round(v, 2) for v in os.getloadavg()]
    except (OSError, AttributeError):
        return None


def _headline(doc: dict) -> dict:
    s = doc.get("summary") or {}
    return {"n": s.get("n"), "successes": s.get("successes"), "p": s.get("p"), "wilson95": s.get("wilson95"),
            "clopper_pearson95": s.get("clopper_pearson95"),
            "groups": [{k: g.get(k) for k in ("id", "label", "count", "rate", "model_sensitive")} for g in s.get("groups", [])]}


# ====================================================================== replay and the text report

def replay(root: str, sid: str, run: int, variant: str | None = None, cache: dict | None = None, *,
           process: bool = False, nice: int | None = None, timeout: float = 300.0) -> dict:
    """One run of a study again as a sim_pick_place.json document (frames for the viewer) with a `robustness` block:
    its parameters, outcome and measurements. `cache`: as run_study's. In this process, or with `process` in a spawned
    study process at the study's priority (the Studio: MuJoCo's warning log and the CPU stay out of the server)."""
    doc = load(root, sid)
    if not doc:
        raise StudyError("There is no such study.")
    if isinstance(run, bool) or not isinstance(run, int) or not 0 <= run < max(doc.get("n") or 0, doc.get("done") or 0):
        raise StudyError("There is no such run in this study.")
    if variant is not None and variant not in VARIANTS:
        raise StudyError(f"There is no such check; use {' or '.join(VARIANTS)}.")
    if doc.get("rev_changed"):                   # the revision's name holds another design: not this study's run
        raise StudyConflict(REPLACED)
    key = (os.path.realpath(root), doc["rev"], doc["design_sha"], doc["pipeline_version"], float(doc["timestep"]))
    hit = (cache or {}).get(key)
    if not process:
        prep = hit or prepare(root, doc["rev"], doc["timestep"])
        out = Runner(prep["blob"]).run(_job(doc, run, prep["boxes"], variant), doc=True)
    else:
        end, late = time.time() + timeout, threading.Event()
        timer = threading.Timer(timeout, late.set)
        timer.daemon = True
        timer.start()
        pool = Pool(1, nice, blob=hit["blob"] if hit else None, prepare_args=(root, doc["rev"], doc["timestep"]))
        try:
            prep = hit or pool.wait_prepared(late)
            if prep is None:
                raise StudyError("The replay took too long; try again when the computer is less busy.")
            if cache is not None and not hit:
                _keep(cache, key, prep)
            out = pool.one(_job(doc, run, prep["boxes"], variant), max(1.0, end - time.time()))
        finally:
            timer.cancel()
            pool.close(kill=True)
    if "robustness" not in out:        # the run stopped with an error: its row, no frames
        from .durable import relative  # the run's own error text: files named from the projects folder
        why = relative(str(out.get("error") or "unknown"), os.path.dirname(os.path.abspath(root)))
        raise StudyError(f"Run {run} stopped with an error in the simulation: {why}")
    out["robustness"].update({"study": sid, "run": run, "seed": doc["seed"], "stress": doc["stress"]})
    return out


def _pct(v) -> str:
    return f"{v * 100:.1f}%"


def uncounted(k: int) -> str:
    """What a study says when none of its runs could be counted."""
    return ("No run could be counted: " + ("its one run was" if k == 1 else f"all {k} runs were")
            + " numerically unstable or stopped with an error.")


def report(doc: dict) -> str:
    """The study as plain ASCII text (what `morph study` prints)."""
    s = doc.get("summary") or {}
    lines = [f"{doc.get('project')} {doc['rev']}: robustness study (beta), {doc.get('done', 0)} of {doc['n']} randomized "
             f"run{_s(doc['n'])} of pick and place" + (", every range doubled (stress)" if doc.get("stress") else ""),
             f"  study {doc['id']}, seed {doc['seed']}, {doc['timestep'] * 1000:g} ms steps, status {doc['status']}"]
    if s.get("n"):
        nom = ((doc.get("model") or {}).get("nominal") or {}).get("success")
        line = f"Succeeds in {s['successes']} of {_runs(s['n'])}: {_pct(s['p'])} (95% CI {_pct(s['wilson95'][0])}-{_pct(s['wilson95'][1])})."
        if s.get("clopper_pearson95") and s["successes"] == s["n"]:
            line = ((f"Succeeds in all {s['n']} runs" if s["n"] > 1 else "Succeeds in its one run")
                    + f": at least {_pct(s['clopper_pearson95'][0])} (95%, exact).")
        if nom is not None:
            line += " The nominal run " + ("succeeds." if nom else "fails.")
        lines += [line, NOTE]
        if s.get("successes_sensitive"):
            lines.append(SUCCESSES_SENSITIVE)
        for key, (one, many) in (("unstable", ("was numerically unstable", "were numerically unstable")),
                                 ("errors", ("stopped with an error", "stopped with an error"))):
            ids = s.get(key) or []
            if ids:
                lines.append(f"{_runs(len(ids))} {one if len(ids) == 1 else many} and {'is' if len(ids) == 1 else 'are'} "
                             f"not counted (run{_s(len(ids))} {_ids(ids)}).")
        if s.get("groups"):
            lines.append("Failure groups:")
        for g in s.get("groups", []):
            lo, hi = g["wilson95"]
            lines.append(f"  {g['count']:>4} run{_s(g['count']) or ' '} ({_pct(g['rate'])}, 95% CI {_pct(lo)}-{_pct(hi)})  {g['label']}")
            if g.get("rule"):
                lines.append(f"       {g['rule']['text']}" + ("" if g["rule"]["significant"] else " (could be chance)"))
            ms = g.get("model_sensitive")
            if ms is not None:
                c = g.get("checks") or {}
                h, i1 = c.get("half_timestep") or {}, c.get("impratio_1") or {}
                lines.append(f"       {'model-sensitive' if ms else 'robust to solver settings'}: at half the timestep "
                             f"{h.get('still_fail', 0)} of {h.get('runs', 0)} still fail this way; with impratio=1, "
                             f"{i1.get('still_fail', 0)} of {i1.get('runs', 0)}.")
                if ms:
                    lines.append(f"       {SENSITIVE}")
            lines.append(f"       closest to nominal: run {g['example_run']}")
        top = [x for x in s.get("sensitivity", []) if x["significant"]][:4]
        if top:
            lines.append("Most influential: " + ", ".join(f"{x['label'].lower()} (J {x['J']:.2f})" for x in top)
                         + f"; chance level J {s.get('chance_J', 0):.2f}.")
    elif s.get("unstable") or s.get("errors"):
        lines.append(uncounted(len(s.get("unstable") or []) + len(s.get("errors") or [])))
    elif doc["status"] in ("cancelled", "error", "interrupted"):
        lines.append("No finished runs.")
    m = doc.get("model") or {}
    if m.get("matches_revision") is False:
        lines.append(f"Note: {doc['rev']} was simulated with other Morph code; this study uses today's model of it.")
    lines.append("Tolerances" + (" (stress: doubled)" if doc.get("stress") else "") + ":")
    for e in doc["spec"]["params"]:
        v = (lambda x: f"{x * 100:g}%") if e["unit"] == "x" else (lambda x: f"{x:g} {e['unit']}")
        rng = f"{v(e['lo'])} to {v(e['hi'])}" if e["on"] else f"off (nominal {v(e['nominal'])})"
        lines.append(f"  {e['label']}: {rng}  [{e['source']}]")
    t = doc.get("timing") or {}
    if t.get("workers"):
        bits = [_runs(doc.get("done", 0))]
        if t.get("runs_per_s"):
            bits.append(f"{t['runs_per_s']} runs/s on {t['workers']} worker{_s(t['workers'])}")
        if t.get("first_result_s") is not None:
            bits.append(f"first result after {t['first_result_s']} s")
        if t.get("checks_runs"):
            bits.append(f"{t['checks_runs']} check runs in {t.get('checks_wall_s')} s")
        lines.append("Timing: " + ", ".join(bits) + ".")
    if doc["status"] == "error" and doc.get("error"):
        lines.append(f"Error: {doc['error']}")
    return "\n".join(lines)


def _s(k: int) -> str:
    return "" if k == 1 else "s"


def _runs(k: int) -> str:
    return f"{k} run{_s(k)}"


def _ids(ids, k=12) -> str:
    return ", ".join(str(i) for i in ids[:k]) + (", ..." if len(ids) > k else "")
