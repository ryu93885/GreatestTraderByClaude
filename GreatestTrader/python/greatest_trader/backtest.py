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
    trades: pd.DataFrame
    equity: pd.Series  # realized equity after each closed trade
    initial_equity: float

    def monthly_returns(self) -> pd.Series:
        e = self.equity.copy()
        start = pd.Series([self.initial_equity], index=[e.index[0] - pd.Timedelta(seconds=1)])
        e = pd.concat([start, e])
        m = e.resample("ME").last().ffill()
        return m.pct_change().dropna()

    def stats(self) -> dict:
        t, e = self.trades, self.equity
        if len(t) == 0:
            return {"trades": 0}
        m = self.monthly_returns()
        full = pd.concat([pd.Series([self.initial_equity], index=[e.index[0] - pd.Timedelta(seconds=1)]), e])
        dd = (full / full.cummax() - 1).min()
        years = max((e.index[-1] - t.entry_time.min()).days / 365.25, 1e-9)
        cagr = (e.iloc[-1] / self.initial_equity) ** (1 / years) - 1
        wins, losses = t.pnl[t.pnl > 0].sum(), -t.pnl[t.pnl < 0].sum()
        return {
            "period": f"{t.entry_time.min():%Y-%m-%d} - {t.exit_time.max():%Y-%m-%d}",
            "trades": int(len(t)),
            "trades_per_month": len(t) / max(len(m), 1),
            "win_rate": float((t.R > 0).mean()),
            "avg_R": float(t.R.mean()),
            "profit_factor": float(wins / losses) if losses > 0 else float("inf"),
            "final_equity": float(e.iloc[-1]),
            "total_return": float(e.iloc[-1] / self.initial_equity - 1),
            "CAGR": float(cagr),
            "monthly_mean": float(m.mean()),
            "monthly_median": float(m.median()),
            "monthly_std": float(m.std()),
            "best_month": float(m.max()),
            "worst_month": float(m.min()),
            "positive_months": float((m > 0).mean()),
            "max_drawdown": float(dd),
            "sharpe_monthly": float(m.mean() / m.std() * np.sqrt(12)) if m.std() > 0 else float("nan"),
        }


# ---------------------------------------------------------------- engine
class _Pos:
    __slots__ = ("strategy", "sym", "dir", "j", "entry", "sl", "sl_dist", "risk_amt", "cost", "pip",
                 "entry_time", "exit_utc", "ticket")


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
                cols = ("close", "mid", "upper", "lower", "atr")
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
        last_day = None

        for ti, t in enumerate(timeline):
            day = tl_mkt[ti].date()
            if day != last_day:
                risk.update_equity(equity, day)
                last_day = day
            # 1) entries at this bar open
            while ent_ptr < n_ent and entries[ent_ptr][0] < t:
                ent_ptr += 1
            while ent_ptr < n_ent and entries[ent_ptr][0] == t:
                _, _, strat, s, j, req = entries[ent_ptr]
                ent_ptr += 1
                views = [PositionView(p.ticket, p.strategy, p.sym, p.dir, 1.0, p.entry, p.entry_time, p.sl, None)
                         for p in open_pos]
                ok, _why = risk.can_open(strat, s, views, equity)
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
                p.risk_amt = equity * risk.risk_fraction(strat, equity)
                slip = nr_cfg.slippage_pips if strat == "NR" else es_cfg.slippage_pips
                sig_i = j - 1 if strat == "NR" else j
                p.cost = (d["spr"][sig_i] + slip) * d["pip"]
                p.pip = d["pip"]
                p.entry_time = t.to_pydatetime()
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
                    risk.update_equity(equity, day)
                open_pos = still

        cols = ["strategy", "symbol", "direction", "entry_time", "exit_time", "entry_price", "exit_price",
                "sl_pips", "cost_pips", "R", "pnl", "equity", "exit_reason"]
        tr = pd.DataFrame(trades, columns=cols)
        eq = pd.Series(eq_v, index=pd.DatetimeIndex(eq_t), name="equity")
        eq = eq.groupby(level=0).last()
        return BacktestResult(tr, eq, cfg.backtest.initial_equity)
