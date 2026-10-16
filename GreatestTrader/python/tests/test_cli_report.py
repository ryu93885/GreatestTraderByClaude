"""Command line: reset-halt, report (with the fake MetaTrader5 module), dry-run paths; live report maths."""
import calendar
import logging
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

import fake_mt5
import main
from fake_mt5 import S
from fakes import synthetic_m15
from greatest_trader.broker import mt5_broker
from greatest_trader.config import Config
from greatest_trader.journal import TradeJournal
from greatest_trader.live_report import BACKTEST_12M_P5, add_rolling, edge_monitor_table, monthly_returns_from_deals
from greatest_trader.ops import acquire_single_instance_lock
from greatest_trader.risk import RiskState


@pytest.fixture(autouse=True)
def restore_logging():
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    yield
    for h in root.handlers:
        if h not in handlers:
            h.close()
    root.handlers[:] = handlers
    root.setLevel(level)


def write_cfg(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(f"preset: stable5\nengine:\n  state_file: {tmp_path / 'state' / 'state.json'}\n"
                 f"  log_dir: {tmp_path / 'logs'}\n  journal_dir: {tmp_path / 'journal'}\n"
                 f"backtest:\n  output_dir: {tmp_path / 'out'}\n", encoding="utf-8")
    return p


def test_reset_halt_clears_the_halt_and_keeps_the_state(tmp_path):
    cfg = write_cfg(tmp_path)
    st = RiskState(peak_equity=10_000, last_equity=7_900, halted=True, halt_reason="max drawdown 21.0% reached",
                   account_login=5, cash_init=True, cash_tickets=[[9, 0.0]], clock_mode="ny7")
    st.save(tmp_path / "state" / "state.json")
    assert main.main(["reset-halt", "-c", str(cfg), "--rebase-peak"]) == 0  # MT5 absent: last equity is used
    st2 = RiskState.load(tmp_path / "state" / "state.json")
    assert not st2.halted and st2.peak_equity == 7_900
    assert (st2.account_login, st2.cash_tickets, st2.clock_mode) == (5, [[9, 0.0]], "ny7")
    j = pd.read_csv(tmp_path / "journal" / f"trades_{datetime.now(timezone.utc):%Y}.csv")
    assert j.event.tolist() == ["reset_halt"] and "peak equity rebased" in j.reason.iloc[0]
    assert list((tmp_path / "logs").glob("reset-halt_*.log"))


def test_reset_halt_refuses_while_the_engine_runs(tmp_path):
    cfg = write_cfg(tmp_path)
    RiskState(halted=True, halt_reason="x").save(tmp_path / "state" / "state.json")
    lock = acquire_single_instance_lock(tmp_path / "state" / "state.json")
    try:
        assert main.main(["reset-halt", "-c", str(cfg)]) == 1
    finally:
        lock.close()
    assert RiskState.load(tmp_path / "state" / "state.json").halted


def live_with_fake_mt5(tmp_path, monkeypatch, loops=3, args=("--dry-run",), netting=False):
    fake_mt5.reset()
    S.margin_mode = 0 if netting else 2
    monkeypatch.setattr(mt5_broker, "mt5", fake_mt5)
    n = [0]

    def sleep(s):
        n[0] += 1
        if n[0] >= loops:
            raise KeyboardInterrupt

    monkeypatch.setattr("greatest_trader.engine.time.sleep", sleep)
    return main.main(["live", "-c", str(write_cfg(tmp_path)), *args])


def test_live_command_dry_run_end_to_end(tmp_path, monkeypatch):
    assert live_with_fake_mt5(tmp_path, monkeypatch) == 10  # Ctrl+C: clean stop, the wrapper does not restart
    assert (tmp_path / "state" / "state_dryrun.json").exists() and not (tmp_path / "state" / "state.json").exists()
    assert list((tmp_path / "logs").glob("dryrun_*.log"))
    assert RiskState.load(tmp_path / "state" / "state_dryrun.json").account_login == S.login


def test_live_command_refuses_a_netting_account(tmp_path, monkeypatch, capsys):
    assert live_with_fake_mt5(tmp_path, monkeypatch, args=("--live-orders",), netting=True) == 10  # no restart
    assert "NETTING" in capsys.readouterr().out


def test_live_command_refuses_a_second_instance(tmp_path, monkeypatch, capsys):
    lock = acquire_single_instance_lock(tmp_path / "state" / "state.json")
    try:
        assert live_with_fake_mt5(tmp_path, monkeypatch, args=("--live-orders",)) == 10
        assert "already uses state.json" in capsys.readouterr().out
        assert S.init_count == 0  # never touched MT5
    finally:
        lock.close()


def test_dry_run_has_its_own_state_and_journal(tmp_path):
    cfg = Config()
    cfg.engine.state_file = str(tmp_path / "state" / "state.json")
    cfg.engine.journal_dir = str(tmp_path / "journal")
    for _ in range(2):  # idempotent
        main.dry_run_paths(cfg)
    assert Path(cfg.engine.state_file).name == "state_dryrun.json"
    assert Path(cfg.engine.journal_dir) == tmp_path / "journal" / "dryrun"


def deal(ts, type_, profit, magic=0, entry=0, ticket=[100]):
    ticket[0] += 1
    return dict(ticket=ticket[0], time=calendar.timegm(pd.Timestamp(ts).timetuple()), time_msc=0, type=type_,
                entry=entry, magic=magic, position_id=0, symbol="", volume=0.1, price=1.0, profit=profit,
                commission=0.0, swap=0.0, fee=0.0, reason=3, comment="")


DEALS = [("2026-01-02", 2, 10_000.0, 0, 0), ("2026-01-20", 1, 500.0, 7101, 1), ("2026-02-05", 0, 200.0, 7102, 1),
         ("2026-02-10", 2, -1_000.0, 0, 0), ("2026-02-20", 1, 300.0, 7101, 1), ("2026-04-03", 1, -97.0, 7101, 1)]


def test_report_command_from_mt5_history(tmp_path, monkeypatch):
    fake_mt5.reset()
    monkeypatch.setattr(mt5_broker, "mt5", fake_mt5)
    S.deals = [deal(*d) for d in DEALS]
    cfg = write_cfg(tmp_path)
    assert main.main(["report", "-c", str(cfg)]) == 0
    out = tmp_path / "out" / "live_report"
    m = pd.read_csv(out / "monthly_returns.csv", index_col=0)
    assert m.loc["2026-01", "return"] == pytest.approx(0.05)
    assert m.loc["2026-02", "return"] == pytest.approx(10_700 / 10_500 * 10_000 / 9_700 - 1)  # split at the flow
    assert (m.loc["2026-02", "pnl_NR"], m.loc["2026-02", "pnl_ES"], m.loc["2026-02", "cash_flow"]) == (300, 200, -1000)
    assert m.loc["2026-03", "return"] == 0 and m.loc["2026-03", "end_balance"] == 10_000  # month without deals
    assert m.loc["2026-04", "return"] == pytest.approx(-0.0097)
    assert m.loc["2026-04", "r3m"] == pytest.approx((1 + m.loc["2026-02", "return"]) * 0.9903 - 1)
    by = pd.read_csv(out / "pnl_by_strategy.csv")
    assert dict(zip(by.strategy, by.pnl)) == {"NR": 703.0, "ES": 200.0}
    assert "compound monthly return" in (out / "summary.txt").read_text(encoding="utf-8")


def test_rolling_returns_flag_windows_outside_the_backtest():
    months = [f"{y}-{mm:02d}" for y in (2026, 2027) for mm in range(1, 13)]
    m = pd.DataFrame({"return": [-0.002] * 12 + [-0.01] * 12}, index=months)
    r = add_rolling(m)
    assert r["r12m"].iloc[11] == pytest.approx(0.998 ** 12 - 1) and r["r12m"].iloc[11] < BACKTEST_12M_P5
    assert r["vs_backtest_12m"].iloc[11] == "below backtest 5th percentile"
    assert r["vs_backtest_12m"].iloc[23] == "WORSE THAN ANY BACKTEST 12m WINDOW"
    assert r["vs_backtest_12m"].iloc[5] == ""  # less than 12 months of history


def test_monthly_returns_from_deals_handles_empty_history():
    assert len(monthly_returns_from_deals(pd.DataFrame(), {7101: "NR"})) == 0


def test_edge_monitor_table(tmp_path):
    j = TradeJournal(tmp_path)
    for month, n, r in (("2021-01", 60, -0.1), ("2021-02", 10, -0.1), ("2021-03", 300, 0.2)):
        for k in range(n):
            j.trade({"event": "close", "time_utc": f"{month}-15 01:00:00", "R": r, "base_risk": 0.035,
                     "ticket": k})
    t = edge_monitor_table(j.closed_trades(), 250)
    assert t.status.tolist() == ["WARN", "REVIEW", "ok"] and t.trades.tolist() == [60, 70, 250]


def test_backtest_command_loads_conversion_pairs_from_the_data_dir(tmp_path, capsys):
    """A pair that is not traded but sits in data_dir converts the lots of a traded symbol (GBPCHF in a USD
    account needs USDCHF): the 20-lot cap binds."""
    d = tmp_path / "data"
    d.mkdir()
    for s, b, p, seed in (("GBPCHF", 1.25, 0.0001, 1), ("USDCHF", 0.92, 0.0001, 2), ("AUDNZD", 1.08, 0.0001, 3)):
        synthetic_m15(datetime(2021, 3, 1), 10, b, p, seed=seed, spike_at=datetime(2021, 3, 3, 0, 30)).rename_axis("time").to_csv(
            d / f"{s}_M15.csv")
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"night_reversion:\n  symbols: [GBPCHF]\neuro_session:\n  enabled: false\nbroker:\n"
                   f"  server_time_mode: ny7\nrisk:\n  blackout_start: ''\n  blackout_end: ''\n"
                   f"engine:\n  log_dir: {tmp_path / 'logs'}\n  state_file: {tmp_path / 'state.json'}\n"
                   f"backtest:\n  data_dir: {d}\n  output_dir: {tmp_path / 'out'}\n  initial_equity: 1000000000\n",
                   encoding="utf-8")
    assert main.main(["backtest", "-c", str(cfg)]) == 0
    out = capsys.readouterr().out
    assert "conversion pairs (not traded): USDCHF" in out and "AUDNZD" not in out
    t = pd.read_csv(next((tmp_path / "out").rglob("trades.csv")))
    assert len(t) and (abs(t.lots - 20) < 1e-9).all()
