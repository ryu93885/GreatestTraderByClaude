"""Live trading engine (production).

One loop every engine.poll_seconds:
  1. broker health (a lost MT5 link is re-initialised) and account binding (no trading on another account);
  2. ticks of every symbol, server-clock verification, stale-feed watchdog;
  3. cash flows (deposits / withdrawals / credit / bonus) rebase the equity baselines BEFORE the guards;
  4. risk guards; at a market-day change the equity log + daily summary, at a month end the R monitor;
  5. hard-stop flattening, time stops, Night Reversion per symbol and Euro Session, each isolated so that one
     failing symbol never stops the others;
  6. trade journal of closed positions, heartbeat, dead-man-switch ping, state save (only on change).

New entries need a verified server clock (auto mode: measured from fresh ticks after every start) and are
blocked while the configured clock disagrees with the broker. Closes, time stops, TP management and hard
stops keep working with the last known clock.
"""
from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

import pandas as pd

from . import __version__
from .broker.base import Broker
from .config import Config
from .indicators import atr
from .journal import EDGE_MIN_TRADES, TradeJournal, risk_weighted_r
from .notify import Notifier
from .ops import EXIT_RESTART, EXIT_STOP, format_versions, process_memory_mb, versions
from .risk import RiskManager, RiskState
from .strategies import CloseRequest, EntryRequest, EuroSession, ModifyRequest, NightReversion, PositionView
from .timeutil import ServerClock, detect_server_mode, utc_to_market

log = logging.getLogger(__name__)
M15 = timedelta(minutes=15)
H1 = timedelta(hours=1)
FRESH_SIGNAL = timedelta(seconds=150)  # act on a closed bar only if it closed within this time
MARGIN_USE_MAX = 0.8  # a new position may use at most 80% of the current free margin
CLOCK_TOL = 120.0  # s: a fresh tick lies within this distance of a whole half-hour offset from real UTC
TICK_FRESH = 120.0  # s: ... and the server time advanced at most this long ago (VPS clock)
CLOCK_CONFIRM = 3  # identical observations to adopt a changed offset (auto) / mismatches to block entries
MAX_FAIL_SECONDS = 1800.0  # continuous failure -> CRITICAL notification and exit code 3 (wrapper restarts)
HEARTBEAT_SECONDS = 3600.0
SYMBOL_RETRY_SECONDS = 3600.0  # unavailable symbols are tried again this often
SYMBOL_FAIL_ALERT = 60  # loops in a row before a failing symbol is notified (then every 6 hours)
STATE_SAVE_SECONDS = 60.0  # equity-only changes are written at most this often (everything else at once)
CLOSED_LOOKUP_TRIES = 120  # loops to wait for the deal history of a closed position before journaling it
FX_HOLIDAYS = {(12, 25), (1, 1)}  # market dates without trading


