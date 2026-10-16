"""Download historical bars from the MT5 terminal for the Python backtester."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from .config import Config

log = logging.getLogger(__name__)


def download_history(cfg: Config, years: float, out_dir: str | Path) -> tuple[list[Path], dict[str, str]]:
    """Returns (files written, {pair: why it was not downloaded}). One pair the broker does not offer (or names
    differently) never stops the others."""
    from .broker.mt5_broker import MT5Broker, mt5

    broker = MT5Broker(cfg)
    broker.connect()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    syms = list(dict.fromkeys(cfg.night_reversion.symbols + [cfg.euro_session.symbol]))
    written: list[Path] = []
    failed: dict[str, str] = {}
    try:
        end = datetime.now(timezone.utc) + timedelta(days=2)
        start = end - timedelta(days=int(years * 365.25) + 2)
        for s in syms:
            try:
                path = _download_symbol(broker, mt5, s, start, end, out)
            except Exception as exc:  # noqa: BLE001 - per pair: the next pair is still downloaded
                log.warning("%s: not downloaded (%s) - continuing with the next pair", s, exc)
                failed[s] = str(exc)
                continue
            if path is None:
                failed[s] = (f"no history returned ({mt5.last_error()}): scroll the M15 chart back in MT5 or raise "
                             f"'Max bars in chart' (Tools > Options > Charts)")
                log.warning("%s: %s", s, failed[s])
                continue
            written.append(path)
    finally:
        broker.shutdown()
    if failed:
        log.warning("not downloaded: %s", ", ".join(sorted(failed)))
    return written, failed


def _download_symbol(broker, mt5, s: str, start: datetime, end: datetime, out: Path) -> Path | None:
    spec = broker.symbol_spec(s)
    frames = []
    t0 = start
    while t0 < end:  # chunked: brokers limit bars per request
        t1 = min(t0 + timedelta(days=180), end)
        arr = mt5.copy_rates_range(spec.name, mt5.TIMEFRAME_M15, t0, t1)
        if arr is not None and len(arr):
            frames.append(pd.DataFrame(arr))
        t0 = t1
    if not frames:
        return None
    df = pd.concat(frames).drop_duplicates("time").sort_values("time")
    df["time"] = pd.to_datetime(df["time"], unit="s")
    df["spread_pips"] = df["spread"] * spec.point / spec.pip
    path = out / f"{s}_M15.csv"
    df[["time", "open", "high", "low", "close", "spread_pips"]].to_csv(path, index=False)
    log.info("%s: %d bars %s -> %s saved to %s", s, len(df), df.time.iloc[0], df.time.iloc[-1], path)
    return path
