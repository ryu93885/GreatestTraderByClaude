@echo off
REM Run the engine and SEND REAL ORDERS. Restarts it automatically after a crash (exit code 3 = restart requested).
REM Exit code 10 = stopped on purpose (Ctrl+C, another instance already running, unsupported account): no restart.
REM Every start / exit is written to logs\restarts.log. Close this window to stop.
cd /d %~dp0
call .venv\Scripts\activate.bat
if not exist logs mkdir logs
:loop
echo %date% %time% start>>logs\restarts.log
python main.py live -c config\config.yaml --live-orders
set RC=%errorlevel%
echo %date% %time% exit %RC%>>logs\restarts.log
if "%RC%"=="10" goto end
echo Engine stopped (exit code %RC%). Restarting in 30 seconds... (close this window to stop)
REM ping instead of timeout: works without a console (Task Scheduler) and cannot be skipped by a key press
ping -n 31 127.0.0.1 >nul
goto loop
:end
echo Engine stopped on purpose (exit code 10): not restarting. See logs\live_*.log
pause
