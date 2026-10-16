@echo off
REM Live performance report from the MT5 account history (monthly returns without deposits/withdrawals,
REM P/L by strategy, rolling 3/6/12-month returns vs the backtest, R-based edge monitor).
REM Output: backtest_output\live_report\
cd /d %~dp0
call .venv\Scripts\activate.bat
python main.py report -c config\config.yaml
pause
