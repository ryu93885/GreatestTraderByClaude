"""Live trading engine (production)."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone


from .broker.base import Broker
from .config import Config
from .indicators import atr
from .notify import Notifier
from .risk import RiskManager, RiskState
from .strategies import CloseRequest, EntryRequest, EuroSession, ModifyRequest, NightReversion, PositionView
from .timeutil import ServerClock, detect_server_mode

log = logging.getLogger(__name__)
M15 = timedelta(minutes=15)
FRESH_SIGNAL = timedelta(seconds=150)  # act on a closed bar only if it closed within this time
MARGIN_USE_MAX = 0.8  # a new position may use at most 80% of the current free margin


class LiveEngine:
    def __init__(self, cfg: Config, broker: Broker, notifier: Notifier | None = None):
        self.cfg = cfg
        self.broker = broker
        self.notifier = notifier or Notifier(cfg.notify)
        self.nr = NightReversion(cfg.night_reversion)
        self.es = EuroSession(cfg.euro_session)
        self.state = RiskState.load(cfg.engine.state_file)
        self.risk = RiskManager(cfg.risk, self.state)
        self.clock: ServerClock | None = None
        self.magics = {cfg.night_reversion.magic: "NR", cfg.euro_session.magic: "ES"}
        self.last_bar: dict[str, datetime] = {}
        self._last_error_note = 0.0
        self._last_heartbeat = 0.0

    # ------------------------------------------------------------------ setup
    def symbols(self) -> list[str]:
        syms = []
        if self.cfg.night_reversion.enabled:
            syms += list(self.cfg.night_reversion.symbols)
        if self.cfg.euro_session.enabled and self.cfg.euro_session.symbol not in syms:
            syms.append(self.cfg.euro_session.symbol)
        return syms

    def start(self) -> None:
        self.broker.connect()
        for s in self.symbols():
            spec = self.broker.symbol_spec(s)
            log.info("symbol %-7s -> %-12s digits=%d pip=%g min_lot=%g step=%g", s, spec.name, spec.digits,
                     spec.pip, spec.volume_min, spec.volume_step)
            self.broker.tick(s)
        self._init_clock()
        eq = self.broker.account_equity()
        mode = "DRY-RUN (no orders)" if self.cfg.engine.dry_run else "LIVE"
        r = self.cfg.risk
        mults = [self.risk.symbol_mult(s) for s in self.cfg.night_reversion.symbols] or [1.0]
        nr_lo, nr_hi = r.base_risk("nr") * min(mults), r.base_risk("nr") * max(mults)
        es_risk = r.base_risk("es") * self.risk.symbol_mult(self.cfg.euro_session.symbol)
        msg = (f"started {mode}: equity={eq:.2f} {self.broker.account_currency()} preset={self.cfg.preset_label()} "
               f"profile={r.profile} NR={nr_lo:.2%}" + (f"-{nr_hi:.2%}" if nr_hi > nr_lo else "")
               + f" ES={es_risk:.2%} (before DD scaling) clock={self.clock}")
        log.info(msg)
        self.notifier.send(msg)

    def _init_clock(self) -> None:
        b = self.cfg.broker
        if b.server_time_mode == "auto":
            st = self.broker.server_time()
            now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
            diff = (st - now_utc).total_seconds() if st is not None else 1e9
            nearest = round(diff / 1800.0) * 1800.0
            # the last tick must be fresh (within 2 minutes of a half-hour offset) to trust it
            if st is None or abs(diff - nearest) > 120 or abs(nearest) > 14 * 3600:
                log.warning("cannot auto-detect server time (market closed / stale ticks) -> "
                            "assuming ny7 (GMT+2 winter / GMT+3 summer). Set broker.server_time_mode "
                            "explicitly if your broker differs.")
                self.clock = ServerClock("ny7")
                return
            mode, off = detect_server_mode(st, now_utc)
            self.clock = ServerClock(mode, off)
            log.info("server time auto-detected: %s (observed offset %+.1fh)", mode, off)
        else:
            self.clock = ServerClock(b.server_time_mode, b.server_gmt_offset)

    # ------------------------------------------------------------------ loop
    def run_forever(self) -> None:
        self.start()
        try:
            while True:
                t0 = time.time()
                try:
                    self.step()
                except Exception as e:  # keep running, but tell the operator
                    log.exception("step failed: %s", e)
                    if time.time() - self._last_error_note > 600:
                        self.notifier.send(f"ERROR: {e}")
                        self._last_error_note = time.time()
                time.sleep(max(0.5, self.cfg.engine.poll_seconds - (time.time() - t0)))
        except KeyboardInterrupt:
            log.info("stopped by user")
        finally:
            self.state.save(self.cfg.engine.state_file)
            self.broker.shutdown()

    def step(self) -> None:
        for s in self.symbols():
            self.broker.tick(s)
        now_server = self.broker.server_time()
        if now_server is None:
            return
        now_utc = self.clock.server_to_utc(now_server)
        now_mkt = self.clock.server_to_market(now_server)
        equity = self.broker.account_equity()
        for ev in self.risk.update_equity(equity, now_mkt.date()):
            log.warning(ev)
            self.notifier.send(ev)
        positions = self.broker.positions(self.magics)
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
                self._nr_symbol(sym, now_server, equity)

        # ---- Euro Session
        if self.cfg.euro_session.enabled:
            self._es(now_server, now_utc, equity)

        if time.time() - self._last_heartbeat > 3600:
            self._last_heartbeat = time.time()
            dd = self.risk.drawdown(equity)
            log.info("heartbeat: server=%s market=%s utc=%s equity=%.2f DD=%.1f%% open=%d halted=%s",
                     now_server, now_mkt, now_utc, equity, dd * 100, len(positions), self.state.halted)
        self.state.save(self.cfg.engine.state_file)

    # ------------------------------------------------------------------ NR
    def _nr_symbol(self, sym: str, now_server: datetime, equity: float) -> None:
        tick = self.broker.tick(sym)
        if tick is None:
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
        tick = self.broker.tick(c.symbol)
        if tick is None:
            return
        spec = self.broker.symbol_spec(c.symbol)
        h1 = self.broker.rates(c.symbol, "H1", c.atr_period_h1 + 30)
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
        Only needed (and only priced) when the total-risk cap is on."""
        if self.cfg.risk.max_total_risk <= 0:
            return positions
        for p in positions:
            p.risk_amount = 0.0
            if p.sl and ((p.direction > 0 and p.sl < p.open_price) or (p.direction < 0 and p.sl > p.open_price)):
                try:
                    p.risk_amount = self.broker.loss_per_lot(p.symbol, p.direction, p.open_price, p.sl) * p.volume
                except Exception as exc:  # noqa: BLE001 - fail closed: an unpriced position fills the risk cap
                    log.warning("cannot price open risk of %s ticket=%s (%s): treated as unlimited", p.symbol,
                                p.ticket, exc)
                    p.risk_amount = float("inf")
        return positions

    def _enter(self, req: EntryRequest, positions: list[PositionView], equity: float) -> None:
        spec = self.broker.symbol_spec(req.symbol)
        tick = self.broker.tick(req.symbol)
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
        risk_amount = equity * risk_frac
        ok, why = self.risk.can_open(req.strategy, req.symbol, self._open_risk(positions), equity, req.direction,
                                     risk_amount)
        if not ok:
            log.info("%s %s signal skipped: %s", req.strategy, req.symbol, why)
            return
        loss_lot = self.broker.loss_per_lot(req.symbol, req.direction, entry, sl)
        lots = self.risk.lot_size(risk_amount, loss_lot, spec.volume_min, spec.volume_max, spec.volume_step)
        side = "BUY" if req.direction > 0 else "SELL"
        desc = (f"{req.strategy} {side} {req.symbol} lots={lots} entry~{entry:.{spec.digits}f} "
                f"SL={sl:.{spec.digits}f} TP={tp if tp is None else round(tp, spec.digits)} "
                f"risk={risk_frac:.2%} ({risk_amount:.2f}) | {req.reason}")
        if lots <= 0:
            log.info("skip (lot below broker minimum): %s", desc)
            return
        need = self.broker.margin_required(req.symbol, req.direction, lots, entry)
        free = self.broker.free_margin()
        if need > free * MARGIN_USE_MAX:
            log.warning("skip (insufficient free margin: need %.2f, free %.2f): %s", need, free, desc)
            self.notifier.send(f"SKIP (margin) {desc}")
            return
        if self.cfg.engine.dry_run:
            log.info("[DRY-RUN] would open %s", desc)
            self.notifier.send(f"[DRY-RUN] {desc}")
            return
        magic = self.cfg.night_reversion.magic if req.strategy == "NR" else self.cfg.euro_session.magic
        res = self.broker.open_position(req.symbol, req.direction, lots, sl, tp, magic, f"GT_{req.strategy}")
        if res.ok:
            log.info("OPEN %s ticket=%s fill=%s", desc, res.ticket, res.price)
            self.notifier.send(f"OPEN {desc}")
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
            else:
                log.warning("modify failed %s: %s", act.symbol, res.message)
                # if the TP cannot be placed because price is already past it, close instead
                if act.tp_price is not None:
                    self._close(CloseRequest(act.ticket, act.symbol, "TP not placeable, close"))

    def _close(self, req: CloseRequest) -> None:
        if self.cfg.engine.dry_run:
            log.info("[DRY-RUN] would close %s (%s)", req.symbol, req.reason)
            return
        res = self.broker.close_position(req.ticket, req.symbol, f"GT {req.reason}")
        if res.ok:
            log.info("CLOSE %s ticket=%s (%s)", req.symbol, req.ticket, req.reason)
            self.notifier.send(f"CLOSE {req.symbol} ({req.reason})")
        else:
            log.error("close failed %s ticket=%s: %s", req.symbol, req.ticket, res.message)
            self.notifier.send(f"CLOSE FAILED {req.symbol} ticket={req.ticket}: {res.message}")
