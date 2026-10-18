"""Round-2 live engine rules (decisions D8-D14 and the notification fixes py-clock-ops#3/#5), on the in-memory
FakeBroker: TP inside the stops level, history gaps only logged, Monday-Friday market days, deferred entry signals,
closed bars by time, Algo Trading off, reset-halt without --rebase-peak, recovery notices."""
import logging
from datetime import datetime, timedelta

import pandas as pd
import pytest

import greatest_trader.engine as engine_mod
import greatest_trader.notify as notify_mod
from fakes import NY7, FakeBroker, ny7_utc, synthetic_m15
from greatest_trader.broker.base import Health, OrderResult, Tick
from greatest_trader.engine import LiveEngine
from greatest_trader.notify import Notifier
from greatest_trader.risk import RiskState
from greatest_trader.strategies import PositionView
from test_live_ops import cfg_for, eurjpy, make, sent


# ------------------------------------------------------------------ D8 / py-cashflow#4
def halted_state(path, request, month_halted=False):
    RiskState(peak_equity=10_000, last_equity=7_900, day="2021-03-10", month="2021-03", day_start_equity=7_900,
              month_start_equity=7_900 if not month_halted else 8_500, month_halted=month_halted, halted=True,
              halt_reason="max drawdown 21.0% reached on 2021-03-09", account_login=111, cash_init=True,
              reset_request=request).save(path / "state.json")


@pytest.mark.parametrize("equity,request_,resumed", [(7_900, "clear", False), (8_100, "clear", True),
                                                     (7_900, "rebase", True)])
def test_reset_halt_without_rebase_is_halted_again_and_says_so(tmp_path, equity, request_, resumed):
    """7_reset_halt.bat used to store a plain 'clear': with the drawdown still >= 20% the engine halted again in the
    same loop. The engine now says so and names the rebase; a clear after the drawdown recovered below the level,
    and the rebase, resume trading."""
    halted_state(tmp_path, request_)
    eng, br = make(cfg_for(tmp_path, preset="stable5"), eurjpy(start=datetime(2021, 3, 8)), equity=equity)
    br.set_time(datetime(2021, 3, 10, 10, 0, 5))
    eng.start()
    eng.step()
    st = eng.state
    assert st.halted is not resumed and st.reset_request == ""
    assert sent(eng, "RESET HALT (python main.py reset-halt" + (" --rebase-peak" if request_ == "rebase" else ""))
    noted = sent(eng, "RESET HALT had no effect: the drawdown 21.0%")
    assert bool(noted) is not resumed
    if noted:
        assert "7_reset_halt.bat (python main.py reset-halt --rebase-peak)" in noted[0]


def test_halt_hints_name_the_rebase(tmp_path):
    RiskState(peak_equity=10_000, day="2021-03-10", month="2021-03", account_login=111, cash_init=True,
              halted=True, halt_reason="max drawdown 21.0% reached on 2021-03-09").save(tmp_path / "state.json")
    eng, br = make(cfg_for(tmp_path, preset="stable5"), eurjpy(start=datetime(2021, 3, 8)), equity=7_900)
    br.set_time(datetime(2021, 3, 10, 23, 59, 55))
    eng.start()
    assert sent(eng, "HALTED: max drawdown 21.0% reached on 2021-03-09 (no new entries; to resume: stop the engine, "
                     "run 7_reset_halt.bat (python main.py reset-halt --rebase-peak), start it)")
    eng.step()
    br.set_time(datetime(2021, 3, 11, 0, 0, 5))
    eng.step()
    assert sent(eng, "-> 7_reset_halt.bat (python main.py reset-halt --rebase-peak)")  # daily summary
    eng2, br2 = make(cfg_for(tmp_path / "b", preset="stable5"), eurjpy(start=datetime(2021, 3, 8)), equity=10_000)
    br2.set_time(datetime(2021, 3, 10, 10, 0, 5))
    eng2.step()
    br2.equity = 7_900
    br2.set_time(datetime(2021, 3, 10, 10, 0, 10))
    eng2.step()
    assert sent(eng2, "HALT: max drawdown 21.0% reached on 2021-03-10 -> no new entries; to resume: stop the engine, "
                      "run 7_reset_halt.bat")


def test_reset_does_not_clear_the_monthly_stop(tmp_path):
    halted_state(tmp_path, "rebase", month_halted=True)
    eng, br = make(cfg_for(tmp_path, preset="stable5"), eurjpy(start=datetime(2021, 3, 8)), equity=7_900)
    br.set_time(datetime(2021, 3, 10, 10, 0, 5))
    eng.start()
    eng.step()
    assert not eng.state.halted and eng.state.month_halted
    assert sent(eng, "the monthly hard stop stays in force until the month ends")

