"""Ctrl+C during a new project's first build, end to end: the real Studio (`morph studio egg-picker`, offline, full
builds) is stopped with SIGINT while kb1's first build runs and two edits of egg-picker wait behind it. Started again, it
sets the cut-off revision aside, builds kb1 again (labelled as rebuilt after a stop) and names both edits in
egg-picker's chat instead of losing them; nothing is re-run on its own and no traceback is printed (ROB-1).

MORPH_E2E=1 runs it (two to four minutes, plus building the demo robots once: live_studio.demo_home). MORPH_E2E_PORTS
picks the Studio's port."""

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

from live_studio import ROOT, demo_home, env, free_port, release_port, until

pytestmark = [pytest.mark.skipif(os.environ.get("MORPH_E2E") != "1",
                                 reason="end-to-end: MORPH_E2E=1 runs a real Studio (minutes)"),
              pytest.mark.skipif(sys.platform == "win32", reason="sends SIGINT the way Ctrl+C does in a terminal")]


class Run:
    """`morph studio egg-picker` with Ctrl+C working as in a terminal (SIGINT is not ignored)."""

    def __init__(self, home: str, port: int):
        self.home, self.port, self.lines = home, port, []
        self.proc = subprocess.Popen([sys.executable, "-m", "morph.cli", "studio", "egg-picker", "--port", str(port)],
                                     cwd=ROOT, env=env(home), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                     encoding="utf-8", errors="replace",
                                     preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL))
        threading.Thread(target=lambda: [self.lines.append(ln) for ln in self.proc.stdout], daemon=True).start()
        until(lambda: any(ln.startswith("Morph Studio:") for ln in self.lines) or self.proc.poll() is not None, 300,
              "the Studio to start")
        assert self.proc.poll() is None, self.log()[-3000:]

    def log(self) -> str:
        return "".join(self.lines)

    def api(self, method: str, path: str, body=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"X-Morph-Client": "e2e-restart", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def ctrl_c(self) -> float:
        t0 = time.time()
        self.proc.send_signal(signal.SIGINT)
        self.proc.wait(30)
        return time.time() - t0

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(15)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def test_ctrl_c_during_a_first_build_then_a_restart_finishes_it(tmp_path, tmp_path_factory):
    home = str(tmp_path / "home")
    shutil.copytree(demo_home(tmp_path_factory), home)
    port = free_port()
    first = Run(home, port)
    try:
        status, out = first.api("POST", "/api/projects", {"name": "kb1"})
        assert status == 202 and out["project"] == "kb1", out
        for changes in ({"object.mass_g": 70}, {"build.quantity": 50}):     # wait behind kb1's build (one build at a time)
            status, out = first.api("POST", "/api/params", {"changes": changes})
            assert status == 202, out
        until(lambda: os.path.isdir(os.path.join(home, "kb1", "r001", "cad")), 240, "kb1's first build to be under way")
        assert first.ctrl_c() < 10                                          # Ctrl+C stops it at once
    finally:
        first.stop()
    assert not os.path.exists(os.path.join(home, "kb1", "r001", "results.json"))  # cut off

    second = Run(home, port)
    try:
        assert "kb1: r001 was cut off mid-build; setting it aside" in second.log()

        def built():
            status, st = second.api("GET", "/api/state?project=kb1")
            return st if status == 200 and st.get("revs") == ["r001"] and not st.get("queue") else None
        st = until(built, 600, "kb1's first build, queued again at start, to finish", step=2)
        assert st["current"]["label"] == "first design (rebuilt after a stop)"
        said = [h["text"] for h in st["history"] if h["type"] == "assistant"]
        assert "The Studio stopped before kb1's first build finished: building it again now." in said
        status, egg = second.api("GET", "/api/state")
        stopped = [h["text"] for h in egg["history"] if h["text"].startswith("The Studio stopped before this was built")]
        assert len(stopped) == 2 and "Object mass" in stopped[0] and "Quantity" in stopped[1], stopped
        assert egg["revs"] == ["r001"] and not egg["queue"]                    # told about, never re-run on its own
        assert "Traceback" not in second.log(), second.log()[-3000:]
    finally:
        second.stop()
        release_port(port)
