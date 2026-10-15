"""Night Reversion (NR) - core strategy.

Edge: right after the daily rollover (00:00-02:00 market time = 17:00-19:00 New York)
liquidity is thin and both European and North-American participants are absent.
Short-term price excursions in EUR/GBP/CHF/CAD/JPY crosses tend to revert to the mean.

Rules (M15 bars, all times in market time = NY+7):
  1. Evaluate at the close of every M15 bar whose OPEN hour is in entry_hours (default 0,1).
  2. Skip Monday (weekend gap / thin liquidity) and Friday after 20:00.
  3. Cost filter: ATR(14) in pips >= atr_cost_mult x (spread + commission + slippage) pips.
  4. close > upper Bollinger(15, 1.8)  -> SELL ;  close < lower band -> BUY.
  5. Stop loss  = sl_atr_mult x ATR(14) from entry.
  6. Take profit = Bollinger middle line (SMA 15), re-set at every new M15 bar while it
     is on the profitable side of the entry price; otherwise no TP.
  7. Time stop: close after max_hold_bars M15 bars (default 12 = 3 hours).
  8. One position per symbol.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd

from ..config import NightReversionConfig
from ..indicators import atr, bollinger
from .base import CloseRequest, EntryRequest, ModifyRequest, PositionView

BAR = timedelta(minutes=15)


class NightReversion:
    name = "NR"

    def __init__(self, cfg: NightReversionConfig):
        self.cfg = cfg

    @property
    def warmup_bars(self) -> int:
        return max(self.cfg.bb_period, self.cfg.atr_period + 1) + 5

    # ------------------------------------------------------------------ data
    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        """Add indicator columns to an M15 OHLC frame (index = bar open time, server)."""
        c = self.cfg
        out = df.copy()
        mid, up, lo = bollinger(out["close"], c.bb_period, c.bb_deviation)
        out["mid"], out["upper"], out["lower"] = mid, up, lo
        out["atr"] = atr(out["high"], out["low"], out["close"], c.atr_period)
        return out

    def in_window(self, bar_open_market: datetime) -> bool:
        c = self.cfg
        dow, hr = bar_open_market.weekday(), bar_open_market.hour
        if dow >= 5:
            return False
        if c.skip_monday and dow == 0 and hr < 2:
            return False
        if dow == 4 and hr >= 20:
            return False
        return hr in c.entry_hours

    # --------------------------------------------------------------- signals
    def entry_signal(self, symbol: str, bar: dict, bar_open_server: datetime, bar_open_market: datetime,
                     pip: float, spread_pips: float, commission_pips: float) -> EntryRequest | None:
        """bar: dict with close, mid, upper, lower, atr of the just-closed signal bar."""
        c = self.cfg
        if not self.in_window(bar_open_market):
            return None
        a = bar["atr"]
        if a is None or not np.isfinite(a) or not np.isfinite(bar["upper"]):
            return None
        if spread_pips > c.max_spread_pips:
            return None
        cost_pips = spread_pips + commission_pips + c.slippage_pips
        if a / pip < c.atr_cost_mult * cost_pips:
            return None
        close = bar["close"]
        sd = (bar["upper"] - bar["mid"]) / c.bb_deviation if c.bb_deviation > 0 else 0.0
        if c.max_band_z > 0 and sd > 0 and abs(close - bar["mid"]) > c.max_band_z * sd:
            return None
        if c.max_bar_range_atr > 0 and "high" in bar and (bar["high"] - bar["low"]) > c.max_bar_range_atr * a:
            return None
        if close > bar["upper"]:
            direction = -1
        elif close < bar["lower"]:
            direction = 1
        else:
            return None
        return EntryRequest(
            strategy=self.name, symbol=symbol, direction=direction,
            sl_distance=c.sl_atr_mult * a, tp_price=float(bar["mid"]),
            signal_time=bar_open_server,
            reason=f"BB{c.bb_period}/{c.bb_deviation} {'upper' if direction < 0 else 'lower'} break, "
                   f"ATR {a / pip:.1f}p cost {cost_pips:.1f}p",
            meta={"atr": float(a), "mid": float(bar["mid"])},
        )

    @staticmethod
    def favorable_tp(direction: int, open_price: float, mid: float) -> float | None:
        if not np.isfinite(mid):
            return None
        if direction > 0 and mid > open_price:
            return float(mid)
        if direction < 0 and mid < open_price:
            return float(mid)
        return None

    # ------------------------------------------------------------ management
    def time_stop_due(self, pos: PositionView, now_server: datetime) -> bool:
        entry_bar_open = _floor15(pos.open_time)
        return now_server >= entry_bar_open + self.cfg.max_hold_bars * BAR

    def manage_on_bar(self, pos: PositionView, last_bar: dict, now_server: datetime,
                      bid: float, ask: float) -> list:
        """Called after each newly closed M15 bar (and on start-up)."""
        if self.time_stop_due(pos, now_server):
            return [CloseRequest(pos.ticket, pos.symbol, "time stop")]
        tp = self.favorable_tp(pos.direction, pos.open_price, last_bar["mid"])
        if tp is not None:
            px = bid if pos.direction > 0 else ask
            if (pos.direction > 0 and px >= tp) or (pos.direction < 0 and px <= tp):
                return [CloseRequest(pos.ticket, pos.symbol, "price already beyond moving average")]
        if not _same_price(tp, pos.tp):
            return [ModifyRequest(pos.ticket, pos.symbol, tp, "TP -> moving average")]
        return []


def _floor15(t: datetime) -> datetime:
    return t.replace(minute=(t.minute // 15) * 15, second=0, microsecond=0)


def _same_price(a: float | None, b: float | None) -> bool:
    if not a and not b:
        return True
    if not a or not b:
        return False
    return abs(a - b) <= 1e-9 * max(1.0, abs(a))
