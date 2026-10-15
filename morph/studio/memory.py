"""The Studio's memory after builds. A build allocates a few hundred MB (meshes, simulations, manufacturing files) and
frees them, but glibc's malloc keeps freed memory for reuse, in one arena per thread that built: a long-running Studio
grew to 1.1-1.2 GB after a handful of builds. limit_arenas() caps the arenas at two (what MALLOC_ARENA_MAX=2 does,
which run_studio.sh also sets), and trim() hands what a build freed back to the system once it ends. Each room's
builds run on one long-lived thread (rooms.JobQueue). Off glibc (macOS, Windows, musl) both do nothing.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import sys
import threading

M_ARENA_MAX = -8            # glibc's mallopt() parameter for the most arenas malloc may create
_lock = threading.Lock()
_libc = None


def _glibc():
    """glibc's libc (it has malloc_trim and mallopt), or None anywhere else."""
    global _libc
    with _lock:
        if _libc is None:
            _libc = False
            if sys.platform.startswith("linux"):
                try:
                    lib = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6")
                    if all(hasattr(lib, f) for f in ("gnu_get_libc_version", "malloc_trim", "mallopt")):
                        lib.malloc_trim.argtypes, lib.malloc_trim.restype = [ctypes.c_size_t], ctypes.c_int
                        lib.mallopt.argtypes, lib.mallopt.restype = [ctypes.c_int, ctypes.c_int], ctypes.c_int
                        _libc = lib
                except OSError:
                    pass
        return _libc or None


def limit_arenas(n: int = 2) -> bool:
    """Let malloc create at most `n` arenas from now on (for the threads started after this). True when it took."""
    lib = _glibc()
    return bool(lib and lib.mallopt(M_ARENA_MAX, int(n)) == 1)


def trim() -> bool:
    """Return the memory malloc holds free in every arena to the system (malloc_trim(0)). True when some went back."""
    lib = _glibc()
    return bool(lib and lib.malloc_trim(0) == 1)