STOPS_LEVEL = 10  # points


class StopsBroker(FakeBroker):
    """FakeBroker with a fixed quote and a server that rejects a TP inside the stops level (retcode 10016)."""
    quote: Tick | None = None

    def symbol_spec(self, base):
        s = super().symbol_spec(base)
        s.stops_level_points = STOPS_LEVEL
        return s

    def tick(self, base):
        return Tick(self.now, self.quote.bid, self.quote.ask) if self.quote else super().tick(base)

    def modify_position(self, ticket, base, sl, tp):
        spec = self.symbol_spec(base)
        p = [x for x in self.positions_ if x.ticket == ticket][0]
        px = self.quote.bid if p.direction > 0 else self.quote.ask
        if tp and abs(tp - px) < STOPS_LEVEL * spec.point:
            self.log.append(("modify-rejected", base, tp))
            return OrderResult(False, message="retcode=10016 Invalid stops")
        return super().modify_position(ticket, base, sl, tp)


# ------------------------------------------------------------------ D13 / parity#2
@pytest.mark.parametrize("points_short,closed", [(5, True), (11, True), (13, False)])
def test_new_tp_inside_the_stops_level_closes_the_position(tmp_path, points_short, closed):
    """The new moving average is a few points above the bid of an NR BUY: inside (stops level + 2) points the
    position is closed at market 'price at moving average' (EA NRManage) instead of keeping the old TP after a
    rejected modify; further away the TP is moved as usual."""
    cfg = cfg_for(tmp_path, syms=("GBPCAD",))
    data = {"GBPCAD": synthetic_m15(datetime(2021, 3, 1), 5, 1.75, 0.0001)}
    br = StopsBroker(data, equity=100_000)
    eng = LiveEngine(cfg, br, Notifier(cfg.notify), utcnow=ny7_utc(br))
    t0 = datetime(2021, 3, 3, 0, 30, 5)
    br.set_time(t0)
    mid = float(eng.nr.prepare(br.rates("GBPCAD", "M15", 300)).iloc[-1]["mid"])
    spec = br.symbol_spec("GBPCAD")
    op = round(mid - 0.0030, 5)
    br.positions_.append(PositionView(501, "NR", "GBPCAD", 1, 1.0, op, datetime(2021, 3, 3, 0, 15, 2),
                                      round(op - 0.0060, 5), round(mid + 0.0020, 5)))
    bid = mid - points_short * spec.point
    br.quote = Tick(t0, bid, bid + 0.00012)
    eng.step()
    if closed:
        assert br.positions_ == [] and ("close", "GBPCAD", "GT price at moving average") in br.log
        assert not [x for x in br.log if x[0] == "modify-rejected"]
    else:
        assert len(br.positions_) == 1 and br.positions_[0].tp == pytest.approx(mid)


# ------------------------------------------------------------------ D12 / parity#4
def test_history_gap_never_stops_the_tp_management(tmp_path, caplog):
    """One weekday M15 bar is missing in the lookback: the TP of the open NR position still follows the new
    moving average (it used to be frozen for up to 4 hours while the gap was in the 16-bar window)."""
    df = synthetic_m15(datetime(2021, 3, 1), 5, 1.75, 0.0001)
    df = df.drop(pd.Timestamp(datetime(2021, 3, 2, 23, 0)))  # Tue 23:00 market time
    eng, br = make(cfg_for(tmp_path, syms=("GBPCAD",), preset="stable5"), {"GBPCAD": df}, equity=100_000)
    t = datetime(2021, 3, 3, 0, 15, 5)
    br.set_time(t)
    mid = float(eng.nr.prepare(br.rates("GBPCAD", "M15", 300)).iloc[-1]["mid"])
    op = mid - 0.0040
    br.positions_.append(PositionView(501, "NR", "GBPCAD", 1, 1.0, op, datetime(2021, 3, 3, 0, 0, 2), op - 0.006,
                                      mid + 0.003))
    for _ in range(3):
        eng.step()
        t += timedelta(seconds=5)
        br.set_time(t)
    assert ("modify", "GBPCAD", pytest.approx(mid)) in br.log
    assert eng.last_bar["GBPCAD"] == datetime(2021, 3, 3, 0, 0)
    assert sum("history gap" in r.message for r in caplog.records) == 1  # logged once for the bar


