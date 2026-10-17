"""GreatestTrader command line.

commands:
  python main.py check     -c config/config.yaml   # connect to MT5, show account/symbols/spreads (no trading)
  python main.py live      -c config/config.yaml   # run the trading engine (dry_run setting in config!)
  python main.py download  -c config/config.yaml --years 5   # fetch M15 history from MT5 into backtest.data_dir
  python main.py backtest  -c config/config.yaml   # Python backtest on CSV data in backtest.data_dir
  python main.py report    -c config/config.yaml [--from 2026-01]   # live monthly returns / P/L / R monitor
  python main.py reset-halt [--rebase-peak]        # clear a drawdown halt (stop the engine first; the engine
                                                   # applies it at its next start, after the cash-flow check);
                                                   # --rebase-peak (7_reset_halt.bat) also resets the peak to the
                                                   # current equity: needed while DD >= max_drawdown_halt
  python main.py backtest --preset safe2           # any command: use a bundled risk preset instead of `preset:`

exit codes:
  0   done (every command except `live`)
  1   failed; `live`: an unexpected error at start-up, or config.yaml could not be read for a moment (held by
      another program) (3_run_live.bat restarts after 30 s)
  3   `live`: MT5 failing for 30 minutes (3_run_live.bat restarts the process)
  10  stopped on purpose or by a problem a restart cannot fix: Ctrl+C, another instance running, netting
      account, an error in config.yaml, the MetaTrader5 package missing (3_run_live.bat does NOT restart)
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from greatest_trader import __version__  # noqa: E402
from greatest_trader.config import load_config  # noqa: E402
from greatest_trader.ops import EXIT_STOP, format_versions, setup_logging, versions  # noqa: E402

log = logging.getLogger("main")


def utf8_stdio() -> None:
    """Japanese report text must never crash a run whose output is redirected (file, Task Scheduler, IDE) on a
    Windows code page that cannot encode it (cp1252 ...): such streams are switched to UTF-8, and characters
    that still cannot be written are replaced."""
    for s in (sys.stdout, sys.stderr):
        try:
            enc = (getattr(s, "encoding", None) or "").lower().replace("-", "")
            if enc == "utf8":
                s.reconfigure(errors="replace")
            else:
                s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):  # not a text stream that can be reconfigured
            pass


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

    written, failed = download_history(cfg, years, cfg.backtest.data_dir)
    print(f"downloaded {len(written)} pairs into {cfg.backtest.data_dir}")
    if failed:
        print(f"NOT downloaded ({len(failed)}):")
        for s, why in failed.items():
            print(f"  {s}: {why}")
    return 0 if written else 1


def cmd_backtest(cfg, out: str | None) -> int:
    from greatest_trader.backtest import Backtester, conversion_pairs, load_data_dir
    from greatest_trader.report import annual_table, format_stats, save_report, yearly_table

    syms = list(dict.fromkeys(cfg.night_reversion.symbols + [cfg.euro_session.symbol]))
    data = load_data_dir(cfg.backtest.data_dir, syms)
    if not data:
        print(f"no data found in {cfg.backtest.data_dir}. Run `python main.py download` first.")
        return 1
    extra = conversion_pairs(cfg.backtest.data_dir, syms, cfg.backtest.account_currency)
    if extra:  # not traded: only for converting lots / margin to the account currency
        data.update(load_data_dir(cfg.backtest.data_dir, extra))
        print(f"conversion pairs (not traded): {', '.join(extra)}")
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


def cmd_report(cfg, since, out: str | None, broker=None) -> int:
    """since: first month (pandas Period 'M', parsed by main) or None."""
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
    if since is not None:  # the monthly table and P/L by strategy both start at the first day of that month
        if len(monthly):
            monthly = monthly[monthly.index.astype(str) >= str(since)]  # both "YYYY-MM"
        if len(deals):
            deals = deals[deals.time >= since.start_time]
    edge = edge_monitor_table(TradeJournal(cfg.engine.journal_dir).closed_trades(), cfg.monitor.edge_window_trades)
    out_dir = Path(out) if out else Path(cfg.backtest.output_dir) / "live_report"
    text = write_report(out_dir, monthly, pnl_by_magic(deals, magics), edge,
                        title=f"GreatestTrader {__version__} live report, account {login}")
    print(text)
    print(f"report saved to {out_dir}")
    return 0


def cmd_reset_halt(cfg, rebase_peak: bool) -> int:
    """Store a reset request in the state. The engine applies it at its next start, right AFTER its first
    cash-flow check: a deposit / withdrawal made while it was stopped is applied to the old peak first, and the
    peak is rebased to an equity that already contains it (otherwise the deposit would be multiplied onto the new
    peak and the halt would fire again at once)."""
    from greatest_trader.journal import TradeJournal
    from greatest_trader.ops import acquire_single_instance_lock
    from greatest_trader.risk import RiskState

    try:
        lock = acquire_single_instance_lock(cfg.engine.state_file)
    except RuntimeError as exc:
        print(f"ERROR: {exc}\nStop the live engine (close its window) before reset-halt.")
        return 1
    try:
        path = Path(cfg.engine.state_file)
        st = RiskState.load(path)
        st.reset_request = "rebase" if rebase_peak else "clear"
        st.save(path)
        what = "clear the halt" + (" and rebase the peak equity to the account equity" if rebase_peak else "")
        msg = (f"reset requested ({what}; halt now: {st.halt_reason if st.halted else 'not halted'}). The engine "
               f"applies it at its next start, after it has checked the deposits/withdrawals")
        now = datetime.now(timezone.utc)
        TradeJournal(cfg.engine.journal_dir).trade({"event": "reset_halt", "time_utc": f"{now:%Y-%m-%d %H:%M:%S}",
                                                     "reason": msg})
        log.warning("reset-halt (%s): %s", path, msg)
        print(f"{path}: {msg}")
    finally:
        lock.close()
    return 0


CONFIG_READ_RETRIES = 5  # a config file that cannot be read (but exists) is read again this many times ...
CONFIG_READ_PAUSE = 0.2  # ... this many seconds apart, like RiskState.load does for a locked state file


def _load(args):
    """Config with paths relative to this folder (python/). Raises ConfigError for anything wrong in it (exit 10:
    a restart cannot fix it): file or folder not found, not UTF-8, invalid YAML, unknown keys, invalid values.
    Any other OSError (PermissionError while a backup / sync tool or an editor holds the file, a network share
    that does not answer) is retried and, when it persists, raised as it is: exit 1, 3_run_live.bat restarts."""
    import yaml

    from greatest_trader.config import ConfigError

    for attempt in range(CONFIG_READ_RETRIES + 1):
        try:
            cfg = load_config(args.config, preset=args.preset)
            break
        except ConfigError:
            raise
        except (FileNotFoundError, NotADirectoryError) as exc:
            raise ConfigError(f"config file not found: {exc.filename or args.config}") from None
        except IsADirectoryError as exc:
            raise ConfigError(f"{exc.filename or args.config} is a folder, not a config file") from None
        except UnicodeDecodeError as exc:
            raise ConfigError(f"{args.config} is not UTF-8 text (save it as UTF-8): {exc}") from None
        except yaml.YAMLError as exc:
            raise ConfigError(f"{args.config} is not valid YAML (indent with spaces, not tabs): {exc}") from None
        except OSError as exc:
            name = exc.filename or args.config
            if Path(name).is_dir():  # Windows raises PermissionError (not IsADirectoryError) for a folder
                raise ConfigError(f"{name} is a folder, not a config file") from None
            if attempt >= CONFIG_READ_RETRIES:
                raise  # still unreadable: not a configuration error, a restart may fix it (exit 1)
            time.sleep(CONFIG_READ_PAUSE)
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            text = exc.args[0] if isinstance(exc, KeyError) and exc.args else str(exc)
            raise ConfigError(f"{args.config}: {text}") from None
    for obj, attr in ((cfg.engine, "state_file"), (cfg.engine, "log_dir"), (cfg.engine, "journal_dir"),
                      (cfg.backtest, "data_dir"), (cfg.backtest, "output_dir")):
        p = Path(getattr(obj, attr))
        if not p.is_absolute():
            setattr(obj, attr, str(HERE / p))
    return cfg


def main(argv: list[str] | None = None) -> int:
    utf8_stdio()
    ap = argparse.ArgumentParser(description=f"GreatestTrader {__version__}", epilog=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
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
    if args.live_orders and args.dry_run:
        ap.error("--live-orders and --dry-run are mutually exclusive")
    since = None
    if args.since:  # YYYY-MM (a day YYYY-MM-DD means its month); parsed once, used for every table of the report
        import re

        import pandas as pd
        try:
            if not re.fullmatch(r"\d{4}-\d{1,2}(-\d{1,2})?", str(args.since).strip()):
                raise ValueError
            since = pd.Period(str(args.since).strip(), "M")
        except (ValueError, TypeError):
            ap.error(f"--from must be a month such as 2026-01 (got {args.since!r})")

    from greatest_trader.broker.mt5_broker import MissingPackageError
    from greatest_trader.config import ConfigError

    try:
        cfg = _load(args)  # relative paths in the config are relative to this folder (python/)
    except ConfigError as exc:  # a restart cannot fix it: exit 10 stops 3_run_live.bat
        print(f"\nCONFIG ERROR: {exc}\nFix config.yaml, then start again (exit code {EXIT_STOP}: not restarted).")
        return EXIT_STOP
    except OSError as exc:  # still unreadable after the retries (held by another program?): a restart may fix it
        print(f"\nERROR: cannot read the config file {args.config}: {exc}\nIt may be held by another program "
              f"(backup / sync tool, editor). Exit code 1: 3_run_live.bat restarts after 30 s.")
        return 1
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
            return cmd_report(cfg, since, args.out)
        if args.command == "reset-halt":
            return cmd_reset_halt(cfg, args.rebase_peak)
        return cmd_backtest(cfg, args.out)
    except MissingPackageError as exc:  # a restart cannot fix it: exit 10 stops 3_run_live.bat
        log.error("%s", exc)
        print(f"\nERROR: {exc}\n(exit code {EXIT_STOP}: not restarted)")
        return EXIT_STOP
    finally:
        fh.flush()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, FileNotFoundError, KeyError, ValueError) as exc:
        logging.getLogger(__name__).error("%s", exc)
        print(f"\nERROR: {exc}")
        raise SystemExit(1)
