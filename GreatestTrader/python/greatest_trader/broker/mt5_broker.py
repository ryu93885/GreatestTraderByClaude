"""MetaTrader 5 broker adapter (uses the official `MetaTrader5` Python package, Windows only).

Fail-closed rules for unattended operation:
  * an error is never read as "no positions" / "already closed": positions() raises, close_position() fails;
  * an order whose reply was lost is looked up before it is sent again (no doubled positions);
  * orders are retried only on return codes that prove nothing was executed;
  * a lost IPC link (terminal restarted / updated / crashed) is re-initialised by health() with back-off,
    and every cached value is dropped on reconnect;
  * symbol specs are refreshed every 6 hours (changes are logged), a symbol hidden from Market Watch is
    selected again.
"""
from __future__ import annotations

import logging
import sys
import time
from dataclasses import fields
from datetime import datetime, timedelta, timezone

import pandas as pd

from ..config import Config
from ..strategies.base import PositionView, pip_size
from .base import (CASH_DEAL_TYPES, DEAL_TYPE_CREDIT, Broker, CashFlow, CashScan, ClosedTrade, Health, HistoryCheck,
                   OrderResult, SymbolSpec, Tick)

log = logging.getLogger(__name__)

try:  # pragma: no cover - only available on Windows with MT5 installed
    import MetaTrader5 as mt5
except ImportError:  # pragma: no cover
    mt5 = None

RES_S_OK = 1  # last_error() code of "Success"
RETCODE_OK = {10008, 10009, 10010}  # PLACED, DONE, DONE_PARTIAL
RETCODE_INVALID_FILL = 10030  # unsupported filling mode -> try the next one
RETCODE_RETRY = {10004, 10020, 10021}  # requote / price changed / no prices: proven NOT executed -> send again
SPEC_TTL = 6 * 3600.0  # symbol specs are re-read after this many seconds
CASH_RESCAN = 600.0  # deal history re-scanned at least this often (and whenever balance / credit change)
CASH_BURST = 60.0  # ... and on every call for this long after a balance / credit change
CASH_WINDOW_DAYS = 30
RECONNECT_FIRST_DELAY, RECONNECT_MAX_DELAY = 5.0, 60.0
DEAL_ENTRY_EXIT = (1, 2, 3)  # DEAL_ENTRY_OUT, DEAL_ENTRY_INOUT, DEAL_ENTRY_OUT_BY
DEAL_REASONS = {0: "manual", 1: "manual (mobile)", 2: "manual (web)", 3: "expert", 4: "SL", 5: "TP",
                6: "stop out", 7: "rollover", 8: "variation margin", 9: "split"}
ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2


def _ts(sec: int | float) -> datetime:
    """MT5 returns server time as seconds since epoch -> naive datetime in server time."""
    return datetime.fromtimestamp(int(sec), tz=timezone.utc).replace(tzinfo=None)


class MissingPackageError(RuntimeError):
    """The MetaTrader5 package is not importable: a restart cannot fix it (exit code 10)."""


def _history(deals):
    """history_deals_get result: None with last_error 'Success' is an EMPTY history (no deal in the window, e.g.
    a month without trades), any other None is an error (raised: never read as 'no deals')."""
    if deals is None:
        err = mt5.last_error()
        if err and int(err[0]) == RES_S_OK:
            return ()
        raise RuntimeError(f"history_deals_get failed: {err}")
    return deals


