"""Shipped config.yaml, restart wrapper, setup script and requirement files."""
import re
from pathlib import Path

from greatest_trader.config import load_config

PY = Path(__file__).resolve().parents[1]


def test_shipped_config_has_the_long_term_keys():
    cfg = load_config(PY / "config" / "config.yaml")
    assert cfg.preset == "stable5" and cfg.broker.server_time_mode == "auto"
    assert cfg.risk.max_lots_per_order == 20 and cfg.risk.max_risk_amount == 0 and cfg.risk.max_drawdown_halt == 0.2
    assert cfg.monitor.dd_alert == 0.15 and cfg.monitor.edge_window_trades == 250 and cfg.notify.healthcheck_url == ""
    text = (PY / "config" / "config.yaml").read_text(encoding="utf-8")
    for key in ("max_lots_per_order", "max_risk_amount", "healthcheck_url", "monitor:", "reset-halt"):
        assert key in text, key


def test_restart_wrapper_logs_restarts_and_stops_on_exit_code_10():
    bat = (PY / "3_run_live.bat").read_bytes().decode("ascii")
    assert "\r\n" in bat and "\n" not in bat.replace("\r\n", "")  # CRLF only, for cmd.exe
    assert 'if "%RC%"=="10" goto end' in bat
    assert "ping -n 31 127.0.0.1" in bat and "timeout /t" not in bat  # works without a console
    assert "--live-orders" in bat
    # the redirection comes first: in `echo ... exit %RC%>>file` a one-digit exit code (0-9, e.g. 3) followed by
    # >> is read by cmd.exe as a handle number, and exit codes 0-9 never reached restarts.log
    assert ">>logs\\restarts.log echo %date% %time% exit %RC%\r\n" in bat
    assert ">>logs\\restarts.log echo %date% %time% start\r\n" in bat


def test_no_batch_file_redirects_after_a_variable_or_a_digit():
    for p in PY.glob("*.bat"):
        for line in p.read_bytes().decode("ascii").split("\r\n"):
            if line.upper().startswith("REM"):
                continue
            assert not re.search(r"%\w+%>", line), (p.name, line)  # `echo %RC%>>file`: "3>>" is a handle


def test_setup_pins_python_313_and_the_lock_file():
    bat = (PY / "setup_windows.bat").read_bytes().decode("ascii")
    assert "\r\n" in bat and "\n" not in bat.replace("\r\n", "")
    assert "py -3.13 -m venv .venv" in bat and "requirements-lock.txt" in bat and "exit /b 1" in bat
    assert "struct.calcsize('P') != 8" in bat  # MetaTrader5 has 64-bit wheels only
    after_venv = bat.split("py -3.13 -m venv .venv\r\n", 1)[1]
    assert after_venv.startswith("if errorlevel 1 (")  # a failed venv must not install into another Python
    lock = (PY / "requirements-lock.txt").read_text(encoding="ascii")
    pins = dict(re.findall(r"^([A-Za-z0-9_.-]+)==([^\s;]+)", lock, re.M))
    assert pins["MetaTrader5"] == "5.0.6231" and 'MetaTrader5==5.0.6231 ; sys_platform == "win32"' in lock
    assert (pins["numpy"], pins["pandas"], pins["PyYAML"]) == ("2.5.3", "3.0.5", "6.0.3")
    assert 'tzdata==2026.5 ; sys_platform == "win32"' in lock  # pandas needs it on Windows
    dev = (PY / "requirements-dev.txt").read_text(encoding="ascii")
    assert "-r requirements-lock.txt" in dev and "pytest" in dev and "matplotlib" in dev
    assert 'colorama==0.4.6 ; sys_platform == "win32"' in dev  # pytest needs it on Windows
    loose = (PY / "requirements.txt").read_text(encoding="ascii")
    assert "numpy>=1.24,<3" in loose and "pandas>=2.0,<4" in loose
