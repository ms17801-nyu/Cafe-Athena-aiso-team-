@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set PY=..\aiso-agent\.venv\Scripts\python.exe
echo === Review each draft: type y to publish, n to skip ===
%PY% action.py review
echo === Measurement: before/after simulation ===
%PY% measure.py simulate
start "" site\index.html
start "" measure_dashboard.html
pause
