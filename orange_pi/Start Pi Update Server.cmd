@echo off
setlocal
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
  echo ATHENA's Python environment is missing. Run the normal PC setup first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" "orange_pi\pc\dev_update_server.py"
if errorlevel 1 pause
