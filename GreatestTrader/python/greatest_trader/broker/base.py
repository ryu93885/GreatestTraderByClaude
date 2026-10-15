"""Broker abstraction used by the live engine."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from ..strategies.base import PositionView


@dataclass
class SymbolSpec:
    base: str  # e.g. "EURUSD"
    name: str  # broker symbol, e.g. "EURUSD.raw"
    digits: int
    point: float
    pip: float
    volume_min: float
    volume_max: float
    volume_step: float
    stops_level_points: int = 0


@dataclass
class Tick:
    time: datetime  # server time
    bid: float
    ask: float


@dataclass
class OrderResult:
    ok: bool
    ticket: int = 0
    price: float = 0.0
    message: str = ""


class Broker(ABC):
    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def shutdown(self) -> None: ...

    @abstractmethod
    def account_equity(self) -> float: ...

    @abstractmethod
    def account_currency(self) -> str: ...

    def free_margin(self) -> float:
        """Free margin in account currency (override in real brokers)."""
        return float("inf")

    def margin_required(self, base: str, direction: int, volume: float, price: float) -> float:
        """Margin needed to open `volume` lots (override in real brokers)."""
        return 0.0

    @abstractmethod
    def symbol_spec(self, base: str) -> SymbolSpec: ...

    @abstractmethod
    def tick(self, base: str) -> Tick | None: ...

    @abstractmethod
    def server_time(self) -> datetime | None:
        """Latest known server time (from the most recent tick of any symbol)."""

    @abstractmethod
    def rates(self, base: str, timeframe: str, count: int) -> pd.DataFrame:
        """Closed bars only, index = bar open time (server time), columns open/high/low/close/spread."""

    @abstractmethod
    def positions(self, magics: dict[int, str]) -> list[PositionView]:
        """Open positions belonging to this system. magics maps magic number -> strategy name."""

    @abstractmethod
    def loss_per_lot(self, base: str, direction: int, entry: float, stop: float) -> float:
        """Loss (positive, account currency) of 1.0 lot moving from entry to stop."""

    @abstractmethod
    def open_position(self, base: str, direction: int, volume: float, sl: float, tp: float | None,
                      magic: int, comment: str) -> OrderResult: ...

    @abstractmethod
    def modify_position(self, ticket: int, base: str, sl: float | None, tp: float | None) -> OrderResult: ...

    @abstractmethod
    def close_position(self, ticket: int, base: str, comment: str = "") -> OrderResult: ...
