"""MetaTrader 5 broker adapter (uses the official `MetaTrader5` Python package, Windows only)."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import pandas as pd

from ..config import Config
from ..strategies.base import PositionView, pip_size
from .base import Broker, OrderResult, SymbolSpec, Tick

log = logging.getLogger(__name__)

try:  # pragma: no cover - only available on Windows with MT5 installed
    import MetaTrader5 as mt5
except ImportError:  # pragma: no cover
    mt5 = None

RETCODE_OK = {10008, 10009, 10010}  # PLACED, DONE, DONE_PARTIAL
RETCODE_INVALID_FILL = 10030


def _ts(sec: int | float) -> datetime:
    """MT5 returns server time as seconds since epoch -> naive datetime in server time."""
    return datetime.fromtimestamp(int(sec), tz=timezone.utc).replace(tzinfo=None)


class MT5Broker(Broker):
    def __init__(self, cfg: Config):
        if mt5 is None:
            raise RuntimeError(
                "MetaTrader5 package is not installed. Run `pip install MetaTrader5` on Windows "
                "with the MT5 terminal installed.")
        self.cfg = cfg
        self._specs: dict[str, SymbolSpec] = {}
        self._last_server_time: datetime | None = None
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
        term = mt5.terminal_info()
        if term is not None and not term.trade_allowed:
            log.warning("Algo trading is DISABLED in the MT5 terminal (enable the 'Algo Trading' button).")
        log.info("Connected to MT5: login=%s server=%s currency=%s leverage=1:%s",
                 info.login, info.server, info.currency, info.leverage)

    def shutdown(self) -> None:
        mt5.shutdown()

    def _name(self, base: str) -> str:
        return self.cfg.broker_symbol(base)

    # ------------------------------------------------------------ account
    def account_equity(self) -> float:
        info = mt5.account_info()
        if info is None:
            raise RuntimeError(f"account_info failed: {mt5.last_error()}")
        return float(info.equity)

    def account_currency(self) -> str:
        info = mt5.account_info()
        return info.currency if info else "?"

    def free_margin(self) -> float:
        info = mt5.account_info()
        return float(info.margin_free) if info else 0.0

    def margin_required(self, base: str, direction: int, volume: float, price: float) -> float:
        otype = mt5.ORDER_TYPE_BUY if direction > 0 else mt5.ORDER_TYPE_SELL
        m = mt5.order_calc_margin(otype, self._name(base), float(volume), float(price))
        return float(m) if m is not None else float("inf")

    # ------------------------------------------------------------ market data
    def symbol_spec(self, base: str) -> SymbolSpec:
        if base in self._specs:
            return self._specs[base]
        name = self._name(base)
        if not mt5.symbol_select(name, True):
            raise RuntimeError(f"symbol {name} not available at this broker ({mt5.last_error()}). "
                               f"Check broker.symbol_suffix / broker.symbol_map in config.yaml")
        si = mt5.symbol_info(name)
        if si is None:
            raise RuntimeError(f"symbol_info({name}) failed: {mt5.last_error()}")
        spec = SymbolSpec(base=base, name=name, digits=si.digits, point=si.point,
                          pip=pip_size(base, si.digits, si.point), volume_min=si.volume_min,
                          volume_max=si.volume_max, volume_step=si.volume_step,
                          stops_level_points=si.trade_stops_level)
        self._specs[base] = spec
        return spec

    def tick(self, base: str) -> Tick | None:
        t = mt5.symbol_info_tick(self._name(base))
        if t is None or t.bid <= 0 or t.ask <= 0:
            return None
        tt = _ts(t.time)
        if self._last_server_time is None or tt > self._last_server_time:
            self._last_server_time = tt
        return Tick(time=tt, bid=float(t.bid), ask=float(t.ask))

    def server_time(self) -> datetime | None:
        return self._last_server_time

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
        if rows is None:
            return []
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

    def _send(self, req: dict) -> OrderResult:
        last = None
        for filling in self._fillings(req["symbol"]):
            req["type_filling"] = filling
            for attempt in range(3):
                if req.get("action") == mt5.TRADE_ACTION_DEAL:
                    t = mt5.symbol_info_tick(req["symbol"])
                    if t is not None:
                        req["price"] = t.ask if req["type"] == mt5.ORDER_TYPE_BUY else t.bid
                res = mt5.order_send(req)
                last = res
                if res is None:
                    time.sleep(0.5)
                    continue
                if res.retcode in RETCODE_OK:
                    return OrderResult(True, int(res.order or res.deal), float(res.price or req.get("price", 0)),
                                       res.comment)
                if res.retcode == RETCODE_INVALID_FILL:
                    break  # try next filling mode
                if res.retcode in (10004, 10020, 10021):  # requote / price changed / no prices
                    time.sleep(0.3)
                    continue
                return OrderResult(False, message=f"retcode={res.retcode} {res.comment}")
        msg = f"retcode={getattr(last, 'retcode', None)} {getattr(last, 'comment', mt5.last_error())}"
        return OrderResult(False, message=msg)

    def open_position(self, base, direction, volume, sl, tp, magic, comment) -> OrderResult:
        spec = self.symbol_spec(base)
        req = {
            "action": mt5.TRADE_ACTION_DEAL, "symbol": spec.name, "volume": float(volume),
            "type": mt5.ORDER_TYPE_BUY if direction > 0 else mt5.ORDER_TYPE_SELL, "price": 0.0,
            "sl": round(sl, spec.digits), "tp": round(tp, spec.digits) if tp else 0.0,
            "deviation": self.cfg.broker.deviation_points, "magic": int(magic), "comment": comment[:31],
            "type_time": mt5.ORDER_TIME_GTC,
        }
        res = self._send(req)
        if res.ok:
            # resolve the position ticket and real fill price
            for p in mt5.positions_get(symbol=spec.name) or []:
                if p.magic == magic and (p.ticket == res.ticket or p.identifier == res.ticket):
                    return OrderResult(True, int(p.ticket), float(p.price_open), res.message)
        return res

    def modify_position(self, ticket, base, sl, tp) -> OrderResult:
        spec = self.symbol_spec(base)
        req = {"action": mt5.TRADE_ACTION_SLTP, "position": int(ticket), "symbol": spec.name,
               "sl": round(sl, spec.digits) if sl else 0.0, "tp": round(tp, spec.digits) if tp else 0.0}
        res = mt5.order_send(req)
        if res is not None and res.retcode in RETCODE_OK:
            return OrderResult(True, int(ticket))
        return OrderResult(False, message=f"retcode={getattr(res, 'retcode', None)} "
                                          f"{getattr(res, 'comment', mt5.last_error())}")

    def close_position(self, ticket, base, comment="") -> OrderResult:
        spec = self.symbol_spec(base)
        pos = mt5.positions_get(ticket=int(ticket))
        if not pos:
            return OrderResult(True, int(ticket), message="already closed")
        p = pos[0]
        req = {
            "action": mt5.TRADE_ACTION_DEAL, "position": int(ticket), "symbol": spec.name,
            "volume": float(p.volume),
            "type": mt5.ORDER_TYPE_SELL if p.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY,
            "price": 0.0, "deviation": self.cfg.broker.deviation_points, "magic": int(p.magic),
            "comment": (comment or "GT close")[:31], "type_time": mt5.ORDER_TIME_GTC,
        }
        return self._send(req)
