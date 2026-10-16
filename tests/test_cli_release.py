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
        busy.listen(8)  # never accepted: Windows refuses a connection once the backlog is full, as if nothing listened
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


def _signals_as_in_a_terminal():
    """In the child: Ctrl+C, SIGTERM and SIGHUP as a terminal gives them, whatever this test process ignores (an
    ignored signal is inherited, and an earlier test in the same pytest worker can leave SIGINT ignored)."""
    for name in ("SIGINT", "SIGTERM", "SIGHUP"):
        signal.signal(getattr(signal, name), signal.SIG_DFL)


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
                            text=True, env=env, cwd=str(tmp_path), preexec_fn=_signals_as_in_a_terminal)
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


_FIRST_ROBOT = """
import os, sys, time
from morph import keys
keys.ENV_PATH = sys.argv[0] + ".env"                    # .env in the test's folder, never the checkout's
sys.argv = ["morph", "studio", sys.argv[1], "--port", sys.argv[2]]
from morph.robot.pipeline2 import RobotProject

def build(self, rev, design, *a, **k):                  # the launcher's first robot, about a minute
    os.makedirs(self.rev_dir(rev), exist_ok=True)
    with open(os.path.join(self.rev_dir(rev), "design.json"), "w") as fh:
        fh.write(design.dumps())
    print("<building>", flush=True)
    time.sleep(120)
RobotProject._build = build
from morph import cli
sys.exit(cli.main())
"""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals sent from outside the process")
@pytest.mark.parametrize("name,signals", [("egg-picker", ["SIGINT"]), ("egg-picker", ["SIGINT", "SIGINT"]),
                                          ("my-arm", ["SIGINT"]), ("my-arm", ["SIGHUP"]), ("my-arm", ["SIGTERM"])])
def test_a_stop_while_the_launcher_builds_the_first_robot_says_so_in_one_line(tmp_path, name, signals):
    """Ctrl+C while `morph studio` built the first robot ("generating the first robot (about a minute)...", before
    serve()) printed MuJoCo's KeyboardInterrupt traceback and ended with -2; for a project of another name than the
    demos it left a meta.json with no revision, which the next `morph studio <name>` refused to start on and "build it
    again" couldn't build (no design kept). It now stops in one line, exits 130, and keeps nothing of a project this
    start made, so the next start generates it again."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    script = tmp_path / "first.py"
    script.write_text(_FIRST_ROBOT, encoding="utf-8")
    home = tmp_path / "projects"
    env = {**os.environ, "PYTHONPATH": CODE, "MORPH_HOME": str(home), "MORPH_OFFLINE": "1", "PYTHONUNBUFFERED": "1"}
    for k in ("MORPH_TOKEN", "DISPLAY", "WAYLAND_DISPLAY"):
        env.pop(k, None)
    proc = subprocess.Popen([sys.executable, str(script), name, str(port)], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=env, cwd=str(tmp_path),
                            preexec_fn=_signals_as_in_a_terminal)
    out = []
    threading.Thread(target=lambda: out.extend(iter(proc.stdout.readline, "")), daemon=True).start()
    try:
        deadline = time.time() + 120
        while time.time() < deadline and "<building>\n" not in out:
            assert proc.poll() is None, (out, proc.stderr.read())
            time.sleep(0.05)
        assert "<building>\n" in out, out
        for sig in signals:
            proc.send_signal(getattr(signal, sig))
            time.sleep(0.2)
        rc = proc.wait(60)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(20)
    time.sleep(0.2)
    err = proc.stderr.read()
    said = out[out.index("<building>\n") + 1:]
    assert rc == 130 and "Traceback" not in err, (rc, err)
    assert len(said) == 1 and said[0].startswith("Stopped before the Studio started"), said
    assert not (home / name).exists()                   # made by this start, never finished: nothing of it kept
    assert not (home / ".studios").exists() or os.listdir(home / ".studios") == []



def _start_studio_script(tmp_path, script_text, name, preexec=None):
    """`morph studio <name>` in a child process running `script_text` (a patched launcher); returns (proc, out lines)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    script = tmp_path / "first.py"
    script.write_text(script_text, encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": CODE, "MORPH_HOME": str(tmp_path / "projects"), "MORPH_OFFLINE": "1",
           "PYTHONUNBUFFERED": "1"}
    for k in ("MORPH_TOKEN", "DISPLAY", "WAYLAND_DISPLAY"):
        env.pop(k, None)
    proc = subprocess.Popen([sys.executable, str(script), name, str(port)], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=env, cwd=str(tmp_path),
                            preexec_fn=_signals_as_in_a_terminal)
    out = []
    threading.Thread(target=lambda: out.extend(iter(proc.stdout.readline, "")), daemon=True).start()
    return proc, out


