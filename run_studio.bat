@echo off
setlocal EnableExtensions DisableDelayedExpansion
REM Morph Studio launcher for Windows. The first run makes .venv with Python 3.10 to 3.14 and installs the
REM tested packages (requirements.txt with constraints.txt: a few minutes, with progress). Later runs start in
REM seconds and use neither git nor the network.
REM   run_studio.bat             start the Studio (other options go to "morph studio", e.g. --remote)
REM   run_studio.bat --update    first switch this git clone to the latest release (newest v* tag), then start
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"
REM probing for Python must never start an install from the Python install manager
set "PYTHON_MANAGER_AUTOMATIC_INSTALL=false"
set "PYCHECK=import sys; sys.exit(not ((3, 10) <= sys.version_info[:2] <= (3, 14)))"
set "UPDATE="
set "ARGS="

:args
if "%~1"=="" goto args_done
if /i "%~1"=="--update" (set "UPDATE=1") else (set ARGS=%ARGS% %1)
shift
goto args
:args_done

REM git replaces this file during --update, so what runs after it comes from this block, read in full beforehand
if defined UPDATE (
  call :update
  if not errorlevel 1 (
    call "%~f0" %ARGS%
    exit /b
  )
)

if exist .venv\Scripts\activate.bat .venv\Scripts\python.exe -c "%PYCHECK%" >nul 2>&1 && goto venv_ready
if not exist .venv goto make_venv
echo The .venv folder is incomplete or uses an unsupported Python: making a new one.
rmdir /s /q .venv
if exist .venv goto venv_locked

:make_venv
call :find_python
if not defined PY goto no_python
echo Creating .venv with %PY%...
%PY% -m venv .venv
if errorlevel 1 goto venv_failed
if not exist .venv\Scripts\activate.bat goto venv_failed

:venv_ready
REM install once, and again only when requirements.txt or constraints.txt change (after --update)
copy /b /y requirements.txt+constraints.txt .venv\morph-packages.new >nul
fc /b .venv\morph-packages.new .venv\morph-installed.txt >nul 2>&1 && goto installed
echo Installing Morph's packages into .venv (numpy, SciPy, MuJoCo and others; the first time takes a few minutes).
.venv\Scripts\python.exe -m pip install --quiet --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt -c constraints.txt
if errorlevel 1 goto install_failed
move /y .venv\morph-packages.new .venv\morph-installed.txt >nul

:installed
call .venv\Scripts\activate.bat
python -m morph.cli studio egg-picker --open %ARGS%
if errorlevel 1 pause
exit /b

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
goto fail

:fail
pause
exit /b 1

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
for /f %%s in ('git rev-parse %LATEST%~0') do set "TAG_SHA=%%s"
if "%HEAD_SHA%"=="%TAG_SHA%" goto update_current
set "DIRTY="
for /f %%s in ('git status --porcelain --untracked-files^=no') do set "DIRTY=1"
if defined DIRTY goto update_dirty
REM one line: once git has replaced this file, nothing more is read from it
git -c advice.detachedHead=false checkout --quiet %LATEST% && (echo Switched to Morph %LATEST%.& exit /b 0) || (echo Couldn't switch to %LATEST%. Starting the version you have.& exit /b 1)

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
echo There is no release (v* tag) yet. Starting the version you have.
exit /b 1

:update_current
echo Already on %LATEST%, the latest release.
exit /b 1

:update_dirty
echo Files in this folder were changed (git status lists them), so --update won't switch to %LATEST%.
echo Commit or undo the changes, then run it again. Starting the version you have.
exit /b 1
