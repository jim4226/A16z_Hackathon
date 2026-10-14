"""Robustness studies (beta) in the Studio (contracts section 16): start, follow, cancel and read a project's studies,
edit its tolerances, and replay one run in the 3D viewer. Studies run on the Studio's computer, beside the build queue
and never in it (with `morph studio --remote` that is the host everyone's browser uses: how a team brings its own
compute); their progress reaches the project's tabs as `study` events (morph/studio/jobs.py).

    GET  /api/study?project=[&rev=]           the studies, the running (or last) job, the stopped ones that can carry
                                              on, limits, where it runs, the card's copy, and the newest study of `rev`
                                              (default: the newest revision), plain and stress
    GET  /api/study?project=&id=              one study's record with the card's sentences
    POST /api/study {runs?, stress?, rev?, seed?, timestep_ms?, workers?, checks?}
                                              start a study, or deepen the saved one with these settings: 202 {job}
    POST /api/study/cancel                    stop the project's study; its finished runs are kept
    POST /api/study/resume {id}               carry on with a study a restart or a cancel cut short
    GET  /api/study/tolerances?project=[&rev=]    the tolerance spec, each range with where it comes from
    POST /api/study/tolerances {changes, rev?}    edit it: {name: {lo?, hi?, on?}}, {world_preset: name} or {reset: true}
    GET  /api/study/replay?project=&id=&run=[&variant=]
                                              one run again with its frames (a sim_pick_place.json document)

Starting, cancelling, resuming and editing tolerances need an editor; reading and replays need a viewer. A study's
size is capped here (MAX_RUNS one-millisecond runs, at most one worker process per core); `morph study` in a terminal
takes larger ones.
"""

from __future__ import annotations

import itertools
import math
import os
import socket
import threading
from collections import OrderedDict

from ..services import robust
from . import jobs
from .routes import HTTPError

MAX_RUNS = 10_000                  # one-millisecond runs a study started from the Studio may ask for
TIMESTEPS_MS = (0.5, 1.0, 2.0)
DEFAULT_RUNS = 200
RATE_PER_WORKER = 0.7              # runs per second per worker process at 1 ms, under load (B2-S1: 0.5-1.0 measured)
REPLAY_WAIT_S = 120.0              # a replay waits this long for the one before it
REPLAYS_KEPT = 8
NOTES = {
    "beta": "Robustness (beta): many randomized runs of this revision's pick and place, each in a slightly different "
            "world drawn from the tolerances below.",
    "none": "One nominal run. Small differences in placement or hardware aren't tested.",
    "simulation": "This is simulation, for the tolerances shown; not a field failure rate.",
    "sensitive": robust.SENSITIVE,
    "robust": "Still fails the same way at half the timestep and with softer friction (impratio=1): not a quirk of the "
              "simulator's settings.",
    "unchecked": "Not re-run under other solver settings yet.",
    "chance": "could be chance",
    "outdated": "The tolerances changed after this study: it is outdated. Run it again for the ones shown now.",
    "rev_missing": "Its revision no longer exists.",
    "rev_changed": "Its revision was deleted (Reset demo) and the name now holds another design: this study is not of it.",
    "other_code": "{rev} was simulated with other Morph code; this study uses today's model of it.",
    "estimate": "Estimated from about one second of computing per run at 1 ms steps.",
}
STATUS_TEXT = {"interrupted": "Stopped when the Studio stopped", "cancelled": "Cancelled", "error": "Stopped with an error"}


def _err(status: int, message: str):
    return status, {"ok": False, "error": message}


def _by(req) -> str | None:
    m = getattr(req, "member", None)
    return m.get("id") if isinstance(m, dict) else None


def _may_edit(req) -> bool:
    """What the page offers this person: without Teams everyone who reaches the Studio edits; with it, editors."""
    m = getattr(req, "member", None)
    if not isinstance(m, dict):
        return True
    from .teams import roles
    return roles.at_least(m.get("role"), "editor")