def _stop_at(proc, out, mark, signals, seconds=30):
    """Send `signals` once `mark` is printed; the exit status, or None when the child was still running after
    `seconds` (it is killed then)."""
    try:
        deadline = time.time() + 120
        while time.time() < deadline and mark not in out:
            assert proc.poll() is None, (out, proc.stderr.read())
            time.sleep(0.02)
        assert mark in out, out
        for sig in signals:
            proc.send_signal(getattr(signal, sig))
            time.sleep(0.2)
        return proc.wait(seconds)
    except subprocess.TimeoutExpired:
        return None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(20)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals sent from outside the process")
def test_a_stop_while_the_launcher_rebuilds_for_newer_code_says_the_finished_revision_is_kept(tmp_path):
    """Ctrl+C while `morph studio` rebuilt a project for newer Morph code ("rebuilding with the latest Morph code")
    said "<project> wasn't finished, and the next start builds it again", but the project has a finished revision
    and keeps it; only the rebuild was cut off."""
    home = tmp_path / "projects"
    _studio_home(home)
    results = home / "egg-picker" / "r001" / "results.json"
    results.write_text(json.dumps({**json.loads(results.read_text(encoding="utf-8")), "pipeline_version": "older"}),
                       encoding="utf-8")
    proc, out = _start_studio_script(tmp_path, _FIRST_ROBOT, "egg-picker")
    rc = _stop_at(proc, out, "<building>\n", ["SIGINT"])
    time.sleep(0.2)
    err = proc.stderr.read()
    assert rc == 130 and "Traceback" not in err, (rc, out, err)
    assert out[out.index("<building>\n") + 1:] == [
        "Stopped before the Studio started: egg-picker's rebuild with the latest Morph code was cut off; its finished "
        "revision r001 is kept, and the next start rebuilds it.\n"], out
    assert sorted(n for n in os.listdir(home / "egg-picker") if n.startswith("r0")) == ["r001"]
    assert json.loads(results.read_text(encoding="utf-8"))["pipeline_version"] == "older"


_SLOW_TRIMESH = """
import os, sys, time
from morph import keys
keys.ENV_PATH = sys.argv[0] + ".env"                    # .env in the test's folder, never the checkout's
sys.argv = ["morph", "studio", sys.argv[1], "--port", sys.argv[2]]


class SlowTrimesh:
    # trimesh's first import, slowed down: like trimesh, it catches BaseException around a part of it (trimesh does
    # around its optional imports), which is where a Ctrl+C pressed then landed
    def find_spec(self, name, path=None, target=None):
        if name == "trimesh":
            sys.meta_path.remove(self)
            print("<importing>", flush=True)
            try:
                for _ in range(60):
                    time.sleep(0.05)
            except BaseException:
                print("<swallowed>", flush=True)
        return None


sys.meta_path.insert(0, SlowTrimesh())
from morph.robot.pipeline2 import RobotProject

def build(self, rev, design, *a, **k):                  # the build, after what it imports first
    from morph.robot import arm  # noqa: F401
    os.makedirs(self.rev_dir(rev), exist_ok=True)
    with open(os.path.join(self.rev_dir(rev), "design.json"), "w") as fh:
        fh.write(design.dumps())
    print("<building>", flush=True)
    time.sleep(120)
RobotProject._build = build
from morph import cli
sys.exit(cli.main())
"""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals sent from outside the process")
@pytest.mark.parametrize("name,signals", [("my-arm", ["SIGINT"]), ("my-arm", ["SIGTERM"]), ("my-arm", ["SIGHUP"]),
                                          ("my-arm", ["SIGINT", "SIGTERM"]), ("rebuild", ["SIGINT"])])
