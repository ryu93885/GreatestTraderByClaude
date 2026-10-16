@echo off
REM Clear a drawdown HALT without losing the rest of the state (peak, deposits/withdrawals, clock, account).
REM Stop the live engine first (close the 3_run_live window). Check the account before resuming!
REM Add --rebase-peak below to also set the peak equity to the current equity (drawdown restarts at 0%).
cd /d %~dp0
call .venv\Scripts\activate.bat
set /p ANSWER=Clear the drawdown halt of the LIVE state now? (y/n)
if /i not "%ANSWER%"=="y" goto end
python main.py reset-halt -c config\config.yaml
:end
pause
