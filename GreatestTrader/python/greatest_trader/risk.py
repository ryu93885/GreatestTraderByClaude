"""Portfolio risk management.

* Fixed-fractional sizing: each trade risks `risk_per_trade x equity` between entry and stop.
* Drawdown scaling: risk x max(floor, 1 - DD / dd_scale_full)   (DD measured from equity peak).
* Daily loss limit: no new entries for the rest of the market day after -daily_loss_limit.
* Max drawdown halt: no new entries at all once DD >= max_drawdown_halt (manual reset).
* Position limits: total open positions, positions per currency, one position per symbol.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

from .config import RiskConfig
from .strategies.base import PositionView, currencies


@dataclass
class RiskState:
    peak_equity: float = 0.0
    day: str = ""  # market-time date (YYYY-MM-DD)
    day_start_equity: float = 0.0
    halted: bool = False
    halt_reason: str = ""
    last_es_entry_day: str = ""  # UTC date of last Euro-session entry

    @classmethod
    def load(cls, path: str | Path) -> "RiskState":
        p = Path(path)
        if p.exists():
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
            return cls(**known)
        return cls()

    def save(self, path: str | Path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2, ensure_ascii=False)
        tmp.replace(p)


class RiskManager:
    def __init__(self, cfg: RiskConfig, state: RiskState | None = None):
        self.cfg = cfg
        self.state = state or RiskState()

    # ------------------------------------------------------------- equity
    def update_equity(self, equity: float, market_day: date) -> list[str]:
        """Update peak / daily bookkeeping. Returns list of event messages."""
        events: list[str] = []
        st = self.state
        if equity > st.peak_equity:
            st.peak_equity = equity
        dkey = market_day.isoformat()
        if st.day != dkey:
            st.day = dkey
            st.day_start_equity = equity
        dd = self.drawdown(equity)
        if not st.halted and self.cfg.max_drawdown_halt > 0 and dd >= self.cfg.max_drawdown_halt:
            st.halted = True
            st.halt_reason = f"max drawdown {dd:.1%} reached on {dkey}"
            events.append(f"HALT: {st.halt_reason}")
        return events

    def drawdown(self, equity: float) -> float:
        pk = self.state.peak_equity
        return 0.0 if pk <= 0 else max(0.0, 1.0 - equity / pk)

    def daily_limit_hit(self, equity: float) -> bool:
        st = self.state
        if self.cfg.daily_loss_limit <= 0 or st.day_start_equity <= 0:
            return False
        return equity <= st.day_start_equity * (1.0 - self.cfg.daily_loss_limit)

    def risk_fraction(self, strategy: str, equity: float) -> float:
        base = self.cfg.base_risk(strategy.lower())
        if self.cfg.dd_scale_full > 0:
            mult = max(self.cfg.dd_scale_floor, 1.0 - self.drawdown(equity) / self.cfg.dd_scale_full)
        else:
            mult = 1.0
        return base * mult

    # ------------------------------------------------------------- limits
    def can_open(self, strategy: str, symbol: str, positions: list[PositionView],
                 equity: float) -> tuple[bool, str]:
        if self.state.halted:
            return False, f"halted ({self.state.halt_reason})"
        if self.daily_limit_hit(equity):
            return False, "daily loss limit reached"
        if len(positions) >= self.cfg.max_open_positions:
            return False, "max open positions"
        if any(p.symbol == symbol for p in positions):
            return False, "position already open on symbol"
        ccy_count: dict[str, int] = {}
        for p in positions:
            for c in currencies(p.symbol):
                ccy_count[c] = ccy_count.get(c, 0) + 1
        for c in currencies(symbol):
            if ccy_count.get(c, 0) >= self.cfg.max_positions_per_currency:
                return False, f"max positions for currency {c}"
        return True, ""

    # ------------------------------------------------------------- sizing
    def lot_size(self, risk_amount: float, loss_per_lot: float, vol_min: float, vol_max: float,
                 vol_step: float) -> float:
        """Lots so that hitting the stop loses `risk_amount` (account currency).

        loss_per_lot: loss of 1.0 lot at the stop (account currency, positive number).
        Returns 0.0 when the trade must be skipped.
        """
        if loss_per_lot <= 0 or risk_amount <= 0:
            return 0.0
        raw = risk_amount / loss_per_lot
        steps = math.floor(raw / vol_step + 1e-9)
        lots = round(steps * vol_step, 8)
        if lots < vol_min:
            if self.cfg.min_lot_policy == "min" and vol_min * loss_per_lot <= risk_amount * 1.5:
                lots = vol_min
            else:
                return 0.0
        return min(lots, vol_max)
