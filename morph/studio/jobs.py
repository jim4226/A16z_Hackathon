"""Heavy jobs (beta): compute-heavy work on a project that runs beside the Studio's build queue, never in it.

A heavy job (today a robustness study, morph/services/robust.py) is saved to disk as it runs, reports its progress to
the project's room as events, can be cancelled within about 2 s keeping every finished run, and can be resumed after
a restart (it carries on from its saved runs, with the same outcome as if it had never stopped). It never takes the
room's lock, its job queue or the Studio's build slots: its processes come from their own limit (MORPH_STUDY_WORKERS,
default the computer's cores minus one) at a lower priority (MORPH_STUDY_NICE, default 5). One heavy job per project
at a time (also across processes: the CLI beside the Studio); jobs on different projects take turns, MORPH_HEAVY_JOBS
at once (default 1). A study only reads its revision: it never changes a revision or a stage hash.

    jobs = manager(studio)                     # one per Studio; HeavyJobs(home, emit=fn(project, event)) elsewhere
    jobs.start_study(project, rev=None, *, runs=200, seed=1, stress=False, workers=None, timestep=0.001, checks=True,
                     by=None) -> job              # StudyError (bad input), StudyBusy (one is running there)
    jobs.status(project) -> job | None          # the project's current (or last) job, with its last event
    jobs.cancel(project) -> job | None          jobs.wait(project, timeout) -> bool
    jobs.resume(project, study_id) -> job       jobs.interrupted() -> [{project, id, rev, done, n}]
    jobs.result(project, study_id, rows=False) -> study       jobs.studies(project) -> [study summaries]
    jobs.tolerances(project, rev=None) / jobs.save_tolerances(project, changes, rev=None, by=None) -> spec
    jobs.replay(project, study_id, run) -> sim_pick_place.json document of one run (frames for the viewer)
    stop(studio, project) / shutdown(studio)    the Studio's hooks: a project deleted or reset, the Studio stopping
A job is {id, kind, project, rev, status: queued|preparing|running|checking|done|cancelled|error, n, done, error,
event}; its events are `study` events to the project's room (see robust._event): {id, rev, status, phase, done, n,
successes, failures, unstable, p, ci, eta_s, elapsed_s, first_result_s, summary (when it ends)}.
"""

from __future__ import annotations

import os
import threading
import time
import traceback
from collections import deque

from ..services import durable, robust
from ..services.robust import StudyBusy, StudyError  # noqa: F401  (callers catch them from here)
from . import projects

EVENT_GAP_S = 0.25              # progress events to a room at most this often (phase changes and the end always go)


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name) or default))
    except ValueError:
        return default


class Job:
    def __init__(self, project: str, root: str, params: dict, study: dict, by: str | None):
        self.project, self.root, self.params, self.by = project, root, params, by
        self.id, self.rev, self.kind = study["id"], study["rev"], "study"
        self.seed, self.stress, self.timestep = study["seed"], study["stress"], study["timestep"]
        self.status, self.error, self.event, self.n = "queued", None, None, study["n"]
        self.cancel, self.finished = threading.Event(), threading.Event()
        self.created, self.started, self.ended, self.cancel_asked = time.time(), None, None, None
        self.thread, self.queued_record = None, False

    def view(self) -> dict:
        ev = self.event or {}
        return {"id": self.id, "kind": self.kind, "project": self.project, "rev": self.rev, "status": self.status,
                "n": ev.get("n", self.n), "done": ev.get("done", 0), "error": self.error, "by": self.by,
                "event": dict(ev) if ev else None,
                "cancel_s": round(self.ended - self.cancel_asked, 2) if self.ended and self.cancel_asked else None}


