@echo off
REM Morph Studio launcher for Windows. Keep these lines byte for byte: the bare REM line after
REM them must start at byte 198, and the script at byte 203 (the notes after it say why).
REM
REM Why those offsets: launchers before v0.2 ran "git pull --ff-only" as their fifth line. When that pull replaces
REM this file, cmd.exe goes on reading the NEW file at the byte where the old file's sixth line began: 198 if the
REM old file had LF line endings, 203 with CRLF (Git for Windows' default). So bytes 0 to 197 hold only "@echo off"
REM and REM lines, bytes 198 to 202 the line "REM", and the script starts at byte 203. Whichever of the two offsets
REM cmd.exe resumes at, all of the script runs, in the old launcher's batch context: echo off, in this folder (the
REM old one changed to it), no setlocal yet, and the old launcher's arguments, which it never shifted. The script
REM assumes nothing else, so it runs the same started fresh, entered from an old launcher, or relaunched by
REM --update. tests/test_launchers.py checks the offsets. These lines contain no percent signs on purpose.
REM
REM The first run makes .venv with Python 3.10 to 3.14 and installs the tested packages (requirements.txt with
REM constraints.txt: a few minutes, with progress) and Morph itself, so .venv has the morph command. Later runs start
REM in seconds and use neither git nor the network.
REM   run_studio.bat             start the Studio (other options go to "morph studio", e.g. --remote)
REM   run_studio.bat --update    first switch this git clone to the latest release (newest v* tag), then start
REM An option starting with # ends the options: the rest of the line was a comment pasted along with the command
REM (cmd.exe has no # comments), so it is dropped instead of reaching "morph studio".
setlocal EnableExtensions DisableDelayedExpansion
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"
REM probing for Python must never start an install from the Python install manager
set "PYTHON_MANAGER_AUTOMATIC_INSTALL=false"
set "PYCHECK=import sys; sys.exit(not ((3, 10) <= sys.version_info[:2] <= (3, 14)))"
set "UPDATE="
set "ARGS="
set "ARG="
REM this file's full path, kept before the option loop: --update starts the file again from it
set "SELF=%~f0"

REM The options never go inside parentheses: cmd.exe reads a parenthesized block in full with every variable
REM already replaced by its text, so an option such as "x)" would end the block early. "shift /1" leaves the
REM file's own name alone; a plain shift would move each option into it.
:args
if "%~1"=="" goto args_done
set "ARG=%~1"
if "%ARG:~0,1%"=="#" goto args_comment
if /i "%ARG%"=="--update" set "UPDATE=1"
if /i not "%ARG%"=="--update" set ARGS=%ARGS% %1
shift /1
goto args

:args_comment
echo Ignoring the rest of the line from # on: it is a comment, not options for Morph.

:args_done
if not defined UPDATE goto start
REM git may replace this file while :update runs, so what comes after it is on this one line, which cmd.exe read in
REM full beforehand: when :update ran git checkout, start the launcher now on disk from its first line, then stop.
REM CALL reads its command a second time, doubling every caret and replacing percent signs again: the doubled
REM percent signs hand it the name SELF, so it reads this file's path (which may hold a caret) once, as it is.
call :update & if not errorlevel 1 call "%%SELF%%" %ARGS% & exit /b

:start

if exist .venv\Scripts\activate.bat .venv\Scripts\python.exe -c "%PYCHECK%" >nul 2>&1 && goto venv_ready
if not exist .venv goto make_venv
echo The .venv folder is incomplete or uses an unsupported Python: making a new one.
rmdir /s /q .venv
if exist .venv goto venv_locked

:make_venv
REM MuJoCo, Morph's physics engine, and some of its other packages have builds for 64-bit Windows on Intel or AMD
REM processors only: say so before anything is made or downloaded
if /i "%PROCESSOR_ARCHITECTURE%"=="ARM64" goto unsupported
if /i "%PROCESSOR_ARCHITEW6432%"=="ARM64" goto unsupported
if /i "%PROCESSOR_ARCHITECTURE%"=="x86" if not defined PROCESSOR_ARCHITEW6432 goto unsupported
call :find_python
if not defined PY goto no_python
echo Creating .venv with %PY%...
%PY% -m venv .venv
if errorlevel 1 goto venv_failed
if not exist .venv\Scripts\activate.bat goto venv_failed

:venv_ready
REM install once, and again only when requirements.txt, constraints.txt or pyproject.toml change (after --update)
copy /b /y requirements.txt+constraints.txt+pyproject.toml .venv\morph-packages.new >nul
fc /b .venv\morph-packages.new .venv\morph-installed.txt >nul 2>&1 && goto installed
echo Installing Morph's packages into .venv (numpy, SciPy, MuJoCo and others; the first time takes a few minutes).
.venv\Scripts\python.exe -m pip install --quiet --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt -c constraints.txt -e .
if errorlevel 1 goto install_failed
move /y .venv\morph-packages.new .venv\morph-installed.txt >nul

:installed
call .venv\Scripts\activate.bat
REM One line, the last one read from this file: a git pull or --update in another window may replace the file while
REM the Studio runs, so nothing after it is read from the file. On an error, pause keeps the window open to read it.
python -m morph.cli studio egg-picker --open %ARGS% || pause & exit /b

:find_python
set "PY="
for %%v in (3.13 3.12 3.11 3.10 3.14) do if not defined PY py -%%v -c "%PYCHECK%" >nul 2>&1 && set "PY=py -%%v"
if not defined PY python -c "%PYCHECK%" >nul 2>&1 && set "PY=python"
exit /b 0

:no_python
echo.
echo Morph needs Python 3.10, 3.11, 3.12, 3.13 or 3.14 and found none of them.
echo Install Python 3.13 from https://www.python.org/downloads/windows/ (tick "Add python.exe to PATH"),
echo then run this again. Python 3.15 is too new for now: one of Morph's libraries has no build for it yet.
goto fail

:venv_failed
rmdir /s /q .venv 2>nul
echo.
echo Python couldn't create .venv (details above). Reinstall Python 3.13 from https://www.python.org/downloads/windows/
goto fail

:venv_locked
echo.
echo Couldn't replace .venv: close any other Morph window, then run this again.
goto fail

:install_failed
echo.
echo Installing the packages failed (details above). Check the internet connection, then run this again.
echo Morph's packages have builds for Windows on Intel or AMD processors, Macs with Apple silicon and 64-bit Linux;
echo on other computers this step fails.
goto fail

:unsupported
echo.
echo Morph doesn't run on this computer yet: MuJoCo, its physics engine, and some of its other packages have builds
echo for 64-bit Windows on Intel or AMD processors only, not for Windows on ARM or 32-bit Windows. Nothing was
echo installed. A Mac with Apple silicon, or a Windows or Linux computer with an Intel or AMD processor, works.
goto fail

:fail
pause
exit /b 1

REM exit code 0: git checkout ran, so the caller starts the launcher now on disk; 1: nothing changed here
:update
if not exist .git goto update_no_git
where git >nul 2>&1 || goto update_no_git_exe
echo Looking for the latest Morph release...
git fetch --tags --force --quiet || goto update_offline
set "LATEST="
for /f "delims=" %%t in ('git -c versionsort.suffix^=a -c versionsort.suffix^=b -c versionsort.suffix^=rc tag --list "v*" --sort^=-v:refname') do if not defined LATEST set "LATEST=%%t"
if not defined LATEST goto update_none
set "HEAD_SHA="
set "TAG_SHA="
for /f %%s in ('git rev-parse HEAD') do set "HEAD_SHA=%%s"
for /f %%s in ('git rev-parse "%LATEST%~0"') do set "TAG_SHA=%%s"
if "%HEAD_SHA%"=="%TAG_SHA%" goto update_current
git merge-base --is-ancestor "%LATEST%" HEAD && goto update_newer
set "DIRTY="
for /f %%s in ('git status --porcelain --untracked-files^=no') do set "DIRTY=1"
if defined DIRTY goto update_dirty
REM One line, the last one read from this file: git may replace the file, so nothing after it is read from it, and
REM even when the checkout fails part way the caller starts whichever launcher is on disk, from its first line.
git -c advice.detachedHead=false checkout --quiet "%LATEST%" && echo Switched to Morph %LATEST%.|| echo Couldn't switch to %LATEST% (details above). Starting the version that is here.& exit /b 0

:update_no_git
echo --update: this folder isn't a git clone, so it can't switch versions itself. Download the latest release
echo from https://github.com/jim4226/A16z_Hackathon/releases into a new folder and copy your projects folder
echo and .env across. Starting the version you have.
exit /b 1

:update_no_git_exe
echo --update needs Git: https://git-scm.com/download/win . Starting the version you have.
exit /b 1

:update_offline
echo Couldn't reach GitHub (details above). Starting the version you have.
exit /b 1

:update_none
echo There is no release (v* tag) yet: "git pull" gets the newest code of this branch. Starting the version you have.
exit /b 1

:update_current
echo Already on %LATEST%, the latest release.
exit /b 1

:update_newer
echo This clone has %LATEST%, the latest release, and newer commits, so --update leaves it as it is. Starting the
echo version you have ("git pull" gets the newest code of its branch).
exit /b 1

:update_dirty
echo Files in this folder were changed (git status lists them), so --update won't switch to %LATEST%.
echo Commit or undo the changes, then run it again. Starting the version you have.
exit /b 1
