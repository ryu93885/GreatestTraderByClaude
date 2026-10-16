"""Append-only audit files of the live engine (never rotated, never rewritten).

* journal/trades_YYYY.csv : one row per entry (event=open) and per closed position (event=close), plus
  operator events (reset_halt). R = P/L / risk amount of the trade (money lost at the stop at entry).
* journal/equity_log.csv  : one row per market day (written at the day change): closing equity, peak, drawdown,
  month-to-date return, cash flows so far, account balance, trades, halt / entry-block status.

The R-based edge monitor reads the closed trades back: rw = sum(base_risk x R) / sum(base_risk) over the last
N closed trades, base_risk = preset risk x symbol multiplier BEFORE drawdown scaling, so the statistic does not
depend on position size.
"""
from __future__ import annotations

import csv
import logging
import math
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

TRADE_FIELDS = ["event", "time_utc", "server_time", "strategy", "symbol", "direction", "lots", "intended_price",
                "fill_price", "sl", "tp", "risk_pct", "risk_amount", "base_risk", "ticket", "close_time",
                "close_price", "close_reason", "pnl", "R", "reason"]
EQUITY_FIELDS = ["market_date", "time_utc", "close_equity", "peak_equity", "drawdown", "month_return",
                 "cash_flow_total", "opens", "closes", "halted", "entries_blocked", "balance"]
EDGE_MIN_TRADES = 50  # the R monitor needs at least this many closed trades before it can warn


class TradeJournal:
    def __init__(self, directory: str | Path):
        self.dir = Path(directory)

    def _append(self, path: Path, fieldnames: list[str], row: dict) -> bool:
        """Append one CSV row; a write problem is logged but never stops trading. A file written by an older
        version keeps its own header (columns added later are left out of its rows), so it stays readable."""
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            new = not path.exists() or path.stat().st_size == 0
            if not new:
                with open(path, "r", newline="", encoding="utf-8") as f:
                    header = next(csv.reader(f), None)
                if header and header != fieldnames and set(header) <= set(fieldnames):
                    fieldnames = header
            with open(path, "a", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
                if new:
                    w.writeheader()
                w.writerow({k: _fmt(row.get(k, "")) for k in fieldnames})
            return True
        except OSError as exc:
            log.error("journal write failed (%s): %s", path.name, exc)
            return False

    def trade(self, row: dict) -> bool:
        year = str(row.get("time_utc", ""))[:4] or "0000"
        return self._append(self.dir / f"trades_{year}.csv", TRADE_FIELDS, row)

    def equity(self, row: dict) -> bool:
        return self._append(self.dir / "equity_log.csv", EQUITY_FIELDS, row)

    def closed_trades(self) -> pd.DataFrame:
        """Every close row of every trades_YYYY.csv, oldest first."""
        frames = []
        for p in sorted(self.dir.glob("trades_*.csv")):
            try:
                df = pd.read_csv(p)
            except (OSError, ValueError, pd.errors.EmptyDataError) as exc:
                log.warning("cannot read %s: %s", p.name, exc)
                continue
            frames.append(df[df["event"] == "close"])
        if not frames:
            return pd.DataFrame(columns=TRADE_FIELDS)
        out = pd.concat(frames, ignore_index=True)
        for c in ("R", "base_risk", "pnl", "risk_amount"):
            out[c] = pd.to_numeric(out[c], errors="coerce")
        return out


def risk_weighted_r(closes: pd.DataFrame, window: int) -> tuple[float, int]:
    """(rw, n): sum(base_risk x R) / sum(base_risk) over the last `window` closed trades with a known R and
    base risk; (nan, n) when there are none."""
    if len(closes) == 0:
        return float("nan"), 0
    d = closes[["R", "base_risk"]].apply(pd.to_numeric, errors="coerce").dropna()
    d = d[d.base_risk > 0].tail(window)
    if len(d) == 0:
        return float("nan"), 0
    return float((d.base_risk * d.R).sum() / d.base_risk.sum()), int(len(d))


def _fmt(v) -> str:
    if isinstance(v, float):
        if math.isnan(v):
            return ""
        return f"{v:.10g}"
    return "" if v is None else str(v)
