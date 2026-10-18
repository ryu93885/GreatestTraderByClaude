"""D7: cash flows decided by AMOUNTS (real MT5 adapter + the fake MetaTrader5 module; shared rule with the EA, spec
section 5). The reference - balance + credit and the newest deal at the last reconciliation, plus the changes
absorbed as profit/loss - is persisted in state.json, so a restart reconciles like any other loop.

Review findings covered: py-cashflow#1 (a deal that does not move the balance 'explained' a lagging flow),
#2 (a deal later than the 60 s window was applied twice), #3 (unreadable history ran the guards on an unexplained
change), #5 (priming on an empty history), ea-cashflow#1 (restart after a flow made while stopped), #3 (deal seen
before its balance change), parity#1 (30-day blind spot after a long stop)."""
import json
from datetime import timedelta

import pytest

from fake_mt5 import S
import fake_mt5
from greatest_trader.broker import mt5_broker
from greatest_trader.broker.mt5_broker import MT5Broker
from greatest_trader.engine import ABSORBED_MAX, FLOW_FIRST_SECONDS, LiveEngine
from greatest_trader.notify import Notifier
from greatest_trader.risk import RiskState
from test_cash_flows_mt5 import Clock, advance, book, deal, make, sent


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(mt5_broker.time, "monotonic", c.monotonic)  # the time module: the engine's clock too
    monkeypatch.setattr(mt5_broker.time, "sleep", lambda s: None)
    return c


def entry(ticket, commission=0.0):
    """The engine's NR position on EURJPY and its entry deal; a commission also moves the balance."""
    S.positions.append(dict(ticket=ticket, identifier=ticket, symbol="EURJPY", magic=7101, type=0, volume=1.0,
                            price_open=130.0, time=S.now, sl=129.5, tp=130.5))
    S.deal_ticket += 1
    S.deals.append(dict(ticket=S.deal_ticket, time=S.now, time_msc=S.now * 1000, type=0, entry=0, magic=7101,
                        position_id=ticket, symbol="EURJPY", volume=1.0, price=130.0, profit=0.0,
                        commission=commission, swap=0, fee=0, reason=3, comment=""))
    book(commission)


def exit_(ticket, pnl, balance=True):
    """The position closes: exit deal (and the balance, unless the terminal shows the deal first)."""
    S.positions = [p for p in S.positions if p["ticket"] != ticket]
    S.deal_ticket += 1
    S.deals.append(dict(ticket=S.deal_ticket, time=S.now, time_msc=S.now * 1000, type=1, entry=1, magic=7101,
                        position_id=ticket, symbol="EURJPY", volume=1.0, price=130.0, profit=pnl, commission=0.0,
                        swap=0, fee=0, reason=5, comment=""))
    if balance:
        book(pnl)


def restart(eng):
    """The engine process stops (state saved) and a new one starts on the same terminal."""
    eng.state.save(eng.cfg.engine.state_file)
    cfg = eng.cfg
    eng2 = LiveEngine(cfg, MT5Broker(cfg), Notifier(cfg.notify), utcnow=eng._utcnow)
    eng2.start()
    return eng2


def pending(eng):
    return eng._flow_pending and eng.entry_block().startswith("balance change not in the deal history")


def history_fails(monkeypatch, on: dict):
    real_get, real_err = fake_mt5.history_deals_get, fake_mt5.last_error
    monkeypatch.setattr(fake_mt5, "history_deals_get", lambda *a, **k: None if on["on"] else real_get(*a, **k))
    monkeypatch.setattr(fake_mt5, "last_error", lambda: (-10005, "IPC timeout") if on["on"] else real_err())


