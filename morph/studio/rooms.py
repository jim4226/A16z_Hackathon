"""Rooms: one per robot project, so each browser tab (and each person) works on its own project.

A room holds the project, its live events (Hub), its chat history (saved in <project>/.morph/chat.jsonl, so a
restart or another tab sees it) and its job queue. Chat and parameter edits wait their turn in the queue instead
of being dropped while a build runs; each job is applied to the newest revision when it starts. The Studio
(server.py) creates rooms on demand and decides what each job does; a global semaphore there limits how many
pipeline builds run at once across rooms.
"""

from __future__ import annotations

import collections
import contextvars
import json
import os
import queue
import re
import secrets
import socket
import threading
import time
import traceback
import weakref

from ..robot.pipeline2 import RobotProject
from ..services import durable


# The client whose request is being handled: the page's tab (its X-Morph-Client value) or a script. Set by the server
# around each route; a job queued meanwhile remembers it, so one tab's quick edits aren't conflicts with each other.
REQUESTER = contextvars.ContextVar("morph_requester", default=None)


# Feature hooks run when a Studio starts (Studio._recover): RESUMED fn(studio, room, job, meta) for a first build it queued
# again (a stop cut it off), STARTED fn(studio) once every project is recovered. A hook that fails never stops the Studio.
RESUMED: list = []
STARTED: list = []
HOST = socket.gethostname()

# Which Studio wrote a queue journal. Each Studio process has its own id (OWNER) and, while it runs, holds an OS lock on
# <projects>/.studios/<OWNER>.lock (claim); the system frees that lock when the process ends, however it ends (Ctrl+C,
# a crash, a kill, a power cut). So a journal whose Studio's lock is free was left by a stopped Studio, and one whose
# lock is held belongs to a Studio still running on the same projects folder, whatever the pid or the computer's name
# says (pids are reused after a reboot, and a Mac's name changes with its network).
OWNER = secrets.token_hex(8)
STUDIOS = ".studios"
JOURNAL = re.compile(r"queue(?:-([0-9a-f]{16}))?\.jsonl")  # queue-<owner>.jsonl; plain queue.jsonl: no owner known
_CLAIMS = weakref.WeakValueDictionary()  # realpath of a projects folder -> its Claim
_CLAIMS_LOCK = threading.Lock()
STALE_LOCK_S = 600  # another Studio's lock file nobody holds is removed once it is this old (never one just made)


def owner_lock(home: str, owner: str) -> str:
    return os.path.join(home, STUDIOS, f"{owner}.lock")


class Claim:
    """This process's OS lock on <home>/.studios/<OWNER>.lock: held while anything of this process uses that projects
    folder (a Studio keeps it, a JobQueue takes it before it writes its journal), released when the last of them is
    gone, and by the system when the process ends, however it ends. `real`: the file system can lock (on one that
    can't, a network share say, a second Studio on the same folder can't be told from a stopped one)."""

    def __init__(self, home: str):
        self.lock = durable.BuildLock(owner_lock(home, OWNER))
        self.lock.acquire()
        self.real = self.lock.real
        weakref.finalize(self, self.lock.release)


def claim(home: str) -> Claim:
    """The Claim of projects folder `home` (taken once; lock files other Studios left behind are removed then)."""
    key = os.path.realpath(home)
    with _CLAIMS_LOCK:
        held = _CLAIMS.get(key)
        if held is None:
            held = _CLAIMS[key] = Claim(home)
            _sweep(home)
        return held


def _sweep(home: str):
    folder, now = os.path.join(home, STUDIOS), time.time()
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for n in names:
        path = os.path.join(folder, n)
        if n == f"{OWNER}.lock" or not n.endswith(".lock"):
            continue
        try:
            if now - os.path.getmtime(path) < STALE_LOCK_S or durable.BuildLock.held(path):
                continue
            os.remove(path)
        except OSError:
            pass


