import json
from datetime import date, datetime

import pytest

import greatest_trader.risk as risk_mod
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
    assert rm.lot_size(1e9, 300.0, 0.01, 50, 0.01) == 20  # max_lots_per_order default 20 < volume_max 50
    rm3 = RiskManager(RiskConfig(max_lots_per_order=0, blackout_start="", blackout_end=""))
    assert rm3.lot_size(1e9, 300.0, 0.01, 50, 0.01) == 50  # cap off -> the broker's volume_max


def test_capped_risk_lots_amount_and_volume_max():
    rm = RiskManager(RiskConfig(max_lots_per_order=20, max_risk_amount=0, blackout_start="", blackout_end=""))
    assert rm.capped_risk(3500.0, 300.0) == 3500.0  # 11.7 lots: below the cap
    assert rm.capped_risk(35000.0, 300.0) == 6000.0  # 116.7 lots -> 20 lots x 300
    assert rm.capped_risk(35000.0, 300.0, volume_max=10) == 3000.0  # the broker's volume_max is tighter
    rm2 = RiskManager(RiskConfig(max_lots_per_order=0, max_risk_amount=5000, blackout_start="", blackout_end=""))
    assert rm2.capped_risk(35000.0, 300.0) == 5000.0
    rm3 = RiskManager(RiskConfig(max_lots_per_order=10, max_risk_amount=5000, blackout_start="", blackout_end=""))
    assert rm3.capped_risk(35000.0, 300.0) == 3000.0  # the tighter of the two
    assert rm3.lot_size(rm3.capped_risk(35000.0, 300.0), 300.0, 0.01, 50, 0.01) == 10
    off = RiskManager(RiskConfig(max_lots_per_order=0, max_risk_amount=0, blackout_start="", blackout_end=""))
    assert off.capped_risk(1e12, 300.0) == 1e12


def test_capped_risk_feeds_the_total_risk_guard():
    """The cap is applied BEFORE the guards: an uncapped 5% trade would break a 4% total-risk cap, the capped
    one (20 lots = 3%) passes."""
    rm = RiskManager(RiskConfig(max_total_risk=0.04, max_lots_per_order=20, blackout_start="", blackout_end=""))
    rm.update_equity(200_000, date(2021, 1, 4))
    intended, loss_lot = 10_000.0, 300.0  # 33 lots
    assert not rm.can_open("NR", "EURJPY", [], 200_000, 1, intended)[0]
    assert rm.can_open("NR", "EURJPY", [], 200_000, 1, rm.capped_risk(intended, loss_lot))[0]


def stable5_rm():
    from greatest_trader.config import load_config
    cfg = load_config(None, preset="stable5").risk
    cfg.blackout_start = cfg.blackout_end = ""
    return RiskManager(cfg)


def test_withdrawal_is_not_a_loss():
    old = stable5_rm()
    old.update_equity(10_000, date(2026, 3, 2))
    ev = old.update_equity(9_600, date(2026, 3, 10))  # 400 withdrawn, NOT applied as a cash flow
    assert any("MONTH STOP" in e for e in ev)  # treated as a -4% month: flatten everything
    rm = stable5_rm()
    rm.update_equity(10_000, date(2026, 3, 2))
    msg = rm.apply_cash_flow(-400, 9_600)
    assert "CASH FLOW -400.00 detected, guards rebased" in msg
    assert rm.update_equity(9_600, date(2026, 3, 10)) == [] and not rm.state.month_halted
    assert rm.state.peak_equity == pytest.approx(9_600) and rm.drawdown(9_600) == pytest.approx(0)
    assert rm.risk_fraction("nr", 9_600) == pytest.approx(0.035)  # no drawdown scaling after a withdrawal
    ev = rm.update_equity(9_600 * 0.969, date(2026, 3, 11))  # a real -3.1% loss still stops the month
    assert any("MONTH STOP" in e for e in ev)


