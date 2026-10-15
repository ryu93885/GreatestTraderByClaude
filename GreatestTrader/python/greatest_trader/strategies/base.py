"""Common data structures shared by strategies, risk manager, brokers, engine and backtester."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


def pip_size(base_symbol: str, digits: int | None = None, point: float | None = None) -> float:
    """Pip size: 0.01 for JPY quotes, 0.1 for XAU, 0.0001 otherwise."""
    s = base_symbol.upper()
    if s.startswith("XAU"):
        return 0.1
    if s.endswith("JPY"):
        return 0.01
    if digits is not None and point is not None and digits in (2, 3):
        return 0.01
    return 0.0001


def currencies(base_symbol: str) -> tuple[str, str]:
    s = base_symbol.upper()
    return s[:3], s[3:6]


@dataclass
class EntryRequest:
    strategy: str  # "NR" | "ES"
    symbol: str  # base symbol, e.g. "EURJPY"
    direction: int  # +1 buy / -1 sell
    sl_distance: float  # price distance entry -> stop loss (> 0)
    tp_price: float | None  # absolute take-profit price or None
    signal_time: datetime  # server time of the signal bar (open time)
    reason: str = ""
    meta: dict = field(default_factory=dict)


@dataclass
class ModifyRequest:
    ticket: int
    symbol: str
    tp_price: float | None  # None -> remove TP
    reason: str = ""


@dataclass
class CloseRequest:
    ticket: int
    symbol: str
    reason: str = ""


@dataclass
class PositionView:
    ticket: int
    strategy: str
    symbol: str  # base symbol
    direction: int
    volume: float
    open_price: float
    open_time: datetime  # server time
    sl: float | None
    tp: float | None
    risk_amount: float = 0.0  # backtest bookkeeping
    meta: dict = field(default_factory=dict)
