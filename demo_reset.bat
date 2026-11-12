@echo off
rem Demo reset: stop the review page, back up + clear local user data, profile back to first-round, restart.
rem Usage: double-click, or  demo_reset.bat [config.json]   (all messages come from platform\demo_reset.py)
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set "CFG=%~1"
if "%CFG%"=="" set "CFG=config.json"
if not exist "%CFG%" (
  echo [demo_reset] config not found: %CFG%
  pause
  exit /b 1
)
set "PY=python"
where python >nul 2>nul || set "PY=py -3"
%PY% -X utf8 platform\demo_reset.py --config "%CFG%" %2 %3 %4
set "ERR=%ERRORLEVEL%"
echo.
pause
exit /b %ERR%
