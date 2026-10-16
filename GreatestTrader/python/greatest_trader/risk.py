"""Portfolio risk management.

* Fixed-fractional sizing: each trade risks `risk_per_trade x equity` between entry and stop,
  times the per-symbol multiplier (symbol_risk_mult) and the adaptive multiplier (vol target /
  equity-curve filter, 1.0 when off).
* Capacity caps: the money at risk of one trade is capped by max_risk_amount, max_lots_per_order and the
  broker's volume_max (lots x loss of 1.0 lot at the stop) BEFORE the portfolio guards see it.
* Drawdown scaling: risk x max(floor, 1 - DD / dd_scale_full)   (DD measured from equity peak).
* Daily / monthly loss limits: no new entries for the rest of the market day / month.
* Daily / monthly hard stops: close everything and stay flat for the rest of the day / month.
* Max drawdown halt: no new entries at all once DD >= max_drawdown_halt (reset: `python main.py reset-halt`).
* Position limits: total open positions, positions per currency, one position per symbol,
  same-direction positions per currency and total open risk.
* Year-end blackout: no new entries between blackout_start and blackout_end (market dates).
* Cash flows (deposits / withdrawals / credit / bonus) are not profit or loss: every equity baseline is
  scaled by equity_after / equity_before (time-weighted), see apply_cash_flow.
"""
from __future__ import annotations

import json
import logging
import math
import os
import shutil
import time
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

from .config import RiskConfig
from .strategies.base import PositionView, currencies

log = logging.getLogger(__name__)
CASH_K_MIN, CASH_K_MAX = 0.02, 50.0  # outside this range a cash flow rebases the baselines to current equity


@dataclass
class RiskState:
    """Persistent engine state (state/state.json). New fields always get defaults so that older state files
    still load; unknown keys in a file are ignored."""
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
    last_equity: float = 0.0  # last equity seen (= the current day's close so far; survives restarts)
    # --- long-term operation: account binding, cash flows, server clock, monitoring, trade journal
    account_login: int = 0  # the account this state belongs to (0 = not bound yet)
    account_server: str = ""
    cash_init: bool = False  # existing cash deals were marked as seen on the first scan
    cash_tickets: list = field(default_factory=list)  # [[deal ticket, server epoch], ...] already seen
    cash_flow_total: float = 0.0  # sum of the cash flows applied since cash_init
    clock_mode: str = ""  # last verified server clock (auto mode): ny7 | fixed
    clock_offset: float = 0.0
    dd_alert_day: str = ""  # market day of the last drawdown alert
    edge_month: str = ""  # last month evaluated by the R monitor (YYYY-MM)
    edge_neg_months: int = 0  # consecutive month ends with a negative risk-weighted R
    day_opens: int = 0  # entries / closes of the current market day (daily summary)
    day_closes: int = 0
    open_trades: dict = field(default_factory=dict)  # str(ticket) -> journal data of an open position
    # `reset-halt` request ("clear" | "rebase"), applied by the engine at its next start AFTER the first cash-flow
    # check (a deposit made while it was stopped must reach the old peak before the peak is rebased)
    reset_request: str = ""

    # ------------------------------------------------------------- persistence
    @classmethod
    def load(cls, path: str | Path) -> "RiskState":
        """Load the state; fall back to the newest readable daily backup. If no copy is readable, start
        HALTED (the peak equity was lost: a human must check the account and run reset-halt).
        `restored_from` tells the caller what happened ("" = normal load or a fresh start)."""
        p = Path(path)
        cands = [p] + sorted(p.parent.glob(f"{p.stem}.*.bak.json"), reverse=True)
        errors = []
        for c in cands:
            if not c.exists():
                continue
            try:
                with open(c, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    raise ValueError("not a JSON object")
                st = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
            except (ValueError, TypeError, OSError) as exc:  # JSONDecodeError is a ValueError
                errors.append(f"{c.name}: {exc}")
                continue
            st.restored_from = "" if c == p else c.name
            if c != p:
                log.error("state file %s unreadable or missing (%s) -> restored from backup %s", p.name,
                          "; ".join(errors) or "missing", c.name)
            return st
        st = cls()
        st.restored_from = ""
        if errors:
            st.halted = True
            st.halt_reason = "state file corrupt and no readable backup: check the account, then run reset-halt"
            st.restored_from = "none (" + "; ".join(errors) + ")"
            log.error("no readable state file (%s): starting HALTED", "; ".join(errors))
        return st

    def save(self, path: str | Path, keep_backups: int = 14) -> bool:
        """Write the state only if it changed: fsync + atomic replace (retried: on Windows an anti-virus or
        indexer may hold the file for a moment), plus one backup per calendar day (the newest `keep_backups`
        are kept). Returns True when the file was written."""
        p = Path(path)
        data = json.dumps(asdict(self), indent=2, ensure_ascii=False)
        if getattr(self, "_saved", None) == (str(p), data) and p.exists():
            return False
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(5):
            try:
                os.replace(tmp, p)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.2)
        self._saved = (str(p), data)
        bak = p.with_name(f"{p.stem}.{date.today():%Y%m%d}.bak.json")
        if not bak.exists():
            try:
                shutil.copy2(p, bak)
                for old in sorted(p.parent.glob(f"{p.stem}.*.bak.json"))[:-keep_backups]:
                    old.unlink()
            except OSError as exc:  # a backup problem must never stop trading
                log.warning("state backup failed: %s", exc)
        return True


