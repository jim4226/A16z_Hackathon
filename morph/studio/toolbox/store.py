"""Private files of the toolbox under MORPH_HOME: `.tools/` (state, approvals, the audit log, the owner's own
manifests in `.tools/catalog/`) and `.runs/` (one JSON file per agent run).

Every file is written atomically (a private temp file in the same folder, then os.replace) and readable by this user
only (0600, folders 0700). Read-modify-write goes through `locked(folder)`: a thread lock plus a lock file opened with
O_EXCL, so the CLI and a Studio sharing a home never lose each other's changes. A run is driven by one thread of one
process at a time through `Claim`, an OS file lock (fcntl.flock / msvcrt.locking) that the system releases when a
process dies, so a crash never leaves a run claimed. Standard library only.
"""

from __future__ import annotations

import json
import os
import threading
import time

TOOLS = ".tools"
RUNS = ".runs"
_LOCKS: dict = {}
_GUARD = threading.Lock()


def home_dir(home: str | None = None) -> str:
    return os.path.abspath(home or os.environ.get("MORPH_HOME") or "projects")


def tools_dir(home: str | None = None) -> str:
    return os.path.join(home_dir(home), TOOLS)


def runs_dir(home: str | None = None) -> str:
    return os.path.join(home_dir(home), RUNS)


def utc(t: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if t is None else t))


def env_items(env) -> list:
    """(name, value) pairs of an environment, read safely while other threads set or remove keys: os.environ copies
    its names atomically, and a name removed meanwhile is skipped instead of raising KeyError."""
    data = env.copy() if isinstance(env, dict) else {k: env.get(k) for k in list(env)}
    return [(k, v) for k, v in data.items() if isinstance(v, str)]


def ensure_dir(folder: str):
    os.makedirs(folder, mode=0o700, exist_ok=True)


def read_json(path: str, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_text(path: str, text: str):
    """Replace `path` with `text` (UTF-8), atomically, synced to the disk and readable by this user only."""
    from ...services import durable
    ensure_dir(os.path.dirname(os.path.abspath(path)))
    durable.write_text(path, text, private=True, prefix=".tmp.")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def write_json(path: str, data):
    write_text(path, json.dumps(data, ensure_ascii=False, separators=(",", ":")))


class _FileLock:
    """A lock file created with O_EXCL; one older than `stale` seconds belongs to a process that died mid-write (every
    write under it takes milliseconds) and is broken."""

    def __init__(self, path: str, timeout: float = 10.0, stale: float = 30.0):
        self.path, self.timeout, self.stale, self.fd = path, timeout, stale, None

    def __enter__(self):
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                try:
                    os.write(self.fd, f"{os.getpid()} {time.time():.0f}".encode())
                except BaseException:  # a full disk: never keep the descriptor, nor a lock file nobody holds
                    os.close(self.fd)
                    self.fd = None
                    try:
                        os.remove(self.path)
                    except OSError:
                        pass
                    raise
                return self
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(self.path) > self.stale:
                        os.remove(self.path)
                        continue
                except OSError:
                    continue
                if time.monotonic() > deadline:
                    raise TimeoutError(f"Morph's tool files are busy ({self.path}); try again.") from None
                time.sleep(0.01)

    def __exit__(self, *exc):
        try:
            os.close(self.fd)
        finally:
            try:
                os.remove(self.path)
            except OSError:
                pass


class locked:
    """`with locked(folder):` holds the folder's thread lock and its .lock file (re-entrant within a thread)."""

    def __init__(self, folder: str):
        key = os.path.normcase(os.path.abspath(folder))
        with _GUARD:
            self.g = _LOCKS.setdefault(key, [threading.RLock(), 0, None])
        self.folder = folder

    def __enter__(self):
        g = self.g
        g[0].acquire()
        try:
            if g[1] == 0:
                ensure_dir(self.folder)
                fl = _FileLock(os.path.join(self.folder, ".lock"))
                fl.__enter__()
                g[2] = fl
            g[1] += 1
        except BaseException:
            g[0].release()
            raise
        return self

    def __exit__(self, *exc):
        g = self.g
        try:
            g[1] -= 1
            if g[1] == 0 and g[2] is not None:
                fl, g[2] = g[2], None
                fl.__exit__(*exc)
        finally:
            g[0].release()


class Claim:
    """The right to drive one run: `acquire()` is non-blocking and False while another thread or process holds it.
    The lock is the OS's (released with the process), held on `<runs>/<id>.lock`, and travels with this object, so the
    thread that checked a run can hand it to the thread that drives it."""

    _held: set = set()
    _guard = threading.Lock()

    def __init__(self, path: str):
        self.path, self.key, self.fd = path, os.path.normcase(os.path.abspath(path)), None
        self.handed_off = False  # set once a driving thread owns it: from then on only that thread releases it

    def acquire(self) -> bool:
        with Claim._guard:
            if self.key in Claim._held:
                return False
            Claim._held.add(self.key)
        try:
            ensure_dir(os.path.dirname(self.path))
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            self._forget()
            return False
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            self._forget()
            return False
        self.fd = fd
        return True

    def release(self):
        fd, self.fd = self.fd, None
        if fd is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
            self._forget()

    @property
    def held(self) -> bool:
        return self.fd is not None

    def _forget(self):
        with Claim._guard:
            Claim._held.discard(self.key)
