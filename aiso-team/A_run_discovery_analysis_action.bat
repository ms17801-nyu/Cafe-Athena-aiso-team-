@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set PY=..\aiso-agent\.venv\Scripts\python.exe
echo === 1. Discovery: 60 queries on Claude with live web search ===
%PY% discovery.py run --platforms claude --web
echo === 2. Analysis: audits + gap diagnosis ===
%PY% analysis.py
echo === 3. Action: generating drafts ===
%PY% action.py generate
%PY% action.py fill
echo.
echo DONE. Next: double-click B_review_and_measure.bat
pause
