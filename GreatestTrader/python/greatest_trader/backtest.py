"""Event-driven portfolio backtester.

Uses exactly the same strategy classes (NightReversion / EuroSession) and RiskManager
as the live engine. Fill model (conservative):
  * market entries at the open of the bar following the signal bar;
  * prices are BID; the round-trip cost (spread + commission + slippage) is deducted
    from every trade;
  * stop loss is checked before take profit inside a bar; gaps through the stop fill at
    the bar open;
  * PnL is expressed in R (multiples of the initial stop distance) and compounded with
    the risk fraction from the RiskManager (fixed-fractional sizing, no lot rounding).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from .config import Config
from .indicators import atr
from .risk import RiskManager, RiskState
from .strategies import EuroSession, NightReversion, PositionView, pip_size
from .timeutil import ServerClock

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- data loading
def read_bars(path: Path) -> pd.DataFrame:
    """Read M15 bars. Supported formats:
    * parquet / CSV with columns time, open, high, low, close[, spread_pips]   (download command)
    * MT5 "Export bars" CSV: <DATE> <TIME> <OPEN> <HIGH> <LOW> <CLOSE> <TICKVOL> <VOL> <SPREAD>
    Times are broker server time.
    """
    if path.suffix == ".parquet":
        df = pd.read_parquet(path)
    else:
        with open(path, "r", encoding="utf-8-sig") as f:
            head = f.readline()
        if "<DATE>" in head:
            df = pd.read_csv(path, sep="\t", encoding="utf-8-sig")
            df.columns = [c.strip("<>").lower() for c in df.columns]
            df["time"] = pd.to_datetime(df["date"] + " " + df["time"], format="%Y.%m.%d %H:%M:%S")
            if "spread" in df.columns:
                df["spread_points"] = df["spread"]
        else:
            df = pd.read_csv(path)
    if "time" in df.columns:
        df["time"] = pd.to_datetime(df["time"])
        df = df.set_index("time")
    df.index = pd.DatetimeIndex(df.index).tz_localize(None)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df


def load_data_dir(data_dir: str | Path, symbols: list[str]) -> dict[str, pd.DataFrame]:
    d = Path(data_dir)
    out = {}
    for s in symbols:
        for cand in (f"{s}_M15.parquet", f"{s}_M15.csv", f"{s}.csv", f"{s}_M15.txt"):
            p = d / cand
            if p.exists():
                out[s] = read_bars(p)
                break
        else:
            log.warning("no data for %s in %s (expected %s_M15.csv)", s, d, s)
    return out


# ---------------------------------------------------------------- result
@dataclass
class BacktestResult:
    """Backtest output.

    Return definitions (all calendar periods in broker server time = market time NY+7; for this
    system the month/year buckets are identical in Japan time because no position is ever open at
    a month end):
      * equity         : realized equity after each closed trade (timestamp = exit bar close).
      * monthly return : month-end equity / previous month-end equity - 1. The month before the
                         first month holds the initial equity, months without trades return 0%.
      * annual return  : year-end equity / previous year-end equity - 1 (first year: initial equity).
      * partial months : first/last month of the period when the data does not cover the whole month.
    """
    trades: pd.DataFrame
    equity: pd.Series  # realized equity after each closed trade
    initial_equity: float
    period_start: pd.Timestamp | None = None  # first bar of the backtest window (server time)
    period_end: pd.Timestamp | None = None  # last bar of the backtest window (server time)
    max_drawdown_mtm: float | None = None  # drawdown incl. floating P/L at every bar close
    mtm: pd.Series | None = None  # mark-to-market equity at every bar close

    # -------------------------------------------------------------- periods
    def _bounds(self) -> tuple[pd.Timestamp, pd.Timestamp] | None:
        """(first bar OPEN time, last bar CLOSE time) of the window."""
        e = self.equity
        if self.period_start is None and len(e) == 0:
            return None
        first = pd.Timestamp(self.period_start) if self.period_start is not None else e.index[0] - _BAR
        if self.period_end is not None:
            last = pd.Timestamp(self.period_end)
        else:
            last = e.index[-1] if len(e) else first
        if len(e):
            first, last = min(first, e.index[0] - _BAR), max(last, e.index[-1])
        return first, last

    def month_end_equity(self) -> pd.Series:
        """Realized equity at the end of every calendar month; the first row is the month BEFORE
        the first month and holds the initial equity. Equity is stamped at the exit-bar CLOSE, so a
        trade closed on the last bar of a month (close = 00:00 of the 1st) belongs to that month."""
        b = self._bounds()
        if b is None:
            return pd.Series(dtype=float)
        months = pd.period_range(b[0].to_period("M") - 1, _close_month(b[1]), freq="M")
        e = self.equity
        me = e.groupby(_close_month(e.index)).last() if len(e) else pd.Series(dtype=float)
        me = me.reindex(months)
        me.iloc[0] = self.initial_equity
        me = me.ffill()
        me.index = months.to_timestamp(how="end").normalize()
        return me.astype(float)

    def partial_months(self) -> list[pd.Period]:
        """First/last month of the window if the data does not cover all its business days
        (fixed FX holidays Jan 1 and Dec 25 are ignored)."""
        b = self._bounds()
        if b is None:
            return []
        out = []
        first, last = b
        m0, m1 = first.to_period("M"), _close_month(last)
        if _business_days(m0.start_time, first.normalize() - pd.Timedelta(days=1)) > 0:
            out.append(m0)
        last_day = (last - _EPS).normalize()
        if _business_days(last_day + pd.Timedelta(days=1), m1.end_time.normalize()) > 0 and m1 not in out:
            out.append(m1)
        return out

    def monthly_returns(self) -> pd.Series:
        me = self.month_end_equity()
        if len(me) < 2:
            return pd.Series(dtype=float)
        return (me / me.shift(1) - 1).iloc[1:]

    def annual_returns(self) -> pd.DataFrame:
        """Per calendar year: start equity, end equity, annual return, months covered, partial flag."""
        me = self.month_end_equity()
        if len(me) < 2:
            return pd.DataFrame(columns=["start_equity", "end_equity", "return", "months", "partial"])
        partial = {p.year for p in self.partial_months()}
        rows = []
        prev = float(me.iloc[0])
        body = me.iloc[1:]
        for y, grp in body.groupby(body.index.year):
            end = float(grp.iloc[-1])
            rows.append((int(y), prev, end, end / prev - 1, len(grp), y in partial or len(grp) < 12))
            prev = end
        df = pd.DataFrame(rows, columns=["year", "start_equity", "end_equity", "return", "months", "partial"])
        return df.set_index("year")

    # -------------------------------------------------------------- stats
    def stats(self) -> dict:
        t, e = self.trades, self.equity
        if len(t) == 0 or len(e) == 0:
            return {"trades": 0}
        m_all = self.monthly_returns()
        partial_p = self.partial_months()
        partial = {p.to_timestamp(how="end").normalize() for p in partial_p}
        m = m_all[~m_all.index.isin(partial)]  # distribution stats on complete months only
        full = pd.concat([pd.Series([self.initial_equity], index=[e.index[0] - _EPS]), e])
        dd = (full / full.cummax() - 1).min()
        first, last = self._bounds()
        years = (last - first).total_seconds() / (365.25 * 86400)
        final = float(e.iloc[-1])
        ratio = final / self.initial_equity
        if years < 1 / 365 or ratio <= 0:
            cagr = float("nan")
        else:
            cagr = float(np.power(np.float64(ratio), 1.0 / years) - 1)
        exit_month = pd.to_datetime(t.exit_time).dt.to_period("M")
        complete_trades = int((~exit_month.isin(partial_p)).sum())
        wins, losses = t.pnl[t.pnl > 0].sum(), -t.pnl[t.pnl < 0].sum()
        out = {
            "period": f"{first:%Y-%m-%d} - {(last - _EPS):%Y-%m-%d}",
            "trades": int(len(t)),
            "months": int(len(m_all)),
            "complete_months": int(len(m)),
            "trades_per_month": complete_trades / len(m) if len(m) else float("nan"),
            "win_rate": float((t.R > 0).mean()),
            "avg_R": float(t.R.mean()),
            "profit_factor": float(wins / losses) if losses > 0 else float("inf"),
            "final_equity": final,
            "total_return": float(ratio - 1),
            "CAGR": cagr,
            "monthly_compound": float(np.power(1 + cagr, 1 / 12) - 1) if np.isfinite(cagr) else float("nan"),
            "monthly_mean": float(m.mean()),
            "monthly_geometric": _geo_mean(m.to_numpy()),
            "monthly_median": float(m.median()),
            "monthly_std": float(m.std()),
            "best_month": float(m.max()),
            "worst_month": float(m.min()),
            "positive_months": float((m > 0).mean()),
            "max_drawdown": float(dd),
            "sharpe_monthly": float(m.mean() / m.std() * np.sqrt(12)) if m.std() > 0 else float("nan"),
        }
        if self.max_drawdown_mtm is not None:
            out["max_drawdown_mtm"] = float(min(self.max_drawdown_mtm, dd))
        return out


_BAR = pd.Timedelta(minutes=15)
_EPS = pd.Timedelta(seconds=1)
_FX_HOLIDAYS = {(1, 1), (12, 25)}


def _close_month(ts):
    """Calendar month of a bar-CLOSE timestamp (a close at 00:00 of the 1st belongs to the previous month)."""
    if isinstance(ts, pd.DatetimeIndex):
        return (ts - _EPS).to_period("M")
    return (pd.Timestamp(ts) - _EPS).to_period("M")


def _geo_mean(r: np.ndarray) -> float:
    if len(r) == 0:
        return float("nan")
    prod = float(np.prod(1 + r))
    return prod ** (1 / len(r)) - 1 if prod > 0 else float("nan")


def _business_days(a: pd.Timestamp, b: pd.Timestamp) -> int:
    if a > b:
        return 0
    return sum(1 for d in pd.bdate_range(a, b) if (d.month, d.day) not in _FX_HOLIDAYS)


# ---------------------------------------------------------------- engine
class _Pos:
    __slots__ = ("strategy", "sym", "dir", "j", "entry", "sl", "sl_dist", "risk_amt", "cost", "pip",
                 "entry_time", "exit_utc", "ticket", "last_c")


class Backtester:
    def __init__(self, cfg: Config, data: dict[str, pd.DataFrame], clock: ServerClock | None = None):
        self.cfg = cfg
        self.data = data
        mode = cfg.broker.server_time_mode
        self.clock = clock or ServerClock("ny7" if mode == "auto" else mode, cfg.broker.server_gmt_offset)
        self.nr = NightReversion(cfg.night_reversion)
        self.es = EuroSession(cfg.euro_session)

    def _slice(self, df: pd.DataFrame) -> pd.DataFrame:
        b = self.cfg.backtest
        if b.start:
            df = df[df.index >= pd.Timestamp(b.start) - pd.Timedelta(days=10)]
        if b.end:
            df = df[df.index < pd.Timestamp(b.end) + pd.Timedelta(days=1)]
        return df

    def _spread(self, sym: str, df: pd.DataFrame, pip: float) -> np.ndarray:
        if "spread_pips" in df.columns:
            sp = df["spread_pips"].to_numpy(float)
        elif "spread_points" in df.columns:
            point = pip / 10.0  # 5/3-digit quotes
            sp = df["spread_points"].to_numpy(float) * point / pip
        else:
            sp = np.full(len(df), np.nan)
        default = self.cfg.backtest.spreads_pips.get(sym, 2.0)
        sp = np.where(np.isfinite(sp) & (sp > 0), sp, default)
        return sp + self.cfg.backtest.commission_pips

    def run(self) -> BacktestResult:
        cfg = self.cfg
        start_ts = pd.Timestamp(cfg.backtest.start) if cfg.backtest.start else None
        sym_data = {}
        entries: list[tuple] = []  # (time, order, strategy, sym, bar_idx, EntryRequest)

        # ---- Night Reversion signals (same code path as live: entry_signal on each closed bar)
        if cfg.night_reversion.enabled:
            for s in cfg.night_reversion.symbols:
                if s not in self.data:
                    continue
                df = self.nr.prepare(self._slice(self.data[s]))
                pip = pip_size(s)
                spr = self._spread(s, df, pip)
                mkt = self.clock.server_index_to_market(df.index)
                sym_data[s] = {"df": df, "pip": pip, "spr": spr, "mkt": mkt,
                               "o": df.open.to_numpy(float), "h": df.high.to_numpy(float),
                               "l": df.low.to_numpy(float), "c": df.close.to_numpy(float),
                               "mid": df.mid.to_numpy(float)}
                hrs = np.asarray(mkt.hour)
                cand = np.where(np.isin(hrs, cfg.night_reversion.entry_hours)
                                & ((df.close > df.upper) | (df.close < df.lower)).to_numpy())[0]
                cols = ("close", "mid", "upper", "lower", "atr", "high", "low")
                arrs = {k: df[k].to_numpy(float) for k in cols}
                for i in cand:
                    if i + 1 >= len(df):
                        continue
                    bar = {k: arrs[k][i] for k in cols}
                    req = self.nr.entry_signal(s, bar, df.index[i].to_pydatetime(), mkt[i].to_pydatetime(),
                                               pip, spr[i] - 0.0, 0.0)
                    if req is not None:
                        entries.append((df.index[i + 1], 0, "NR", s, i + 1, req))

        # ---- Euro Session signals
        es_sym = cfg.euro_session.symbol
        if cfg.euro_session.enabled and es_sym in self.data:
            df = self._slice(self.data[es_sym])
            if es_sym not in sym_data:
                pip = pip_size(es_sym)
                sym_data[es_sym] = {"df": df, "pip": pip, "spr": self._spread(es_sym, df, pip),
                                    "mkt": self.clock.server_index_to_market(df.index),
                                    "o": df.open.to_numpy(float), "h": df.high.to_numpy(float),
                                    "l": df.low.to_numpy(float), "c": df.close.to_numpy(float),
                                    "mid": np.full(len(df), np.nan)}
            d = sym_data[es_sym]
            df = d["df"]
            utc = self.clock.server_index_to_utc(df.index)
            d["utc"] = utc
            h1 = df[["open", "high", "low", "close"]].resample("1h").agg(
                {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
            a_h1 = atr(h1.high, h1.low, h1.close, cfg.euro_session.atr_period_h1)
            # ATR of the last COMPLETED H1 bar at each M15 bar open: H1 bar starting 1h before the hour
            key = df.index.floor("h") - pd.Timedelta(hours=1)
            atr_at = a_h1.reindex(key).to_numpy(float)
            cand = np.where((np.asarray(utc.hour) == cfg.euro_session.entry_utc_hour)
                            & (np.asarray(utc.minute) == 0))[0]
            for i in cand:
                req = self.es.entry_signal(utc[i].to_pydatetime(), df.index[i].to_pydatetime(), atr_at[i],
                                           d["pip"], d["spr"][i] - cfg.backtest.commission_pips)
                if req is not None:
                    entries.append((df.index[i], 1, "ES", es_sym, i, req))

        if not entries:
            return BacktestResult(pd.DataFrame(), pd.Series(dtype=float), cfg.backtest.initial_equity)

        entries.sort(key=lambda x: (x[0], x[1], x[3]))
        if start_ts is not None:
            entries = [e for e in entries if e[0] >= start_ts]
        timeline = pd.DatetimeIndex(np.unique(np.concatenate(
            [v["df"].index.to_numpy(dtype="datetime64[ns]") for v in sym_data.values()])))
        if start_ts is not None:
            timeline = timeline[timeline >= start_ts]
        # per-symbol position of each timeline stamp
        pos_map = {s: v["df"].index.get_indexer(timeline) for s, v in sym_data.items()}
        tl_mkt = self.clock.server_index_to_market(timeline)

        risk = RiskManager(cfg.risk, RiskState())
        equity = cfg.backtest.initial_equity
        risk.update_equity(equity, tl_mkt[0].date())
        open_pos: list[_Pos] = []
        trades = []
        eq_t, eq_v = [], []
        ent_ptr = 0
        n_ent = len(entries)
        ticket = 0
        nr_cfg, es_cfg = cfg.night_reversion, cfg.euro_session
        mtm_peak, mtm_dd = equity, 0.0
        mtm_arr = np.empty(len(timeline))
        last_mtm = equity

        for ti, t in enumerate(timeline):
            day = tl_mkt[ti].date()
            # 1) entries at this bar open
            while ent_ptr < n_ent and entries[ent_ptr][0] < t:
                ent_ptr += 1
            while ent_ptr < n_ent and entries[ent_ptr][0] == t:
                _, _, strat, s, j, req = entries[ent_ptr]
                ent_ptr += 1
                views = [PositionView(p.ticket, p.strategy, p.sym, p.dir, 1.0, p.entry, p.entry_time, p.sl, None,
                                      risk_amount=p.risk_amt) for p in open_pos]
                rfrac = risk.risk_fraction(strat, last_mtm) * risk.symbol_mult(s)
                ok, _why = risk.can_open(strat, s, views, last_mtm, req.direction, equity * rfrac)
                if not ok:
                    continue
                d = sym_data[s]
                entry = d["o"][j]
                if strat == "NR" and NightReversion.favorable_tp(req.direction, entry, req.tp_price) is None:
                    continue
                p = _Pos()
                ticket += 1
                p.ticket, p.strategy, p.sym, p.dir, p.j = ticket, strat, s, req.direction, j
                p.entry, p.sl_dist = entry, req.sl_distance
                p.sl = entry - req.direction * req.sl_distance
                # sized on realized equity (live sizes on account equity incl. floating P/L; the difference
                # is negligible here: stable5 5.38% vs 5.39%/month when sized on mark-to-market equity)
                p.risk_amt = equity * rfrac
                slip = nr_cfg.slippage_pips if strat == "NR" else es_cfg.slippage_pips
                sig_i = j - 1 if strat == "NR" else j
                p.cost = (d["spr"][sig_i] + slip) * d["pip"]
                p.pip = d["pip"]
                p.entry_time = t.to_pydatetime()
                p.last_c = entry
                if strat == "ES":
                    u = d["utc"][j]
                    p.exit_utc = u.replace(hour=es_cfg.exit_utc_hour, minute=0)
                open_pos.append(p)

            # 2) process this bar for open positions
            if open_pos:
                still = []
                for p in open_pos:
                    k = pos_map[p.sym][ti]
                    if k < 0:
                        still.append(p)
                        continue
                    d = sym_data[p.sym]
                    o, h, l, c = d["o"][k], d["h"][k], d["l"][k], d["c"][k]
                    exitp, why = None, ""
                    first = k == p.j
                    if p.dir > 0 and l <= p.sl:
                        exitp, why = (p.sl if first else min(p.sl, o)), "SL"
                    elif p.dir < 0 and h >= p.sl:
                        exitp, why = (p.sl if first else max(p.sl, o)), "SL"
                    if exitp is None and p.strategy == "NR":
                        tp = NightReversion.favorable_tp(p.dir, p.entry, d["mid"][k - 1])
                        if tp is not None:
                            if p.dir > 0 and h >= tp:
                                exitp, why = (tp if first else max(tp, o)), "TP"
                            elif p.dir < 0 and l <= tp:
                                exitp, why = (tp if first else min(tp, o)), "TP"
                        if exitp is None:
                            mk = d["mkt"][k]
                            if k - p.j + 1 >= nr_cfg.max_hold_bars:
                                exitp, why = c, "time"
                            elif mk.dayofweek == 4 and mk.hour >= 22:
                                exitp, why = c, "friday"
                    if exitp is None and p.strategy == "ES":
                        if d["utc"][k] + timedelta(minutes=15) >= p.exit_utc:
                            exitp, why = c, "session end"
                    if exitp is None:
                        still.append(p)
                        continue
                    move = p.dir * (exitp - p.entry) - p.cost
                    R = move / p.sl_dist
                    pnl = p.risk_amt * R
                    equity += pnl
                    exit_time = d["df"].index[k]
                    trades.append((p.strategy, p.sym, p.dir, p.entry_time, exit_time.to_pydatetime(), p.entry,
                                   exitp, p.sl_dist / p.pip, p.cost / p.pip, R, pnl, equity, why))
                    eq_t.append(exit_time + pd.Timedelta(minutes=15))
                    eq_v.append(equity)
                open_pos = still

            # 3) mark-to-market equity at this bar close (for the floating drawdown)
            if open_pos:
                fl = 0.0
                for p in open_pos:
                    k = pos_map[p.sym][ti]
                    if k >= 0:
                        p.last_c = sym_data[p.sym]["c"][k]
                    fl += p.risk_amt * (p.dir * (p.last_c - p.entry) - p.cost) / p.sl_dist
                mtm = equity + fl
            else:
                mtm = equity
            # 4) portfolio guards on mark-to-market equity (hard stops close everything at this bar close)
            risk.update_equity(mtm, day)
            if risk.flatten_reason and open_pos:
                why = "month stop" if "month" in risk.flatten_reason else "day stop"
                for p in open_pos:
                    exitp = p.last_c
                    move = p.dir * (exitp - p.entry) - p.cost
                    R = move / p.sl_dist
                    pnl = p.risk_amt * R
                    equity += pnl
                    trades.append((p.strategy, p.sym, p.dir, p.entry_time, t.to_pydatetime(), p.entry, exitp,
                                   p.sl_dist / p.pip, p.cost / p.pip, R, pnl, equity, why))
                    eq_t.append(t + pd.Timedelta(minutes=15))
                    eq_v.append(equity)
                open_pos = []
                mtm = equity
            last_mtm = mtm
            mtm_arr[ti] = mtm
            if mtm > mtm_peak:
                mtm_peak = mtm
            elif mtm / mtm_peak - 1 < mtm_dd:
                mtm_dd = mtm / mtm_peak - 1

        cols = ["strategy", "symbol", "direction", "entry_time", "exit_time", "entry_price", "exit_price",
                "sl_pips", "cost_pips", "R", "pnl", "equity", "exit_reason"]
        tr = pd.DataFrame(trades, columns=cols)
        eq = pd.Series(eq_v, index=pd.DatetimeIndex(eq_t), name="equity")
        eq = eq.groupby(level=0).last()
        # the window starts at the requested start unless the data begins clearly later (> 4 days)
        period_start = timeline[0]
        if cfg.backtest.start and timeline[0] - pd.Timestamp(cfg.backtest.start) <= pd.Timedelta(days=4):
            period_start = pd.Timestamp(cfg.backtest.start)
        return BacktestResult(tr, eq, cfg.backtest.initial_equity, period_start=period_start,
                              period_end=timeline[-1] + pd.Timedelta(minutes=15), max_drawdown_mtm=mtm_dd,
                              mtm=pd.Series(mtm_arr, index=timeline + pd.Timedelta(minutes=15), name="mtm"))
