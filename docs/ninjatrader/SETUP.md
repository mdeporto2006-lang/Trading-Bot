# Paper trading the bred bots on your Tradovate demo account (NinjaTrader 8)

The bots run inside **NinjaTrader 8 Desktop** (free) as a strategy called **BredBot**, logged in to
your **Tradovate demo account** (NinjaTrader and Tradovate are the same company). No API keys
are involved, so there's nothing secret to store anywhere.

**Paper only.** BredBot refuses to start on any account whose name doesn't begin with `DEMO`
(Tradovate/NinjaTrader demo), `Sim` (NinjaTrader's own simulator, e.g. `Sim101`) or `Playback`
(market replay). There's no setting that changes this. Live trading (one micro contract, step 4 of
the plan) will be separate work, and only after the paper test.

## Why not the Tradovate API?

Tradovate's API needs a **funded live account with $1,000+** and the **API add-on ($25/month)**,
and real-time prices through the API need a **CME data license (~$390/month)**. Those are the
terms reported in Nov 2026 by third-party guides, since Tradovate's own help page wouldn't load.
NinjaTrader Desktop reaches the same demo account for free.

## What it costs

- NinjaTrader 8 Desktop and the demo account: free.
- **Live prices: free for the first 14 days.** After that NinjaTrader shows delayed data unless you
  pay for a real-time CME feed (about $12/month for non-professional Level 1, per third-party
  guides). **Delayed data won't work for this test**: the bots would act 10 minutes late while the
  demo fills at current prices. So the first 14 days are free. The rest of the 4–6 week test needs
  that feed, and that's your decision when the time comes.
- If your Tradovate demo itself expires, use NinjaTrader's built-in `Sim101` account instead. It
  never expires and BredBot accepts it.

## One-time setup (about 20 minutes, Windows)

### 1. Install NinjaTrader 8 and connect your demo account

1. Download **NinjaTrader Desktop** from ninjatrader.com (Platform → Download), install it, and
   log in with your NinjaTrader/Tradovate username and password.
2. If your demo account isn't listed under **Control Center → Accounts**:
   - **Tools → Options → General**: tick **Multi-provider**, then restart NinjaTrader.
   - **Connections → Configure**: add a **NinjaTrader** connection (in some versions it's called
     "My NinjaTrader"), set it to **Simulation** (demo), enter your login, and press OK.
   - **Connections →** click that connection to connect.
   - Your `DEMO…` account should now show under **Accounts**. NinjaTrader staff give these steps on
     their support forum.
3. Check you have live prices: **New → Chart**, type `MES` and pick the contract NinjaTrader offers
   first (right now `MES 12-26`). Choose 5 Minute, press OK, and watch the last bar move during
   market hours.

### 2. Install the BredBot strategy

1. Download these two files from the repository's `ninjatrader/` folder:
   - `BredBot.cs`
   - `BredBotCore.cs`
