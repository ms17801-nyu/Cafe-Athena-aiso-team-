@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set PY=..\aiso-agent\.venv\Scripts\python.exe
echo ============================================
echo   LIVE DEMO: human approval before publishing
echo ============================================
rem Un-publish one page so it comes back for review
%PY% action.py undo happy-hour.md >nul
%PY% action.py review
echo.
echo Last entries in the approval log:
powershell -NoProfile -Command "Get-Content actions_log.csv -Tail 3"
start "" site\index.html
pause
