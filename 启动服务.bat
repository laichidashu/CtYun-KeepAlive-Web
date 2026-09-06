@echo off
rem CtYun KeepAlive - one-click start (Python backend + web console, no Docker)
cd /d "%~dp0"
echo ================================================
echo  CtYun KeepAlive  starting on http://localhost:8080
echo  Default password: admin   (change it in Settings)
echo  Press Ctrl+C to stop.
echo ================================================
rem Prefer the project venv if present, then system python
if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
    goto run
)
where python >nul 2>nul
if %errorlevel%==0 (
    set "PY=python"
    goto run
)
echo [ERROR] Python not found. Please install Python 3.10+ first.
pause
exit /b 1
:run
"%PY%" backend\server.py
pause