def journal_owner(path: str) -> str | None:
    """The Studio (OWNER) that wrote the queue journal at `path`, from its name; None for an older plain queue.jsonl."""
    m = JOURNAL.fullmatch(os.path.basename(path))
    return m.group(1) if m else None


def journals(root: str) -> list:
    """The queue journals in project folder `root` (<root>/.morph/queue-<owner>.jsonl, one per Studio that had jobs
    open there), oldest first."""
    folder = os.path.join(root, ".morph")
    try:
        names = [n for n in os.listdir(folder) if JOURNAL.fullmatch(n)]
    except OSError:
        return []
    def when(path):
        try:
            return os.path.getmtime(path)
        except OSError:
            return 0.0
    return sorted((os.path.join(folder, n) for n in names), key=lambda path: (when(path), path))


def owner_live(home: str, owner) -> bool:
    """True when `owner` (rooms.OWNER of some process) is another process that still holds its lock on projects folder
    `home` (claim): what it left there is still its own. This process, and anything unnamed, never is."""
    return (isinstance(owner, str) and re.fullmatch(r"[0-9a-f]{16}", owner) is not None and owner != OWNER
            and durable.BuildLock.held(owner_lock(home, owner)))


def journal_live(path: str, home: str) -> bool:
    """True when the Studio that writes the journal at `path` still runs (another Studio on the same projects folder:
    its jobs are its own). This process's own journal and an older one without an owner are never live."""
    return owner_live(home, journal_owner(path))


def open_jobs(path: str) -> list:
    """The jobs a queue journal (JobQueue.journal) says were still open when its Studio stopped, in the order they were
    queued: the last line of each job, `op` "queued" (waiting or building) or "held" (a conflict waiting for an answer),
    with `started: True` when its build had started (a first build cut off mid-build) and no clean stop of the Studio
    ended it ("interrupted": Ctrl+C is no sign the build stopped the Studio). Lines that don't parse are skipped."""
    last = collections.OrderedDict()
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(e, dict) or not isinstance(e.get("id"), str):
                    continue
                if e.get("op") in ("started", "interrupted"):  # interrupted: a clean stop cut it off, no try
                    if e["id"] in last:
                        last[e["id"]]["started"] = e["op"] == "started"
                elif e.get("op") in ("queued", "held", "ended"):
                    last[e["id"]] = {**last.get(e["id"], {}), **e}  # what each line adds; the newest op wins
    except OSError:
        return []
    return [e for e in last.values() if e["op"] in ("queued", "held")]


class UnknownProject(LookupError):
    """There is no robot project with that name."""


class QueueFull(RuntimeError):
    """The room already has JobQueue.MAX_QUEUED jobs waiting."""


