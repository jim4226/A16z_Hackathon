# Launcher fixtures

`run_studio_v01.bat` is the Windows launcher of commit 0f75a21, byte for byte, with the LF line endings it was
committed with (`.gitattributes` keeps it LF). `main` shipped it until v0.2.

Every Windows launcher before v0.2 began with the same five lines: e8e642c, 253c204, d53dcaf and 0f75a21 on this
branch, and the `main`, `morph-sponsors` and `claude/focused-volta-famp5o` branches. They are `@echo off`, two REM
lines, `cd /d "%~dp0"` and `git pull --ff-only`, so they all pulled on every start. c3a4949 is the first launcher that
never runs git on its own.

When that pull brings in a new `run_studio.bat`, cmd.exe goes on reading the new file at the byte where the old
file's sixth line began: 198 when the old file has LF line endings (git with `core.autocrlf` false or input), 203
with CRLF (Git for Windows' default, `core.autocrlf` true). `tests/test_launchers.py` checks that the current
launcher starts a line at both offsets, and the `launchers` job in `.github/workflows/ci.yml` runs this file in a
clone and lets its pull hand over to the current launcher, with LF and with CRLF.
