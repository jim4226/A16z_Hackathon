"""Team storage: MORPH_HOME/.team/ holds team.json (members, hashed invites, device links and script tokens,
the policy), the HMAC `secret` (0600) and the hash-chained audit log; each project keeps its own collaboration
files in <project>/.team/.

Every write is atomic (a temp file in the same folder, then os.replace) and happens under a thread lock plus a
lock file opened with O_EXCL, so the `morph team` CLI and a running Studio never lose each other's changes.
Standard library only; works the same on Windows, macOS and Linux.
"""

from __future__ import annotations

import calendar
import copy
import json
import os
import secrets
import tempfile
import threading
import time

TEAM = ".team"
VERSION = 1
POLICY = {
    "viewers_can_comment": True,      # viewers may comment and @mention
    "viewers_can_propose": True,      # viewers may open change requests (an editor applies them)
    "viewers_can_approve": False,     # viewers holding a discipline may approve it
    "unreleased_downloads": "warn",   # order packages of an unreleased revision: "warn" = editors may (logged, draft); "owner"
    "specialist_required": {"pilot": [], "production": []},  # disciplines a generalist can't approve at that build level
}
COLORS = ("#7091ff", "#3dd68c", "#f5b942", "#ff9152", "#a78bfa", "#2dd4d4", "#ff6b6b", "#e3c35a", "#5ea8ff", "#f472b6")
_LOCKS: dict = {}
_LOCKS_GUARD = threading.Lock()


def home_dir(home: str | None = None) -> str:
    from ...resources import projects_home
    return os.path.abspath(home or projects_home())


def team_dir(home: str | None = None) -> str:
    return os.path.join(home_dir(home), TEAM)


def utc(t: float | None = None) -> str:
    """ISO 8601 UTC with a Z suffix, to the second."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if t is None else t))


def parse_ts(s) -> float | None:
    """Epoch seconds from a ledger or team timestamp. Old ledger lines are local time without a zone."""
    if not isinstance(s, str) or len(s) < 19:
        return None
    try:
        tm = time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    return float(calendar.timegm(tm)) if s.endswith("Z") else time.mktime(tm)


def new_id(prefix: str, n: int = 5) -> str:
    return prefix + secrets.token_hex(n)


class _Guard:
    """One folder's in-process lock: an RLock plus how deep the owning thread holds it and its lock file."""

    def __init__(self):
        self.rlock, self.depth, self.fl = threading.RLock(), 0, None


def _guard(path: str) -> _Guard:
    key = os.path.normcase(os.path.abspath(path))
    with _LOCKS_GUARD:
        g = _LOCKS.get(key)
        if g is None:
            g = _LOCKS[key] = _Guard()
        return g


class FileLock:
    """A lock file created with O_EXCL: portable across processes. A lock older than `stale` seconds belongs to a
    process that died mid-write and is broken (every write here takes milliseconds)."""

    def __init__(self, path: str, timeout: float = 10.0, stale: float = 30.0):
        self.path, self.timeout, self.stale, self.fd = path, timeout, stale, None

    def __enter__(self):
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.write(self.fd, f"{os.getpid()} {time.time():.0f}".encode())
                return self
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(self.path) > self.stale:
                        os.remove(self.path)
                        continue
                except OSError:
                    continue
                if time.monotonic() > deadline:
                    raise TimeoutError(f"Morph team files are busy ({self.path} is held by another Morph process); try again.")
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
        self.folder, self.g = folder, _guard(folder)

    def __enter__(self):
        g = self.g
        g.rlock.acquire()
        try:
            if g.depth == 0:
                ensure_dir(self.folder)
                fl = FileLock(os.path.join(self.folder, ".lock"))
                fl.__enter__()
                g.fl = fl
            g.depth += 1
        except BaseException:
            g.rlock.release()
            raise
        return self

    def __exit__(self, *exc):
        g = self.g
        try:
            g.depth -= 1
            if g.depth == 0 and g.fl is not None:
                fl, g.fl = g.fl, None
                fl.__exit__(*exc)
        finally:
            g.rlock.release()


def ensure_dir(folder: str, private: bool = False):
    os.makedirs(folder, exist_ok=True)
    if private and os.name != "nt":
        try:
            os.chmod(folder, 0o700)
        except OSError:
            pass