class Hub:
    """A room's live events. Every event gets the room's `project`; user and assistant messages are kept (and saved
    when the hub has a `path`) so a tab that opens later replays the conversation. An event with `to` (a member id)
    goes only to that member's tabs and is never saved."""

    MAX_EVENTS = 500    # per open tab: a tab that stops reading loses its oldest events (then reloads), never the build
    MAX_HISTORY = 500   # chat messages replayed to a new tab, and kept on disk
    HISTORY = ("user", "assistant")
    MAX_FILE = 8 << 20  # chat.jsonl is read from its last 8 MiB at most
    addressed = True    # an event with `to` reaches only that member's tabs (Teams sends inbox items this way)

    def __init__(self, project: str = "", path: str | None = None):
        self.project, self.path = project, path
        self.clients, self.lock = {}, threading.Lock()  # queue -> member id (None: not signed in as a member)
        self.history, self._lines = self._load()

    def _load(self) -> tuple[list, int]:
        if not self.path:
            return [], 0
        try:
            with open(self.path, "rb") as fh:
                size = fh.seek(0, os.SEEK_END)
                fh.seek(max(0, size - self.MAX_FILE))
                raw = fh.read().decode("utf-8", errors="replace").splitlines()
        except OSError:
            return [], 0
        if size > self.MAX_FILE:
            raw = raw[1:]  # the first line was cut in half
        out = []
        for line in raw:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if isinstance(ev, dict) and ev.get("type") in self.HISTORY and isinstance(ev.get("text"), str):
                out.append(ev)
        return out[-self.MAX_HISTORY:], len(raw)

    def _save(self, event: dict):
        """Append one message to chat.jsonl; past 2 x MAX_HISTORY lines, rewrite it with the last MAX_HISTORY."""
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            if self._lines >= 2 * self.MAX_HISTORY:
                durable.write_text(self.path, "".join(json.dumps(e) + "\n" for e in self.history))
                self._lines = len(self.history)
            else:
                durable.append_line(self.path, json.dumps(event))  # a cut-off last line never swallows this one
                self._lines += 1
        except (OSError, TypeError, ValueError):
            traceback.print_exc()  # a full disk loses saved chat, never the build

    def clear_history(self):
        with self.lock:
            self.history.clear()
            self._lines = 0
            if self.path:
                try:
                    os.remove(self.path)
                except OSError:
                    pass

    def subscribe(self, member: str | None = None):
        q = queue.Queue(maxsize=self.MAX_EVENTS)
        with self.lock:
            self.clients[q] = member
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.clients.pop(q, None)

    def online(self) -> list:
        """The member id of each open tab (None for tabs without one)."""
        with self.lock:
            return list(self.clients.values())

    def send(self, event: dict):
        if self.project and "project" not in event:
            event = {**event, "project": self.project}
        to = event.get("to")
        with self.lock:
            if event.get("type") in self.HISTORY and not to:
                self.history.append(event)
                del self.history[:-self.MAX_HISTORY]
                if self.path:
                    self._save(event)
            for q, member in self.clients.items():
                if to and member != to:
                    continue
                try:
                    q.put_nowait(event)
                except queue.Full:  # drop the oldest and tell the tab to reload its state
                    for _ in range(2):
                        try:
                            q.get_nowait()
                        except queue.Empty:
                            break
                    q.put_nowait({"type": "resync", **({"project": self.project} if self.project else {})})
                    q.put_nowait(event)


class Job:
    """One request waiting in a room. `fn(job)` runs in the room's worker and sets `status` when it ends otherwise
    than "done" (noop, error, conflict), `result` (the new revision) and `data` (what to apply again after a conflict:
    changes, label, reply, source)."""

    def __init__(self, kind: str, fn, *, by: str | None = None, base_rev: str | None = None, label: str = ""):
        self.id = secrets.token_hex(6)
        self.kind, self.fn, self.by, self.base_rev, self.label = kind, fn, by, base_rev, label or kind
        self.status, self.result, self.error, self.conflict, self.data = "queued", None, None, None, {}
        self.origin = REQUESTER.get()  # the tab or script that asked
        self.after = ()                # its own requests (same member and tab) still waiting or building when it asked
        self.created, self.started, self.finished = time.time(), None, None
        self._done = threading.Event()
        self.answered = threading.Event()  # a conflict got its answer: "resolved" (see successor) or "dropped"
        self.successor = None              # the job a resolved conflict's Apply queued: it builds this request

    def wait(self, timeout: float | None = None) -> bool:
        """True once the job has finished (whatever its status)."""
        return self._done.wait(timeout)

    def end(self, status: str):
        """A request answered at once, never queued (Studio.queue_chat): it is over, with `status`."""
        self.status, self.finished = status, time.time()
        self._done.set()

    def answer(self, status: str, successor: "Job | None" = None):
        """This conflict was answered: Apply queued `successor` ("resolved"), or it was dropped."""
        self.successor = successor
        self.status = status
        self.answered.set()

    def info(self, position: int | None = None) -> dict:
        out = {"id": self.id, "kind": self.kind, "by": self.by, "status": self.status, "position": position,
               "label": self.label[:120], "base_rev": self.base_rev}
        if self.result:
            out["rev"] = self.result
        if self.conflict:
            out["conflict"] = self.conflict
        return out