def _study_error(exc: robust.StudyError):
    """A StudyError as an HTTP answer: busy 409, something that isn't there 404, a replay to try again 503, a bad
    request 400."""
    text = str(exc)
    if isinstance(exc, robust.StudyBusy):
        return _err(409, text)
    if text.startswith(("There is no", "This project has no finished revision r")):
        return _err(404, text)
    if text.startswith("This project has no finished revision"):
        return _err(409, text)
    if text.startswith("The replay"):        # it took too long or its process stopped: worth another try later
        return _err(503, text)
    return _err(400, text)


def _cores() -> int:
    return max(1, os.cpu_count() or 1)


# ---------------------------------------------------------------------- the card's sentences

def _pct(v: float, down: bool, fine: bool) -> str:
    """A bound of an interval as a percentage, rounded away from the estimate (a lower bound down, an upper one up)."""
    k = 10 if fine else 1
    x = (math.floor if down else math.ceil)(round(v * 100 * k, 6)) / k
    return f"{x:.1f}" if fine else f"{x:.0f}"


def interval(lo: float, hi: float) -> str:
    fine = hi - lo < 0.04
    return f"{_pct(lo, True, fine)}-{_pct(hi, False, fine)}%"


def headline(doc: dict) -> str | None:
    """The study's result in one sentence (heavy_plan section 6, note 1): "Succeeds in 140 of 200 randomized runs (95%
    CI 63-76%); the nominal run succeeds." None before its first counted run."""
    s = doc.get("summary") or {}
    n, k = s.get("n") or 0, s.get("successes") or 0
    if not n:
        return None
    runs = "randomized run" + ("" if n == 1 else "s")
    cp = s.get("clopper_pearson95")
    if k == n and cp:
        text = (f"Succeeds in all {n} {runs}" if n > 1 else "Succeeds in its one randomized run") + \
               f" (at least {_pct(cp[0], True, True)}% with 95% confidence)"
    elif k == 0 and cp:
        text = f"Succeeds in 0 of {n} {runs} (95% CI 0-{_pct(cp[1], False, True)}%)"
    else:
        lo, hi = s.get("wilson95") or robust.wilson(k, n)
        text = f"Succeeds in {k} of {n} {runs} (95% CI {interval(lo, hi)})"
    if doc.get("stress"):
        text = "With every tolerance doubled (stress test), s" + text[1:]
    nominal = ((doc.get("model") or {}).get("nominal") or {}).get("success")
    if nominal is not None:
        text += "; the nominal run " + ("succeeds." if nominal else "fails.")
    else:
        text += "."
    return text


def _checks_text(c: dict) -> str | None:
    h, i = (c or {}).get("half_timestep") or {}, (c or {}).get("impratio_1") or {}
    if not h.get("runs") and not i.get("runs"):
        return None
    return (f"Re-run at half the timestep, {h.get('still_fail', 0)} of {h.get('runs', 0)} still fail this way; "
            f"with impratio=1, {i.get('still_fail', 0)} of {i.get('runs', 0)}.")


def card(doc: dict) -> dict:
    """What the Robustness card shows for a study record: the headline, the honesty notes that apply, and each
    failure group in words (its share with the interval, the rule that best isolates it, whether it survives other
    solver settings, and the run closest to nominal to replay)."""
    s = doc.get("summary") or {}
    n = s.get("n") or 0
    flags = []
    if doc.get("rev_changed"):
        flags.append(NOTES["rev_changed"])
    elif doc.get("rev_missing"):
        flags.append(NOTES["rev_missing"])
    elif doc.get("outdated"):
        flags.append(NOTES["outdated"])
    if doc.get("status") in STATUS_TEXT and doc.get("done", 0) < doc.get("n", 0):
        flags.append(f"{STATUS_TEXT[doc['status']]} after {doc.get('done', 0)} of {doc.get('n', 0)} runs; the numbers are "
                     "for those runs. The same settings carry on from there.")
    elif doc.get("status") == "error" and doc.get("error"):
        flags.append(f"Stopped with an error: {doc['error']}")
    for key, one, many in (("unstable", "was numerically unstable", "were numerically unstable"),
                           ("errors", "stopped with an error", "stopped with an error")):
        ids = s.get(key) or []
        if ids:
            flags.append(f"{len(ids)} run{'' if len(ids) == 1 else 's'} {one if len(ids) == 1 else many} and "
                         f"{'is' if len(ids) == 1 else 'are'} not counted.")
    if (doc.get("model") or {}).get("matches_revision") is False:
        flags.append(NOTES["other_code"].format(rev=doc.get("rev")))
    groups = []
    for g in s.get("groups") or []:
        rule = g.get("rule") or None
        lo, hi = g.get("wilson95") or (0.0, 1.0)
        ms = g.get("model_sensitive")
        groups.append({"id": g["id"], "label": g["label"], "count": g["count"],
                       "share": f"{g['count']} of {n} runs ({interval(lo, hi)}, 95% CI)",
                       "rule": (rule["text"] + ("" if rule.get("significant") else f" ({NOTES['chance']})")) if rule else None,
                       "significant": bool(rule and rule.get("significant")),
                       "model_sensitive": ms,
                       "sensitivity": NOTES["sensitive"] if ms else NOTES["robust"] if ms is False else NOTES["unchecked"],
                       "checks": _checks_text(g.get("checks")), "example_run": g.get("example_run")})
    return {"headline": headline(doc), "note": NOTES["simulation"], "flags": flags, "groups": groups,
            "stress": bool(doc.get("stress"))}


