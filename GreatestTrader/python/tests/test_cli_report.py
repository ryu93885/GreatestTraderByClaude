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


def test_reset_halt_stores_a_request_and_keeps_the_state(tmp_path):
    """reset-halt only stores the request: the engine applies it at its next start after the cash-flow check
    (test_live_ops: test_reset_halt_is_applied_after_the_first_cash_flow_check)."""
    cfg = write_cfg(tmp_path)
    st = RiskState(peak_equity=10_000, last_equity=7_900, halted=True, halt_reason="max drawdown 21.0% reached",
                   account_login=5, cash_init=True, cash_tickets=[[9, 0.0]], clock_mode="ny7")
    st.save(tmp_path / "state" / "state.json")
    assert main.main(["reset-halt", "-c", str(cfg), "--rebase-peak"]) == 0
    st2 = RiskState.load(tmp_path / "state" / "state.json")
    assert st2.reset_request == "rebase" and st2.halted and st2.peak_equity == 10_000
    assert (st2.account_login, st2.cash_tickets, st2.clock_mode) == (5, [[9, 0.0]], "ny7")
    j = pd.read_csv(tmp_path / "journal" / f"trades_{datetime.now(timezone.utc):%Y}.csv")
    assert j.event.tolist() == ["reset_halt"] and "rebase the peak" in j.reason.iloc[0]
    assert list((tmp_path / "logs").glob("reset-halt_*.log"))
    assert main.main(["reset-halt", "-c", str(cfg)]) == 0
    assert RiskState.load(tmp_path / "state" / "state.json").reset_request == "clear"


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
    cfg = backtest_setup(tmp_path, (("GBPCHF", 1.25, 1), ("USDCHF", 0.92, 2), ("AUDNZD", 1.08, 3)), ["GBPCHF"])
    assert main.main(["backtest", "-c", str(cfg)]) == 0
    out = capsys.readouterr().out
    assert "conversion pairs (not traded): USDCHF" in out and "AUDNZD" not in out
    t = pd.read_csv(tmp_path / "out" / "trades.csv")
    assert len(t) and (abs(t.lots - 20) < 1e-9).all()


def backtest_setup(tmp_path, pairs, traded, equity=1_000_000_000):
    d = tmp_path / "data"
    d.mkdir()
    for s, b, seed in pairs:
        synthetic_m15(datetime(2021, 3, 1), 10, b, 0.0001, seed=seed, spike_at=datetime(2021, 3, 3, 0, 30)) \
            .rename_axis("time").to_csv(d / f"{s}_M15.csv")
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"night_reversion:\n  symbols: [{', '.join(traded)}]\neuro_session:\n  enabled: false\nbroker:\n"
                   f"  server_time_mode: ny7\nrisk:\n  blackout_start: ''\n  blackout_end: ''\n"
                   f"engine:\n  log_dir: {tmp_path / 'logs'}\n  state_file: {tmp_path / 'state.json'}\n"
                   f"backtest:\n  data_dir: {d}\n  output_dir: {tmp_path / 'out'}\n  initial_equity: {equity}\n",
                   encoding="utf-8")
    return cfg


def test_backtest_output_survives_a_non_utf8_redirected_stdout(tmp_path):
    """Redirected output on an English Windows (cp1252) crashed on the Japanese tables before the report was
    written. stdout is switched to UTF-8 (errors replaced)."""
    import os
    import subprocess
    import sys
    cfg = backtest_setup(tmp_path, (("GBPCHF", 1.25, 1), ("USDCHF", 0.92, 2)), ["GBPCHF"], equity=10_000)
    env = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    r = subprocess.run([sys.executable, str(Path(main.__file__)), "backtest", "-c", str(cfg)], env=env,
                       capture_output=True, timeout=300)
    assert r.returncode == 0, r.stdout.decode("utf-8", "replace")[-2000:] + r.stderr.decode("utf-8", "replace")
    assert "月利".encode("utf-8") in r.stdout and (tmp_path / "out" / "summary.txt").exists()


# ------------------------------------------------------------------ exit codes (3_run_live.bat stops on 10)
@pytest.mark.parametrize("body, text", [
    ("risk:\n  max_lot_per_order: 20\n", "unknown config key: risk.max_lot_per_order"),
    ("risk:\n\tmax_lots_per_order: 20\n", "not valid YAML"),
    ("preset: classic_standard\nrisk:\n  risk_nr: 0.005\n", "only apply with risk.profile: custom"),
    ("risk:\n  max_drawdown_halt: 2\n", "max_drawdown_halt"),
    ("preset: nosuchpreset\n", "unknown preset"),
])
def test_config_errors_stop_with_exit_code_10(tmp_path, capsys, body, text):
    p = tmp_path / "config.yaml"
    p.write_text(body, encoding="utf-8")
    assert main.main(["live", "-c", str(p), "--live-orders"]) == 10
    out = capsys.readouterr().out
    assert "CONFIG ERROR" in out and text in out
    assert main.main(["live", "-c", str(tmp_path / "missing.yaml")]) == 10