# ------------------------------------------------------------------ py-cashflow#1
@pytest.mark.parametrize("entry_loop", ["earlier", "same"])
def test_entry_without_commission_never_explains_a_lagging_withdrawal_or_deposit(monkeypatch, tmp_path, clock,
                                                                                 entry_loop):
    """No-commission entry: the entry deal does not move the balance. A 4% withdrawal and then a 10% deposit
    whose deals reach the history one loop late: pending (no MONTH STOP, no peak raise), then applied once.
    The entry deal used to count as 'a newer deal' that explained the withdrawal (MONTH STOP, close-all) and
    the deposit (counted as profit, then applied again at the next close: phantom drawdown)."""
    eng = make(monkeypatch, tmp_path)
    eng.step()
    entry(555)
    if entry_loop == "earlier":  # the entry deal reconciles on its own (amount 0 = change 0)
        advance(clock)
        eng.step()
        assert not eng._flow_pending and eng.state.cash_deal_ref == S.deal_ticket
    book(-400)
    advance(clock)
    eng.step()
    st = eng.state
    assert pending(eng) and not st.month_halted and len(S.positions) == 1 and st.peak_equity == 10_000
    deal(-400.0, "withdrawal")
    advance(clock)
    eng.step()
    assert not eng._flow_pending and not st.month_halted and len(S.positions) == 1
    assert st.peak_equity == pytest.approx(9_600) and st.month_start_equity == pytest.approx(9_600)
    book(1_000)
    advance(clock)
    eng.step()
    assert pending(eng) and st.peak_equity == pytest.approx(9_600)
    deal(1_000.0, "deposit")
    advance(clock)
    eng.step()
    assert st.peak_equity == pytest.approx(10_600)
    exit_(555, 20.0)  # the position closes at a profit: a trade deal explains the change
    advance(clock)
    eng.step()
    assert not eng._flow_pending and st.peak_equity == pytest.approx(10_620) and eng.risk.drawdown(S.equity) == 0
    assert st.cash_flow_total == pytest.approx(600) and len(sent(eng, "CASH FLOW")) == 2
    assert not sent(eng, "STOP") and not sent(eng, "HALT") and not sent(eng, "without a matching deal")


# ------------------------------------------------------------------ ea-cashflow#3
def test_trade_deal_seen_before_its_balance_change_is_a_short_pending(monkeypatch, tmp_path, clock):
    """The close deal is in the history one loop before the balance shows it: one loop pending (deals 100 vs
    change 0), then reconciled when the balance catches up - no 60 s wait, no 'without a matching deal' alert."""
    eng = make(monkeypatch, tmp_path)
    eng.step()
    entry(555, commission=-3.0)
    advance(clock)
    eng.step()
    exit_(555, -100.0, balance=False)
    advance(clock, 1)
    eng.step()
    assert pending(eng)
    book(-100)
    advance(clock, 1)
    eng.step()
    assert not eng._flow_pending and eng.entry_block() == "" and eng.state.peak_equity == 10_000
    for _ in range(14):
        advance(clock)
        eng.step()
    assert not sent(eng, "without a matching deal") and not sent(eng, "CASH FLOW")
    assert eng.state.cash_money_ref == pytest.approx(9_897) and eng.state.cash_deal_ref == S.deal_ticket


# ------------------------------------------------------------------ py-cashflow#2
def later_trade(eng, clock, pnl=30.0):
    for _ in range(3):
        advance(clock, 60)
        eng.step()
    entry(777, commission=-3.0)
    advance(clock)
    eng.step()
    exit_(777, pnl)
    advance(clock)
    eng.step()


@pytest.mark.parametrize("late,with_close,restart_between", [(75, False, False), (600, False, False),
                                                             (75, True, False), (300, False, True)])
def test_cash_deal_later_than_the_window_is_not_applied_again(monkeypatch, tmp_path, clock, late, with_close,
                                                              restart_between):
    """A +2,000 deposit whose deal reaches the history `late` seconds after the balance: after 60 s it counts as
    profit (documented, notified) and is kept as an absorbed record (persisted). When the deal shows up - alone,
    together with a trade close, or after an engine restart - it is matched to that record: marked seen, NOT
    applied again (it used to be applied at the next balance change: peak 14,400, DD 16.5%)."""
    eng = make(monkeypatch, tmp_path)
    eng.step()
    if with_close:
        entry(555, commission=-3.0)
        advance(clock)
        eng.step()
    book(2_000)
    t = 0
    while t < late:
        advance(clock)
        t += 5
        eng.step()
    st = eng.state
    assert sent(eng, "balance / credit changed by +2,000.00 without a matching deal in the history for 60 s")
    assert st.peak_equity == pytest.approx(S.equity) and [r[0] for r in st.cash_absorbed] == [2_000.0]
    if restart_between:
        eng = restart(eng)
        st = eng.state
        assert [r[0] for r in st.cash_absorbed] == [2_000.0]
    deal(2_000.0, "deposit", when=S.booked_at)  # booked before the change was counted as profit/loss
    dep = S.deal_ticket
    if with_close:
        exit_(555, 27.0)  # the same loop: change +27, new deals +2,027
    advance(clock)
    eng.step()
    assert not eng._flow_pending and st.cash_flow_total == 0
    assert dep in {t for t, _ in st.cash_tickets}  # seen: never applied later
    assert sent(eng, "late cash deal +2,000.00") and sent(eng, "already counted as profit/loss, not applied again")
    assert not sent(eng, "CASH FLOW")
    later_trade(eng, clock)
    assert st.peak_equity == pytest.approx(S.equity) and eng.risk.drawdown(S.equity) == 0 and not st.halted
    assert eng.risk.risk_fraction("nr", S.equity) == pytest.approx(eng.risk.cfg.base_risk("nr"))


