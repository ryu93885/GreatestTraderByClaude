from datetime import datetime, timedelta

import pandas as pd

import logging

import numpy as np
import pytest

from fakes import FakeBroker, ny7_utc, step_until, synthetic_m15
from greatest_trader.backtest import Backtester, _FxConverter
from greatest_trader.broker.base import OrderResult
from greatest_trader.config import Config, load_config
from greatest_trader.engine import LiveEngine
from greatest_trader.notify import Notifier
from greatest_trader.timeutil import ServerClock

SPIKE = datetime(2021, 3, 3, 0, 30)  # Wednesday, market time (= server time for ny7 brokers)


def make_cfg(tmp_path, dry_run=False):
    cfg = Config()
    cfg.night_reversion.symbols = ["EURJPY"]
    cfg.euro_session.enabled = False
    cfg.engine.dry_run = dry_run
    cfg.engine.state_file = str(tmp_path / "state.json")
    cfg.engine.journal_dir = str(tmp_path / "journal")
    cfg.broker.server_time_mode = "ny7"
    cfg.risk.blackout_start = cfg.risk.blackout_end = ""  # synthetic data starts in early January
    return cfg


def make_engine(cfg, data, equity=1_000_000):
    broker = FakeBroker(data, equity=equity)
    eng = LiveEngine(cfg, broker, Notifier(cfg.notify), utcnow=ny7_utc(broker))
    eng.clock = ServerClock("ny7")
    return eng, broker


def test_live_engine_opens_manages_and_closes(tmp_path):
    data = {"EURJPY": synthetic_m15(datetime(2021, 3, 1), 5, 130.0, 0.01, spike_at=SPIKE)}
    eng, broker = make_engine(make_cfg(tmp_path), data)
    step_until(eng, broker, datetime(2021, 3, 2, 22, 0), datetime(2021, 3, 3, 6, 0))
    opens = [x for x in broker.log if x[0] == "open"]
    shorts = [x for x in opens if x[2] == -1]
    assert shorts, broker.log
    _, sym, direction, volume, sl, tp = shorts[0]
    assert sym == "EURJPY"
    assert sl > tp  # short: stop above, target (moving average) below
    assert volume > 0
    assert not broker.positions_  # all closed (TP / engine exit / time stop) by 06:00


def test_live_engine_dry_run_sends_nothing(tmp_path):
    data = {"EURJPY": synthetic_m15(datetime(2021, 3, 1), 5, 130.0, 0.01, spike_at=SPIKE)}
    eng, broker = make_engine(make_cfg(tmp_path, dry_run=True), data)
    step_until(eng, broker, datetime(2021, 3, 2, 22, 0), datetime(2021, 3, 3, 6, 0))
    assert broker.log == []


def test_live_engine_retries_flatten_while_hard_stop_active(tmp_path):
    data = {"EURJPY": synthetic_m15(datetime(2021, 3, 1), 5, 130.0, 0.01, spike_at=SPIKE)}
    cfg = make_cfg(tmp_path)
    cfg.risk.monthly_hard_stop = 0.03
    eng, broker = make_engine(cfg, data)
    broker.set_time(datetime(2021, 3, 2, 22, 0, 5))
    eng.step()  # month start equity = 1,000,000
    broker.open_position("EURJPY", 1, 0.1, 129.0, None, 7101, "GT_NR")
    real_close, fails = broker.close_position, []

    def failing_close(ticket, base, comment=""):  # first attempt is rejected (e.g. requote)
        if not fails:
            fails.append(comment)
            return OrderResult(False, ticket, message="requote")
        return real_close(ticket, base, comment)

    broker.close_position = failing_close
    broker.equity = 960_000  # -4% month to date -> monthly hard stop
    broker.set_time(datetime(2021, 3, 2, 22, 15, 5))
    eng.step()
    assert fails and broker.positions_  # the stop fired but the close failed
    broker.set_time(datetime(2021, 3, 2, 22, 15, 10))
    eng.step()
    assert not broker.positions_  # retried on the next loop
    assert any(x[0] == "close" and "monthly hard stop" in x[2] for x in broker.log)


