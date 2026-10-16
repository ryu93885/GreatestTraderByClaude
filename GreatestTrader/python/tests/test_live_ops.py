"""Live engine operations for long unattended runs: cash flows, server clock, fault isolation, account binding,
monitoring (DD alert, daily summary, R monitor), trade journal, state durability."""
from datetime import datetime, timedelta

import pandas as pd
import pytest

import greatest_trader.engine as engine_mod
import greatest_trader.risk as risk_mod
from fakes import NY7, FakeBroker, ny7_utc, step_until, synthetic_m15
from greatest_trader.broker.base import CashFlow
from greatest_trader.config import Config, load_config
from greatest_trader.engine import M15, LiveEngine
from greatest_trader.journal import TradeJournal, risk_weighted_r
from greatest_trader.live_report import edge_monitor_table
from greatest_trader.notify import Notifier
from greatest_trader.risk import RiskState
from greatest_trader.strategies import PositionView

SPIKE = datetime(2021, 3, 3, 0, 30)


def cfg_for(tmp_path, syms=("EURJPY",), preset=None, mode="ny7", es=False):
    cfg = load_config(None, preset=preset) if preset else Config()
    cfg.night_reversion.symbols = list(syms)
    cfg.euro_session.enabled = es
    cfg.engine.dry_run = False
    cfg.engine.state_file = str(tmp_path / "state.json")
    cfg.engine.journal_dir = str(tmp_path / "journal")
    cfg.broker.server_time_mode = mode
    cfg.risk.blackout_start = cfg.risk.blackout_end = ""
    return cfg


def make(cfg, data, equity=10_000.0, utcnow=None):
    br = FakeBroker(data, equity=equity)
    eng = LiveEngine(cfg, br, Notifier(cfg.notify), utcnow=utcnow or ny7_utc(br))
    return eng, br


def sent(eng, text):
    return [m for m in eng.notifier.recent if text in m]


def eurjpy(start=datetime(2021, 3, 1), days=5, **kw):
    return {"EURJPY": synthetic_m15(start, days, 130.0, 0.01, **kw)}


# ------------------------------------------------------------------ cash flows (contract 3)
def test_withdrawal_is_not_a_loss(tmp_path):
    eng, br = make(cfg_for(tmp_path, preset="stable5"), eurjpy())
    br.set_time(datetime(2021, 3, 2, 22, 0, 5))
    eng.step()
    br.set_time(datetime(2021, 3, 2, 22, 0, 10))
    br.add_cash(-400, "withdrawal")  # 4% of equity
    eng.step()
    st = eng.state
    assert not st.month_halted and not st.day_halted and not sent(eng, "MONTH STOP")
    assert (st.peak_equity, st.month_start_equity, st.day_start_equity) == pytest.approx((9_600, 9_600, 9_600))
    assert sent(eng, "CASH FLOW -400.00 detected, guards rebased")
    assert eng.risk.risk_fraction("NR", 9_600) == pytest.approx(0.035)  # no drawdown scaling
    # the same equity drop without a balance deal is a real loss: monthly hard stop
    eng2, br2 = make(cfg_for(tmp_path / "b", preset="stable5"), eurjpy())
    br2.set_time(datetime(2021, 3, 2, 22, 0, 5))
    eng2.step()
    br2.equity -= 400
    br2.set_time(datetime(2021, 3, 2, 22, 0, 10))
    eng2.step()
    assert sent(eng2, "MONTH STOP")


def test_deposit_does_not_hide_a_loss(tmp_path):
    eng, br = make(cfg_for(tmp_path, preset="stable5"), eurjpy())
    br.set_time(datetime(2021, 3, 2, 22, 0, 5))
    eng.step()
    br.equity = 9_750  # -2.5% this month
    br.set_time(datetime(2021, 3, 2, 23, 0, 5))
    eng.step()
    assert not sent(eng, "MONTH STOP")
    br.add_cash(2_000, "deposit")
    br.equity *= 0.99  # and a further -1% loss: -3.5% time-weighted
    br.set_time(datetime(2021, 3, 2, 23, 0, 10))
    eng.step()
    assert sent(eng, "MONTH STOP") and sent(eng, "CASH FLOW +2,000.00")


def test_cash_flow_first_run_dedup_and_restart(tmp_path):
    cfg = cfg_for(tmp_path)
    eng, br = make(cfg, eurjpy())
    br.flows.append(CashFlow(1, datetime(2021, 2, 1), 10_000.0, 2, "initial deposit"))  # history before v1.2
    br.set_time(datetime(2021, 3, 2, 10, 0, 5))
    eng.step()
    st = eng.state
    assert st.cash_init and st.peak_equity == 10_000 and st.cash_flow_total == 0  # seen, not applied
    assert [t for t, _ in st.cash_tickets] == [1]
    br.set_time(datetime(2021, 3, 2, 10, 0, 10))
    br.add_cash(-1_000)
    eng.step()
    eng.step()  # the same deal again: applied once
    assert st.peak_equity == pytest.approx(9_000) and st.cash_flow_total == -1_000
    assert len(sent(eng, "CASH FLOW")) == 1
    eng2 = LiveEngine(cfg, br, Notifier(cfg.notify), utcnow=ny7_utc(br))  # restart on the saved state
    eng2.step()
    assert eng2.state.cash_flow_total == -1_000 and eng2.state.peak_equity == pytest.approx(9_000)
    assert not sent(eng2, "CASH FLOW")


