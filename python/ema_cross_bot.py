"""EMA 3/4 cross bot for MetaTrader 5 (official MetaTrader5 Python API).

Strategy (pure cross, no SL / no TP):
  * EMA(fast)/EMA(slow) evaluated on closed bars of the chosen timeframe.
  * Bullish cross  -> close short (if any), open long.
  * Bearish cross  -> close long (if any), open short.
  * Positions are only ever closed by the opposite cross.
  * Re-entry (whipsaw filter): if a cross closes a trade at a small loss
    within -N pips (default 10), the bot re-enters in the SAME direction
    as the closed trade instead of reversing, up to --max-reentries
    consecutive times.
  * Break-even: once a trade is +N pips in profit (default 5), the SL
    moves to entry +/- an offset (default +1 pip), so the trade can no
    longer close as a loss. Disable with --no-breakeven.
  * Optional take-profit (--tp-pips) and weak-cross filter (--min-sep):
    both off by default; they raise the win rate at the cost of cutting
    runners / skipping trades.

Pip size is auto-detected: 10 points on 3/5-digit FX quotes and on
gold (XAUUSD pip = 0.1), otherwise 1 point; override with --pip.

Requirements: Windows, a running & logged-in MT5 terminal with
"Algo Trading" enabled, and `pip install MetaTrader5`.

Example:
    python ema_cross_bot.py --symbol XAUUSD --timeframe M5 --lots 0.01
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime

import MetaTrader5 as mt5

TIMEFRAMES = {
    "M1": mt5.TIMEFRAME_M1,
    "M5": mt5.TIMEFRAME_M5,
    "M15": mt5.TIMEFRAME_M15,
    "M30": mt5.TIMEFRAME_M30,
    "H1": mt5.TIMEFRAME_H1,
    "H4": mt5.TIMEFRAME_H4,
    "D1": mt5.TIMEFRAME_D1,
}

FILLING_MODES = (mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN)

WARMUP_BARS = 200  # enough history for EMA(3)/EMA(4) to fully converge


def log(msg: str) -> None:
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def ema(values, period: int):
    """EMA matching MT5's iMA(MODE_EMA): alpha=2/(period+1), seeded with the first value."""
    alpha = 2.0 / (period + 1.0)
    out = [values[0]]
    for v in values[1:]:
        out.append(alpha * v + (1.0 - alpha) * out[-1])
    return out