def test_a_stop_while_the_launcher_first_imports_the_build_stops_in_one_line(tmp_path, name, signals):
    """Ctrl+C in the first half second of "generating the first robot" landed while trimesh was first imported, and
    trimesh catches BaseException around its optional imports: the press was swallowed (the build went on and the
    Studio started), or left trimesh half-made, which ended in a TypeError or a segmentation fault and a project the
    next start refused. The launcher now loads what the build imports before the line, with stop signals held until
    it is loaded, then stops in one line. The same before a rebuild for newer Morph code."""
    home = tmp_path / "projects"
    if name == "rebuild":
        name = "egg-picker"
        _studio_home(home)
        results = home / name / "r001" / "results.json"
        results.write_text(json.dumps({**json.loads(results.read_text(encoding="utf-8")), "pipeline_version": "older"}),
                           encoding="utf-8")
        line = (f"Stopped before the Studio started: {name}'s rebuild with the latest Morph code was cut off; its "
                "finished revision r001 is kept, and the next start rebuilds it.\n")
    else:
        line = f"Stopped before the Studio started: {name} wasn't finished, and the next start builds it again.\n"
    proc, out = _start_studio_script(tmp_path, _SLOW_TRIMESH, name)
    rc = _stop_at(proc, out, "<importing>\n", signals)
    time.sleep(0.2)
    err = proc.stderr.read()
    assert rc == 130 and "Traceback" not in err, (rc, out, err)
    assert out[out.index("<importing>\n") + 1:] == [line], out
    if line.endswith("builds it again.\n"):
        assert not (home / name).exists()                   # made by this start, never finished: nothing of it kept
    else:
        assert sorted(n for n in os.listdir(home / name) if n.startswith("r0")) == ["r001"]
    assert not (home / ".studios").exists() or os.listdir(home / ".studios") == []



@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals sent from outside the process")
@pytest.mark.parametrize("argv", ['["morph", "robot", "new", sys.argv[1]]',
                                  '["morph", "robot", "new", "a robot arm that picks eggs from a tray", "--defaults"]'],
                         ids=["name", "one-liner"])
def test_ctrl_c_while_morph_robot_new_first_imports_the_build_is_not_swallowed(tmp_path, argv):
    """`morph robot new my-arm` (or a one-liner, whose brief checks import the arm generator before anything is built)
    in a terminal: Ctrl+C while trimesh was first imported was swallowed the same way (the build went on) or broke
    trimesh. It now stops before the project is made."""
    script = _SLOW_TRIMESH.replace('sys.argv = ["morph", "studio", sys.argv[1], "--port", sys.argv[2]]',
                                   f"sys.argv = {argv}")
    proc, out = _start_studio_script(tmp_path, script, "my-arm")
    rc = _stop_at(proc, out, "<importing>\n", ["SIGINT"])
    err = proc.stderr.read()
    assert rc == -signal.SIGINT and err.rstrip().endswith("KeyboardInterrupt"), (rc, out, err)
    assert "<swallowed>\n" not in out and "<building>\n" not in out, out
    assert not (tmp_path / "projects").exists() or os.listdir(tmp_path / "projects") == []


_CHECK_ONLY = """
import sys
from morph import keys
keys.ENV_PATH = sys.argv[0] + ".env"
from morph.studio import server
server.serve = lambda *a, **k: print("<serve>", sorted(m for m in ("trimesh", "morph.robot.imported", "morph.robot.arm")
                                                       if m in sys.modules), flush=True)
from morph import cli
sys.exit(cli.main(["studio", "egg-picker", "--port", sys.argv[1]]))
"""


