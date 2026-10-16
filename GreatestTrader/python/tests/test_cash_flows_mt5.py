"""Cash flows with the real MT5 adapter and the fake MetaTrader5 module (shared rule with the EA, spec section 5):
the lag guard (a balance change without a deal that explains it is 'pending' for at most 60 s), k computed from an
equity read after the deal scan, the first-run priming of the cash-flow cursor and the isolation of a failing deal
history."""
import calendar
from datetime import datetime

import numpy as np
import pytest

import fake_mt5
from fake_mt5 import S
from fakes import synthetic_m15
from greatest_trader.broker import mt5_broker
from greatest_trader.broker.mt5_broker import MT5Broker
from greatest_trader.config import load_config
from greatest_trader.engine import LiveEngine
from greatest_trader.notify import Notifier
from greatest_trader.timeutil import ServerClock

NY7 = ServerClock("ny7")


class Clock:
    def __init__(self):
        self.t = 1000.0

    def monotonic(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(mt5_broker.time, "monotonic", c.monotonic)  # the time module: the engine's clock too
    monkeypatch.setattr(mt5_broker.time, "sleep", lambda s: None)
    return c


def setup_symbol(name, df):
    S.specs[name] = dict(digits=3, point=0.001, volume_min=0.01, volume_max=100.0, volume_step=0.01,
                         trade_stops_level=0, filling_mode=1)
    arr = np.zeros(len(df), dtype=[("time", "i8"), ("open", "f8"), ("high", "f8"), ("low", "f8"), ("close", "f8"),
                                   ("tick_volume", "i8"), ("spread", "i4"), ("real_volume", "i8")])
    arr["time"] = (df.index.astype("int64") // 10**9).to_numpy()
    for c in ("open", "high", "low", "close"):
        arr[c] = df[c].to_numpy()
    arr["spread"] = 10
    S.rates[(name, fake_mt5.TIMEFRAME_M15)] = arr


def deal(amount, comment="", when=None, type_=2):
    S.deal_ticket += 1
    S.deals.append(dict(ticket=S.deal_ticket, time=S.now if when is None else when, time_msc=0, type=type_, entry=0,
                        magic=0, position_id=0, symbol="", volume=0, price=0, profit=amount, commission=0, swap=0,
                        fee=0, reason=0, comment=comment))


def book(amount):
    """The account shows a deposit (+) / withdrawal (-) at once; its deal is added separately (deal())."""
    S.balance += amount
    S.equity += amount


def advance(clock, seconds=5):
    clock.t += seconds
    S.now += seconds
    S.ticks["EURJPY"]["time"] = S.now


def make(monkeypatch, tmp_path, history=True, balance=10_000.0):
    fake_mt5.reset()
    monkeypatch.setattr(mt5_broker, "mt5", fake_mt5)
    setup_symbol("EURJPY", synthetic_m15(datetime(2021, 3, 1), 12, 130.0, 0.01))
    S.now = calendar.timegm(datetime(2021, 3, 3, 10, 0, 3).timetuple())
    S.ticks["EURJPY"] = dict(time=S.now, bid=130.0, ask=130.01)
    S.balance = S.equity = balance
    cfg = load_config(None, preset="stable5")
    cfg.night_reversion.symbols = ["EURJPY"]
    cfg.euro_session.enabled = False
    cfg.engine.dry_run = False
    cfg.broker.server_time_mode = "ny7"
    cfg.engine.state_file = str(tmp_path / "state.json")
    cfg.engine.journal_dir = str(tmp_path / "journal")
    cfg.risk.blackout_start = cfg.risk.blackout_end = ""
    if history:
        deal(balance, "initial deposit", when=S.now - 7 * 86400)  # the history adds up to the balance
    eng = LiveEngine(cfg, MT5Broker(cfg), Notifier(cfg.notify),
                     utcnow=lambda: NY7.server_to_utc(mt5_broker._ts(S.now)))
    eng.start()
    return eng


def sent(eng, text):
    return [m for m in eng.notifier.recent if text in m]


def position():
    S.positions.append(dict(ticket=555, identifier=555, symbol="EURJPY", magic=7101, type=0, volume=1.0,
                            price_open=130.0, time=S.now - 600, sl=129.5, tp=130.5))


# ------------------------------------------------------------------ lag guard
def test_deposit_whose_deal_arrives_one_loop_late_is_no_profit(monkeypatch, tmp_path, clock):
    eng = make(monkeypatch, tmp_path)
    eng.step()
    assert eng.state.cash_init and eng.entry_block() == ""
    book(5_000)  # the account shows the deposit, the deal reaches the history 5 s later
    advance(clock)
    eng.step()
    assert eng.state.peak_equity == 10_000 and eng.entry_block().startswith("balance change not in the deal history")
    deal(5_000.0, "deposit")
    advance(clock)
    eng.step()
    st = eng.state
    assert st.peak_equity == pytest.approx(15_000) and not st.halted and eng.risk.drawdown(15_000) == 0
    assert sent(eng, "CASH FLOW +5,000.00") and not sent(eng, "HALT") and not sent(eng, "DD ALERT")
    assert eng.entry_block() == ""  # was: peak 22,500, 'HALT: max drawdown 33.3%'


def test_withdrawal_whose_deal_arrives_one_loop_late_is_no_loss(monkeypatch, tmp_path, clock):
    eng = make(monkeypatch, tmp_path)
    eng.step()
    position()
    book(-400)  # 4% withdrawal; the deal one loop later
    advance(clock)
    eng.step()
    deal(-400.0, "withdrawal")
    advance(clock)
    eng.step()
    st = eng.state
    assert not st.month_halted and not st.day_halted and len(S.positions) == 1  # was: MONTH STOP, flattened
    assert st.peak_equity == pytest.approx(9_600) and st.month_start_equity == pytest.approx(9_600)
    assert sent(eng, "CASH FLOW -400.00") and not sent(eng, "STOP")


def test_pending_lasts_at_most_60_seconds(monkeypatch, tmp_path, clock):
    """A balance change that no deal explains within 60 s counts as profit / loss (notified): the guards run
    again. Here a 4% drop: the monthly hard stop then fires (the deal never reached the history)."""
    eng = make(monkeypatch, tmp_path)
    eng.step()
    position()
    book(-400)
    for _ in range(13):  # up to 60 s: still pending, no stop decision, no entry, closes would still run
        advance(clock)
        eng.step()
        assert eng.entry_block().startswith("balance change not in the deal history")
    assert not eng.state.month_halted and len(S.positions) == 1
    advance(clock)
    eng.step()
    assert sent(eng, "without a matching deal in the history for 60 s: treated as profit/loss")
    assert eng.state.month_halted and S.positions == [] and sent(eng, "MONTH STOP")
    assert eng.entry_block() == ""  # the guards decide again (the monthly stop blocks entries in can_open)


def test_trade_close_explains_the_balance_change(monkeypatch, tmp_path, clock):
    """A balance change with a new TRADE deal is profit / loss: never pending."""
    eng = make(monkeypatch, tmp_path)
    eng.step()
    S.deal_ticket += 1
    S.deals.append(dict(ticket=S.deal_ticket, time=S.now, time_msc=0, type=1, entry=1, magic=7101, position_id=77,
                        symbol="EURJPY", volume=1.0, price=130.0, profit=-350.0, commission=0, swap=0, fee=0,
                        reason=4, comment="sl"))
    book(-350)
    advance(clock)
    eng.step()
    assert eng.entry_block() == "" and eng.state.peak_equity == 10_000 and not sent(eng, "CASH FLOW")


def test_cash_deal_seen_before_its_balance_change_waits_for_it(monkeypatch, tmp_path, clock):
    """Reverse order: the deal is in the history but balance and equity do not show it yet. k = E / (E - F) on an
    equity without F gave a phantom drawdown; the flow is applied once the balance shows it."""
    eng = make(monkeypatch, tmp_path)
    eng.step()
    deal(-400.0, "withdrawal")
    clock.t += 700  # the broker's periodic re-scan sees the deal
    advance(clock)
    eng.step()
    assert eng.state.peak_equity == 10_000 and eng.state.cash_flow_total == 0
    book(-400)
    advance(clock)
    eng.step()
    assert eng.state.peak_equity == pytest.approx(9_600) and eng.risk.drawdown(9_600) == 0
    assert len(sent(eng, "CASH FLOW -400.00")) == 1


def test_flow_booked_while_the_history_is_read_is_decided_next_loop(monkeypatch, tmp_path, clock):
    """The withdrawal is booked between the history read and the account read (EA finding ea#1): the scan missed
    the deal but the equity contains it. Nothing is applied in that loop; the next loop applies it."""
    eng = make(monkeypatch, tmp_path)
    eng.step()
    real = fake_mt5.history_deals_get

    def racing(*a, **k):
        out = real(*a, **k)
        if not any(d["profit"] == -5_000.0 for d in S.deals):
            book(-5_000)
            deal(-5_000.0, "withdrawal")
        return out

    monkeypatch.setattr(fake_mt5, "history_deals_get", racing)
    clock.t += 700  # periodic re-scan
    advance(clock)
    eng.step()
    assert eng.state.peak_equity == 10_000 and not eng.state.halted and eng.entry_block().startswith("balance")
    advance(clock)
    eng.step()
    st = eng.state
    assert st.peak_equity == pytest.approx(5_000) and not st.halted and not st.month_halted  # was: HALT 50%


# ------------------------------------------------------------------ first-run priming
def test_priming_waits_for_a_complete_history(monkeypatch, tmp_path, clock):
    """11,000 on the account = a 7-day-old deposit of 10,000 + 1,000 profit. The terminal's history is empty at
    the first scan; the deposit shows up later. It must be history, not a new flow (k = 11 -> 'HALT 90.9%')."""
    eng = make(monkeypatch, tmp_path, history=False, balance=11_000.0)
    eng.step()
    assert not eng.state.cash_init and eng.entry_block() == "deal history not confirmed complete yet (first run)"
    deal(10_000.0, "initial deposit", when=S.now - 7 * 86400)
    advance(clock, 30)
    eng.step()
    assert not eng.state.cash_init  # the set of cash deals changed: wait for 60 s without changes
    for _ in range(2):
        advance(clock, 30)
        eng.step()
    st = eng.state
    assert st.cash_init and [t for t, _ in st.cash_tickets] == [S.deal_ticket] and st.cash_flow_total == 0
    assert not st.halted and st.peak_equity == 11_000 and eng.entry_block() == "" and not sent(eng, "CASH FLOW")


def test_priming_at_once_when_the_deals_add_up_to_the_balance(monkeypatch, tmp_path, clock):
    eng = make(monkeypatch, tmp_path)
    S.deal_ticket += 1  # a closed trade with commission: every deal type but CREDIT counts toward the balance
    S.deals.append(dict(ticket=S.deal_ticket, time=S.now - 3600, time_msc=0, type=1, entry=1, magic=7101,
                        position_id=7, symbol="EURJPY", volume=1.0, price=130.0, profit=250.0, commission=-6.0, swap=0,
                        fee=0, reason=5, comment="tp"))
    deal(500.0, "credit", type_=3)
    S.balance, S.credit, S.equity = 10_244.0, 500.0, 10_744.0
    eng.step()
    assert eng.state.cash_init and len(eng.state.cash_tickets) == 2 and eng.entry_block() == ""


def test_priming_after_10_minutes_anyway_with_a_warning(monkeypatch, tmp_path, clock):
    eng = make(monkeypatch, tmp_path, history=False, balance=11_000.0)
    for k in range(25):  # the history keeps changing (a new cash deal every 30 s) and never adds up
        deal(1.0, f"bonus {k}", type_=6)
        eng.step()
        advance(clock, 30)
    assert eng.state.cash_init and sent(eng, "not confirmed complete within 10 min")


# ------------------------------------------------------------------ isolation
def test_failing_deal_history_never_stops_time_stops(monkeypatch, tmp_path, clock):
    """history_deals_get fails: new entries wait, but the time stop of an open NR position still closes it (the
    whole loop used to fail until the exit-3 restart)."""
    eng = make(monkeypatch, tmp_path)
    eng.step()
    S.positions.append(dict(ticket=555, identifier=555, symbol="EURJPY", magic=7101, type=0, volume=1.0,
                            price_open=130.0, time=S.now - 4 * 3600, sl=129.5, tp=130.5))  # 3 h time stop due
    real = fake_mt5.history_deals_get
    monkeypatch.setattr(fake_mt5, "history_deals_get", lambda *a, **k: None)
    monkeypatch.setattr(fake_mt5, "last_error", lambda: (-10005, "IPC timeout"))
    book(-1)  # forces a history read
    advance(clock)
    eng.step()
    assert S.positions == [] and eng.entry_block().startswith("deal history cannot be read")
    assert sent(eng, "cash-flow check failed")
    monkeypatch.setattr(fake_mt5, "history_deals_get", real)
    monkeypatch.setattr(fake_mt5, "last_error", lambda: (1, "Success"))
    advance(clock)
    eng.step()
    assert eng.entry_block() == ""
