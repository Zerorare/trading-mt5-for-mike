"""Offline backtester for the EMA 3/4 cross bot.

Replicates ema_cross_bot.py / EmaCross34.mq5 exactly:
  * EMA(fast)/EMA(slow) cross evaluated on closed bars.
  * Signal acts at the OPEN of the next bar (like the live bot, which
    trades on new-bar arrival).
  * Cross closes the position and reverses. No SL / no TP.
  * Whipsaw re-entry: a cross that closes a trade at a loss within
    --reentry-pips re-opens the SAME direction (max --max-reentries
    consecutive times).
  * Break-even (default on, like the bot): once a trade is +trigger
    pips in profit, an SL is armed at entry +/- offset. Simulated with
    bar highs/lows; if the trigger and the stop are both inside one
    bar, the stop is assumed to hit (conservative). Disable with
    --no-breakeven.

Fill model: candle prices are treated as bid. Longs buy at open+spread
and sell at open; shorts sell at open and buy back at open+spread, so
each round trip pays the full spread once.

Data sources (pick one):
  --csv FILE            CSV with columns: time/datetime, open, high, low, close
  --from-mt5 SYMBOL     pull bars straight from a running MT5 terminal
                        (Windows; use with --timeframe and --bars)
  --sample              bundled real EURUSD H1 2017-2018 (pip install backtesting)

Pips: --pip 0 (default) auto-detects — from the terminal for --from-mt5
(0.1 on XAUUSD), by price magnitude for CSV data. Set it explicitly for
anything unusual.

Examples:
  python backtest.py --from-mt5 XAUUSD --timeframe M5 --bars 40000 --spread 2.5
  python backtest.py --sample --spread 1.0 --no-breakeven
  python backtest.py --csv xauusd_m5.csv --spread 2.5 --pip 0.1
"""

from __future__ import annotations

import argparse
import csv as csvmod
import sys
from dataclasses import dataclass
from datetime import datetime


# ----------------------------------------------------------------- strategy


def ema_next(prev: float, value: float, period: int) -> float:
    alpha = 2.0 / (period + 1.0)
    return alpha * value + (1.0 - alpha) * prev


@dataclass
class Trade:
    direction: int          # +1 long, -1 short
    entry_time: object
    exit_time: object
    pips: float
    bars_held: int
    is_reentry: bool
    reason: str             # 'cross' | 'be' | 'end'


@dataclass
class Result:
    label: str
    spread: float
    trades: list

    @property
    def closed(self):
        return [t for t in self.trades if t.reason != "end"]

    def stats(self) -> dict:
        tr = self.closed
        if not tr:
            return {}
        wins = [t.pips for t in tr if t.pips > 0]
        losses = [t.pips for t in tr if t.pips <= 0]
        total = sum(t.pips for t in tr)
        gross_win = sum(wins)
        gross_loss = -sum(losses)
        equity = peak = dd = 0.0
        for t in tr:
            equity += t.pips
            peak = max(peak, equity)
            dd = max(dd, peak - equity)
        return {
            "trades": len(tr),
            "reentries": sum(1 for t in tr if t.is_reentry),
            "be_closes": sum(1 for t in tr if t.reason == "be"),
            "tp_closes": sum(1 for t in tr if t.reason == "tp"),
            "win_rate": 100.0 * len(wins) / len(tr),
            "loss_rate": 100.0 * len(losses) / len(tr),
            "avg_win": (gross_win / len(wins)) if wins else 0.0,
            "avg_loss": (-gross_loss / len(losses)) if losses else 0.0,
            "total_pips": total,
            "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else float("inf"),
            "max_dd_pips": dd,
            "avg_bars_held": sum(t.bars_held for t in tr) / len(tr),
        }