class FatalError(RuntimeError):
    """A setup the engine must never trade with (e.g. a netting account): stop, do not restart."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _epoch(t: datetime) -> float:
    return t.replace(tzinfo=timezone.utc).timestamp()


class LiveEngine:
    def __init__(self, cfg: Config, broker: Broker, notifier: Notifier | None = None, utcnow=None):
        self.cfg = cfg
        self.broker = broker
        self.notifier = notifier or Notifier(cfg.notify)
        self.nr = NightReversion(cfg.night_reversion)
        self.es = EuroSession(cfg.euro_session)
        # corrupt -> newest backup, else HALTED; a file that stays locked raises (exit 1: the wrapper restarts)
        self.state = RiskState.load(cfg.engine.state_file)
        self.risk = RiskManager(cfg.risk, self.state)
        self.journal = TradeJournal(cfg.engine.journal_dir)
        self.magics = {cfg.night_reversion.magic: "NR", cfg.euro_session.magic: "ES"}
        self.last_bar: dict[str, datetime] = {}
        self.disabled: dict[str, str] = {}  # symbol -> why it is not traded (unavailable at the broker)
        self._utcnow = utcnow or _utcnow
        self.clock = ServerClock("ny7")
        self.clock_verified = False
        self.clock_problem = ""  # explicit clock disagrees with the broker -> new entries blocked
        self._init_clock()
        self._started = False
        self._sym_fail: dict[str, int] = {}
        self._prev_st: datetime | None = None
        self._st_moved_utc: datetime | None = None  # VPS UTC when the server time last advanced
        self._watch_from_utc: datetime | None = None  # stale-feed watchdog reference
        self._clock_cand: float | None = None
        self._clock_n = 0
        self._account_problem = ""
        self._balance: float | None = None  # account balance at the last cash-flow check (equity log)
        self._lot_cap_day: dict[str, str] = {}
        self._closing: dict[int, str] = {}  # ticket -> reason of a close sent by the engine (journal)
        self._gap_logged: dict[str, datetime] = {}
        self._last_heartbeat = -1e18
        self._last_sym_retry = time.monotonic()
        self._last_save = -1e18
        self._saved_key = ""

    # ------------------------------------------------------------------ helpers
    def _alert(self, text: str, key: str | None = None, every: float = 0.0, level: int = logging.WARNING) -> bool:
        """Log and notify. A keyed alert is sent (and logged at `level`) at most every `every` seconds.
        Returns True when it was sent."""
        if self.notifier.send(text, key=key, every=every):
            log.log(level, text)
            return True
        log.debug(text)
        return False

    def all_symbols(self) -> list[str]:
        syms = []
        if self.cfg.night_reversion.enabled:
            syms += list(self.cfg.night_reversion.symbols)
        if self.cfg.euro_session.enabled and self.cfg.euro_session.symbol not in syms:
            syms.append(self.cfg.euro_session.symbol)
        return syms

    def symbols(self) -> list[str]:
        """Symbols that are traded now (configured and available at the broker)."""
        return [s for s in self.all_symbols() if s not in self.disabled]

    def _base_risk(self, strategy: str, symbol: str) -> float:
        return self.cfg.risk.base_risk(strategy.lower()) * self.risk.symbol_mult(symbol)

    # ------------------------------------------------------------------ setup
    def _init_clock(self) -> None:
        b = self.cfg.broker
        if b.server_time_mode == "auto":
            st = self.state
            if st.clock_mode in ("ny7", "fixed"):
                self.clock = ServerClock(st.clock_mode, st.clock_offset)
            else:
                self.clock = ServerClock("ny7")
            self.clock_verified = False  # new entries wait for a fresh-tick measurement
        else:
            self.clock = ServerClock(b.server_time_mode, b.server_gmt_offset)
            self.clock_verified = True

    def start(self) -> None:
        self.broker.connect()
        if not self.broker.is_hedging():
            raise FatalError("this MT5 account uses NETTING position accounting. GreatestTrader needs a HEDGING "
                             "account (one position per strategy and symbol, closed by ticket): open a hedging "
                             "account at the broker.")
        log.info("versions: GreatestTrader=%s %s", __version__, format_versions({**versions(), **self.broker.versions()}))
        for s in self.all_symbols():
            try:
                spec = self.broker.symbol_spec(s)
                log.info("symbol %-7s -> %-12s digits=%d pip=%g min_lot=%g step=%g max_lot=%g", s, spec.name,
                         spec.digits, spec.pip, spec.volume_min, spec.volume_step, spec.volume_max)
                self.broker.tick(s)
                self.disabled.pop(s, None)
            except Exception as exc:  # noqa: BLE001 - one missing symbol must not stop the others
                self.disabled[s] = str(exc)
                self._alert(f"symbol {s} unavailable ({exc}): trading continues without it", key=f"sym-{s}",
                            every=86400)
        eq = self.broker.account_equity()
        mode = "DRY-RUN (no orders)" if self.cfg.engine.dry_run else "LIVE"
        r = self.cfg.risk
        mults = [self.risk.symbol_mult(s) for s in self.cfg.night_reversion.symbols] or [1.0]
        nr_lo, nr_hi = r.base_risk("nr") * min(mults), r.base_risk("nr") * max(mults)
        es_risk = r.base_risk("es") * self.risk.symbol_mult(self.cfg.euro_session.symbol)
        caps = (f"max {r.max_lots_per_order:g} lots/order" if r.max_lots_per_order > 0 else "no lot cap") + \
            (f", max risk {r.max_risk_amount:,.0f}/trade" if r.max_risk_amount > 0 else "")
        clock = f"{self.clock}" + ("" if self.clock_verified else " (entries wait for verification)")
        msg = (f"started {mode}: equity={eq:.2f} {self.broker.account_currency()} preset={self.cfg.preset_label()} "
               f"profile={r.profile} NR={nr_lo:.2%}" + (f"-{nr_hi:.2%}" if nr_hi > nr_lo else "")
               + f" ES={es_risk:.2%} (before DD scaling) {caps} DD halt={r.max_drawdown_halt:.0%} clock={clock}")
        if self.disabled:
            msg += f" UNAVAILABLE: {', '.join(sorted(self.disabled))}"
        log.info(msg)
        self.notifier.send(msg)
        if self.state.restored_from:
            self._alert(f"state file was unreadable or missing -> restored from: {self.state.restored_from}",
                        level=logging.ERROR)
        if self.state.reset_request:
            log.warning("reset-halt requested (%s): applied after the first cash-flow check", self.state.reset_request)
        elif self.state.halted:
            self._alert(f"HALTED: {self.state.halt_reason} (no new entries; `python main.py reset-halt` to resume)",
                        level=logging.ERROR)
        self._started = True

    # ------------------------------------------------------------------ loop
    def run_forever(self) -> int:
        """Run until stopped. Returns the process exit code: EXIT_STOP (10) after Ctrl+C, EXIT_RESTART (3) after
        30 minutes of continuous failure (the wrapper starts a fresh process). FatalError propagates."""
        fails, first_fail, next_start, told = 0, None, 0.0, False
        try:
            while True:
                t0 = time.monotonic()
                try:
                    if not self._started:
                        if t0 >= next_start:
                            self.start()
                    if self._started:
                        self.step()
                        if fails and told:  # the operator got an ERROR for this failure: tell the recovery too
                            self._alert(f"recovered after {fails} failed loops", key="loop-recovered", every=1800)
                        elif fails:
                            log.info("recovered after %d failed loops", fails)
                        fails, first_fail, told = 0, None, False
                except FatalError:
                    raise
                except Exception as exc:  # noqa: BLE001 - keep running, but tell the operator
                    fails += 1
                    first_fail = first_fail or t0
                    if fails <= 3 or fails % 120 == 0:  # full traceback now and then, one line a minute
                        log.exception("loop failed (%d in a row): %s", fails, exc)
                    elif fails % 12 == 0:
                        log.error("loop failed (%d in a row): %s", fails, exc)
                    else:
                        log.debug("loop failed (%d in a row): %s", fails, exc)
                    if self._alert(f"ERROR: {exc}", key="loop-error", every=1800, level=logging.ERROR):
                        told = True
                    if not self._started:
                        next_start = time.monotonic() + min(60.0, 5.0 * 2 ** min(fails - 1, 4))
                    elif fails % 12 == 0:
                        try:
                            self.broker.reconnect()
                        except Exception as exc2:  # noqa: BLE001
                            log.error("reconnect failed: %s", exc2)
                    if time.monotonic() - first_fail >= MAX_FAIL_SECONDS:
                        self._alert(f"CRITICAL: failing for {MAX_FAIL_SECONDS / 60:.0f} minutes ({exc}) -> exiting "
                                    f"with code {EXIT_RESTART} so that the wrapper restarts the process",
                                    level=logging.CRITICAL)
                        return EXIT_RESTART
                time.sleep(max(0.5, self.cfg.engine.poll_seconds - (time.monotonic() - t0)))
        except KeyboardInterrupt:
            log.info("stopped by user")
            return EXIT_STOP
        finally:
            try:
                self.state.save(self.cfg.engine.state_file)
            except Exception as exc:  # noqa: BLE001
                log.error("state save failed: %s", exc)
            self.notifier.flush(10)
            try:
                self.broker.shutdown()
            except Exception:  # noqa: BLE001
                pass

    def step(self) -> None:
        why = self.broker.health()
        if why:
            self._alert(f"trading paused: {why}", key="health", every=1800)
            return
        if not self._check_account():
            return
        for s in self.symbols():
            try:
                self.broker.tick(s)
            except Exception as exc:  # noqa: BLE001 - handled per symbol below
                log.debug("tick %s failed: %s", s, exc)
        self._retry_disabled()
        utc = self._utcnow()
        self._check_clock(utc)
        now_server = self.broker.server_time()
        if now_server is None:
            return
        now_utc = self.clock.server_to_utc(now_server)
        now_mkt = self.clock.server_to_market(now_server)
        equity = self._scan_cash_flows(self.broker.account_equity())  # BEFORE the guards: a withdrawal is no loss
        self._apply_reset_request(equity)
        st = self.state
        before = (st.day, st.month, st.last_equity, st.peak_equity, st.month_start_equity, st.day_opens,
                  st.day_closes)
        for ev in self.risk.update_equity(equity, now_mkt.date()):
            self._alert(ev)
        if before[0] and st.day != before[0]:
            self._new_day(before, utc)
        if before[1] and st.month != before[1]:
            self._month_end(before[1])
        self._dd_alert(equity)
        positions = self.broker.positions(self.magics)
        self._sync_trades(positions, utc)
        stop_reason = self.risk.flatten_reason or self.risk.hard_stop_active()
        if stop_reason and positions:  # also retries closes that failed on the first attempt
            for p in positions:
                self._close(CloseRequest(p.ticket, p.symbol, stop_reason))
            positions = self.broker.positions(self.magics)

        # ---- Night Reversion: time stops every loop, signals on new M15 bars
        if self.cfg.night_reversion.enabled:
            for p in [p for p in positions if p.strategy == "NR"]:
                if self.nr.time_stop_due(p, now_server):
                    self._close(CloseRequest(p.ticket, p.symbol, "time stop"))
            # same order as the backtester (alphabetical): when several pairs signal on the same bar and a
            # portfolio guard can only admit some of them, live trading must pick the same ones
            for sym in sorted(self.cfg.night_reversion.symbols):
                if sym not in self.disabled:
                    self._isolated(sym, self._nr_symbol, sym, now_server, equity)

        # ---- Euro Session
        if self.cfg.euro_session.enabled and self.cfg.euro_session.symbol not in self.disabled:
            self._isolated("ES", self._es, now_server, now_utc, equity)

        self._heartbeat(now_server, now_mkt, now_utc, equity, positions)
        self.notifier.heartbeat()  # dead-man switch: only healthy loops ping
        self._save_state()

    def _isolated(self, name: str, fn, *args) -> None:
        """Run one symbol's work; a failure is logged (full traceback only now and then), notified after 60 loops
        in a row and then every 6 hours while it lasts (also listed in the daily summary), and never stops the
        other symbols."""
        try:
            fn(*args)
        except Exception as exc:  # noqa: BLE001
            n = self._sym_fail[name] = self._sym_fail.get(name, 0) + 1
            if n <= 3 or n % 720 == 0:  # full traceback now and then, one line a minute
                log.exception("%s failed (%d in a row): %s", name, n, exc)
            elif n % 12 == 0:
                log.error("%s failed (%d in a row): %s", name, n, exc)
            else:
                log.debug("%s failed (%d in a row): %s", name, n, exc)
            if n >= SYMBOL_FAIL_ALERT:
                self._alert(f"{name} failing for {n} loops: {exc} (the other symbols keep trading)",
                            key=f"symfail-{name}", every=6 * 3600)
            return
        if self._sym_fail.pop(name, 0) >= SYMBOL_FAIL_ALERT:
            self.notifier.reset(f"symfail-{name}")  # a new failure is notified again after 60 loops
            self._alert(f"{name} works again")

    def failing(self) -> list[str]:
        """Symbols (and ES) failing for SYMBOL_FAIL_ALERT loops or more in a row."""
        return sorted(n for n, k in self._sym_fail.items() if k >= SYMBOL_FAIL_ALERT)

    def _save_state(self) -> None:
        """Write the state when it changed; equity-only changes at most every STATE_SAVE_SECONDS."""
        d = asdict(self.state)
        d.pop("last_equity", None)
        d.pop("peak_equity", None)
        key = json.dumps(d, sort_keys=True, default=str)
        now = time.monotonic()
        if key != self._saved_key or now - self._last_save >= STATE_SAVE_SECONDS:
            self.state.save(self.cfg.engine.state_file)
            self._saved_key, self._last_save = key, now

    # ------------------------------------------------------------------ account / symbols
    def _check_account(self) -> bool:
        login = self.broker.account_login()
        if not login:
            return True
        st = self.state
        if not st.account_login:
            st.account_login = int(login)
            st.account_server = self.broker.account_server()
            log.info("state %s bound to account %s (%s)", self.cfg.engine.state_file, login, st.account_server)
            return True
        if int(login) != int(st.account_login):
            self._account_problem = (f"terminal is logged into account {login}, but the state belongs to account "
                                     f"{st.account_login}: NOT trading until the terminal is logged back in")
            self._alert(self._account_problem, key="account", every=1800, level=logging.ERROR)
            return False
        if self._account_problem:
            self._account_problem = ""
            self._alert(f"terminal is logged into account {login} again: trading resumes")
        return True

    def _retry_disabled(self) -> None:
        if not self.disabled or time.monotonic() - self._last_sym_retry < SYMBOL_RETRY_SECONDS:
            return
        self._last_sym_retry = time.monotonic()
        for s in list(self.disabled):
            try:
                self.broker.symbol_spec(s)
            except Exception:  # noqa: BLE001
                continue
            del self.disabled[s]
            self._alert(f"symbol {s} is available again: trading it")

    # ------------------------------------------------------------------ server clock
    def _market_open(self, utc: datetime) -> bool:
        m = utc_to_market(utc)
        return m.weekday() < 5 and (m.month, m.day) not in FX_HOLIDAYS

    def _check_clock(self, utc: datetime) -> None:
        """Verify the server clock from fresh ticks and watch the price feed.

        A measurement is valid only when the server time advanced within the last TICK_FRESH seconds AND lies
        within CLOCK_TOL of a whole half-hour offset from real UTC (a stale weekend tick never qualifies).
        auto: a different offset is adopted after CLOCK_CONFIRM identical observations (persisted, notified);
        new entries wait until the clock was verified after start-up. ny7 / fixed: CLOCK_CONFIRM mismatches
        in a row block new entries until the clock matches again."""
        st = self.broker.server_time()
        if self._watch_from_utc is None:
            self._watch_from_utc = utc
        if st is None:
            return
        prev, self._prev_st = self._prev_st, st
        if prev is not None and st > prev:
            self._st_moved_utc = self._watch_from_utc = utc
        stale = utc - self._watch_from_utc
        if stale > timedelta(minutes=self.cfg.monitor.stale_feed_minutes) and self._market_open(utc):
            self._alert(f"no price updates for {stale.total_seconds() / 60:.0f} min on a market day (terminal "
                        f"disconnected? feed stopped?)", key="stale-feed", every=3600)
        if self._st_moved_utc is None or (utc - self._st_moved_utc).total_seconds() > TICK_FRESH:
            return  # no fresh tick: no measurement (weekend, holiday, frozen feed)
        diff = (st - utc).total_seconds()
        nearest = round(diff / 1800.0) * 1800.0
        if abs(diff - nearest) > CLOCK_TOL or abs(nearest) > 14 * 3600:
            self._alert(f"server time {st:%Y-%m-%d %H:%M:%S} vs VPS UTC {utc:%Y-%m-%d %H:%M:%S}: no whole half-hour "
                        f"offset ({diff - nearest:+.0f} s) -> check the Windows time synchronisation",
                        key="clock-drift", every=6 * 3600)
            return
        observed = nearest / 3600.0
        expected = (self.clock.utc_to_server(utc) - utc).total_seconds() / 3600.0
        auto = self.cfg.broker.server_time_mode == "auto"
        if abs(observed - expected) < 0.01:
            self._clock_cand, self._clock_n = None, 0
            if not self.clock_verified:
                self.clock_verified = True
                log.info("server clock verified: %s (offset %+.1f h)", self.clock, observed)
            if self.clock_problem:
                self.clock_problem = ""
                self._alert(f"broker clock matches the configured {self.clock} again ({observed:+.1f} h): new "
                            f"entries allowed")
            if auto and (self.state.clock_mode, self.state.clock_offset) != (self.clock.mode, self.clock.offset):
                self.state.clock_mode, self.state.clock_offset = self.clock.mode, self.clock.offset
            return
        if auto:
            self.clock_verified = False
            self._clock_n = self._clock_n + 1 if self._clock_cand == observed else 1
            self._clock_cand = observed
            if self._clock_n >= CLOCK_CONFIRM:
                old, first = self.clock, not self.state.clock_mode
                mode, off = detect_server_mode(st, utc)
                self.clock = ServerClock(mode, off)
                self.state.clock_mode, self.state.clock_offset = mode, off
                self.clock_verified = True
                self._clock_cand, self._clock_n = None, 0
                self._alert(f"server clock {'detected' if first else 'CHANGED'}: {self.clock} (observed offset "
                            f"{observed:+.1f} h, was {old}; persisted)")
        else:
            self._clock_n += 1
            if self._clock_n >= CLOCK_CONFIRM:
                self.clock_problem = f"broker clock mismatch: observed {observed:+.1f} h, expected {expected:+.1f} h"
                self._alert(f"NEW ENTRIES BLOCKED: {self.clock_problem} (check broker.server_time_mode)",
                            key="clock-mismatch", every=3600, level=logging.ERROR)

    def entry_block(self) -> str:
        """Why new entries are not allowed right now ('' = allowed)."""
        if self.cfg.broker.server_time_mode == "auto" and not self.clock_verified:
            return "server clock not verified yet (waiting for fresh ticks)"
        return self.clock_problem

    # ------------------------------------------------------------------ cash flows
    def _scan_cash_flows(self, equity: float) -> float:
        """Deposits / withdrawals / credit / bonus since the last loop, de-duplicated by deal ticket. On the very
        first scan the existing deals are only marked as seen (they are history, not new flows). Returns the
        equity the guards must use (read again when new flows were found, so that it contains them)."""
        flows = self.broker.cash_flows()
        st = self.state
        seen = {int(t) for t, _ in st.cash_tickets}
        new = sorted((f for f in flows if int(f.ticket) not in seen), key=lambda f: (f.time, f.ticket))
        if new:
            st.cash_tickets = st.cash_tickets + [[int(f.ticket), _epoch(f.time)] for f in new]
        if st.cash_tickets:  # keep 60 days (the broker scan looks back 30)
            ref = max(e for _, e in st.cash_tickets)
            now_st = self.broker.server_time()
            if now_st is not None:
                ref = max(ref, _epoch(now_st))
            kept = [[t, e] for t, e in st.cash_tickets if e >= ref - 60 * 86400]
            if len(kept) != len(st.cash_tickets):
                st.cash_tickets = kept
        if not st.cash_init:
            st.cash_init = True
            if new:
                log.info("cash flows: %d existing balance/credit/bonus deals marked as seen (not applied)", len(new))
            return equity
        if not new:
            return equity
        equity = self.broker.account_equity()  # a flow booked after the first read must be in E
        rest = sum(f.amount for f in new)
        for f in new:  # in time order; E_i = equity without the flows booked after this one
            rest -= f.amount
            msg = self.risk.apply_cash_flow(f.amount, equity - rest)
            self._alert(f"{msg} [deal {f.ticket}{' ' + f.comment if f.comment else ''}]")
        return equity

    def _apply_reset_request(self, equity: float) -> None:
        """`reset-halt` request stored in the state: applied after the cash-flow check of this start, so that a
        deposit made while the engine was stopped has already been applied to the old peak."""
        st = self.state
        if not st.reset_request:
            return
        msg = self.risk.reset_halt(equity if st.reset_request == "rebase" else None)
        st.reset_request = ""
        self._alert(f"RESET HALT (python main.py reset-halt, applied after the cash-flow check): {msg}")
        self.journal.trade({"event": "reset_halt", "time_utc": f"{self._utcnow():%Y-%m-%d %H:%M:%S}",
                            "reason": f"applied by the engine: {msg}"})

    # ------------------------------------------------------------------ monitoring
    def _new_day(self, before: tuple, utc: datetime) -> None:
        """Equity log row and daily summary for the market day that just ended."""
        st = self.state
        prev_day, _, close, peak, month_start, opens, closes = before
        dd = max(0.0, 1 - close / peak) if peak > 0 and close > 0 else 0.0
        mret = close / month_start - 1 if month_start > 0 and close > 0 else float("nan")
        block = self.entry_block()
        self.journal.equity({"market_date": prev_day, "time_utc": f"{utc:%Y-%m-%d %H:%M:%S}", "close_equity": close,
                             "peak_equity": peak, "drawdown": dd, "month_return": mret,
                             "cash_flow_total": st.cash_flow_total, "opens": opens, "closes": closes,
                             "halted": st.halted, "entries_blocked": block,
                             "balance": self._balance if self._balance is not None else ""})
        if self.cfg.monitor.daily_summary:
            parts = [f"DAILY {prev_day}: equity {close:,.2f} {self.broker.account_currency()}", f"DD {dd:.1%}",
                     f"month-to-date {mret:+.2%} (flow-adjusted)", f"trades opened {opens} / closed {closes}"]
            if st.halted:
                parts.append(f"HALTED ({st.halt_reason}) -> python main.py reset-halt")
            if st.month_halted or st.day_halted:
                parts.append(f"stopped: {self.risk.hard_stop_active()}")
            if block:
                parts.append(f"entries blocked: {block}")
            if self.disabled:
                parts.append(f"unavailable symbols: {', '.join(sorted(self.disabled))}")
            if self.failing():
                parts.append("failing: " + ", ".join(f"{n} ({self._sym_fail[n]} loops)" for n in self.failing()))
            if self.notifier.failed_total:
                parts.append(f"notification failures so far: {self.notifier.failed_total}")
            self._alert(" | ".join(parts), level=logging.INFO)
        st.day_opens = st.day_closes = 0

    def _month_end(self, month: str) -> None:
        """R-based edge monitor (notification only): rw = sum(base_risk x R) / sum(base_risk) over the last N
        closed trades. WARN when rw < 0 at a month end, REVIEW at 2 consecutive month ends."""
        st = self.state
        if st.edge_month == month:
            return
        st.edge_month = month
        rw, n = risk_weighted_r(self.journal.closed_trades(), self.cfg.monitor.edge_window_trades)
        if n < EDGE_MIN_TRADES or not math.isfinite(rw):
            log.info("R monitor %s: %d closed trades in the journal (needs %d): not evaluated", month, n,
                     EDGE_MIN_TRADES)
            return
        if rw < 0:
            st.edge_neg_months += 1
            if st.edge_neg_months >= 2:
                self._alert(f"EDGE REVIEW ({month}): risk-weighted mean R of the last {n} trades = {rw:+.3f} < 0 at "
                            f"{st.edge_neg_months} consecutive month ends -> re-validate costs and recent data "
                            f"(notification only, trading continues)")
            else:
                self._alert(f"EDGE WARN ({month}): risk-weighted mean R of the last {n} trades = {rw:+.3f} < 0 "
                            f"(notification only, trading continues)")
        else:
            if st.edge_neg_months:
                self._alert(f"edge monitor ({month}): risk-weighted mean R back to {rw:+.3f}", level=logging.INFO)
            else:
                log.info("R monitor %s: risk-weighted mean R %+.3f over %d trades", month, rw, n)
            st.edge_neg_months = 0

    def _dd_alert(self, equity: float) -> None:
        lvl = self.cfg.monitor.dd_alert
        if lvl <= 0:
            return
        dd = self.risk.drawdown(equity)
        if dd >= lvl and self.state.dd_alert_day != self.state.day:
            self.state.dd_alert_day = self.state.day
            halt = self.cfg.risk.max_drawdown_halt
            self._alert(f"DD ALERT: drawdown {dd:.1%} from the peak {self.state.peak_equity:,.2f} (alert level "
                        f"{lvl:.0%}" + (f", new entries halt at {halt:.0%})" if halt > 0 else ")"))

    def _heartbeat(self, now_server, now_mkt, now_utc, equity, positions) -> None:
        if time.monotonic() - self._last_heartbeat < HEARTBEAT_SECONDS:
            return
        self._last_heartbeat = time.monotonic()
        mem = process_memory_mb()
        log.info("heartbeat: server=%s market=%s utc=%s equity=%.2f DD=%.1f%% open=%d halted=%s clock=%s%s "
                 "entries=%s unavailable=%s failing=%s mem=%s notify_failures=%d | GreatestTrader=%s %s", now_server,
                 now_mkt, now_utc, equity, self.risk.drawdown(equity) * 100, len(positions), self.state.halted,
                 self.clock, "" if self.clock_verified else " (unverified)", self.entry_block() or "allowed",
                 sorted(self.disabled) or "-", self.failing() or "-", f"{mem:.0f}MB" if mem is not None else "n/a",
                 self.notifier.failed_total, __version__, format_versions({**versions(), **self.broker.versions()}))

    # ------------------------------------------------------------------ trade journal
    def _trade_info(self, p: PositionView) -> dict:
        """Journal data of a position the engine did not open in this process (older version, lost reply)."""
        risk = 0.0
        if p.sl and ((p.direction > 0 and p.sl < p.open_price) or (p.direction < 0 and p.sl > p.open_price)):
            try:
                risk = self.broker.loss_per_lot(p.symbol, p.direction, p.open_price, p.sl) * p.volume
            except Exception:  # noqa: BLE001
                risk = 0.0
        return {"strategy": p.strategy, "symbol": p.symbol, "direction": p.direction, "lots": p.volume,
                "fill": p.open_price, "sl": p.sl or 0.0, "risk_amount": risk,
                "base_risk": self._base_risk(p.strategy, p.symbol), "open_server": f"{p.open_time}", "adopted": True}

    def _sync_trades(self, positions: list[PositionView], utc: datetime) -> None:
        """Journal every position that closed since the last loop (P/L incl. commission and swap from the deal
        history, R = P/L / risk amount)."""
        known = self.state.open_trades
        live = {str(p.ticket): p for p in positions}
        for t, p in live.items():
            if t not in known:
                known[t] = self._trade_info(p)
        for t in [t for t in known if t not in live]:
            info = known[t]
            res = self.broker.closed_trade(int(t))
            if res is None:
                info["lookups"] = int(info.get("lookups", 0)) + 1
                if info["lookups"] < CLOSED_LOOKUP_TRIES:
                    continue
                log.warning("position %s closed but no deal history after %d tries: journaled without P/L", t,
                            CLOSED_LOOKUP_TRIES)
            engine_reason = self._closing.pop(int(t), "")
            reason = res.reason if res is not None else "unknown"
            if engine_reason and reason not in ("SL", "TP", "stop out"):
                reason = engine_reason
            risk = float(info.get("risk_amount") or 0.0)
            pnl = res.pnl if res is not None else float("nan")
            r_mult = pnl / risk if res is not None and risk > 0 else float("nan")
            self.journal.trade({
                "event": "close", "time_utc": f"{utc:%Y-%m-%d %H:%M:%S}",
                "server_time": f"{res.close_time}" if res is not None else "", "strategy": info.get("strategy"),
                "symbol": info.get("symbol"), "direction": info.get("direction"), "lots": info.get("lots"),
                "fill_price": info.get("fill"), "sl": info.get("sl"), "risk_amount": risk,
                "base_risk": info.get("base_risk"), "ticket": t,
                "close_time": f"{res.close_time}" if res is not None else "",
                "close_price": res.close_price if res is not None else "", "close_reason": reason, "pnl": pnl,
                "R": r_mult, "reason": "adopted position" if info.get("adopted") else ""})
            del known[t]
            self.state.day_closes += 1
            log.info("CLOSED %s %s ticket=%s P/L=%.2f R=%.2f (%s)", info.get("strategy"), info.get("symbol"), t, pnl,
                     r_mult, reason)

    # ------------------------------------------------------------------ NR
    def _gap_free(self, df: pd.DataFrame, n: int, step: timedelta) -> bool:
        """True when the last n bars are contiguous; missing bars are allowed only across the weekend close
        (market Friday 23:00 - Monday 00:15) and on Dec 25 / Jan 1."""
        idx = df.index[-n:]
        if len(idx) < n:
            return False
        for a, b in zip(idx[:-1], idx[1:]):
            if b - a == step:
                continue
            if b <= a or b - a > timedelta(days=4):
                return False
            for t in pd.date_range(a + step, b - step, freq=step):
                m = self.clock.server_to_market(t.to_pydatetime())
                wd = m.weekday()
                closed = (wd == 4 and m.hour >= 23) or wd >= 5 or (wd == 0 and m.hour == 0 and m.minute < 15)
                if not closed and (m.month, m.day) not in FX_HOLIDAYS:
                    return False
        return True

    def _nr_symbol(self, sym: str, now_server: datetime, equity: float) -> None:
        tick = self.broker.tick(sym)
        if tick is None:
            self._alert(f"{sym}: no price from the terminal (symbol hidden from Market Watch?)", key=f"notick-{sym}",
                        every=3600)
            return
        # cheap check: has a new M15 bar closed since we last looked?
        bar_open_now = now_server.replace(minute=(now_server.minute // 15) * 15, second=0, microsecond=0)
        last_closed_open = bar_open_now - M15
        if self.last_bar.get(sym) == last_closed_open:
            return
        df = self.broker.rates(sym, "M15", self.cfg.engine.history_bars)
        if df.empty:
            return
        bar_time = df.index[-1].to_pydatetime()
        if self.last_bar.get(sym) == bar_time:
            return
        c = self.cfg.night_reversion
        if not self._gap_free(df, max(c.bb_period, c.atr_period + 1) + 1, M15):
            if self._gap_logged.get(sym) != bar_time:
                self._gap_logged[sym] = bar_time
                log.warning("%s: history gap in the last bars before %s: indicators not computed, retrying", sym,
                            bar_time)
            return  # last_bar is not set: checked again on the next loop
        first_time = sym not in self.last_bar
        self.last_bar[sym] = bar_time
        prep = self.nr.prepare(df)
        row = prep.iloc[-1]
        bar = {k: float(row[k]) for k in ("close", "mid", "upper", "lower", "atr", "high", "low")}
        spec = self.broker.symbol_spec(sym)

        # manage existing NR position on this symbol (TP -> SMA, time stop)
        positions = self.broker.positions(self.magics)
        for p in [p for p in positions if p.strategy == "NR" and p.symbol == sym]:
            for act in self.nr.manage_on_bar(p, bar, now_server, tick.bid, tick.ask):
                self._apply(act, p)

        # new entry only on a fresh bar close
        if first_time and now_server - (bar_time + M15) > FRESH_SIGNAL:
            return
        if now_server - (bar_time + M15) > FRESH_SIGNAL:
            log.debug("%s bar %s is stale, skip entry check", sym, bar_time)
            return
        bar_mkt = self.clock.server_to_market(bar_time)
        spread_pips = (tick.ask - tick.bid) / spec.pip
        commission_pips = self._commission_pips(sym, tick)
        req = self.nr.entry_signal(sym, bar, bar_time, bar_mkt, spec.pip, spread_pips, commission_pips)
        if req is not None:
            positions = self.broker.positions(self.magics)
            self._enter(req, positions, equity)

    def _commission_pips(self, sym: str, tick) -> float:
        c = self.cfg.broker.commission_per_lot
        if c <= 0:
            return 0.0
        spec = self.broker.symbol_spec(sym)
        pip_value = self.broker.loss_per_lot(sym, 1, tick.ask, tick.ask - spec.pip)
        return c / pip_value if pip_value > 0 else 0.0

    # ------------------------------------------------------------------ ES
    def _es(self, now_server: datetime, now_utc: datetime, equity: float) -> None:
        c = self.cfg.euro_session
        positions = self.broker.positions(self.magics)
        for p in [p for p in positions if p.strategy == "ES"]:
            open_utc = self.clock.server_to_utc(p.open_time)
            for act in self.es.manage(p, now_utc, open_utc):
                self._apply(act, p)
        today = now_utc.date().isoformat()
        if self.state.last_es_entry_day == today or not self.es.is_entry_time(now_utc):
            return
        if any(p.strategy == "ES" for p in positions):
            return
        block = self.entry_block()
        if block:  # not marked as decided: tried again while the entry window lasts
            self._alert(f"ES entry waiting: {block}", key="es-block", every=900, level=logging.INFO)
            return
        tick = self.broker.tick(c.symbol)
        if tick is None:
            return
        spec = self.broker.symbol_spec(c.symbol)
        h1 = self.broker.rates(c.symbol, "H1", c.atr_period_h1 + 30)
        if not self._gap_free(h1, c.atr_period_h1 + 1, H1):
            log.warning("ES: history gap in the last H1 bars of %s, retrying", c.symbol)
            return
        a = atr(h1["high"], h1["low"], h1["close"], c.atr_period_h1).iloc[-1]
        spread_pips = (tick.ask - tick.bid) / spec.pip
        req = self.es.entry_signal(now_utc, now_server, float(a), spec.pip, spread_pips)
        self.state.last_es_entry_day = today  # one decision per day, even if skipped
        if req is not None:
            self._enter(req, positions, equity)
        else:
            log.info("ES: no entry today (spread %.1fp, ATR %.1fp)", spread_pips, float(a) / spec.pip)

    # ------------------------------------------------------------------ execution
    def _open_risk(self, positions: list[PositionView]) -> list[PositionView]:
        """Attach the remaining risk (loss to stop, account currency) to each position.
        Only needed (and only priced) when the total-risk cap is on. Fail closed (EA: same rule): an own position
        WITHOUT a stop loss, or one that cannot be priced, counts as unlimited risk and fills the cap; a stop at
        or beyond the entry price leaves no remaining risk."""
        if self.cfg.risk.max_total_risk <= 0:
            return positions
        for p in positions:
            p.risk_amount = 0.0
            if not p.sl:
                p.risk_amount = float("inf")
                self._alert(f"{p.strategy} {p.symbol} ticket={p.ticket} has NO stop loss: its open risk counts as "
                            f"unlimited, so the total-risk cap blocks new entries until it has a stop or is closed",
                            key=f"nosl-{p.ticket}", every=6 * 3600)
                continue
            if (p.direction > 0 and p.sl < p.open_price) or (p.direction < 0 and p.sl > p.open_price):
                try:
                    p.risk_amount = self.broker.loss_per_lot(p.symbol, p.direction, p.open_price, p.sl) * p.volume
                except Exception as exc:  # noqa: BLE001 - fail closed: an unpriced position fills the risk cap
                    log.warning("cannot price open risk of %s ticket=%s (%s): treated as unlimited", p.symbol,
                                p.ticket, exc)
                    p.risk_amount = float("inf")
        return positions

    def _enter(self, req: EntryRequest, positions: list[PositionView], equity: float) -> None:
        block = self.entry_block()
        if block:
            log.info("%s %s signal not taken: %s", req.strategy, req.symbol, block)
            return
        spec = self.broker.symbol_spec(req.symbol)
        tick = self.broker.tick(req.symbol)
        if tick is None:
            log.warning("%s %s signal not taken: no price", req.strategy, req.symbol)
            return
        entry = tick.ask if req.direction > 0 else tick.bid
        sl = entry - req.direction * req.sl_distance
        tp = None
        if req.tp_price is not None:
            tp = NightReversion.favorable_tp(req.direction, entry, req.tp_price)
            if tp is None:
                log.info("%s %s: price already at the moving average, skip", req.strategy, req.symbol)
                return
        min_dist = (spec.stops_level_points + 2) * spec.point
        if tp is not None and abs(tp - entry) < min_dist:
            log.info("%s %s: TP too close to price (stops level), skip", req.strategy, req.symbol)
            return
        risk_frac = self.risk.risk_fraction(req.strategy, equity) * self.risk.symbol_mult(req.symbol)
        intended = equity * risk_frac
        loss_lot = self.broker.loss_per_lot(req.symbol, req.direction, entry, sl)
        # capacity caps BEFORE the portfolio guards: the total-risk cap must see the real (capped) size
        risk_amount = self.risk.capped_risk(intended, loss_lot, spec.volume_max)
        capped = risk_amount < intended * (1 - 1e-9)
        ok, why = self.risk.can_open(req.strategy, req.symbol, self._open_risk(positions), equity, req.direction,
                                     risk_amount)
        if not ok:
            log.info("%s %s signal skipped: %s", req.strategy, req.symbol, why)
            return
        lots = self.risk.lot_size(risk_amount, loss_lot, spec.volume_min, spec.volume_max, spec.volume_step)
        side = "BUY" if req.direction > 0 else "SELL"
        real_risk = lots * loss_lot
        desc = (f"{req.strategy} {side} {req.symbol} lots={lots} entry~{entry:.{spec.digits}f} "
                f"SL={sl:.{spec.digits}f} TP={tp if tp is None else round(tp, spec.digits)} "
                f"risk={real_risk / equity if equity > 0 else 0:.2%} ({real_risk:.2f})"
                + (f" [capped from {risk_frac:.2%}]" if capped else "") + f" | {req.reason}")
        if lots <= 0:
            log.info("skip (lot below broker minimum): %s", desc)
            return
        need = self.broker.margin_required(req.symbol, req.direction, lots, entry)
        free = self.broker.free_margin()
        if need > free * MARGIN_USE_MAX:
            log.warning("skip (insufficient free margin: need %.2f, free %.2f): %s", need, free, desc)
            self.notifier.send(f"SKIP (margin) {desc}")
            return
        if capped:
            note = (f"lot cap: intended {intended / loss_lot:.2f} lots, sending {lots:g} ({req.strategy} {req.symbol}, "
                    f"risk {risk_amount:,.2f} instead of {intended:,.2f})")
            log.warning(note)
            if self._lot_cap_day.get(req.symbol) != self.state.day:  # notify at most once a day per symbol
                self._lot_cap_day[req.symbol] = self.state.day
                self.notifier.send(note)
        utc = self._utcnow()
        row = {"event": "open", "time_utc": f"{utc:%Y-%m-%d %H:%M:%S}", "server_time": f"{self.broker.server_time()}",
               "strategy": req.strategy, "symbol": req.symbol, "direction": req.direction, "lots": lots,
               "intended_price": entry, "sl": sl, "tp": tp if tp is not None else "",
               "risk_pct": real_risk / equity if equity > 0 else 0.0, "risk_amount": real_risk,
               "base_risk": self._base_risk(req.strategy, req.symbol), "reason": req.reason}
        if self.cfg.engine.dry_run:
            log.info("[DRY-RUN] would open %s", desc)
            self.notifier.send(f"[DRY-RUN] {desc}")
            self.journal.trade({**row, "ticket": 0, "reason": f"[DRY-RUN] {req.reason}"})
            self.state.day_opens += 1
            return
        magic = self.cfg.night_reversion.magic if req.strategy == "NR" else self.cfg.euro_session.magic
        res = self.broker.open_position(req.symbol, req.direction, lots, sl, tp, magic, f"GT_{req.strategy}")
        if res.ok:
            log.info("OPEN %s ticket=%s fill=%s", desc, res.ticket, res.price)
            self.notifier.send(f"OPEN {desc}")
            self.journal.trade({**row, "fill_price": res.price, "ticket": res.ticket})
            self.state.open_trades[str(res.ticket)] = {
                "strategy": req.strategy, "symbol": req.symbol, "direction": req.direction, "lots": lots,
                "fill": res.price, "sl": sl, "risk_amount": real_risk, "base_risk": row["base_risk"],
                "open_utc": row["time_utc"]}
            self.state.day_opens += 1
        else:
            log.error("order failed: %s -> %s", desc, res.message)
            self.notifier.send(f"ORDER FAILED {desc}: {res.message}")

    def _apply(self, act, pos: PositionView) -> None:
        if isinstance(act, CloseRequest):
            self._close(act)
        elif isinstance(act, ModifyRequest):
            if self.cfg.engine.dry_run:
                log.info("[DRY-RUN] would modify %s TP -> %s", act.symbol, act.tp_price)
                return
            res = self.broker.modify_position(act.ticket, act.symbol, pos.sl, act.tp_price)
            if res.ok:
                log.info("MODIFY %s %s TP -> %s (%s)", pos.strategy, act.symbol, act.tp_price, act.reason)
            elif self._beyond_tp(pos, act):
                # the TP can never be placed: a fresh quote is already past it -> take the profit at market
                self._close(CloseRequest(act.ticket, act.symbol, "price beyond TP (TP not placeable)"))
            else:
                # any other failure (too many requests, no connection, a lost reply, stops / freeze level, ...): the
                # position keeps its SL and the time stop, the TP is sent again on the next bar (EA: same rule)
                log.warning("modify failed %s ticket=%s: %s (SL kept, TP retried next bar)", act.symbol, act.ticket,
                            res.message)

    def _beyond_tp(self, pos: PositionView, act: ModifyRequest) -> bool:
        """True when a fresh quote is already at or beyond the requested TP (bid for a long, ask for a short)."""
        if act.tp_price is None:
            return False
        try:
            t = self.broker.tick(act.symbol)
        except Exception:  # noqa: BLE001 - no quote: nothing proves the TP is unplaceable
            return False
        if t is None:
            return False
        px = t.bid if pos.direction > 0 else t.ask
        return px >= act.tp_price if pos.direction > 0 else px <= act.tp_price

    def _close(self, req: CloseRequest) -> None:
        if self.cfg.engine.dry_run:
            log.info("[DRY-RUN] would close %s (%s)", req.symbol, req.reason)
            return
        try:
            res = self.broker.close_position(req.ticket, req.symbol, f"GT {req.reason}")
        except Exception as exc:  # noqa: BLE001 - one failing close must not stop the others
            log.exception("close %s ticket=%s raised: %s", req.symbol, req.ticket, exc)
            self.notifier.send(f"CLOSE FAILED {req.symbol} ticket={req.ticket}: {exc}", key=f"closefail-{req.ticket}",
                               every=1800)
            return
        if res.ok:
            self._closing[int(req.ticket)] = req.reason
            log.info("CLOSE %s ticket=%s (%s) %s", req.symbol, req.ticket, req.reason, res.message)
            self.notifier.send(f"CLOSE {req.symbol} ({req.reason})")
        else:
            log.error("close failed %s ticket=%s: %s", req.symbol, req.ticket, res.message)
            self.notifier.send(f"CLOSE FAILED {req.symbol} ticket={req.ticket}: {res.message}",
                               key=f"closefail-{req.ticket}", every=1800)