def test_flow_booked_between_the_equity_read_and_the_scan(tmp_path):
    eng, br = make(cfg_for(tmp_path, preset="stable5"), eurjpy())
    br.set_time(datetime(2021, 3, 2, 22, 0, 5))
    eng.step()
    br.add_cash(-400)
    reads = iter([10_000.0, 9_600.0])  # the first read of the loop still saw the old equity
    br.account_equity = lambda: next(reads, 9_600.0)
    br.set_time(datetime(2021, 3, 2, 22, 0, 10))
    eng.step()
    assert eng.state.peak_equity == pytest.approx(9_600) and eng.state.last_equity == 9_600
    assert not sent(eng, "MONTH STOP")


def test_reset_halt_is_applied_after_the_first_cash_flow_check(tmp_path):
    """reset-halt --rebase-peak while a 5,000 deposit was made with the engine stopped: the deposit is applied to
    the OLD peak first, then the peak is rebased to the equity that contains it (EA: InpResetHalt). Rebasing first
    multiplied the deposit onto the new peak: 'HALT: drawdown 38.8%' at once."""
    cfg = cfg_for(tmp_path, preset="stable5")
    RiskState(peak_equity=10_000, last_equity=7_900, day="2021-03-02", month="2021-03", day_start_equity=7_900,
              month_start_equity=8_000, halted=True, halt_reason="max drawdown 21.0% reached", account_login=111,
              cash_init=True, cash_tickets=[[1, 0.0]], reset_request="rebase").save(tmp_path / "state.json")
    eng, br = make(cfg, eurjpy(), equity=7_900)
    br.set_time(datetime(2021, 3, 2, 10, 0, 5))
    br.add_cash(5_000, "deposit while stopped")
    eng.step()
    st = eng.state
    assert not st.halted and st.reset_request == "" and st.peak_equity == pytest.approx(12_900)
    assert eng.risk.risk_fraction("NR", 12_900) == pytest.approx(0.035)
    assert sent(eng, "CASH FLOW +5,000.00") and sent(eng, "RESET HALT") and not sent(eng, "HALT: max drawdown")
    j = pd.read_csv(tmp_path / "journal" / "trades_2021.csv")
    assert j.event.tolist() == ["reset_halt"] and "applied by the engine" in j.reason.iloc[0]


def test_several_flows_in_one_loop_compound_exactly(tmp_path):
    eng, br = make(cfg_for(tmp_path), eurjpy())
    br.set_time(datetime(2021, 3, 2, 10, 0, 5))
    eng.step()
    br.set_time(datetime(2021, 3, 2, 10, 0, 10))
    br.add_cash(5_000)
    br.add_cash(-3_000)
    eng.step()
    assert eng.state.peak_equity == pytest.approx(12_000)  # k = 12000 / 10000 overall


# ------------------------------------------------------------------ server clock (contract 4)
def test_weekend_restart_never_trades_on_a_guessed_clock(tmp_path):
    cfg = cfg_for(tmp_path, mode="auto")
    br = FakeBroker({"EURJPY": synthetic_m15(datetime(2025, 1, 13), 14, 160.0, 0.01)})
    wall = {"utc": datetime(2025, 1, 18, 8, 0)}  # restart on Saturday 08:00 UTC (v1.1 installed fixed -8 h)
    eng = LiveEngine(cfg, br, Notifier(cfg.notify), utcnow=lambda: wall["utc"])
    br.set_time(datetime(2025, 1, 17, 23, 59, 58))  # the last tick of the week (ny7 server)
    for _ in range(15):
        wall["utc"] += timedelta(minutes=1)
        eng.step()
    assert not eng.clock_verified and eng.entry_block().startswith("server clock not verified")
    assert eng.state.clock_mode == "" and not sent(eng, "no price updates")  # weekend: no stale-feed alarm
    for k in range(2):  # Monday open: fresh ticks
        br.set_time(datetime(2025, 1, 20, 0, 0, 5) + timedelta(seconds=5 * k))
        wall["utc"] = NY7.server_to_utc(br.now)
        eng.step()
    assert eng.clock_verified and eng.entry_block() == "" and eng.state.clock_mode == "ny7"


