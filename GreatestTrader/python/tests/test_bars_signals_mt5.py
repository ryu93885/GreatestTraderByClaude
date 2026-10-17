"""Closed bars by time (D11, parity#3) and deferred entry signals (D9) with the real MT5 adapter and the fake
MetaTrader5 module, whose bars - like MT5's - only exist after a symbol's first tick in them."""
import calendar
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

import fake_mt5
from fake_mt5 import S
from greatest_trader.broker import mt5_broker
from greatest_trader.broker.mt5_broker import MT5Broker
from greatest_trader.config import Config
from greatest_trader.engine import LiveEngine
from greatest_trader.notify import Notifier
from greatest_trader.timeutil import ServerClock

NY7 = ServerClock("ny7")
SPIKE = datetime(2021, 3, 3, 0, 30)  # Wednesday (market time = ny7 server time); the signal bar closes at 00:45
BASE = {"AUDCAD": 0.97, "GBPCAD": 1.75}


def ep(dt):
    return calendar.timegm(dt.timetuple())


def flat_with_spike(base, pip=0.0001):
    """Constant closes (no band break) with a 16-pip range, one -60 pip close on the SPIKE bar (BUY signal)."""
    idx = pd.date_range(datetime(2021, 3, 1), periods=5 * 96, freq="15min")
    df = pd.DataFrame({"open": base, "high": base + 8 * pip, "low": base - 8 * pip, "close": base}, index=idx)
    k = df.index.get_loc(pd.Timestamp(SPIKE))
    df.iloc[k, df.columns.get_loc("close")] = base - 60 * pip
    df.iloc[k, df.columns.get_loc("low")] = base - 62 * pip
    df.iloc[k + 1, df.columns.get_loc("open")] = base - 59 * pip
    df.iloc[k + 1, df.columns.get_loc("low")] = base - 61 * pip
    return df


class Clock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(mt5_broker.time, "monotonic", c.monotonic)  # the time module: the engine's clock too
    monkeypatch.setattr(mt5_broker.time, "sleep", lambda s: None)
    return c


def setup(monkeypatch, tmp_path, syms, delays):
    fake_mt5.reset()
    monkeypatch.setattr(mt5_broker, "mt5", fake_mt5)
    data = {}
    for s in syms:
        df = data[s] = flat_with_spike(BASE[s])
        S.specs[s] = dict(digits=5, point=0.00001, volume_min=0.01, volume_max=100.0, volume_step=0.01,
                          trade_stops_level=0, filling_mode=1)
        arr = np.zeros(len(df), dtype=[("time", "i8"), ("open", "f8"), ("high", "f8"), ("low", "f8"), ("close", "f8"),
                                       ("tick_volume", "i8"), ("spread", "i4"), ("real_volume", "i8")])
        arr["time"] = pd.DatetimeIndex(df.index).as_unit("s").asi8  # epoch seconds whatever the index resolution
        for c in ("open", "high", "low", "close"):
            arr[c] = df[c].to_numpy()
        arr["spread"] = 10
        S.rates[(s, fake_mt5.TIMEFRAME_M15)] = arr
    S.balance = S.equity = 100_000.0
    S.deal_ticket += 1
    S.deals.append(dict(ticket=S.deal_ticket, time=ep(datetime(2021, 2, 1)), time_msc=0, type=2, entry=0, magic=0,
                        position_id=0, symbol="", volume=0, price=0, profit=100_000.0, commission=0, swap=0, fee=0,
                        reason=0, comment="initial deposit"))
    cfg = Config()
    cfg.night_reversion.symbols = list(syms)
    cfg.euro_session.enabled = False
    cfg.engine.dry_run = False
    cfg.engine.state_file = str(tmp_path / "state.json")
    cfg.engine.journal_dir = str(tmp_path / "journal")
    cfg.broker.server_time_mode = "ny7"
    cfg.risk.blackout_start = cfg.risk.blackout_end = ""
    cfg.risk.profile = "custom"
    cfg.risk.risk_nr = cfg.risk.risk_es = 0.035
    cfg.risk.max_total_risk = 0.05  # only one of two 3.5% trades fits

    def ticks(t):
        """Each pair's latest tick at server time t: its first tick of a new bar arrives delays[sym] s late."""
        S.now = ep(t)
        for s, df in data.items():
            b = pd.Timestamp(t).floor("15min").to_pydatetime()
            new = (t - b).total_seconds() >= delays.get(s, 0)
            tt = t if new else b - timedelta(seconds=2)
            k = df.index.searchsorted(pd.Timestamp(tt), side="right") - 1
            bid = float(df.iloc[k]["open"] if new else df.iloc[k]["close"])  # the old bar's last tick: its close
            S.ticks[s] = dict(time=ep(tt), bid=bid, ask=bid + 0.0001)

    ticks(datetime(2021, 3, 2, 23, 0, 2))
    eng = LiveEngine(cfg, MT5Broker(cfg), Notifier(cfg.notify), utcnow=lambda: NY7.server_to_utc(mt5_broker._ts(S.now)))
    eng.start()
    return eng, ticks