def test_unpriced_open_position_fills_the_total_risk_cap(tmp_path):
    data = {"EURJPY": synthetic_m15(datetime(2021, 3, 1), 5, 130.0, 0.01, spike_at=SPIKE)}
    eng, broker = make_engine(make_cfg(tmp_path), data)
    broker.set_time(datetime(2021, 3, 2, 22, 0, 5))
    broker.open_position("EURJPY", 1, 0.1, 129.0, None, 7101, "GT_NR")

    def broken(*a, **k):
        raise RuntimeError("no price")

    broker.loss_per_lot = broken
    assert eng._open_risk(broker.positions(eng.magics))[0].risk_amount == 0.0  # cap off: not priced at all
    eng.risk.cfg.max_total_risk = 0.05
    views = eng._open_risk(broker.positions(eng.magics))
    assert views[0].risk_amount == float("inf")
    assert not eng.risk.can_open("NR", "GBPJPY", views, 1_000_000, -1, 1000)[0]


def test_live_engine_ignores_stale_bar_on_startup(tmp_path):
    data = {"EURJPY": synthetic_m15(datetime(2021, 3, 1), 5, 130.0, 0.01, spike_at=SPIKE)}
    eng, broker = make_engine(make_cfg(tmp_path), data)
    broker.set_time(SPIKE + timedelta(minutes=15, seconds=400))  # signal bar closed 6+ minutes ago
    eng.step()
    assert broker.log == []


def test_backtest_matches_live_engine(tmp_path):
    """The live engine (driven bar by bar through a fake broker) and the backtester must take
    the same entries at the same bars."""
    data = {"EURJPY": synthetic_m15(datetime(2021, 3, 1), 5, 130.0, 0.01, spike_at=SPIKE)}
    cfg = make_cfg(tmp_path)
    cfg.backtest.spreads_pips = {"EURJPY": 1.0}
    res = Backtester(cfg, data).run()
    t = res.trades
    spike_trade = t[pd.to_datetime(t.entry_time) == pd.Timestamp(SPIKE + timedelta(minutes=15))]
    assert len(spike_trade) == 1 and spike_trade.iloc[0].direction == -1  # entry at next bar open
    assert (t.cost_pips == 1.5).all()  # 1.0 spread + 0.5 slippage

    eng, broker = make_engine(make_cfg(tmp_path), data)
    opened = []
    orig = broker.open_position

    def rec(base, direction, volume, sl, tp, magic, comment):
        opened.append((pd.Timestamp(broker.now).floor("15min"), direction))
        return orig(base, direction, volume, sl, tp, magic, comment)

    broker.open_position = rec
    step_until(eng, broker, datetime(2021, 3, 1, 0, 0), datetime(2021, 3, 5, 23, 45))
    bt = [(pd.Timestamp(r.entry_time), int(r.direction)) for r in t.itertuples()]
    assert opened == bt


def test_backtest_matches_live_engine_when_guards_compete(tmp_path):
    """Several pairs signal on the same bars and the stable5 guards (total risk, same direction) can admit
    only some of them: live trading must pick exactly the same trades as the backtester, even when the
    configured symbol order is not alphabetical."""
    syms = [("EURJPY", 130.0, 0.01), ("GBPJPY", 150.0, 0.01), ("CHFJPY", 115.0, 0.01),
            ("GBPCAD", 1.70, 0.0001), ("EURCAD", 1.50, 0.0001), ("AUDCAD", 0.95, 0.0001)]
    data = {s: synthetic_m15(datetime(2021, 3, 1), 8, b, p, seed=7 + k) for k, (s, b, p) in enumerate(syms)}
    cfg = load_config(None, preset="stable5")
    cfg.night_reversion.symbols = list(data)  # not alphabetical
    cfg.euro_session.enabled = False
    cfg.engine.dry_run = False
    cfg.engine.state_file = str(tmp_path / "state.json")
    cfg.engine.journal_dir = str(tmp_path / "journal")
    cfg.broker.server_time_mode = "ny7"
    cfg.risk.blackout_start = cfg.risk.blackout_end = ""
    # the fake broker's equity is constant: switch off the equity-driven guards on both sides
    cfg.risk.dd_scale_full = cfg.risk.daily_loss_limit = cfg.risk.monthly_hard_stop = 0.0
    cfg.backtest.spreads_pips = {s: 1.0 for s in data}
    res = Backtester(cfg, data).run()
    bt = sorted((pd.Timestamp(r.entry_time), r.symbol, int(r.direction)) for r in res.trades.itertuples())

    eng, broker = make_engine(cfg, data)
    opened = []
    orig = broker.open_position

    def rec(base, direction, volume, sl, tp, magic, comment):
        opened.append((pd.Timestamp(broker.now).floor("15min"), base, direction))
        return orig(base, direction, volume, sl, tp, magic, comment)

    broker.open_position = rec
    step_until(eng, broker, datetime(2021, 3, 1, 0, 0), datetime(2021, 3, 8, 23, 45))
    assert len(bt) >= 10
    # several bars where the guards had to choose between competing signals
    assert sum(1 for k in {t for t, _, _ in bt} if sum(1 for t, _, _ in bt if t == k) >= 2) >= 3
    assert sorted(opened) == bt


