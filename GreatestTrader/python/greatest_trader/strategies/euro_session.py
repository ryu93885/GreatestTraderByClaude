"""Euro Session flow (ES) - satellite strategy.

Edge: the euro tends to weaken against the US dollar during the European morning
(07:00 -> 13:00 UTC). Documented intraday seasonality ("currencies depreciate during
their local trading hours"); positive in 21 of 23 years 2000-2022 in our tests.

Rules:
  1. On UTC weekdays in `weekdays`, at 07:00 UTC open a position in `direction`
     (default SELL EURUSD).
  2. Stop loss = sl_atr_mult x ATR(H1, 24). No take profit.
  3. Close at 13:00 UTC.
  4. Skip when spread > max_spread_pips.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np

from ..config import EuroSessionConfig
from .base import CloseRequest, EntryRequest, PositionView


class EuroSession:
    name = "ES"

    def __init__(self, cfg: EuroSessionConfig):
        self.cfg = cfg

    def is_entry_time(self, now_utc: datetime) -> bool:
        """True during the first 15 minutes after the entry hour on a trading weekday."""
        c = self.cfg
        return (now_utc.weekday() in c.weekdays and now_utc.hour == c.entry_utc_hour
                and now_utc.minute < 15)

    def entry_signal(self, now_utc: datetime, now_server: datetime, atr_h1: float,
                     pip: float, spread_pips: float) -> EntryRequest | None:
        c = self.cfg
        if not self.is_entry_time(now_utc):
            return None
        if atr_h1 is None or not np.isfinite(atr_h1) or atr_h1 <= 0:
            return None
        if spread_pips > c.max_spread_pips:
            return None
        return EntryRequest(
            strategy=self.name, symbol=c.symbol, direction=c.direction,
            sl_distance=c.sl_atr_mult * atr_h1, tp_price=None, signal_time=now_server,
            reason=f"Euro session {'sell' if c.direction < 0 else 'buy'} {c.entry_utc_hour}:00-"
                   f"{c.exit_utc_hour}:00 UTC, ATR(H1) {atr_h1 / pip:.1f}p",
            meta={"atr_h1": float(atr_h1)},
        )

    def exit_due(self, pos: PositionView, now_utc: datetime, open_utc: datetime) -> bool:
        c = self.cfg
        exit_t = open_utc.replace(hour=c.exit_utc_hour, minute=0, second=0, microsecond=0)
        if open_utc.hour >= c.exit_utc_hour:  # opened late (should not happen) -> next day's exit
            exit_t += timedelta(days=1)
        return now_utc >= exit_t

    def manage(self, pos: PositionView, now_utc: datetime, open_utc: datetime) -> list:
        if self.exit_due(pos, now_utc, open_utc):
            return [CloseRequest(pos.ticket, pos.symbol, f"session exit {self.cfg.exit_utc_hour}:00 UTC")]
        return []