class HeavyJobs:
    """The Studio's heavy jobs: started per project, run on their own threads (the study's worker processes do the
    computing), queued across projects, reported to `emit(project, event)`."""

    def __init__(self, home: str, emit=None, max_running: int | None = None, cache: dict | None = None):
        self.home = os.path.realpath(home)
        self.emit = emit or (lambda project, event: None)
        self.max_running = max_running or _env_int("MORPH_HEAVY_JOBS", 1)
        self.lock = threading.Lock()
        self.jobs: dict[str, Job] = {}        # project -> its current or last job
        self.waiting: deque[Job] = deque()
        self.running: set = set()
        self.cache = {} if cache is None else cache   # robust.run_study keeps the last packed robots here

    def _root(self, project) -> str:
        root = projects.project_root(self.home, project)
        if not root:
            raise StudyError(f"There is no robot project called {project}.")
        return root

    # -- starting, resuming, cancelling
    def start_study(self, project: str, rev: str | None = None, *, runs: int = 200, seed: int = robust.DEFAULT_SEED,
                    stress: bool = False, workers: int | None = None, timestep: float = 0.001, checks: bool = True,
                    by: str | None = None, study_id: str | None = None) -> dict:
        """Queue a robustness study of `rev` (default: the newest finished revision); it starts at once unless
        another project's heavy job holds the last slot. The same settings again resume or deepen the saved study."""
        root = self._root(project)
        if workers is not None and (isinstance(workers, bool) or not isinstance(workers, int)
                                    or not 1 <= workers <= robust.MAX_WORKERS):
            raise StudyError(f"Use 1 to {robust.MAX_WORKERS} workers.")
        study = robust.new_study(root, rev, runs=runs, seed=seed, stress=stress, timestep=timestep, by=by,
                                 study_id=study_id)
        params = {"runs": study["n"], "workers": workers, "checks": bool(checks), "study_id": study_id}
        with self.lock:
            cur = self.jobs.get(project)
            if cur is not None and not cur.finished.is_set():
                raise StudyBusy(f"A robustness study is already {'waiting' if cur.status == 'queued' else 'running'} "
                                f"for {project}.")
            if robust.active(root):
                raise StudyBusy(f"A robustness study is already running for {project} (started from another Studio or a "
                                "terminal).")
            job = self.jobs[project] = Job(project, root, params, study, by)
            try:  # on disk while it waits: a Studio that stops first lists it as interrupted, to carry on
                job.queued_record = robust.save_queued(root, study)
            except OSError:
                job.queued_record = False
            self.waiting.append(job)
            self._send(job, self._event(job, "queued"), force=True)
            view = job.view()
            self._pump()
        return view

    def resume(self, project: str, study_id: str, by: str | None = None) -> dict:
        """Carry on with a saved study (interrupted by a restart, or cancelled) from its saved runs, as it was defined."""
        root = self._root(project)
        doc = robust.load(root, study_id)
        if not doc:
            raise StudyError("There is no such study.")
        return self.start_study(project, doc["rev"], runs=int(doc["n"]), by=by, study_id=study_id)

    def cancel(self, project: str, keep: bool = False) -> dict | None:
        """Stop the project's study (within about 2 s): finished runs are kept and it can be resumed. One still waiting
        for its turn goes as if never asked for, unless `keep` (the Studio is stopping: it is listed as interrupted)."""
        with self.lock:
            job = self.jobs.get(project)
            if job is None or job.finished.is_set():
                return None
            job.cancel_asked = time.time()
            job.cancel.set()
            if job in self.waiting:
                self.waiting.remove(job)
                job.status, job.ended = "cancelled", time.time()
                job.finished.set()
                if job.queued_record and not keep:
                    self._drop_queued(job, job.id)
                self._send(job, self._event(job, "cancelled"), force=True)
        return job.view()

    @staticmethod
    def _drop_queued(job: Job, sid: str):
        try:
            robust.drop_queued(job.root, sid)
        except (OSError, StudyError):
            pass

    def wait(self, project: str, timeout: float | None = None) -> bool:
        job = self.jobs.get(project)
        return True if job is None else job.finished.wait(timeout)

    def shutdown(self, timeout: float = 5.0):
        """Cancel every job (the Studio is stopping); their saved runs stay resumable."""
        for project in list(self.jobs):
            self.cancel(project, keep=True)
        for job in list(self.jobs.values()):
            job.finished.wait(timeout)

    # -- reading
    def status(self, project: str) -> dict | None:
        job = self.jobs.get(project)
        return job.view() if job else None

    def result(self, project: str, study_id: str, rows: bool = False) -> dict | None:
        return robust.load(self._root(project), study_id, rows=rows)

    def studies(self, project: str) -> list:
        out = robust.studies(self._root(project))
        job = self.jobs.get(project)
        for s in out:
            if job is not None and s["id"] == job.id and not job.finished.is_set():
                s.update({"status": job.status, "event": job.view()["event"]})
        return out

    def interrupted(self) -> list:
        """Studies that stopped with the process running them (a restart) and can carry on (their revision and Morph's
        code are still theirs): {project, id, rev, done, n}."""
        out = []
        for info in projects.list_projects(self.home):
            job = self.jobs.get(info["name"])
            for s in robust.studies(os.path.join(self.home, info["name"])):
                if s.get("rev_missing") or s.get("rev_changed") or s.get("code_changed"):
                    continue
                if s["status"] == "interrupted" and not (job and job.id == s["id"] and not job.finished.is_set()):
                    out.append({"project": info["name"], **{k: s[k] for k in ("id", "rev", "done", "n")}})
        return out

    def tolerances(self, project: str, rev: str | None = None) -> dict:
        return robust.tolerances(self._root(project), rev)

    def save_tolerances(self, project: str, changes: dict, rev: str | None = None, by: str | None = None) -> dict:
        return robust.save_tolerances(self._root(project), changes, rev, by)

    def replay(self, project: str, study_id: str, run: int, variant: str | None = None) -> dict:
        """One run again with its frames, in a study process (a few seconds; the first one of a revision also rebuilds
        its robot): the Studio's own process never runs it."""
        return robust.replay(self._root(project), study_id, run, variant, cache=self.cache, process=True)

    # -- running
    def _pump(self):
        while self.waiting and len(self.running) < self.max_running:
            job = self.waiting.popleft()
            self.running.add(job)
            job.thread = threading.Thread(target=self._run, args=(job,), daemon=True, name=f"morph-study-{job.project}")
            job.thread.start()

    def _run(self, job: Job):
        job.started, job.status = time.time(), "preparing"
        p = job.params
        try:
            doc = robust.run_study(job.root, job.rev, runs=p["runs"], workers=p["workers"], checks=p["checks"],
                                   cancel=job.cancel, progress=lambda ev: self._progress(job, ev), by=job.by,
                                   cache=self.cache, study_id=p["study_id"], seed=job.seed, stress=job.stress,
                                   timestep=job.timestep)
            job.id, job.status = doc["id"], doc["status"]
        except Exception as exc:  # noqa: BLE001  (a failed study ends as an error event; the Studio keeps going)
            if not isinstance(exc, StudyError):
                traceback.print_exc()
            job.status, job.error = "error", durable.explain(exc, self.home)[:500]
            self._send(job, {**self._event(job, "error"), "error": job.error}, force=True)
        finally:
            job.ended = time.time()
            with self.lock:
                self.running.discard(job)
                self._pump()
            job.finished.set()

    def _event(self, job: Job, status: str) -> dict:
        ev = dict(job.event or {})
        ev.update({"type": "study", "id": job.id, "rev": job.rev, "status": status, "n": ev.get("n", job.n),
                   "done": ev.get("done", 0)})
        return ev

    def _progress(self, job: Job, ev: dict):
        prev = job.event or {}
        job.status = ev.get("status", job.status)
        if ev.get("id", job.id) != job.id and job.queued_record:  # the tolerances changed while it waited: a new study
            job.queued_record = False                                # runs, and the one written for the wait goes
            self._drop_queued(job, job.id)
        job.id = ev.get("id", job.id)            # the study that runs (tolerances edited while it waited: a new one)
        force = ev.get("phase") != prev.get("phase") or ev.get("status") != prev.get("status") or "summary" in ev
        self._send(job, ev, force=force)

    def _send(self, job: Job, ev: dict, force: bool = False):
        now = time.time()
        job.event = ev
        if not force and now - getattr(job, "_sent", 0.0) < EVENT_GAP_S:
            return
        job._sent = now
        try:
            self.emit(job.project, dict(ev))
        except Exception:  # noqa: BLE001  (a room that went away never stops the study)
            traceback.print_exc()


_MAKE = threading.Lock()


def manager(studio) -> HeavyJobs:
    """The Studio's heavy jobs (made on first use): events go to each project's room."""
    with _MAKE:
        jobs = getattr(studio, "heavy_jobs", None)
        if jobs is None:
            def emit(project, event):
                try:
                    studio.room(project).hub.send(event)
                except LookupError:   # the project was deleted
                    pass
            jobs = studio.heavy_jobs = HeavyJobs(studio.home, emit=emit)
    return jobs


def stop(studio, project: str, timeout: float = 5.0) -> bool:
    """Cancel a project's study and wait for it to end (its project is about to be deleted or reset); True when none
    runs any more. Nothing to do on a Studio that never ran a heavy job."""
    jobs = getattr(studio, "heavy_jobs", None)
    if jobs is None or jobs.cancel(project) is None:
        return True
    return jobs.wait(project, timeout)


def shutdown(studio, timeout: float = 5.0):
    """The Studio is stopping: cancel its studies (their finished runs stay, and the same settings carry on)."""
    jobs = getattr(studio, "heavy_jobs", None)
    if jobs is not None:
        jobs.shutdown(timeout)
