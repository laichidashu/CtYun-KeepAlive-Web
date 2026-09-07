@echo off
rem CtYun KeepAlive launcher - all logic lives in start.ps1
rem (the PowerShell script uses an ASCII name on purpose: cmd mangles Chinese args passed to powershell)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"
pause
