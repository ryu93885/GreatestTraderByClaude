@echo off
REM Run the engine and SEND REAL ORDERS. Auto-restarts after a crash.
cd /d %~dp0
call .venv\Scripts\activate.bat
:loop
python main.py live -c config\config.yaml --live-orders
echo Engine stopped (exit code %errorlevel%). Restarting in 30 seconds... (close this window to stop)
timeout /t 30
goto loop
