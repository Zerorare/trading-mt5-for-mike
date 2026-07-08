"""Offline test harness: stubs the MetaTrader5 module and exercises
ema(), read_signal() cross detection, and process_signal() re-entry logic."""
import sys
import types
from types import SimpleNamespace

# ---------------------------------------------------------------- mt5 stub
mt5 = types.ModuleType("MetaTrader5")
mt5.TIMEFRAME_M1, mt5.TIMEFRAME_M5, mt5.TIMEFRAME_M15 = 1, 5, 15
mt5.TIMEFRAME_M30, mt5.TIMEFRAME_H1, mt5.TIMEFRAME_H4, mt5.TIMEFRAME_D1 = 30, 60, 240, 1440
mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN = 1, 2, 3
mt5.POSITION_TYPE_BUY, mt5.POSITION_TYPE_SELL = 0, 1
mt5.ORDER_TYPE_BUY, mt5.ORDER_TYPE_SELL = 0, 1
mt5.TRADE_ACTION_DEAL = 1
mt5.ORDER_TIME_GTC = 0
mt5.TRADE_RETCODE_DONE = 10009
mt5.TRADE_RETCODE_INVALID_FILL = 10030

STATE = {
    "rates": [],          # list of dicts with time/close
    "position": None,     # SimpleNamespace or None
    "bid": 1.10000,
    "ask": 1.10002,
    "opened": [],         # log of (direction, price)
    "closed": [],         # log of tickets
    "next_ticket": 100,
}

def _initialize(**kw): return True
def _shutdown(): pass
def _last_error(): return (0, "ok")
def _symbol_info(sym):
    return SimpleNamespace(point=0.00001, digits=5, visible=True)
def _symbol_info_tick(sym):
    return SimpleNamespace(bid=STATE["bid"], ask=STATE["ask"])
def _account_info():
    return SimpleNamespace(login=1, server="demo", balance=50.0, currency="USD")
def _symbol_select(sym, on): return True
def _copy_rates_from_pos(sym, tf, start, count):
    rates = STATE["rates"][-count:]
    return rates if rates else None
def _positions_get(symbol=None):
    return [STATE["position"]] if STATE["position"] else []
def _order_send(req):
    if "position" in req:  # close
        STATE["closed"].append(req["position"])
        STATE["position"] = None
    else:                   # open
        direction = 1 if req["type"] == mt5.ORDER_TYPE_BUY else -1
        STATE["next_ticket"] += 1
        STATE["position"] = SimpleNamespace(
            ticket=STATE["next_ticket"],
            type=mt5.POSITION_TYPE_BUY if direction > 0 else mt5.POSITION_TYPE_SELL,
            price_open=req["price"], volume=req["volume"], magic=req["magic"],
        )
        STATE["opened"].append((direction, req["price"]))
    return SimpleNamespace(retcode=mt5.TRADE_RETCODE_DONE, price=req["price"], comment="ok")

mt5.initialize = _initialize
mt5.shutdown = _shutdown
mt5.last_error = _last_error
mt5.symbol_info = _symbol_info
mt5.symbol_info_tick = _symbol_info_tick
mt5.account_info = _account_info
mt5.symbol_select = _symbol_select
mt5.copy_rates_from_pos = _copy_rates_from_pos
mt5.positions_get = _positions_get
mt5.order_send = _order_send
sys.modules["MetaTrader5"] = mt5

import os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from ema_cross_bot import EmaCrossBot, ema

failures = []
def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        failures.append(name)

# ---------------------------------------------------------------- ema()
e3 = ema([1, 1, 1, 1, 1], 3)
check("ema constant series stays constant", all(abs(v - 1) < 1e-12 for v in e3))
e = ema([1.0, 2.0], 3)  # alpha=0.5 -> 1, 1.5
check("ema alpha math", abs(e[1] - 1.5) < 1e-12)

# ---------------------------------------------------------------- bot setup
def make_bot(**over):
    args = SimpleNamespace(
        symbol="EURUSD", timeframe="M5", lots=0.01, fast=3, slow=4,
        no_reentry=False, reentry_pips=10.0, max_reentries=1,
        magic=340034, deviation=20, enter_on_start=False,
    )
    for k, v in over.items():
        setattr(args, k, v)
    b = EmaCrossBot(args)
    b.pip_size = 0.0001
    return b

def set_closes(closes):
    STATE["rates"] = [{"time": i, "close": c} for i, c in enumerate(closes)]