class EmaCrossBot:
    def __init__(self, args: argparse.Namespace) -> None:
        self.symbol = args.symbol
        self.timeframe = TIMEFRAMES[args.timeframe]
        self.lots = args.lots
        self.fast = args.fast
        self.slow = args.slow
        self.reentry_enabled = not args.no_reentry
        self.reentry_max_loss_pips = args.reentry_pips
        self.max_reentries = args.max_reentries
        self.breakeven_enabled = not args.no_breakeven
        self.be_trigger_pips = args.be_trigger
        self.be_offset_pips = args.be_offset
        self.tp_pips = args.tp_pips
        self.min_sep_pips = args.min_sep
        self.pip_override = args.pip
        self.magic = args.magic
        self.deviation = args.deviation
        self.enter_on_start = args.enter_on_start

        self.last_bar_time = None
        self.reentry_count = 0
        self.pip_size = None
        self.point = None
        self.digits = None
        self.stops_level_points = 0

    # ------------------------------------------------------------------ setup

    def connect(self) -> None:
        if not mt5.initialize():
            raise SystemExit(f"mt5.initialize() failed: {mt5.last_error()}")

        info = mt5.symbol_info(self.symbol)
        if info is None:
            raise SystemExit(f"Unknown symbol {self.symbol}")
        if not info.visible and not mt5.symbol_select(self.symbol, True):
            raise SystemExit(f"Failed to select symbol {self.symbol}")

        self.point = info.point
        self.digits = info.digits
        self.stops_level_points = getattr(info, "trade_stops_level", 0)
        is_gold = "XAU" in self.symbol.upper() or "GOLD" in self.symbol.upper()
        if self.pip_override > 0:
            self.pip_size = self.pip_override
        else:
            self.pip_size = info.point * (10 if (info.digits in (3, 5) or is_gold) else 1)

        acc = mt5.account_info()
        log(
            f"Connected to account {acc.login} ({acc.server}), "
            f"balance {acc.balance:.2f} {acc.currency}"
        )
        log(
            f"EMA {self.fast}/{self.slow} on {self.symbol} | lots {self.lots} | "
            f"pip {self.pip_size:g} | "
            f"re-entry {'ON' if self.reentry_enabled else 'OFF'} "
            f"(within -{self.reentry_max_loss_pips} pips, max {self.max_reentries}) | "
            f"break-even {'ON' if self.breakeven_enabled else 'OFF'} "
            f"(+{self.be_trigger_pips} -> lock +{self.be_offset_pips})"
        )

    # ------------------------------------------------------------------ data

    def read_signal(self, state_only: bool = False) -> int:
        """+1 bullish cross, -1 bearish cross, 0 none (evaluated on closed bars)."""
        rates = mt5.copy_rates_from_pos(self.symbol, self.timeframe, 0, WARMUP_BARS)
        if rates is None or len(rates) < self.slow + 3:
            return 0
        closes = [r["close"] for r in rates[:-1]]  # drop the still-forming bar
        f = ema(closes, self.fast)
        s = ema(closes, self.slow)
        if state_only:
            return 1 if f[-1] > s[-1] else (-1 if f[-1] < s[-1] else 0)
        if self.min_sep_pips > 0 and abs(f[-1] - s[-1]) < self.min_sep_pips * self.pip_size:
            return 0  # weak cross: EMAs barely apart, skip
        cross_up = f[-2] <= s[-2] and f[-1] > s[-1]
        cross_down = f[-2] >= s[-2] and f[-1] < s[-1]
        return 1 if cross_up else (-1 if cross_down else 0)

    def current_bar_time(self):
        rates = mt5.copy_rates_from_pos(self.symbol, self.timeframe, 0, 1)
        return rates[0]["time"] if rates is not None and len(rates) else None

    # ------------------------------------------------------------------ trading

    def find_position(self):
        positions = mt5.positions_get(symbol=self.symbol)
        if positions:
            for pos in positions:
                if pos.magic == self.magic:
                    return pos
        return None

    def floating_pips(self, pos) -> float:
        tick = mt5.symbol_info_tick(self.symbol)
        if tick is None:
            return 0.0
        if pos.type == mt5.POSITION_TYPE_BUY:
            return (tick.bid - pos.price_open) / self.pip_size
        return (pos.price_open - tick.ask) / self.pip_size

    def _order_send(self, request: dict):
        for filling in FILLING_MODES:
            request["type_filling"] = filling
            result = mt5.order_send(request)
            if result is None:
                log(f"order_send returned None: {mt5.last_error()}")
                return None
            if result.retcode == mt5.TRADE_RETCODE_INVALID_FILL:
                continue
            return result
        return result

    def open_position(self, direction: int) -> bool:
        tick = mt5.symbol_info_tick(self.symbol)
        if tick is None:
            return False
        order_type = mt5.ORDER_TYPE_BUY if direction > 0 else mt5.ORDER_TYPE_SELL
        price = tick.ask if direction > 0 else tick.bid
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": self.lots,
            "type": order_type,
            "price": price,
            "deviation": self.deviation,
            "magic": self.magic,
            "comment": "EMA 3/4 cross",
            "type_time": mt5.ORDER_TIME_GTC,
        }
        if self.tp_pips > 0:
            request["tp"] = round(
                price + direction * self.tp_pips * self.pip_size, self.digits
            )
        result = self._order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            log(
                f"Open {'LONG' if direction > 0 else 'SHORT'} failed: "
                f"{getattr(result, 'retcode', '?')} {getattr(result, 'comment', '')}"
            )
            return False
        log(f"Opened {'LONG' if direction > 0 else 'SHORT'} {self.lots} {self.symbol} @ {result.price}")
        return True

    def close_position(self, pos) -> bool:
        tick = mt5.symbol_info_tick(self.symbol)
        if tick is None:
            return False
        closing_buy = pos.type == mt5.POSITION_TYPE_SELL
        result = self._order_send(
            {
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": self.symbol,
                "volume": pos.volume,
                "type": mt5.ORDER_TYPE_BUY if closing_buy else mt5.ORDER_TYPE_SELL,
                "position": pos.ticket,
                "price": tick.ask if closing_buy else tick.bid,
                "deviation": self.deviation,
                "magic": self.magic,
                "comment": "EMA 3/4 cross close",
                "type_time": mt5.ORDER_TIME_GTC,
            }
        )
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            log(
                f"Close failed: {getattr(result, 'retcode', '?')} {getattr(result, 'comment', '')}"
            )
            return False
        return True

    def manage_breakeven(self) -> None:
        """Once the trade is +trigger pips, move the SL to entry +/- offset."""
        pos = self.find_position()
        if pos is None or self.floating_pips(pos) < self.be_trigger_pips:
            return
        direction = 1 if pos.type == mt5.POSITION_TYPE_BUY else -1
        be = round(pos.price_open + direction * self.be_offset_pips * self.pip_size, self.digits)
        if pos.sl != 0.0 and (
            (direction > 0 and pos.sl >= be - self.point / 2)
            or (direction < 0 and pos.sl <= be + self.point / 2)
        ):
            return  # already locked at break-even or better

        tick = mt5.symbol_info_tick(self.symbol)
        if tick is None:
            return
        min_dist = self.stops_level_points * self.point
        if (direction > 0 and tick.bid - be < min_dist) or (
            direction < 0 and be - tick.ask < min_dist
        ):
            return  # broker stops-level; retry on a later pass

        result = mt5.order_send(
            {
                "action": mt5.TRADE_ACTION_SLTP,
                "symbol": self.symbol,
                "position": pos.ticket,
                "sl": be,
                "tp": pos.tp,  # keep the TP, only move the SL
            }
        )
        if result is not None and result.retcode == mt5.TRADE_RETCODE_DONE:
            log(f"Break-even armed: SL {be} locks {self.be_offset_pips:+.1f} pips")

    def process_signal(self, direction: int) -> bool:
        """Act on a cross. Returns True when fully handled (retry otherwise)."""
        pos = self.find_position()
        if pos is None:
            # flat (start, or stopped out at break-even)
            self.reentry_count = 0
            return self.open_position(direction)

        pos_dir = 1 if pos.type == mt5.POSITION_TYPE_BUY else -1
        if pos_dir == direction:
            self.reentry_count = 0  # signal confirms what we already hold
            return True

        pips = self.floating_pips(pos)
        if not self.close_position(pos):
            return False

        reenter = (
            self.reentry_enabled
            and pips <= 0.0
            and pips >= -self.reentry_max_loss_pips
            and self.reentry_count < self.max_reentries
        )
        if reenter:
            self.reentry_count += 1
            target = pos_dir
            log(
                f"Closed {'LONG' if pos_dir > 0 else 'SHORT'} at {pips:.1f} pips (whipsaw) "
                f"-> re-entry #{self.reentry_count} in the same direction"
            )
        else:
            self.reentry_count = 0
            target = direction
            log(
                f"Closed {'LONG' if pos_dir > 0 else 'SHORT'} at {pips:.1f} pips "
                f"-> reversing to {'LONG' if direction > 0 else 'SHORT'}"
            )
        return self.open_position(target)

    # ------------------------------------------------------------------ loop

    def run(self) -> None:
        self.connect()
        pending = 0
        first_bar = True
        try:
            while True:
                bar_time = self.current_bar_time()
                if bar_time is not None and bar_time != self.last_bar_time:
                    was_first = first_bar and self.last_bar_time is None
                    self.last_bar_time = bar_time
                    first_bar = False
                    if was_first:
                        if self.enter_on_start:
                            signal = self.read_signal(state_only=True)
                            if signal != 0:
                                pending = signal
                    else:
                        signal = self.read_signal()
                        if signal != 0:
                            pending = signal

                if pending != 0 and self.process_signal(pending):
                    pending = 0

                if self.breakeven_enabled:
                    self.manage_breakeven()

                time.sleep(1)
        except KeyboardInterrupt:
            log("Stopped by user (open position, if any, is left running)")
        finally:
            mt5.shutdown()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Pure EMA 3/4 cross bot for MetaTrader 5")
    p.add_argument("--symbol", default="XAUUSD")
    p.add_argument("--timeframe", default="M5", choices=sorted(TIMEFRAMES))
    p.add_argument("--lots", type=float, default=0.01)
    p.add_argument("--fast", type=int, default=3, help="fast EMA period")
    p.add_argument("--slow", type=int, default=4, help="slow EMA period")
    p.add_argument("--no-reentry", action="store_true", help="disable the whipsaw re-entry")
    p.add_argument(
        "--reentry-pips",
        type=float,
        default=10.0,
        help="re-enter same direction when closed at a loss within this many pips",
    )
    p.add_argument("--max-reentries", type=int, default=1, help="max consecutive re-entries")
    p.add_argument("--no-breakeven", action="store_true", help="disable the break-even stop")
    p.add_argument(
        "--tp-pips",
        type=float,
        default=0.0,
        help="take-profit in pips (0 = off, positions exit on cross only)",
    )
    p.add_argument(
        "--min-sep",
        type=float,
        default=1.0,
        help="skip crosses where the EMAs are closer than this many pips (0 = off)",
    )
    p.add_argument(
        "--be-trigger",
        type=float,
        default=3.0,
        help="profit in pips that arms the break-even stop",
    )
    p.add_argument(
        "--be-offset",
        type=float,
        default=1.0,
        help="pips locked in when the break-even stop is set",
    )
    p.add_argument(
        "--pip",
        type=float,
        default=0.0,
        help="pip size override (0 = auto; e.g. 0.1 for XAUUSD)",
    )
    p.add_argument(
        "--enter-on-start",
        action="store_true",
        help="open in the current EMA direction immediately instead of waiting for a fresh cross",
    )
    p.add_argument("--magic", type=int, default=340034)
    p.add_argument("--deviation", type=int, default=20, help="max slippage in points")
    args = p.parse_args()
    if args.fast >= args.slow:
        p.error("--fast must be smaller than --slow")
    return args


if __name__ == "__main__":
    try:
        EmaCrossBot(parse_args()).run()
    except SystemExit as e:
        if e.code:
            log(str(e))
        sys.exit(e.code if isinstance(e.code, int) else 1)
