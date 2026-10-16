@echo off
REM Run the engine and SEND REAL ORDERS. Restarts it automatically after a crash (exit code 3 = restart requested).
REM Exit code 10 = stopped on purpose or by a problem a restart cannot fix (Ctrl+C, another instance already
REM running, unsupported account, error in config.yaml, MetaTrader5 package missing): no restart.
REM Every start / exit is written to logs\restarts.log. Close this window to stop.
cd /d %~dp0
call .venv\Scripts\activate.bat
if not exist logs mkdir logs
:loop
>>logs\restarts.log echo %date% %time% start
python main.py live -c config\config.yaml --live-orders
set RC=%errorlevel%
REM the redirection comes first: at the end of the line a one-digit exit code would be read as a handle number
>>logs\restarts.log echo %date% %time% exit %RC%
if "%RC%"=="10" goto end
echo Engine stopped (exit code %RC%). Restarting in 30 seconds... (close this window to stop)
REM ping instead of timeout: works without a console (Task Scheduler) and cannot be skipped by a key press
ping -n 31 127.0.0.1 >nul
goto loop
:end
echo Engine stopped (exit code 10: stopped on purpose or a problem a restart cannot fix): not restarting. See the messages above and logs\live_*.log
pause