def _view(doc: dict) -> dict:
    """A study record for the page: everything but its rows, with the card's sentences."""
    out = {k: v for k, v in doc.items() if k not in ("runs", "checks")}
    out["card"] = card(doc)
    return out


# ---------------------------------------------------------------------- GET /api/study

def _newest(studies: list, rev: str, stress: bool):
    for s in studies:                          # newest first
        if s["rev"] == rev and bool(s.get("stress")) == stress and not s.get("rev_changed") and s.get("done"):
            return s
    return None


def _result(hj, project: str, sid: str):
    try:
        return hj.result(project, sid)
    except robust.StudyError:
        return None


def _rate(hj, project: str, listing: list, shown: list) -> dict:
    """Runs per second per worker process at 1 ms on this computer: the shown studies' timing, else the project's
    newest timed study (of the eight newest), else the estimate."""
    seen = {d["id"] for d in shown}
    for doc in itertools.chain(shown, (_result(hj, project, s["id"]) for s in listing[:8] if s["id"] not in seen)):
        t = (doc or {}).get("timing") or {}
        if t.get("runs_per_s") and t.get("workers") and (t.get("runs") or 0) >= 8:
            per = t["runs_per_s"] / t["workers"] * (0.001 / (doc.get("timestep") or 0.001))  # a run at 1 ms costs ~2x 2 ms
            return {"per_worker": round(per, 3), "measured": True}
    return {"per_worker": RATE_PER_WORKER, "measured": False}


def get_study(req):
    room, hj = req.room, jobs.manager(req.studio)
    project = room.name
    sid = req.arg("id")
    try:
        if sid is not None:
            doc = hj.result(project, sid)
            if not doc:
                return _err(404, "There is no such study.")
            return 200, {"ok": True, "study": _view(doc)}
        listing = hj.studies(project)
    except robust.StudyError as exc:
        return _study_error(exc)
    revs = room.completed() if hasattr(room, "completed") else room.p.revs()
    rev = req.arg("rev")
    if rev is not None and rev not in revs:
        return _err(404, "That revision doesn't exist or isn't finished.")
    rev = rev or (revs[-1] if revs else None)
    full = {}
    for key, stress in (("latest", False), ("latest_stress", True)):
        s = _newest(listing, rev, stress) if rev else None
        doc = hj.result(project, s["id"]) if s else None
        full[key] = _view(doc) if doc else None
    try:
        obj = robust.noun(room.p.load_design(rev).object.name) if rev else "object"
    except (OSError, ValueError, KeyError, TypeError):
        obj = "object"
    labels = {g: robust.group_label(g, obj) for g in robust.LABELS if g != "world_collision"}
    labels.update({f"world_collision:{ph}": robust.group_label(f"world_collision:{ph}", obj) for ph in robust.PHASES})
    rate = _rate(hj, project, listing, [d for d in (full["latest"], full["latest_stress"]) if d])
    remote = bool(getattr(req.studio, "remote", False))
    job = hj.status(project)
    live = job["id"] if job and job["status"] in robust.LIVE else None
    stopped = [{k: s[k] for k in ("id", "rev", "done", "n", "status", "stress")} for s in listing
               if s["status"] in ("interrupted", "cancelled") and s["id"] != live and (s.get("done") or 0) < (s.get("n") or 0)
               and not s.get("rev_missing") and not s.get("rev_changed")]
    return 200, {"ok": True, "project": project, "rev": rev, "job": job, "studies": listing, **full, "stopped": stopped,
                 "limits": {"max_runs": MAX_RUNS, "max_workers": _cores(), "workers": min(robust.default_workers(), _cores()),
                            "timesteps_ms": list(TIMESTEPS_MS), "default_runs": DEFAULT_RUNS},
                 "host": {"name": socket.gethostname(), "cores": _cores(), "remote": remote},
                 "rate": rate, "may": {"start": _may_edit(req)}, "notes": NOTES, "group_labels": labels,
                 "scenario": robust.SCENARIO}


