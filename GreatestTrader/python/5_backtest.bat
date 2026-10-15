@echo off
REM Python backtest on the CSV files in the data folder
cd /d %~dp0
call .venv\Scripts\activate.bat
python main.py backtest -c config\config.yaml
pause