def test_an_absorbed_record_matches_only_when_the_rest_reconciles(monkeypatch, tmp_path, clock):
    """A late deal of the absorbed amount arrives while another balance change is still unexplained: pending
    (not a late deal yet); once that change's deal shows, both are decided - the late deal not applied again,
    the withdrawal applied."""
    eng = make(monkeypatch, tmp_path)
    eng.step()
    book(2_000)
    for _ in range(14):
        advance(clock)
        eng.step()
    assert [r[0] for r in eng.state.cash_absorbed] == [2_000.0]
    deal(2_000.0, "deposit", when=S.booked_at)
    book(-500)  # a withdrawal, its deal one loop late
    advance(clock)
    eng.step()
    assert pending(eng) and eng.state.cash_absorbed
    deal(-500.0, "withdrawal")
    advance(clock)
    eng.step()
    st = eng.state
    assert not eng._flow_pending and st.cash_flow_total == pytest.approx(-500)
    assert st.peak_equity == pytest.approx(11_500) and len(sent(eng, "late cash deal")) == 1


def test_absorbed_records_are_capped_and_expire_after_7_days(monkeypatch, tmp_path, clock):
    eng = make(monkeypatch, tmp_path)
    eng.step()
    for k in range(ABSORBED_MAX + 2):
        eng._absorb(100.0 + k)
    assert [r[0] for r in eng.state.cash_absorbed] == [102.0, 103.0, 104.0, 105.0, 106.0]
    S.now += 8 * 86400
    eng._prune_absorbed()
    assert eng.state.cash_absorbed == []


# ------------------------------------------------------------------ py-cashflow#3
def test_history_outage_at_a_deposit_is_pending_not_raw_equity(monkeypatch, tmp_path, clock):
    """The history read fails for one loop exactly when a +3,000 deposit reaches the balance: pending (no peak
    raise). The next loop applies it once (it used to raise the peak in the outage loop and apply it again:
    peak 16,900, 'HALT: max drawdown 23.1%')."""
    eng = make(monkeypatch, tmp_path)
    fail = {"on": False}
    history_fails(monkeypatch, fail)
    eng.step()
    book(3_000)
    deal(3_000.0, "deposit")
    fail["on"] = True
    advance(clock)
    eng.step()
    st = eng.state
    assert eng._flow_pending and eng.entry_block().startswith("deal history cannot be read")
    assert st.peak_equity == 10_000 and sent(eng, "cash-flow check failed")
    fail["on"] = False
    advance(clock)
    eng.step()
    assert not eng._flow_pending and st.peak_equity == pytest.approx(13_000) and not st.halted
    assert len(sent(eng, "CASH FLOW +3,000.00")) == 1 and eng.entry_block() == ""


def test_unreadable_history_while_the_balance_changes_is_pending_then_expires(monkeypatch, tmp_path, clock):
    """A -400 withdrawal (4%) with a position open while the deal history stays unreadable: pending for 60 s (no
    MONTH STOP, the position stays), then it counts as a loss (documented; here the 3% monthly stop) and is kept
    as an absorbed record. When the history is readable again, the deal is a late deal: not applied again."""
    eng = make(monkeypatch, tmp_path)
    fail = {"on": False}
    history_fails(monkeypatch, fail)
    eng.step()
    entry(555, commission=-3.0)
    advance(clock)
    eng.step()
    fail["on"] = True
    book(-400)
    st = eng.state
    for _ in range(13):  # up to 60 s
        advance(clock)
        eng.step()
        assert eng._flow_pending and not st.month_halted and len(S.positions) == 1
    advance(clock)
    eng.step()
    assert sent(eng, "changed by -400.00 without a matching deal in the history for 60 s (the deal history cannot "
                     "be read): treated as profit/loss")
    assert st.month_halted and S.positions == [] and [r[0] for r in st.cash_absorbed] == [-400.0]
    month_start = st.month_start_equity
    deal(-400.0, "withdrawal", when=S.booked_at)
    fail["on"] = False
    advance(clock)
    eng.step()
    assert not eng._flow_pending and st.cash_flow_total == 0
    assert st.month_start_equity == month_start and sent(eng, "late cash deal -400.00")