class JobQueue:
    """First in, first out, one job at a time per room. The worker thread starts with the first job and ends when the
    queue is empty. Every change is published to the room as a `queue` event. A job that ended as a conflict waits for
    an answer (Apply or Drop) in `waiting` until someone gives one, however many jobs run meanwhile.

    The queue is journaled in <project>/.morph/queue-<OWNER>.jsonl (`journal`, one file per Studio process): a line
    when a job is queued (`record`: what it takes to tell about it, or to build it again), when a first build starts
    building (started), when it ends as a conflict waiting for an answer, and when it ends otherwise. The file goes once
    nothing waits or runs. A Studio that stopped with jobs open leaves it; the next Studio finds it when it starts
    (Studio._recover): a first build is built again, an edit is named in the chat, a conflict waits again."""

    MAX_QUEUED = 10
    KEEP = 50            # finished jobs remembered for wait/resolve
    MAX_CONFLICTS = 50   # conflicts waiting for an answer; past it the oldest is dropped (and the room is told)

    def __init__(self, room: "Room"):
        self.room = room
        self.cond = threading.Condition()  # an RLock inside: snapshot() may run under publish()
        self.pending = collections.deque()
        self.running = None
        self.jobs = collections.OrderedDict()
        self.waiting = collections.OrderedDict()  # job id -> a conflict nobody has answered yet
        self.worker = None
        p = getattr(room, "p", None)
        self.journal = os.path.join(p.root, ".morph", f"queue-{OWNER}.jsonl") if p is not None else None
        self.home = os.path.dirname(p.root) if p is not None else None
        self.claim = None  # this process's lock on the projects folder, taken before the first journal line
        self.cuttable = set()  # ids of jobs journaled "started": a stop from then on cuts them off

    # -- the journal
    def _log(self, op: str, job: Job, **fields):
        """One journal line (under self.cond). A journal that can't be written never stops the queue."""
        if not self.journal:
            return
        try:
            if self.claim is None:  # the lock that tells other Studios this journal's writer still runs
                self.claim = claim(self.home)
            os.makedirs(os.path.dirname(self.journal), exist_ok=True)
            durable.append_line(self.journal, json.dumps({"op": op, "id": job.id, "studio": OWNER, "pid": os.getpid(),
                                                          "host": HOST, **fields}, default=str))
        except (OSError, TypeError, ValueError):
            traceback.print_exc()

    def started(self, job: Job):
        """`job`'s build has the build slot and starts now (Studio.build): a stop from here on cut it off."""
        with self.cond:
            self.cuttable.add(job.id)
            self._log("started", job)

    def stopping(self):
        """The Studio stops cleanly (Ctrl+C, serve() returning): a first build running now is cut off by that stop, not
        by anything in its design, so the journal says so ("interrupted") and the next start doesn't count it as a try
        (open_jobs). Never raises."""
        with self.cond:
            job = self.running
            if job is not None and job.id in self.cuttable:
                self._log("interrupted", job)

    def _ended(self, job: Job):
        self._log("ended", job, status=job.status)
        self._settle()

    def _settle(self):
        """Nothing waits, runs or waits for an answer: the journal has nothing left to tell, so it goes."""
        if self.journal and self.running is None and not self.pending and not self.waiting:
            try:
                os.remove(self.journal)
            except OSError:
                pass

    def restore(self, entry: dict):
        """A conflict the journal says was still waiting for an answer when the Studio stopped: it waits again (its
        question is in the chat history, so the tabs show Apply and Drop for it)."""
        with self.cond:
            job = Job(entry.get("kind") or "change", None, by=entry.get("by"), base_rev=entry.get("base_rev"),
                      label=entry.get("label") or "")
            job.id, job.origin = entry["id"], entry.get("origin")
            job.status, job.conflict = "conflict", entry.get("conflict") or {}
            job.data = dict(entry.get("data") or {})
            job.finished = time.time()
            job._done.set()
            self.waiting[job.id] = job
            self._log("held", job, **self._held(job))
        return job

    @staticmethod
    def _held(job: Job) -> dict:
        return {"kind": job.kind, "label": job.label, "by": job.by, "origin": job.origin, "base_rev": job.base_rev,
                "conflict": job.conflict, "data": job.data}

    def submit(self, kind: str, fn, *, by: str | None = None, base_rev: str | None = None, label: str = "",
               announce=None, record: dict | None = None) -> Job:
        """Queue `fn(job)`. `announce(job)` runs before the job can start (to send the user's message first). `record`:
        what the journal keeps besides kind, label and who asked (a first build's design, a change request's id).
        Raises QueueFull."""
        with self.cond:
            if len(self.pending) >= self.MAX_QUEUED:
                raise QueueFull(f"{self.MAX_QUEUED} changes are already waiting for {self.room.name}. Try again when "
                                "some of them have been built.")
            job = Job(kind, fn, by=by, base_rev=base_rev, label=label)
            # what the same person asked from the same tab before this and is still waiting or building: this request
            # builds on it, so a change it makes is never a conflict for this one (server.Studio._conflict)
            job.after = tuple(j for j in [self.running, *self.pending]
                              if j is not None and j.by == job.by and j.origin == job.origin)
            if announce:
                announce(job)
            self.pending.append(job)
            self.jobs[job.id] = job
            while len(self.jobs) > self.KEEP + self.MAX_QUEUED + 1:
                self.jobs.popitem(last=False)
            self._log("queued", job, kind=kind, label=job.label, by=by, base_rev=base_rev, ts=time.time(),
                      **(record or {}))
            self.publish()
            if self.worker is None or not self.worker.is_alive():
                self.worker = threading.Thread(target=self._work, daemon=True, name=f"morph-room-{self.room.name}")
                self.worker.start()
        return job

    def get(self, job_id) -> Job | None:
        with self.cond:
            if not isinstance(job_id, str):
                return None
            return self.jobs.get(job_id) or self.waiting.get(job_id)

    def hold(self, job: Job, conflict: dict):
        """`job` ends as a conflict and waits for an answer: listed in conflicts() (state, `queue` events) until
        take_conflict() hands it to whoever answers first."""
        with self.cond:
            job.status, job.conflict = "conflict", conflict
            self.waiting[job.id] = job
            self._log("held", job, **self._held(job))
            gone = []
            while len(self.waiting) > self.MAX_CONFLICTS:
                _, old = self.waiting.popitem(last=False)
                old.answer("dropped")
                gone.append(old)
                self._log("ended", old, status="dropped")
        for old in gone:
            self.publish(old)
            self.room.hub.send({"type": "assistant", "text": f"Dropped without an answer: {old.label[:80]} "
                                f"({self.MAX_CONFLICTS} newer changes were waiting for one).", "noop": True, "job": old.id,
                                **({"by": old.by} if old.by else {})})

    def take_conflict(self, job_id) -> Job | None:
        """The waiting conflict `job_id` for the one answer it gets (two tabs answering at once: one gets it), or None."""
        with self.cond:
            job = self.waiting.pop(job_id, None) if isinstance(job_id, str) else None
            if job is None or job.status != "conflict":
                return None
            self._log("ended", job, status="answered")  # a full queue holds it again (hold: "held" once more)
            self._settle()
            return job

    def drop_conflicts(self) -> list:
        """Every waiting conflict ends as dropped (Reset demo removed the revisions, and the chat, they were about)."""
        with self.cond:
            gone = list(self.waiting.values())
            self.waiting.clear()
            for job in gone:
                job.answer("dropped")
                self._log("ended", job, status="dropped")
            self._settle()
        for job in gone:
            self.publish(job)
        return gone

    def conflicts(self) -> list:
        """The conflicts waiting for an answer, oldest first (position None, status "conflict", conflict {...})."""
        with self.cond:
            return [j.info() for j in self.waiting.values()]

    def cancel(self, job_id) -> Job | None:
        """Take a job that has not started out of the queue."""
        with self.cond:
            job = self.get(job_id)
            if job is None or job not in self.pending:
                return None
            self.pending.remove(job)
            job.status, job.finished = "cancelled", time.time()
            job._done.set()
            self._ended(job)
        self.publish(job)
        return job

    def busy(self) -> bool:
        with self.cond:
            return self.running is not None or bool(self.pending)

    def snapshot(self) -> list:
        """The running job (position 0) and the waiting ones (1, 2, ...)."""
        with self.cond:
            out = [self.running.info(0)] if self.running else []
            return out + [j.info(i + 1) for i, j in enumerate(self.pending)]

    def publish(self, finished: Job | None = None):
        """A `queue` event with the current jobs (plus one that just finished, with its final status). Sent under the
        queue's lock, so tabs never get an older picture after a newer one."""
        with self.cond:
            jobs = self.snapshot() + ([finished.info()] if finished is not None else [])
            try:
                self.room.hub.send({"type": "queue", "jobs": jobs, "conflicts": self.conflicts()})
            except Exception:  # noqa: BLE001  (an event that can't be sent must not stop the queue)
                traceback.print_exc()

    def _work(self):
        while True:
            with self.cond:
                if not self.pending:
                    self.worker = None
                    self._settle()
                    return
                job = self.pending.popleft()
                job.status, job.started = "running", time.time()
                self.running = job
            self.publish()
            try:
                with self.room.lock:
                    job.fn(job)
                if job.status == "running":
                    job.status = "done"
            except Exception as exc:  # noqa: BLE001  (the room keeps working after a failed job)
                traceback.print_exc()
                job.status, job.error = "error", f"{exc.__class__.__name__}: {exc}"
                self.room.hub.send({"type": "assistant", "text": f"Error: {exc}", "job": job.id})
            finally:
                job.finished = time.time()
                job.after = ()  # read when it started; a chain of quick edits keeps no finished job alive through it
                with self.cond:
                    self.running = None
                    self.cuttable.discard(job.id)
                    if job.status != "conflict":  # a conflict stays open in the journal ("held") until it is answered
                        self._log("ended", job, status=job.status)
                    self._settle()
                    self.publish(job)
                job._done.set()