def test_auto_clock_adopts_a_new_offset_after_three_observations(tmp_path):
    cfg = cfg_for(tmp_path, mode="auto")
    br = FakeBroker({"EURJPY": synthetic_m15(datetime(2025, 1, 13), 10, 160.0, 0.01)})
    eng = LiveEngine(cfg, br, Notifier(cfg.notify), utcnow=lambda: br.now)  # broker on GMT+0: server == UTC
    base = datetime(2025, 1, 21, 10, 0, 5)
    br.positions_.append(PositionView(1, "NR", "EURJPY", 1, 0.1, 160.0, base - timedelta(hours=4), 159.0, None))
    for k in range(3):
        br.set_time(base + timedelta(seconds=5 * k))
        eng.step()
    assert not eng.clock_verified and eng.state.clock_mode == "" and eng.entry_block()
    assert ("close", "EURJPY", "GT time stop") in br.log  # management works before the clock is verified
    br.set_time(base + timedelta(seconds=15))
    eng.step()
    assert eng.clock_verified and (eng.clock.mode, eng.clock.offset) == ("fixed", 0.0)
    assert (eng.state.clock_mode, eng.state.clock_offset) == ("fixed", 0.0) and sent(eng, "server clock detected")
    eng2 = LiveEngine(cfg, br, Notifier(cfg.notify), utcnow=lambda: br.now)  # restart: persisted, unverified
    assert (eng2.clock.mode, eng2.clock.offset) == ("fixed", 0.0) and not eng2.clock_verified
    for k in (20, 25):
        br.set_time(base + timedelta(seconds=k))
        eng2.step()
    assert eng2.clock_verified and not sent(eng2, "CHANGED")


def test_configured_clock_mismatch_blocks_entries_until_it_matches(tmp_path):
    cfg = cfg_for(tmp_path, mode="ny7")
    br = FakeBroker({"EURJPY": synthetic_m15(datetime(2025, 1, 13), 10, 160.0, 0.01)})
    wall = {}
    eng = LiveEngine(cfg, br, Notifier(cfg.notify), utcnow=lambda: wall["utc"])
    base = datetime(2025, 1, 21, 10, 0, 5)
    for k in range(4):  # the broker actually runs on GMT+0
        br.set_time(base + timedelta(seconds=5 * k))
        wall["utc"] = br.now
        eng.step()
        if k == 2:
            assert eng.entry_block() == ""  # 2 mismatches: not yet
    assert eng.entry_block() == "broker clock mismatch: observed +0.0 h, expected +2.0 h"
    assert sent(eng, "NEW ENTRIES BLOCKED: broker clock mismatch")
    br.set_time(base + timedelta(seconds=25))
    wall["utc"] = NY7.server_to_utc(br.now)  # matches again
    eng.step()
    assert eng.entry_block() == "" and sent(eng, "matches the configured")


def test_vps_clock_drift_is_reported_and_never_verifies(tmp_path):
    cfg = cfg_for(tmp_path, mode="auto")
    br = FakeBroker({"EURJPY": synthetic_m15(datetime(2025, 1, 13), 10, 160.0, 0.01)})
    eng = LiveEngine(cfg, br, Notifier(cfg.notify), utcnow=lambda: NY7.server_to_utc(br.now) + timedelta(minutes=5))
    for k in range(3):  # the first read is no observation, then 2 off-grid observations: no alert yet
        br.set_time(datetime(2025, 1, 21, 10, 0, 5) + timedelta(seconds=5 * k))
        eng.step()
    assert not sent(eng, "PC CLOCK")
    for k in range(3, 6):
        br.set_time(datetime(2025, 1, 21, 10, 0, 5) + timedelta(seconds=5 * k))
        eng.step()
    assert not eng.clock_verified and len(sent(eng, "check the Windows time synchronisation")) == 1  # 6-hourly
    assert "PC clock off" in eng.entry_block() and "new entries wait" in sent(eng, "PC CLOCK")[0]


def test_weekend_stale_tick_that_becomes_readable_never_counts(tmp_path):
    """Saturday restart: the newest Friday tick 'advances' the server time one loop later (a symbol that was
    unreadable, or the fake's one-off jump). No clock may be adopted, saved or used on the weekend."""
    cfg = cfg_for(tmp_path, mode="auto")
    br = FakeBroker({"EURJPY": synthetic_m15(datetime(2025, 1, 13), 14, 160.0, 0.01)})
    wall = {"utc": datetime(2025, 1, 18, 8, 0, 0)}
    eng = LiveEngine(cfg, br, Notifier(cfg.notify), utcnow=lambda: wall["utc"])
    br.set_time(datetime(2025, 1, 17, 23, 59, 30))
    eng.step()
    br.now = datetime(2025, 1, 17, 23, 59, 58)  # within +-120 s of a half-hour step from the Saturday wall clock
    for _ in range(5):
        wall["utc"] += timedelta(seconds=5)
        eng.step()
    assert eng.state.clock_mode == "" and not eng.clock_verified and eng.state.day == "2025-01-17"
    assert not sent(eng, "server clock")


