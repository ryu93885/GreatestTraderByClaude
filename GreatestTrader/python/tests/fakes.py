"""Test doubles: synthetic market data and an in-memory broker."""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from greatest_trader.broker.base import Broker, OrderResult, SymbolSpec, Tick
from greatest_trader.strategies.base import PositionView, pip_size


def synthetic_m15(start: datetime, days: int, base: float, pip: float, spike_at: datetime | None = None,
                  spike_pips: float = 60.0, seed: int = 1) -> pd.DataFrame:
    """Gently oscillating market (ATR ~ 15-20 pips) with an optional one-bar spike that reverts."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=days * 96, freq="15min")
    idx = idx[idx.dayofweek < 5]
    n = len(idx)
    mid = base + 25 * pip * np.sin(np.arange(n) / 9.0) + rng.normal(0, 2 * pip, n).cumsum() * 0.05
    o = mid + rng.normal(0, 1 * pip, n)
    c = mid + rng.normal(0, 1 * pip, n)
    h = np.maximum(o, c) + 8 * pip
    l = np.minimum(o, c) - 8 * pip
    df = pd.DataFrame({"open": o, "high": h, "low": l, "close": c, "spread_pips": 1.0}, index=idx)
    if spike_at is not None:
        k = df.index.get_loc(pd.Timestamp(spike_at))
        df.iloc[k, df.columns.get_loc("close")] += spike_pips * pip
        df.iloc[k, df.columns.get_loc("high")] = df.iloc[k]["close"] + 2 * pip
        # next bar opens back near the spike close, then market reverts
        df.iloc[k + 1, df.columns.get_loc("open")] = df.iloc[k]["close"] - 1 * pip
        df.iloc[k + 1, df.columns.get_loc("high")] = df.iloc[k + 1]["open"] + 2 * pip
    return df


class FakeBroker(Broker):
    def __init__(self, data: dict[str, pd.DataFrame], equity: float = 10000.0):
        self.data = data
        self.now: datetime | None = None
        self.equity = equity
        self.positions_: list[PositionView] = []
        self.log: list[tuple] = []
        self._ticket = 100

    # helpers for tests
    def set_time(self, t: datetime) -> None:
        """Advance the clock; positions whose SL/TP were touched by bars that completed in the
        meantime (bid prices, SL checked first) are closed like the broker server would."""
        prev = self.now
        self.now = t
        if prev is None:
            return
        for p in list(self.positions_):
            df = self.data[p.symbol]
            lo_t = pd.Timestamp(prev).floor("15min")
            bars = df[(df.index >= lo_t) & (df.index < pd.Timestamp(t).floor("15min"))]
            bars = bars[bars.index >= pd.Timestamp(p.open_time).floor("15min")]
            for ts, b in bars.iterrows():
                hit = None
                if p.direction > 0:
                    if p.sl and b.low <= p.sl:
                        hit = "SL"
                    elif p.tp and b.high >= p.tp:
                        hit = "TP"
                else:
                    if p.sl and b.high >= p.sl:
                        hit = "SL"
                    elif p.tp and b.low <= p.tp:
                        hit = "TP"
                if hit:
                    self.positions_.remove(p)
                    self.log.append(("server_" + hit, p.symbol, ts))
                    break

    def _bar(self, base: str) -> pd.Series:
        df = self.data[base]
        k = df.index.searchsorted(pd.Timestamp(self.now), side="right") - 1
        return df.iloc[max(k, 0)]

    # Broker API
    def connect(self) -> None: ...

    def shutdown(self) -> None: ...

    def account_equity(self) -> float:
        return self.equity

    def account_currency(self) -> str:
        return "JPY"

    def symbol_spec(self, base: str) -> SymbolSpec:
        pip = pip_size(base)
        digits = 3 if pip == 0.01 else 5
        return SymbolSpec(base, base, digits, pip / 10, pip, 0.01, 100.0, 0.01, 0)

    def tick(self, base: str) -> Tick | None:
        b = self._bar(base)
        bid = float(b["open"])
        return Tick(self.now, bid, bid + float(b["spread_pips"]) * pip_size(base))

    def server_time(self) -> datetime | None:
        return self.now

    def rates(self, base: str, timeframe: str, count: int) -> pd.DataFrame:
        df = self.data[base]
        if timeframe == "H1":
            df = df.resample("1h").agg({"open": "first", "high": "max", "low": "min", "close": "last",
                                        "spread_pips": "last"}).dropna()
            cur = pd.Timestamp(self.now).floor("h")
        else:
            cur = pd.Timestamp(self.now).floor("15min")
        closed = df[df.index < cur]
        return closed.iloc[-count:]

    def positions(self, magics: dict[int, str]) -> list[PositionView]:
        return list(self.positions_)

    def loss_per_lot(self, base: str, direction: int, entry: float, stop: float) -> float:
        return abs(entry - stop) * 100000

    def open_position(self, base, direction, volume, sl, tp, magic, comment) -> OrderResult:
        self._ticket += 1
        t = self.tick(base)
        price = t.ask if direction > 0 else t.bid
        strat = "NR" if magic == 7101 else "ES"
        self.positions_.append(PositionView(self._ticket, strat, base, direction, volume, price, self.now, sl, tp))
        self.log.append(("open", base, direction, volume, sl, tp))
        return OrderResult(True, self._ticket, price)

    def modify_position(self, ticket, base, sl, tp) -> OrderResult:
        for p in self.positions_:
            if p.ticket == ticket:
                p.tp = tp
        self.log.append(("modify", base, tp))
        return OrderResult(True, ticket)

    def close_position(self, ticket, base, comment="") -> OrderResult:
        self.positions_ = [p for p in self.positions_ if p.ticket != ticket]
        self.log.append(("close", base, comment))
        return OrderResult(True, ticket)


def step_until(engine, broker: FakeBroker, start: datetime, end: datetime, every=timedelta(minutes=15),
               offset=timedelta(seconds=5)) -> None:
    t = start
    while t <= end:
        broker.set_time(t + offset)
        engine.step()
        t += every
