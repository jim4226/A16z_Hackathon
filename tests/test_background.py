"""Network lookups never keep a stopping process alive (services/background.DaemonPool): Ctrl+C stops the Studio at
once even while Darwin lookups hang, where concurrent.futures made the interpreter wait for every queued lookup."""

import os
import socket
import subprocess
import sys
import threading
import time

import pytest

from morph.services.background import DaemonPool

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_a_daemon_pool_works_like_an_executor():
    with DaemonPool(3) as pool:
        assert pool.map(lambda x: x * 2, range(10)) == [x * 2 for x in range(10)]
        futures = [pool.submit(time.sleep, 0.05) for _ in range(5)]
        failing = pool.submit(lambda: 1 / 0)
    assert all(f.done() for f in futures)                   # the with block waits for what was submitted
    with pytest.raises(ZeroDivisionError):
        failing.result()
    with pytest.raises(RuntimeError):
        pool.submit(print)
    assert all(t.daemon for t in pool._threads) and len(pool._threads) == 3


@pytest.fixture
def silent_server():
    """A server that accepts connections and never answers (an SSL read that never returns, as seen live)."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(64)
    held = []

    def accept():
        while True:
            try:
                held.append(srv.accept()[0])
            except OSError:
                return
    threading.Thread(target=accept, daemon=True).start()
    yield srv.getsockname()[1], held
    srv.close()
    for c in held:
        c.close()


def test_hung_darwin_lookups_never_keep_a_stopping_process_alive(tmp_path, silent_server):
    """What the Studio does on Find agents (darwin.source on a daemon thread), with Darwin not answering: once the main
    thread is done (Ctrl+C ended serve()), the process ends at once. With ThreadPoolExecutor it waited for every queued
    lookup to time out in turn (16 lookups, 4 at a time, 30 s each: two minutes)."""
    port, held = silent_server
    (tmp_path / "rev").mkdir()
    code = f"""
import sys, threading, time
import morph.darwin as d
d.API = "http://127.0.0.1:{port}/api/v3/search"
class Project:
    def load_results(self, rev): return {{"bom": [{{"item": f"part {{i}}", "cost": 1}} for i in range(12)]}}
    def rev_dir(self, rev): return {str(tmp_path / "rev")!r}
    def log(self, entry): pass
threading.Thread(target=d.source, args=(Project(), "r001"), kwargs={{"workers": 4, "timeout": 30, "retries": 0}},
                 daemon=True).start()
print("looking up", flush=True)
sys.stdin.readline()  # the main thread is done when the test says so (as serve() returns on Ctrl+C)
"""
    env = {**os.environ, "PYTHONPATH": ROOT, "MORPH_HOME": str(tmp_path / "home")}
    proc = subprocess.Popen([sys.executable, "-c", code], cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            text=True)
    assert proc.stdout.readline().strip() == "looking up"
    deadline = time.time() + 20
    while len(held) < 4 and time.time() < deadline:                # four lookups hang on the silent server
        time.sleep(0.05)
    assert len(held) >= 4
    t0 = time.time()
    proc.stdin.write("stop\n")
    proc.stdin.flush()
    try:
        proc.wait(15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(5)
        pytest.fail("the process waited for the hung lookups")
    assert proc.returncode == 0 and time.time() - t0 < 15
