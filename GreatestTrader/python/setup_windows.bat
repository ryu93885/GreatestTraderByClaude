@echo off
REM GreatestTrader - first time setup (creates .venv and installs packages)
cd /d %~dp0
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 -m venv .venv
) else (
  python -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
echo.
echo Setup finished. Next: edit config\config.yaml and run 1_check.bat
pause