def test_backtest_es_times(tmp_path):
    # EURUSD, server = market time; 07:00 UTC = 09:00 server in winter
    data = {"EURUSD": synthetic_m15(datetime(2021, 1, 4), 5, 1.2, 0.0001)}
    cfg = make_cfg(tmp_path)
    cfg.night_reversion.enabled = False
    cfg.euro_session.enabled = True
    res = Backtester(cfg, data).run()
    t = res.trades
    assert len(t) >= 4
    for _, r in t.iterrows():
        assert pd.Timestamp(r.entry_time).hour == 9 and pd.Timestamp(r.entry_time).minute == 0
        if r.exit_reason == "session end":
            assert pd.Timestamp(r.exit_time).hour == 14 and pd.Timestamp(r.exit_time).minute == 45


def test_backtest_mtm_drawdown_not_smaller_than_realized(tmp_path):
    data = {"EURJPY": synthetic_m15(datetime(2021, 3, 1), 10, 130.0, 0.01, spike_at=SPIKE)}
    cfg = make_cfg(tmp_path)
    res = Backtester(cfg, data).run()
    st = res.stats()
    assert st["max_drawdown_mtm"] <= st["max_drawdown"] <= 0
    m = res.monthly_returns()
    assert abs(float((1 + m).prod()) - res.equity.iloc[-1] / res.initial_equity) < 1e-9


def test_mtm_drawdown_matches_independent_reconstruction(tmp_path):
    """Rebuild the mark-to-market equity from trades.csv + bar closes and compare the max drawdown
    exactly (catches sign errors, missing floating P/L or missing costs)."""
    data = {"EURJPY": synthetic_m15(datetime(2021, 3, 1), 10, 130.0, 0.01, spike_at=SPIKE)}
    cfg = make_cfg(tmp_path)
    res = Backtester(cfg, data).run()
    t = res.trades.copy()
    assert len(t) > 3
    bars = data["EURJPY"]
    pip = 0.01
    t["risk_amt"] = t.pnl / t.R
    eq0 = cfg.backtest.initial_equity
    mtm = []
    for ts, row in bars[bars.index >= res.period_start].iterrows():
        realized = eq0 + t.loc[pd.to_datetime(t.exit_time) <= ts, "pnl"].sum()
        live = t[(pd.to_datetime(t.entry_time) <= ts) & (pd.to_datetime(t.exit_time) > ts)]
        fl = (live.risk_amt * (live.direction * (row.close - live.entry_price) - live.cost_pips * pip)
              / (live.sl_pips * pip)).sum()
        mtm.append(realized + fl)
    s = pd.Series(mtm)
    dd = float((s / s.cummax().clip(lower=eq0) - 1).min())
    assert abs(dd - res.max_drawdown_mtm) < 1e-9


def _three_jpy_crosses():
    syms = [("EURJPY", 130.0, 0.01), ("GBPJPY", 150.0, 0.01), ("CHFJPY", 115.0, 0.01)]
    return {s: synthetic_m15(datetime(2021, 3, 1), 8, b, p, seed=7 + k) for k, (s, b, p) in enumerate(syms)}


def _stable5_flat_equity_cfg(tmp_path, data):
    cfg = load_config(None, preset="stable5")
    cfg.night_reversion.symbols = list(data)
    cfg.euro_session.enabled = False
    cfg.engine.dry_run = False
    cfg.engine.state_file = str(tmp_path / "state.json")
    cfg.engine.journal_dir = str(tmp_path / "journal")
    cfg.broker.server_time_mode = "ny7"
    cfg.risk.blackout_start = cfg.risk.blackout_end = ""
    # the fake broker's equity is constant: switch off the equity-driven guards on both sides
    cfg.risk.dd_scale_full = cfg.risk.daily_loss_limit = cfg.risk.monthly_hard_stop = 0.0
    cfg.backtest.spreads_pips = {s: 1.0 for s in data}
    return cfg


