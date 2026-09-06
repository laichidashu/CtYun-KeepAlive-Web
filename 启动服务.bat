@echo off
rem CtYun KeepAlive - one-click start (Python backend + web console, no Docker)
rem Flow: locate python -> ensure .venv -> env check & auto-install deps -> run server
setlocal
cd /d "%~dp0"
chcp 65001 >nul
echo ================================================
echo  CtYun KeepAlive  starting on http://localhost:8080
echo  Default password: admin   (change it in Settings)
echo  Press Ctrl+C to stop.
echo ================================================

rem ---- 1. Locate a base interpreter (prefer existing .venv) ----
set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if defined PY goto envcheck

set "SYS="
where python >nul 2>nul && set "SYS=python"
if not defined SYS (
    where py >nul 2>nul && set "SYS=py -3"
)
if not defined SYS (
    echo [ERROR] Python not found. Please install Python 3.10+ first.
    echo   Download from: https://www.python.org/downloads/
    echo   IMPORTANT: check "Add python.exe to PATH" during install.
    pause
    exit /b 1
)

rem ---- 2. Create project venv for dependency isolation ----
if not exist ".venv\Scripts\python.exe" (
    echo [SETUP] Creating virtual environment .venv ...
    %SYS% -m venv .venv
)
if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else (
    echo [WARN] Failed to create .venv, dependencies will be installed into system Python.
    set "PY=%SYS%"
)

:envcheck
rem ---- 3. Environment check + auto-install missing deps (fails - abort) ----
echo [SETUP] Checking environment and dependencies ...
%PY% backend\bootstrap.py
if errorlevel 1 (
    echo [ERROR] Environment check failed. Please fix the problems above and retry.
    pause
    exit /b 1
)

rem ---- 4. Start server ----
%PY% backend\server.py
pause