def run_backtest(
    times,
    opens,
    highs,
    lows,
    closes,
    fast: int = 3,
    slow: int = 4,
    spread_pips: float = 1.0,
    pip: float = 0.0001,
    reentry: bool = True,
    reentry_max_loss_pips: float = 10.0,
    max_reentries: int = 1,
    breakeven: bool = True,
    be_trigger_pips: float = 3.0,
    be_offset_pips: float = 1.0,
    tp_pips: float = 0.0,
    min_sep_pips: float = 0.0,
    label: str = "",
) -> Result:
    n = len(closes)
    if n < slow + 3:
        raise SystemExit("not enough bars")

    spread = spread_pips * pip
    ef = [closes[0]]
    es = [closes[0]]
    for c in closes[1:]:
        ef.append(ema_next(ef[-1], c, fast))
        es.append(ema_next(es[-1], c, slow))

    pos_dir = 0
    entry_price = 0.0      # ask for longs, bid for shorts
    entry_time = None
    entry_bar = 0
    is_reentry = False
    be_armed = False
    reentry_count = 0
    trades: list = []

    def open_pos(direction: int, price_bid: float, t, bar: int, reentered: bool):
        nonlocal pos_dir, entry_price, entry_time, entry_bar, is_reentry, be_armed
        pos_dir = direction
        entry_price = price_bid + spread if direction > 0 else price_bid
        entry_time = t
        entry_bar = bar
        is_reentry = reentered
        be_armed = False

    def close_pips(price_bid: float) -> float:
        exit_price = price_bid if pos_dir > 0 else price_bid + spread
        diff = (exit_price - entry_price) if pos_dir > 0 else (entry_price - exit_price)
        return diff / pip

    def record(t, bar_i: int, pips: float, reason: str):
        nonlocal pos_dir
        trades.append(Trade(pos_dir, entry_time, t, pips, bar_i - entry_bar, is_reentry, reason))
        pos_dir = 0

    warmup = max(slow * 5, 20)  # let both EMAs converge before trading
    for i in range(warmup, n):
        # 1) cross signal, acting at this bar's open
        cross_up = ef[i - 1] > es[i - 1] and ef[i - 2] <= es[i - 2]
        cross_down = ef[i - 1] < es[i - 1] and ef[i - 2] >= es[i - 2]
        if min_sep_pips > 0 and abs(ef[i - 1] - es[i - 1]) < min_sep_pips * pip:
            cross_up = cross_down = False  # weak cross: EMAs barely apart, skip
        sig = 1 if cross_up else (-1 if cross_down else 0)
        if sig != 0:
            o = opens[i]
            if pos_dir == 0:
                reentry_count = 0
                open_pos(sig, o, times[i], i, False)
            elif pos_dir == sig:
                reentry_count = 0
            else:
                pips = close_pips(o)
                closed_dir = pos_dir
                record(times[i], i, pips, "cross")
                reenter = (
                    reentry
                    and -reentry_max_loss_pips <= pips <= 0.0
                    and reentry_count < max_reentries
                )
                if reenter:
                    reentry_count += 1
                    open_pos(closed_dir, o, times[i], i, True)
                else:
                    reentry_count = 0
                    open_pos(sig, o, times[i], i, False)

        # 2) take-profit and break-even stop, simulated inside this bar
        #    (entry bar included: fills happen at the open, so the whole
        #    bar is post-entry). Intrabar order uses the OHLC path
        #    heuristic: bullish bar open->low->high->close, bearish bar
        #    open->high->low->close.
        if pos_dir != 0 and (breakeven or tp_pips > 0):
            bullish = closes[i] >= opens[i]
            if pos_dir > 0:
                tp_level = entry_price + tp_pips * pip                # bid target
                arm_level = entry_price + be_trigger_pips * pip
                be_stop = entry_price + be_offset_pips * pip          # bid stop
                armed_at_start = be_armed

                # gaps at the open
                if tp_pips > 0 and opens[i] >= tp_level:
                    record(times[i], i, close_pips(opens[i]), "tp")
                elif armed_at_start and opens[i] <= be_stop:
                    record(times[i], i, close_pips(opens[i]), "be")
                elif bullish:
                    # low leg first
                    if armed_at_start and lows[i] <= be_stop:
                        record(times[i], i, close_pips(be_stop), "be")
                    else:
                        # then the high leg
                        if tp_pips > 0 and highs[i] >= tp_level:
                            record(times[i], i, close_pips(tp_level), "tp")
                        else:
                            if breakeven and not be_armed and highs[i] >= arm_level:
                                be_armed = True
                            # stop armed mid-bar can only hit by the close
                            if be_armed and not armed_at_start and closes[i] <= be_stop:
                                record(times[i], i, close_pips(be_stop), "be")
                else:
                    # bearish bar: high leg first
                    if tp_pips > 0 and highs[i] >= tp_level:
                        record(times[i], i, close_pips(tp_level), "tp")
                    else:
                        if breakeven and not be_armed and highs[i] >= arm_level:
                            be_armed = True
                        # then the low leg: high came first, stop may hit
                        if be_armed and lows[i] <= be_stop:
                            record(times[i], i, close_pips(be_stop), "be")
            else:
                tp_ask = entry_price - tp_pips * pip                  # ask target
                arm_level_ask = entry_price - be_trigger_pips * pip
                be_stop_ask = entry_price - be_offset_pips * pip      # ask stop
                armed_at_start = be_armed

                if tp_pips > 0 and opens[i] + spread <= tp_ask:
                    record(times[i], i, close_pips(opens[i]), "tp")
                elif armed_at_start and opens[i] + spread >= be_stop_ask:
                    record(times[i], i, close_pips(opens[i]), "be")
                elif bullish:
                    # bullish bar, short: favorable low leg first
                    if tp_pips > 0 and lows[i] + spread <= tp_ask:
                        record(times[i], i, close_pips(tp_ask - spread), "tp")
                    else:
                        if breakeven and not be_armed and lows[i] + spread <= arm_level_ask:
                            be_armed = True
                        # then the adverse high leg
                        if be_armed and highs[i] + spread >= be_stop_ask:
                            record(times[i], i, close_pips(be_stop_ask - spread), "be")
                else:
                    # bearish bar, short: adverse high leg first
                    if armed_at_start and highs[i] + spread >= be_stop_ask:
                        record(times[i], i, close_pips(be_stop_ask - spread), "be")
                    else:
                        if tp_pips > 0 and lows[i] + spread <= tp_ask:
                            record(times[i], i, close_pips(tp_ask - spread), "tp")
                        else:
                            if breakeven and not be_armed and lows[i] + spread <= arm_level_ask:
                                be_armed = True
                            if be_armed and not armed_at_start and closes[i] + spread >= be_stop_ask:
                                record(times[i], i, close_pips(be_stop_ask - spread), "be")

    if pos_dir != 0:  # mark-to-market the position left open at data end
        record(times[-1], n - 1, close_pips(closes[-1]), "end")

    return Result(label=label, spread=spread_pips, trades=trades)