# ------------------------------------------------------------------ ea-cashflow#1 / parity#1: restarts
@pytest.mark.parametrize("deal_after", [20, 120])
def test_restart_after_a_withdrawal_made_while_stopped(monkeypatch, tmp_path, clock, deal_after):
    """The engine is stopped, 3,000 is withdrawn, the engine starts again and the withdrawal deal reaches the
    history `deal_after` seconds later. The persisted reference makes the first loops pending - up to 300 s in the
    first reconciliation after a start - instead of 'HALT 30%' + MONTH STOP + close-all."""
    eng = make(monkeypatch, tmp_path)
    eng.step()
    advance(clock)
    eng.step()
    book(-3_000)
    advance(clock, 600)
    eng2 = restart(eng)
    st = eng2.state
    t = 0
    while t < deal_after:
        eng2.step()
        assert pending(eng2) and not st.halted and not st.month_halted and st.peak_equity == 10_000
        advance(clock)
        t += 5
    deal(-3_000.0, "withdrawal")
    eng2.step()
    assert not eng2._flow_pending and st.peak_equity == pytest.approx(7_000) and eng2.risk.drawdown(7_000) == 0
    assert sent(eng2, "CASH FLOW -3,000.00") and not sent(eng2, "HALT") and not sent(eng2, "STOP")
    assert st.cash_money_ref == pytest.approx(7_000) and st.cash_deal_ref == S.deal_ticket


def test_restart_window_is_300_s_then_the_change_counts_as_profit_loss(monkeypatch, tmp_path, clock):
    eng = make(monkeypatch, tmp_path)
    eng.step()
    book(500)
    eng2 = restart(eng)
    for _ in range(int(FLOW_FIRST_SECONDS / 5) + 1):  # the timer starts at the first loop: 300 s after it
        advance(clock)
        eng2.step()
        assert pending(eng2)
    advance(clock)
    eng2.step()
    assert not eng2._flow_pending and sent(eng2, "without a matching deal in the history for 300 s")
    assert eng2.state.peak_equity == pytest.approx(10_500)
    book(-100)  # the next unexplained change (no restart since): the normal 60 s window
    for _ in range(13):
        advance(clock)
        eng2.step()
    assert pending(eng2)
    advance(clock)
    eng2.step()
    assert not eng2._flow_pending and sent(eng2, "without a matching deal in the history for 60 s")


@pytest.mark.parametrize("days", [20, 33, 70])
def test_withdrawal_during_a_stop_longer_than_30_days_is_a_cash_flow(monkeypatch, tmp_path, clock, days):
    """parity#1: the history is read from the reference deal's time minus a day (not only the last 30 days), so a
    withdrawal booked one hour after the stop is found after a stop of any length (33 days: it used to be a
    loss - 'DD 20%', NR risk 0.35% instead of 3.5%)."""
    eng = make(monkeypatch, tmp_path)
    S.filter_deal_times = True  # like MT5: history_deals_get returns the deals of the requested window only
    eng.step()
    advance(clock)
    eng.step()
    eng.state.save(eng.cfg.engine.state_file)
    S.now += 3600
    deal(-2_000.0, "withdrawal")
    book(-2_000)
    S.now += days * 86400
    S.ticks["EURJPY"]["time"] = S.now
    eng2 = restart(eng)
    for _ in range(3):
        advance(clock)
        eng2.step()
    st = eng2.state
    assert st.peak_equity == pytest.approx(8_000) and eng2.risk.drawdown(S.equity) == 0 and not st.halted
    assert st.cash_flow_total == pytest.approx(-2_000) and sent(eng2, "CASH FLOW -2,000.00")
    assert eng2.risk.risk_fraction("nr", S.equity) == pytest.approx(eng2.risk.cfg.base_risk("nr"))