def test_friday_close_and_monday_open_raise_no_false_alarms(tmp_path):
    """The last tick of the week was measured with a growing delay (false 'check the Windows time
    synchronisation' at every pause of 2 minutes), and the stale-feed watchdog counted from Friday's close at
    the Monday open ('no price updates for 2880 min'). A feed that stays silent after the open is reported."""
    br = FakeBroker({"EURJPY": synthetic_m15(datetime(2025, 1, 13), 14, 160.0, 0.01)})
    wall = {}
    eng = LiveEngine(cfg_for(tmp_path), br, Notifier(Config().notify), utcnow=lambda: wall["utc"])
    t = datetime(2025, 1, 17, 23, 58, 0)
    while t <= datetime(2025, 1, 17, 23, 59, 58):  # Friday close, VPS clock exact, ticks seen 2 s late
        br.set_time(t)
        wall["utc"] = NY7.server_to_utc(t) + timedelta(seconds=2)
        eng.step()
        t += timedelta(seconds=2)
    for _ in range(40):  # 200 s without ticks after the close
        wall["utc"] += timedelta(seconds=5)
        eng.step()
    assert not sent(eng, "PC CLOCK") and not sent(eng, "no price updates")
    for u in (datetime(2025, 1, 18, 12, 0), datetime(2025, 1, 19, 22, 0), datetime(2025, 1, 19, 22, 9)):
        wall["utc"] = u  # Saturday; Monday 00:00 market time; 9 minutes later, still no tick
        eng.step()
    assert not sent(eng, "no price updates")
    br.set_time(datetime(2025, 1, 20, 0, 9, 30))  # the broker's first tick of the week
    wall["utc"] = NY7.server_to_utc(br.now) + timedelta(seconds=1)
    eng.step()
    assert not sent(eng, "no price updates")
    eng2 = LiveEngine(cfg_for(tmp_path / "b"), br, Notifier(Config().notify), utcnow=lambda: wall["utc"])
    br.set_time(datetime(2025, 1, 17, 23, 59, 58))
    for u in (datetime(2025, 1, 19, 21, 0), datetime(2025, 1, 19, 22, 15), datetime(2025, 1, 19, 22, 21)):
        wall["utc"] = u  # the feed never comes back after the weekend
        eng2.step()
    assert len(sent(eng2, "no price updates for 21 min")) == 1


def test_auto_clock_change_is_adopted_or_blocked_after_three_observations(tmp_path):
    """Shared rule with the EA: a verified clock keeps allowing entries until CLOCK_CONFIRM observations
    disagree; 3 identical ones are adopted, 3 that disagree among themselves block new entries."""
    cfg = cfg_for(tmp_path, mode="auto")
    br = FakeBroker({"EURJPY": synthetic_m15(datetime(2025, 1, 13), 10, 160.0, 0.01)})
    wall = {"utc": datetime(2025, 1, 21, 8, 0, 5)}  # the VPS clock is exact; the broker's offset changes
    eng = LiveEngine(cfg, br, Notifier(cfg.notify), utcnow=lambda: wall["utc"])

    def loop(offset_h):
        wall["utc"] += timedelta(seconds=5)
        br.set_time(wall["utc"] + timedelta(hours=offset_h))
        eng.step()

    for _ in range(2):
        loop(2)  # NY7 in winter
    assert eng.clock_verified and eng.entry_block() == "" and eng.state.clock_mode == "ny7"
    for k, off in enumerate((3, 4, 5)):  # differing observations that never agree with each other
        loop(off)
        assert eng.entry_block() == ("" if k < 2 else "broker clock mismatch: observed +5.0 h, expected +2.0 h")
    assert sent(eng, "NEW ENTRIES BLOCKED") and not sent(eng, "CHANGED")
    for _ in range(4):  # then steady on GMT+0 (the first tick goes back in time: no observation)
        loop(0)
    assert (eng.clock.mode, eng.clock.offset) == ("fixed", 0.0) and eng.entry_block() == ""
    assert sent(eng, "server clock CHANGED")


def test_explicit_clock_allows_entries_from_the_start(tmp_path):
    cfg = cfg_for(tmp_path, mode="ny7")
    eng, _ = make(cfg, eurjpy())
    assert eng.clock_verified and eng.entry_block() == ""


def test_stale_feed_watchdog_only_on_market_days(tmp_path):
    cfg = cfg_for(tmp_path)
    br = FakeBroker({"EURJPY": synthetic_m15(datetime(2025, 1, 13), 14, 160.0, 0.01)})
    wall = {}
    eng = LiveEngine(cfg, br, Notifier(cfg.notify), utcnow=lambda: wall["utc"])
    br.set_time(datetime(2025, 1, 21, 10, 0, 5))  # Tuesday
    wall["utc"] = NY7.server_to_utc(br.now)
    eng.step()
    for m in (5, 9, 11, 30):  # server time frozen while the wall clock runs
        wall["utc"] = NY7.server_to_utc(br.now) + timedelta(minutes=m)
        eng.step()
    assert len(sent(eng, "no price updates for 11 min")) == 1 and len(sent(eng, "no price updates")) == 1  # hourly
    eng2 = LiveEngine(cfg_for(tmp_path / "w"), br, Notifier(cfg.notify), utcnow=lambda: wall["utc"])
    br.set_time(datetime(2025, 1, 17, 23, 59, 58))  # Friday close, weekend
    wall["utc"] = datetime(2025, 1, 18, 9, 0)
    eng2.step()
    wall["utc"] = datetime(2025, 1, 18, 12, 0)
    eng2.step()
    assert not sent(eng2, "no price updates")


