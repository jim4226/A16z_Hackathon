@echo off
setlocal
cd /d "%~dp0"
set PYTHONUTF8=1
py -3.11 tools\install_local.py --setup
if errorlevel 1 (
  echo.
  echo Setup failed. Python 3.11 and internet access are required. See docs\INSTALL.md.
  pause
  exit /b 1
)
echo R Cubed is installed. Open run_studio.bat to start.
pause
