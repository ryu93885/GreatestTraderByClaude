@echo off
REM Download M15 history (default 5 years) from MT5 into the data folder
cd /d %~dp0
call .venv\Scripts\activate.bat
python main.py download -c config\config.yaml --years 5
pause