class MT5Broker(Broker):
    def __init__(self, cfg: Config):
        if mt5 is None:
            raise MissingPackageError(
                f"the MetaTrader5 package is not installed in this Python ({sys.executable}). Run "
                f"setup_windows.bat once (it creates the .venv folder with the tested packages; the package exists "
                f"for 64-bit Windows only) and start GreatestTrader with the .bat files, which use .venv")
        self.cfg = cfg
        self._specs: dict[str, SymbolSpec] = {}
        self._spec_t: dict[str, float] = {}
        self._tick_t: dict[str, datetime] = {}  # latest tick time per symbol (server time)
        self._money: tuple[float, float] | None = None
        self._cash: list[CashFlow] = []
        self._newest: int | None = None  # newest deal ticket of any type seen in the history
        self._cash_t = self._cash_burst_until = -1e18
        self._reconnect_delay = RECONNECT_FIRST_DELAY
        self._next_reconnect = 0.0
        self._tf = {"M15": mt5.TIMEFRAME_M15, "H1": mt5.TIMEFRAME_H1, "M1": mt5.TIMEFRAME_M1}

    # ------------------------------------------------------------ session
    def connect(self) -> None:
        a = self.cfg.account
        kwargs = {"timeout": 60000}
        if a.terminal_path:
            kwargs["path"] = a.terminal_path
        if a.login:
            kwargs.update(login=int(a.login), password=a.password or "", server=a.server or "")
        if not mt5.initialize(**kwargs):
            raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
        info = mt5.account_info()
        if info is None:
            raise RuntimeError(f"MT5 account_info failed: {mt5.last_error()}")
        self._clear_cache()
        term = mt5.terminal_info()
        if term is not None and not term.trade_allowed:
            log.warning("Algo trading is DISABLED in the MT5 terminal (enable the 'Algo Trading' button).")
        log.info("Connected to MT5: login=%s server=%s currency=%s leverage=1:%s hedging=%s", info.login,
                 info.server, info.currency, info.leverage,
                 getattr(info, "margin_mode", ACCOUNT_MARGIN_MODE_RETAIL_HEDGING) == ACCOUNT_MARGIN_MODE_RETAIL_HEDGING)

    def shutdown(self) -> None:
        mt5.shutdown()

    def _clear_cache(self) -> None:
        self._specs.clear()
        self._spec_t.clear()
        self._tick_t.clear()  # the server clock may legitimately go back (new server / offset change)
        self._money, self._cash, self._cash_t, self._cash_burst_until = None, [], -1e18, -1e18

    def reconnect(self) -> None:
        try:
            mt5.shutdown()
        except Exception:  # noqa: BLE001 - the old session may already be dead
            pass
        self._clear_cache()
        self.connect()  # raises while the terminal is not back yet

    def health(self) -> Health:
        """'' when trading decisions are safe, otherwise why not: the terminal is up but not connected to the
        trade server (the whole loop pauses), or only sending orders is off - Algo Trading switched off, the account
        does not allow (expert) trading - which is a Health with orders_only=True (the engine keeps its guards
        running, sends no order). A lost IPC link (terminal restarted by an update, crashed, closed) never comes back
        by itself: it is re-initialised with back-off up to 60 s, and RuntimeError is raised while the terminal
        stays unreachable (the engine counts it as a failure: exit 3 after 30 min)."""
        ti = mt5.terminal_info()
        if ti is None:
            now = time.monotonic()
            if now < self._next_reconnect:
                raise RuntimeError(f"MT5 terminal unreachable ({mt5.last_error()}), reconnecting")
            log.warning("MT5 IPC link lost (%s): re-initialising", mt5.last_error())
            try:
                self.reconnect()
            except Exception as exc:  # noqa: BLE001
                self._next_reconnect = now + self._reconnect_delay
                self._reconnect_delay = min(RECONNECT_MAX_DELAY, self._reconnect_delay * 2)
                raise RuntimeError(f"MT5 reconnect failed: {exc}") from None
            self._reconnect_delay, self._next_reconnect = RECONNECT_FIRST_DELAY, 0.0
            ti = mt5.terminal_info()
            if ti is None:
                raise RuntimeError(f"MT5 terminal_info unavailable after reconnect ({mt5.last_error()})")
            log.warning("MT5 connection re-established")
        if not ti.connected:
            return Health("MT5 terminal is not connected to the trade server")
        if self.cfg.engine.dry_run:
            return Health()
        if not ti.trade_allowed:
            return Health("Algo Trading is switched off in the MT5 terminal", orders_only=True)
        info = mt5.account_info()
        if info is not None and not getattr(info, "trade_allowed", True):
            return Health("trading is not allowed on this account (investor password? account blocked?)",
                          orders_only=True)
        if info is not None and not getattr(info, "trade_expert", True):
            return Health("the account does not allow expert (automated) trading", orders_only=True)
        return Health()

    def versions(self) -> dict[str, str]:
        out = {"MetaTrader5": str(getattr(mt5, "__version__", "?"))}
        ti = mt5.terminal_info()
        if ti is not None:
            out["terminal_build"] = str(getattr(ti, "build", "?"))
        return out

    def _name(self, base: str) -> str:
        return self.cfg.broker_symbol(base)

    # ------------------------------------------------------------ account
    def _account(self):
        info = mt5.account_info()
        if info is None:
            raise RuntimeError(f"account_info failed: {mt5.last_error()}")
        return info

    def account_equity(self) -> float:
        return float(self._account().equity)

    def account_currency(self) -> str:
        info = mt5.account_info()
        return info.currency if info else "?"

    def account_login(self) -> int:
        return int(self._account().login)

    def account_server(self) -> str:
        return str(self._account().server)

    def is_hedging(self) -> bool:
        mode = getattr(self._account(), "margin_mode", ACCOUNT_MARGIN_MODE_RETAIL_HEDGING)
        return int(mode) == ACCOUNT_MARGIN_MODE_RETAIL_HEDGING

    def free_margin(self) -> float:
        info = mt5.account_info()
        return float(info.margin_free) if info else 0.0

    def margin_required(self, base: str, direction: int, volume: float, price: float) -> float:
        otype = mt5.ORDER_TYPE_BUY if direction > 0 else mt5.ORDER_TYPE_SELL
        m = mt5.order_calc_margin(otype, self._name(base), float(volume), float(price))
        return float(m) if m is not None else float("inf")

    def cash_flows(self, force: bool = False) -> list[CashFlow]:
        """BALANCE / CREDIT / BONUS deals of the last 30 days. The history is re-read when balance or credit
        changed (and on every call during the following minute, in case the deal reaches the history a moment
        after the balance), when `force` is set, otherwise every 10 minutes. The newest deal ticket of ANY type
        in the window is kept for the engine's cash-flow lag guard."""
        info = self._account()
        money = (round(float(info.balance), 2), round(float(getattr(info, "credit", 0.0)), 2))
        now = time.monotonic()
        if money != self._money and self._money is not None:
            self._cash_burst_until = now + CASH_BURST
        if not force and money == self._money and now - self._cash_t < CASH_RESCAN and now >= self._cash_burst_until:
            return list(self._cash)
        end = datetime.now() + timedelta(days=2)  # generous window: history times are server-time based
        deals = _history(mt5.history_deals_get(end - timedelta(days=CASH_WINDOW_DAYS + 2), end))
        self._cash = [CashFlow(int(d.ticket), _ts(d.time), float(d.profit), int(d.type), str(getattr(d, "comment", "")))
                      for d in deals if int(d.type) in CASH_DEAL_TYPES]
        newest = max((int(d.ticket) for d in deals), default=None)
        if newest is not None and (self._newest is None or newest > self._newest):
            self._newest = newest
        self._money, self._cash_t = money, now
        return list(self._cash)

    def cash_scan(self, fresh: bool = False) -> CashScan:
        """The deal scan first, then ONE account read (equity, balance, credit together): k = E / (E - F) uses an
        equity that contains every flow the scan found, and a deal booked between the history read and the account
        read shows as money != scan_money (the engine then decides on the next loop)."""
        flows = self.cash_flows(force=fresh)
        scan_money = round(sum(self._money), 2) if self._money is not None else None
        info = self._account()
        balance = float(info.balance)
        money = round(balance + float(getattr(info, "credit", 0.0)), 2)
        return CashScan(flows, float(info.equity), self._newest, money, scan_money, balance)

    def history_check(self) -> HistoryCheck:
        """The whole deal history (first run): complete when the deals add up to the balance - every deal type but
        CREDIT: profit + commission + swap + fee - and the CREDIT deals to the credit, within max(1, 0.1%)."""
        deals = _history(mt5.history_deals_get(datetime(2000, 1, 1), datetime.now() + timedelta(days=2)))
        info = self._account()
        sum_bal = sum_cred = 0.0
        for d in deals:
            amount = float(d.profit) + float(getattr(d, "commission", 0.0)) + float(getattr(d, "swap", 0.0)) \
                + float(getattr(d, "fee", 0.0))
            if int(d.type) == DEAL_TYPE_CREDIT:
                sum_cred += amount
            else:
                sum_bal += amount
        bal, cred = float(info.balance), float(getattr(info, "credit", 0.0))
        ok = abs(sum_bal - bal) <= max(1.0, 0.001 * abs(bal)) and abs(sum_cred - cred) <= max(1.0, 0.001 * abs(cred))
        flows = [CashFlow(int(d.ticket), _ts(d.time), float(d.profit), int(d.type), str(getattr(d, "comment", "")))
                 for d in deals if int(d.type) in CASH_DEAL_TYPES]
        return HistoryCheck(flows, ok, f"{len(deals)} deals sum to {sum_bal:,.2f} (balance {bal:,.2f}), credit deals "
                                       f"to {sum_cred:,.2f} (credit {cred:,.2f})")

    def closed_trade(self, ticket: int) -> ClosedTrade | None:
        deals = mt5.history_deals_get(position=int(ticket))
        if not deals:  # None (error) or () (history not synchronised yet): the caller retries
            return None
        exits = [d for d in deals if int(getattr(d, "entry", 0)) in DEAL_ENTRY_EXIT]
        if not exits:
            return None
        pnl = sum(float(d.profit) + float(getattr(d, "commission", 0.0)) + float(getattr(d, "swap", 0.0))
                  + float(getattr(d, "fee", 0.0)) for d in deals)
        last = max(exits, key=lambda d: (int(getattr(d, "time_msc", 0)), int(d.time), int(d.ticket)))
        reason = int(getattr(last, "reason", -1))
        return ClosedTrade(int(ticket), _ts(last.time), float(last.price), pnl, DEAL_REASONS.get(reason, str(reason)))

    def history_deals(self, start: datetime, end: datetime) -> pd.DataFrame:
        """Every deal between start and end (report command)."""
        deals = _history(mt5.history_deals_get(start, end))
        cols =["ticket", "time", "type", "entry", "magic", "position_id", "symbol", "volume", "price", "profit",
                "commission", "swap", "fee", "reason", "comment"]
        df = pd.DataFrame([{c: getattr(d, c, 0) for c in cols} for d in deals], columns=cols)
        df["time"] = pd.to_datetime(df["time"], unit="s")
        return df

    # ------------------------------------------------------------ market data
    def _fetch_spec(self, base: str) -> SymbolSpec:
        name = self._name(base)
        if not mt5.symbol_select(name, True):
            raise RuntimeError(f"symbol {name} not available at this broker ({mt5.last_error()}). "
                               f"Check broker.symbol_suffix / broker.symbol_map in config.yaml")
        si = mt5.symbol_info(name)
        if si is None:
            raise RuntimeError(f"symbol_info({name}) failed: {mt5.last_error()}")
        return SymbolSpec(base=base, name=name, digits=si.digits, point=si.point,
                          pip=pip_size(base, si.digits, si.point), volume_min=si.volume_min,
                          volume_max=si.volume_max, volume_step=si.volume_step,
                          stops_level_points=si.trade_stops_level)

    def symbol_spec(self, base: str) -> SymbolSpec:
        now = time.monotonic()
        old = self._specs.get(base)
        if old is not None and now - self._spec_t.get(base, now) < SPEC_TTL:
            return old
        try:
            spec = self._fetch_spec(base)
        except RuntimeError as exc:
            if old is None:
                raise
            log.warning("symbol spec refresh of %s failed (%s): keeping the cached spec", base, exc)
            self._spec_t[base] = now - SPEC_TTL + 600  # try again in 10 minutes
            return old
        if old is not None and spec != old:
            diff = ", ".join(f"{f.name} {getattr(old, f.name)} -> {getattr(spec, f.name)}" for f in fields(spec)
                             if getattr(old, f.name) != getattr(spec, f.name))
            log.warning("symbol spec of %s changed at the broker: %s", base, diff)
        self._specs[base], self._spec_t[base] = spec, now
        return spec

    def tick(self, base: str) -> Tick | None:
        name = self._name(base)
        t = mt5.symbol_info_tick(name)
        if t is None:  # e.g. the symbol was hidden from Market Watch: select it again
            mt5.symbol_select(name, True)
            t = mt5.symbol_info_tick(name)
        if t is None or t.bid <= 0 or t.ask <= 0:
            return None
        tt = _ts(t.time)
        self._tick_t[base] = tt
        return Tick(time=tt, bid=float(t.bid), ask=float(t.ask))

    def server_time(self) -> datetime | None:
        """Newest of the latest ticks of the polled symbols (not a running maximum: after a backwards change
        of the broker's clock the server time follows within a few ticks instead of freezing)."""
        return max(self._tick_t.values()) if self._tick_t else None

    def rates(self, base: str, timeframe: str, count: int) -> pd.DataFrame:
        arr = mt5.copy_rates_from_pos(self._name(base), self._tf[timeframe], 1, count)  # pos 1 = last CLOSED bar
        if arr is None or len(arr) == 0:
            raise RuntimeError(f"copy_rates {base} {timeframe} failed: {mt5.last_error()}")
        df = pd.DataFrame(arr)
        df.index = pd.to_datetime(df["time"], unit="s")
        spec = self.symbol_spec(base)
        df["spread_pips"] = df["spread"] * spec.point / spec.pip
        return df[["open", "high", "low", "close", "spread_pips"]]

    # ------------------------------------------------------------ positions
    def positions(self, magics: dict[int, str]) -> list[PositionView]:
        rows = mt5.positions_get()
        if rows is None:  # an ERROR, not "flat": fail closed (no duplicate entries, no false 'closed')
            raise RuntimeError(f"positions_get failed: {mt5.last_error()}")
        inv = {}
        for b in set(self.cfg.night_reversion.symbols) | {self.cfg.euro_session.symbol}:
            inv[self._name(b)] = b
        out = []
        for p in rows:
            if p.magic not in magics:
                continue
            base = inv.get(p.symbol, p.symbol[:6])
            out.append(PositionView(
                ticket=int(p.ticket), strategy=magics[p.magic], symbol=base,
                direction=1 if p.type == mt5.POSITION_TYPE_BUY else -1, volume=float(p.volume),
                open_price=float(p.price_open), open_time=_ts(p.time),
                sl=float(p.sl) or None, tp=float(p.tp) or None))
        return out

    def loss_per_lot(self, base: str, direction: int, entry: float, stop: float) -> float:
        otype = mt5.ORDER_TYPE_BUY if direction > 0 else mt5.ORDER_TYPE_SELL
        prof = mt5.order_calc_profit(otype, self._name(base), 1.0, entry, stop)
        if prof is None:
            raise RuntimeError(f"order_calc_profit failed: {mt5.last_error()}")
        return abs(float(prof))

    # ------------------------------------------------------------ orders
    def _fillings(self, name: str) -> list[int]:
        si = mt5.symbol_info(name)
        fm = si.filling_mode if si else 0
        order = []
        if fm & 1:
            order.append(mt5.ORDER_FILLING_FOK)
        if fm & 2:
            order.append(mt5.ORDER_FILLING_IOC)
        order.append(mt5.ORDER_FILLING_RETURN)
        for f in (mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_IOC):
            if f not in order:
                order.append(f)
        return order

    def _send(self, req: dict, executed) -> OrderResult:
        """Send a market request. It is sent again only when nothing was executed for sure: requote / price
        changed / no prices (again), unsupported filling (next filling mode), or a lost reply after
        `executed()` proved that the request had no effect. `executed()` returns (known, result): known=False
        when that cannot be checked (then the request is NOT repeated), result = the OrderResult when the
        request was executed after all, None when it was not."""
        last, lost = None, ""
        for filling in self._fillings(req["symbol"]):
            req["type_filling"] = filling
            attempt = 0
            while attempt < 3:
                attempt += 1
                t = mt5.symbol_info_tick(req["symbol"])
                if t is not None:
                    req["price"] = t.ask if req["type"] == mt5.ORDER_TYPE_BUY else t.bid
                res = mt5.order_send(req)
                last = res
                if res is None:
                    lost = str(mt5.last_error())
                    time.sleep(1.0)
                    known, result = executed()
                    if not known:
                        return OrderResult(False, message=f"no reply ({lost}) and the result cannot be checked: "
                                                          f"not sent again, check the account")
                    if result is not None:
                        return result
                    log.warning("order reply lost (%s), nothing was executed: sending again", lost)
                    continue
                if res.retcode in RETCODE_OK:
                    return OrderResult(True, int(res.order or res.deal), float(res.price or req.get("price", 0)),
                                       res.comment)
                if res.retcode == RETCODE_INVALID_FILL:
                    break  # try the next filling mode
                if res.retcode in RETCODE_RETRY:
                    time.sleep(0.3)
                    continue
                return OrderResult(False, message=f"retcode={res.retcode} {res.comment}")
        if last is None:
            return OrderResult(False, message=f"no reply from the terminal ({lost})")
        return OrderResult(False, message=f"retcode={last.retcode} {last.comment}")

    def _ours(self, name: str, magic: int) -> dict[int, object] | None:
        rows = mt5.positions_get(symbol=name)
        if rows is None:
            return None
        return {int(p.ticket): p for p in rows if p.magic == magic}

    def open_position(self, base, direction, volume, sl, tp, magic, comment) -> OrderResult:
        spec = self.symbol_spec(base)
        before = self._ours(spec.name, magic)
        if before is None:
            return OrderResult(False, message=f"positions_get failed ({mt5.last_error()}): order not sent")
        req = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": spec.name, "volume": float(volume),
            "type": mt5.ORDER_TYPE_BUY if direction > 0 else mt5.ORDER_TYPE_SELL, "price": 0.0,
            "sl": round(sl, spec.digits), "tp": round(tp, spec.digits) if tp else 0.0,
            "deviation": self.cfg.broker.deviation_points, "magic": int(magic), "comment": comment[:31],
            "type_time": mt5.ORDER_TIME_GTC,
        }

        def executed():
            now = self._ours(spec.name, magic)
            if now is None:
                return False, None
            new = [p for t, p in now.items() if t not in before]
            if new:
                return True, OrderResult(True, int(new[0].ticket), float(new[0].price_open), "filled (reply was lost)")
            return True, None

        res = self._send(req, executed)
        if res.ok and "reply was lost" not in res.message:
            # resolve the position ticket and the real fill price
            for p in (self._ours(spec.name, magic) or {}).values():
                if p.ticket == res.ticket or getattr(p, "identifier", None) == res.ticket:
                    return OrderResult(True, int(p.ticket), float(p.price_open), res.message)
        return res

    def modify_position(self, ticket, base, sl, tp) -> OrderResult:
        spec = self.symbol_spec(base)
        req = {"action": mt5.TRADE_ACTION_SLTP, "position": int(ticket), "symbol": spec.name,
               "sl": round(sl, spec.digits) if sl else 0.0, "tp": round(tp, spec.digits) if tp else 0.0}
        res = mt5.order_send(req)
        if res is not None and res.retcode in RETCODE_OK:
            return OrderResult(True, int(ticket))
        if res is None:
            return OrderResult(False, message=f"no reply ({mt5.last_error()})")
        return OrderResult(False, message=f"retcode={res.retcode} {res.comment}")

    def close_position(self, ticket, base, comment="") -> OrderResult:
        spec = self.symbol_spec(base)
        pos = mt5.positions_get(ticket=int(ticket))
        if pos is None:  # an error is not "already closed"
            return OrderResult(False, int(ticket), message=f"positions_get failed ({mt5.last_error()}): "
                                                           f"close not confirmed")
        if len(pos) == 0:
            return OrderResult(True, int(ticket), message="already closed")
        p = pos[0]
        req = {
            "action": mt5.TRADE_ACTION_DEAL, "position": int(ticket), "symbol": spec.name,
            "volume": float(p.volume),
            "type": mt5.ORDER_TYPE_SELL if p.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY,
            "price": 0.0, "deviation": self.cfg.broker.deviation_points, "magic": int(p.magic),
            "comment": (comment or "GT close")[:31], "type_time": mt5.ORDER_TIME_GTC,
        }

        def executed():
            now = mt5.positions_get(ticket=int(ticket))
            if now is None:
                return False, None
            if len(now) == 0:
                return True, OrderResult(True, int(ticket), message="closed (reply was lost)")
            return True, None

        return self._send(req, executed)
