@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run start.bat once to prepare Python dependencies.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m mock_shop.run
if errorlevel 1 pause
