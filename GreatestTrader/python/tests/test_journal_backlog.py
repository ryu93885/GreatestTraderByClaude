"""Journal rows that cannot be written (CSV open in Excel) are kept in journal/pending_rows.json and written in
order once the file is free; one rate-limited alert per incident, one ERROR log line even without a callback
(py-clock-ops#1 / ops-backtest#2)."""
import builtins
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

import greatest_trader.journal as jm
from fakes import FakeBroker, ny7_utc, synthetic_m15
from greatest_trader.broker.base import ClosedTrade
from greatest_trader.config import Config
from greatest_trader.engine import LiveEngine
from greatest_trader.journal import TradeJournal
from greatest_trader.notify import Notifier
from greatest_trader.strategies import PositionView


class Excel:
    """Windows / Excel semantics: other programs may READ the open CSV, but opening it to write or append fails
    with a sharing violation (PermissionError errno 13). Only the CSV files named in `locked` are held."""

    def __init__(self, monkeypatch):
        self.locked: set[str] = set()
        real_open = builtins.open

        def fake_open(file, mode="r", *a, **k):
            if Path(file).name in self.locked and any(c in mode for c in "aw+x"):
                raise PermissionError(13, "Permission denied", str(file))
            return real_open(file, mode, *a, **k)

        monkeypatch.setattr(jm, "open", fake_open, raising=False)


class Clock:
    def __init__(self, monkeypatch):
        self.t = 1000.0
        monkeypatch.setattr(jm.time, "monotonic", lambda: self.t)


def close_row(ticket, r=1.0, when="2026-03-10 01:00:00"):
    return {"event": "close", "time_utc": when, "strategy": "NR", "symbol": "GBPCAD", "ticket": ticket,
            "pnl": r * 100, "risk_amount": 100.0, "R": r, "base_risk": 0.035, "close_reason": "TP"}


def rows(path):
    return pd.read_csv(path)


def errors(caplog):
    return [r for r in caplog.records if r.name == jm.__name__ and r.levelno >= logging.ERROR]


def test_rows_wait_while_the_file_is_locked_and_are_written_in_order(tmp_path, monkeypatch, caplog):
    xl, clock, alerts = Excel(monkeypatch), Clock(monkeypatch), []
    j = TradeJournal(tmp_path, alert=alerts.append)
    trades = tmp_path / "trades_2026.csv"
    assert j.trade(close_row(1))
    xl.locked.add(trades.name)
    with caplog.at_level(logging.DEBUG, logger=jm.__name__):
        assert not j.trade(close_row(2, r=-1.0))  # kept, not lost
        assert not j.trade({"event": "open", "time_utc": "2026-03-10 02:00:00", "ticket": 3})
        assert j.equity({"market_date": "2026-03-10", "close_equity": 10_100})  # another file is not held up
    assert j.pending_count == 2 and len(errors(caplog)) == 1  # one ERROR per incident
    assert "journal file locked (open in Excel?) - 1 rows pending" in errors(caplog)[0].getMessage()
    assert alerts == [alerts[0]] and alerts[0].startswith("journal file locked (open in Excel?) - 1 rows pending")
    saved = json.loads(j.pending_path.read_text(encoding="utf-8"))["rows"]
    assert [(r["file"], r["row"]["ticket"]) for r in saved] == [("trades_2026.csv", "2"), ("trades_2026.csv", "3")]
    assert list(rows(trades).ticket) == [1]
    # the R monitor sees the pending close row already (with numbers, not text)
    c = j.closed_trades()
    assert list(c.ticket) == [1, 2] and list(c.R) == [1.0, -1.0] and c.R.dtype.kind == "f"
    # rate-limited: no second alert within the hour, a reminder after it
    clock.t += 1800
    assert not j.trade(close_row(4)) and len(alerts) == 1
    clock.t += 1801
    assert j.flush_pending() == 3 and len(alerts) == 2 and "- 3 rows pending" in alerts[1]
    # Excel closed: the next row writes the backlog first, in order
    xl.locked.clear()
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=jm.__name__):
        assert j.trade(close_row(5))
    assert list(rows(trades).ticket) == [1, 2, 3, 4, 5] and list(rows(trades).event) == ["close"] * 2 + ["open"] \
        + ["close"] * 2
    assert j.pending_count == 0 and not j.pending_path.exists()
    assert alerts[-1] == "journal files writable again: 3 pending rows written" and len(alerts) == 3
    assert any("writable again" in r.getMessage() for r in caplog.records)
    assert list(j.closed_trades().ticket) == [1, 2, 4, 5]
    # a new incident logs and alerts again at once
    xl.locked.add(trades.name)
    caplog.clear()
    with caplog.at_level(logging.ERROR, logger=jm.__name__):
        assert not j.trade(close_row(6))
    assert len(errors(caplog)) == 1 and len(alerts) == 4


