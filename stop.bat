@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
call "%~dp0portable_env.bat"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop.ps1" -ProjectRoot "%~dp0."
if errorlevel 1 (
    echo Stop failed. No unverified process will be stopped.
    pause
    exit /b 1
)
pause
exit /b 0
