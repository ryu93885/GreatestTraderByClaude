"""Portfolio risk management.

* Fixed-fractional sizing: each trade risks `risk_per_trade x equity` between entry and stop,
  times the per-symbol multiplier (symbol_risk_mult) and the adaptive multiplier (vol target /
  equity-curve filter, 1.0 when off).
* Drawdown scaling: risk x max(floor, 1 - DD / dd_scale_full)   (DD measured from equity peak).
* Daily / monthly loss limits: no new entries for the rest of the market day / month.
* Daily / monthly hard stops: close everything and stay flat for the rest of the day / month.
* Max drawdown halt: no new entries at all once DD >= max_drawdown_halt (manual reset).
* Position limits: total open positions, positions per currency, one position per symbol,
  same-direction positions per currency and total open risk.
* Year-end blackout: no new entries between blackout_start and blackout_end (market dates).
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
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
    month: str = ""  # market-time month (YYYY-MM)
    month_start_equity: float = 0.0
    month_halted: bool = False
    day_halted: bool = False
    daily_equity: list = field(default_factory=list)  # closing equity of past market days (most recent last)

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
        self.flatten_reason = ""
        self._last_equity: float | None = None
        self._adaptive_mult = self._compute_adaptive_mult()

    # ------------------------------------------------------------- equity
    def update_equity(self, equity: float, market_day: date) -> list[str]:
        """Update peak / daily / monthly bookkeeping. Returns list of event messages.
        After the call, `flatten_reason` is set when a hard stop requires closing all positions."""
        events: list[str] = []
        st = self.state
        c = self.cfg
        self.flatten_reason = ""
        if equity > st.peak_equity:
            st.peak_equity = equity
        dkey = market_day.isoformat()
        if st.day != dkey:
            if st.day and self._last_equity is not None:
                st.daily_equity.append(float(self._last_equity))
                keep = max(c.vol_lookback_days, c.eq_filter_days) + 2
                if len(st.daily_equity) > keep:
                    del st.daily_equity[:-keep]
            st.day = dkey
            st.day_start_equity = equity
            st.day_halted = False
            self._adaptive_mult = self._compute_adaptive_mult()
        self._last_equity = equity
        mkey = dkey[:7]
        if st.month != mkey:
            st.month = mkey
            st.month_start_equity = equity
            st.month_halted = False
        dd = self.drawdown(equity)
        if not st.halted and c.max_drawdown_halt > 0 and dd >= c.max_drawdown_halt:
            st.halted = True
            st.halt_reason = f"max drawdown {dd:.1%} reached on {dkey}"
            events.append(f"HALT: {st.halt_reason}")
        if c.monthly_hard_stop > 0 and not st.month_halted and st.month_start_equity > 0 \
                and equity <= st.month_start_equity * (1 - c.monthly_hard_stop):
            st.month_halted = True
            self.flatten_reason = f"monthly hard stop {c.monthly_hard_stop:.1%}"
            events.append(f"MONTH STOP: {self.flatten_reason} ({mkey})")
        if c.daily_hard_stop > 0 and not st.day_halted and st.day_start_equity > 0 \
                and equity <= st.day_start_equity * (1 - c.daily_hard_stop):
            st.day_halted = True
            self.flatten_reason = self.flatten_reason or f"daily hard stop {c.daily_hard_stop:.1%}"
            events.append(f"DAY STOP: daily hard stop {c.daily_hard_stop:.1%} ({dkey})")
        return events

    def hard_stop_active(self) -> str:
        """Reason while a monthly/daily hard stop is in force (our positions must stay flat), else ''.
        Lets the live engine retry closing positions whose first close attempt failed."""
        if self.state.month_halted:
            return f"monthly hard stop {self.cfg.monthly_hard_stop:.1%}"
        if self.state.day_halted:
            return f"daily hard stop {self.cfg.daily_hard_stop:.1%}"
        return ""

    def _compute_adaptive_mult(self) -> float:
        """Volatility-target and equity-curve multipliers, recomputed once per market day."""
        c, eqs = self.cfg, self.state.daily_equity
        mult = 1.0
        if c.vol_target_daily > 0 and len(eqs) >= max(5, c.vol_lookback_days // 2):
            x = eqs[-(c.vol_lookback_days + 1):]
            rets = [x[i] / x[i - 1] - 1 for i in range(1, len(x)) if x[i - 1] > 0]
            if len(rets) >= 2:
                mu = sum(rets) / len(rets)
                sd = (sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)) ** 0.5
                if sd > 0:
                    mult *= min(c.vol_mult_max, max(c.vol_mult_min, c.vol_target_daily / sd))
        if c.eq_filter_days > 0 and len(eqs) >= c.eq_filter_days:
            sma = sum(eqs[-c.eq_filter_days:]) / c.eq_filter_days
            if eqs[-1] < sma:
                mult *= c.eq_filter_mult
        return mult

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
        return base * mult * self._adaptive_mult

    # ------------------------------------------------------------- limits
    def can_open(self, strategy: str, symbol: str, positions: list[PositionView], equity: float,
                 direction: int = 0, new_risk: float = 0.0) -> tuple[bool, str]:
        """new_risk: risk amount (account currency) of the candidate trade; positions carry risk_amount."""
        c, st = self.cfg, self.state
        if st.halted:
            return False, f"halted ({st.halt_reason})"
        if self.in_blackout():
            return False, "year-end holiday blackout"
        if st.month_halted:
            return False, "monthly stop"
        if st.day_halted:
            return False, "daily stop"
        if self.daily_limit_hit(equity):
            return False, "daily loss limit reached"
        if c.monthly_loss_limit > 0 and st.month_start_equity > 0 \
                and equity <= st.month_start_equity * (1 - c.monthly_loss_limit):
            return False, "monthly loss limit reached"
        if len(positions) >= c.max_open_positions:
            return False, "max open positions"
        if any(p.symbol == symbol for p in positions):
            return False, "position already open on symbol"
        ccy_count: dict[str, int] = {}
        dir_count: dict[tuple[str, int], int] = {}
        for p in positions:
            b, q = currencies(p.symbol)
            for cc in (b, q):
                ccy_count[cc] = ccy_count.get(cc, 0) + 1
            dir_count[(b, p.direction)] = dir_count.get((b, p.direction), 0) + 1
            dir_count[(q, -p.direction)] = dir_count.get((q, -p.direction), 0) + 1
        b, q = currencies(symbol)
        for cc in (b, q):
            if ccy_count.get(cc, 0) >= c.max_positions_per_currency:
                return False, f"max positions for currency {cc}"
        if c.max_same_direction_per_currency > 0 and direction != 0:
            for key in ((b, direction), (q, -direction)):
                if dir_count.get(key, 0) >= c.max_same_direction_per_currency:
                    return False, f"max same-direction positions for {key[0]}"
        if c.max_total_risk > 0 and equity > 0:
            open_risk = sum(p.risk_amount for p in positions)
            if (open_risk + new_risk) / equity > c.max_total_risk + 1e-12:
                return False, "max total open risk"
        return True, ""

    def in_blackout(self, market_day: date | None = None) -> bool:
        """True when the market date (default: the current one) lies in the blackout window."""
        c = self.cfg
        if not c.blackout_start or not c.blackout_end:
            return False
        if market_day is None:
            if not self.state.day:
                return False
            market_day = date.fromisoformat(self.state.day)
        md = (market_day.month, market_day.day)
        a = tuple(int(x) for x in c.blackout_start.split("-"))
        b = tuple(int(x) for x in c.blackout_end.split("-"))
        return (a <= md <= b) if a <= b else (md >= a or md <= b)

    def symbol_mult(self, symbol: str) -> float:
        return float(self.cfg.symbol_risk_mult.get(symbol, 1.0))

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