def run(eng, ticks, clock, start, end, hook=None):
    t = start
    while t <= end:
        clock.t += 5
        ticks(t)
        if hook:
            hook(t)
        eng.step()
        t += timedelta(seconds=5)


def opened():
    return sorted((p["symbol"], p["time"]) for p in S.positions)


# ------------------------------------------------------------------ D11 / parity#3
@pytest.mark.parametrize("delays", [{"AUDCAD": 6, "GBPCAD": 1}, {"AUDCAD": 1, "GBPCAD": 6}, {}])
def test_same_bar_signals_are_decided_alphabetically_whatever_ticks_first(monkeypatch, tmp_path, clock, delays):
    """AUDCAD and GBPCAD both signal on the 00:30 bar; the total-risk cap admits one. GBPCAD's first tick of the
    00:45 bar arrives 1 s after the boundary, AUDCAD's 6 s after: the loop at 00:45:02 used to see only GBPCAD's
    closed bar (MT5 forms a bar at its first tick) and gave it the capacity. Bars are now closed by time (+2 s
    settle): both are decided in the 00:45:02 loop, alphabetically, as in the backtester."""
    eng, ticks = setup(monkeypatch, tmp_path, ("GBPCAD", "AUDCAD"), delays)
    run(eng, ticks, clock, datetime(2021, 3, 2, 23, 0, 2), datetime(2021, 3, 3, 1, 0, 0))
    assert [s for s, _ in opened()] == ["AUDCAD"]
    assert opened()[0][1] == ep(datetime(2021, 3, 3, 0, 45, 2))  # the first loop after the boundary + 2 s


def test_a_bar_is_not_closed_before_the_2_second_settle(monkeypatch, tmp_path, clock):
    eng, ticks = setup(monkeypatch, tmp_path, ("AUDCAD",), {})
    run(eng, ticks, clock, datetime(2021, 3, 3, 0, 0, 1), datetime(2021, 3, 3, 0, 45, 1))  # xx:xx:01 loops
    assert eng.last_bar["AUDCAD"] == datetime(2021, 3, 3, 0, 15) and S.positions == []  # 00:30 bar: 1 s < 2 s
    run(eng, ticks, clock, datetime(2021, 3, 3, 0, 45, 6), datetime(2021, 3, 3, 0, 45, 6))
    assert eng.last_bar["AUDCAD"] == datetime(2021, 3, 3, 0, 30) and opened()[0][0] == "AUDCAD"


def test_broker_rates_drop_the_bar_that_is_not_closed_by_time(monkeypatch, tmp_path, clock):
    eng, ticks = setup(monkeypatch, tmp_path, ("AUDCAD", "GBPCAD"), {"AUDCAD": 30})
    ticks(datetime(2021, 3, 3, 0, 45, 10))  # GBPCAD has ticked in the new bar, AUDCAD not yet
    b = eng.broker
    for s in ("AUDCAD", "GBPCAD"):
        b.tick(s)
    for s in ("AUDCAD", "GBPCAD"):
        df = b.rates(s, "M15", 50)
        assert df.index[-1] == pd.Timestamp(2021, 3, 3, 0, 30) and len(df) == 50  # just closed, not forming


# ------------------------------------------------------------------ D9
def test_signal_blocked_by_a_pending_cash_flow_is_taken_when_it_is_explained(monkeypatch, tmp_path, clock):
    """A deposit reaches the balance right at the bar close; its deal shows 60 s later. The NR signal of the 00:30
    bar is blocked by the pending cash flow at 00:45:02: it is deferred (it used to be lost) and taken in the first
    loop after the deposit is explained, with the current quote."""
    eng, ticks = setup(monkeypatch, tmp_path, ("AUDCAD",), {})

    def hook(t):
        if t == datetime(2021, 3, 3, 0, 45, 2):
            S.balance += 5_000
            S.equity += 5_000
        if t == datetime(2021, 3, 3, 0, 46, 2):
            S.deal_ticket += 1
            S.deals.append(dict(ticket=S.deal_ticket, time=S.now - 60, time_msc=0, type=2, entry=0, magic=0,
                                position_id=0, symbol="", volume=0, price=0, profit=5_000.0, commission=0, swap=0,
                                fee=0, reason=0, comment="deposit"))

    run(eng, ticks, clock, datetime(2021, 3, 3, 0, 40, 2), datetime(2021, 3, 3, 0, 50, 2), hook)
    assert opened() == [("AUDCAD", ep(datetime(2021, 3, 3, 0, 46, 2)))]
    assert eng.state.cash_flow_total == 5_000 and eng._deferred == {}