class RiskManager:
    def __init__(self, cfg: RiskConfig, state: RiskState | None = None):
        self.cfg = cfg
        self.state = state or RiskState()
        self.flatten_reason = ""
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
            if st.day and st.last_equity > 0:
                st.daily_equity.append(float(st.last_equity))
                keep = max(c.vol_lookback_days, c.eq_filter_days) + 2
                if len(st.daily_equity) > keep:
                    del st.daily_equity[:-keep]
            st.day = dkey
            st.day_start_equity = equity
            st.day_halted = False
            self._adaptive_mult = self._compute_adaptive_mult()
        st.last_equity = float(equity)
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
        """new_risk: risk amount (account currency) of the candidate trade AFTER the capacity caps;
        positions carry risk_amount."""
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

    # ------------------------------------------------------------- cash flows / halt
    def apply_cash_flow(self, amount: float, equity_now: float) -> str:
        """A deposit (+) / withdrawal (-) of `amount` that is already contained in `equity_now`: scale every
        equity baseline by k = E / (E - F) so that drawdown, day and month returns stay time-weighted (a
        withdrawal is not a loss and a deposit hides none). Outside k = 0.02..50 they are rebased to E."""
        st = self.state
        before = equity_now - amount
        k = equity_now / before if before > 0 and equity_now > 0 else 0.0
        if CASH_K_MIN <= k <= CASH_K_MAX:
            st.peak_equity *= k
            st.day_start_equity *= k
            st.month_start_equity *= k
            st.last_equity *= k
            st.daily_equity = [float(x) * k for x in st.daily_equity]
            how = f"baselines x{k:.4f}"
        else:
            e = float(equity_now)
            st.peak_equity = st.day_start_equity = st.month_start_equity = st.last_equity = e
            st.daily_equity = [e for _ in st.daily_equity]
            how = "baselines rebased to current equity"
        st.cash_flow_total += amount
        self._adaptive_mult = self._compute_adaptive_mult()
        return f"CASH FLOW {amount:+,.2f} detected, guards rebased ({how}; not counted as profit/loss)"

    def reset_halt(self, rebase_peak_to: float | None = None) -> str:
        """Clear a max-drawdown halt and keep every other piece of state. Optionally rebase the peak."""
        st = self.state
        msg = f"halt cleared (was: {st.halt_reason or 'not halted'})"
        st.halted, st.halt_reason = False, ""
        if rebase_peak_to is not None and rebase_peak_to > 0:
            msg += f"; peak equity rebased {st.peak_equity:,.2f} -> {rebase_peak_to:,.2f}"
            st.peak_equity = float(rebase_peak_to)
        return msg

    # ------------------------------------------------------------- sizing
    def capped_risk(self, risk_amount: float, loss_per_lot: float, volume_max: float = 0.0) -> float:
        """Money at risk (account currency) after the capacity caps: max_risk_amount, max_lots_per_order and
        the broker's volume_max, the lot caps converted with the loss of 1.0 lot at the stop. Call it BEFORE
        can_open so that the total-risk cap sees the real size."""
        c = self.cfg
        if c.max_risk_amount > 0:
            risk_amount = min(risk_amount, c.max_risk_amount)
        if loss_per_lot > 0:
            if c.max_lots_per_order > 0:
                risk_amount = min(risk_amount, c.max_lots_per_order * loss_per_lot)
            if volume_max > 0:
                risk_amount = min(risk_amount, volume_max * loss_per_lot)
        return risk_amount

    def lot_size(self, risk_amount: float, loss_per_lot: float, vol_min: float, vol_max: float,
                 vol_step: float) -> float:
        """Lots so that hitting the stop loses `risk_amount` (account currency), at most
        min(vol_max, max_lots_per_order).

        loss_per_lot: loss of 1.0 lot at the stop (account currency, positive number).
        Returns 0.0 when the trade must be skipped.
        """
        if loss_per_lot <= 0 or risk_amount <= 0:
            return 0.0
        if self.cfg.max_lots_per_order > 0:
            vol_max = min(vol_max, self.cfg.max_lots_per_order)
        raw = risk_amount / loss_per_lot
        steps = math.floor(raw / vol_step + 1e-9)
        lots = round(steps * vol_step, 8)
        if lots < vol_min:
            if self.cfg.min_lot_policy == "min" and vol_min * loss_per_lot <= risk_amount * 1.5:
                lots = vol_min
            else:
                return 0.0
        return min(lots, vol_max)
