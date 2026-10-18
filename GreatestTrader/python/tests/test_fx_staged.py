"""Backtest FX converter: the USD leg takes a fresh direct pair first, then a cross, then the direct pair up to
7 days old (ops-backtest#1) - for a USD account too, where the account pair IS the USD leg."""
import numpy as np
import pandas as pd

from greatest_trader.backtest import _FxConverter

IDX = pd.date_range("2025-01-06", "2025-01-17 23:45", freq="15min")
T = np.array([np.datetime64("2025-01-14T10:00")], dtype="datetime64[ns]")  # 4 days after the cut below
CUT = "2025-01-10"


def px(v, cut=None, start=None):
    d = pd.DataFrame({"open": np.full(len(IDX), float(v))}, index=IDX)
    if cut is not None:
        d = d[d.index < cut]
    if start is not None:
        d = d[d.index >= start]
    return d


def test_usd_account_stale_direct_pair_gives_way_to_a_fresh_cross():
    """USDCHF stopped 4 days ago at 0.80 (stale), GBPUSD / GBPCHF are fresh: CHF->USD must come from the cross
    (1.30 / 1.235), not from the stale USDCHF (1.25) - before the fix usd() read USDCHF with the 7-day limit."""
    fx = _FxConverter({"USDCHF": px(0.80, cut=CUT), "GBPUSD": px(1.30), "GBPCHF": px(1.235)})
    assert np.allclose(fx.rate("CHF", "USD", T), 1.30 / 1.235)
    assert np.allclose(fx.usd("CHF", T), 1.30 / 1.235)
    # the inverse direction (USD as the base currency of a pair priced in CHF) gives the reciprocal
    assert np.allclose(fx.rate("USD", "CHF", T), 1.235 / 1.30)


def test_usd_account_fresh_direct_pair_still_wins():
    fx = _FxConverter({"USDCHF": px(0.80), "GBPUSD": px(1.30), "GBPCHF": px(1.235)})
    assert np.allclose(fx.rate("CHF", "USD", T), 1 / 0.80)
    assert np.allclose(fx.usd("CHF", T), 1 / 0.80)


def test_stale_direct_pair_is_used_when_no_cross_has_a_price():
    # no cross at all
    fx = _FxConverter({"USDCHF": px(0.80, cut=CUT), "GBPUSD": px(1.30)})
    assert np.allclose(fx.rate("CHF", "USD", T), 1 / 0.80)
    # a cross exists but is itself older than 7 days: the direct pair (4 days) is the freshest price left
    fx = _FxConverter({"USDCHF": px(0.80, cut=CUT), "GBPUSD": px(1.30), "GBPCHF": px(1.235, cut="2025-01-07")})
    assert np.allclose(fx.rate("CHF", "USD", T), 1 / 0.80)
    # older than 7 days and nothing else: no rate (the cap is not applied, with a warning)
    fx = _FxConverter({"USDCHF": px(0.80, cut="2025-01-07"), "GBPUSD": px(1.30)})
    assert np.isnan(fx.rate("CHF", "USD", T)).all()


def test_jpy_account_via_route_uses_the_staged_usd_legs():
    """rate()'s route through USD for a JPY account: CHF->JPY = usd(CHF) / usd(JPY); both legs follow the same
    order, so a stale USDJPY gives way to a fresh EURUSD x EURJPY cross as well."""
    fx = _FxConverter({"USDCHF": px(0.80, cut=CUT), "GBPUSD": px(1.30), "GBPCHF": px(1.235),
                       "USDJPY": px(140.0, cut=CUT), "EURUSD": px(1.05), "EURJPY": px(157.5)})
    usd_per_jpy = 1.05 / 157.5  # EURUSD / EURJPY
    assert np.allclose(fx.usd("JPY", T), usd_per_jpy)
    assert np.allclose(fx.rate("CHF", "JPY", T), (1.30 / 1.235) / usd_per_jpy)
    assert np.allclose(fx.rate("USD", "JPY", T), 1 / usd_per_jpy)  # no CHF/JPY or USDJPY fresh: via the cross
    # a fresh direct account pair still wins over every route
    fx = _FxConverter({"USDCHF": px(0.80, cut=CUT), "GBPUSD": px(1.30), "GBPCHF": px(1.235),
                       "USDJPY": px(140.0), "CHFJPY": px(170.0)})
    assert np.allclose(fx.rate("CHF", "JPY", T), 170.0)
    assert np.allclose(fx.rate("USD", "JPY", T), 140.0)


def test_mixed_times_pick_per_element():
    """Each time takes its own best candidate: before the cut the direct pair is fresh, after it the cross."""
    fx = _FxConverter({"USDCHF": px(0.80, cut=CUT), "GBPUSD": px(1.30), "GBPCHF": px(1.235)})
    t = np.array([np.datetime64("2025-01-09T12:00"), np.datetime64("2025-01-10T12:00"),
                  np.datetime64("2025-01-13T00:00")], dtype="datetime64[ns]")
    # 2025-01-10 12:00 is 12h15m after the last USDCHF bar (09 23:45): still fresh (<= 1 day)
    assert np.allclose(fx.rate("CHF", "USD", t), [1 / 0.80, 1 / 0.80, 1.30 / 1.235])
