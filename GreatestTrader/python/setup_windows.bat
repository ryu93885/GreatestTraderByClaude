@echo off
REM GreatestTrader - first time setup: creates .venv with Python 3.13 and installs the TESTED package versions
REM (requirements-lock.txt). The pinned packages are tested with Python 3.13 64-bit only (the MetaTrader5
REM package exists for 64-bit Windows Python only; newer Python versions are not tested with these pins).
cd /d %~dp0
py -3.13 -c "import sys" >nul 2>nul
if errorlevel 1 (
  echo.
  echo ERROR: Python 3.13 was not found.
  echo   Install Python 3.13 64-bit from https://www.python.org/downloads/ with the "py launcher" option,
  echo   then run this file again. Other Python versions are not tested with the pinned packages.
  echo.
  pause
  exit /b 1
)
py -3.13 -c "import struct, sys; sys.exit(struct.calcsize('P') != 8)" >nul 2>nul
if errorlevel 1 (
  echo.
  echo ERROR: the Python 3.13 found is 32-bit. The MetaTrader5 package needs 64-bit Python.
  echo   Install Python 3.13 from https://www.python.org/downloads/ with the Windows installer 64-bit,
  echo   then run this file again.
  echo.
  pause
  exit /b 1
)
if exist .venv\Scripts\python.exe (
  .venv\Scripts\python.exe -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 13) else 1)"
  if errorlevel 1 (
    echo The existing .venv uses another Python version: recreating it with Python 3.13.
    rmdir /s /q .venv
  )
)
py -3.13 -m venv .venv
if errorlevel 1 (
  echo.
  echo ERROR: creating the virtual environment .venv failed. Delete the .venv folder and run this file again.
  pause
  exit /b 1
)
if not exist .venv\Scripts\activate.bat (
  echo.
  echo ERROR: .venv\Scripts\activate.bat is missing. Delete the .venv folder and run this file again.
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements-lock.txt
if errorlevel 1 (
  echo.
  echo ERROR: installing the packages failed. Check the internet connection and run this file again.
  pause
  exit /b 1
)
REM optional: charts for the backtest report and the test suite
python -m pip install -r requirements-dev.txt
python -c "import sys, numpy, pandas, yaml; print('Python', sys.version.split()[0], 'numpy', numpy.__version__, 'pandas', pandas.__version__, 'PyYAML', yaml.__version__)"
echo.
echo Setup finished. Next: edit config\config.yaml and run 1_check.bat
pause