# ---------------------------------------------------------------- read_signal
b = make_bot()
# rising series then check no cross (fast stays above slow), last bar is forming (dropped)
set_closes([1.0] * 50 + [1.001, 1.002, 1.003, 1.004, 999])
check("no cross while trending", b.read_signal() == 0)

# construct bullish cross: falling then a strong up close on the last CLOSED bar
set_closes([1.010 - 0.0005 * i for i in range(50)] + [1.05, 999])
check("bullish cross detected", b.read_signal() == 1)

# bearish cross: rising then strong down close
set_closes([1.000 + 0.0005 * i for i in range(50)] + [0.95, 999])
check("bearish cross detected", b.read_signal() == -1)

check("state_only reports direction", b.read_signal(state_only=True) == -1)

# ---------------------------------------------------------------- process_signal: flat -> open
STATE["position"] = None
STATE["opened"].clear()
b = make_bot()
check("flat + bullish signal handled", b.process_signal(1) is True)
check("opened LONG", STATE["opened"][-1][0] == 1)

# ---------------------------------------------------------------- reversal on big loss
# long from 1.10002 (ask). Price falls 30 pips -> bearish cross closes at big loss -> reverse
STATE["bid"], STATE["ask"] = 1.09702, 1.09704
check("bearish signal handled", b.process_signal(-1) is True)
check("reversed to SHORT after -30 pip close", STATE["opened"][-1][0] == -1)
check("reentry count reset", b.reentry_count == 0)

# ---------------------------------------------------------------- whipsaw re-entry
# short from 1.09702 (bid). Price rises 5 pips -> bullish cross closes at -5 pips -> RE-ENTER SHORT
STATE["bid"], STATE["ask"] = 1.09750, 1.09752
check("bullish signal handled", b.process_signal(1) is True)
check("re-entered SHORT (not reversed)", STATE["opened"][-1][0] == -1)
check("reentry count = 1", b.reentry_count == 1)

# next bearish cross confirms the held short -> hold, counter resets
check("confirming signal holds position", b.process_signal(-1) is True)
check("position still open", STATE["position"] is not None)
check("counter reset on confirmation", b.reentry_count == 0)

# ---------------------------------------------------------------- max reentries respected
# whipsaw again at small loss: re-entry #1 ok...
STATE["bid"], STATE["ask"] = 1.09755, 1.09757
b.process_signal(1)
check("second whipsaw re-enters (count back to 1)", STATE["opened"][-1][0] == -1 and b.reentry_count == 1)
# ...another small-loss cross right after: count exhausted -> reverse
STATE["bid"], STATE["ask"] = 1.09760, 1.09762
b.process_signal(1)
check("re-entry limit reached -> reverses to LONG", STATE["opened"][-1][0] == 1)
check("counter reset after forced reversal", b.reentry_count == 0)

# ---------------------------------------------------------------- profit close never re-enters
# long from ~1.09762; price up 20 pips, bearish cross -> profit -> reverse
STATE["bid"], STATE["ask"] = 1.09962, 1.09964
b.process_signal(-1)
check("profitable close reverses (no re-entry)", STATE["opened"][-1][0] == -1)

# ---------------------------------------------------------------- loss just past threshold
# short from 1.09962; price rises 10.1 pips -> loss worse than -10 -> reverse
STATE["bid"], STATE["ask"] = 1.10065, 1.10067
b.process_signal(1)
check("-10.5 pip close reverses (outside threshold)", STATE["opened"][-1][0] == 1)

# ---------------------------------------------------------------- --no-reentry flag
b2 = make_bot(no_reentry=True)
STATE["position"] = SimpleNamespace(ticket=1, type=mt5.POSITION_TYPE_SELL,
                                    price_open=1.10000, volume=0.01, magic=340034)
STATE["bid"], STATE["ask"] = 1.10030, 1.10032  # -3.2 pips on the short
b2.process_signal(1)
check("re-entry disabled -> reverses on small loss", STATE["opened"][-1][0] == 1)

# ---------------------------------------------------------------- foreign magic ignored
STATE["position"] = SimpleNamespace(ticket=2, type=mt5.POSITION_TYPE_SELL,
                                    price_open=1.10000, volume=0.01, magic=999)
b3 = make_bot()
check("foreign-magic position invisible", b3.find_position() is None)

print()
if failures:
    print(f"{len(failures)} FAILURES: {failures}")
    sys.exit(1)
print("all tests passed")
