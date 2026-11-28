# Trading-Bot

A disciplined **day-trading** bot. Claude plans trades as JSON; code checks every
plan against hard rules and only then places orders. The default answer is
always `NO_TRADE`.

> **Paper trading by default.** Live trading is off unless you turn it on in two
> places (see [Live trading](#live-trading)). Nothing here is financial advice;
> automated trading can lose money quickly. Run it on paper for a long time first.

## How it works

```
watchlist ─► market data (Alpaca) ─► snapshot ─► Claude planner ─► risk gate ─► bracket order
                                         │                            ▲
                                         └──── code's own checklist ──┘
```

1. **Snapshot** (`tradebot/market_data.py`): for each ticker, fetch this run's
   daily bars, today's 1-minute bars, the latest quote and the last 48h of news
   from Alpaca. Compute VWAP, ATR(14), 20-day average volume, pace-adjusted
   relative volume, spread, and support/resistance levels (5-minute swing
   pivots, today's high/low, yesterday's high/low/close). Anything that can't be
   fetched is listed as missing, which means `NO_TRADE`.
2. **Planner** (`tradebot/planner.py`): sends the snapshot to Claude
   (`claude-opus-5-5`) with your trading rules as the system prompt, and gets
   back one JSON plan in your output format, enforced with structured outputs.
   News is wrapped in `<untrusted_news>` tags and treated as data, never
   instructions. Errors, refusals and unparseable output all become `NO_TRADE`.
3. **Risk gate** (`tradebot/risk.py`, `tradebot/checklist.py`): code does not
   trust the planner's checklist. It recomputes every item from the snapshot and
   also checks price geometry, confidence, the entry window, account limits,
   borrow availability for shorts, and position size. Any failure means
   `NO_TRADE`.
4. **Execution** (`tradebot/broker.py`): an approved plan becomes a stop-limit
   **bracket order** (entry trigger + take-profit + stop-loss) on Alpaca.
5. **Management** (`tradebot/runner.py`): every 30s, once a trade is up 1R the
   stop trails 1R behind price (it only ever tightens). Unfilled entries are
   cancelled at 15:30 ET, and everything is flattened at **15:50 ET**.

Every plan and decision is written to `journal/trades.jsonl`.

## The checklist (DAY mode)

All thresholds are in `config.yaml` (see `config.example.yaml`). Defaults are the
examples from the original trading prompt:

| # | Item | Default rule |
|---|------|--------------|
| 1 | Catalyst | news less than 24h old (Claude also judges whether it is material) |
| 2 | Relative volume | > 2x the 20-day average, adjusted for time of day |
| 3 | Liquidity | average daily volume > 1M **and** spread < 0.1% |
| 4 | Trend | price above VWAP for BUY, below for SHORT |
| 5 | Clean level | a support (BUY) / resistance (SHORT) within 1.5 ATR of entry, with the stop beyond it |
| 6 | Reward:risk | >= 2:1, with the target capped at the next level |

Exits: stop beyond the level or 1.5x ATR, target the next level or 2R, trail
after 1R, flat by 15:50 ET, no overnight holds. Risk limits: 0.5% of equity at
risk per trade, max 20% of equity per position, max 3 open positions, max 5
trades a day, no new trades after a 2% daily loss.

## Setup

1. Python 3.11+.
   ```bash
   python -m venv .venv && . .venv/bin/activate
   pip install -r requirements-dev.txt
   ```
2. Create an [Alpaca](https://alpaca.markets) account and get **paper trading**
   API keys. The free plan's IEX data feed works; set `data_feed: sip` if you
   pay for full market data.
3. Get a Claude API key from [platform.claude.com](https://platform.claude.com).
4. Configure:
   ```bash
   cp .env.example .env               # fill in the keys
   cp config.example.yaml config.yaml # edit watchlist and thresholds
   set -a; . ./.env; set +a
   ```

## Usage

```bash
# Plan and risk-check tickers once. Prints plan + gate decision as JSON.
# Never places orders, in any mode.
python -m tradebot plan AAPL NVDA

# Run the trading loop for the day (paper by default).
python -m tradebot run            # uses trading.watchlist
python -m tradebot run AAPL TSLA

# Emergency: cancel all orders and close all positions.
python -m tradebot flatten
```

Set `trading.mode: dry_run` to run the full loop while only logging the orders
it would place.

**Cost:** each scan makes one Claude call per ticker that has complete data
(tickers with missing data are skipped without a call). With the default 5-minute
scan interval and a 4-ticker watchlist that's up to about 280 calls a day.
Raise `scan_interval_seconds` or shorten the watchlist to spend less.

## Self-improvement

The bot learns from its own results, but it cannot quietly rewrite its rules.

1. **Score.** After the close, every plan Claude priced (taken or not) is
   replayed on the minute bars that followed: did it hit the stop, the target,
   or get flattened, and for how many R? Results go to `journal/outcomes.jsonl`.
2. **Review.** Claude gets a numbers-only table of those results (no news text)
   and proposes up to a few setting changes plus short "lessons" for the planner.
3. **Test.** Code checks each change against an allowlist with bounds, then
   replays the last 30 days of plans with the new settings. A change passes only
   if it beats the current settings by at least 0.05R per trade over at least 20
   trades, without a worse drawdown.
4. **Adopt.** By default you approve: `python -m tradebot approve <id>`.
   Failed proposals can't be adopted. Set `improvement.auto_apply: true` to let
   passed proposals adopt themselves.
5. **Undo.** Every adopted version is saved in `tuning/history/`.
   `python -m tradebot rollback` goes back one version; `rollback --to 0`
   returns to exactly your `config.yaml`.

What it can tune: catalyst age, relative volume, average volume, spread, minimum
R:R, level distance, trailing settings and minimum confidence. Checklist bounds
never go looser than your original rules (2:1 R:R, 1M volume, 0.1% spread, 2x
relative volume, 24h catalyst). It can never touch the risk limits, trading
mode, the live switch, the 3:50pm flatten, or the model. Lessons are shown to
the planner but can't relax the checklist, because code still checks every
rule itself.

`run` scores the day and drafts a proposal automatically after flattening (one
extra Claude call per day). You can also run `score`, `review` and `proposals`
by hand.

## Live trading

Live trading needs **both**:

1. `trading.mode: live` in `config.yaml`, and
2. the environment variable `TRADEBOT_ALLOW_LIVE=I_UNDERSTAND_THIS_USES_REAL_MONEY`.

Without both, the bot refuses to start in live mode. Use live API keys only when
you mean it.

## Tests

```bash
python -m pytest
```

Tests use fake bars, quotes and news and a fake Claude client, so they need no
keys and make no network calls.

## Known limits

- Relative volume is today's volume pace against the 20-day average daily
  volume, scaled by time of day. Volume is heavy at the open, so it reads high
  early; entries wait until 09:45 ET by default.
- Support/resistance comes from simple swing pivots and prior-day levels.
- The catalyst check in code only confirms fresh news exists; whether it matters
  is Claude's judgment.
- SWING mode from the original prompt is not built; the code is DAY-only.
- Replay only covers plans Claude priced (BUY/SHORT), so it can judge
  tightening a rule far better than loosening one. Replays use bar data, not
  real fills, and assume a stop hits first when one bar touches both.
