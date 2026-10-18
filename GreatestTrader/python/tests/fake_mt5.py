"""Minimal in-memory stand-in for the `MetaTrader5` package, enough to drive broker/mt5_broker.py on any OS.

Models the IPC link of the real package: after the terminal restarts (terminal_restart()), every call returns
None and last_error() is (-10004, 'No IPC connection') until initialize() is called again. Tests patch it in
with `monkeypatch.setattr(mt5_broker, "mt5", fake_mt5)` and reset it with reset().
"""
import calendar
from types import SimpleNamespace as NS

__version__ = "5.0.fake"
TIMEFRAME_M1, TIMEFRAME_M15, TIMEFRAME_H1 = 1, 15, 16385
ORDER_TYPE_BUY, ORDER_TYPE_SELL = 0, 1
POSITION_TYPE_BUY, POSITION_TYPE_SELL = 0, 1
ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 1, 2
TRADE_ACTION_DEAL, TRADE_ACTION_SLTP = 1, 6
ORDER_TIME_GTC = 0
DEAL_TYPE_BUY, DEAL_TYPE_SELL, DEAL_TYPE_BALANCE, DEAL_TYPE_CREDIT, DEAL_TYPE_BONUS = 0, 1, 2, 3, 6
DEAL_ENTRY_IN, DEAL_ENTRY_OUT = 0, 1

S = NS()


def reset():
    S.__dict__.clear()
    S.__dict__.update(
        session_ok=False, terminal_alive=True, connected=True, trade_allowed=True, now=0, calls={},
        positions=[], deals=[], ticket=1000, deal_ticket=9000, selected=set(), specs={}, ticks={}, rates={},
        equity=10000.0, balance=10000.0, credit=0.0, login=111, server="Demo", margin_mode=2,
        order_send_none_once=False, order_send_none_no_exec=False, positions_get_none=False, init_count=0,
        retcodes=[], account_trade_allowed=True, trade_expert=True, filter_deal_times=False)


reset()


def _count(name):
    S.calls[name] = S.calls.get(name, 0) + 1


def _ok():
    return S.session_ok and S.terminal_alive


def last_error():
    return (1, "Success") if _ok() else (-10004, "No IPC connection")


def initialize(**kw):
    _count("initialize")
    S.init_count += 1
    if not S.terminal_alive:
        return False
    S.session_ok = True
    return True


def shutdown():
    _count("shutdown")
    S.session_ok = False


def terminal_restart():
    """The terminal process restarts: the existing IPC session is dead until initialize() is called again."""
    S.session_ok = False


def account_info():
    _count("account_info")
    if not _ok():
        return None
    return NS(login=S.login, server=S.server, currency="USD", leverage=100, equity=S.equity, balance=S.balance,
              credit=S.credit, margin_free=S.equity, margin_mode=S.margin_mode, trade_allowed=S.account_trade_allowed,
              trade_expert=S.trade_expert)


def terminal_info():
    _count("terminal_info")
    if not _ok():
        return None
    return NS(connected=S.connected, trade_allowed=S.trade_allowed, build=5000)


def symbol_select(name, flag=True):
    _count("symbol_select")
    if not _ok() or name not in S.specs:
        return False
    S.selected.add(name)
    return True


def symbol_info(name):
    if not _ok():
        return None
    d = S.specs.get(name)
    return NS(**d) if d else None


def symbol_info_tick(name):
    _count("symbol_info_tick")
    if not _ok() or name not in S.selected:
        return None
    t = S.ticks.get(name)
    return NS(**t) if t else None


def copy_rates_from_pos(name, tf, pos, count):
    """Like MT5: a symbol's bar exists only after its first tick, so bar 0 is the bar of the symbol's latest tick
    (S.ticks[name]['time'], else S.now); `pos` counts back from it, at most `count` bars."""
    if not _ok():
        return None
    arr = S.rates.get((name, tf))
    if arr is None or len(arr) == 0:
        return arr
    t = S.ticks[name]["time"] if S.ticks.get(name) else S.now
    upto = arr[arr["time"] <= t]
    end = len(upto) - pos
    return upto[max(0, end - count):max(0, end)]


