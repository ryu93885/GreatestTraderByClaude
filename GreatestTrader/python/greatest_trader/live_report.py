"""Live performance report (`python main.py report`).

* Monthly time-weighted returns from the MT5 deal history, split at every deposit / withdrawal / credit /
  bonus deal (cash flows are not performance).
* P/L by magic number (NR / ES / other), rolling 3 / 6 / 12-month returns.
* Comparison with the stable5 backtest 2007-2022: 5% of the rolling 12-month windows were below -1.6% and the
  worst one was -8.4%.
* The R-based edge monitor from the trade journal, evaluated at every month end.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from .broker.base import CASH_DEAL_TYPES
from .journal import EDGE_MIN_TRADES
from .timeutil import utc_index_to_market

BACKTEST_12M_P5 = -0.016  # stable5 backtest: 5th percentile of the rolling 12-month returns
BACKTEST_12M_WORST = -0.084  # stable5 backtest: worst rolling 12-month return
EXIT_ENTRIES = (1, 2, 3)  # DEAL_ENTRY_OUT, INOUT, OUT_BY


def monthly_returns_from_deals(deals: pd.DataFrame, magics: dict[int, str]) -> pd.DataFrame:
    """deals: time (server), ticket, type, entry, magic, profit, commission, swap, fee. The balance is rebuilt
    from the first deal, so the history must start with the account's first deposit. Month-end balance is used
    as month-end equity: exact when no position is open over a month end (this system holds positions for
    hours only). Returns one row per calendar month (server time) with the time-weighted return."""
    cols = ["return", "cash_flow", "end_balance", "closed_trades"] + [f"pnl_{n}" for n in magics.values()] + \
        ["pnl_other"]
    if len(deals) == 0:
        return pd.DataFrame(columns=cols)
    d = deals.copy()
    for c in ("commission", "swap", "fee", "entry", "magic"):
        d[c] = pd.to_numeric(d[c], errors="coerce").fillna(0) if c in d.columns else 0
    d["time"] = pd.to_datetime(d["time"])
    d = d.sort_values(["time", "ticket"] if "ticket" in d.columns else ["time"])
    d["amount"] = d.profit.astype(float) + d.commission + d.swap + d.fee
    d["flow"] = d.type.astype(int).isin(CASH_DEAL_TYPES)
    d["bal"] = d.amount.cumsum()
    d["month"] = d.time.dt.to_period("M")
    rows, prev_end = {}, None
    for m, g in d.groupby("month"):
        r, base = 1.0, prev_end
        for amount, bal, flow in zip(g.amount, g.bal, g.flow):
            if flow:
                if base is not None and base > 0:
                    r *= (bal - amount) / base
                base = bal
        end = float(g.bal.iloc[-1])
        if base is not None and base > 0:
            r *= end / base
        trades = g[~g.flow]
        row = {"return": r - 1 if base is not None and base > 0 else np.nan,
               "cash_flow": float(g[g.flow].amount.sum()), "end_balance": end,
               "closed_trades": int((trades.entry.isin(EXIT_ENTRIES) & trades.magic.isin(list(magics))).sum())}
        for mg, name in magics.items():
            row[f"pnl_{name}"] = float(trades[trades.magic == mg].amount.sum())
        row["pnl_other"] = float(trades[~trades.magic.isin(list(magics))].amount.sum())
        rows[m] = row
        prev_end = end
    out = pd.DataFrame.from_dict(rows, orient="index")
    full = pd.period_range(out.index.min(), out.index.max(), freq="M")
    out = out.reindex(full)
    out["end_balance"] = out["end_balance"].ffill()
    out = out.fillna({c: 0.0 for c in cols if c != "end_balance"})
    out["closed_trades"] = out["closed_trades"].astype(int)
    out.index = out.index.astype(str)
    out.index.name = "month"
    return out[cols]


def add_rolling(monthly: pd.DataFrame) -> pd.DataFrame:
    """Rolling 3 / 6 / 12-month compounded returns and the comparison with the backtest's 12-month windows."""
    out = monthly.copy()
    g = 1 + out["return"].astype(float)
    for w in (3, 6, 12):
        out[f"r{w}m"] = g.rolling(w).apply(np.prod, raw=True) - 1
    r12 = out["r12m"]
    out["vs_backtest_12m"] = np.where(r12 < BACKTEST_12M_WORST, "WORSE THAN ANY BACKTEST 12m WINDOW",
                                      np.where(r12 < BACKTEST_12M_P5, "below backtest 5th percentile", ""))
    out.loc[r12.isna(), "vs_backtest_12m"] = ""
    return out