# ------------------------------------------------------------------ D10 / py-clock-ops#4
def test_no_sunday_market_day_while_a_clock_change_is_unconfirmed(tmp_path):
    """EET broker in auto mode (verified ny7 = +3 in the summer). EU DST ends Sun 2026-10-25: at the FX open
    (Sun 21:00:05 UTC) the server is +2, the old clock still maps it to market Sunday 23:00. No Sunday market day,
    DAILY summary or equity_log row may be booked; the day rolls from Friday straight to Monday."""
    cfg = cfg_for(tmp_path, preset="stable5", mode="auto")
    br = FakeBroker({"EURJPY": synthetic_m15(datetime(2026, 10, 5), 25, 160.0, 0.01)})
    wall = {}
    eng = LiveEngine(cfg, br, Notifier(cfg.notify), utcnow=lambda: wall["utc"])

    def loop(server, off):
        br.set_time(server)
        wall["utc"] = server - timedelta(hours=off)
        eng.step()

    t = datetime(2026, 10, 23, 23, 58, 0)  # Friday, server +3 (EEST) = NY+7
    while t <= datetime(2026, 10, 23, 23, 59, 50):
        loop(t, 3)
        t += timedelta(seconds=5)
    assert eng.clock_verified and eng.state.day == "2026-10-23"
    t = datetime(2026, 10, 25, 23, 0, 5)  # Sunday 21:00:05 UTC; EET is +2 now
    for k in range(5):
        loop(t + timedelta(seconds=5 * k), 2)
        assert eng.state.day in ("2026-10-23", "2026-10-26")
    assert eng.state.day == "2026-10-26" and sent(eng, "server clock CHANGED")
    assert not sent(eng, "2026-10-25") and len(sent(eng, "DAILY 2026-10-23 Fri")) == 1
    log = pd.read_csv(tmp_path / "journal" / "equity_log.csv")
    assert list(log.market_date) == ["2026-10-23"] and len(eng.state.daily_equity) == 1


def test_first_run_on_a_weekend_books_the_friday(tmp_path):
    eng, br = make(cfg_for(tmp_path), eurjpy())
    br.set_time(datetime(2021, 3, 6, 10, 0, 5))  # a Saturday tick (ny7: market time)
    eng.step()
    assert eng.state.day == "2021-03-05"


# ------------------------------------------------------------------ D14 / parity#5
ALGO_OFF = Health("Algo Trading is switched off in the MT5 terminal", orders_only=True)


def test_algo_trading_off_still_latches_the_monthly_stop(tmp_path):
    """Algo Trading is off while a position is open: equity dips to -3.5% for the month and recovers to -2.5% before
    it is switched back on. The guards keep running (EA: same rule), so MONTH STOP is latched while it is off - no
    order is sent then - and the position is closed as soon as trading is possible again."""
    eng, br = make(cfg_for(tmp_path, preset="stable5"), eurjpy(), equity=100_000)
    t = datetime(2021, 3, 3, 10, 0, 5)
    br.set_time(t)
    eng.step()
    br.open_position("EURJPY", 1, 1.0, 129.0, None, 7101, "GT_NR")
    br.health_reason = ALGO_OFF
    for eq in (98_000, 96_500, 97_500):
        t += timedelta(seconds=5)
        br.set_time(t)
        br.equity = eq
        eng.step()
    st = eng.state
    assert st.month_halted and sent(eng, "MONTH STOP") and len(br.positions_) == 1  # latched, nothing sent
    assert not [x for x in br.log if x[0] == "close"] and sent(eng, "trading paused: Algo Trading is switched off")
    assert eng.entry_block().startswith("trading paused") and st.peak_equity == 100_000 and st.last_equity == 97_500
    br.health_reason = ""
    t += timedelta(seconds=5)
    br.set_time(t)
    eng.step()
    assert br.positions_ == [] and ("close", "EURJPY", "GT monthly hard stop 3.0%") in br.log
    assert sent(eng, "trading resumed (Algo Trading is switched off in the MT5 terminal: cleared)")
    assert eng.entry_block() == ""


def test_terminal_not_connected_still_pauses_everything(tmp_path):
    eng, br = make(cfg_for(tmp_path, preset="stable5"), eurjpy(), equity=100_000)
    t = datetime(2021, 3, 3, 10, 0, 5)
    br.set_time(t)
    eng.step()
    br.health_reason = "MT5 terminal is not connected to the trade server"  # a plain reason: account data unsure
    br.equity = 96_500
    br.set_time(t + timedelta(seconds=5))
    eng.step()
    assert not eng.state.month_halted and eng.state.last_equity == 100_000


