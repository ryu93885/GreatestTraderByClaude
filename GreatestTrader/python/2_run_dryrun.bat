@echo off
REM Run the engine WITHOUT sending orders (signals are only logged)
cd /d %~dp0
call .venv\Scripts\activate.bat
python main.py live -c config\config.yaml --dry-run
pause
