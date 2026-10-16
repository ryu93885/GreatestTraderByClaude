"""GreatestTrader command line.

  python main.py check     -c config/config.yaml   # connect to MT5, show account/symbols/spreads (no trading)
  python main.py live      -c config/config.yaml   # run the trading engine (dry_run setting in config!)
  python main.py download  -c config/config.yaml --years 5   # fetch M15 history from MT5 into backtest.data_dir
  python main.py backtest  -c config/config.yaml   # Python backtest on CSV data in backtest.data_dir
  python main.py report    -c config/config.yaml   # live monthly returns / P/L / R monitor from MT5 history
  python main.py reset-halt [--rebase-peak]        # clear a drawdown halt (stop the engine first)
  python main.py backtest --preset safe2           # any command: use a bundled risk preset instead of `preset:`

Exit codes of `live`: 0 normal, 3 = restart me (persistent failure), 10 = stopped / do not restart.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from greatest_trader import __version__  # noqa: E402
from greatest_trader.config import load_config  # noqa: E402
from greatest_trader.ops import EXIT_STOP, format_versions, setup_logging, versions  # noqa: E402

log = logging.getLogger("main")


def dry_run_paths(cfg) -> None:
    """Dry-run never shares state or journal with live trading: state/state_dryrun.json, journal/dryrun/."""
    p = Path(cfg.engine.state_file)
    if not p.stem.endswith("_dryrun"):
        cfg.engine.state_file = str(p.with_name(f"{p.stem}_dryrun{p.suffix}"))
    j = Path(cfg.engine.journal_dir)
    if j.name != "dryrun":
        cfg.engine.journal_dir = str(j / "dryrun")


def cmd_check(cfg) -> int:
    from greatest_trader.broker.mt5_broker import MT5Broker
    from greatest_trader.timeutil import detect_server_mode

    b = MT5Broker(cfg)
    b.connect()
    try:
        print(f"versions       : {format_versions({**versions(), **b.versions()})}")
        print(f"account        : {b.account_login()} @ {b.account_server()}  "
              f"{'HEDGING' if b.is_hedging() else 'NETTING (not supported: a hedging account is required)'}")
        print(f"account equity : {b.account_equity():.2f} {b.account_currency()}")
        syms = list(dict.fromkeys(cfg.night_reversion.symbols + [cfg.euro_session.symbol]))
        print(f"{'symbol':8} {'broker':14} {'digits':>6} {'spread(p)':>9} {'min lot':>8} {'step':>6} {'max lot':>8}")
        for s in syms:
            try:
                sp = b.symbol_spec(s)
                t = b.tick(s)
                spread = (t.ask - t.bid) / sp.pip if t else float("nan")
                print(f"{s:8} {sp.name:14} {sp.digits:6d} {spread:9.2f} {sp.volume_min:8.2f} {sp.volume_step:6.2f} "
                      f"{sp.volume_max:8.2f}")
            except Exception as e:  # noqa: BLE001
                print(f"{s:8} ERROR: {e}")
        st = b.server_time()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        if st:
            mode, off = detect_server_mode(st, now)
            print(f"server time {st}  UTC {now:%Y-%m-%d %H:%M:%S}  -> detected mode={mode} offset={off:+.1f}h")
            print("(detection is only valid while the market is open and ticks are fresh; the live engine "
                  "verifies the clock continuously)")
    finally:
        b.shutdown()
    return 0


def cmd_live(cfg) -> int:
    from greatest_trader.broker.mt5_broker import MT5Broker
    from greatest_trader.engine import FatalError, LiveEngine
    from greatest_trader.notify import Notifier
    from greatest_trader.ops import acquire_single_instance_lock, disable_console_quickedit

    disable_console_quickedit()
    try:
        lock = acquire_single_instance_lock(cfg.engine.state_file)  # held for the life of the engine
    except RuntimeError as exc:
        log.error("%s", exc)
        print(f"\nERROR: {exc}")
        return EXIT_STOP  # the other instance keeps running: this wrapper must not loop
    notifier = Notifier(cfg.notify)
    try:
        return LiveEngine(cfg, MT5Broker(cfg), notifier).run_forever()
    except FatalError as exc:
        log.critical("%s", exc)
        print(f"\nFATAL: {exc}")
        notifier.send(f"FATAL (engine stopped, not restarted): {exc}")
        notifier.flush(10)
        return EXIT_STOP
    finally:
        lock.close()


def cmd_download(cfg, years: float) -> int:
    from greatest_trader.data import download_history

    download_history(cfg, years, cfg.backtest.data_dir)
    return 0


def cmd_backtest(cfg, out: str | None) -> int:
    from greatest_trader.backtest import Backtester, load_data_dir
    from greatest_trader.report import annual_table, format_stats, save_report, yearly_table

    syms = list(dict.fromkeys(cfg.night_reversion.symbols + [cfg.euro_session.symbol]))
    data = load_data_dir(cfg.backtest.data_dir, syms)
    if not data:
        print(f"no data found in {cfg.backtest.data_dir}. Run `python main.py download` first.")
        return 1
    res = Backtester(cfg, data).run()
    if len(res.trades) == 0:
        print("no trades")
        return 1
    print(f"preset: {cfg.preset_label()}")
    r = cfg.risk
    print(f"capacity caps: max_lots_per_order={r.max_lots_per_order:g} max_risk_amount={r.max_risk_amount:g} "
          f"({cfg.backtest.account_currency}); capped trades: {int((res.trades.lots < res.trades.lots_uncapped - 1e-9).sum())}")
    print(format_stats(res.stats()))
    print("\n[年利（各年）]")
    print(annual_table(res).to_string())
    print("\n[月利（%）]")
    print(yearly_table(res).to_string())
    path = save_report(res, out or cfg.backtest.output_dir,
                       title=f"GreatestTrader {__version__} backtest (preset: {cfg.preset_label()})")
    print(f"\nreport saved to {path}")
    return 0


def cmd_report(cfg, since: str | None, out: str | None, broker=None) -> int:
    import pandas as pd

    from greatest_trader.journal import TradeJournal
    from greatest_trader.live_report import add_rolling, edge_monitor_table, monthly_returns_from_deals, \
        pnl_by_magic, write_report

    if broker is None:
        from greatest_trader.broker.mt5_broker import MT5Broker
        broker = MT5Broker(cfg)
    broker.connect()
    try:
        login = broker.account_login()
        deals = broker.history_deals(datetime(2000, 1, 1), datetime.now() + timedelta(days=2))  # whole history
    finally:
        broker.shutdown()
    magics = {cfg.night_reversion.magic: "NR", cfg.euro_session.magic: "ES"}
    monthly = add_rolling(monthly_returns_from_deals(deals, magics))
    if since:
        monthly = monthly[monthly.index >= since]
        deals = deals[deals.time >= pd.Timestamp(since)]
    edge = edge_monitor_table(TradeJournal(cfg.engine.journal_dir).closed_trades(), cfg.monitor.edge_window_trades)
    out_dir = Path(out) if out else Path(cfg.backtest.output_dir) / "live_report"
    text = write_report(out_dir, monthly, pnl_by_magic(deals, magics), edge,
                        title=f"GreatestTrader {__version__} live report, account {login}")
    print(text)
    print(f"report saved to {out_dir}")
    return 0


def cmd_reset_halt(cfg, rebase_peak: bool, equity: float | None = None) -> int:
    from greatest_trader.journal import TradeJournal
    from greatest_trader.ops import acquire_single_instance_lock
    from greatest_trader.risk import RiskManager, RiskState

    try:
        lock = acquire_single_instance_lock(cfg.engine.state_file)
    except RuntimeError as exc:
        print(f"ERROR: {exc}\nStop the live engine (close its window) before reset-halt.")
        return 1
    try:
        path = Path(cfg.engine.state_file)
        st = RiskState.load(path)
        if rebase_peak and equity is None:
            equity = _current_equity(cfg, st)
        msg = RiskManager(cfg.risk, st).reset_halt(equity if rebase_peak else None)
        st.save(path)
        now = datetime.now(timezone.utc)
        TradeJournal(cfg.engine.journal_dir).trade({"event": "reset_halt", "time_utc": f"{now:%Y-%m-%d %H:%M:%S}",
                                                     "reason": msg})
        log.warning("reset-halt (%s): %s", path, msg)
        print(f"{path}: {msg}")
    finally:
        lock.close()
    return 0


def _current_equity(cfg, st) -> float:
    """Account equity from MT5 when it is reachable, else the last equity the engine saw."""
    try:
        from greatest_trader.broker.mt5_broker import MT5Broker
        b = MT5Broker(cfg)
        b.connect()
        try:
            return b.account_equity()
        finally:
            b.shutdown()
    except Exception as exc:  # noqa: BLE001
        print(f"(MT5 not reachable: {exc}; using the last equity seen by the engine: {st.last_equity:,.2f})")
        return st.last_equity


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=f"GreatestTrader {__version__}")
    ap.add_argument("command", choices=["check", "live", "download", "backtest", "report", "reset-halt"])
    ap.add_argument("-c", "--config", default=str(HERE / "config" / "config.yaml"))
    ap.add_argument("--years", type=float, default=5.0, help="download: years of M15 history")
    ap.add_argument("--out", default=None, help="backtest / report: output directory")
    ap.add_argument("--from", dest="since", default=None, help="report: first month, e.g. 2026-01")
    ap.add_argument("--live-orders", action="store_true",
                    help="live: override engine.dry_run=false (send real orders)")
    ap.add_argument("--dry-run", action="store_true",
                    help="live: force dry-run (never send orders); reset-halt: act on the dry-run state")
    ap.add_argument("--rebase-peak", action="store_true", help="reset-halt: also set the peak to current equity")
    ap.add_argument("--preset", default=None,
                    help="apply a bundled preset (overrides `preset:` in the config file), e.g. stable5 / safe2")
    args = ap.parse_args(argv)

    cfg = load_config(args.config, preset=args.preset)
    # relative paths in the config are relative to this folder (python/)
    for obj, attr in ((cfg.engine, "state_file"), (cfg.engine, "log_dir"), (cfg.engine, "journal_dir"),
                      (cfg.backtest, "data_dir"), (cfg.backtest, "output_dir")):
        p = Path(getattr(obj, attr))
        if not p.is_absolute():
            setattr(obj, attr, str(HERE / p))
    if args.live_orders and args.dry_run:
        ap.error("--live-orders and --dry-run are mutually exclusive")
    if args.live_orders:
        cfg.engine.dry_run = False
    if args.dry_run:
        cfg.engine.dry_run = True
    prefix = args.command
    if (args.command == "live" and cfg.engine.dry_run) or (args.command == "reset-halt" and args.dry_run):
        dry_run_paths(cfg)
        prefix = "dryrun" if args.command == "live" else prefix
    real_orders = args.command == "live" and not cfg.engine.dry_run
    fh = setup_logging(cfg.engine.log_dir, cfg.engine.log_level, prefix,
                       console_level="WARNING" if real_orders else None)
    ver = format_versions(versions())
    print(f"GreatestTrader {__version__} | {ver}")
    if real_orders:
        print(f"console shows warnings only; full log: {Path(cfg.engine.log_dir) / (prefix + '_YYYY-MM-DD.log')}")
    log.info("GreatestTrader %s command=%s config=%s preset=%s | %s", __version__, args.command, args.config,
             cfg.preset_label(), ver)
    if args.preset and getattr(cfg, "_risk_overrides", []):
        log.warning("--preset %s is combined with the risk: overrides in %s (%s). Remove them to use the pure preset.",
                    args.preset, args.config, ", ".join(cfg._risk_overrides))
    try:
        if args.command == "check":
            return cmd_check(cfg)
        if args.command == "live":
            return cmd_live(cfg)
        if args.command == "download":
            return cmd_download(cfg, args.years)
        if args.command == "report":
            return cmd_report(cfg, args.since, args.out)
        if args.command == "reset-halt":
            return cmd_reset_halt(cfg, args.rebase_peak)
        return cmd_backtest(cfg, args.out)
    finally:
        fh.flush()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, FileNotFoundError, KeyError, ValueError) as exc:
        logging.getLogger(__name__).error("%s", exc)
        print(f"\nERROR: {exc}")
        raise SystemExit(1)
