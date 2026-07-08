# MT5 EMA 3/4 Cross Bot

A pure EMA crossover trading bot for MetaTrader 5. Two interchangeable implementations of the same strategy:

- **`mql5/EmaCross34.mq5`** — native Expert Advisor that runs inside the MT5 terminal (recommended: runs 24/5 with the terminal, and can be backtested in the Strategy Tester).
- **`python/ema_cross_bot.py`** — Python bot using the official `MetaTrader5` package (Windows only, needs the terminal running).

## Strategy rules

The bot relies **only** on the EMA(3)/EMA(4) cross. There is **no stop loss and no take profit** — a position is only ever closed by the opposite cross.

| Event (on closed bars) | Action |
|---|---|
| EMA(3) crosses **above** EMA(4) | Close short (if any), open **long** |
| EMA(3) crosses **below** EMA(4) | Close long (if any), open **short** |
| Cross closes a trade at a **loss within −10 pips** (whipsaw) | **Re-enter the same direction** as the closed trade instead of reversing (up to 1 consecutive re-entry by default) |

Details:

- Signals are evaluated once per bar, on **bar close** (the last completed bar vs. the one before it), so intrabar EMA flickering is ignored.
- **Re-entry (whipsaw filter):** a cross that closes a trade at a small loss (between 0 and −10 pips) is treated as a fake signal — the bot immediately re-opens in the *same* direction as the trade that was just closed. The re-entered position is then managed normally: the next cross against it closes it (and the re-entry check applies again, limited by the max-consecutive-re-entries setting). A close at a profit, or at a loss worse than −10 pips, reverses normally.
- After a re-entry, if the next signal *confirms* the held direction, the re-entry counter resets.
- By default the bot waits for the **first fresh cross** after starting before opening anything. Enable *enter on start* to open immediately in the current EMA direction instead.
- One position per symbol per magic number. Positions opened manually or by other EAs (different magic) are ignored.

## MQL5 Expert Advisor

1. Open MetaEditor (F4 in MT5), copy `mql5/EmaCross34.mq5` into `MQL5/Experts/`, and compile (F7).
2. In MT5, enable **Algo Trading** (toolbar button) and allow it in *Tools → Options → Expert Advisors*.
3. Open a chart of the symbol **and the timeframe you want the bot to trade** (the EA uses the chart timeframe — e.g. M5), then drag `EmaCross34` onto it.

### Inputs

| Input | Default | Meaning |
|---|---|---|
| `InpFastPeriod` / `InpSlowPeriod` | 3 / 4 | EMA periods |
| `InpLots` | 0.01 | Lot size (auto-clamped to the symbol's min/step) |
| `InpEnableReentry` | true | Whipsaw re-entry on/off |
| `InpReentryMaxLossPips` | 10.0 | Re-enter when closed at a loss within this many pips |
| `InpMaxConsecReentries` | 1 | Max consecutive re-entries before reversing anyway |
| `InpEnterOnStart` | false | Open in the current EMA direction on start |
| `InpMagic` | 340034 | Magic number identifying this bot's positions |
| `InpSlippagePoints` | 20 | Max slippage in points |

Backtest first: *View → Strategy Tester*, select `EmaCross34`, your symbol/timeframe, "Every tick based on real ticks" if available.

## Python bot

Requirements: **Windows**, MT5 terminal installed, running and logged into your (demo) account, Algo Trading enabled.

```bash
pip install -r python/requirements.txt
python python/ema_cross_bot.py --symbol EURUSD --timeframe M5 --lots 0.01
```

All strategy parameters are flags — see `python python/ema_cross_bot.py --help` (`--reentry-pips`, `--max-reentries`, `--no-reentry`, `--enter-on-start`, `--fast`, `--slow`, `--magic`, ...).

Stopping the script (Ctrl-C) leaves any open position running — close it manually or restart the bot.

## Notes for a $50 demo account

- Use **0.01 lots**. One open 0.01 EURUSD position needs roughly $10–25 of margin depending on leverage.
- EMA 3/4 is an extremely fast pair — it crosses **a lot**. On M1 the spread will eat most moves; M5 or M15 is a more realistic starting point. Run it on demo and watch the trade count vs. spread cost.
- No SL means a position can carry a large floating loss until the opposite cross arrives (fast EMAs cross quickly, but gaps and news spikes still hurt). That's the strategy's design — just be aware of it.
- After a re-entry the bot is intentionally holding *against* the latest cross until price crosses back; that exposure is also part of the design.