class Room:
    """One robot project's live state. `lock` is held while one of its jobs (or the demo reset) works on the project,
    so a room builds one revision at a time; `sourcing` lets a Darwin search run beside a build."""

    def __init__(self, name: str, project: RobotProject | str):
        self.name = name
        self.p = project if isinstance(project, RobotProject) else RobotProject(project)
        self.hub = Hub(name, os.path.join(self.p.root, ".morph", "chat.jsonl"))
        self.lock = threading.RLock()
        self.jobs = JobQueue(self)
        self.sourcing = threading.Lock()
        self.live = None  # the revision being built: {rev, parent, dirty, nodes: {node: status}}
        self.last_target = None  # chat: the grip target (N) "gentler grip" scales when a revision reports none

    def emit(self, event: dict):
        """Send a pipeline event, remembering the build's progress for tabs that open the project mid-build."""
        kind = event.get("type")
        if kind == "start":
            self.live = {"rev": event.get("rev"), "parent": event.get("parent"), "dirty": list(event.get("dirty") or []),
                         "nodes": {}}
        elif kind == "node" and self.live is not None:
            self.live["nodes"][event.get("node")] = event.get("status")
        elif kind == "done":
            self.live = None
        self.hub.send(event)

    def building(self) -> dict | None:
        live = self.live
        return {**live, "nodes": dict(live["nodes"])} if live else None

    def completed(self) -> list:
        """Revisions with readable results: what tabs show. A revision still being built, cut off or damaged doesn't
        count (Project.finished)."""
        return self.p.finished()

    def latest(self) -> str | None:
        revs = self.completed()
        return revs[-1] if revs else None
