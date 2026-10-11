@echo off
REM Morph Studio one-click launcher for Windows.
REM First run: creates .venv and installs dependencies (a few minutes). Every run: pulls the latest code.
cd /d "%~dp0"
git pull --ff-only
if not exist .venv (
  py -3 -m venv .venv || python -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install -q --upgrade pip
python -m pip install -q -r requirements.txt
set PYTHONUTF8=1
python -m morph.cli studio egg-picker --open