def test_missing_metatrader5_package_stops_with_exit_code_10(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(mt5_broker, "mt5", None)
    assert main.main(["live", "-c", str(write_cfg(tmp_path)), "--live-orders"]) == 10
    out = capsys.readouterr().out
    assert "MetaTrader5 package is not installed" in out and "setup_windows.bat" in out


def test_help_lists_the_exit_codes(capsys):
    with pytest.raises(SystemExit):
        main.main(["--help"])
    out = capsys.readouterr().out
    assert "exit codes:" in out and "10  stopped on purpose" in out and "0 normal" not in out


# ------------------------------------------------------------------ report --from, download
def report_months(tmp_path, monkeypatch, frm, deals=None):
    fake_mt5.reset()
    monkeypatch.setattr(mt5_broker, "mt5", fake_mt5)
    S.deals = [deal(*d) for d in (DEALS_12M if deals is None else deals)]
    tmp_path.mkdir(parents=True, exist_ok=True)
    cfg = write_cfg(tmp_path)
    assert main.main(["report", "-c", str(cfg), "--from", frm]) == 0
    m = pd.read_csv(tmp_path / "out" / "live_report" / "monthly_returns.csv", index_col=0)
    by = pd.read_csv(tmp_path / "out" / "live_report" / "pnl_by_strategy.csv")
    return list(m.index), float(by.pnl.sum()) if len(by) else 0.0


DEALS_12M = [("2025-11-02", 2, 10_000.0, 0, 0)] + [(f"2026-{m:02d}-10", 1, 100.0, 7101, 1) for m in range(1, 13)]


def test_report_from_is_parsed_as_a_month(tmp_path, monkeypatch):
    full = [f"2026-{m:02d}" for m in range(1, 13)]
    assert report_months(tmp_path / "a", monkeypatch, "2026-01") == (full, 1200.0)
    assert report_months(tmp_path / "b", monkeypatch, "2026-1") == (full, 1200.0)  # was: only Oct-Dec in the table
    months, pnl = report_months(tmp_path / "c", monkeypatch, "2026-02-15")  # the month of that day
    assert months == full[1:] and pnl == 1100.0  # table and P/L by strategy cover the same months
    with pytest.raises(SystemExit):
        main.main(["report", "-c", str(write_cfg(tmp_path)), "--from", "Feb"])


def test_report_with_an_empty_history(tmp_path, monkeypatch):
    assert report_months(tmp_path, monkeypatch, "2026-01", deals=[]) == ([], 0.0)  # was: TypeError
    assert "no deals in the history" in (tmp_path / "out" / "live_report" / "summary.txt").read_text(encoding="utf-8")


def test_history_none_with_success_is_an_empty_history(tmp_path, monkeypatch):
    fake_mt5.reset()
    monkeypatch.setattr(mt5_broker, "mt5", fake_mt5)
    monkeypatch.setattr(fake_mt5, "history_deals_get", lambda *a, **k: None)  # last_error() = (1, 'Success')
    b = mt5_broker.MT5Broker(Config())
    b.connect()
    assert len(b.history_deals(datetime(2000, 1, 1), datetime(2030, 1, 1))) == 0 and b.cash_flows() == []
    monkeypatch.setattr(fake_mt5, "last_error", lambda: (-10005, "IPC timeout"))
    with pytest.raises(RuntimeError, match="history_deals_get failed"):
        b.history_deals(datetime(2000, 1, 1), datetime(2030, 1, 1))


def test_download_continues_after_a_missing_pair(tmp_path, monkeypatch, capsys):
    import numpy as np
    fake_mt5.reset()
    monkeypatch.setattr(mt5_broker, "mt5", fake_mt5)
    for s in ("GBPJPY", "EURUSD"):  # EURJPY (first in the list) is not offered by this broker
        S.specs[s] = dict(digits=3 if s.endswith("JPY") else 5, point=0.001 if s.endswith("JPY") else 0.00001,
                          volume_min=0.01, volume_max=100, volume_step=0.01, trade_stops_level=0)
    dt = np.dtype([("time", "<i8"), ("open", "<f8"), ("high", "<f8"), ("low", "<f8"), ("close", "<f8"),
                   ("tick_volume", "<u8"), ("spread", "<i4"), ("real_volume", "<u8")])

    def copy_rates_range(name, tf, t0, t1):
        a = np.zeros(4, dtype=dt)
        a["time"] = int(t0.timestamp()) // 900 * 900 + np.arange(4) * 900
        a["open"] = a["high"] = a["low"] = a["close"] = 1.0
        a["spread"] = 10
        return a

    monkeypatch.setattr(fake_mt5, "copy_rates_range", copy_rates_range, raising=False)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"night_reversion:\n  symbols: [EURJPY, GBPJPY]\nengine:\n  log_dir: {tmp_path / 'logs'}\n"
                   f"  state_file: {tmp_path / 'state.json'}\nbacktest:\n  data_dir: {tmp_path / 'data'}\n",
                   encoding="utf-8")
    assert main.main(["download", "-c", str(cfg), "--years", "0.01"]) == 0
    assert sorted(p.name for p in (tmp_path / "data").glob("*.csv")) == ["EURUSD_M15.csv", "GBPJPY_M15.csv"]
    out = capsys.readouterr().out
    assert "NOT downloaded (1)" in out and "EURJPY" in out
