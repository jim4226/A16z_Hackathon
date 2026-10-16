"""The Studio's memory after builds (W3 ROB-11): each room's jobs run on one long-lived thread (its malloc arena is
reused, not a new one per queue drain), what a job freed goes back to the system after it (malloc_trim on glibc),
malloc may create two arenas (mallopt in `morph studio`, MALLOC_ARENA_MAX=2 in run_studio.sh). MORPH_SLOW=1 adds a real
Studio's resident memory over six builds."""

import json
import os
import signal
import subprocess
import sys
import threading
import time
import types
import urllib.request

import pytest

from morph.studio import memory, rooms

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SLOW = os.environ.get("MORPH_SLOW") == "1"
GLIBC = sys.platform.startswith("linux") and memory._glibc() is not None


def queue_of(monkeypatch, idle_s=5.0):
    room = types.SimpleNamespace(name="egg", lock=threading.RLock(), hub=types.SimpleNamespace(send=lambda ev: None))
    q = rooms.JobQueue(room)
    monkeypatch.setattr(rooms.JobQueue, "IDLE_S", idle_s)
    return q


def test_a_rooms_jobs_run_on_one_thread_that_waits_for_the_next_and_each_is_trimmed_after(monkeypatch):
    trims = []
    monkeypatch.setattr(memory, "trim", lambda: trims.append(threading.get_ident()) or True)
    q = queue_of(monkeypatch)
    ran = []
    a = q.submit("params", lambda job: ran.append(threading.get_ident()))
    assert a.wait(10)
    time.sleep(0.2)                                         # the queue is empty: the worker waits instead of ending
    assert q.worker is not None and q.worker.is_alive()
    b = q.submit("params", lambda job: ran.append(threading.get_ident()))
    assert b.wait(10) and a.status == b.status == "done"
    assert len(set(ran)) == 1 and trims == ran              # the same thread, trimmed after each job


def test_the_worker_ends_when_idle_and_the_next_job_starts_another(monkeypatch):
    monkeypatch.setattr(memory, "trim", lambda: True)
    q = queue_of(monkeypatch, idle_s=0.2)
    first = q.submit("params", lambda job: None)
    assert first.wait(10)
    for _ in range(100):
        if q.worker is None:
            break
        time.sleep(0.05)
    assert q.worker is None
    second = q.submit("params", lambda job: None)
    assert second.wait(10) and second.status == "done"


def test_trim_collects_what_a_build_left_in_reference_cycles():
    """A build leaves hundreds of MB in reference cycles that Python's collector reaches only now and then; malloc can't
    give that back until they are collected, so trim() collects first (R3: 530 MB idle after start-up without it, 300
    with it)."""
    import gc
    import weakref

    class Node:
        pass

    was = gc.isenabled()
    gc.disable()                                         # as if the collector's next full pass were far off
    try:
        a, b = Node(), Node()
        a.other, b.other = b, a
        gone = weakref.ref(a)
        del a, b
        assert gone() is not None                        # only a cycle collection frees it
        memory.trim()
        assert gone() is None
    finally:
        if was:
            gc.enable()


@pytest.mark.skipif(not GLIBC, reason="glibc only (malloc_trim, mallopt)")
def test_trim_and_the_arena_cap_reach_glibc():
    assert isinstance(memory.trim(), bool)
    assert memory.limit_arenas(8 * (os.cpu_count() or 1))                # glibc's own cap: this process stays as it was


def test_the_launcher_caps_malloc_arenas_and_the_studio_does_too():
    text = open(os.path.join(REPO, "run_studio.sh"), encoding="utf-8").read()
    i, j = text.index('export MALLOC_ARENA_MAX="${MALLOC_ARENA_MAX:-2}"'), text.index("python -m morph.cli studio")
    assert i < j
    src = open(os.path.join(REPO, "morph", "studio", "server.py"), encoding="utf-8").read()
    serve = src[src.index("def serve("):]
    assert serve.index("memory.limit_arenas()") < serve.index("studio = Studio(")


def test_the_start_up_builds_share_the_arenas_and_are_trimmed_before_the_studio_waits():
    """`morph studio` builds the demo robots (and rebuilds old ones) before serve(), outside any room's job queue: the
    arena cap comes before the first of those builds, and what they freed goes back before serve()."""
    src = open(os.path.join(REPO, "morph", "cli.py"), encoding="utf-8").read()
    studio = src[src.index("def cmd_studio("):]
    studio = studio[:studio.index("\ndef ")]
    first_build = min(studio.index("cmd_robot("), studio.index("proj.run("))
    assert studio.index("memory.limit_arenas()") < first_build
    assert first_build < studio.rindex("memory.trim()") < studio.rindex("serve(p, ")


def _call(port, method, path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "X-Morph-Client": "1"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read() or b"{}")


@pytest.mark.skipif(not (SLOW and GLIBC), reason="MORPH_SLOW=1 on Linux: six real builds in a Studio (10 to 20 minutes)")
def test_a_studios_memory_stays_near_one_builds_needs_over_six_builds(tmp_path):
    """A fresh Studio started as the launcher starts it builds the two demo robots before it serves, then six parameter
    edits alternate between them. Before ROB-11 its resident memory climbed after every build: 444, 706, 805, 971,
    1058, 1242 MB (with MALLOC_ARENA_MAX=2 alone, 484 to 1011 MB); with each job trimmed but neither the start-up builds
    nor the reference cycles, it idled at 783 MB after start-up and went 383 to 698 MB between builds. Now (R3) it idles
    at about 300 MB after start-up and stays at 280 to 340 MB between builds: under 500 MB after start-up, under 600 MB
    after any build, and the last three builds no more than 200 MB above the first three on average."""
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "e2e"))
    from live_studio import free_port                                    # inside MORPH_E2E_PORTS when that is set
    port = free_port()
    env = {**os.environ, "MORPH_HOME": str(tmp_path / "home"), "MORPH_OFFLINE": "1", "MALLOC_ARENA_MAX": "2"}
    proc = subprocess.Popen([sys.executable, "-m", "morph.cli", "studio", "egg-picker", "--port", str(port)], cwd=REPO,
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def rss():
        with open(f"/proc/{proc.pid}/status") as fh:
            return int(next(ln.split()[1] for ln in fh if ln.startswith("VmRSS:"))) // 1024

    def idle(room):
        end = time.time() + 1800
        while time.time() < end:
            try:
                if not _call(port, "GET", f"/api/jobs?project={room}")["jobs"]:
                    return
            except OSError:                                              # a busy machine: ask again
                pass
            time.sleep(1)
        raise AssertionError(f"{room} still building")
    try:
        for _ in range(900):
            try:
                _call(port, "GET", "/api/state?project=egg-picker")
                break
            except OSError:
                time.sleep(1)
        for room in ("egg-picker", "so101"):
            idle(room)
        time.sleep(2)
        started = rss()
        seen = []
        for i in range(6):
            room = ("egg-picker", "so101")[i % 2]
            _call(port, "POST", f"/api/params?project={room}", {"changes": {"build.quantity": 10 + i}})
            time.sleep(1)
            idle(room)
            time.sleep(2)
            seen.append(rss())
        assert started < 500 and max(seen) < 600 and sum(seen[3:]) / 3 - sum(seen[:3]) / 3 < 200, (started, seen)
    finally:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(30)
        except subprocess.TimeoutExpired:
            proc.kill()