def test_deposit_does_not_hide_a_loss():
    rm = stable5_rm()
    rm.update_equity(10_000, date(2026, 3, 2))
    rm.update_equity(9_750, date(2026, 3, 3))  # -2.5%
    rm.apply_cash_flow(2_000, 11_750)
    ev = rm.update_equity(11_750 * 0.99, date(2026, 3, 4))  # time-weighted month: 0.975 x 0.99 - 1 = -3.5%
    assert any("MONTH STOP" in e for e in ev)
    assert rm.state.cash_flow_total == 2_000


def test_cash_flow_scales_every_baseline_or_rebases():
    st = RiskState(peak_equity=12_000, day_start_equity=10_000, month_start_equity=10_500, last_equity=10_000,
                   daily_equity=[9_000.0, 10_000.0])
    rm = RiskManager(RiskConfig(blackout_start="", blackout_end=""), st)
    rm.apply_cash_flow(5_000, 15_000)  # k = 1.5
    assert (st.peak_equity, st.day_start_equity, st.month_start_equity, st.last_equity) == \
        pytest.approx((18_000, 15_000, 15_750, 15_000))
    assert st.daily_equity == pytest.approx([13_500, 15_000])
    rm.apply_cash_flow(-14_900, 100)  # k = 100 / 15000 < 0.02: rebase everything to current equity
    assert (st.peak_equity, st.day_start_equity, st.month_start_equity, st.last_equity) == (100, 100, 100, 100)
    assert st.daily_equity == [100, 100]


def test_reset_halt_keeps_state():
    st = RiskState(peak_equity=10_000, halted=True, halt_reason="max drawdown 20.5%", cash_tickets=[[1, 0.0]],
                   account_login=7)
    rm = RiskManager(RiskConfig(), st)
    msg = rm.reset_halt()
    assert not st.halted and st.peak_equity == 10_000 and "max drawdown" in msg
    st.halted = True
    rm.reset_halt(rebase_peak_to=7_900)
    assert not st.halted and st.peak_equity == 7_900 and st.cash_tickets == [[1, 0.0]] and st.account_login == 7


def test_state_roundtrip(tmp_path):
    st = RiskState(peak_equity=123.0, day="2021-01-04", halted=True, halt_reason="x")
    p = tmp_path / "s.json"
    st.save(p)
    st2 = RiskState.load(p)
    assert st2.peak_equity == 123.0 and st2.halted and st2.halt_reason == "x"


def test_old_state_file_still_loads(tmp_path):
    """v1.1 state files (no account / cash-flow / clock fields, plus an unknown key) load with defaults."""
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"peak_equity": 5000.0, "day": "2026-10-01", "day_start_equity": 4900.0,
                             "halted": False, "halt_reason": "", "last_es_entry_day": "", "month": "2026-10",
                             "month_start_equity": 4800.0, "month_halted": False, "day_halted": False,
                             "daily_equity": [4700.0], "last_equity": 4950.0, "some_future_key": 1}), encoding="utf-8")
    st = RiskState.load(p)
    assert st.peak_equity == 5000.0 and st.account_login == 0 and not st.cash_init and st.open_trades == {}
    assert st.restored_from == ""


def test_truncated_state_restores_backup_or_starts_halted(tmp_path):
    p = tmp_path / "state.json"
    RiskState(peak_equity=12_345.0, month="2026-10").save(p)
    assert len(list(tmp_path.glob("state.*.bak.json"))) == 1  # daily backup
    p.write_text('{"peak_equity": 12345.0, "day": "2026-10-1', encoding="utf-8")  # torn write after a reset
    st = RiskState.load(p)
    assert st.peak_equity == 12_345.0 and st.restored_from.endswith(".bak.json") and not st.halted
    for f in tmp_path.glob("state.*.bak.json"):
        f.write_text("", encoding="utf-8")
    st = RiskState.load(p)
    assert st.halted and "corrupt" in st.halt_reason and st.restored_from.startswith("none")
    assert RiskState.load(tmp_path / "fresh.json").restored_from == ""  # no file at all: normal fresh start


