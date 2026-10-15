@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
call "%~dp0portable_env.bat"
if exist "%WP_PYTHON%" goto install
if defined WP_SETUP_PYTHON (
    "%WP_SETUP_PYTHON%" -m venv "%~dp0.venv"
) else (
    py -3.12 -m venv "%~dp0.venv"
)
if errorlevel 1 goto failed
:install
"%WP_PYTHON%" -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 goto failed
echo Dependencies ready. Run start.bat, then configure a model or API in Settings.
pause
exit /b 0
:failed
echo Setup failed. Check Python 3.12 and network access. See the included guide.
pause
exit /b 1
