# MT5 EMA 3/4 Cross Bot

A pure EMA crossover trading bot for MetaTrader 5, set up for **gold (XAUUSD)** by default but working on any symbol. Two interchangeable implementations of the same strategy:

- **`mql5/EmaCross34.mq5`** — native Expert Advisor that runs inside the MT5 terminal (recommended: runs 24/5 with the terminal, and can be backtested in the Strategy Tester).
- **`python/ema_cross_bot.py`** — Python bot using the official `MetaTrader5` package (Windows only, needs the terminal running).

## Strategy rules

The bot relies **only** on the EMA(3)/EMA(4) cross. There is **no stop loss and no take profit** — a position is only ever closed by the opposite cross.

| Event (on closed bars) | Action |
|---|---|
| EMA(3) crosses **above** EMA(4) | Close short (if any), open **long** |
| EMA(3) crosses **below** EMA(4) | Close long (if any), open **short** |
| EMAs are **less than 1 pip apart** at the cross | **Weak-cross filter**: signal ignored entirely (no open, no close) — this is the single biggest win-rate lever |
| Cross closes a trade at a **loss within −10 pips** (whipsaw) | **Re-enter the same direction** as the closed trade instead of reversing (up to 1 consecutive re-entry by default) |
| Trade reaches **+3 pips** profit | **Break-even**: SL moves to entry +1 pip, so the trade can no longer close as a loss (the only stop the bot sets unless a take-profit is enabled) |
| Optional take-profit (`--tp-pips` / `InpTakeProfitPips`, off by default) | Fixed TP attached to each entry; smooths results slightly at 20 pips in testing |

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
| `InpEnableBreakEven` | true | Break-even stop on/off |
| `InpBreakEvenTriggerPips` | 3.0 | Profit (pips) that arms the break-even stop |
| `InpBreakEvenOffsetPips` | 1.0 | Pips locked in when it arms |
| `InpTakeProfitPips` | 0 (off) | Fixed take-profit in pips |
| `InpMinCrossSepPips` | 1.0 | Skip crosses where the EMA gap is below this (0 = off) |
| `InpPipSizeOverride` | 0 (auto) | Pip size; auto = 10 points on 3/5-digit FX and gold (XAUUSD pip = $0.10) |
| `InpMagic` | 340034 | Magic number identifying this bot's positions |
| `InpSlippagePoints` | 20 | Max slippage in points |

A "pip" on gold here means a **$0.10 move in the gold price** — so the defaults on XAUUSD read as: re-enter if closed within −$1.00, arm break-even at +$0.30, lock +$0.10, skip crosses where the EMAs are less than $0.10 apart. The 1-pip filter was calibrated on EURUSD H1; on gold M5 the equivalent is likely nearer 2 pips — sweep `--min-sep` on your own data (see Backtesting).

Backtest first: *View → Strategy Tester*, select `EmaCross34`, your symbol/timeframe, "Every tick based on real ticks" if available.

## Python bot

Requirements: **Windows**, MT5 terminal installed, running and logged into your (demo) account, Algo Trading enabled.

```bash
pip install -r python/requirements.txt
python python/ema_cross_bot.py --symbol XAUUSD --timeframe M5 --lots 0.01
```

All strategy parameters are flags — see `python python/ema_cross_bot.py --help` (`--reentry-pips`, `--max-reentries`, `--no-reentry`, `--enter-on-start`, `--fast`, `--slow`, `--magic`, ...).

Stopping the script (Ctrl-C) leaves any open position running — close it manually or restart the bot.

## Backtesting

`python/backtest.py` replicates the bot's logic exactly (bar-close signals, next-bar-open fills, stop-and-reverse, −10 pip re-entry, break-even stop, no SL/TP otherwise) and charges the full spread once per round trip. The break-even stop is simulated from bar highs/lows using the standard OHLC path heuristic (bullish bar: open→low→high→close, bearish: open→high→low→close).

```bash
# gold, straight from your running MT5 terminal (Windows)
python python/backtest.py --from-mt5 XAUUSD --timeframe M5 --bars 40000 --spread 2.5
python python/backtest.py --from-mt5 XAUUSD --timeframe M1 --bars 40000 --spread 2.5

# real EURUSD H1 2017-2018 bundled with `pip install backtesting`
python python/backtest.py --sample --spread 1.0

# or any exported CSV with time,open,high,low,close columns
python python/backtest.py --csv xauusd_m5.csv --spread 2.5 --pip 0.1
```

### Measured on real EURUSD H1 (Apr 2017 – Feb 2018, 5,000 bars), 1.0 pip spread

The progression that led to the current defaults (see `results/winrate_filter_eurusd_h1.png`):

| Variant | Trades | Win rate | Total | Profit factor | Max DD |
|---|---|---|---|---|---|
| Original (cross only) | 654 | 34.1% | −1,288 pips | 0.81 | 1,502 pips |
| + break-even (+5 trigger) | 770 | 66.2% | −121 pips | 0.97 | 1,294 pips |
| **+ 1-pip weak-cross filter, +3 trigger (defaults)** | 172 | **97.1%** | **+838 pips** | **3.87** | 146 pips |

Robustness of the default config on this data: profitable in both halves of the sample (PF 2.09 / 9.42), profitable at every spread tested (0 → +1,048; 1.5 → +781; 2.5 → +575; 3.5 pips → +513, PF 1.85), and the filter sweep is smooth (0.6 → 0.8 → 1.0 pips improves monotonically; above 1.0 the win rate holds but trade count shrinks). A 20-pip take-profit (`--tp 20`) smoothed the weaker half (+376 vs +242) and cut drawdown, at slightly lower total — optional.

**Mind the win-rate shape**: 97% wins means the many small wins are paid for by rare large losses (the ~3% of trades that never reach +3 pips and only exit on the next strong cross, averaging roughly −60 pips). That's inherent to break-even-plus-no-SL. The numbers above are one symbol, one timeframe, ten months — sweep `--min-sep` (and `--be-trigger`) on your own symbol/timeframe with `--from-mt5` before trusting them, and confirm in the MT5 Strategy Tester on real ticks. Earlier stages: `results/backtest_eurusd_h1.png`, `results/breakeven_effect_eurusd_h1.png`.

## Notes for a $50 demo account trading gold

- Use **0.01 lots** (= 1 oz). One open 0.01 XAUUSD position needs roughly $7–35 of margin depending on leverage and the gold price; 1 pip ($0.10 of price) = $0.10 of P/L.
- Typical broker spread on XAUUSD is $0.20–0.35 — that's **2.0–3.5 pips paid on every trade**. EMA 3/4 trades every ~5–8 bars, so on M1 the spread alone can burn the account within days; M5/M15 is the more survivable starting point.
- No SL (other than the break-even stop once armed) means a position can carry a large floating loss until the opposite cross arrives. Gold gaps and news spikes make this bigger than on FX majors.
- After a re-entry the bot is intentionally holding *against* the latest cross until price crosses back; that exposure is also part of the design.
