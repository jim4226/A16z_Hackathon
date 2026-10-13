"""Rooms: one per robot project, so each browser tab (and each person) works on its own project.

A room holds the project, its live events (Hub), its chat history (saved in <project>/.morph/chat.jsonl, so a
restart or another tab sees it) and its job queue. Chat and parameter edits wait their turn in the queue instead
of being dropped while a build runs; each job is applied to the newest revision when it starts. The Studio
(server.py) creates rooms on demand and decides what each job does; a global semaphore there limits how many
pipeline builds run at once across rooms.
"""

from __future__ import annotations

import collections
import json
import os
import queue
import secrets
import tempfile
import threading
import time
import traceback

from ..robot.pipeline2 import RobotProject


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
                fd, tmp = tempfile.mkstemp(prefix=".chat.", suffix=".tmp", dir=os.path.dirname(self.path))
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                    fh.writelines(json.dumps(e) + "\n" for e in self.history)
                os.replace(tmp, self.path)
                self._lines = len(self.history)
            else:
                with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
                    fh.write(json.dumps(event) + "\n")
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
        self.created, self.started, self.finished = time.time(), None, None
        self._done = threading.Event()

    def wait(self, timeout: float | None = None) -> bool:
        """True once the job has finished (whatever its status)."""
        return self._done.wait(timeout)

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
    queue is empty. Every change is published to the room as a `queue` event."""

    MAX_QUEUED = 10
    KEEP = 50  # finished jobs remembered for wait/resolve

    def __init__(self, room: "Room"):
        self.room = room
        self.cond = threading.Condition()  # an RLock inside: snapshot() may run under publish()
        self.pending = collections.deque()
        self.running = None
        self.jobs = collections.OrderedDict()
        self.worker = None

    def submit(self, kind: str, fn, *, by: str | None = None, base_rev: str | None = None, label: str = "",
               announce=None) -> Job:
        """Queue `fn(job)`. `announce(job)` runs before the job can start (to send the user's message first).
        Raises QueueFull."""
        with self.cond:
            if len(self.pending) >= self.MAX_QUEUED:
                raise QueueFull(f"{self.MAX_QUEUED} changes are already waiting for {self.room.name}. Try again when "
                                "some of them have been built.")
            job = Job(kind, fn, by=by, base_rev=base_rev, label=label)
            if announce:
                announce(job)
            self.pending.append(job)
            self.jobs[job.id] = job
            while len(self.jobs) > self.KEEP + self.MAX_QUEUED + 1:
                self.jobs.popitem(last=False)
            self.publish()
            if self.worker is None or not self.worker.is_alive():
                self.worker = threading.Thread(target=self._work, daemon=True, name=f"morph-room-{self.room.name}")
                self.worker.start()
        return job

    def get(self, job_id) -> Job | None:
        with self.cond:
            return self.jobs.get(job_id) if isinstance(job_id, str) else None

    def cancel(self, job_id) -> Job | None:
        """Take a job that has not started out of the queue."""
        with self.cond:
            job = self.get(job_id)
            if job is None or job not in self.pending:
                return None
            self.pending.remove(job)
            job.status, job.finished = "cancelled", time.time()
            job._done.set()
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
                self.room.hub.send({"type": "queue", "jobs": jobs})
            except Exception:  # noqa: BLE001  (an event that can't be sent must not stop the queue)
                traceback.print_exc()

    def _work(self):
        while True:
            with self.cond:
                if not self.pending:
                    self.worker = None
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
                with self.cond:
                    self.running = None
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
        """Revisions with results: what tabs show. A revision still being built (or cut off) has only design.json."""
        return [r for r in self.p.revs() if os.path.isfile(os.path.join(self.p.rev_dir(r), "results.json"))]

    def latest(self) -> str | None:
        revs = self.completed()
        return revs[-1] if revs else None