def test_backtest_matches_live_engine_with_lot_cap(tmp_path):
    """Large account: max_lots_per_order binds on every trade. Live and backtest must take the same trades
    with the same (capped) volume; the total-risk cap sees the capped risk on both sides."""
    data = _three_jpy_crosses()
    cfg = _stable5_flat_equity_cfg(tmp_path, data)
    cfg.risk.max_lots_per_order = 3.0
    cfg.backtest.account_currency = "JPY"  # the fake broker prices JPY crosses in JPY
    cfg.backtest.initial_equity = 1e9
    res = Backtester(cfg, data).run()
    t = res.trades
    assert len(t) >= 5 and (t.lots_uncapped > 3.0).all() and (abs(t.lots - 3.0) < 1e-9).all()
    assert np.allclose(t.risk_amount, 3.0 * 100000 * t.sl_pips * 0.01)  # 3 lots x loss of 1 lot at the stop
    bt = sorted((pd.Timestamp(r.entry_time), r.symbol, int(r.direction), 3.0) for r in t.itertuples())

    eng, broker = make_engine(cfg, data, equity=1e9)
    opened = []
    orig = broker.open_position

    def rec(base, direction, volume, sl, tp, magic, comment):
        opened.append((pd.Timestamp(broker.now).floor("15min"), base, direction, volume))
        return orig(base, direction, volume, sl, tp, magic, comment)

    broker.open_position = rec
    step_until(eng, broker, datetime(2021, 3, 1, 0, 0), datetime(2021, 3, 8, 23, 45))
    assert sorted(opened) == bt
    caps = [m for m in eng.notifier.recent if m.startswith("lot cap: intended")]
    assert caps and "sending 3 " in caps[0]
    # notified at most once per market day and symbol, although every trade was capped
    assert len(caps) == len({(o[0].date(), o[1]) for o in opened})


def test_backtest_caps_off_and_unbound_cap_give_identical_trades(tmp_path):
    data = _three_jpy_crosses()
    cfg = _stable5_flat_equity_cfg(tmp_path, data)
    cfg.backtest.account_currency = "JPY"
    cfg.backtest.initial_equity = 1_000_000  # ~0.1 lot trades: a 20-lot cap never binds
    capped = Backtester(cfg, data).run().trades
    cfg.risk.max_lots_per_order = 0
    off = Backtester(cfg, data).run().trades
    cols = ["entry_time", "symbol", "pnl", "equity"]
    assert len(off) > 5 and off[cols].equals(capped[cols])


def test_fx_converter_usd_and_jpy_accounts():
    idx = pd.date_range("2021-03-01", periods=4, freq="15min")

    def px(v):
        return pd.DataFrame({"open": [v] * 4}, index=idx)

    fx = _FxConverter({"USDJPY": px(110.0), "GBPUSD": px(1.40), "GBPCAD": px(1.75), "EURJPY": px(130.0)})
    t = idx.to_numpy(dtype="datetime64[ns]")
    assert np.allclose(fx.rate("JPY", "USD", t), 1 / 110.0)
    assert np.allclose(fx.rate("CAD", "USD", t), 1.40 / 1.75)  # triangulated through GBP
    assert np.allclose(fx.rate("CAD", "JPY", t), 1.40 / 1.75 * 110.0)
    assert np.allclose(fx.rate("EUR", "JPY", t), 130.0)  # direct pair
    assert np.isnan(fx.rate("CHF", "USD", t)).all()  # nothing to convert CHF with


def test_backtest_missing_conversion_pair_leaves_symbol_uncapped(tmp_path, caplog):
    data = {"EURJPY": synthetic_m15(datetime(2021, 3, 1), 5, 130.0, 0.01, spike_at=SPIKE)}
    cfg = make_cfg(tmp_path)
    cfg.backtest.spreads_pips = {"EURJPY": 1.0}
    cfg.backtest.initial_equity = 1e12  # would be capped if JPY could be converted to USD
    with caplog.at_level(logging.WARNING, logger="greatest_trader.backtest"):
        res = Backtester(cfg, data).run()
    assert any("EURJPY" in r.message and "NOT applied" in r.message for r in caplog.records)
    t = res.trades
    assert len(t) and t.lots.isna().all()
    assert t.iloc[0].risk_amount == pytest.approx(1e12 * cfg.risk.base_risk("nr"))  # uncapped