# ------------------------------------------------------------------ py-clock-ops#3
def test_recovery_notice_of_a_second_error_is_not_swallowed(tmp_path, monkeypatch):
    """ERROR at t, 'recovered' at t+300; a second ERROR 1800 s after the first one, healed one loop later: its
    'recovered' was dropped by a 30-minute rate limit, leaving ERROR as the operator's last message."""
    clock = {"t": 1000.0}

    class FakeTime:
        monotonic = staticmethod(lambda: clock["t"])

        @staticmethod
        def sleep(s):
            clock["t"] += s

    monkeypatch.setattr(engine_mod, "time", FakeTime)
    monkeypatch.setattr(notify_mod, "time", FakeTime)
    eng, br = make(cfg_for(tmp_path), {"EURJPY": synthetic_m15(datetime(2025, 1, 13), 10, 160.0, 0.01)})
    br.set_time(datetime(2025, 1, 21, 3, 0, 5))
    t0 = 1060.0
    fail = [(t0, t0 + 300), (t0 + 1800, t0 + 1805)]
    orig = br.health

    def health():
        now = clock["t"]
        if now >= t0 + 2400:
            raise KeyboardInterrupt
        br.set_time(br.now + timedelta(seconds=5))
        if any(a <= now < b for a, b in fail):
            raise RuntimeError("MT5 terminal unreachable (No IPC connection), reconnecting")
        return orig()

    br.health = health
    assert eng.run_forever() == 10
    msgs = [m for m in eng.notifier.recent if m.startswith("ERROR") or m.startswith("recovered")]
    assert [m.split(":")[0].split(" after")[0] for m in msgs] == ["ERROR", "recovered", "ERROR", "recovered"]


# ------------------------------------------------------------------ py-clock-ops#5
def test_stale_feed_and_paused_trading_get_recovery_notices(tmp_path, monkeypatch):
    """A frozen feed and Algo Trading switched off are notified, so is their end ('prices are updating again',
    'trading resumed'), and a second incident inside the old rate-limit window is notified again at once (it was
    silent for up to an hour)."""
    clk = {"t": 0.0}

    class T:
        monotonic = staticmethod(lambda: clk["t"])
        sleep = staticmethod(lambda s: None)

    monkeypatch.setattr(notify_mod, "time", T)
    cfg = cfg_for(tmp_path)
    br = FakeBroker({"EURJPY": synthetic_m15(datetime(2025, 1, 13), 10, 160.0, 0.01)})
    wall = {}
    eng = LiveEngine(cfg, br, Notifier(cfg.notify), utcnow=lambda: wall["utc"])
    s = {"srv": datetime(2025, 1, 21, 10, 0, 0)}
    s["utc"] = NY7.server_to_utc(s["srv"])
    log = []

    def run(seconds, ticking, health=""):
        br.health_reason = health
        for _ in range(0, seconds, 5):
            s["utc"] += timedelta(seconds=5)
            clk["t"] += 5
            if ticking:
                s["srv"] = NY7.utc_to_server(s["utc"])
                br.set_time(s["srv"])
            wall["utc"] = s["utc"]
            n = len(eng.notifier.recent)
            eng.step()
            log.extend(list(eng.notifier.recent)[n:])

    def kinds():
        out = []
        for m in log:
            for k in ("no price updates", "prices are updating again", "trading paused", "trading resumed"):
                if m.startswith(k):
                    out.append(k)
        return out

    run(60, True)
    run(12 * 60, False)  # freeze 1
    run(8 * 60, True)  # prices back
    run(35 * 60, False)  # freeze 2, starts 20 min after freeze 1 (inside the old 1-hour rate limit)
    run(10 * 60, True)
    assert kinds() == ["no price updates", "prices are updating again", "no price updates",
                       "prices are updating again"]
    log.clear()
    run(2 * 60, True, ALGO_OFF)
    run(10 * 60, True)
    run(15 * 60, True, ALGO_OFF)  # second episode 12 min after the first (inside the old 30-min rate limit)
    run(60, True)
    assert kinds() == ["trading paused", "trading resumed", "trading paused", "trading resumed"]


# ------------------------------------------------------------------ D9
SPIKE = datetime(2021, 3, 3, 0, 30)  # NR signal bar (Wednesday, market time), closes at 00:45