def test_flush_pending_without_a_new_row(tmp_path, monkeypatch):
    xl, alerts = Excel(monkeypatch), []
    j = TradeJournal(tmp_path, alert=alerts.append)
    xl.locked.add("equity_log.csv")
    assert not j.equity({"market_date": "2026-03-10", "close_equity": 10_100})
    assert j.flush_pending() == 1 and len(alerts) == 1
    xl.locked.clear()
    assert j.flush_pending() == 0 and list(rows(tmp_path / "equity_log.csv").market_date) == ["2026-03-10"]
    assert j.flush_pending() == 0 and len(alerts) == 2  # nothing to do: no I/O, no message


def test_the_backlog_survives_a_restart(tmp_path, monkeypatch, caplog):
    xl = Excel(monkeypatch)
    TradeJournal(tmp_path).trade(close_row(1))
    xl.locked.add("trades_2026.csv")
    assert not TradeJournal(tmp_path).trade(close_row(2))
    xl.locked.clear()
    with caplog.at_level(logging.WARNING, logger=jm.__name__):
        j = TradeJournal(tmp_path)  # the restarted engine
    assert j.pending_count == 1 and "1 journal rows pending from an earlier run" in caplog.text
    assert list(j.closed_trades().ticket) == [1, 2]
    assert j.equity({"market_date": "2026-03-10", "close_equity": 1.0})  # any row writes the backlog first
    assert list(rows(tmp_path / "trades_2026.csv").ticket) == [1, 2] and not j.pending_path.exists()


def test_without_a_callback_the_error_is_logged_once_per_incident(tmp_path, monkeypatch, caplog):
    xl, clock = Excel(monkeypatch), Clock(monkeypatch)
    j = TradeJournal(tmp_path)
    xl.locked.add("trades_2026.csv")
    with caplog.at_level(logging.WARNING, logger=jm.__name__):
        for k in range(5):
            j.trade(close_row(k))
        clock.t += 3600
        j.trade(close_row(9))
    assert len(errors(caplog)) == 1 and "rows pending" in errors(caplog)[0].getMessage()
    assert sum("journal file locked" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING) == 1


def test_a_failing_alert_callback_never_stops_the_journal(tmp_path, monkeypatch):
    xl = Excel(monkeypatch)

    def boom(text):
        raise RuntimeError("webhook down")

    j = TradeJournal(tmp_path, alert=boom)
    xl.locked.add("trades_2026.csv")
    assert not j.trade(close_row(1)) and j.pending_count == 1


def test_an_older_header_is_kept_for_pending_rows(tmp_path, monkeypatch):
    old = "market_date,time_utc,close_equity\n2026-03-09,2026-03-09 21:00:05,10000\n"
    (tmp_path / "equity_log.csv").write_text(old, encoding="utf-8")
    xl = Excel(monkeypatch)
    j = TradeJournal(tmp_path)
    xl.locked.add("equity_log.csv")
    assert not j.equity({"market_date": "2026-03-10", "close_equity": 10_100, "balance": 1})
    xl.locked.clear()
    assert j.flush_pending() == 0
    log = rows(tmp_path / "equity_log.csv")
    assert list(log.columns) == ["market_date", "time_utc", "close_equity"] and list(log.close_equity) == [10000, 10100]


def test_a_corrupt_backlog_is_moved_aside(tmp_path, caplog):
    (tmp_path / jm.PENDING_FILE).write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.ERROR, logger=jm.__name__):
        j = TradeJournal(tmp_path)
    assert j.pending_count == 0 and "unreadable" in caplog.text
    assert len(list(tmp_path.glob(jm.PENDING_FILE + ".corrupt-*"))) == 1 and not j.pending_path.exists()
    assert j.trade(close_row(1))


