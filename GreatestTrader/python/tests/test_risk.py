from datetime import date, datetime

from greatest_trader.config import RiskConfig
from greatest_trader.risk import RiskManager, RiskState
from greatest_trader.strategies import PositionView


def pv(sym):
    return PositionView(0, "NR", sym, 1, 0.1, 1.0, datetime(2021, 1, 1), None, None)


def test_profiles_and_dd_scaling():
    rm = RiskManager(RiskConfig(profile="aggressive", blackout_start="", blackout_end=""))
    rm.update_equity(10000, date(2021, 1, 4))
    assert rm.risk_fraction("NR", 10000) == 0.015
    # 15% drawdown -> multiplier 1 - 0.15/0.30 = 0.5
    assert abs(rm.risk_fraction("NR", 8500) - 0.0075) < 1e-12
    # deep drawdown -> floor 0.30
    assert abs(rm.risk_fraction("NR", 5000) - 0.0045) < 1e-12


def test_position_limits():
    rm = RiskManager(RiskConfig(max_open_positions=3, max_positions_per_currency=2, blackout_start="", blackout_end=""))
    rm.update_equity(10000, date(2021, 1, 4))
    assert rm.can_open("NR", "EURJPY", [], 10000)[0]
    assert not rm.can_open("NR", "EURJPY", [pv("EURJPY")], 10000)[0]  # same symbol
    assert not rm.can_open("NR", "USDJPY", [pv("EURJPY"), pv("GBPJPY")], 10000)[0]  # JPY limit
    assert not rm.can_open("NR", "EURGBP", [pv("EURJPY"), pv("GBPCAD"), pv("AUDCAD")], 10000)[0]  # total


def test_daily_limit_and_halt():
    rm = RiskManager(RiskConfig(daily_loss_limit=0.05, max_drawdown_halt=0.40, blackout_start="", blackout_end=""))
    rm.update_equity(10000, date(2021, 1, 4))
    assert not rm.can_open("NR", "EURJPY", [], 9400)[0]  # -6% today
    rm.update_equity(9400, date(2021, 1, 5))  # new day resets
    assert rm.can_open("NR", "EURJPY", [], 9400)[0]
    ev = rm.update_equity(5900, date(2021, 1, 6))
    assert ev and rm.state.halted
    assert not rm.can_open("NR", "EURJPY", [], 5900)[0]


def test_lot_size_rounding_and_minimum():
    rm = RiskManager(RiskConfig(min_lot_policy="skip", blackout_start="", blackout_end=""))
    assert rm.lot_size(100.0, 300.0, 0.01, 50, 0.01) == 0.33
    assert rm.lot_size(1.0, 300.0, 0.01, 50, 0.01) == 0.0  # below min -> skip
    rm2 = RiskManager(RiskConfig(min_lot_policy="min", blackout_start="", blackout_end=""))
    assert rm2.lot_size(2.5, 300.0, 0.01, 50, 0.01) == 0.01  # 3.0 loss <= 1.5 x 2.5
    assert rm.lot_size(1e9, 300.0, 0.01, 50, 0.01) == 50


def test_state_roundtrip(tmp_path):
    st = RiskState(peak_equity=123.0, day="2021-01-04", halted=True, halt_reason="x")
    p = tmp_path / "s.json"
    st.save(p)
    st2 = RiskState.load(p)
    assert st2.peak_equity == 123.0 and st2.halted and st2.halt_reason == "x"
