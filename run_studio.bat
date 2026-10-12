@echo off
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
if exist .rcubed-venv\Scripts\python.exe (
  .rcubed-venv\Scripts\python.exe tools\install_local.py --launch
) else (
  py -3.11 tools\install_local.py --launch
)
if errorlevel 1 (
  echo.
  echo R Cubed could not start. Install Python 3.11 from python.org, then run setup_studio.bat.
  pause
  exit /b 1
)
