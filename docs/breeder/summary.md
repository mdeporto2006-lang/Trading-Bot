## Bot breeder: Hit 60%+ on days it never saw (82% over 22 trades, +0.21R a trade), but 22 trades over a few weeks is not proof: the 95% range runs 61%–93%. A candidate to paper trade, not a proven bot.

**Champion G48-4395** (intraday_mom): follow the first 45 min move (from prev close) if > 0.2 ATR; 0.2 ATR stop, 0.5R target; with 60d trend, breakeven at 1.0R, trail 2.0R after 0.5R; CL ES NQ RTY YM

| Split | Trades | Win rate (95% range) | Per trade | Profit factor | Total |
|---|---|---|---|---|---|
| Train 2026-09-18→2026-10-21 (24 days) | 39 | 92% (80%–97%) | +0.363R | 5.55 | +14.1R ($+1,494) |
| Validate 2026-10-22→2026-11-06 (12 days) | 18 | 89% (67%–97%) | +0.314R | 3.74 | +5.6R ($+678) |
| **Holdout** 2026-11-09→2026-11-25 (13 days) | 22 | 82% (61%–93%) | +0.213R | 2.15 | +4.7R ($+990) |

- In-sample (TRAIN, the days it was bred on): 92% win rate over 39 trades, +0.363R per trade.
- VALIDATE (used to pick the champion): 89% over 18 trades, +0.314R per trade.
- HOLDOUT (never seen, the honest number): 82% over 22 trades, +0.213R per trade, profit factor 2.15. With only 22 trades the true win rate could be anywhere from 61% to 93% (95% range).
- Luck test: the same breeder run on scrambled prices (all real patterns destroyed) still bred a 69% in-sample bot (+0.575R over 42 trades), which then made no trades on its holdout. That is what luck alone can do in-sample, so only the holdout number counts.
- Stress test, holdout with double slippage (2 ticks a fill): 82%, +0.212R per trade, profit factor 2.14.

### Textbook versions (generation 0, no breeding)

| Family | Train | Validate | Holdout |
|---|---|---|---|
| orb | 45%, +0.00R, n=184 | 30%, -0.23R, n=88 | 52%, +0.14R, n=100 |
| vwap_revert | 33%, -0.21R, n=42 | 50%, +0.15R, n=32 | 36%, -0.25R, n=28 |
| intraday_mom | 40%, -0.08R, n=162 | 54%, +0.28R, n=83 | 51%, +0.09R, n=85 |
| trend_pullback | 39%, -0.13R, n=120 | 41%, -0.11R, n=59 | 42%, -0.07R, n=50 |

### Every breeding run (different random seeds)

| Seed | Champion | Train | Validate | Holdout |
|---|---|---|---|---|
| 1 | intraday_mom | 92%, +0.36R, n=39 | 89%, +0.31R, n=18 | 82%, +0.21R, n=22 |
| 2 | vwap_revert | 88%, +0.22R, n=33 | 68%, +0.00R, n=28 | 70%, -0.00R, n=20 |
| 3 | intraday_mom | 78%, +0.78R, n=37 | 62%, +0.36R, n=13 | 42%, -0.08R, n=19 |

Data: yahoo 5-minute bars, ES=F, NQ=F, YM=F, RTY=F, CL=F, GC=F, ZN=F, 6E=F. 4600 bots bred over 50 generations. Costs: 1 tick slippage on stops and market fills, round-trip commission per contract. A win is a trade with P&L above zero after costs.
- Free data is short: Yahoo keeps 5-minute bars for ~60 days (about 40 sessions) and 1-minute bars for ~30 days. That is a few weeks of one market regime; a bot bred on it can be luck.
- Continuous front-month prices: contract roll days can show a jump.
- Fills are simulated on bars: stop first if a bar hits both, targets need a trade through by one tick, 1 tick slippage on stops and market orders. Real fills can be worse in fast markets.
- Each instrument trades one contract independently; no portfolio margin or overlap limits.
- Alpaca does not trade futures. Trading these bots live needs a futures broker (e.g. Interactive Brokers, Tradovate, NinjaTrader), and they should be paper traded first.
- Strategy families are public, documented types (opening range breakout, VWAP reversion, intraday momentum, trend following). They are not any bank's proprietary strategies, which aren't public.
