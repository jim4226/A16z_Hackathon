"""The CLI fails loudly when a robot build fails, never crashes on a console that can't show a character, says
which Morph it is, and talks the same way on every OS. Offline."""

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
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