def write_json(path: str, data, private: bool = False):
    """Atomic JSON write: temp file in the same folder, fsync, os.replace (retried briefly: Windows refuses to
    replace a file another process is reading)."""
    folder = os.path.dirname(path)
    ensure_dir(folder)
    fd, tmp = tempfile.mkstemp(prefix=".tmp.", suffix=".json", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, indent=1, ensure_ascii=False)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        if private and os.name != "nt":
            os.chmod(tmp, 0o600)
        for attempt in range(20):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.05)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: str, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return copy.deepcopy(default)


def append_jsonl(path: str, obj: dict):
    """One JSON object per line. Callers hold locked(folder), so lines never interleave."""
    ensure_dir(os.path.dirname(path))
    with open(path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")


def read_jsonl(path: str) -> list:
    """Every parseable object line; a torn last line (a crash mid-write) is skipped."""
    out = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
    except OSError:
        pass
    return out


def exists(home: str | None = None) -> bool:
    return os.path.isfile(os.path.join(team_dir(home), "team.json"))


class TeamStore:
    """team.json in MORPH_HOME/.team/: read() is cached by the file's mtime and size (it runs on every request);
    update(fn) re-reads under the lock, lets fn change the dict, then writes it atomically."""

    _cache: dict = {}

    def __init__(self, home: str | None = None):
        self.home = home_dir(home)
        self.dir = team_dir(home)
        self.path = os.path.join(self.dir, "team.json")
        self.secret_path = os.path.join(self.dir, "secret")

    def exists(self) -> bool:
        return os.path.isfile(self.path)

    def _stat(self):
        try:
            st = os.stat(self.path)
            return st.st_mtime_ns, st.st_size
        except OSError:
            return None

    def read(self) -> dict:
        key = self._stat()
        hit = self._cache.get(self.path)
        if key is None:
            return {}
        if hit is None or hit[0] != key:
            data = read_json(self.path, {})
            if not isinstance(data, dict):
                data = {}
            hit = self._cache[self.path] = (key, data)
        return copy.deepcopy(hit[1])

    def update(self, fn):
        with locked(self.dir):
            data = read_json(self.path, {}) if self.exists() else {}
            if not isinstance(data, dict):
                data = {}
            out = fn(data)
            data["version"] = VERSION
            write_json(self.path, data, private=True)
            self._cache.pop(self.path, None)
            return out

    def members(self, include_revoked: bool = False) -> dict:
        ms = self.read().get("members") or {}
        return {k: v for k, v in ms.items() if include_revoked or not v.get("revoked")}

    def member(self, mid: str) -> dict | None:
        return (self.read().get("members") or {}).get(mid) if isinstance(mid, str) else None

    def policy(self) -> dict:
        return merged_policy(self.read().get("policy"))

    _secrets: dict = {}

    def secret(self) -> bytes:
        """The HMAC key for sessions: 32 random bytes, created once, readable only by this user (0600).
        Deleting it signs everyone out. Cached by the file's mtime (it is checked on every request)."""
        try:
            key = os.stat(self.secret_path).st_mtime_ns
            hit = self._secrets.get(self.secret_path)
            if hit and hit[0] == key:
                return hit[1]
            with open(self.secret_path, encoding="ascii") as fh:
                raw = fh.read().strip()
            if len(raw) >= 64:
                self._private(self.secret_path)
                self._secrets[self.secret_path] = (key, bytes.fromhex(raw[:64]))
                return self._secrets[self.secret_path][1]
        except (OSError, ValueError):
            pass
        with locked(self.dir):
            try:
                with open(self.secret_path, encoding="ascii") as fh:
                    raw = fh.read().strip()
                if len(raw) >= 64:
                    return bytes.fromhex(raw[:64])
            except (OSError, ValueError):
                pass
            ensure_dir(self.dir, private=True)
            tmp = self.secret_path + ".new"
            try:
                os.remove(tmp)
            except OSError:
                pass
            fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w", encoding="ascii") as fh:
                fh.write(secrets.token_hex(32) + "\n")
            os.replace(tmp, self.secret_path)
            self._private(self.secret_path)
            with open(self.secret_path, encoding="ascii") as fh:
                return bytes.fromhex(fh.read().strip()[:64])

    @staticmethod
    def _private(path: str):
        if os.name != "nt":
            try:
                if os.stat(path).st_mode & 0o077:
                    os.chmod(path, 0o600)
            except OSError:
                pass


def merged_policy(raw) -> dict:
    out = copy.deepcopy(POLICY)
    if isinstance(raw, dict):
        for k, v in raw.items():
            if k in out:
                out[k] = copy.deepcopy(v)
    return out


def project_dir(root: str) -> str:
    """<project>/.team/: comments, proposals, reviews, branches and releases of one project."""
    return os.path.join(root, TEAM)