def test_an_unreadable_backlog_is_not_overwritten(tmp_path, monkeypatch):
    """pending_rows.json held for a moment (anti-virus) at start: new rows wait in memory behind its rows."""
    xl = Excel(monkeypatch)
    xl.locked.add("trades_2026.csv")
    assert not TradeJournal(tmp_path).trade(close_row(1))
    xl.locked.clear()
    real_read = Path.read_text
    held = {"on": True}

    def read_text(self, *a, **k):
        if self.name == jm.PENDING_FILE and held["on"]:
            raise PermissionError(13, "held", str(self))
        return real_read(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", read_text)
    j = TradeJournal(tmp_path)
    assert not j.trade(close_row(2)) and j.pending_count == 1  # not written before row 1
    assert json.loads(real_read(j.pending_path))["rows"][0]["row"]["ticket"] == "1"  # untouched
    held["on"] = False
    assert j.trade(close_row(3))
    assert list(rows(tmp_path / "trades_2026.csv").ticket) == [1, 2, 3] and not j.pending_path.exists()


def test_engine_close_row_is_not_lost_while_the_journal_is_open_in_excel(tmp_path, monkeypatch):
    """The review's scenario: a position closes and the market day changes while trades_2025.csv and
    equity_log.csv are open in Excel; Excel is closed a minute later. The engine forgets the ticket (the journal
    owns the row now); the close row and the equity row are written once the files are free."""
    cfg = Config()
    cfg.night_reversion.symbols = ["EURJPY"]
    cfg.euro_session.enabled = False
    cfg.engine.dry_run = False
    cfg.engine.state_file = str(tmp_path / "state.json")
    cfg.engine.journal_dir = str(tmp_path / "journal")
    cfg.broker.server_time_mode = "ny7"
    cfg.risk.blackout_start = cfg.risk.blackout_end = ""
    br = FakeBroker({"EURJPY": synthetic_m15(datetime(2025, 1, 13), 10, 160.0, 0.01)})
    eng = LiveEngine(cfg, br, Notifier(cfg.notify), utcnow=ny7_utc(br))
    t0 = datetime(2025, 1, 21, 23, 50, 0)  # Tuesday 23:50 server time
    br.set_time(t0)
    br.positions_.append(PositionView(4711, "NR", "EURJPY", 1, 0.5, 160.00, t0 - timedelta(minutes=30), 159.50,
                                      160.20))
    eng.journal.trade({"event": "close", "time_utc": "2025-01-20 01:00:00", "ticket": 1, "R": 1.0,
                       "base_risk": 0.01})
    eng.step()
    xl = Excel(monkeypatch)
    xl.locked |= {"trades_2025.csv", "equity_log.csv"}
    br.positions_.clear()
    br.closed[4711] = ClosedTrade(4711, t0 + timedelta(minutes=5), 160.20, 10000.0, "TP")
    for k in range(1, 200):  # 23:50 -> 00:06:35: the close and the day change both while locked
        t = t0 + timedelta(seconds=5 * k)
        br.set_time(t)
        if t >= datetime(2025, 1, 22, 0, 1):
            xl.locked.clear()  # Excel closed at 00:01
        eng.step()
        if k == 120:  # 00:00: the ticket is forgotten, its close row waits in the backlog
            assert "4711" not in eng.state.open_trades and eng.journal.pending_count >= 1
            assert list(eng.journal.closed_trades().ticket) == [1, 4711]
    eng.journal.flush_pending()  # what the next journal row (or a loop that calls it) does
    trades = rows(tmp_path / "journal" / "trades_2025.csv")
    assert list(trades[trades.event == "close"].ticket) == [1, 4711]
    assert len(rows(tmp_path / "journal" / "equity_log.csv")) == 1
    assert eng.journal.pending_count == 0 and not eng.journal.pending_path.exists()


def test_live_command_sends_the_journal_alert_through_the_notifier(tmp_path, monkeypatch):
    import fake_mt5
    import greatest_trader.engine as engine_mod
    import main
    from greatest_trader.broker import mt5_broker

    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    fake_mt5.reset()
    monkeypatch.setattr(mt5_broker, "mt5", fake_mt5)
    seen = {}

    def run_forever(self):
        seen.update(alert=self.journal.alert, notifier=self.notifier)
        return 10

    monkeypatch.setattr(engine_mod.LiveEngine, "run_forever", run_forever)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"engine:\n  state_file: {tmp_path / 'state.json'}\n  log_dir: {tmp_path / 'logs'}\n"
                   f"  journal_dir: {tmp_path / 'journal'}\n", encoding="utf-8")
    try:
        assert main.main(["live", "-c", str(cfg), "--dry-run"]) == 10
    finally:
        for h in root.handlers:
            if h not in handlers:
                h.close()
        root.handlers[:] = handlers
        root.setLevel(level)
    assert seen["alert"] == seen["notifier"].send


def test_constructor_and_api_stay_backward_compatible(tmp_path):
    j = TradeJournal(str(tmp_path))  # the engine's call
    assert j.trade(close_row(1)) is True and j.equity({"market_date": "2026-03-10"}) is True
    assert j.flush_pending() == 0 and j.pending_count == 0 and not j.pending_path.exists()
    with pytest.raises(TypeError):
        TradeJournal()