def test_reset_rebase_waits_for_a_deposit_made_while_stopped(monkeypatch, tmp_path, clock):
    """D6 order with the persisted reference: the deposit's deal lags after the restart, so the first loops are
    pending and the reset waits; the peak is rebased only after the deposit has been applied to the old peak."""
    eng = make(monkeypatch, tmp_path)
    eng.step()
    st = eng.state
    st.halted, st.halt_reason, st.peak_equity = True, "max drawdown 21.0% reached", 12_700.0
    st.reset_request = "rebase"
    book(5_000)
    eng2 = restart(eng)
    advance(clock)
    eng2.step()
    assert pending(eng2) and eng2.state.halted and eng2.state.reset_request == "rebase"
    deal(5_000.0, "deposit while stopped")
    advance(clock)
    eng2.step()
    st = eng2.state
    assert not st.halted and st.reset_request == "" and st.peak_equity == pytest.approx(15_000)
    msgs = list(eng2.notifier.recent)
    i_flow = next(i for i, m in enumerate(msgs) if "CASH FLOW +5,000.00" in m)
    i_reset = next(i for i, m in enumerate(msgs) if "RESET HALT" in m)
    assert i_flow < i_reset and not sent(eng2, "HALT: max drawdown")


# ------------------------------------------------------------------ upgrade / persistence
def test_state_of_an_older_version_takes_the_reference_without_priming_again(monkeypatch, tmp_path, clock):
    """state.json written before D7 (no cash_money_ref / cash_deal_ref / cash_absorbed): it loads with defaults;
    the first successful read is the reference (no priming again, nothing applied twice); the lag rule works from
    the next loop on and the new fields are saved."""
    old = RiskState(peak_equity=10_000, day="2021-03-02", month="2021-03", day_start_equity=10_000,
                    month_start_equity=10_000, last_equity=10_000, account_login=111, account_server="Demo",
                    cash_init=True, cash_tickets=[[9001, 0.0]])
    data = {k: v for k, v in json.loads(json.dumps(old.__dict__)).items()
            if not k.startswith("cash_") or k in ("cash_init", "cash_tickets", "cash_flow_total")}
    data.pop("restored_from", None)
    (tmp_path / "state.json").write_text(json.dumps(data), encoding="utf-8")
    eng = make(monkeypatch, tmp_path)  # the history holds the initial deposit 9001 (already seen)
    st = eng.state
    assert st.cash_money_ref is None and st.cash_deal_ref == 0 and st.cash_absorbed == []
    eng.step()
    assert st.cash_money_ref == pytest.approx(10_000) and st.cash_deal_ref == 9001
    assert not sent(eng, "CASH FLOW") and st.cash_flow_total == 0 and eng.entry_block() == ""
    book(-400)
    advance(clock)
    eng.step()
    assert pending(eng) and not st.month_halted
    deal(-400.0, "withdrawal")
    advance(clock)
    eng.step()
    assert st.peak_equity == pytest.approx(9_600) and len(sent(eng, "CASH FLOW -400.00")) == 1
    saved = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert saved["cash_money_ref"] == pytest.approx(9_600) and saved["cash_deal_ref"] == S.deal_ticket
    assert saved["cash_deal_ref_time"] > 0 and saved["cash_absorbed"] == []


def test_credit_deals_reconcile_against_balance_plus_credit(monkeypatch, tmp_path, clock):
    eng = make(monkeypatch, tmp_path)
    eng.step()
    S.credit += 500
    S.equity += 500
    advance(clock)
    eng.step()
    assert pending(eng)
    deal(500.0, "credit", type_=3)
    advance(clock)
    eng.step()
    assert not eng._flow_pending and eng.state.peak_equity == pytest.approx(10_500)
    assert sent(eng, "CASH FLOW +500.00") and eng.state.cash_money_ref == pytest.approx(10_500)