def test_fx_converter_falls_back_when_the_direct_pair_is_stale():
    """A direct pair without a fresh price (its data ended, a gap) gives way to the route through USD instead of
    returning NaN (a JPY account kept USDCHF trades uncapped after the CHFJPY data ended in 2019)."""
    idx = pd.date_range("2021-03-01", periods=30, freq="1D")

    def px(v, n=30):
        return pd.DataFrame({"open": [v] * n}, index=idx[:n])

    fx = _FxConverter({"USDJPY": px(110.0), "USDCHF": px(0.90), "CHFJPY": px(122.0, 1)})
    t = idx[20:24].to_numpy(dtype="datetime64[ns]")  # 19+ days after the last CHFJPY bar
    assert np.allclose(fx.rate("CHF", "JPY", t), 110.0 / 0.90)
    early = idx[:1].to_numpy(dtype="datetime64[ns]")
    assert np.allclose(fx.rate("CHF", "JPY", early), 122.0)  # the fresh direct pair wins
    # a direct pair 3 days old gives way to a fresh route through USD; with no other route it is still used
    fx2 = _FxConverter({"USDJPY": px(110.0), "GBPUSD": px(1.40), "GBPJPY": px(150.0, 2)})
    t3 = idx[4:5].to_numpy(dtype="datetime64[ns]")
    assert np.allclose(fx2.rate("GBP", "JPY", t3), 1.40 * 110.0)
    fx3 = _FxConverter({"GBPJPY": px(150.0, 2)})
    assert np.allclose(fx3.rate("GBP", "JPY", t3), 150.0)


def test_jpy_account_lot_cap_after_the_conversion_pair_ends(caplog):
    start = datetime(2021, 3, 1)
    data = {"USDCHF": synthetic_m15(start, 10, 0.90, 0.0001, seed=3),
            "USDJPY": synthetic_m15(start, 10, 110.0, 0.01, seed=4),
            "CHFJPY": synthetic_m15(start, 2, 122.0, 0.01, seed=5)}  # the direct conversion pair ends early
    cfg = Config()
    cfg.night_reversion.symbols = ["USDCHF"]
    cfg.euro_session.enabled = False
    cfg.broker.server_time_mode = "ny7"
    cfg.risk.blackout_start = cfg.risk.blackout_end = ""
    cfg.backtest.account_currency = "JPY"
    cfg.backtest.initial_equity = 1e12  # every trade far above the 20-lot cap
    with caplog.at_level(logging.WARNING, logger="greatest_trader.backtest"):
        t = Backtester(cfg, data).run().trades
    late = t[pd.to_datetime(t.entry_time) > pd.Timestamp(start) + pd.Timedelta(days=9)]
    assert len(late) > 0 and late.lots.notna().all() and (abs(late.lots - 20) < 1e-9).all()
    assert not any("NOT applied" in r.message for r in caplog.records)


def test_margin_check_fails_closed_without_a_base_rate(caplog):
    """backtest.leverage: a symbol whose base currency cannot be converted must not get margin = NaN (that let
    every other entry pass the free-margin check too): its signals are skipped, with a counted warning."""
    start = datetime(2021, 3, 1)
    syms = [("AUDCAD", 0.95, 0.0001), ("GBPCAD", 1.70, 0.0001), ("EURCAD", 1.50, 0.0001), ("GBPCHF", 1.25, 0.0001)]
    data = {s: synthetic_m15(start, 15, b, p, seed=7 + k) for k, (s, b, p) in enumerate(syms)}
    data["EURUSD"] = synthetic_m15(start, 15, 1.20, 0.0001, seed=20)  # converts CAD (via EURCAD)
    data["USDCHF"] = synthetic_m15(start, 15, 0.92, 0.0001, seed=21)  # converts CHF; nothing converts AUD
    cfg = Config()
    cfg.night_reversion.symbols = [s for s, _, _ in syms]
    cfg.euro_session.enabled = False
    cfg.broker.server_time_mode = "ny7"
    cfg.risk.blackout_start = cfg.risk.blackout_end = ""
    cfg.risk.max_lots_per_order = 0
    cfg.backtest.initial_equity = 100_000.0
    cfg.backtest.leverage = 5.0  # a few lots fit; concurrent positions compete for the free margin
    with caplog.at_level(logging.WARNING, logger="greatest_trader.backtest"):
        t = Backtester(cfg, data).run().trades
    assert len(t) > 0 and "AUDCAD" not in set(t.symbol) and np.isfinite(t.notional).all()
    assert any("AUDCAD" in r.message and "margin check" in r.message for r in caplog.records)
    cfg.night_reversion.symbols = [s for s, _, _ in syms[1:]]  # the unconvertible symbol changes nothing else
    t2 = Backtester(cfg, data).run().trades
    cols = ["entry_time", "symbol", "lots"]
    assert t[cols].equals(t2[cols])
    cfg.backtest.leverage = 1.0  # 1:1: no position of 1 lot (100k notional) or more fits into 80% of 100k
    t3 = Backtester(cfg, data).run().trades
    assert (t3.lots < 1.0).all()

