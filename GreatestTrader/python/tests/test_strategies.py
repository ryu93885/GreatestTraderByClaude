from datetime import datetime, timedelta

from greatest_trader.config import EuroSessionConfig, NightReversionConfig
from greatest_trader.strategies import CloseRequest, EuroSession, ModifyRequest, NightReversion, PositionView

PIP = 0.01  # JPY pair


def bar(close, mid=150.0, upper=150.30, lower=149.70, atr=0.20):
    return {"close": close, "mid": mid, "upper": upper, "lower": lower, "atr": atr}


def nr():
    return NightReversion(NightReversionConfig())


def test_nr_sell_above_upper_band_in_window():
    t = datetime(2021, 3, 3, 0, 30)  # Wednesday 00:30 market time
    req = nr().entry_signal("EURJPY", bar(150.40), t, t, PIP, spread_pips=1.0, commission_pips=0.0)
    assert req is not None and req.direction == -1
    assert abs(req.sl_distance - 0.60) < 1e-12  # 3 x ATR
    assert req.tp_price == 150.0


def test_nr_buy_below_lower_band():
    t = datetime(2021, 3, 3, 1, 45)
    req = nr().entry_signal("EURJPY", bar(149.60), t, t, PIP, 1.0, 0.0)
    assert req is not None and req.direction == 1


def test_nr_filters():
    s = nr()
    wed = datetime(2021, 3, 3, 0, 30)
    assert s.entry_signal("EURJPY", bar(150.0), wed, wed, PIP, 1.0, 0.0) is None  # inside bands
    late = datetime(2021, 3, 3, 2, 0)
    assert s.entry_signal("EURJPY", bar(150.40), late, late, PIP, 1.0, 0.0) is None  # outside hours
    mon = datetime(2021, 3, 1, 0, 30)
    assert s.entry_signal("EURJPY", bar(150.40), mon, mon, PIP, 1.0, 0.0) is None  # Monday
    # ATR 20 pips < 2.0 x (4.9 spread + 4.7 commission + 0.5 slippage = 10.1) -> too expensive
    assert s.entry_signal("EURJPY", bar(150.40), wed, wed, PIP, 4.9, 4.7) is None
    assert s.entry_signal("EURJPY", bar(150.40), wed, wed, PIP, 4.9, 4.6) is not None  # exactly 2.0x
    # spread above max_spread_pips
    assert s.entry_signal("EURJPY", bar(150.40), wed, wed, PIP, 5.1, 0.0) is None


def pos(direction=-1, open_price=150.40, tp=150.0, open_time=datetime(2021, 3, 3, 0, 45, 3)):
    return PositionView(1, "NR", "EURJPY", direction, 0.1, open_price, open_time, open_price + 0.6, tp)


def test_nr_manage_tp_follows_moving_average():
    s = nr()
    acts = s.manage_on_bar(pos(), bar(150.35, mid=150.10), datetime(2021, 3, 3, 1, 0, 2), 150.30, 150.32)
    assert len(acts) == 1 and isinstance(acts[0], ModifyRequest) and acts[0].tp_price == 150.10
    # MA moved above entry for a short -> TP removed
    acts = s.manage_on_bar(pos(), bar(150.6, mid=150.45), datetime(2021, 3, 3, 1, 0, 2), 150.6, 150.62)
    assert isinstance(acts[0], ModifyRequest) and acts[0].tp_price is None
    # price already below MA -> close
    acts = s.manage_on_bar(pos(), bar(150.0, mid=150.10), datetime(2021, 3, 3, 1, 0, 2), 150.0, 150.02)
    assert isinstance(acts[0], CloseRequest)


def test_nr_time_stop_after_12_bars():
    s = nr()
    p = pos()
    assert not s.time_stop_due(p, datetime(2021, 3, 3, 3, 44, 59))
    assert s.time_stop_due(p, datetime(2021, 3, 3, 3, 45, 0))  # 00:45 + 12 x 15min


def test_es_entry_and_exit():
    es = EuroSession(EuroSessionConfig())
    now = datetime(2021, 3, 3, 7, 0, 5)
    req = es.entry_signal(now, now + timedelta(hours=2), atr_h1=0.0010, pip=0.0001, spread_pips=0.5)
    assert req is not None and req.direction == -1 and abs(req.sl_distance - 0.0030) < 1e-12
    assert es.entry_signal(datetime(2021, 3, 3, 7, 20), now, 0.001, 0.0001, 0.5) is None  # too late
    assert es.entry_signal(datetime(2021, 3, 6, 7, 0), now, 0.001, 0.0001, 0.5) is None  # Saturday
    assert es.entry_signal(now, now, 0.001, 0.0001, 2.5) is None  # spread
    p = PositionView(2, "ES", "EURUSD", -1, 0.1, 1.2, now, 1.203, None)
    assert es.manage(p, datetime(2021, 3, 3, 12, 59), now) == []
    assert isinstance(es.manage(p, datetime(2021, 3, 3, 13, 0), now)[0], CloseRequest)