def positions_get(symbol=None, ticket=None):
    _count("positions_get")
    if not _ok() or S.positions_get_none:
        return None
    return tuple(NS(**p) for p in S.positions
                 if (symbol is None or p["symbol"] == symbol) and (ticket is None or p["ticket"] == ticket))


def order_calc_profit(otype, name, vol, a, b):
    if not _ok():
        return None
    return (b - a) * 100000 * vol * (1 if otype == ORDER_TYPE_BUY else -1)


def order_calc_margin(otype, name, vol, price):
    if not _ok():
        return None
    return 1000.0 * vol


def _deal(**kw):
    S.deal_ticket += 1
    d = dict(ticket=S.deal_ticket, time=S.now, time_msc=S.now * 1000, type=0, entry=0, magic=0, position_id=0,
             symbol="", volume=0.0, price=0.0, profit=0.0, commission=0.0, swap=0.0, fee=0.0, reason=3, comment="")
    d.update(kw)
    S.deals.append(d)
    return d


def _book(amount):
    """A deal of the fake server moves the balance (and the equity: floating P/L is not modelled) at once."""
    S.balance += amount
    S.equity += amount


def order_send(req):
    _count("order_send")
    if not _ok():
        return None
    if S.retcodes:  # scripted rejections, e.g. [10004] = one requote
        rc = S.retcodes.pop(0)
        return NS(retcode=rc, order=0, deal=0, price=0.0, comment=f"rc {rc}")
    if S.order_send_none_no_exec:  # reply lost AND nothing executed
        S.order_send_none_no_exec = False
        return None
    if req["action"] == TRADE_ACTION_DEAL and "position" not in req:
        S.ticket += 1
        S.positions.append(dict(ticket=S.ticket, identifier=S.ticket, symbol=req["symbol"], magic=req["magic"],
                                type=0 if req["type"] == ORDER_TYPE_BUY else 1, volume=req["volume"],
                                price_open=req.get("price", 1.0), time=S.now, sl=req["sl"], tp=req["tp"]))
        _deal(type=req["type"], entry=DEAL_ENTRY_IN, magic=req["magic"], position_id=S.ticket, symbol=req["symbol"],
              volume=req["volume"], price=req.get("price", 1.0), commission=-3.0)
        _book(-3.0)
        if S.order_send_none_once:  # executed on the server but the reply is lost (timeout / IPC hiccup)
            S.order_send_none_once = False
            return None
        return NS(retcode=10009, order=S.ticket, deal=S.ticket, price=req.get("price", 1.0), comment="done")
    if req["action"] == TRADE_ACTION_DEAL and "position" in req:
        pos = [p for p in S.positions if p["ticket"] == req["position"]]
        S.positions = [p for p in S.positions if p["ticket"] != req["position"]]
        for p in pos:
            sign = 1 if p["type"] == POSITION_TYPE_BUY else -1
            profit = sign * (req.get("price", 1.0) - p["price_open"]) * 100000 * p["volume"]
            _deal(type=req["type"], entry=DEAL_ENTRY_OUT, magic=p["magic"], position_id=p["ticket"],
                  symbol=p["symbol"], volume=p["volume"], price=req.get("price", 1.0), commission=-3.0,
                  profit=profit, reason=3)
            _book(profit - 3.0)
        if S.order_send_none_once:
            S.order_send_none_once = False
            return None
        return NS(retcode=10009, order=req["position"], deal=1, price=req.get("price", 1.0), comment="done")
    return NS(retcode=10009, order=0, deal=0, price=0, comment="done")


def history_deals_get(date_from=None, date_to=None, position=None):
    """By position, or every deal; with S.filter_deal_times only those whose time (server-time epoch) lies in
    [date_from, date_to] (naive datetimes), like MT5."""
    _count("history_deals_get")
    if not _ok():
        return None
    if position is not None:
        return tuple(NS(**d) for d in S.deals if d["position_id"] == position)
    if not S.filter_deal_times:
        return tuple(NS(**d) for d in S.deals)
    a = calendar.timegm(date_from.timetuple()) if date_from is not None else -1e18
    b = calendar.timegm(date_to.timetuple()) if date_to is not None else 1e18
    return tuple(NS(**d) for d in S.deals if a <= d["time"] <= b)
