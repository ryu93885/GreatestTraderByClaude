from datetime import datetime, timedelta

import pandas as pd

from fakes import FakeBroker, step_until, synthetic_m15
from greatest_trader.backtest import Backtester
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
    cfg.broker.server_time_mode = "ny7"
    cfg.risk.blackout_start = cfg.risk.blackout_end = ""  # synthetic data starts in early January
    return cfg


def make_engine(cfg, data):
    broker = FakeBroker(data, equity=1_000_000)
    eng = LiveEngine(cfg, broker, Notifier(cfg.notify))
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
