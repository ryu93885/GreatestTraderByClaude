"""Append-only audit files of the live engine (never rotated, never rewritten).

* journal/trades_YYYY.csv : one row per entry (event=open) and per closed position (event=close), plus
  operator events (reset_halt). R = P/L / risk amount of the trade (money lost at the stop at entry).
* journal/equity_log.csv  : one row per market day (written at the day change): closing equity, peak, drawdown,
  month-to-date return, cash flows so far, account balance, trades, halt / entry-block status.
* journal/pending_rows.json : rows that could not be written yet (only while a CSV cannot be written, e.g. it is
  open in Excel, which refuses writes by other programs). They are written, in order and before any newer row of
  the same file, as soon as the file can be written again; the file is deleted when nothing is pending. A row is
  therefore never lost: the engine may forget a closed ticket once its close row is handed to the journal.

The R-based edge monitor reads the closed trades back: rw = sum(base_risk x R) / sum(base_risk) over the last
N closed trades, base_risk = preset risk x symbol multiplier BEFORE drawdown scaling, so the statistic does not
depend on position size. Close rows still pending are included.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import math
import os
import time
from pathlib import Path
from typing import Callable

import pandas as pd

log = logging.getLogger(__name__)

TRADE_FIELDS = ["event", "time_utc", "server_time", "strategy", "symbol", "direction", "lots", "intended_price",
                "fill_price", "sl", "tp", "risk_pct", "risk_amount", "base_risk", "ticket", "close_time",
                "close_price", "close_reason", "pnl", "R", "reason"]
EQUITY_FIELDS = ["market_date", "time_utc", "close_equity", "peak_equity", "drawdown", "month_return",
                 "cash_flow_total", "opens", "closes", "halted", "entries_blocked", "balance"]
EDGE_MIN_TRADES = 50  # the R monitor needs at least this many closed trades before it can warn
PENDING_FILE = "pending_rows.json"
ALERT_EVERY = 3600.0  # seconds between two 'journal file locked' alerts of the same incident
_FIELDS = {"trade": TRADE_FIELDS, "equity": EQUITY_FIELDS}


class TradeJournal:
    """`alert` (optional) is called with one text when rows cannot be written ('journal file locked (open in
    Excel?) - N rows pending'), at once and then at most every `alert_every` seconds while rows stay pending, and
    once more when they have been written. Without it the journal still logs an ERROR once per incident."""

    def __init__(self, directory: str | Path, alert: Callable[[str], object] | None = None,
                 alert_every: float = ALERT_EVERY):
        self.dir = Path(directory)
        self.alert = alert
        self.alert_every = alert_every
        self._pending: list[dict] = []  # {"file": name, "kind": "trade" | "equity", "row": {field: text}}
        self._loaded = False  # pending_rows.json of an earlier run read (a missing file counts as read)
        self._dirty = False  # the backlog changed but pending_rows.json could not be written yet
        self._incident = False  # rows pending since the last time everything was written
        self._alerted = False  # an alert was sent in this incident
        self._last_alert = -math.inf
        self._load()

    @property
    def pending_path(self) -> Path:
        return self.dir / PENDING_FILE

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    # ------------------------------------------------------------------ public API
    def trade(self, row: dict) -> bool:
        """Append a row to trades_<year of time_utc>.csv. True: written now; False: kept in the backlog (written
        with the next row, or by flush_pending(), once the file can be written)."""
        year = str(row.get("time_utc", ""))[:4] or "0000"
        return self._add(f"trades_{year}.csv", "trade", row)

    def equity(self, row: dict) -> bool:
        return self._add("equity_log.csv", "equity", row)

    def flush_pending(self) -> int:
        """Write the rows still pending, in order (the engine may call this every loop; nothing to do = no I/O).
        Returns the number of rows still pending."""
        if not self._load():
            return len(self._pending)
        if self._pending:
            self._flush()
        elif self._dirty:
            self._persist()
        return len(self._pending)

    def closed_trades(self) -> pd.DataFrame:
        """Every close row of every trades_YYYY.csv, oldest first, including close rows still pending."""
        self._load()
        pending: dict[str, list[dict]] = {}
        for item in self._pending:
            if item["kind"] == "trade":
                pending.setdefault(item["file"], []).append(item["row"])
        frames = []
        for name in sorted({p.name for p in self.dir.glob("trades_*.csv")} | set(pending)):
            p = self.dir / name
            try:
                if name in pending:  # file rows, then its pending rows: read as one table (same column types)
                    text = p.read_text(encoding="utf-8") if p.exists() else ""
                    header = next(csv.reader(io.StringIO(text)), None) or TRADE_FIELDS
                    buf = io.StringIO()
                    w = csv.DictWriter(buf, fieldnames=header, extrasaction="ignore")
                    if not text.strip():
                        w.writeheader()
                    elif not text.endswith("\n"):
                        buf.write("\n")
                    w.writerows(pending[name])
                    df = pd.read_csv(io.StringIO(text + buf.getvalue() if text.strip() else buf.getvalue()))
                else:
                    df = pd.read_csv(p)
            except (OSError, ValueError, pd.errors.EmptyDataError) as exc:
                log.warning("cannot read %s: %s", name, exc)
                continue
            if "event" in df.columns:
                frames.append(df[df["event"] == "close"])
        if not frames:
            return pd.DataFrame(columns=TRADE_FIELDS)
        out = pd.concat(frames, ignore_index=True)
        for c in ("R", "base_risk", "pnl", "risk_amount"):
            out[c] = pd.to_numeric(out[c], errors="coerce")
        return out

    # ------------------------------------------------------------------ backlog
    def _add(self, name: str, kind: str, row: dict) -> bool:
        item = {"file": name, "kind": kind, "row": {k: _fmt(row.get(k, "")) for k in _FIELDS[kind]}}
        if not self._load():  # the backlog of an earlier run cannot be read yet: keep the order, write nothing
            self._pending.append(item)  # (in memory: pending_rows.json must not be overwritten)
            self._failed(name, None, f"{self.pending_path} (rows of an earlier run) cannot be read yet")
            return False
        blocked = self._flush(report=False) if self._pending else {}
        exc = blocked.get(name)
        if exc is None and not any(it["file"] == name for it in self._pending):
            exc = self._write(item)
            if exc is None:
                if self._dirty:
                    self._persist()
                return True
        self._pending.append(item)  # after the older rows of this file: the order is kept
        self._persist()
        self._failed(name, exc)
        return False

    def _flush(self, report: bool = True) -> dict[str, OSError]:
        """Write the pending rows in order; a file that fails keeps the rest of its rows pending (other files go
        on). Returns {file: error} of the files that could not be written."""
        before = len(self._pending)
        blocked: dict[str, OSError] = {}
        remaining = []
        for item in self._pending:
            if item["file"] in blocked:
                remaining.append(item)
                continue
            exc = self._write(item)
            if exc is not None:
                blocked[item["file"]] = exc
                remaining.append(item)
        written = before - len(remaining)
        self._pending = remaining
        if written or self._dirty:
            self._persist()
        if remaining:
            if report:
                name = remaining[0]["file"]
                self._failed(name, blocked.get(name))
        elif self._incident:
            self._resolved(written)
        return blocked

    def _write(self, item: dict) -> OSError | None:
        """Append one formatted row; None when written. A file written by an older version keeps its own header
        (columns added later are left out of its rows), so it stays readable."""
        path = self.dir / item["file"]
        fieldnames = _FIELDS[item["kind"]]
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            new = not path.exists() or path.stat().st_size == 0
            if not new:
                with open(path, "r", newline="", encoding="utf-8") as f:
                    header = next(csv.reader(f), None)
                if header and header != fieldnames and set(header) <= set(fieldnames):
                    fieldnames = header
            with open(path, "a", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
                if new:
                    w.writeheader()
                w.writerow({k: item["row"].get(k, "") for k in fieldnames})
            return None
        except OSError as exc:
            return exc

    def _load(self) -> bool:
        """Read pending_rows.json once. False while it exists but cannot be read (it is retried later; the rows
        it holds must be written before newer rows, and it must not be overwritten)."""
        if self._loaded:
            return True
        p = self.pending_path
        try:
            text = p.read_text(encoding="utf-8")
        except FileNotFoundError:
            self._loaded = True
            return True
        except OSError as exc:
            log.warning("cannot read %s yet: %s (retried with the next journal row)", p, exc)
            return False
        rows: list[dict] = []
        try:
            data = json.loads(text)
            for it in data["rows"]:
                if it["kind"] not in _FIELDS or not isinstance(it["file"], str) or "/" in it["file"] \
                        or "\\" in it["file"] or not isinstance(it["row"], dict):
                    raise ValueError(f"bad entry {it!r}")
                rows.append({"file": it["file"], "kind": it["kind"],
                             "row": {k: _fmt(it["row"].get(k, "")) for k in _FIELDS[it["kind"]]}})
        except (ValueError, TypeError, KeyError) as exc:
            bad = p.with_name(f"{p.name}.corrupt-{time.strftime('%Y%m%d%H%M%S')}")
            try:
                p.replace(bad)
                kept = f"moved to {bad.name}"
            except OSError:
                kept = "could not be moved aside"
            log.error("journal backlog %s is unreadable (%s): %s, its rows are NOT written", p, exc, kept)
            rows = []
        queued = self._pending  # rows queued while it could not be read come after its rows
        self._pending = rows + queued
        self._loaded = True
        if rows:
            self._incident = True
            log.warning("%d journal rows pending from an earlier run (%s): written before the next rows", len(rows),
                        p)
        if queued:
            self._persist()
        return True

    def _persist(self) -> None:
        """Write the backlog atomically (tmp file + replace); delete the file when nothing is pending."""
        p = self.pending_path
        try:
            if not self._pending:
                if p.exists():
                    p.unlink()
            else:
                self.dir.mkdir(parents=True, exist_ok=True)
                tmp = p.with_name(p.name + ".tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump({"version": 1, "rows": self._pending}, f, ensure_ascii=False, indent=1)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, p)
            self._dirty = False
        except OSError as exc:
            if not self._dirty:
                log.error("cannot save the journal backlog %s: %s (%d rows kept in memory, saved again with the "
                          "next row)", p, exc, len(self._pending))
            self._dirty = True

    # ------------------------------------------------------------------ incident: log once, alert rate-limited
    def _failed(self, name: str, exc: OSError | None, why: str = "") -> None:
        n = len(self._pending)
        locked = exc is None or isinstance(exc, PermissionError)
        what = "journal file locked (open in Excel?)" if locked else "journal file cannot be written"
        text = (f"{what} - {n} rows pending: "
                + (why or f"{name} cannot be written" + (f" ({exc})" if exc is not None else ""))
                + f". The rows are kept (in {self.pending_path}) and written in order once the file is free: close "
                  "it in Excel (open a copy to look at the journal)")
        if not self._incident:
            self._incident = True
            log.error(text)
        else:
            log.debug(text)
        now = time.monotonic()
        if now - self._last_alert >= self.alert_every:
            if self._alerted:
                log.warning(text)  # hourly reminder in the log as well
            self._last_alert = now
            self._alerted = True
            self._notify(text)

    def _resolved(self, written: int) -> None:
        text = f"journal files writable again: {written} pending rows written"
        log.warning(text)
        if self._alerted:
            self._notify(text)
        self._incident = self._alerted = False
        self._last_alert = -math.inf

    def _notify(self, text: str) -> None:
        if self.alert is None:
            return
        try:
            self.alert(text)
        except Exception as exc:  # noqa: BLE001 - a notification problem must never stop the journal
            log.warning("journal alert failed: %s", exc)


def risk_weighted_r(closes: pd.DataFrame, window: int) -> tuple[float, int]:
    """(rw, n): sum(base_risk x R) / sum(base_risk) over the last `window` closed trades with a known R and
    base risk; (nan, n) when there are none."""
    if len(closes) == 0:
        return float("nan"), 0
    d = closes[["R", "base_risk"]].apply(pd.to_numeric, errors="coerce").dropna()
    d = d[d.base_risk > 0].tail(window)
    if len(d) == 0:
        return float("nan"), 0
    return float((d.base_risk * d.R).sum() / d.base_risk.sum()), int(len(d))


def _fmt(v) -> str:
    if isinstance(v, float):
        if math.isnan(v):
            return ""
        return f"{v:.10g}"
    return "" if v is None else str(v)
