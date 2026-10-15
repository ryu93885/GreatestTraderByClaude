"""Time handling.

Three clocks are used in this system:

* UTC          - absolute time.
* Market time  - "New York time + 7 hours" (= GMT+2 in winter, GMT+3 during US DST).
                 The FX trading day starts at 00:00 market time (17:00 New York).
                 All Night-Reversion rules are expressed in market time.
* Server time  - the clock of the broker's MT5 server. Most MT5 brokers use
                 market time (GMT+2/+3 with US DST); some use a fixed offset.

US DST rules are implemented by hand (no tz database needed, identical to the
MQL5 implementation):
  * 2007 onwards : 2nd Sunday of March 07:00 UTC  -> 1st Sunday of November 06:00 UTC
  * 1987 - 2006  : 1st Sunday of April 07:00 UTC  -> last Sunday of October 06:00 UTC
"""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd

MARKET_OFFSET_WINTER = 2  # hours, market time = UTC + 2 when US DST is off
MARKET_OFFSET_SUMMER = 3  # hours, market time = UTC + 3 when US DST is on


def _nth_sunday(year: int, month: int, n: int) -> datetime:
    d = datetime(year, month, 1)
    first_sunday = d + timedelta(days=(6 - d.weekday()) % 7)
    return first_sunday + timedelta(weeks=n - 1)


def _last_sunday(year: int, month: int) -> datetime:
    nxt = datetime(year + (month == 12), month % 12 + 1, 1)
    last = nxt - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - 6) % 7)


def us_dst_bounds_utc(year: int) -> tuple[datetime, datetime]:
    """Return (start, end) of US daylight saving time in UTC for the given year."""
    if year >= 2007:
        start = _nth_sunday(year, 3, 2) + timedelta(hours=7)
        end = _nth_sunday(year, 11, 1) + timedelta(hours=6)
    else:
        start = _nth_sunday(year, 4, 1) + timedelta(hours=7)
        end = _last_sunday(year, 10) + timedelta(hours=6)
    return start, end


def is_us_dst_utc(t: datetime) -> bool:
    start, end = us_dst_bounds_utc(t.year)
    return start <= t.replace(tzinfo=None) < end


def utc_to_market(t: datetime) -> datetime:
    off = MARKET_OFFSET_SUMMER if is_us_dst_utc(t) else MARKET_OFFSET_WINTER
    return t.replace(tzinfo=None) + timedelta(hours=off)


def market_to_utc(t: datetime) -> datetime:
    t = t.replace(tzinfo=None)
    cand = t - timedelta(hours=MARKET_OFFSET_SUMMER)
    if is_us_dst_utc(cand):
        return cand
    return t - timedelta(hours=MARKET_OFFSET_WINTER)


# ---------------------------------------------------------------- vectorised
def _dst_mask_utc(idx: pd.DatetimeIndex) -> np.ndarray:
    years = np.unique(idx.year)
    mask = np.zeros(len(idx), dtype=bool)
    yrs = idx.year
    for y in years:
        s, e = us_dst_bounds_utc(int(y))
        sel = yrs == y
        mask[sel] = (idx[sel] >= s) & (idx[sel] < e)
    return mask


def utc_index_to_market(idx: pd.DatetimeIndex) -> pd.DatetimeIndex:
    idx = pd.DatetimeIndex(idx).tz_localize(None)
    off = np.where(_dst_mask_utc(idx), MARKET_OFFSET_SUMMER, MARKET_OFFSET_WINTER)
    return idx + pd.to_timedelta(off, unit="h")


def market_index_to_utc(idx: pd.DatetimeIndex) -> pd.DatetimeIndex:
    idx = pd.DatetimeIndex(idx).tz_localize(None)
    cand = idx - pd.Timedelta(hours=MARKET_OFFSET_SUMMER)
    off = np.where(_dst_mask_utc(cand), MARKET_OFFSET_SUMMER, MARKET_OFFSET_WINTER)
    return idx - pd.to_timedelta(off, unit="h")


class ServerClock:
    """Converts broker server time <-> UTC / market time.

    mode = "ny7"   : server time follows market time (GMT+2/+3 with US DST). Most MT5 brokers.
    mode = "fixed" : server time = UTC + fixed offset (hours), e.g. 0, 2, 3, 9.
    """

    def __init__(self, mode: str = "ny7", fixed_offset_hours: float = 2.0):
        mode = mode.lower()
        if mode not in ("ny7", "fixed"):
            raise ValueError(f"unknown server time mode: {mode}")
        self.mode = mode
        self.offset = float(fixed_offset_hours)

    # scalar ---------------------------------------------------------------
    def server_to_utc(self, t: datetime) -> datetime:
        if self.mode == "ny7":
            return market_to_utc(t)
        return t.replace(tzinfo=None) - timedelta(hours=self.offset)

    def utc_to_server(self, t: datetime) -> datetime:
        if self.mode == "ny7":
            return utc_to_market(t)
        return t.replace(tzinfo=None) + timedelta(hours=self.offset)

    def server_to_market(self, t: datetime) -> datetime:
        if self.mode == "ny7":
            return t.replace(tzinfo=None)
        return utc_to_market(self.server_to_utc(t))

    # vectorised -----------------------------------------------------------
    def server_index_to_utc(self, idx: pd.DatetimeIndex) -> pd.DatetimeIndex:
        if self.mode == "ny7":
            return market_index_to_utc(idx)
        return pd.DatetimeIndex(idx).tz_localize(None) - pd.Timedelta(hours=self.offset)

    def server_index_to_market(self, idx: pd.DatetimeIndex) -> pd.DatetimeIndex:
        if self.mode == "ny7":
            return pd.DatetimeIndex(idx).tz_localize(None)
        return utc_index_to_market(self.server_index_to_utc(idx))

    def __repr__(self) -> str:
        return f"ServerClock(mode={self.mode!r}, offset={self.offset})"


def detect_server_mode(server_now: datetime, utc_now: datetime) -> tuple[str, float]:
    """Guess the broker's server-time convention from the current server clock.

    Returns ("ny7", offset) when the observed offset equals the market-time offset
    for today's US DST state, otherwise ("fixed", observed offset).
    """
    observed = round((server_now - utc_now).total_seconds() / 1800.0) / 2.0  # 0.5h resolution
    expected = MARKET_OFFSET_SUMMER if is_us_dst_utc(utc_now) else MARKET_OFFSET_WINTER
    if abs(observed - expected) < 1e-9:
        return "ny7", observed
    return "fixed", observed
