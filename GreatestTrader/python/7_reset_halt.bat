@echo off
REM Clear a drawdown HALT and restart the drawdown record (same as the EA input InpResetHalt=true):
REM runs `python main.py reset-halt --rebase-peak`. The halt is cleared AND the peak equity is reset to the
REM current account equity, so the drawdown restarts at 0%. A reset without --rebase-peak would only clear
REM the flag, and the engine would set the halt again at once while the drawdown is still at or above
REM risk.max_drawdown_halt - which is normally the case right after a max-DD halt.
REM The rest of the state is kept (deposits/withdrawals, clock, account). The monthly hard stop is NOT
REM cleared by a reset: it ends with the month.
REM The request is stored in the state: the engine applies it at its next start, right after it has
REM checked the deposits/withdrawals made while it was stopped (start 3_run_live.bat afterwards).
REM Stop the live engine first (close the 3_run_live window). Check the account before resuming!
cd /d %~dp0
call .venv\Scripts\activate.bat
echo This clears the drawdown HALT of the LIVE state AND resets the peak equity to the current account
echo equity: the drawdown restarts at 0%%. The engine applies it at its next start, after it has checked
echo the deposits/withdrawals. The monthly hard stop is not cleared (it ends with the month).
echo Stop the live engine first (close the 3_run_live window).
set /p ANSWER=Clear the halt and reset the peak equity to the current equity now? (y/n)
if /i not "%ANSWER%"=="y" goto end
python main.py reset-halt --rebase-peak -c config\config.yaml
:end
pause
