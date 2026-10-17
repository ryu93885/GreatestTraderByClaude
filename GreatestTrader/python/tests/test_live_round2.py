"""Round-2 live engine rules (decisions D8-D14 and the notification fixes py-clock-ops#3/#5), on the in-memory
FakeBroker: TP inside the stops level, history gaps only logged, Monday-Friday market days, deferred entry signals,
closed bars by time, Algo Trading off, reset-halt without --rebase-peak, recovery notices."""
from datetime import datetime, timedelta

import pandas as pd
import pytest

from fakes import FakeBroker, ny7_utc, synthetic_m15
from greatest_trader.broker.base import OrderResult, Tick
from greatest_trader.engine import LiveEngine
from greatest_trader.notify import Notifier
from greatest_trader.strategies import PositionView
from test_live_ops import cfg_for, eurjpy, make, sent

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
