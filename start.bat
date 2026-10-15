@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
call "%~dp0portable_env.bat"
if not exist "%WP_PYTHON%" (
    echo Run setup.bat first, or configure WP_PYTHON in local-config.bat.
    pause
    exit /b 1
)
"%WP_PYTHON%" -B "%~dp0launch.py"
if errorlevel 1 (
    echo Startup failed. See data\logs\server-startup.log and the message above.
    pause
    exit /b 1
)
exit /b 0