# ----------------------------------------------------------------- data


def load_csv(path: str):
    times, opens, highs, lows, closes = [], [], [], [], []
    with open(path, newline="") as fh:
        reader = csvmod.DictReader(fh)
        cols = {c.lower().strip(): c for c in reader.fieldnames}
        tcol = next(cols[k] for k in ("time", "datetime", "date") if k in cols)
        for row in reader:
            times.append(row[tcol])
            opens.append(float(row[cols["open"]]))
            highs.append(float(row[cols["high"]]))
            lows.append(float(row[cols["low"]]))
            closes.append(float(row[cols["close"]]))
    return times, opens, highs, lows, closes, 0.0


def load_sample():
    from backtesting.test import EURUSD  # real EURUSD H1, Apr 2017 - Feb 2018

    return (
        list(EURUSD.index),
        list(EURUSD.Open),
        list(EURUSD.High),
        list(EURUSD.Low),
        list(EURUSD.Close),
        0.0001,
    )


def load_mt5(symbol: str, timeframe: str, bars: int):
    import MetaTrader5 as mt5

    tf = getattr(mt5, f"TIMEFRAME_{timeframe}")
    if not mt5.initialize():
        raise SystemExit(f"mt5.initialize() failed: {mt5.last_error()}")
    info = mt5.symbol_info(symbol)
    rates = mt5.copy_rates_from_pos(symbol, tf, 0, bars)
    mt5.shutdown()
    if rates is None or not len(rates):
        raise SystemExit(f"no rates for {symbol} {timeframe}")
    is_gold = "XAU" in symbol.upper() or "GOLD" in symbol.upper()
    pip = info.point * (10 if (info.digits in (3, 5) or is_gold) else 1) if info else 0.0
    times = [datetime.utcfromtimestamp(int(r["time"])) for r in rates]
    return (
        times,
        [float(r["open"]) for r in rates],
        [float(r["high"]) for r in rates],
        [float(r["low"]) for r in rates],
        [float(r["close"]) for r in rates],
        pip,
    )


# ----------------------------------------------------------------- report