# ------------------------------------------------------------------ py-cashflow#5: priming
def test_priming_never_takes_an_empty_history_as_stable(monkeypatch, tmp_path, clock):
    """11,000 on the account and no deal in the history for minutes: never 'unchanged for 60 s = complete'. When
    the deposits arrive they are history (seen, not applied: it used to prime on the empty set and apply the
    10,000 deposit later with k = 11: 'HALT 90.7%')."""
    eng = make(monkeypatch, tmp_path, history=False, balance=11_000.0)
    for _ in range(30):  # 150 s of an empty history
        eng.step()
        advance(clock)
    assert not eng.state.cash_init
    deal(10_000.0, "initial deposit", when=S.now - 20 * 86400)
    deal(1_000.0, "deposit", when=S.now - 3 * 86400)
    eng.step()
    st = eng.state
    assert st.cash_init and st.cash_flow_total == 0 and not sent(eng, "WARNING") and not sent(eng, "CASH FLOW")
    assert st.cash_money_ref == pytest.approx(11_000) and st.cash_deal_ref == S.deal_ticket


def test_priming_on_a_stable_set_without_matching_sums_is_notified(monkeypatch, tmp_path, clock):
    eng = make(monkeypatch, tmp_path, history=False, balance=11_000.0)
    deal(10_000.0, "initial deposit", when=S.now - 20 * 86400)  # 1,000 of profit deals are missing
    for _ in range(14):
        eng.step()
        advance(clock)
    assert eng.state.cash_init
    assert sent(eng, "WARNING: the deal history was not confirmed complete (its cash deals were unchanged for 60 s)")


def test_priming_on_an_empty_history_after_10_minutes_is_notified(monkeypatch, tmp_path, clock):
    eng = make(monkeypatch, tmp_path, history=False, balance=11_000.0)
    for _ in range(21):
        eng.step()
        advance(clock, 30)
    assert eng.state.cash_init and sent(eng, "not confirmed complete within 10 min")


def test_broker_reads_from_the_reference_deal_time_minus_one_day(monkeypatch, tmp_path, clock):
    """The window of the reconciliation read: from the reference deal's time minus a day (or the last 2 days if
    that starts earlier) to now + 2 days, every deal type with a ticket above the reference."""
    eng = make(monkeypatch, tmp_path)
    eng.step()
    windows = []
    real = fake_mt5.history_deals_get

    def spy(date_from=None, date_to=None, position=None):
        if position is None:
            windows.append((date_from, date_to))
        return real(date_from, date_to, position)

    monkeypatch.setattr(fake_mt5, "history_deals_get", spy)
    advance(clock)
    eng.step()
    ref = mt5_broker._ts(S.now - 5 - 7 * 86400)  # the initial deposit 9001 = the reference deal
    assert windows[-1][0] == ref - timedelta(days=1) and windows[-1][1] >= mt5_broker._ts(S.now) + timedelta(days=2)


# ------------------------------------------------------------------ review5 py-cash#1: late change of several deals
@pytest.mark.parametrize("parts", [[(5_000.0, 2), (-50.0, 2)], [(5_000.0, 2), (27.0, 1)], [(3_000.0, 2), (1_500.0, 6)]])
def test_late_change_made_of_several_deals_is_not_applied_again(monkeypatch, tmp_path, clock, parts):
    """A deposit with a separate fee deal, a deposit and a trade close, or a deposit and a bonus reach the history
    after the 60 s window, so their change was counted as profit/loss. Every deal booked before that moment is
    already in the reference balance (server-time watermark): none is applied again - no phantom drawdown, no
    false HALT (it used to apply the deposit a second time: 'HALT: max drawdown 33%')."""
    eng = make(monkeypatch, tmp_path)
    eng.step()
    book(sum(a for a, _ in parts))
    for _ in range(14):
        advance(clock)
        eng.step()
    st = eng.state
    assert sent(eng, "without a matching deal in the history for 60 s") and st.peak_equity == pytest.approx(S.equity)
    for amount, type_ in parts:
        deal(amount, "late", when=S.booked_at, type_=type_)
    advance(clock)
    eng.step()
    assert not eng._flow_pending and st.cash_flow_total == 0 and not sent(eng, "CASH FLOW")
    assert st.peak_equity == pytest.approx(S.equity) and eng.risk.drawdown(S.equity) == 0 and not st.halted
    assert st.cash_money_ref == pytest.approx(S.balance) and st.cash_deal_ref == S.deal_ticket
    deal(1_000.0, "a new deposit later")  # booked after the watermark: a normal cash flow
    book(1_000)
    advance(clock)
    eng.step()
    assert st.cash_flow_total == pytest.approx(1_000) and len(sent(eng, "CASH FLOW +1,000.00")) == 1
