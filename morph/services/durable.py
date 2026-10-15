"""Files that survive a crash, a power cut or a full disk.

Morph's records are JSON files replaced whole and JSON-lines files that only grow. Either can be cut short: a process
stopped mid-write, a disk that filled up, a power cut after a rename the disk hadn't written yet. The helpers here are
what every writer uses so that a cut never costs more than the record being written:

    append_line(path, line)      one JSON line; a cut-off last line is ended first, so the new record never joins it
    write_text(path, text)       replace a file whole: a temp file beside it, fsync, os.replace, the folder synced
    write_json(path, obj)        the same for JSON
    readable_json(path)          the file holds a JSON object (cached by size and mtime): a finished record
    BuildLock(path)              an OS lock held while a revision builds (or a Studio runs: rooms.claim); the system
                                 frees it when the process ends, however it ends
    disk_full(exc), explain(exc) "The disk is full" instead of "[Errno 28] No space left on device"
    process_alive(pid)           whether a process of this computer still runs (a study's writer)

Standard library only. Outside pipeline_version (morph/services/), so editing this file never rebuilds a robot.
"""

from __future__ import annotations

import errno
import json
import os
import tempfile
import threading
import time

DISK_FULL = "The disk is full: free some space on this computer, then try again."


def append_line(path: str, line) -> bool:
    """Append `line` (str or bytes, without its newline) and a newline to a JSON-lines file. A last line left without its
    newline (an append cut off by a crash or a full disk) is ended first, so the new record starts on a line of its own
    and survives: readers skip the cut-off line, never the next one. True when there was such a line to end.

    One write per call, so lines from two processes appending at once never interleave."""
    data = line.encode("utf-8") if isinstance(line, str) else bytes(line)
    with open(path, "ab+") as fh:
        end = fh.seek(0, os.SEEK_END)
        torn = False
        if end:
            fh.seek(end - 1)
            torn = fh.read(1) != b"\n"
        fh.write((b"\n" if torn else b"") + data + b"\n")
    return torn


def fsync_dir(folder: str):
    """Make a rename in `folder` reach the disk (POSIX; Windows has no directory handles to sync, NTFS logs it)."""
    if os.name == "nt":
        return
    try:
        fd = os.open(folder, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def write_text(path: str, text, *, private: bool = False, prefix: str = ".tmp."):
    """Replace `path` with `text` (str, written as UTF-8 with \\n line ends, or bytes): a temp file in the same folder,
    flushed and fsynced, then os.replace (retried for a second: Windows refuses while another process reads the old
    file), then the folder synced. Readers see the old file or the new one, never part of one, also after a power cut.
    `private`: readable by this user only (0600). The temp file is removed on any failure, which is raised."""
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    data = text.encode("utf-8") if isinstance(text, str) else bytes(text)
    fd, tmp = tempfile.mkstemp(prefix=prefix, suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
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
    fsync_dir(folder)


def write_json(path: str, obj, *, indent=1, private: bool = False, **dumps):
    """write_text() of json.dumps(obj, indent=indent, **dumps) and a newline."""
    write_text(path, json.dumps(obj, indent=indent, **dumps) + "\n", private=private)


_READABLE: dict = {}
_READABLE_LOCK = threading.Lock()


def readable_json(path: str) -> bool:
    """True when `path` holds a JSON object. A record written by write_text() is whole or absent, but a power cut can
    still leave one empty, and a hand edit can break one: such a file doesn't count. Cached by the file's mtime and
    size, so a revision's results are parsed once per process."""
    try:
        st = os.stat(path)
    except OSError:
        return False
    key = (st.st_mtime_ns, st.st_size)
    with _READABLE_LOCK:
        hit = _READABLE.get(path)
    if hit is not None and hit[0] == key:
        return hit[1]
    try:
        with open(path, encoding="utf-8") as fh:
            ok = isinstance(json.load(fh), dict)
    except (OSError, ValueError):
        ok = False
    with _READABLE_LOCK:
        _READABLE[path] = (key, ok)
        while len(_READABLE) > 8192:
            _READABLE.pop(next(iter(_READABLE)))
    return ok


def disk_full(exc) -> bool:
    return isinstance(exc, OSError) and exc.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC))


def explain(exc) -> str:
    """What to tell a person about `exc`: DISK_FULL for a full disk (or quota), else the error's own text."""
    return DISK_FULL if disk_full(exc) else str(exc) or exc.__class__.__name__


def process_alive(pid) -> bool:
    """Whether process `pid` of this computer still runs; True when that can't be told."""
    import sys
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return True
    try:
        if sys.platform == "win32":                # os.kill(pid, 0) would end the process there
            import ctypes
            from ctypes import wintypes
            k = ctypes.WinDLL("kernel32", use_last_error=True)
            k.OpenProcess.restype, k.OpenProcess.argtypes = wintypes.HANDLE, (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            k.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
            k.CloseHandle.argtypes = (wintypes.HANDLE,)
            h = k.OpenProcess(0x1000, False, pid)     # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return ctypes.get_last_error() != 87   # ERROR_INVALID_PARAMETER: no such process
            try:
                code = wintypes.DWORD()
                return not k.GetExitCodeProcess(h, ctypes.byref(code)) or code.value == 259   # STILL_ACTIVE
            finally:
                k.CloseHandle(h)
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (OSError, AttributeError, ValueError):
        return True
    return True


class BuildLock:
    """An OS file lock (fcntl.flock / msvcrt.locking) on `path`, taken without waiting: held while a revision builds, so
    the start-up sweep of another process (a second Studio, the CLI) never sets aside a build that is still running.
    The system frees it when the process ends, however it ends; release() also removes the file."""

    def __init__(self, path: str):
        self.path, self.fd = path, None
        self.real = False  # True once the OS really holds it (False where the file system can't lock)

    def acquire(self) -> bool:
        """True once held; False while another process (or another BuildLock in this one) holds it. Where the file
        system can't lock at all, it counts as held by nobody: True (and `real` stays False)."""
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            return True
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.real = True
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EACCES, getattr(errno, "EWOULDBLOCK", errno.EAGAIN), errno.EDEADLK):
                os.close(fd)
                return False
        self.fd = fd
        return True

    def release(self, remove: bool = True):
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
        finally:
            os.close(fd)  # POSIX: closing frees the flock
        if remove:
            try:
                os.remove(self.path)
            except OSError:
                pass

    @staticmethod
    def held(path: str) -> bool:
        """True while some process holds the lock on `path` (a build is running there)."""
        if not os.path.exists(path):
            return False
        probe = BuildLock(path)
        if probe.acquire():
            probe.release(remove=False)
            return False
        return True