def print_report(res: Result, lot_pip_value: float = 0.10, start_balance: float = 50.0):
    s = res.stats()
    if not s:
        print("no closed trades")
        return
    open_note = ""
    still_open = [t for t in res.trades if t.reason == "end"]
    if still_open:
        open_note = f"  (+1 position still open at data end: {still_open[0].pips:+.1f} pips)"
    print(f"\n=== {res.label} | spread {res.spread:.1f} pips ===")
    print(f"trades:            {s['trades']}{open_note}")
    print(f"  re-entries:      {s['reentries']}")
    print(f"  break-even outs: {s['be_closes']}")
    print(f"  take-profits:    {s['tp_closes']}")
    print(f"win rate:          {s['win_rate']:.1f}%   (loss rate {s['loss_rate']:.1f}%)")
    print(f"avg win / loss:    {s['avg_win']:+.1f} / {s['avg_loss']:+.1f} pips")
    print(f"avg bars held:     {s['avg_bars_held']:.1f}")
    print(f"total:             {s['total_pips']:+.1f} pips")
    print(f"profit factor:     {s['profit_factor']:.2f}")
    print(f"max drawdown:      {s['max_dd_pips']:.1f} pips")
    end_bal = start_balance + s["total_pips"] * lot_pip_value
    print(
        f"$ at 0.01 lots:    {start_balance:.2f} -> {end_bal:.2f} "
        f"(max DD ${s['max_dd_pips'] * lot_pip_value:.2f})"
    )


def main() -> None:
    p = argparse.ArgumentParser(description="Backtest the EMA 3/4 cross strategy")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--csv", help="CSV file with time,open,high,low,close")
    src.add_argument("--from-mt5", metavar="SYMBOL", help="pull bars from a running MT5 terminal")
    src.add_argument("--sample", action="store_true", help="bundled real EURUSD H1 2017-2018")
    p.add_argument("--timeframe", default="M5", help="MT5 timeframe for --from-mt5 (M1, M5, ...)")
    p.add_argument("--bars", type=int, default=40000, help="bar count for --from-mt5")
    p.add_argument("--fast", type=int, default=3)
    p.add_argument("--slow", type=int, default=4)
    p.add_argument("--spread", type=float, default=1.0, help="spread in pips (round-trip cost)")
    p.add_argument("--pip", type=float, default=0.0, help="pip size (0 = auto-detect)")
    p.add_argument("--no-reentry", action="store_true")
    p.add_argument("--reentry-pips", type=float, default=10.0)
    p.add_argument("--max-reentries", type=int, default=1)
    p.add_argument("--no-breakeven", action="store_true", help="disable the break-even stop")
    p.add_argument("--be-trigger", type=float, default=3.0, help="pips of profit that arm break-even")
    p.add_argument("--be-offset", type=float, default=1.0, help="pips locked at break-even")
    p.add_argument("--tp", type=float, default=0.0, help="take-profit in pips (0 = off)")
    p.add_argument("--min-sep", type=float, default=1.0,
                   help="skip crosses where the EMAs are closer than this many pips (0 = off)")
    p.add_argument("--label", default=None)
    args = p.parse_args()

    if args.csv:
        times, opens, highs, lows, closes, pip = load_csv(args.csv)
        label = args.label or args.csv
    elif args.from_mt5:
        times, opens, highs, lows, closes, pip = load_mt5(args.from_mt5, args.timeframe, args.bars)
        label = args.label or f"{args.from_mt5} {args.timeframe} ({len(closes)} bars)"
    else:
        times, opens, highs, lows, closes, pip = load_sample()
        label = args.label or f"EURUSD H1 sample ({len(closes)} bars)"

    if args.pip > 0:
        pip = args.pip
    elif pip == 0.0:
        mid = sorted(closes)[len(closes) // 2]
        pip = 0.1 if mid > 100 else 0.0001  # gold/indices vs FX majors
        print(f"note: pip size auto-set to {pip:g} from price magnitude; override with --pip")

    res = run_backtest(
        times,
        opens,
        highs,
        lows,
        closes,
        fast=args.fast,
        slow=args.slow,
        spread_pips=args.spread,
        pip=pip,
        reentry=not args.no_reentry,
        reentry_max_loss_pips=args.reentry_pips,
        max_reentries=args.max_reentries,
        breakeven=not args.no_breakeven,
        be_trigger_pips=args.be_trigger,
        be_offset_pips=args.be_offset,
        tp_pips=args.tp,
        min_sep_pips=args.min_sep,
        label=label,
    )
    print(f"data: {times[0]} .. {times[-1]} | pip {pip:g}")
    print_report(res)


if __name__ == "__main__":
    main()