# ---------------------------------------------------------------------- POST /api/study

def _int(body: dict, key: str, lo: int, hi: int, what: str):
    v = body.get(key)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise HTTPError(400, what)
    return v


def start_study(req):
    room, body = req.room, req.body or {}
    cores = _cores()
    ts = body.get("timestep_ms", 1.0)
    if isinstance(ts, bool) or not isinstance(ts, (int, float)) or float(ts) not in TIMESTEPS_MS:
        return _err(400, "The timestep must be 0.5, 1 or 2 ms.")
    ts = float(ts) / 1000.0
    cap = int(MAX_RUNS * ts / 0.001)
    try:
        runs = _int(body, "runs", 1, cap, f"Ask for 1 to {cap:,} runs at {ts * 1000:g} ms steps. For more, run `morph "
                                          "study` in a terminal on the Studio's computer.")
        workers = _int(body, "workers", 1, cores, f"The Studio's computer has {cores} CPU core{'s' if cores != 1 else ''}"
                                                  f": use 1 to {cores} worker processes.")
        seed = _int(body, "seed", 0, 2 ** 31 - 1, "The seed must be a whole number from 0 to 2147483647.")
    except HTTPError as exc:
        return _err(exc.status, exc.message)
    rev, stress, checks = body.get("rev"), body.get("stress", False), body.get("checks", True)
    if rev is not None and not isinstance(rev, str):
        return _err(400, "rev must be a revision name such as r002.")
    if not isinstance(stress, bool) or not isinstance(checks, bool):
        return _err(400, "stress and checks are true or false.")
    try:
        job = jobs.manager(req.studio).start_study(room.name, rev, runs=runs or DEFAULT_RUNS,
                                                   seed=robust.DEFAULT_SEED if seed is None else seed, stress=stress,
                                                   workers=workers, timestep=ts, checks=checks, by=_by(req))
    except robust.StudyError as exc:
        return _study_error(exc)
    return 202, {"ok": True, "job": job}


def cancel_study(req):
    job = jobs.manager(req.studio).cancel(req.room.name)
    if job is None:
        return _err(409, f"No robustness study is running for {req.room.name}.")
    return 200, {"ok": True, "job": job}


def resume_study(req):
    sid = (req.body or {}).get("id")
    if not isinstance(sid, str):
        return _err(400, "Send the id of the study to carry on with.")
    try:
        job = jobs.manager(req.studio).resume(req.room.name, sid, by=_by(req))
    except robust.StudyError as exc:
        return _study_error(exc)
    return 202, {"ok": True, "job": job}


# ---------------------------------------------------------------------- tolerances

def _limits(spec: dict) -> dict:
    """Each entry with the range an edit may use (`limit`, in its own unit), and the world presets."""
    for e in spec["params"]:
        a, b = robust.LIMITS[e["name"]]
        if e["name"] == "object_mass_g":
            a, b = a * e["nominal"], b * e["nominal"]
        e["limit"] = [round(a, 4), round(b, 4)]
    spec["presets"] = sorted(robust.WORLD_PRESETS)
    return spec


