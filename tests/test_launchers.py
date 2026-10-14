"""The launchers, offline: run_studio.bat lets a launcher from before v0.2 (whose fifth line was "git pull") hand over
to it in the middle of a run; neither launcher passes a pasted "# comment" on to the Studio; and the Windows launcher
keeps options out of parenthesized blocks."""

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
OLD_BAT = REPO / "tests" / "fixtures" / "launchers" / "run_studio_v01.bat"
OLD_BAT_SHA256 = "4fae88c504fa90994cbfc469a0d7f3157e86ffc1163659e09af26d9081c075d5"  # git show 0f75a21:run_studio.bat
OLD_FIRST_LINES = [b"@echo off", b"REM Morph Studio one-click launcher for Windows.",
                   b"REM First run: creates .venv and installs dependencies (a few minutes). Every run: pulls the "
                   b"latest code.", b'cd /d "%~dp0"', b"git pull --ff-only"]
COMMENT_NOTE = "Ignoring the rest of the line from # on: it is a comment, not options for Morph."
needs_bash = pytest.mark.skipif(os.name == "nt" or not shutil.which("bash"), reason="needs bash (macOS, Linux)")


def _crlf(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")


def _line_starts(data: bytes) -> set:
    return {0} | {i + 1 for i, c in enumerate(data) if c == 0x0A}


def _resume_offset(old: bytes) -> int:
    """Where cmd.exe reads on once the old launcher's fifth line, "git pull", has run: where its sixth line began."""
    lines = old.split(b"\n")
    assert [line.rstrip(b"\r") for line in lines[:5]] == OLD_FIRST_LINES
    return sum(len(line) + 1 for line in lines[:5])


def _bat() -> bytes:
    """run_studio.bat as git checks it out on Windows (.gitattributes: CRLF), whatever this checkout did."""
    return _crlf((REPO / "run_studio.bat").read_bytes())


def _bat_lines() -> list:
    return _bat().decode("utf-8").split("\r\n")[:-1]


# ------------------------------------------------------------------ Windows: upgrading from a launcher that pulled

def test_the_old_launcher_fixture_is_the_v01_launcher_with_lf_endings():
    old = OLD_BAT.read_bytes()
    assert hashlib.sha256(old).hexdigest() == OLD_BAT_SHA256 and b"\r" not in old
    if shutil.which("git") and (REPO / ".git").exists():
        shown = subprocess.run(["git", "-C", str(REPO), "show", "0f75a21:run_studio.bat"], capture_output=True)
        if shown.returncode == 0:  # a shallow clone (CI) doesn't have that commit
            assert shown.stdout == old


def test_old_launchers_resume_at_byte_198_with_lf_and_203_with_crlf():
    old = OLD_BAT.read_bytes()
    assert _resume_offset(old) == 198
    assert _resume_offset(_crlf(old)) == 203


def test_the_launcher_starts_a_line_at_both_resume_offsets():
    new = _bat()
    old = OLD_BAT.read_bytes()
    for offset in (_resume_offset(old), _resume_offset(_crlf(old))):
        assert offset in _line_starts(new), offset
    assert new[198:203] == b"REM\r\n"
    head = new[:198].decode("ascii").split("\r\n")
    assert head[-1] == "" and head[0] == "@echo off", head
    for line in head[1:-1]:
        assert line == "REM" or line.startswith("REM "), line
    # REM lines are percent-expanded before cmd.exe sees the REM: "%~" there would stop the launcher
    rems = [line for line in _bat_lines() if line.upper().startswith("REM")]
    assert not [line for line in rems if "%" in line or line.endswith("^")]


def test_a_resumed_launcher_runs_every_command_of_a_fresh_one():
    new = _bat()
    fresh = [line for line in new.decode().split("\r\n") if line and not line.upper().startswith(("REM", "@ECHO"))]
    for offset in (198, 203):
        resumed = [line for line in new[offset:].decode().split("\r\n")
                   if line and not line.upper().startswith(("REM", "@ECHO"))]
        assert resumed == fresh
    first = fresh[0]
    assert first == "setlocal EnableExtensions DisableDelayedExpansion"  # before it, nothing depends on the context


# ------------------------------------------------------------------ Windows: the options

USER_TEXT = re.compile(r"%~?[1-9*]|%ARGS?[%:]", re.I)


def test_options_never_go_inside_a_parenthesized_block():
    """cmd.exe reads a block in full with every %variable% replaced first, so an option like "x)" would close it."""
    depth = 0
    for n, line in enumerate(_bat_lines(), 1):
        stripped = line.strip()
        if not stripped or stripped.upper().startswith("REM") or stripped.startswith(":"):
            continue
        if depth == 0 and stripped.lower().startswith("echo"):
            continue  # outside a block, an echo prints its parentheses
        code = re.sub(r'"[^"]*"', '""', stripped)  # quoted text neither opens nor closes a block
        if depth or "(" in code or ")" in code:
            assert not USER_TEXT.search(line), f"run_studio.bat line {n} puts options inside parentheses: {line}"
        depth += code.count("(") - code.count(")")
    assert depth == 0


def test_the_option_loop_ends_at_a_pasted_comment_and_finds_update_anywhere():
    lines = _bat_lines()
    loop = lines[lines.index(":args"):lines.index(":args_done")]
    assert 'set "ARG=%~1"' in loop and 'if "%ARG:~0,1%"=="#" goto args_comment' in loop
    assert 'if /i "%ARG%"=="--update" set "UPDATE=1"' in loop
    assert 'if /i not "%ARG%"=="--update" set ARGS=%ARGS% %1' in loop
    assert "shift" in loop and loop[loop.index(":args_comment") + 1] == "echo " + COMMENT_NOTE


def test_update_relaunches_from_a_line_read_before_git_runs():
    lines = _bat_lines()
    calls = [line for line in lines if "call :update" in line]
    assert calls == ['call :update & if not errorlevel 1 call "%~f0" %ARGS% & exit /b']
    i = lines.index(calls[0])
    assert lines[i - 3] == "if not defined UPDATE goto start" and lines[i + 2] == ":start"
    checkout = [line for line in lines if "checkout" in line and not line.startswith("REM")]
    assert len(checkout) == 1 and checkout[0].endswith("& exit /b 0")  # git may replace the file: nothing after it
    j = lines.index(checkout[0])
    assert lines[j + 1] == "" and lines[j + 2].startswith(":update_")


# ------------------------------------------------------------------ macOS and Linux

def _bash(script: str, cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", script, "bash", *args], cwd=cwd, capture_output=True, text=True, timeout=60)


@needs_bash
def test_parse_args_drops_a_pasted_comment(tmp_path):
    show = (f'source "{REPO / "run_studio.sh"}"; parse_args "$@"; echo "update=$want_update"; '
            'for a in ${args[@]+"${args[@]}"}; do echo "[$a]"; done')
    out = _bash(show, tmp_path, "--update", "#", "macOS", "/", "Linux", "(later,", "./run_studio.sh)")
    assert out.returncode == 0 and out.stdout.splitlines() == [COMMENT_NOTE, "update=1"], out
    out = _bash(show, tmp_path, "--remote", "--port", "8766", "--update", "#Windows", "--team")
    assert out.stdout.splitlines() == [COMMENT_NOTE, "update=1", "[--remote]", "[--port]", "[8766]"], out
    out = _bash(show, tmp_path, "--remote", "a#b", "R Cubed")
    assert out.stdout.splitlines() == ["update=0", "[--remote]", "[a#b]", "[R Cubed]"], out
    assert out.stderr == "" and not (tmp_path / ".venv").exists()  # sourcing defines the functions, runs nothing


@needs_bash
def test_the_shell_launcher_starts_the_studio_without_the_comment(tmp_path):
    """The whole launcher, with a stand-in .venv whose python prints what it was asked to run."""
    for name in ("run_studio.sh", "requirements.txt", "constraints.txt"):
        shutil.copy(REPO / name, tmp_path / name)
    bin_dir = tmp_path / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "activate").write_text(f'PATH="{bin_dir}:$PATH"\nexport PATH\n', encoding="utf-8")
    (bin_dir / "python").write_text('#!/bin/sh\n[ "$1" = "-c" ] && exit 0\necho "python $*"\n', encoding="utf-8")
    (bin_dir / "python").chmod(0o755)
    want = _bash("cat requirements.txt constraints.txt | cksum", tmp_path).stdout
    (tmp_path / ".venv" / ".morph-installed").write_text(want, encoding="utf-8")
    run = ["bash", str(tmp_path / "run_studio.sh")]
    out = subprocess.run(run + ["--help", "#", "macOS", "/", "Linux", "(later,", "./run_studio.sh)"],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out
    assert out.stdout.splitlines() == [COMMENT_NOTE, "python -m morph.cli studio egg-picker --open --help"], out
    out = subprocess.run(run + ["#", "macOS", "/", "Linux"], capture_output=True, text=True, timeout=60)
    assert out.stdout.splitlines()[-1] == "python -m morph.cli studio egg-picker --open", out
    out = subprocess.run(run + ["--remote", "--update", "--port", "8766"], capture_output=True, text=True, timeout=60)
    assert "isn't a git clone" in out.stdout, out  # --update anywhere; this folder has no .git, so it starts as is
    assert out.stdout.splitlines()[-1] == "python -m morph.cli studio egg-picker --open --remote --port 8766", out


@needs_bash
@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_bash_finishes_the_script_it_opened_when_git_replaces_it(tmp_path):
    """Why run_studio.sh needs no byte layout: git writes a new file, and bash keeps reading the one it opened."""
    git = ["git", "-c", "user.name=Morph", "-c", "user.email=morph@example.com", "-c", "commit.gpgsign=false",
           "-c", "core.autocrlf=false", "-C", str(tmp_path)]
    env = {**os.environ, "HOME": str(tmp_path), "GIT_CONFIG_NOSYSTEM": "1"}

    def sh(*args):
        subprocess.run(git + list(args), check=True, capture_output=True, env=env)

    sh("init", "-q")
    (tmp_path / "launch.sh").write_text('git checkout -q newer\necho "the old script went on"\n', encoding="utf-8")
    sh("add", "launch.sh")
    sh("commit", "-q", "-m", "old")
    sh("checkout", "-q", "-b", "newer")
    (tmp_path / "launch.sh").write_text('echo "a much longer first line, so that the offsets differ: ..........."\n'
                                        'echo "the new script ran"\n', encoding="utf-8")
    sh("commit", "-q", "-am", "new")
    sh("checkout", "-q", "-")
    out = subprocess.run(["bash", "launch.sh"], cwd=tmp_path, capture_output=True, text=True, env=env, timeout=60)
    assert out.stdout == "the old script went on\n", out
    assert "the new script ran" in (tmp_path / "launch.sh").read_text(encoding="utf-8")


def test_shell_launcher_option_handling():
    sh = (REPO / "run_studio.sh").read_text(encoding="utf-8")
    assert "'#'*)" in sh and COMMENT_NOTE in sh and "--update) want_update=1 ;;" in sh
    assert sh.rstrip().endswith('if ! (return 0 2>/dev/null); then\n  main "$@"; exit $?\nfi')
