@echo off
REM Connect to MT5 and show account / symbols / spreads / server time (no trading)
cd /d %~dp0
call .venv\Scripts\activate.bat
python main.py check -c config\config.yaml
pause