def test_the_launcher_checks_an_imported_robot_without_loading_the_build(tmp_path):
    """Every `morph studio` start checks whether each robot was built by older Morph code. For a robot from a bundle
    (so101) that check imported morph.robot.imported, and with it trimesh, in the main thread, where trimesh's own
    BaseException catches swallow a Ctrl+C or break on it (the Studio then started with trimesh half-made). The
    bundle is now found without loading the build."""
    from morph.studio import projects
    home = tmp_path / "projects"
    _studio_home(home)
    design = projects.template("so101", "so101")
    (home / "so101" / "r001" / "design.json").write_text(design.dumps(), encoding="utf-8")
    (home / "so101" / "r001" / "results.json").write_text(json.dumps(
        {"parent": None, "summary": {}, "pipeline_version": pipeline2.pipeline_version("so101")}), encoding="utf-8")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    script = tmp_path / "check.py"
    script.write_text(_CHECK_ONLY, encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": CODE, "MORPH_HOME": str(home), "MORPH_OFFLINE": "1"}
    for k in ("MORPH_TOKEN", "DISPLAY", "WAYLAND_DISPLAY"):
        env.pop(k, None)
    out = subprocess.run([sys.executable, str(script), str(port)], env=env, cwd=str(tmp_path), capture_output=True,
                         text=True, timeout=120)
    assert "<serve> []" in out.stdout.splitlines(), (out.stdout, out.stderr)
    assert "rebuilding" not in out.stdout, out.stdout

_HELD = """
import os, signal, sys, time
import numpy  # noqa: F401  (its BLAS threads: a signal the main thread blocks goes to one of them)
from morph import cli

def kill(*sigs):
    for sig in sigs:
        os.kill(os.getpid(), sig)
    time.sleep(0.2)                                      # whichever thread takes it, Python's handler has run

def case(name, body, limit=10.0):
    try:
        with cli._stops_held(limit):
            try:
                body()
                inside = "nothing"
            except KeyboardInterrupt:
                inside = "KeyboardInterrupt"
        after = "nothing"
    except KeyboardInterrupt:
        after = "KeyboardInterrupt"
    print(name, inside, after, signal.getsignal(signal.SIGINT) is signal.default_int_handler,
          signal.getsignal(signal.SIGTERM) == signal.SIG_DFL, flush=True)

# native threads are counted where /proc lists them (Linux); macOS has no /proc, and its numpy starts them too
print("threads", len(os.listdir("/proc/self/task")) > 1 if os.path.isdir("/proc/self/task") else True, flush=True)
case("int", lambda: kill(signal.SIGINT))
case("int-int", lambda: kill(signal.SIGINT, signal.SIGINT))
case("none", lambda: time.sleep(0.2))
case("limit", lambda: kill(signal.SIGINT), limit=0.0)
signal.signal(signal.SIGINT, signal.SIG_IGN)
case("ignored", lambda: kill(signal.SIGINT))
signal.signal(signal.SIGINT, signal.default_int_handler)
case("term", lambda: kill(signal.SIGTERM))           # the default action: ends the process, after the context
print("still here", flush=True)
"""


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals sent from inside the process")
@pytest.mark.parametrize("blas_threads", ["4", "1"])
def test_stops_held_note_a_stop_and_hand_it_on_when_the_context_ends(tmp_path, blas_threads):
    """cli._stops_held: Ctrl+C inside the context changes nothing there and raises KeyboardInterrupt once it ends
    (twice pressed: once); the handlers are put back; an ignored signal stays ignored; past the limit a stop goes
    through at once; a signal whose default ends the process ends it when the context ends. Blocking the signal for
    the main thread alone wasn't enough with numpy's threads running; with none running (one BLAS thread, as on
    macOS) a blocked stop waited for the context's end, past the limit, and then left a noting handler behind, so the
    next context's SIGTERM handed itself on for ever (RecursionError)."""
    script = tmp_path / "held.py"
    script.write_text(_HELD, encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": CODE, "OPENBLAS_NUM_THREADS": blas_threads, "OMP_NUM_THREADS": blas_threads}
    out = subprocess.run([sys.executable, str(script)], env=env, capture_output=True,
                         text=True, timeout=120, preexec_fn=_signals_as_in_a_terminal)
    assert out.stdout.splitlines()[1:] == [
        "int nothing KeyboardInterrupt True True",
        "int-int nothing KeyboardInterrupt True True",
        "none nothing nothing True True",
        "limit KeyboardInterrupt nothing True True",
        "ignored nothing nothing False True",
    ], (out.stdout, out.stderr)
    assert out.returncode == -signal.SIGTERM and "still here" not in out.stdout

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


def test_a_stop_while_the_launcher_builds_keeps_a_script_s_ctrl_c_and_what_was_there(tmp_path, monkeypatch, capsys):
    """cmd_studio called from a script or a test (main(argv), not the command line): a stop during the first robot
    returns 130 and leaves the process's Ctrl+C handling as it was; only folders that start made go, never one that was
    there before (a demo project with no finished revision is built again next time)."""
    import signal as sig
    home = tmp_path / "projects"
    (home / "egg-picker").mkdir(parents=True)                       # there before, with no finished revision
    (home / "egg-picker" / "meta.json").write_text(json.dumps({"kind": "robot", "demo": True}), encoding="utf-8")
    monkeypatch.setattr(cli, "HOME", str(home))
    monkeypatch.setattr(keys, "ENV_PATH", str(tmp_path / ".env"))

    def stopped(a):
        from morph.studio import projects
        projects.create(str(home), a.name, demo=a.name in projects.DEMOS, unique=False)
        raise KeyboardInterrupt
    monkeypatch.setattr(cli, "cmd_robot", stopped)
    before = sig.getsignal(sig.SIGINT)
    for name, kept in (("egg-picker", True), ("my-arm", False)):
        with socket.socket() as free:
            free.bind(("127.0.0.1", 0))
            port = free.getsockname()[1]
        a = argparse.Namespace(name=name, host="127.0.0.1", port=port, open=False, remote=False, team=False,
                               allow_url_import=False)
        assert cli.cmd_studio(a) == 130
        assert sig.getsignal(sig.SIGINT) is before
        assert (home / name).exists() is kept and not (home / "so101").exists()
        out = capsys.readouterr().out.splitlines()
        assert out[-1] == f"Stopped before the Studio started: {name} wasn't finished, and the next start builds it again."
