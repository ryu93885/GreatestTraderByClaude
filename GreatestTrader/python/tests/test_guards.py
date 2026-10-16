"""Portfolio guards, adaptive risk and news filters added in v1.1."""
from datetime import date, datetime

from greatest_trader.config import NightReversionConfig, RiskConfig
from greatest_trader.risk import RiskManager
from greatest_trader.strategies import NightReversion, PositionView


def pv(sym, direction=1, risk=0.0):
    return PositionView(0, "NR", sym, direction, 0.1, 1.0, datetime(2021, 1, 1), None, None, risk_amount=risk)


def test_total_risk_cap():
    rm = RiskManager(RiskConfig(max_total_risk=0.03, blackout_start="", blackout_end=""))
    rm.update_equity(10000, date(2021, 1, 4))
    assert rm.can_open("NR", "EURJPY", [pv("GBPCAD", risk=150)], 10000, 1, 150)[0]  # 3.0% total
    ok, why = rm.can_open("NR", "EURJPY", [pv("GBPCAD", risk=150)], 10000, 1, 151)
    assert not ok and "total" in why


def test_same_direction_per_currency():
    rm = RiskManager(RiskConfig(max_same_direction_per_currency=1, blackout_start="", blackout_end=""))
    rm.update_equity(10000, date(2021, 1, 4))
    pos = [pv("EURJPY", direction=-1)]  # short EURJPY = long JPY
    assert not rm.can_open("NR", "GBPJPY", pos, 10000, -1, 100)[0]  # also long JPY
    assert rm.can_open("NR", "GBPJPY", pos, 10000, 1, 100)[0]  # short JPY is allowed
    assert not rm.can_open("NR", "EURGBP", pos, 10000, -1, 100)[0]  # short EUR twice


def test_monthly_limits_and_hard_stops():
    rm = RiskManager(RiskConfig(monthly_loss_limit=0.02, monthly_hard_stop=0.03, daily_loss_limit=0.0, blackout_start="", blackout_end=""))
    rm.update_equity(10000, date(2021, 3, 1))
    assert rm.can_open("NR", "EURJPY", [], 9850, 1, 10)[0]
    assert not rm.can_open("NR", "EURJPY", [], 9790, 1, 10)[0]  # -2.1% month to date
    rm.update_equity(9690, date(2021, 3, 2))
    assert "monthly hard stop" in rm.flatten_reason and rm.state.month_halted
    rm.update_equity(9700, date(2021, 3, 3))
    assert rm.flatten_reason == "" and not rm.can_open("NR", "EURJPY", [], 9700, 1, 10)[0]
    rm.update_equity(9700, date(2021, 4, 1))  # new month resets
    assert rm.can_open("NR", "EURJPY", [], 9700, 1, 10)[0]


def test_hard_stop_stays_active_until_period_ends():
    rm = RiskManager(RiskConfig(monthly_hard_stop=0.03, daily_loss_limit=0.0, blackout_start="", blackout_end=""))
    rm.update_equity(10000, date(2021, 3, 1))
    assert rm.hard_stop_active() == ""
    rm.update_equity(9690, date(2021, 3, 2))
    rm.update_equity(9700, date(2021, 3, 3))
    assert rm.flatten_reason == "" and "monthly hard stop" in rm.hard_stop_active()
    rm.update_equity(9700, date(2021, 4, 1))
    assert rm.hard_stop_active() == ""


def test_daily_hard_stop():
    rm = RiskManager(RiskConfig(daily_hard_stop=0.02, daily_loss_limit=0.0, blackout_start="", blackout_end=""))
    rm.update_equity(10000, date(2021, 3, 1))
    rm.update_equity(9790, date(2021, 3, 1))
    assert "daily hard stop" in rm.flatten_reason
    assert not rm.can_open("NR", "EURJPY", [], 9790, 1, 10)[0]
    rm.update_equity(9790, date(2021, 3, 2))
    assert rm.can_open("NR", "EURJPY", [], 9790, 1, 10)[0]


def test_vol_target_and_equity_filter():
    cfg = RiskConfig(profile="custom", risk_nr=0.02, vol_target_daily=0.005, vol_lookback_days=10, vol_mult_min=0.25,
                     vol_mult_max=1.5, dd_scale_full=0.0, blackout_start="", blackout_end="")
    rm = RiskManager(cfg)
    eq = 10000.0
    for i in range(12):  # +-2% daily swings -> realised vol ~2% -> multiplier 0.25 (floor)
        eq *= 1.02 if i % 2 == 0 else 0.98
        rm.update_equity(eq, date(2021, 3, 1 + i))
    assert abs(rm.risk_fraction("NR", eq) - 0.02 * 0.25) < 1e-9
    cfg2 = RiskConfig(profile="custom", risk_nr=0.02, eq_filter_days=5, eq_filter_mult=0.5, dd_scale_full=0.0, blackout_start="", blackout_end="")
    rm2 = RiskManager(cfg2)
    for i, v in enumerate([100, 101, 102, 103, 104, 90, 90]):
        rm2.update_equity(v, date(2021, 3, 1 + i))
    assert abs(rm2.risk_fraction("NR", 90) - 0.01) < 1e-12  # equity below its 5-day SMA


def test_news_filters():
    s = NightReversion(NightReversionConfig(max_band_z=3.0, max_bar_range_atr=3.0))
    t = datetime(2021, 3, 3, 0, 30)
    base = {"mid": 150.0, "upper": 150.36, "lower": 149.64, "atr": 0.20}  # sd = 0.2
    ok = dict(base, close=150.40, high=150.45, low=150.10)
    assert s.entry_signal("EURJPY", ok, t, t, 0.01, 1.0, 0.0) is not None
    far = dict(base, close=150.70, high=150.72, low=150.40)  # 3.5 sd from the mean
    assert s.entry_signal("EURJPY", far, t, t, 0.01, 1.0, 0.0) is None
    spike = dict(base, close=150.40, high=150.75, low=150.05)  # range 0.70 > 3 x ATR
    assert s.entry_signal("EURJPY", spike, t, t, 0.01, 1.0, 0.0) is None


def test_year_end_blackout():
    rm = RiskManager(RiskConfig(blackout_start="12-20", blackout_end="01-05", daily_loss_limit=0.0))
    for d, blocked in [(date(2021, 12, 19), False), (date(2021, 12, 20), True), (date(2021, 12, 31), True),
                       (date(2022, 1, 3), True), (date(2022, 1, 5), True), (date(2022, 1, 6), False)]:
        rm.update_equity(10000, d)
        assert rm.can_open("NR", "EURJPY", [], 10000, 1, 10)[0] is (not blocked), d
    off = RiskManager(RiskConfig(blackout_start="", blackout_end=""))
    off.update_equity(10000, date(2021, 12, 25))
    assert off.can_open("NR", "EURJPY", [], 10000, 1, 10)[0]
