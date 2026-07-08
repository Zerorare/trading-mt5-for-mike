"""Offline backtester for the EMA 3/4 cross bot.

Replicates ema_cross_bot.py / EmaCross34.mq5 exactly:
  * EMA(fast)/EMA(slow) cross evaluated on closed bars.
  * Signal acts at the OPEN of the next bar (like the live bot, which
    trades on new-bar arrival).
  * Cross closes the position and reverses. No SL / no TP.
  * Whipsaw re-entry: a cross that closes a trade at a loss within
    --reentry-pips re-opens the SAME direction (max --max-reentries
    consecutive times).

Fill model: candle prices are treated as bid. Longs buy at open+spread
and sell at open; shorts sell at open and buy back at open+spread, so
each round trip pays the full spread once.

Data sources (pick one):
  --csv FILE            CSV with columns: time/datetime, open, high, low, close
  --from-mt5 SYMBOL     pull bars straight from a running MT5 terminal
                        (Windows; use with --timeframe and --bars)
  --sample              bundled real EURUSD H1 2017-2018 (pip install backtesting)

Examples:
  python backtest.py --sample --spread 1.0
  python backtest.py --from-mt5 EURUSD --timeframe M1 --bars 40000 --spread 1.0
  python backtest.py --csv eurusd_m5.csv --spread 0.8 --plot equity.png
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
    open_at_end: bool = False


@dataclass
class Result:
    label: str
    spread: float
    trades: list

    @property
    def closed(self):
        return [t for t in self.trades if not t.open_at_end]

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
            "win_rate": 100.0 * len(wins) / len(tr),
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
    closes,
    fast: int = 3,
    slow: int = 4,
    spread_pips: float = 1.0,
    pip: float = 0.0001,
    reentry: bool = True,
    reentry_max_loss_pips: float = 10.0,
    max_reentries: int = 1,
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
    entry_price = 0.0
    entry_time = None
    entry_bar = 0
    is_reentry = False
    reentry_count = 0
    trades: list = []

    def open_pos(direction: int, price_bid: float, t, bar: int, reentered: bool):
        nonlocal pos_dir, entry_price, entry_time, entry_bar, is_reentry
        pos_dir = direction
        entry_price = price_bid + spread if direction > 0 else price_bid
        entry_time = t
        entry_bar = bar
        is_reentry = reentered

    def close_pips(price_bid: float) -> float:
        exit_price = price_bid if pos_dir > 0 else price_bid + spread
        diff = (exit_price - entry_price) if pos_dir > 0 else (entry_price - exit_price)
        return diff / pip

    warmup = max(slow * 5, 20)  # let both EMAs converge before trading
    for i in range(warmup, n):
        cross_up = ef[i - 1] > es[i - 1] and ef[i - 2] <= es[i - 2]
        cross_down = ef[i - 1] < es[i - 1] and ef[i - 2] >= es[i - 2]
        sig = 1 if cross_up else (-1 if cross_down else 0)
        if sig == 0:
            continue

        o = opens[i]
        if pos_dir == 0:
            reentry_count = 0
            open_pos(sig, o, times[i], i, False)
            continue
        if pos_dir == sig:
            reentry_count = 0
            continue

        pips = close_pips(o)
        trades.append(Trade(pos_dir, entry_time, times[i], pips, i - entry_bar, is_reentry))

        reenter = (
            reentry
            and -reentry_max_loss_pips <= pips <= 0.0
            and reentry_count < max_reentries
        )
        if reenter:
            reentry_count += 1
            open_pos(pos_dir, o, times[i], i, True)
        else:
            reentry_count = 0
            open_pos(sig, o, times[i], i, False)

    if pos_dir != 0:  # mark-to-market the position left open at data end
        pips = close_pips(closes[-1])
        trades.append(Trade(pos_dir, entry_time, times[-1], pips, n - 1 - entry_bar, is_reentry, True))

    return Result(label=label, spread=spread_pips, trades=trades)


# ----------------------------------------------------------------- data


def load_csv(path: str):
    times, opens, closes = [], [], []
    with open(path, newline="") as fh:
        reader = csvmod.DictReader(fh)
        cols = {c.lower().strip(): c for c in reader.fieldnames}
        tcol = next(cols[k] for k in ("time", "datetime", "date") if k in cols)
        ocol, ccol = cols["open"], cols["close"]
        for row in reader:
            times.append(row[tcol])
            opens.append(float(row[ocol]))
            closes.append(float(row[ccol]))
    return times, opens, closes


def load_sample():
    from backtesting.test import EURUSD  # real EURUSD H1, Apr 2017 - Feb 2018

    return list(EURUSD.index), list(EURUSD.Open), list(EURUSD.Close)


def load_mt5(symbol: str, timeframe: str, bars: int):
    import MetaTrader5 as mt5

    tf = getattr(mt5, f"TIMEFRAME_{timeframe}")
    if not mt5.initialize():
        raise SystemExit(f"mt5.initialize() failed: {mt5.last_error()}")
    rates = mt5.copy_rates_from_pos(symbol, tf, 0, bars)
    mt5.shutdown()
    if rates is None or not len(rates):
        raise SystemExit(f"no rates for {symbol} {timeframe}")
    times = [datetime.utcfromtimestamp(int(r["time"])) for r in rates]
    return times, [float(r["open"]) for r in rates], [float(r["close"]) for r in rates]


# ----------------------------------------------------------------- report


def print_report(res: Result, lot_pip_value: float = 0.10, start_balance: float = 50.0):
    s = res.stats()
    if not s:
        print("no closed trades")
        return
    open_note = ""
    still_open = [t for t in res.trades if t.open_at_end]
    if still_open:
        open_note = f"  (+1 position still open at data end: {still_open[0].pips:+.1f} pips)"
    print(f"\n=== {res.label} | spread {res.spread:.1f} pips ===")
    print(f"trades:            {s['trades']}{open_note}")
    print(f"  re-entries:      {s['reentries']}")
    print(f"win rate:          {s['win_rate']:.1f}%")
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
    p.add_argument("--pip", type=float, default=0.0001, help="pip size (0.0001 for EURUSD)")
    p.add_argument("--no-reentry", action="store_true")
    p.add_argument("--reentry-pips", type=float, default=10.0)
    p.add_argument("--max-reentries", type=int, default=1)
    p.add_argument("--label", default=None)
    args = p.parse_args()

    if args.csv:
        times, opens, closes = load_csv(args.csv)
        label = args.label or args.csv
    elif args.from_mt5:
        times, opens, closes = load_mt5(args.from_mt5, args.timeframe, args.bars)
        label = args.label or f"{args.from_mt5} {args.timeframe} ({len(closes)} bars)"
    else:
        times, opens, closes = load_sample()
        label = args.label or f"EURUSD H1 sample ({len(closes)} bars)"

    res = run_backtest(
        times,
        opens,
        closes,
        fast=args.fast,
        slow=args.slow,
        spread_pips=args.spread,
        pip=args.pip,
        reentry=not args.no_reentry,
        reentry_max_loss_pips=args.reentry_pips,
        max_reentries=args.max_reentries,
        label=label,
    )
    print(f"data: {times[0]} .. {times[-1]}")
    print_report(res)


if __name__ == "__main__":
    main()
