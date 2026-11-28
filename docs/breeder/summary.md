## Bot breeder: Did not reach 60% out of sample, and the champion lost money on the holdout. Its in-sample edge did not survive new days.

**Champion G42-3795** (orb): 10-min opening range breakout, 0 tick buffer; 0.18 ATR stop, 1.25R target; breakeven at 0.75R, trail 1.0R after 1.5R, time stop 12 bars; 6E CL ES YM

| Split | Trades | Win rate (95% range) | Per trade | Profit factor | Total |
|---|---|---|---|---|---|
| Train 2026-09-18→2026-10-21 (24 days) | 36 | 83% (68%–92%) | +0.613R | 4.84 | +22.1R ($+1,190) |
| Validate 2026-10-22→2026-11-06 (12 days) | 20 | 50% (30%–70%) | +0.018R | 1.04 | +0.3R ($+215) |
| **Holdout** 2026-11-09→2026-11-25 (13 days) | 13 | 46% (23%–71%) | -0.111R | 0.65 | -1.4R ($-208) |

- In-sample (TRAIN, the days it was bred on): 83% win rate over 36 trades, +0.613R per trade.
- VALIDATE (used to pick the champion): 50% over 20 trades, +0.018R per trade.
- HOLDOUT (never seen, the honest number): 46% over 13 trades, -0.111R per trade, profit factor 0.65. With only 13 trades the true win rate could be anywhere from 23% to 71% (95% range).
- Luck test: the same breeder run on scrambled prices (all real patterns destroyed) still bred a 95% in-sample bot (+0.134R over 38 trades), which then did 83% (+0.070R) over 6 trades on its holdout. That is what luck alone can do in-sample, so only the holdout number counts.
- Stress test, holdout with double slippage (2 ticks a fill): 38%, -0.120R per trade, profit factor 0.62.

### Textbook versions (generation 0, no breeding)

| Family | Train | Validate | Holdout |
|---|---|---|---|
| orb | 45%, +0.00R, n=184 | 30%, -0.23R, n=88 | 52%, +0.14R, n=100 |
| vwap_revert | 36%, -0.20R, n=45 | 50%, +0.18R, n=30 | 34%, -0.28R, n=29 |
| intraday_mom | 40%, -0.09R, n=163 | 54%, +0.29R, n=83 | 51%, +0.09R, n=85 |
| trend_pullback | 39%, -0.13R, n=120 | 40%, -0.14R, n=60 | 42%, -0.07R, n=50 |

### Every breeding run (different random seeds)

| Seed | Champion | Train | Validate | Holdout |
|---|---|---|---|---|
| 1 | orb | 83%, +0.61R, n=36 | 50%, +0.02R, n=20 | 46%, -0.11R, n=13 |
| 2 | intraday_mom | 78%, +0.34R, n=32 | 53%, -0.09R, n=17 | 35%, -0.21R, n=17 |
| 3 | intraday_mom | 78%, +0.32R, n=41 | 46%, -0.17R, n=13 | 48%, +0.17R, n=25 |

Data: yahoo 5-minute bars, ES=F, NQ=F, YM=F, RTY=F, CL=F, GC=F, ZN=F, 6E=F. 4600 bots bred over 50 generations. Costs: 1 tick slippage on stops and market fills, round-trip commission per contract. A win is a trade with P&L above zero after costs.
- Free data is short: Yahoo keeps 5-minute bars for ~60 days (about 40 sessions) and 1-minute bars for ~30 days. That is a few weeks of one market regime; a bot bred on it can be luck.
- Continuous front-month prices: contract roll days can show a jump.
- Fills are simulated on bars: stop first if a bar hits both, targets need a trade through by one tick, 1 tick slippage on stops and market orders. Real fills can be worse in fast markets.
- Each instrument trades one contract independently; no portfolio margin or overlap limits.
- Alpaca does not trade futures. Trading these bots live needs a futures broker (e.g. Interactive Brokers, Tradovate, NinjaTrader), and they should be paper traded first.
- Strategy families are public, documented types (opening range breakout, VWAP reversion, intraday momentum, trend following). They are not any bank's proprietary strategies, which aren't public.
