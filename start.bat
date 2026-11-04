@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  python -m venv .venv
  if errorlevel 1 goto error
)
".venv\Scripts\python.exe" -c "import fastapi,uvicorn,numpy,cryptography,argon2,openai,httpx" >nul 2>&1
if errorlevel 1 (
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 goto error
)
".venv\Scripts\python.exe" run.py %*
if errorlevel 1 goto error
exit /b 0
:error
echo Installation or startup failed. Check Python 3.12+ and the error above.
pause
exit /b 1