# ------------------------------------------------------------------ fault isolation
def isolation_setup(tmp_path):
    syms = [("AUDCAD", 0.95, 1e-4), ("EURCAD", 1.50, 1e-4), ("GBPCAD", 1.70, 1e-4), ("EURJPY", 130.0, 0.01),
            ("EURUSD", 1.20, 1e-4)]
    data = {s: synthetic_m15(datetime(2021, 3, 1), 8, b, p, seed=7 + k) for k, (s, b, p) in enumerate(syms)}
    cfg = cfg_for(tmp_path, syms=[s for s, _, _ in syms[:4]], preset="stable5", es=True)
    cfg.risk.dd_scale_full = cfg.risk.daily_loss_limit = cfg.risk.monthly_hard_stop = 0.0
    eng, br = make(cfg, data, equity=1_000_000)
    opened = []
    orig = br.open_position
    br.open_position = lambda *a: (opened.append(a[0]), orig(*a))[1]
    return eng, br, opened


def test_one_broken_symbol_does_not_stop_the_others(tmp_path):
    eng, br, opened = isolation_setup(tmp_path)
    orig = br.rates

    def rates(base, tf, n):
        if base == "AUDCAD":
            raise RuntimeError("copy_rates AUDCAD failed")
        return orig(base, tf, n)

    br.rates = rates
    step_until(eng, br, datetime(2021, 3, 1), datetime(2021, 3, 8, 23, 45))
    assert {"EURCAD", "EURJPY", "EURUSD"} <= set(opened) and "AUDCAD" not in opened
    assert sent(eng, "AUDCAD failing for 60 loops")


def test_broken_euro_session_does_not_stop_night_reversion(tmp_path):
    eng, br, opened = isolation_setup(tmp_path)
    orig = br.rates

    def rates(base, tf, n):
        if tf == "H1":
            raise RuntimeError("copy_rates H1 failed")
        return orig(base, tf, n)

    br.rates = rates
    step_until(eng, br, datetime(2021, 3, 1), datetime(2021, 3, 8, 23, 45))
    assert {"EURCAD", "EURJPY"} <= set(opened) and "EURUSD" not in opened


def test_failing_symbol_is_notified_every_6_hours_and_in_the_daily_summary(tmp_path, monkeypatch):
    t = [0.0]
    monkeypatch.setattr(engine_mod.time, "monotonic", lambda: t[0])
    cfg = cfg_for(tmp_path, syms=["EURJPY", "GBPJPY"])
    data = {s: synthetic_m15(datetime(2025, 1, 13), 5, 160.0, 0.01, seed=k) for k, s in enumerate(("EURJPY", "GBPJPY"))}
    eng, br = make(cfg, data)
    orig = br.rates
    broken = {"GBPJPY"}

    def rates(base, tf, n):
        if base in broken:
            raise RuntimeError(f"copy_rates {base} M15 failed")
        return orig(base, tf, n)

    br.rates = rates
    start = datetime(2025, 1, 14, 0, 0, 5)  # Tuesday
    for k in range(289):  # every 5 minutes for 24 hours
        t[0] = k * 300.0
        br.set_time(start + timedelta(minutes=5 * k))
        eng.step()
    alerts = sent(eng, "GBPJPY failing for")
    assert len(alerts) == 4 and alerts[0].startswith("GBPJPY failing for 60 loops")  # 05:00, 11:00, 17:00, 23:00
    daily = sent(eng, "DAILY 2025-01-14")
    assert daily and "failing: GBPJPY (288 loops)" in daily[0]
    broken.clear()
    t[0] += 300
    br.set_time(start + timedelta(minutes=5 * 289))
    eng.step()
    assert sent(eng, "GBPJPY works again") and eng.failing() == []


