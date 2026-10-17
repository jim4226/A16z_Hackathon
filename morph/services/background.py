"""Worker threads for network lookups that never keep the process alive once it is told to stop.

concurrent.futures.ThreadPoolExecutor joins its threads when the interpreter exits, and its threads first work through
every task still queued. A Studio stopped with Ctrl+C while Darwin lookups hung (a server that accepts and never
answers, an SSL read that never returns) therefore waited for each of them to time out in turn: about a minute for one
"Find agents", during which Ctrl+C did nothing. DaemonPool has the interface the lookups use (submit, map, `with`),
but its threads are daemons that nothing joins at exit: the code that asked still waits for its answers (the `with`
block, map(), Future.result()), the process ending never does. Standard library only.
"""

from __future__ import annotations

import queue
import threading
from concurrent.futures import Future


class DaemonPool:
    """At most `workers` daemon threads running submitted calls in order. `with DaemonPool(n) as pool:` waits at the
    end for what was submitted, like ThreadPoolExecutor; exiting the process never waits for it."""

    def __init__(self, workers: int, name: str = "morph-lookup"):
        self._max, self._name = max(1, int(workers)), name
        self._tasks: queue.SimpleQueue = queue.SimpleQueue()
        self._threads: list = []
        self._lock = threading.Lock()
        self._closed = False

    def submit(self, fn, /, *args, **kwargs) -> Future:
        future = Future()
        with self._lock:
            if self._closed:
                raise RuntimeError("cannot schedule new work after shutdown")
            self._tasks.put((future, fn, args, kwargs))
            if len(self._threads) < self._max:
                t = threading.Thread(target=self._work, name=f"{self._name}-{len(self._threads)}", daemon=True)
                self._threads.append(t)
                t.start()
        return future

    def map(self, fn, *iterables) -> list:
        """fn over the items, in their order (a list, not an iterator); an exception is raised as map() would."""
        futures = [self.submit(fn, *args) for args in zip(*iterables)]
        return [f.result() for f in futures]

    def _work(self):
        while True:
            task = self._tasks.get()
            if task is None:
                return
            future, fn, args, kwargs = task
            if not future.set_running_or_notify_cancel():
                continue
            try:
                result = fn(*args, **kwargs)
            except BaseException as exc:  # noqa: BLE001  (handed to whoever waits for it, as concurrent.futures does)
                future.set_exception(exc)
            else:
                future.set_result(result)

    def shutdown(self, wait: bool = True):
        with self._lock:
            self._closed, threads = True, list(self._threads)
        for _ in threads:
            self._tasks.put(None)
        if wait:
            for t in threads:
                t.join()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.shutdown(wait=True)
        return False
