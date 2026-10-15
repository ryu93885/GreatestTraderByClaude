from datetime import datetime, timedelta

import pandas as pd

from fakes import FakeBroker, step_until, synthetic_m15
from greatest_trader.backtest import Backtester
from greatest_trader.config import Config
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
