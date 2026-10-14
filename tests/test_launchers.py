"""The launchers, offline: run_studio.bat lets a launcher from before v0.2 (whose fifth line was "git pull") hand over
to it in the middle of a run; neither launcher passes a pasted "# comment" on to the Studio; the Windows launcher keeps
options out of parenthesized blocks; and every command in the docs pastes as is into Command Prompt, PowerShell, bash
and zsh. The launchers job in .github/workflows/ci.yml runs both launchers for real, on Windows, macOS and Linux."""

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
    assert "shift /1" in loop and loop[loop.index(":args_comment") + 1] == "echo " + COMMENT_NOTE


def test_the_option_loop_never_moves_an_option_into_the_launchers_own_name():
    """A plain SHIFT copies %1 into %0, so after the loop %~f0 named the last option ("<folder>\\--update") and the
    --update relaunch ran that instead of the launcher. The loop uses SHIFT /1, and the relaunch uses the path kept
    in SELF before the loop."""
    lines = _bat_lines()
    code = [line.strip().lower() for line in lines if line.strip() and not line.upper().startswith("REM")]
    assert not [line for line in code if line == "shift" or line.startswith("shift ") and line != "shift /1"]
    loop = lines.index(":args")
    assert 'set "SELF=%~f0"' in lines[:loop]
    for line in lines[loop:]:  # %0 itself is never read after the loop starts (cd /d "%~dp0" comes before it)
        if not line.upper().startswith("REM"):
            assert not re.search(r"%~?[a-z]*0", re.sub(r"%\w+%", "", line), re.I), line


def test_update_relaunches_from_a_line_read_before_git_runs():
    lines = _bat_lines()
    calls = [line for line in lines if "call :update" in line]
    # CALL reads its command again, doubling carets and expanding percent signs: %%SELF%% hands it the name, so the
    # path, which may hold ^ or %, is read once, as it is. %ARGS% is expanded when the line is read (an undefined
    # ARGS, as with --update alone, is then simply empty)
    assert calls == ['call :update & if not errorlevel 1 call "%%SELF%%" %ARGS% & exit /b']
    i = lines.index(calls[0])
    assert lines[i - 5] == "if not defined UPDATE goto start" and lines[i + 2] == ":start"
    checkout = [line for line in lines if "checkout" in line and not line.startswith("REM")]
    assert len(checkout) == 1 and checkout[0].endswith("& exit /b 0")  # git may replace the file: nothing after it
    j = lines.index(checkout[0])
    assert lines[j + 1] == "" and lines[j + 2].startswith(":update_")


def test_update_leaves_a_clone_that_is_ahead_of_the_newest_tag_alone():
    lines = _bat_lines()
    same = lines.index('if "%HEAD_SHA%"=="%TAG_SHA%" goto update_current')
    assert lines[same + 1] == 'git merge-base --is-ancestor "%LATEST%" HEAD && goto update_newer'
    newer = lines.index(":update_newer")
    assert lines[newer + 1].startswith("echo This clone has %LATEST%, the latest release, and newer commits")
    assert lines[newer + 3] == "exit /b 1"


def test_nothing_is_read_from_the_launcher_after_the_studio_starts():
    """A git pull or --update in another window may replace run_studio.bat while the Studio runs; cmd.exe would then
    read on mid-line in the new file when the Studio stops (the bug of launchers before v0.2)."""
    lines = _bat_lines()
    studio = [line for line in lines if "morph.cli studio" in line and not line.upper().startswith("REM")]
    assert studio == ["python -m morph.cli studio egg-picker --open %ARGS% || pause & exit /b"]


def test_the_launchers_install_the_morph_command_and_reinstall_when_pyproject_changes():
    bat, sh = _bat().decode(), (REPO / "run_studio.sh").read_text(encoding="utf-8")
    assert "copy /b /y requirements.txt+constraints.txt+pyproject.toml .venv\\morph-packages.new >nul" in bat
    assert ".venv\\Scripts\\python.exe -m pip install -r requirements.txt -c constraints.txt -e .\r\n" in bat
    assert "want=$(cat requirements.txt constraints.txt pyproject.toml | cksum)" in sh
    assert ".venv/bin/python -m pip install -r requirements.txt -c constraints.txt -e .; then" in sh


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
    for name in ("run_studio.sh", "requirements.txt", "constraints.txt", "pyproject.toml"):
        shutil.copy(REPO / name, tmp_path / name)
    bin_dir = tmp_path / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "activate").write_text(f'PATH="{bin_dir}:$PATH"\nexport PATH\n', encoding="utf-8")
    (bin_dir / "python").write_text('#!/bin/sh\n[ "$1" = "-c" ] && exit 0\necho "python $*"\n', encoding="utf-8")
    (bin_dir / "python").chmod(0o755)
    want = _bash("cat requirements.txt constraints.txt pyproject.toml | cksum", tmp_path).stdout
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


