@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
..\aiso-agent\.venv\Scripts\python.exe build_dashboard.py
start "" ai_visibility_dashboard.html