def test_recovery_is_notified_only_after_an_error_notification(tmp_path, monkeypatch):
    """Failures that come and go: 'recovered' is sent only for a failure whose ERROR was sent, at most every 30
    minutes (it used to be sent after every failed loop: 360 notifications an hour)."""
    t = [0.0]
    monkeypatch.setattr(engine_mod.time, "monotonic", lambda: t[0])
    eng, br = make(cfg_for(tmp_path), eurjpy(start=datetime(2021, 3, 1), days=5))
    br.set_time(datetime(2021, 3, 2, 10, 0, 5))
    real_step, n = eng.step, [0]

    def flaky_step():  # every other loop fails
        n[0] += 1
        if n[0] % 2 == 0:
            raise RuntimeError("positions_get failed: (-10005, 'IPC timeout')")
        real_step()

    def fake_sleep(s):
        t[0] += 5
        br.set_time(br.now + timedelta(seconds=5))
        if t[0] > 3600:
            raise KeyboardInterrupt

    eng.step = flaky_step
    monkeypatch.setattr(engine_mod.time, "sleep", fake_sleep)
    assert eng.run_forever() == 10
    errors, recovered = sent(eng, "ERROR: positions_get failed"), sent(eng, "recovered after")
    assert len(errors) == 2 and len(recovered) == 2  # 360 fail/recover pairs in the hour


def test_unavailable_symbol_at_start_is_disabled_and_notified(tmp_path):
    cfg = cfg_for(tmp_path, syms=["EURJPY", "XYZJPY"])
    eng, br = make(cfg, eurjpy(spike_at=SPIKE), equity=1_000_000)
    br.set_time(datetime(2021, 3, 2, 22, 0, 5))
    eng.start()
    assert "XYZJPY" in eng.disabled and eng.symbols() == ["EURJPY"]
    assert sent(eng, "symbol XYZJPY unavailable") and sent(eng, "UNAVAILABLE: XYZJPY")
    step_until(eng, br, datetime(2021, 3, 2, 22, 0), datetime(2021, 3, 3, 6, 0))
    assert any(x[0] == "open" and x[1] == "EURJPY" for x in br.log)


# ------------------------------------------------------------------ account binding
def test_other_account_is_never_traded(tmp_path):
    eng, br = make(cfg_for(tmp_path), eurjpy(spike_at=SPIKE), equity=1_000_000)
    br.set_time(datetime(2021, 3, 2, 22, 0, 5))
    eng.step()
    assert eng.state.account_login == 111
    br.login, br.equity = 222, 50_000  # terminal logged into a demo account
    step_until(eng, br, datetime(2021, 3, 2, 22, 15), datetime(2021, 3, 3, 6, 0))
    assert not [x for x in br.log if x[0] == "open"]
    assert eng.state.peak_equity == 1_000_000 and eng.state.last_equity == 1_000_000  # no state updates
    assert len(sent(eng, "terminal is logged into account 222")) == 1  # rate limited
    br.login, br.equity = 111, 1_000_000
    br.set_time(datetime(2021, 3, 3, 6, 0, 10))
    eng.step()
    assert sent(eng, "trading resumes")


# ------------------------------------------------------------------ monitoring (contract 5)
def test_dd_alert_at_most_once_a_day(tmp_path):
    eng, br = make(cfg_for(tmp_path, preset="stable5"), eurjpy())
    br.set_time(datetime(2021, 3, 2, 22, 0, 5))
    eng.step()
    br.equity = 8_400  # 16% drawdown
    for s in (10, 15):
        br.set_time(datetime(2021, 3, 2, 22, 0, s))
        eng.step()
    assert len(sent(eng, "DD ALERT: drawdown 16.0%")) == 1 and not eng.state.halted
    br.set_time(datetime(2021, 3, 3, 0, 0, 5))  # next market day, still above the alert level
    eng.step()
    assert len(sent(eng, "DD ALERT")) == 2


def test_daily_summary_and_equity_log(tmp_path):
    cfg = cfg_for(tmp_path)
    eng, br = make(cfg, eurjpy())
    br.set_time(datetime(2021, 3, 2, 23, 50, 5))
    eng.step()
    br.equity = 10_100
    br.set_time(datetime(2021, 3, 2, 23, 55, 5))
    eng.step()
    br.set_time(datetime(2021, 3, 3, 0, 0, 5))
    eng.step()
    msg = sent(eng, "DAILY 2021-03-02")
    assert msg and "equity 10,100.00" in msg[0] and "month-to-date +1.00%" in msg[0] and "DD 0.0%" in msg[0]
    log = pd.read_csv(tmp_path / "journal" / "equity_log.csv")
    assert list(log.market_date) == ["2021-03-02"] and log.close_equity.iloc[0] == 10_100


def test_equity_log_of_an_older_version_keeps_its_columns(tmp_path):
    old = "market_date,time_utc,close_equity,peak_equity,drawdown,month_return,cash_flow_total,opens,closes," \
          "halted,entries_blocked\n2021-03-01,2021-03-01 21:00:05,10000,10000,0,0,0,0,0,False,\n"
    (tmp_path / "equity_log.csv").write_text(old, encoding="utf-8")
    j = TradeJournal(tmp_path)
    assert j.equity({"market_date": "2021-03-02", "close_equity": 10_100, "balance": 10_050})
    log = pd.read_csv(tmp_path / "equity_log.csv")  # still one consistent table
    assert list(log.market_date) == ["2021-03-01", "2021-03-02"] and "balance" not in log
    j2 = TradeJournal(tmp_path / "new")
    j2.equity({"market_date": "2021-03-02", "close_equity": 10_100, "balance": 10_050})
    assert pd.read_csv(tmp_path / "new" / "equity_log.csv").balance.tolist() == [10_050]


