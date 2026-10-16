"""Broker abstraction used by the live engine."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

import pandas as pd

from ..strategies.base import PositionView

CASH_DEAL_TYPES = (2, 3, 6)  # MT5 DEAL_TYPE_BALANCE, DEAL_TYPE_CREDIT, DEAL_TYPE_BONUS
DEAL_TYPE_CREDIT = 3


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


@dataclass
class CashFlow:
    """A deposit / withdrawal / credit / bonus booked on the account (not trading profit or loss)."""
    ticket: int  # deal ticket (unique, used for de-duplication)
    time: datetime  # server time
    amount: float  # + deposit, - withdrawal (account currency)
    type: int  # 2 balance, 3 credit, 6 bonus
    comment: str = ""


@dataclass
class CashScan:
    """One cash-flow check: the cash deals of the scan window plus ONE account read taken AFTER the history read
    (so that k = E / (E - F) uses an equity that contains every flow the scan found)."""
    flows: list[CashFlow]
    equity: float
    newest_deal: int | None = None  # newest deal ticket of ANY type (trade or cash) in the history; None = unknown
    money: float | None = None  # balance + credit at the account read; None = unknown (no cash-flow lag guard)
    scan_money: float | None = None  # balance + credit when the history was read (differs: booked meanwhile)
    balance: float | None = None


@dataclass
class HistoryCheck:
    """The WHOLE deal history, read for the first-run priming of the cash-flow cursor."""
    flows: list[CashFlow]  # every balance / credit / bonus deal
    complete: bool | None  # the deals add up to the balance (and the credit); None = cannot be checked
    detail: str = ""


@dataclass
class ClosedTrade:
    """Result of a closed position from the deal history."""
    ticket: int  # position ticket
    close_time: datetime  # server time of the (last) exit deal
    close_price: float
    pnl: float  # profit + commission + swap + fee of every deal of the position (account currency)
    reason: str = ""  # "SL", "TP", "stop out", "expert", "manual", ...


class Broker(ABC):
    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def shutdown(self) -> None: ...

    def reconnect(self) -> None:
        """Drop the connection and connect again (clears cached data)."""
        try:
            self.shutdown()
        finally:
            self.connect()

    def health(self) -> str:
        """'' when trading decisions are safe, otherwise the reason why not (may re-initialise a lost link)."""
        return ""

    @abstractmethod
    def account_equity(self) -> float: ...

    @abstractmethod
    def account_currency(self) -> str: ...

    def account_login(self) -> int:
        """Login of the account the terminal is logged into (0 = unknown: account binding is skipped)."""
        return 0

    def account_server(self) -> str:
        return ""

    def is_hedging(self) -> bool:
        """True for hedging accounts (required: one position per strategy and symbol, closed by ticket)."""
        return True

    def free_margin(self) -> float:
        """Free margin in account currency (override in real brokers)."""
        return float("inf")

    def margin_required(self, base: str, direction: int, volume: float, price: float) -> float:
        """Margin needed to open `volume` lots (override in real brokers)."""
        return 0.0

    def cash_flows(self) -> list[CashFlow]:
        """Recent deposits / withdrawals / credit / bonus deals (the engine de-duplicates by ticket)."""
        return []

    def cash_scan(self, fresh: bool = False) -> CashScan:
        """The engine's cash-flow check: cash_flows() first, then the account (equity, balance + credit).
        fresh=True: the engine waits for a deal that explains a balance change, read the history again.
        This default knows neither the balance nor the deal tickets: no cash-flow lag guard."""
        flows = self.cash_flows()
        return CashScan(flows, self.account_equity())

    def history_check(self) -> HistoryCheck:
        """First run: every cash deal of the whole history and whether the history is complete (None = cannot be
        checked: the engine primes the cash-flow cursor at once)."""
        return HistoryCheck(self.cash_flows(), None)

    def closed_trade(self, ticket: int) -> ClosedTrade | None:
        """P/L of a position that is no longer open (None while the deal history does not show it yet)."""
        return None

    def versions(self) -> dict[str, str]:
        """Versions of the broker package / terminal for the start-up log and the heartbeat."""
        return {}

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
        """Open positions belonging to this system. magics maps magic number -> strategy name.
        Must RAISE when the position list cannot be read (an error is never 'no positions')."""

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
