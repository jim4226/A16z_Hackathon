"""The CLI fails loudly when a robot build fails, never crashes on a console that can't show a character, says
which Morph it is, and talks the same way on every OS. Offline."""

import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import morph
from morph import cli, keys
from morph.robot import pipeline2
from morph.robot.pipeline2 import RobotProject
from morph.spec import Design

CODE = str(Path(morph.__file__).resolve().parents[1])  # the checkout under test, for subprocesses


def _robot_args(**kw):
    return argparse.Namespace(**{"action": "new", "name": "egg-picker", "source": None, "need": None, "trials": 1,
                                 "fast": True, "base": None, "changes": [], **kw})


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "HOME", str(tmp_path / "projects"))
    monkeypatch.setattr(keys, "ENV_PATH", str(tmp_path / ".env"))  # main() must not load the developer's keys
    return tmp_path / "projects"


def test_a_build_with_failed_stages_exits_non_zero(home, monkeypatch, capsys):
    def compute(self, node, design, results, *args):
        if node == "electronics":
            raise RuntimeError("no controller fits")
        results[pipeline2.OUTPUT_KEYS[node]] = {"ok": True}

    monkeypatch.setattr(RobotProject, "_compute", compute)
    assert cli.main(["robot", "new", "egg-picker", "--fast", "--trials", "1"]) == 1
    out = capsys.readouterr().out
    assert "stages did not complete: electronics" in out and "x electronics stage error" in out


def test_a_clean_build_exits_zero(home, capsys):
    assert cli.main(["robot", "new", "egg-picker", "--fast", "--trials", "1"]) == 0
    res = RobotProject(str(home / "egg-picker")).load_results("r001")
    assert set(res["stage_status"].values()) == {"computed"}
    assert cli.main(["robot", "show", "egg-picker"]) == 0


def test_robot_names_and_sources_are_checked_before_anything_is_created(home, tmp_path):
    for kw in ({"name": "../escape"}, {"name": "Bad_Name"}, {"name": str(tmp_path / "abs")}, {"name": "ok", "source": "../x"},
               {"name": "ok", "source": "nope"}, {"action": "change", "name": "missing"}, {"action": "show", "name": "missing"}):
        with pytest.raises(SystemExit):
            cli.cmd_robot(_robot_args(**kw))
    assert not (tmp_path / "escape").exists() and not (tmp_path / "abs").exists()
    assert not home.exists() or sorted(os.listdir(home)) == []
    with pytest.raises(SystemExit, match="lowercase"):
        cli.cmd_studio(argparse.Namespace(name="../x", host="127.0.0.1", port=1, open=False, remote=False))


def _fake_revision(root, issues):
    (root / "r001").mkdir(parents=True)
    (root / "meta.json").write_text(json.dumps({"kind": "robot"}), encoding="utf-8")
    (root / "r001" / "design.json").write_text(Design().dumps(), encoding="utf-8")
    (root / "r001" / "results.json").write_text(json.dumps({"summary": {"dof": 5, "issues": issues}}), encoding="utf-8")