2. Put them in `Documents\NinjaTrader 8\bin\Custom\Strategies\`.
   (If you have Python and this repo on the PC, `python -m tradebot paper install` does this.)
3. In NinjaTrader: **New → NinjaScript Editor**. Open **Strategies → BredBot** and press **F5**
   (compile). The bottom of the editor should show no errors. If it shows errors, send a
   screenshot. The checks here compile the code against stand-ins for NinjaTrader, so
   NinjaTrader's own compiler gets the final word.

### 3. Give it the bots

1. Download `ninjatrader/export/bots.txt` from the repository. The nightly breeder refreshes it.
2. Create the folder `Documents\NinjaTrader 8\tradebot\` and put `bots.txt` in it.
3. Open `bots.txt` in Notepad. Each `[bot B1]` section has an `about=` line saying what the bot does,
   and `gene.symbols=` lists its markets. The instruments to use are in the `[symbol …]` sections
   (`nt_root=MES` and so on).

### 4. Turn the bots on

For **each bot and each of its instruments** (right now: bot B1 on MCL, MES, MNQ, M2K, MYM, which
makes 5 strategies):

1. **Control Center → Strategies tab →** right-click → **New Strategy…** → pick **BredBot**.
2. Set:
   - **Instrument**: the root from bots.txt (e.g. `MES`). Pick the contract NinjaTrader offers
     first, the front month.
   - **Data series**: **5 Minute**.
   - **Days to load: 150**. It needs about 60 days of daily bars for its trend filter. Fewer,
     and it logs a warning and may skip trading.
   - **Bot**: `B1` (or B2, B3 … for other bots).
   - **Account**: your `DEMO…` account (or `Sim101`).
   - Leave **Bots file** empty.
3. Tick **Enabled** and press OK.
4. Open **New → NinjaScript Output**. Each strategy prints a line like
   `BredBot B1 (G48-4395) ready on MES 12-26 / DEMO1234567: follow the first 45 min move …`.
   If one says **will NOT trade**, it gives the reason (wrong account, wrong instrument, bots.txt
   missing).
5. **File → Workspaces → Save** so the setup comes back after a restart.

## Every trading day

- Keep NinjaTrader open and connected from about **8:15 am to 4:05 pm New York time** (the bots
  trade 8:20 am–4:00 pm ET, depending on the market). Stop the PC from sleeping
  (Settings → System → Power → Sleep: Never, while plugged in).
- If NinjaTrader restarts, re-enable the strategies in the **Strategies** tab (tick Enabled).
- Each bot is flat before its market's session window ends, so it never holds overnight.

## Safety: limits and kill switch

Built into the strategy (the numbers come from bots.txt, with hard ceilings in the code):

| Limit | Default |
|---|---|
| Contracts per trade | 1, always (micro contracts; ZN has no micro, so it's full size) |
| Daily loss per bot, all its instruments together | $250, then that bot flattens and stops for the day |
| Daily loss for all bots together | $600, then everything flattens and stops for the day |
| Open positions at once, all bots | 6 |
| Trades per day, all bots | 40 |
| Unprotected position | if a stop order is rejected, the position is closed at once |

**Kill switch:** create a file named `KILL` (or `KILL.txt`) in `Documents\NinjaTrader 8\tradebot\`.
To do it, right-click in the folder → New → Text Document → name it KILL. Within about 2 seconds
every BredBot cancels its orders, closes its position and stops trading. Delete the file to allow
trading again from the next day. `python -m tradebot paper kill` / `paper resume` do the same.

Belt and braces: in NinjaTrader, **Control Center → Positions → right-click → Flatten Everything**
closes everything on the account at once. Your Tradovate app shows the same demo account, so
you can also flatten from your phone.

## Checking results against the backtest

The strategy writes every trade to `Documents\NinjaTrader 8\tradebot\paper_trades.csv` and every
decision to `events.csv`. Once a week, send `paper_trades.csv` in the project chat, or run:

```
python -m tradebot paper status     # today's trades, P&L, kill switch, recent events
python -m tradebot paper compare    # each bot's paper results vs its backtest
```

`compare` reports, per bot:

- win rate and R per trade on paper;
- the backtest's R per trade, and the 95% range the paper average should land in for that many
  trades;
- a verdict ("in line", "WORSE than the backtest", "too few trades");
- the same days re-run through the backtest and paired trade by trade, with matched and missed
  trades and the average entry slippage in ticks.

Judge after 4–6 weeks, not days. A bot that lands below its range gets dropped, not tuned.

## What can differ from the backtest

- The backtest fills on 5-minute bars, and real fills can be worse in fast markets. That's what
  the paper test measures.
- Exits at the end of the session window and time stops go at market right after the bar closes
  (the backtest uses the close price).
- On a breakout bar that hits both the long and short levels, the backtest skips the trade, but
  live the first level touched fills.
- Prices: the bots were bred on Yahoo's continuous series. NinjaTrader trades a specific contract
  month, so after a roll (NinjaTrader shows a rollover notice), re-add the strategies on the new
  month.
- The strategy's decision code makes **exactly** the same trades as the Python backtest on the
  same bars. CI checks this on every change (`.github/workflows/ninjatrader.yml`).
