"""Operations fixes of review round 2: reset-halt wrapper, webhook User-Agent, config read retry and type checks,
backtest clock warning."""
from pathlib import Path

PY = Path(__file__).resolve().parents[1]


def test_reset_halt_wrapper_rebases_the_peak():
    """D8: the documented recovery after a max-DD halt must resume trading, like the EA's InpResetHalt (always
    rebases): without --rebase-peak the halt is set again at once while DD >= max_drawdown_halt."""
    bat = (PY / "7_reset_halt.bat").read_bytes().decode("ascii")
    assert "\r\n" in bat and "\n" not in bat.replace("\r\n", "")  # CRLF only, for cmd.exe
    lines = bat.split("\r\n")
    cmd = [ln for ln in lines if ln.startswith("python ")]
    assert cmd == ["python main.py reset-halt --rebase-peak -c config\\config.yaml"]
    echo = " ".join(ln for ln in lines if ln.lower().startswith("echo "))
    assert "resets the peak equity to the current account" in echo and "equity" in echo
    assert "0%%" in echo  # a single % would be eaten by cmd.exe
    assert 'if /i not "%ANSWER%"=="y" goto end' in lines