def test_trade_journal_open_and_close_with_r(tmp_path):
    cfg = cfg_for(tmp_path)
    eng, br = make(cfg, eurjpy(spike_at=SPIKE), equity=1_000_000)
    step_until(eng, br, datetime(2021, 3, 2, 22, 0), datetime(2021, 3, 3, 6, 0))
    j = pd.read_csv(tmp_path / "journal" / "trades_2021.csv")
    opens, closes = j[j.event == "open"], j[j.event == "close"]
    assert len(opens) >= 1 and len(closes) == len(opens) and eng.state.open_trades == {}
    o, c = opens.iloc[0], closes.iloc[0]
    assert o.ticket == c.ticket and o.base_risk == pytest.approx(0.01) and o.risk_amount > 0
    assert o.risk_pct == pytest.approx(o.risk_amount / 1_000_000)
    assert c.R == pytest.approx(c.pnl / o.risk_amount) and c.close_reason in ("TP", "SL", "time stop",
                                                                               "price already beyond moving average")
    closes_df = TradeJournal(tmp_path / "journal").closed_trades()
    assert len(closes_df) == len(closes)


def test_positions_from_an_older_version_are_adopted_and_journaled(tmp_path):
    eng, br = make(cfg_for(tmp_path), eurjpy(), equity=1_000_000)
    br.set_time(datetime(2021, 3, 2, 22, 0, 5))
    br.positions_.append(PositionView(77, "NR", "EURJPY", 1, 0.5, 130.0, br.now, 129.5, None))
    eng.step()
    assert eng.state.open_trades["77"]["risk_amount"] == pytest.approx(0.5 * 0.5 * 100000)
    br.close_position(77, "EURJPY")
    br.set_time(datetime(2021, 3, 2, 22, 0, 10))
    eng.step()
    c = TradeJournal(tmp_path / "journal").closed_trades()
    assert list(c.ticket) == [77] and c.reason.iloc[0] == "adopted position" and pd.notna(c.R.iloc[0])


def _journal_closes(directory, n, r, month="2021-03"):
    j = TradeJournal(directory)
    for k in range(n):
        j.trade({"event": "close", "time_utc": f"{month}-10 01:{k % 60:02d}:00", "strategy": "NR",
                 "symbol": "GBPCAD", "ticket": 1000 + k, "pnl": r * 100, "risk_amount": 100, "R": r,
                 "base_risk": 0.035})


def test_edge_monitor_warn_review_and_recovery(tmp_path):
    cfg = cfg_for(tmp_path)
    eng, br = make(cfg, eurjpy(start=datetime(2021, 3, 29), days=66))
    _journal_closes(tmp_path / "journal", 60, -0.1)
    for t in (datetime(2021, 3, 31, 23, 0, 5), datetime(2021, 4, 1, 0, 0, 5)):
        br.set_time(t)
        eng.step()
    assert sent(eng, "EDGE WARN (2021-03)") and eng.state.edge_neg_months == 1
    for t in (datetime(2021, 4, 30, 23, 0, 5), datetime(2021, 5, 3, 0, 0, 5)):
        br.set_time(t)
        eng.step()
    assert sent(eng, "EDGE REVIEW (2021-04)") and eng.state.edge_neg_months == 2
    _journal_closes(tmp_path / "journal", 200, 0.2, month="2021-05")
    for t in (datetime(2021, 5, 31, 23, 0, 5), datetime(2021, 6, 1, 0, 0, 5)):
        br.set_time(t)
        eng.step()
    assert sent(eng, "back to") and eng.state.edge_neg_months == 0


def test_report_edge_monitor_uses_market_months_like_the_engine():
    """NR trades of the first market day of April close on March 31 (21:00-23:59 UTC): the report must count them
    in April, as the engine's month-end evaluation does (it showed a March WARN the engine never sent)."""
    rows = [{"event": "close", "time_utc": f"2026-03-{2 + k // 3:02d} 01:{k % 3:02d}:00", "R": 0.10,
             "base_risk": 0.035} for k in range(60)]
    rows += [{"event": "close", "time_utc": f"2026-03-31 {21 + k // 3}:{(k % 3) * 20:02d}:00", "R": -1.5,
              "base_risk": 0.035} for k in range(6)]
    t = edge_monitor_table(pd.DataFrame(rows), 250)
    assert t.loc["2026-03", "trades"] == 60 and t.loc["2026-03", "status"] == "ok"
    assert t.loc["2026-04", "trades"] == 66 and t.loc["2026-04", "status"] == "WARN"


