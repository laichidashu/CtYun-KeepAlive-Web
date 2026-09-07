@echo off
rem CtYun KeepAlive - one-click start (Python backend + web console, no Docker)
rem Flow: locate/install python -> ensure .venv -> env check & auto-install deps -> run server
setlocal
cd /d "%~dp0"
chcp 65001 >nul
echo ================================================
echo  CtYun KeepAlive  starting on http://localhost:8080
echo  Default password: admin   (change it in Settings)
echo  Press Ctrl+C to stop.
echo ================================================

rem ---- 1. Locate a usable interpreter (venv / PATH / common paths / auto-install) ----
set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY call :try_cmd python
if not defined PY call :try_cmd py
if not defined PY call :try_paths
if not defined PY call :install_python
if not defined PY goto no_python
echo [SETUP] Using Python: %PY%

rem ---- 2. Create project venv for dependency isolation ----
if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
    goto envcheck
)
echo [SETUP] Creating virtual environment .venv ...
"%PY%" -m venv .venv
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not exist ".venv\Scripts\python.exe" echo [WARN] Failed to create .venv, dependencies will be installed into the interpreter above.

:envcheck
rem ---- 3. Environment check + auto-install missing deps (fails - abort) ----
echo [SETUP] Checking environment and dependencies ...
"%PY%" backend\bootstrap.py
if errorlevel 1 (
    echo [ERROR] Environment check failed. Please fix the problems above and retry.
    pause
    exit /b 1
)

rem ---- 4. Start server ----
"%PY%" backend\server.py
pause
exit /b 0

rem ---------- subroutines ----------

:try_cmd
rem %1 = command name. Sets PY only if it exists AND is a real python (filters MS Store stub)
where %1 >nul 2>nul || goto :eof
%1 --version >nul 2>nul || goto :eof
set "PY=%~1"
goto :eof

:try_paths
rem Scan common per-user / system install locations
for %%V in (314 313 312 311 310) do (
    if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python%%V%\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python%%V%\python.exe"
    if not defined PY if exist "%ProgramFiles%\Python%%V%\python.exe" set "PY=%ProgramFiles%\Python%%V%\python.exe"
)
goto :eof

:install_python
rem Auto-download and silently install Python 3.12 (per-user, no admin needed)
where curl >nul 2>nul || goto :eof
set "INST=%TEMP%\python-3.12.8-amd64.exe"
del "%INST%" >nul 2>nul
echo [SETUP] Python not found. Auto-installing Python 3.12 ^(about 25 MB, please wait^)...
echo [SETUP] Downloading mirror 1/2: Huawei Cloud ...
curl -L --fail --connect-timeout 20 -o "%INST%" "https://mirrors.huaweicloud.com/python/3.12.8/python-3.12.8-amd64.exe" >nul 2>nul
call :check_inst
if not errorlevel 1 goto got_inst
echo [SETUP] Downloading mirror 2/2: python.org ...
curl -L --fail --connect-timeout 30 -o "%INST%" "https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe" >nul 2>nul
call :check_inst
if errorlevel 1 goto :eof
:got_inst
echo [SETUP] Installing silently - per-user, auto PATH. Takes 1-2 minutes, do not close...
"%INST%" /quiet InstallAllUsers=0 PrependPath=1 Include_test=0
del "%INST%" >nul 2>nul
set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
set /a TRIES=0
:wait_py
if exist "%PY%" goto :eof
set /a TRIES+=1
if %TRIES% gtr 80 (
    echo [ERROR] Python install did not finish in time.
    set "PY="
    goto :eof
)
timeout /t 3 /nobreak >nul
goto wait_py

:check_inst
rem Installer must exist and be larger than 10 MB (guards against failed/empty downloads)
if not exist "%INST%" exit /b 1
for %%A in ("%INST%") do if %%~zA LSS 10000000 exit /b 1
exit /b 0

:no_python
echo [ERROR] Python 3.10+ is required but could not be found or auto-installed.
echo   Manual install:
echo     1. Download: https://www.python.org/downloads/
echo     2. During install, CHECK "Add python.exe to PATH".
echo     3. Then run this script again.
pause
exit /b 1
