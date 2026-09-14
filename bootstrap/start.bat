@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0\.."
set "PYTHONPATH=%CD%"
echo Starting the deployment wizard in this window.
echo Close this window to stop. No background business process is started.
where py >nul 2>&1
if %ERRORLEVEL%==0 (
  py -3 -m bootstrap %*
) else (
  python -m bootstrap %*
)
if errorlevel 1 (
  echo.
  echo Start failed. This window stays open so the error is visible.
  pause
  exit /b 1
)
exit /b 0