def test_risk_weighted_r_uses_base_risk_weights():
    df = pd.DataFrame({"R": [1.0, -1.0, -1.0], "base_risk": [0.035, 0.00875, 0.00875]})
    rw, n = risk_weighted_r(df, 250)
    assert n == 3 and rw == pytest.approx((0.035 - 0.0175) / 0.0525)
    assert risk_weighted_r(df, 2) == (pytest.approx(-1.0), 2)  # the last N trades only
    assert risk_weighted_r(df.iloc[0:0], 10)[1] == 0


# ------------------------------------------------------------------ bar data integrity
def test_history_gap_check(tmp_path):
    eng, _ = make(cfg_for(tmp_path), eurjpy())
    idx = pd.date_range("2021-02-26 20:00", "2021-02-26 23:45", freq="15min").append(
        pd.date_range("2021-03-01 00:00", periods=8, freq="15min"))
    df = pd.DataFrame({"close": 1.0}, index=idx)
    assert eng._gap_free(df, len(idx), M15)  # across the weekend close: fine
    assert not eng._gap_free(df.drop(df.index[-3]), len(idx) - 1, M15)  # a missing bar mid-week
    assert not eng._gap_free(df, len(idx) + 1, M15)  # not enough history


def test_signal_with_a_history_gap_is_not_traded(tmp_path, caplog):
    data = eurjpy(spike_at=SPIKE)
    data["EURJPY"] = data["EURJPY"].drop(pd.Timestamp(SPIKE) - pd.Timedelta(minutes=30))
    eng, br = make(cfg_for(tmp_path), data, equity=1_000_000)
    step_until(eng, br, datetime(2021, 3, 2, 22, 0), datetime(2021, 3, 3, 1, 0))
    assert not [x for x in br.log if x[0] == "open"]
    assert any("history gap" in r.message for r in caplog.records)


# ------------------------------------------------------------------ capacity caps live
def test_broker_volume_max_is_a_cap_before_the_guards(tmp_path):
    cfg = cfg_for(tmp_path)
    eng, br = make(cfg, eurjpy(spike_at=SPIKE), equity=1e9)
    br.volume_max = 2.0  # tighter than max_lots_per_order 20
    step_until(eng, br, datetime(2021, 3, 2, 22, 0), datetime(2021, 3, 3, 6, 0))
    vols = [x[3] for x in br.log if x[0] == "open"]
    assert vols and all(v == 2.0 for v in vols) and sent(eng, "lot cap: intended")
    j = pd.read_csv(tmp_path / "journal" / "trades_2021.csv")
    o = j[j.event == "open"].iloc[0]
    assert o.risk_amount == pytest.approx(2.0 * abs(o.intended_price - o.sl) * 100000)


# ------------------------------------------------------------------ state durability
def test_state_is_saved_on_change_and_equity_only_every_minute(tmp_path, monkeypatch):
    t = [0.0]
    monkeypatch.setattr(engine_mod.time, "monotonic", lambda: t[0])
    writes = []
    real = risk_mod.os.replace
    monkeypatch.setattr(risk_mod.os, "replace", lambda a, b: (writes.append(1), real(a, b)))
    eng, br = make(cfg_for(tmp_path), eurjpy())
    br.set_time(datetime(2021, 3, 2, 10, 0, 5))
    eng.step()
    n = len(writes)
    assert n >= 1
    for k in range(5):  # equity moves every loop (floating P/L)
        t[0] += 5
        br.equity += 1
        br.set_time(datetime(2021, 3, 2, 10, 0, 10 + k))
        eng.step()
    assert len(writes) == n
    t[0] += 60
    eng.step()
    assert len(writes) == n + 1
    br.add_cash(100)  # an important change is written at once
    eng.step()
    assert len(writes) == n + 2


def test_corrupt_state_starts_halted_and_notifies(tmp_path):
    cfg = cfg_for(tmp_path)
    (tmp_path / "state.json").write_text("{broken", encoding="utf-8")
    eng, br = make(cfg, eurjpy(spike_at=SPIKE), equity=1_000_000)
    assert eng.state.halted
    br.set_time(datetime(2021, 3, 2, 22, 0, 5))
    eng.start()
    assert sent(eng, "state file was unreadable") and sent(eng, "HALTED")
    step_until(eng, br, datetime(2021, 3, 2, 22, 0), datetime(2021, 3, 3, 6, 0))
    assert not [x for x in br.log if x[0] == "open"]


def test_dry_run_journals_intent_without_orders(tmp_path):
    cfg = cfg_for(tmp_path)
    cfg.engine.dry_run = True
    eng, br = make(cfg, eurjpy(spike_at=SPIKE), equity=1_000_000)
    step_until(eng, br, datetime(2021, 3, 2, 22, 0), datetime(2021, 3, 3, 6, 0))
    assert br.log == []
    j = pd.read_csv(tmp_path / "journal" / "trades_2021.csv")
    assert (j.event == "open").all() and j.reason.str.startswith("[DRY-RUN]").all() and (j.ticket == 0).all()