@needs_bash
@pytest.mark.skipif(not shutil.which("git"), reason="needs git")
def test_update_switches_only_a_clone_that_is_behind_the_newest_tag(tmp_path):
    """run_studio.sh's update() against a real upstream: no tag yet, behind the tag (switches), on it, and ahead of it
    (a branch with commits after the release stays as it is). run_studio.bat's :update makes the same checks."""
    env = {**os.environ, "HOME": str(tmp_path), "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}
    for k in [k for k in env if k.startswith("GIT_") and k not in ("GIT_CONFIG_NOSYSTEM", "GIT_TERMINAL_PROMPT")]:
        del env[k]
    cfg = ["-c", "user.name=Morph", "-c", "user.email=morph@example.com", "-c", "commit.gpgsign=false",
           "-c", "tag.gpgsign=false", "-c", "init.defaultBranch=main"]

    def git(cwd, *args):
        return subprocess.run(["git", *cfg, *args], cwd=cwd, check=True, capture_output=True, text=True,
                              env=env).stdout.strip()

    up, clone = tmp_path / "up.git", tmp_path / "clone"
    git(tmp_path, "init", "-q", "--bare", str(up))
    git(tmp_path, "clone", "-q", str(up), str(clone))
    shutil.copy(REPO / "run_studio.sh", clone / "run_studio.sh")
    git(clone, "add", "run_studio.sh")
    git(clone, "commit", "-q", "-m", "before the release")
    git(clone, "push", "-q", "origin", "main")

    def update():
        return subprocess.run(["bash", "-c", 'source ./run_studio.sh; update; echo "rc=$?"'], cwd=clone,
                              capture_output=True, text=True, env=env, timeout=60)

    out = update()
    assert "There is no release (v* tag) yet" in out.stdout and out.stdout.endswith("rc=1\n"), out
    before = git(clone, "rev-parse", "HEAD")
    (clone / "release.txt").write_text("the release\n", encoding="utf-8")
    git(clone, "add", "release.txt")
    git(clone, "commit", "-q", "-m", "the release")
    git(clone, "tag", "v0.2.0b1")
    git(clone, "push", "-q", "origin", "main", "v0.2.0b1")
    release = git(clone, "rev-parse", "HEAD")
    git(clone, "reset", "-q", "--hard", before)  # behind the tag: --update switches
    out = update()
    assert "Switched to Morph v0.2.0b1." in out.stdout and out.stdout.endswith("rc=0\n"), out
    assert git(clone, "rev-parse", "HEAD") == release
    out = update()
    assert "Already on v0.2.0b1, the latest release." in out.stdout and out.stdout.endswith("rc=1\n"), out
    git(clone, "checkout", "-q", "-B", "main", "v0.2.0b1")
    (clone / "fix.txt").write_text("a fix after the release\n", encoding="utf-8")
    git(clone, "add", "fix.txt")
    git(clone, "commit", "-q", "-m", "a fix after the release")
    ahead = git(clone, "rev-parse", "HEAD")
    out = update()
    assert "This clone has v0.2.0b1, the latest release, and newer commits" in out.stdout, out
    assert out.stdout.endswith("rc=1\n") and git(clone, "rev-parse", "HEAD") == ahead
    assert git(clone, "branch", "--show-current") == "main"  # not detached


# ------------------------------------------------------------------ morph studio, as any launcher starts it

def test_morph_studio_ignores_update_and_a_pasted_comment(tmp_path, capsys, monkeypatch):
    """A launcher from before v0.2 runs git pull, then passes everything it was given to the new `morph studio`:
    --update (the README's command now) and a comment pasted with the command. Neither stops the Studio."""
    from morph import cli
    monkeypatch.setattr(cli, "HOME", str(tmp_path))
    monkeypatch.delenv("MORPH_TOKEN", raising=False)
    with pytest.raises(SystemExit) as stop:  # the name is refused only after the options were accepted
        cli.main(["studio", "../not-a-project", "--open", "--update", "#", "macOS", "/", "Linux"])
    out = capsys.readouterr().out
    assert COMMENT_NOTE in out and "--update is an option of the launcher" in out
    assert "../not-a-project" in str(stop.value.code)
    assert cli._studio_argv(["robot", "new", "#1 robot"]) == ["robot", "new", "#1 robot"]  # only `morph studio`


def test_commands_in_messages_name_the_launchers_venv(monkeypatch, tmp_path):
    """Messages that tell people what to run give a command that works in a terminal in the Morph folder: the
    launchers' .venv by its path (no activating needed), and never `pip install morph[...]`, an unrelated PyPI project."""
    from morph import resources
    venv = tmp_path / ".venv"
    sub = venv / ("Scripts" if os.name == "nt" else "bin")
    sub.mkdir(parents=True)
    monkeypatch.setattr(resources, "checkout", lambda: str(tmp_path))
    monkeypatch.setattr(resources.sys, "prefix", str(venv))
    py = os.path.join(".venv", "Scripts" if os.name == "nt" else "bin", "python")
    assert resources.morph_command() == f"{py} -m morph.cli"
    (sub / ("morph.exe" if os.name == "nt" else "morph")).write_text("", encoding="utf-8")
    assert resources.morph_command() == os.path.join(".venv", sub.name, "morph")
    assert resources.pip_install_command("imageio-ffmpeg") == f"{py} -m pip install imageio-ffmpeg -c constraints.txt"
    monkeypatch.setattr(resources.sys, "prefix", str(tmp_path / "elsewhere"))
    assert resources.morph_command() == "morph"
    assert resources.pip_install_command("cascadio") == "python -m pip install cascadio"
    for path in ("morph/cli.py", "morph/importers/xacro_sandbox.py", "morph/importers/urdf.py",
                 "morph/studio/api_video.py", "morph/studio/static/video.js"):
        assert "morph[" not in (REPO / path).read_text(encoding="utf-8"), path


# ------------------------------------------------------------------ the docs

DOCS = [REPO / "README.md", REPO / "SECURITY.md", REPO / "CHANGELOG.md"] + sorted((REPO / "docs").glob("*.md"))
# command blocks by label: which shells each must paste into as is
SHELLS = {"shell": "Command Prompt, PowerShell, bash and zsh", "bat": "Command Prompt and PowerShell",
          "cmd": "Command Prompt", "powershell": "PowerShell", "sh": "bash and zsh"}
NOT_COMMANDS = {"json", "python", "js", "text"}


def _blocks(path: Path):
    """(label, first line number, lines) of each fenced code block."""
    label, start, lines = None, 0, []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        m = re.match(r"\s*```(.*)$", line)
        if m and label is None:
            label, start, lines = m[1].strip(), n + 1, []
        elif m:
            yield label, start, lines
            label = None
        elif label is not None:
            lines.append(line)


def _unquoted(line: str, singles: bool) -> str:
    """The line without its quoted strings: what each shell sees as operators and globs. Command Prompt has no
    single quotes, and it toggles quoting at every double quote, \\" included."""
    return re.sub(r"\"[^\"]*\"|'[^']*'" if singles else r'"[^"]*"', " ", line)


def _problems(label: str, line: str) -> list:
    out = []
    bare = _unquoted(line, singles=label in ("sh", "powershell"))
    if re.search(r"(^|\s)#", bare):
        out.append("a # comment (zsh and Command Prompt pass it on as arguments)")
    if "&&" in bare or "||" in bare:
        out.append("&& or || (Windows PowerShell 5.1 has neither): one command per line")
    if label in ("shell", "bat", "cmd") and "'" in bare:
        out.append("single quotes (Command Prompt doesn't treat them as quotes)")
    if label in ("shell", "bat"):
        bad = sorted(set(re.findall(r"[$`%!;&|<>(){}\[\]*?,^]", bare)) | ({"\\"} if label == "shell" and "\\" in line else set()))
        if bad:
            out.append(f"{' '.join(bad)} outside double quotes")
        if re.search(r"(^|\s)[@~]", bare):
            out.append("@ or ~ starting a word")
    if label == "shell" and re.search(r"(^|\s)\.[/\\]|run_studio\.", line):
        out.append("a launcher or a ./ path: Windows and macOS/Linux need their own blocks")
    if label == "sh" and re.search(r"[()*?\[\]{}!;]", bare):
        out.append("a glob or a parenthesis outside quotes")
    if label == "sh" and re.search(r"run_studio\.bat|\.\\", line):
        out.append("a Windows command in a macOS/Linux block")
    if label in ("bat", "cmd", "powershell") and re.search(r"run_studio\.sh|(^|\s)\./", line):
        out.append("a macOS/Linux command in a Windows block")
    if label in ("bat", "cmd", "powershell") and re.search(r"(?<!\.\\)run_studio\.bat", line):
        out.append(r"run_studio.bat without .\ (PowerShell doesn't run it from the current folder)")
    return out


def test_doc_commands_paste_as_is_into_every_shell():
    problems = []
    for path in DOCS:
        name = path.relative_to(REPO)
        for label, start, lines in _blocks(path):
            if label in NOT_COMMANDS:
                continue
            if label not in SHELLS:
                problems.append(f"{name}:{start - 1}: label the block: " + ", ".join(
                    f"{k} ({v})" for k, v in SHELLS.items()) + " or text")
                continue
            for n, line in enumerate(lines, start):
                problems += [f"{name}:{n}: ```{label} must paste into {SHELLS[label]}: {p}: {line}"
                             for p in _problems(label, line)]
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"`([^`\n]+)`", text):
            code = m[1]
            line = text.count("\n", 0, m.start()) + 1
            if re.search(r"(?<!\.\\)run_studio\.bat\s+-", code) or re.search(r"run_studio\s+-", code):
                problems.append(rf"{name}:{line}: write .\run_studio.bat (Command Prompt and PowerShell): `{code}`")
            if re.match(r"[A-Z][A-Z0-9_]*=\S*\s+\w", code):
                problems.append(f"{name}:{line}: VAR=value command works in bash and zsh only: `{code}`")
            if re.search(r"\s#\s", code) and re.match(r"(morph|python|git|\.?[/\\]?run_studio)\b", code):
                problems.append(f"{name}:{line}: a # comment on a command: `{code}`")
    assert problems == [], "\n".join(problems)
