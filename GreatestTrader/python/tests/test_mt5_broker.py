"""MT5Broker against the fake MetaTrader5 module (tests/fake_mt5.py): fail-closed reads, lost order replies,
retry policy, reconnect after a terminal restart, spec refresh, cash flows and deal history."""
import calendar
import logging
from datetime import datetime, timedelta

import pytest

import fake_mt5
from fake_mt5 import S
from fakes import synthetic_m15
from greatest_trader.broker import mt5_broker
from greatest_trader.broker.mt5_broker import SPEC_TTL, MT5Broker
from greatest_trader.config import Config
from greatest_trader.engine import FatalError, LiveEngine
from greatest_trader.notify import Notifier
from greatest_trader.ops import EXIT_RESTART
from greatest_trader.strategies import ModifyRequest
from greatest_trader.timeutil import ServerClock

NY7 = ServerClock("ny7")


class Clock:
    """Controllable time.monotonic / time.sleep for the broker module."""

    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += s


def setup_symbol(name, df):
    import numpy as np
    S.specs[name] = dict(digits=3, point=0.001, volume_min=0.01, volume_max=100.0, volume_step=0.01,
                         trade_stops_level=0, filling_mode=1)
    arr = np.zeros(len(df), dtype=[("time", "i8"), ("open", "f8"), ("high", "f8"), ("low", "f8"), ("close", "f8"),
                                   ("tick_volume", "i8"), ("spread", "i4"), ("real_volume", "i8")])
    arr["time"] = (df.index.astype("int64") // 10**9).to_numpy()
    for c in ("open", "high", "low", "close"):
        arr[c] = df[c].to_numpy()
    arr["spread"] = 10
    S.rates[(name, fake_mt5.TIMEFRAME_M15)] = arr


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(mt5_broker.time, "monotonic", c.monotonic)
    monkeypatch.setattr(mt5_broker.time, "sleep", c.sleep)
    return c


@pytest.fixture
def cfg(monkeypatch, tmp_path):
    fake_mt5.reset()
    monkeypatch.setattr(mt5_broker, "mt5", fake_mt5)
    setup_symbol("EURJPY", synthetic_m15(datetime(2021, 3, 1), 5, 130.0, 0.01))
    S.now = calendar.timegm(datetime(2021, 3, 3, 10, 0, 3).timetuple())
    S.ticks["EURJPY"] = dict(time=S.now, bid=130.0, ask=130.01)
    c = Config()
    c.night_reversion.symbols = ["EURJPY"]
    c.euro_session.enabled = False
    c.engine.dry_run = False
    c.broker.server_time_mode = "ny7"
    c.engine.state_file = str(tmp_path / "state.json")
    c.engine.journal_dir = str(tmp_path / "journal")
    c.risk.blackout_start = c.risk.blackout_end = ""
    return c


@pytest.fixture
def b(cfg, clock):
    br = MT5Broker(cfg)
    br.connect()
    return br


def test_positions_error_is_not_an_empty_book(b):
    r = b.open_position("EURJPY", 1, 0.1, 129.5, 130.5, 7101, "GT_NR")
    assert r.ok and len(b.positions({7101: "NR"})) == 1
    S.positions_get_none = True
    with pytest.raises(RuntimeError):
        b.positions({7101: "NR"})  # an error is never "flat"
    res = b.close_position(r.ticket, "EURJPY")
    assert not res.ok and "not confirmed" in res.message and len(S.positions) == 1
    assert not b.open_position("EURJPY", 1, 0.1, 129.5, 130.5, 7101, "GT_NR").ok  # cannot check -> not sent
    S.positions_get_none = False
    assert b.close_position(r.ticket, "EURJPY").ok and S.positions == []
    assert b.close_position(r.ticket, "EURJPY").message == "already closed"  # () really means closed


def test_lost_order_reply_does_not_double_the_position(b):
    S.order_send_none_once = True  # executed on the server, reply lost
    r = b.open_position("EURJPY", 1, 0.1, 129.5, 130.5, 7101, "GT_NR")
    assert r.ok and "reply was lost" in r.message and len(S.positions) == 1
    assert r.ticket == S.positions[0]["ticket"]


def test_lost_reply_without_execution_is_sent_again(b):
    S.order_send_none_no_exec = True  # reply lost and nothing executed: proven by the position check
    r = b.open_position("EURJPY", 1, 0.1, 129.5, 130.5, 7101, "GT_NR")
    assert r.ok and len(S.positions) == 1 and S.calls["order_send"] == 2


def test_retry_only_on_retcodes_that_prove_non_execution(b):
    S.retcodes = [10004]  # requote: nothing executed -> sent again
    assert b.open_position("EURJPY", 1, 0.1, 129.5, 130.5, 7101, "GT_NR").ok and S.calls["order_send"] == 2
    S.positions.clear()
    S.calls["order_send"] = 0
    S.retcodes = [10019]  # no money: not retried
    r = b.open_position("EURJPY", 1, 0.1, 129.5, 130.5, 7101, "GT_NR")
    assert not r.ok and "10019" in r.message and S.calls["order_send"] == 1 and S.positions == []


def test_lost_close_reply_is_resolved_by_lookup(b):
    r = b.open_position("EURJPY", -1, 0.1, 130.5, 129.5, 7101, "GT_NR")
    S.order_send_none_once = True
    res = b.close_position(r.ticket, "EURJPY", "time stop")
    assert res.ok and "reply was lost" in res.message and S.positions == [] and S.calls["order_send"] == 2


def test_reconnect_after_terminal_restart_with_backoff(b, clock):
    assert b.health() == ""
    b.symbol_spec("EURJPY")
    fake_mt5.terminal_restart()  # LiveUpdate / crash: the old IPC session is dead
    assert b.health() == "" and S.init_count == 2  # re-initialised by itself
    assert b._specs == {} and b._tick_t == {}  # caches dropped on reconnect
    S.terminal_alive = False
    fake_mt5.terminal_restart()
    with pytest.raises(RuntimeError, match="reconnect failed"):  # a failure: counts toward the exit-3 restart
        b.health()
    assert S.init_count == 3
    with pytest.raises(RuntimeError, match="reconnecting"):
        b.health()
    assert S.init_count == 3  # back-off: no new attempt yet
    clock.t += 6
    with pytest.raises(RuntimeError, match="reconnect failed"):
        b.health()
    assert S.init_count == 4  # next attempt after 5 s, then 10 s
    clock.t += 6
    with pytest.raises(RuntimeError):
        b.health()
    assert S.init_count == 4
    S.terminal_alive = True
    clock.t += 10
    assert b.health() == "" and S.init_count == 5
    S.connected = False
    h = b.health()
    assert "not connected" in h and not h.orders_only  # the whole loop pauses
    S.connected, S.trade_allowed = True, False
    h = b.health()
    assert "Algo Trading" in h and h.orders_only  # D14: structured result, the guards keep running
    S.trade_allowed, S.trade_expert = True, False
    h = b.health()
    assert "expert" in h and h.orders_only
    S.trade_expert, S.account_trade_allowed = True, False
    assert b.health().orders_only
    S.account_trade_allowed = True
    assert b.health() == "" and not b.health().orders_only


def test_spec_refresh_logs_changes_and_market_watch_reselect(b, clock, caplog):
    assert b.symbol_spec("EURJPY").volume_step == 0.01
    S.specs["EURJPY"]["volume_step"] = 0.1
    S.specs["EURJPY"]["trade_stops_level"] = 30
    assert b.symbol_spec("EURJPY").volume_step == 0.01  # cached
    clock.t += SPEC_TTL + 1
    with caplog.at_level(logging.WARNING):
        spec = b.symbol_spec("EURJPY")
    assert spec.volume_step == 0.1 and spec.stops_level_points == 30
    assert any("volume_step 0.01 -> 0.1" in r.message for r in caplog.records)
    S.selected.discard("EURJPY")  # hidden from Market Watch
    assert b.tick("EURJPY") is not None and "EURJPY" in S.selected


def test_server_time_follows_the_latest_ticks(b):
    b.tick("EURJPY")
    t0 = b.server_time()
    S.ticks["EURJPY"]["time"] = S.now - 3600  # the broker's clock went back one hour
    b.tick("EURJPY")
    assert b.server_time() == t0.replace(hour=t0.hour - 1)  # not frozen at the old maximum


def test_cash_flows_and_closed_trade_from_history(b, clock):
    S.deals.append(dict(ticket=1, time=S.now - 86400, time_msc=0, type=2, entry=0, magic=0, position_id=0, symbol="",
                        volume=0, price=0, profit=10_000.0, commission=0, swap=0, fee=0, reason=0, comment="deposit"))
    flows = b.cash_flows()
    assert [(f.ticket, f.amount, f.type) for f in flows] == [(1, 10_000.0, 2)]
    S.deals.append(dict(ticket=2, time=S.now, time_msc=0, type=2, entry=0, magic=0, position_id=0, symbol="",
                        volume=0, price=0, profit=-500.0, commission=0, swap=0, fee=0, reason=0, comment="withdrawal"))
    S.deals.append(dict(ticket=3, time=S.now + 5, time_msc=0, type=1, entry=1, magic=7101, position_id=9,
                        symbol="EURJPY", volume=1, price=130.0, profit=40.0, commission=-3.0, swap=-1.0, fee=0,
                        reason=5, comment=""))
    S.balance += -500 + 36
    scan = b.cash_scan(after=1, since=mt5_broker._ts(S.now - 86400))  # every deal type newer than the reference
    assert [(d.ticket, d.effect, d.cash) for d in scan.deals] == [(2, -500.0, True), (3, 36.0, False)]
    assert [f.ticket for f in scan.flows] == [2] and scan.newest_deal == 3
    assert scan.money == scan.scan_money == S.balance and scan.equity == S.equity
    assert b.cash_scan(after=3, since=mt5_broker._ts(S.now)).deals == []  # nothing newer: no deals, no error
    S.filter_deal_times = True  # like MT5: only the window is returned (from the reference deal - 1 day)
    S.deals.append(dict(ticket=4, time=S.now - 40 * 86400, time_msc=0, type=2, entry=0, magic=0, position_id=0,
                        symbol="", volume=0, price=0, profit=-1.0, commission=0, swap=0, fee=0, reason=0, comment=""))
    b.tick("EURJPY")  # the server time: the window's 'now'
    assert [d.ticket for d in b.cash_scan(after=1, since=mt5_broker._ts(S.now - 39 * 86400)).deals] == [4, 2, 3]
    assert [d.ticket for d in b.cash_scan(after=1, since=mt5_broker._ts(S.now)).deals] == [2, 3]  # last 2 days
    assert [f.ticket for f in b.cash_flows()] == [2]  # 30 days
    S.filter_deal_times = False
    hc = b.history_check()
    assert hc.newest_deal == 4 and hc.money == S.balance and hc.newest_time == mt5_broker._ts(S.now - 40 * 86400)
    r = b.open_position("EURJPY", 1, 0.1, 129.5, 130.5, 7101, "GT_NR")
    assert b.closed_trade(r.ticket) is None  # still open
    S.ticks["EURJPY"].update(bid=130.10, ask=130.11)
    b.close_position(r.ticket, "EURJPY")
    ct = b.closed_trade(r.ticket)
    assert ct.reason == "expert" and ct.pnl == pytest.approx((130.10 - 130.01) * 100000 * 0.1 - 6.0)


def test_transient_tp_modify_error_keeps_the_position(b, cfg):
    """retcode 10024 (too many requests) on the TP update: the position is kept with its SL (it used to be closed
    at market); the next bar sends the TP again."""
    eng = LiveEngine(cfg, b, Notifier(cfg.notify))
    b.open_position("EURJPY", 1, 0.1, 129.5, 130.3, 7101, "GT_NR")
    pos = b.positions(eng.magics)[0]
    S.retcodes = [10024]
    eng._apply(ModifyRequest(pos.ticket, "EURJPY", 130.25, "TP -> moving average"), pos)
    assert len(S.positions) == 1 and S.positions[0]["tp"] == 130.3
    eng._apply(ModifyRequest(pos.ticket, "EURJPY", 130.25, "TP -> moving average"), pos)  # next bar: works
    assert len(S.positions) == 1 and S.calls["order_send"] == 3


def test_weekend_restart_with_a_late_symbol_adopts_no_clock(cfg, clock, tmp_path):
    """Saturday 08:00 UTC restart in auto mode: EURUSD's (Friday) tick is unreadable in the first loop and readable
    in the next. The maximum of the latest ticks 'advanced', and a fixed -8 h clock was adopted, saved and used
    (a fake Saturday market day)."""
    import numpy as np
    S.specs["EURUSD"] = dict(digits=5, point=0.00001, volume_min=0.01, volume_max=100.0, volume_step=0.01,
                             trade_stops_level=0, filling_mode=1)
    S.rates[("EURUSD", fake_mt5.TIMEFRAME_M15)] = np.zeros(0)
    ep = lambda dt: calendar.timegm(dt.timetuple())  # noqa: E731
    S.now = ep(datetime(2025, 1, 17, 23, 59, 58))
    S.ticks["EURJPY"] = dict(time=ep(datetime(2025, 1, 17, 23, 59, 30)), bid=160.0, ask=160.02)
    S.ticks.pop("EURUSD", None)
    cfg.night_reversion.symbols = ["EURJPY", "EURUSD"]
    cfg.broker.server_time_mode = "auto"
    wall = {"utc": datetime(2025, 1, 18, 8, 0, 0)}
    eng = LiveEngine(cfg, MT5Broker(cfg), Notifier(cfg.notify), utcnow=lambda: wall["utc"])
    eng.start()
    for k in range(6):
        if k == 1:
            S.ticks["EURUSD"] = dict(time=ep(datetime(2025, 1, 17, 23, 59, 58)), bid=1.03, ask=1.0301)
        eng.step()
        wall["utc"] += timedelta(seconds=5)
    assert eng.state.clock_mode == "" and not eng.clock_verified and eng.entry_block()
    assert eng.state.day in ("", "2025-01-17") and not any("server clock" in m for m in eng.notifier.recent)


def test_netting_account_is_detected(b, cfg):
    assert b.is_hedging()
    S.margin_mode = 0  # ACCOUNT_MARGIN_MODE_RETAIL_NETTING
    assert not b.is_hedging()
    with pytest.raises(FatalError, match="NETTING"):
        LiveEngine(cfg, b, Notifier(cfg.notify)).start()


def test_engine_recovers_from_terminal_restart(cfg, clock, monkeypatch):
    br = MT5Broker(cfg)
    eng = LiveEngine(cfg, br, Notifier(cfg.notify), utcnow=lambda: NY7.server_to_utc(mt5_broker._ts(S.now)))
    ok, loops = [0], [0]
    real = eng.step

    def counting_step():
        real()
        ok[0] += 1

    def fake_sleep(s):
        loops[0] += 1
        S.now += 5
        S.ticks["EURJPY"]["time"] = S.now
        if loops[0] == 10:
            fake_mt5.terminal_restart()
        if loops[0] >= 40:
            raise KeyboardInterrupt

    eng.step = counting_step
    monkeypatch.setattr("greatest_trader.engine.time.sleep", fake_sleep)
    assert eng.run_forever() == 10  # Ctrl+C = clean stop
    assert S.init_count == 2 and ok[0] >= 39  # re-initialised once, every loop worked


def test_engine_exits_for_restart_when_the_terminal_stays_down(cfg, monkeypatch):
    br = MT5Broker(cfg)
    eng = LiveEngine(cfg, br, Notifier(cfg.notify), utcnow=lambda: NY7.server_to_utc(mt5_broker._ts(S.now)))
    t, loops = [0.0], [0]
    monkeypatch.setattr("greatest_trader.engine.time.monotonic", lambda: t[0])

    def fake_sleep(s):
        t[0] += 5
        loops[0] += 1
        S.now += 5
        S.ticks["EURJPY"]["time"] = S.now
        if loops[0] == 3:  # the terminal is closed and does not come back
            S.terminal_alive = False
            fake_mt5.terminal_restart()

    monkeypatch.setattr("greatest_trader.engine.time.sleep", fake_sleep)
    assert eng.run_forever() == EXIT_RESTART
    assert 1800 <= t[0] <= 1900 and any("CRITICAL" in m for m in eng.notifier.recent)
    assert 5 <= S.init_count < 120  # re-initialised with back-off, not on every 5-second loop


def test_engine_exits_for_restart_after_30_minutes_of_failure(cfg, monkeypatch):
    br = MT5Broker(cfg)
    eng = LiveEngine(cfg, br, Notifier(cfg.notify))
    t = [0.0]
    monkeypatch.setattr("greatest_trader.engine.time.monotonic", lambda: t[0])

    def fake_sleep(s):
        t[0] += 5

    monkeypatch.setattr("greatest_trader.engine.time.sleep", fake_sleep)
    S.terminal_alive = False  # initialize() keeps failing: start() never succeeds
    assert eng.run_forever() == EXIT_RESTART
    assert t[0] >= 1800 and any("CRITICAL" in m for m in eng.notifier.recent)
    assert sum(1 for m in eng.notifier.recent if m.startswith("ERROR")) <= 2  # rate limited (every 30 min)
