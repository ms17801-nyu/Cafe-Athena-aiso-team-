@echo off
cd /d "%~dp0"
if not exist .env copy key_template.txt .env >nul
notepad .env