def pnl_by_magic(deals: pd.DataFrame, magics: dict[int, str]) -> pd.DataFrame:
    if len(deals) == 0:
        return pd.DataFrame(columns=["strategy", "closed_trades", "pnl"])
    d = deals[~deals.type.astype(int).isin(CASH_DEAL_TYPES)].copy()
    for c in ("commission", "swap", "fee"):
        d[c] = pd.to_numeric(d[c], errors="coerce").fillna(0) if c in d.columns else 0
    d["pnl"] = d.profit.astype(float) + d.commission + d.swap + d.fee
    d["strategy"] = d.magic.map(lambda m: magics.get(int(m), f"other (magic {int(m)})"))
    d["closed"] = d.entry.isin(EXIT_ENTRIES).astype(int)
    return d.groupby("strategy").agg(closed_trades=("closed", "sum"), pnl=("pnl", "sum")).reset_index()


def edge_monitor_table(closes: pd.DataFrame, window: int) -> pd.DataFrame:
    """R monitor at every month end of the journal: rw = sum(base_risk x R) / sum(base_risk) over the last
    `window` closed trades, WARN when rw < 0, REVIEW at 2 consecutive month ends."""
    cols = ["trades", "rw", "status"]
    if len(closes) == 0:
        return pd.DataFrame(columns=cols)
    d = closes.copy()
    d["R"] = pd.to_numeric(d["R"], errors="coerce")
    d["base_risk"] = pd.to_numeric(d["base_risk"], errors="coerce")
    d = d.dropna(subset=["R", "base_risk"])
    d = d[d.base_risk > 0]
    if len(d) == 0:
        return pd.DataFrame(columns=cols)
    # market-time month (NY+7), like the engine's month-end evaluation: an NR trade of the 1st market day closes
    # on the previous UTC day (21:00-23:59 UTC) but belongs to the new month
    d["month"] = utc_index_to_market(pd.DatetimeIndex(pd.to_datetime(d["time_utc"]))).to_period("M")
    rows, streak = {}, 0
    for m in pd.period_range(d.month.min(), d.month.max(), freq="M"):
        w = d[d.month <= m].tail(window)
        rw = float((w.base_risk * w.R).sum() / w.base_risk.sum())
        if len(w) < EDGE_MIN_TRADES:
            status = f"not evaluated (< {EDGE_MIN_TRADES} trades)"
        elif rw < 0:
            streak += 1
            status = "REVIEW" if streak >= 2 else "WARN"
        else:
            streak, status = 0, "ok"
        rows[str(m)] = {"trades": len(w), "rw": rw, "status": status}
    out = pd.DataFrame.from_dict(rows, orient="index")
    out.index.name = "month"
    return out


def write_report(out_dir: str | Path, monthly: pd.DataFrame, by_magic: pd.DataFrame, edge: pd.DataFrame,
                 title: str) -> str:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    monthly.to_csv(out / "monthly_returns.csv")
    by_magic.to_csv(out / "pnl_by_strategy.csv", index=False)
    edge.to_csv(out / "edge_monitor.csv")
    lines = [f"# {title}", ""]
    if len(monthly):
        r = monthly["return"].astype(float)
        comp = float(np.prod(1 + r.fillna(0)) ** (1 / len(r)) - 1)
        lines += [f"months: {len(r)}  ({monthly.index[0]} .. {monthly.index[-1]})",
                  f"compound monthly return (time-weighted): {comp:+.2%}",
                  f"best / worst month: {r.max():+.2%} / {r.min():+.2%}",
                  f"cash flows (deposits - withdrawals): {monthly['cash_flow'].sum():,.2f}",
                  f"backtest reference (stable5 2007-2022): rolling 12m 5th percentile {BACKTEST_12M_P5:+.1%}, "
                  f"worst 12m {BACKTEST_12M_WORST:+.1%}"]
        alarms = monthly[monthly["vs_backtest_12m"] != ""] if "vs_backtest_12m" in monthly else monthly.iloc[0:0]
        if len(alarms):
            lines.append("12-month windows outside the backtest range: "
                         + ", ".join(f"{m} {a.r12m:+.1%} ({a.vs_backtest_12m})" for m, a in alarms.iterrows()))
        show = monthly.copy()
        for c in ("return", "r3m", "r6m", "r12m"):
            if c in show:
                show[c] = (show[c] * 100).round(2)
        lines += ["", "## monthly (returns in %)", show.to_string()]
    else:
        lines.append("no deals in the history")
    lines += ["", "## P/L by strategy", by_magic.to_string(index=False) if len(by_magic) else "-"]
    lines += ["", "## R-based edge monitor (journal)", edge.round(4).to_string() if len(edge) else
              "no closed trades in the journal yet"]
    text = "\n".join(lines) + "\n"
    (out / "summary.txt").write_text(text, encoding="utf-8")
    return text
