@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set PY=..\aiso-agent\.venv\Scripts\python.exe
%PY% discovery.py run --platforms claude --web --limit 3
pause