def get_tolerances(req):
    try:
        spec = jobs.manager(req.studio).tolerances(req.room.name, req.arg("rev"))
    except robust.StudyError as exc:
        return _study_error(exc)
    return 200, {"ok": True, "spec": _limits(spec)}


def save_tolerances(req):
    body = req.body or {}
    changes, rev = body.get("changes"), body.get("rev")
    if not isinstance(changes, dict) or not changes:
        return _err(400, "Send {changes: {name: {lo, hi, on}}}, {changes: {world_preset: name}} or {changes: {reset: true}}.")
    if rev is not None and not isinstance(rev, str):
        return _err(400, "rev must be a revision name such as r002.")
    try:
        spec = jobs.manager(req.studio).save_tolerances(req.room.name, changes, rev, by=_by(req))
    except robust.StudyError as exc:
        return _study_error(exc)
    return 200, {"ok": True, "spec": _limits(spec)}


# ---------------------------------------------------------------------- GET /api/study/replay

_REPLAY = threading.BoundedSemaphore(1)  # one replay at a time on the Studio's computer
_REPLAYS: OrderedDict = OrderedDict()   # (root, study, run, variant) -> its document, the newest last
_KEPT = threading.Lock()


def replay(req):
    room, hj = req.room, jobs.manager(req.studio)
    sid, raw, variant = req.arg("id"), req.arg("run"), req.arg("variant") or None
    if not isinstance(raw, str) or not raw.isdigit() or len(raw) > 6:
        return _err(400, "Name the run to replay: run=<number>.")
    run = int(raw)
    key = (room.p.root, sid, run, variant)
    with _KEPT:
        doc = _REPLAYS.get(key)
        if doc is not None:
            _REPLAYS.move_to_end(key)
    if doc is None:
        if not _REPLAY.acquire(timeout=REPLAY_WAIT_S):
            return _err(503, "Another replay is being prepared on the Studio's computer; try again in a moment.")
        try:
            doc = hj.replay(room.name, sid, run, variant)
            study = hj.result(room.name, sid, rows=True) or {}
        except robust.StudyError as exc:
            return _study_error(exc)
        except OSError as exc:               # no process to replay it in (memory, open files)
            return _err(503, "The Studio couldn't start a simulation process for the replay "
                             f"({exc.strerror or exc.__class__.__name__}); try again in a moment.")
        finally:
            _REPLAY.release()
        rows = study.get("checks") if variant else study.get("runs")
        rec = next((r for r in rows or [] if r.get("i") == run and r.get("variant") == variant), None)
        rb = doc["robustness"]
        rb["recorded"] = {k: rec.get(k) for k in ("outcome", "group", "place_error_mm")} if rec else None
        rb["matches_record"] = None if not rec else (rec.get("outcome") == rb.get("outcome") and
                                                     rec.get("place_error_mm") == rb.get("place_error_mm"))
        rb["label"] = robust.group_label(rb.get("group") or rb.get("outcome") or "", study.get("object") or "object")
        rb["rev"] = study.get("rev")
        with _KEPT:
            _REPLAYS[key] = doc
            while len(_REPLAYS) > REPLAYS_KEPT:
                _REPLAYS.popitem(last=False)
    return 200, doc


# ---------------------------------------------------------------------- registration

ROUTES = (("GET", "/api/study", get_study, {"role": "viewer"}),
          ("POST", "/api/study", start_study, {}),
          ("POST", "/api/study/cancel", cancel_study, {}),
          ("POST", "/api/study/resume", resume_study, {}),
          ("GET", "/api/study/tolerances", get_tolerances, {"role": "viewer"}),
          ("POST", "/api/study/tolerances", save_tolerances, {}),
          ("GET", "/api/study/replay", replay, {"role": "viewer"}))


def register(route=None):
    """Add the study routes to the Studio's route table (morph.studio.routes.route by default)."""
    if route is None:
        from .routes import route
    for method, path, fn, kw in ROUTES:
        route(method, path, **kw)(fn)


register()  # the Studio loads every api_*.py at start (contracts section 2)
