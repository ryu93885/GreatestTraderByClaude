"""Operations fixes of review round 2: reset-halt wrapper, webhook User-Agent, config read retry and type checks,
backtest clock warning."""
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

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


# ------------------------------------------------------------------ config: type checks of list / text keys
def load(path):
    return main._load(SimpleNamespace(config=str(path), preset=None))


@pytest.mark.parametrize("yaml_text, words", [
    ("night_reversion:\n  symbols: GBPCAD\n", "night_reversion.symbols must be a list"),
    ("night_reversion:\n  symbols: []\n", "night_reversion.symbols is empty"),
    ("night_reversion:\n  symbols:\n", "night_reversion.symbols must be a list"),
    ("night_reversion:\n  symbols: [GBPCAD, 7]\n", "6 letters"),
    ("night_reversion:\n  symbols: [GBP]\n", "6 letters"),
    ("night_reversion:\n  symbols: [GBPCAD EURCAD]\n", "6 letters"),
    ("night_reversion:\n  entry_hours: 0\n", "night_reversion.entry_hours must be a list"),
    ("night_reversion:\n  entry_hours: [0, '1']\n", "night_reversion.entry_hours must be a list"),
    ("night_reversion:\n  entry_hours: [24]\n", "0..23"),
    ("night_reversion:\n  entry_hours: [1.0]\n", "whole numbers"),
    ("euro_session:\n  symbol: [EURUSD]\n", "euro_session.symbol"),
    ("euro_session:\n  symbol: 123456\n", "euro_session.symbol"),
    ("euro_session:\n  weekdays: 0-4\n", "euro_session.weekdays must be a list"),
    ("euro_session:\n  weekdays: [0, 7]\n", "0..6"),
    ("euro_session:\n  weekdays: [true]\n", "euro_session.weekdays"),
    ("broker:\n  server_time_mode: fixed\n  server_gmt_offset: '+2'\n", "broker.server_gmt_offset must be a number"),
    ("broker:\n  server_gmt_offset:\n", "broker.server_gmt_offset must be a number"),
    ("broker:\n  server_gmt_offset: 15\n", "between -12 and 14"),
    ("broker:\n  symbol_suffix: 1\n", "broker.symbol_suffix must be text"),
    ("broker:\n  symbol_map: [EURUSD]\n", "broker.symbol_map must be a mapping"),
    ("broker:\n  symbol_map: {EURUSD: 5}\n", "broker.symbol_map must be a mapping"),
    ("broker:\n  symbol_map: {EUR: EURUSD}\n", "6 letters"),
])
def test_wrong_types_are_config_errors(tmp_path, capsys, yaml_text, words):
    """ops-backtest#3: `symbols: GBPCAD` used to pass and be split into G, B, P, C, A, D in live (and crash
    backtest / download / check with a TypeError traceback): every such mistake is now CONFIG ERROR (exit 10)."""
    cfg = write_cfg(tmp_path, yaml_text)
    with pytest.raises(config_mod.ConfigError, match=re.escape(words)):
        load(cfg)
    for cmd in (["live", "--live-orders"], ["backtest"], ["download"], ["check"]):
        assert main.main([cmd[0], "-c", str(cfg), *cmd[1:]]) == 10, cmd
        out = capsys.readouterr().out
        assert "CONFIG ERROR" in out and words in out, (cmd, out)


def test_valid_lists_and_names_are_normalized(tmp_path):
    cfg = write_cfg(tmp_path, "night_reversion:\n  symbols: [' gbpcad ', EURcad, EURUSD.raw]\n  entry_hours: [0]\n"
                              "euro_session:\n  symbol: eurusd\n  weekdays: [0, 2, 4]\n"
                              "broker:\n  server_time_mode: fixed\n  server_gmt_offset: 3\n  symbol_suffix:\n"
                              "  symbol_map: {eurusd: ' EURUSDmicro '}\n")
    c = load(cfg)
    assert c.night_reversion.symbols == ["GBPCAD", "EURCAD", "EURUSD.raw"]
    assert c.night_reversion.entry_hours == [0] and c.euro_session.weekdays == [0, 2, 4]
    assert c.euro_session.symbol == "EURUSD" and c.broker.server_gmt_offset == 3
    assert c.broker.symbol_suffix == "" and c.broker_symbol("EURUSD") == "EURUSDmicro"
    assert c.broker_symbol("GBPCAD") == "GBPCAD"
    # one pair is fine as a list; an empty list is fine when Night Reversion is off; tuples become lists
    write_cfg(tmp_path, "night_reversion:\n  symbols: [GBPCAD]\n")
    assert load(cfg).night_reversion.symbols == ["GBPCAD"]
    off = config_mod.Config()
    off.night_reversion.enabled = False
    off.night_reversion.symbols = []
    off.euro_session.weekdays = (0, 1)
    off.validate()
    assert off.euro_session.weekdays == [0, 1]


def test_every_bundled_preset_and_the_shipped_config_still_validate():
    for name in ["", *config_mod.available_presets()]:
        c = config_mod.load_config(PY / "config" / "config.yaml", preset=name or None)
        assert c.night_reversion.symbols == config_mod.DEFAULT_NR_SYMBOLS or c.preset, name
        assert all(len(s) == 6 and s.isupper() for s in c.night_reversion.symbols)
        assert isinstance(c.broker.server_gmt_offset, (int, float))