def test_state_written_only_on_change_and_backups_pruned(tmp_path, monkeypatch):
    p = tmp_path / "state.json"
    st = RiskState(peak_equity=1.0)
    writes = []
    real = risk_mod.os.replace
    monkeypatch.setattr(risk_mod.os, "replace", lambda a, b: (writes.append(1), real(a, b)))
    for _ in range(100):
        st.save(p)
    st.peak_equity = 2.0
    assert st.save(p) and not st.save(p)
    assert len(writes) == 2
    for d in range(20):  # old backups beyond 14 are deleted
        (tmp_path / f"state.202501{d + 10:02d}.bak.json").write_text("{}", encoding="utf-8")
    (tmp_path / f"state.{datetime.now():%Y%m%d}.bak.json").unlink()
    st.peak_equity = 3.0
    st.save(p)
    assert len(list(tmp_path.glob("state.*.bak.json"))) == 14


def test_state_replace_is_retried_on_permission_error(tmp_path, monkeypatch):
    p = tmp_path / "state.json"
    real, fails = risk_mod.os.replace, []

    def flaky(a, b):
        if len(fails) < 2:
            fails.append(1)
            raise PermissionError("locked by anti-virus")
        return real(a, b)

    monkeypatch.setattr(risk_mod.os, "replace", flaky)
    monkeypatch.setattr(risk_mod.time, "sleep", lambda s: None)
    RiskState(peak_equity=9.0).save(p)
    assert RiskState.load(p).peak_equity == 9.0 and len(fails) == 2


def test_a_locked_state_file_is_read_again_not_rolled_back(tmp_path, monkeypatch):
    """A PermissionError (anti-virus / indexer lock) is not corruption: the main file is read again instead of
    silently restoring the day's first backup (which cleared halted / month_halted)."""
    p = tmp_path / "state.json"
    RiskState(peak_equity=10_000, month="2026-10").save(p)  # the first save of the day is also today's backup
    RiskState(peak_equity=10_000, month="2026-10", halted=True, halt_reason="max drawdown 20.0%", month_halted=True,
              cash_tickets=[[77, 1.0]]).save(p)
    real_open, fails = open, []

    def flaky_open(f, *a, **k):
        if str(f) == str(p) and len(fails) < 2:
            fails.append(1)
            raise PermissionError(13, "The process cannot access the file because it is being used")
        return real_open(f, *a, **k)

    monkeypatch.setattr("builtins.open", flaky_open)
    monkeypatch.setattr(risk_mod.time, "sleep", lambda s: None)
    st = RiskState.load(p)
    assert st.restored_from == "" and st.halted and st.month_halted and st.cash_tickets == [[77, 1.0]]

    def locked(f, *a, **k):
        if str(f) == str(p):
            raise PermissionError(13, "locked")
        return real_open(f, *a, **k)

    monkeypatch.setattr("builtins.open", locked)
    with pytest.raises(RuntimeError, match="cannot be read"):  # stays locked: restart, never an older backup
        RiskState.load(p)


def test_a_corrupt_state_file_is_kept_for_inspection(tmp_path):
    p = tmp_path / "state.json"
    RiskState(peak_equity=12_345.0).save(p)
    p.write_text('{"peak_equity": 12345.0, "day": "2026-10-1', encoding="utf-8")
    st = RiskState.load(p)
    assert st.peak_equity == 12_345.0 and st.restored_from.endswith(".bak.json")
    kept = list(tmp_path.glob("state.*.corrupt.json"))
    assert len(kept) == 1 and kept[0].read_text(encoding="utf-8").startswith('{"peak_equity": 12345.0, "day"')
    assert not p.exists()  # the next save writes a fresh file; the corrupt one is not overwritten
    st.save(p)
    assert RiskState.load(p).peak_equity == 12_345.0 and len(list(tmp_path.glob("state.*.corrupt.json"))) == 1
