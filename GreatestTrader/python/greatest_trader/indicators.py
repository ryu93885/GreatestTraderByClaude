"""Technical indicators (identical definitions in the MQL5 EA).

* SMA            : simple moving average of close.
* Bollinger band : SMA +/- k * population standard deviation (MT5 iBands definition).
* ATR            : simple moving average of True Range (MT5 iATR definition).
                   TR needs the previous close, so the first bar has no TR.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def sma(x: pd.Series, period: int) -> pd.Series:
    return x.rolling(period, min_periods=period).mean()


def bollinger(close: pd.Series, period: int, dev: float) -> tuple[pd.Series, pd.Series, pd.Series]:
    mid = sma(close, period)
    sd = close.rolling(period, min_periods=period).std(ddof=0)
    return mid, mid + dev * sd, mid - dev * sd


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    pc = close.shift(1)
    tr = np.maximum(high - low, np.maximum((high - pc).abs(), (low - pc).abs()))
    tr[pc.isna()] = np.nan
    return tr


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    return true_range(high, low, close).rolling(period, min_periods=period).mean()
