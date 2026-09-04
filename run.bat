@echo off
REM Girder alignment tool - Windows launcher.
cd /d "%~dp0"
if not exist .venv (
  echo Creating virtual environment...
  python -m venv .venv
  ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
  ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
  echo.
  echo For LIVE EPICS also install cothread ^(or pyepics^) and aapy:
  echo    .venv\Scripts\pip install cothread aapy
  echo.
)
".venv\Scripts\python.exe" server.py %*
pause
