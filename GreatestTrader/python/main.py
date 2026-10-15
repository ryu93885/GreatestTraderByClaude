"""GreatestTrader command line.

  python main.py check     -c config/config.yaml   # connect to MT5, show account/symbols/spreads (no trading)
  python main.py live      -c config/config.yaml   # run the trading engine (dry_run setting in config!)
  python main.py download  -c config/config.yaml --years 5   # fetch M15 history from MT5 into backtest.data_dir
  python main.py backtest  -c config/config.yaml   # Python backtest on CSV data in backtest.data_dir
"""
from __future__ import annotations

import argparse
import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from greatest_trader import __version__  # noqa: E402
from greatest_trader.config import load_config  # noqa: E402


def setup_logging(log_dir: str, level: str) -> None:
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    fh = TimedRotatingFileHandler(Path(log_dir) / "greatest_trader.log", when="midnight", backupCount=60,
                                  encoding="utf-8")
    fh.setFormatter(fmt)
    root.handlers[:] = [sh, fh]


def cmd_check(cfg) -> int:
    from datetime import datetime, timezone

    from greatest_trader.broker.mt5_broker import MT5Broker
    from greatest_trader.timeutil import detect_server_mode

    b = MT5Broker(cfg)
    b.connect()
    try:
        print(f"account equity : {b.account_equity():.2f} {b.account_currency()}")
        syms = list(dict.fromkeys(cfg.night_reversion.symbols + [cfg.euro_session.symbol]))
        print(f"{'symbol':8} {'broker':14} {'digits':>6} {'spread(p)':>9} {'min lot':>8} {'step':>6}")
        for s in syms:
            try:
                sp = b.symbol_spec(s)
                t = b.tick(s)
                spread = (t.ask - t.bid) / sp.pip if t else float("nan")
                print(f"{s:8} {sp.name:14} {sp.digits:6d} {spread:9.2f} {sp.volume_min:8.2f} {sp.volume_step:6.2f}")
            except Exception as e:  # noqa: BLE001
                print(f"{s:8} ERROR: {e}")
        st = b.server_time()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        if st:
            mode, off = detect_server_mode(st, now)
            print(f"server time {st}  UTC {now:%Y-%m-%d %H:%M:%S}  -> detected mode={mode} offset={off:+.1f}h")
            print("(detection is only valid while the market is open and ticks are fresh)")
    finally:
        b.shutdown()
    return 0


def cmd_live(cfg) -> int:
    from greatest_trader.broker.mt5_broker import MT5Broker
    from greatest_trader.engine import LiveEngine

    LiveEngine(cfg, MT5Broker(cfg)).run_forever()
    return 0


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
    print(format_stats(res.stats()))
    print("\n[年利（各年）]")
    print(annual_table(res).to_string())
    print("\n[月利（%）]")
    print(yearly_table(res).to_string())
    path = save_report(res, out or cfg.backtest.output_dir)
    print(f"\nreport saved to {path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=f"GreatestTrader {__version__}")
    ap.add_argument("command", choices=["check", "live", "download", "backtest"])
    ap.add_argument("-c", "--config", default=str(HERE / "config" / "config.yaml"))
    ap.add_argument("--years", type=float, default=5.0, help="download: years of M15 history")
    ap.add_argument("--out", default=None, help="backtest: output directory")
    ap.add_argument("--live-orders", action="store_true",
                    help="live: override engine.dry_run=false (send real orders)")
    ap.add_argument("--dry-run", action="store_true", help="live: force dry-run (never send orders)")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    # relative paths in the config are relative to this folder (python/)
    for obj, attr in ((cfg.engine, "state_file"), (cfg.engine, "log_dir"), (cfg.backtest, "data_dir"),
                      (cfg.backtest, "output_dir")):
        p = Path(getattr(obj, attr))
        if not p.is_absolute():
            setattr(obj, attr, str(HERE / p))
    if args.live_orders and args.dry_run:
        ap.error("--live-orders and --dry-run are mutually exclusive")
    if args.live_orders:
        cfg.engine.dry_run = False
    if args.dry_run:
        cfg.engine.dry_run = True
    setup_logging(cfg.engine.log_dir, cfg.engine.log_level)
    logging.getLogger(__name__).info("GreatestTrader %s command=%s config=%s", __version__, args.command,
                                     args.config)
    if args.command == "check":
        return cmd_check(cfg)
    if args.command == "live":
        return cmd_live(cfg)
    if args.command == "download":
        return cmd_download(cfg, args.years)
    return cmd_backtest(cfg, args.out)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, FileNotFoundError, KeyError, ValueError) as exc:
        logging.getLogger(__name__).error("%s", exc)
        print(f"\nERROR: {exc}")
        raise SystemExit(1)