def gated_run(tmp_path, closed_until, syms=("EURJPY",), data=None, max_total_risk=None):
    """Loops every 5 s from 00:40:03; the deal history cannot be read (an entry gate) from 00:44 until
    `closed_until`."""
    cfg = cfg_for(tmp_path, syms=syms)
    if max_total_risk is not None:
        cfg.risk.max_total_risk = max_total_risk
    eng, br = make(cfg, data or eurjpy(spike_at=SPIKE), equity=1_000_000)
    flows = br.cash_flows

    def cash_flows():
        if datetime(2021, 3, 3, 0, 44) <= br.now < closed_until:
            raise RuntimeError("history_deals_get failed: (-10005, 'IPC timeout')")
        return flows()

    br.cash_flows = cash_flows
    opened, real_open = [], br.open_position

    def open_position(base, *a):
        opened.append((br.now, base))
        return real_open(base, *a)

    br.open_position = open_position
    t = datetime(2021, 3, 2, 22, 0, 3)
    while t <= datetime(2021, 3, 3, 0, 52, 3):
        br.set_time(t)
        eng.step()
        t += timedelta(seconds=5) if t >= datetime(2021, 3, 3, 0, 40) else timedelta(minutes=5)
    return eng, [o for o in opened if o[0] >= datetime(2021, 3, 3, 0, 44)]  # entries of the 00:30 bar


def test_signal_blocked_by_an_entry_gate_is_taken_when_the_gate_opens_within_150_s(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="greatest_trader.engine")
    eng, opened = gated_run(tmp_path, closed_until=datetime(2021, 3, 3, 0, 46, 30))
    assert opened == [(datetime(2021, 3, 3, 0, 46, 33), "EURJPY")]  # the first loop with the gate open
    msgs = [r.message for r in caplog.records]
    assert any("signal of the 2021-03-03 00:30:00 bar deferred: deal history cannot be read" in m for m in msgs)
    assert any("deferred signal of the 2021-03-03 00:30:00 bar: entry gate open again" in m for m in msgs)
    # the control: no gate -> taken in the first loop after the bar close (+2 s settle)
    eng2, opened2 = gated_run(tmp_path / "b", closed_until=datetime(2021, 1, 1))
    assert opened2 == [(datetime(2021, 3, 3, 0, 45, 3), "EURJPY")]


def test_deferred_signal_is_dropped_after_150_s(tmp_path, caplog):
    eng, opened = gated_run(tmp_path, closed_until=datetime(2021, 3, 3, 0, 47, 35))  # bar close + 155 s
    assert opened == [] and eng._deferred == {}
    assert any("signal of the 2021-03-03 00:30:00 bar dropped: not taken within 150 s" in r.message
               for r in caplog.records)


def test_deferred_signals_are_retried_alphabetically(tmp_path):
    """Both pairs signal on the blocked bar; the total-risk cap admits one: at the retry the alphabetically first
    pair gets it, as in the backtester."""
    data = {"GBPJPY": synthetic_m15(datetime(2021, 3, 1), 5, 150.0, 0.01, spike_at=SPIKE),
            "EURJPY": synthetic_m15(datetime(2021, 3, 1), 5, 130.0, 0.01, spike_at=SPIKE)}
    until = datetime(2021, 3, 3, 0, 46, 30)
    t = datetime(2021, 3, 3, 0, 46, 33)
    eng, opened = gated_run(tmp_path, until, syms=("GBPJPY", "EURJPY"), data=data)
    assert opened == [(t, "EURJPY"), (t, "GBPJPY")]  # without a cap both are taken, alphabetically
    one = eng.risk.risk_fraction("NR", 1_000_000) * 1.5  # room for one trade only
    eng2, opened2 = gated_run(tmp_path / "b", until, syms=("GBPJPY", "EURJPY"), data=data, max_total_risk=one)
    assert opened2 == [(t, "EURJPY")]


# ------------------------------------------------------------------ ES decision waits for the H1 bar to close (D11)
def test_es_decision_waits_until_the_last_h1_bar_counts_as_closed(tmp_path):
    """The previous H1 bar counts as closed only BAR_SETTLE (2 s) after the hour, so the ES decision at 07:00 UTC
    waits until then (EA: same rule); otherwise its ATR would end one hour earlier than in the backtester."""
    cfg = cfg_for(tmp_path, syms=["EURJPY"], es=True)
    data = {"EURJPY": synthetic_m15(datetime(2021, 3, 1), 10, 130.0, 0.01),
            "EURUSD": synthetic_m15(datetime(2021, 3, 1), 10, 1.20, 0.0001, seed=3)}
    eng, br = make(cfg, data)
    br.set_time(datetime(2021, 3, 9, 8, 59, 55))  # NY7 server time = UTC+2 in March before the US DST switch
    eng.start()
    eng.step()
    br.set_time(datetime(2021, 3, 9, 9, 0, 1))  # 07:00:01 UTC: inside the ES window, H1 bar not settled yet
    eng.step()
    assert eng.state.last_es_entry_day != "2021-03-09"
    br.set_time(datetime(2021, 3, 9, 9, 0, 6))
    eng.step()
    assert eng.state.last_es_entry_day == "2021-03-09"