def test_a_cp1252_console_never_crashes_the_cli(tmp_path):
    _fake_revision(tmp_path / "projects" / "egg-picker", ["j2: needs 2.1 N·m → 3× sts3215 ≈ too weak ✗", "Ø44 mm 🥚 egg"])
    env = {**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONPATH": CODE, "MORPH_HOME": str(tmp_path / "projects")}
    env.pop("PYTHONUTF8", None)
    out = subprocess.run([sys.executable, "-m", "morph.cli", "robot", "show", "egg-picker"], capture_output=True,
                         env=env, cwd=str(tmp_path), timeout=120)
    assert out.returncode == 0, out.stderr.decode("cp1252", "replace")[-1500:]
    text = out.stdout.decode("cp1252")
    assert "  x j2: needs 2.1 N·m ? 3× sts3215 ? too weak ?" in text and "UnicodeEncodeError" not in out.stderr.decode("cp1252", "replace")


def test_the_exit_status_reaches_the_shell(tmp_path):
    env = {**os.environ, "PYTHONPATH": CODE, "MORPH_HOME": str(tmp_path / "projects")}
    out = subprocess.run([sys.executable, "-m", "morph.cli", "robot", "show", "missing"], capture_output=True, text=True,
                         env=env, cwd=str(tmp_path), timeout=120)
    assert out.returncode != 0 and "no project missing" in out.stderr


def test_doctor_says_which_morph_it_is(home, monkeypatch, capsys):
    monkeypatch.setattr(keys, "TEMPLATE", str(home.parent / "no-template"))
    cli.cmd_doctor(argparse.Namespace(offline=True))
    out = capsys.readouterr().out
    assert re.search(rf"Morph {re.escape(morph.__version__)}(, git [0-9a-f]{{12}}( with local changes)?| \(not a git checkout\))", out)
    sha = cli._git_sha()
    if shutil.which("git") and os.path.exists(os.path.join(CODE, ".git")):
        assert re.fullmatch(r"[0-9a-f]{12}( with local changes)?", sha or "")
    else:
        assert sha is None


def test_messages_are_the_same_on_every_os(home, monkeypatch):
    with socket.socket() as sock:  # a Studio already running on this port
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        with pytest.raises(SystemExit) as exc:
            cli.cmd_studio(argparse.Namespace(name="egg-picker", host="127.0.0.1", port=port, open=False, remote=False))
    assert "Ctrl+C" in str(exc.value) and "CMD" not in str(exc.value)
    static = Path(CODE) / "morph" / "studio" / "static"
    for name in ("present.js", "app.js", "keys.js"):
        text = (static / name).read_text(encoding="utf-8")
        assert "CMD window" not in text and "press Ctrl and -" not in text and "<b>Ctrl</b> and" not in text
    assert "CMD window" not in (Path(CODE) / "morph" / "studio" / "server.py").read_text(encoding="utf-8")


def test_doctor_names_stages_that_were_not_built(home, monkeypatch, capsys):
    monkeypatch.setattr(keys, "TEMPLATE", str(home.parent / "no-template"))
    _fake_revision(home / "egg-picker", [])
    path = home / "egg-picker" / "r001" / "results.json"
    path.write_text(json.dumps({"summary": {}, "provenance": {"electronics": "error", "cad": "blocked", "sim": "computed"},
                                "errors": {"electronics": "no controller fits", "cad": "not built: needs electronics"}}),
                    encoding="utf-8")
    cli.cmd_doctor(argparse.Namespace(offline=True))
    out = capsys.readouterr().out
    assert "egg-picker r001: electronics error (no controller fits); cad blocked (not built: needs electronics)" in out


# ------------------------------------------------------------------ what the CLI says, and when


def _studio_home(home):
    """egg-picker and so101 with a revision this code built, so `morph studio` starts without building anything."""
    for name in ("egg-picker", "so101"):
        root = home / name
        (root / "r001").mkdir(parents=True)
        (root / "meta.json").write_text(json.dumps({"kind": "robot", "need": name}), encoding="utf-8")
        (root / "r001" / "design.json").write_text(Design().dumps(), encoding="utf-8")
        (root / "r001" / "results.json").write_text(json.dumps({"parent": None, "summary": {},
                                                                "pipeline_version": pipeline2.pipeline_version()}),
                                                    encoding="utf-8")


def test_the_access_link_reaches_a_log_file_while_the_studio_runs(tmp_path):
    """`morph studio --remote > studio.log` (nohup, a service, the launcher on a server): the access link is in the log
    while the Studio runs, not when an output buffer happens to fill, nor never (a Studio stopped with SIGTERM)."""
    _studio_home(tmp_path / "projects")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {**os.environ, "PYTHONPATH": CODE, "MORPH_HOME": str(tmp_path / "projects"), "MORPH_OFFLINE": "1"}
    for k in ("PYTHONUNBUFFERED", "MORPH_TOKEN", "DISPLAY", "WAYLAND_DISPLAY"):
        env.pop(k, None)
    run = ("import sys; from morph import keys; keys.ENV_PATH = sys.argv[1]; from morph import cli; "
           "sys.exit(cli.main(sys.argv[2:]))")                     # .env in the test's folder, never the checkout's
    log = tmp_path / "studio.log"
    with open(log, "wb") as out:
        proc = subprocess.Popen([sys.executable, "-c", run, str(tmp_path / ".env"), "studio", "egg-picker", "--remote",
                                 "--port", str(port)], stdout=out, stderr=subprocess.STDOUT, env=env, cwd=str(tmp_path))
    try:
        deadline = time.time() + 120
        while time.time() < deadline and "?token=" not in log.read_text(encoding="utf-8", errors="replace"):
            assert proc.poll() is None, log.read_text(encoding="utf-8", errors="replace")[-2000:]
            time.sleep(0.2)
        text = log.read_text(encoding="utf-8", errors="replace")
        assert "?token=" in text and "Access code: " in text, text[-2000:]
        assert proc.poll() is None                                  # and the Studio is still running
    finally:
        proc.terminate()
        try:
            proc.wait(20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(20)


def test_relaunching_on_a_server_without_a_screen_opens_no_browser(home, monkeypatch, capsys):
    """The launchers pass --open; on a headless Linux server a second launch while a Studio runs must not start a text
    browser in the terminal (Python's webbrowser runs w3m or lynx there and waits for it)."""
    opened = []
    monkeypatch.setattr(cli, "open_app_window", lambda url: opened.append(url))
    monkeypatch.setattr(cli.sys, "platform", "linux")
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        a = argparse.Namespace(name="egg-picker", host="127.0.0.1", port=busy.getsockname()[1], open=True, remote=False,
                               team=False, allow_url_import=False)
        monkeypatch.delenv("DISPLAY", raising=False)
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
        with pytest.raises(SystemExit, match="already running"):
            cli.cmd_studio(a)
        assert opened == []
        monkeypatch.setenv("DISPLAY", ":0")                         # a desktop: the running Studio's window comes up
        with pytest.raises(SystemExit, match="already running"):
            cli.cmd_studio(a)
        assert opened == [f"http://127.0.0.1:{a.port}"]


def test_morph_ask_says_why_there_are_no_rows(home, monkeypatch, capsys):
    from morph import keewano, settings
    calls = []
    monkeypatch.setattr(keewano, "query", lambda sql: calls.append(sql) or None)
    assert cli.main(["ask", "SELECT 1"]) == 1
    assert "Keewano not configured" in capsys.readouterr().err and calls == []
    monkeypatch.setenv("KEEWANO_PAT", "kwpat-test-not-real")
    monkeypatch.setenv("KEEWANO_PROJECT_ID", "00000000-0000-0000-0000-000000000000")
    monkeypatch.setenv("MORPH_OFFLINE", "1")
    assert cli.main(["ask", "SELECT 1"]) == 1
    err = capsys.readouterr().err
    assert "network lookups are off (MORPH_OFFLINE=1)" in err and "not configured" not in err and calls == []
    monkeypatch.delenv("MORPH_OFFLINE")
    settings.save({"network_enabled": False})
    assert cli.main(["ask", "SELECT 1"]) == 1
    assert "network lookups are off (Keys, Privacy and network)" in capsys.readouterr().err and calls == []
    settings.save({"network_enabled": True})
    assert cli.main(["ask", "SELECT 1"]) == 1                       # configured and allowed, but no answer
    err = capsys.readouterr().err
    assert "didn't answer" in err and "not configured" not in err and calls == ["SELECT 1"]
    monkeypatch.setattr(keewano, "query", lambda sql: [{"n": 1}])
    assert cli.main(["ask", "SELECT 1"]) == 0 and json.loads(capsys.readouterr().out) == [{"n": 1}]


def test_morph_study_refuses_before_it_announces_a_study(home, monkeypatch, capsys):
    (home / "offl").mkdir(parents=True)                             # a gripper-only project of the earlier prototype
    (home / "offl" / "meta.json").write_text(json.dumps({"need": "pick up an egg"}), encoding="utf-8")
    assert cli.main(["study", "offl", "--runs", "2", "--workers", "1"]) == 1
    out = capsys.readouterr()
    assert out.err.startswith("offl: ") and "randomized run" not in out.out


def test_a_stopped_studio_process_leaves_without_interpreter_shutdown(tmp_path, monkeypatch):
    """`morph studio` (the command line, argv None) ends its process with _leave once serve() returns: interpreter
    shutdown ran native finalizers that crashed now and then after an in-process build (exit code -11). Called with
    argv (a script, a test), main() returns as usual."""
    from morph.studio import server
    _studio_home(tmp_path / "projects")
    monkeypatch.setattr(cli, "HOME", str(tmp_path / "projects"))
    monkeypatch.setattr(server, "serve", lambda *a, **k: None)
    left = []
    monkeypatch.setattr(cli, "_leave", left.append)
    monkeypatch.setattr(sys, "argv", ["morph", "studio", "egg-picker", "--port", "1"])
    cli.main()
    assert left == [0]
    cli.main(["studio", "egg-picker", "--port", "1"])
    assert left == [0]


_LEAVING = """
import os, sys, time
from morph import keys
keys.ENV_PATH = sys.argv[0] + ".env"                    # .env in the test's folder, never the checkout's
sys.argv = ["morph", "studio", "egg-picker", "--port", sys.argv[1]]
from morph import cli
from morph.studio import rooms
release = rooms.release_claims
def slow():                                             # the few bytecodes after serve() returned, widened
    print("<leaving>", flush=True)
    time.sleep(3)
    release()
rooms.release_claims = slow
sys.exit(cli.main())
"""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals sent from outside the process")
def test_a_stop_signal_after_the_studio_stopped_cleanly_changes_nothing(tmp_path):
    """Ctrl+C pressed again once serve() had returned (in cli._leave, before os._exit): the handlers serve() set were
    put back, so it printed a KeyboardInterrupt traceback, the process ended with -2 and .studios/<id>.json stayed. The
    stop is written down by then: from the end of a clean serve() to os._exit, stop signals are ignored."""
    _studio_home(tmp_path / "projects")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    script = tmp_path / "leaving.py"
    script.write_text(_LEAVING, encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": CODE, "MORPH_HOME": str(tmp_path / "projects"), "MORPH_OFFLINE": "1",
           "PYTHONUNBUFFERED": "1"}
    for k in ("MORPH_TOKEN", "DISPLAY", "WAYLAND_DISPLAY"):
        env.pop(k, None)
    proc = subprocess.Popen([sys.executable, str(script), str(port)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env=env, cwd=str(tmp_path))
    out = []
    reader = threading.Thread(target=lambda: out.extend(iter(proc.stdout.readline, "")), daemon=True)
    reader.start()

    def wait_for(text, seconds=120):
        deadline = time.time() + seconds
        while time.time() < deadline and not any(text in line for line in out):
            assert proc.poll() is None, (out, proc.stderr.read())
            time.sleep(0.05)
        assert any(text in line for line in out), out

    try:
        wait_for("Ctrl+C to stop")
        proc.send_signal(signal.SIGINT)                         # the clean stop
        wait_for("<leaving>")
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):  # and again, while it leaves
            proc.send_signal(sig)
            time.sleep(0.1)
        rc = proc.wait(60)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(20)
    err = proc.stderr.read()
    assert rc == 0 and "Traceback" not in err, (rc, err)
    assert os.listdir(tmp_path / "projects" / ".studios") == []    # its lock file and its start record both went


def test_leaving_flushes_the_terminal_and_ends_study_workers(tmp_path):
    code = ("import multiprocessing as mp, sys, time\n"
            "from morph import cli\n"
            "from morph.studio import rooms\n"
            "if __name__ == '__main__':\n"
            f"    claim = rooms.claim({str(tmp_path / 'projects')!r})\n"
            "    child = mp.get_context('spawn').Process(target=time.sleep, args=(600,), daemon=True)\n"
            "    child.start()\n"
            "    print(child.pid, end=' ')\n"
            "    sys.stdout.write('saved')\n"
            "    cli._leave(0)\n")
    script = tmp_path / "leave.py"
    script.write_text(code, encoding="utf-8")
    out = subprocess.run([sys.executable, str(script)], cwd=CODE, env={**os.environ, "PYTHONPATH": CODE},
                         capture_output=True, text=True, timeout=60)
    pid, said = out.stdout.split()
    assert out.returncode == 0 and said == "saved"
    assert os.listdir(tmp_path / "projects" / ".studios") == []   # its lock file went with it, not 10 min later
    from morph.services.durable import process_alive
    for _ in range(100):                                  # the worker was ended, not left behind
        if not process_alive(int(pid)):
            break
        time.sleep(0.05)
    else:
        pytest.fail("the study worker outlived the Studio")


def test_studio_on_a_project_with_no_finished_revision_never_replaces_its_design(home, monkeypatch, capsys):
    """`morph studio boom-kb` on a project whose first build failed (its design kept in first-build.json): it said "no
    project boom-kb yet", built the default template into it as r001 and rewrote its meta.json; the Studio then dropped
    the kept design. It now says how to go on and changes nothing."""
    from morph.spec import Design
    from morph.studio import projects
    info = projects.create(str(home), "boom-kb", design=Design().with_changes({"arm.dof": 6}), need="Pick strawberries")
    root = os.path.join(str(home), "boom-kb")
    projects.park_first_build(root, {"design": info["design"].to_dict(), "label": "first design", "cut": 0,
                                     "error": "PermissionError"})
    before = {n: open(os.path.join(root, n), encoding="utf-8").read() for n in ("meta.json", projects.FIRST_BUILD)}
    built = []
    monkeypatch.setattr(cli, "cmd_robot", lambda a: built.append(a.name))
    monkeypatch.setattr(cli, "HOME", str(home))
    a = argparse.Namespace(name="boom-kb", host="127.0.0.1", port=1, open=False, remote=False, team=False,
                           allow_url_import=False)
    with pytest.raises(SystemExit) as exc:
        cli.cmd_studio(a)
    assert "boom-kb has no finished revision yet" in str(exc.value) and "build it again" in str(exc.value)
    assert "no project boom-kb yet" not in capsys.readouterr().out and built == []
    assert {n: open(os.path.join(root, n), encoding="utf-8").read() for n in before} == before
    assert not [n for n in os.listdir(root) if n.startswith("r0")]
