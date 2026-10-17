"""Operations fixes of review round 2: reset-halt wrapper, webhook User-Agent, config read retry and type checks,
backtest clock warning."""
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

import greatest_trader.config as config_mod
import main
from greatest_trader import __version__
from greatest_trader.config import NotifyConfig
from greatest_trader.notify import USER_AGENT, Notifier
from greatest_trader.risk import RiskState

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


def test_every_webhook_and_ping_request_sends_an_explicit_user_agent():
    """py-clock-ops#2: urllib's default 'Python-urllib/3.x' is refused by Discord / Cloudflare (HTTP 403)."""
    seen = []

    class H(BaseHTTPRequestHandler):
        def _ok(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                self.rfile.read(n)
            seen.append((self.command, self.path, self.headers.get("User-Agent")))
            self.send_response(200)
            self.end_headers()

        do_GET = do_POST = _ok

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        n = Notifier(NotifyConfig(discord_webhook=f"{base}/discord", slack_webhook=f"{base}/slack",
                                  healthcheck_url=f"{base}/ping"), background=False)
        n.send("HALT: test")
        n.heartbeat()
    finally:
        srv.shutdown()
    assert USER_AGENT == f"GreatestTrader/{__version__} (+https://github.com/ryu93885/GreatestTraderByClaude)"
    assert sorted(seen) == [("GET", "/ping", USER_AGENT), ("POST", "/discord", USER_AGENT),
                            ("POST", "/slack", USER_AGENT)]
    assert n.failed_total == 0


# ------------------------------------------------------------------ config read: exit 10 only for real config errors
@pytest.fixture(autouse=True)
def restore_logging():
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    for h in root.handlers:
        if h not in handlers:
            h.close()
    root.handlers[:] = handlers
    root.setLevel(level)


def write_cfg(tmp_path, extra=""):
    p = tmp_path / "config.yaml"
    p.write_text(f"preset: stable5\nengine:\n  state_file: {tmp_path / 'state' / 'state.json'}\n"
                 f"  log_dir: {tmp_path / 'logs'}\n  journal_dir: {tmp_path / 'journal'}\n"
                 f"backtest:\n  output_dir: {tmp_path / 'out'}\n" + extra, encoding="utf-8")
    return p


def lock_config(monkeypatch, path, fails, exc=None):
    """Make open() of `path` in the config module fail `fails` times (PermissionError like a file held by a backup
    / sync tool on Windows), then work again. Returns the list of attempts."""
    real_open = open
    tries = []

    def fake_open(file, *a, **k):
        if Path(file) == Path(path):
            tries.append(file)
            if len(tries) <= fails:
                raise exc or PermissionError(13, "The process cannot access the file because it is being used by "
                                                 "another process", str(file))
        return real_open(file, *a, **k)

    monkeypatch.setattr(config_mod, "open", fake_open, raising=False)
    monkeypatch.setattr(main.time, "sleep", lambda s: None)
    return tries


def test_a_briefly_locked_config_file_is_read_again(tmp_path, monkeypatch, capsys):
    """ops-backtest#0: a PermissionError for a moment (backup / sync tool) is retried, not CONFIG ERROR / exit 10."""
    cfg = write_cfg(tmp_path)
    RiskState().save(tmp_path / "state" / "state.json")
    tries = lock_config(monkeypatch, cfg, fails=2)
    assert main.main(["reset-halt", "-c", str(cfg)]) == 0
    assert len(tries) == 3 and "CONFIG ERROR" not in capsys.readouterr().out
    assert RiskState.load(tmp_path / "state" / "state.json").reset_request == "clear"


def test_a_config_file_that_stays_locked_exits_1_so_the_wrapper_restarts(tmp_path, monkeypatch, capsys):
    cfg = write_cfg(tmp_path)
    tries = lock_config(monkeypatch, cfg, fails=99)
    assert main.main(["live", "-c", str(cfg), "--live-orders"]) == 1  # 3_run_live.bat restarts after 30 s
    out = capsys.readouterr().out
    assert len(tries) == main.CONFIG_READ_RETRIES + 1 == 6
    assert "CONFIG ERROR" not in out and "cannot read the config file" in out and "Exit code 1" in out


def test_real_config_errors_still_exit_10(tmp_path, monkeypatch, capsys):
    assert main.main(["live", "-c", str(tmp_path / "missing.yaml"), "--live-orders"]) == 10
    assert main.main(["live", "-c", str(tmp_path), "--live-orders"]) == 10  # a folder (IsADirectoryError)
    assert "is a folder" in capsys.readouterr().out
    bad = tmp_path / "cp932.yaml"
    bad.write_bytes("# 設定\nengine:\n  dry_run: true\n".encode("cp932"))
    assert main.main(["live", "-c", str(bad), "--live-orders"]) == 10
    assert "not UTF-8" in capsys.readouterr().out
    # Windows: opening a folder raises PermissionError, which must not loop restarting
    lock_config(monkeypatch, tmp_path, fails=99,
                exc=PermissionError(13, "Permission denied", str(tmp_path)))
    assert main.main(["live", "-c", str(tmp_path), "--live-orders"]) == 10
    assert "is a folder" in capsys.readouterr().out
