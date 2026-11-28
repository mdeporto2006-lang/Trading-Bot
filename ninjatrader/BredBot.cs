// BredBot.cs: paper trades one bred bot on one instrument in NinjaTrader 8. SIM ACCOUNTS ONLY.
//
// Setup: docs/ninjatrader/SETUP.md. In short: copy this file and BredBotCore.cs into
// Documents\NinjaTrader 8\bin\Custom\Strategies\, compile (NinjaScript Editor, F5), run
// `python -m tradebot paper export`, then add one BredBot per bot and instrument on a 5-minute
// chart or from the Strategies tab, on a Sim account.
//
// What it does, every 5-minute bar close (same rules as the Python backtest, tradebot/breeder/sim.py):
//   - feeds the bar to BredBotCore, which decides entries, stop moves and exits;
//   - market entries go in right after the close (the backtest fills at the next bar's open);
//   - opening range breakouts use stop-entry orders (one-cancels-other for long and short);
//   - every filled entry gets a stop and a target at once (one-cancels-other);
//   - one contract, always. Flat before the session window ends.
//
// Safety, on top of the bot's own rules:
//   - refuses to run unless the account is a paper account: NinjaTrader's simulator (name starts
//     with "Sim", or "Playback" for market replay) or a Tradovate demo account (starts with "DEMO").
//     There is no setting to change this.
//   - daily loss limit per bot and for all bots together, max open positions, max trades a day
//     (from bots.txt; hard ceilings in BredBotRisk). A limit hit flattens and halts until tomorrow.
//   - kill switch: create the file Documents\NinjaTrader 8\tradebot\KILL or KILL.txt (or run
//     `python -m tradebot paper kill`). Within ~2 seconds every BredBot cancels its orders,
//     flattens and stops entering. Delete the file (`paper resume`) to allow trading the next day.
//   - an entry whose stop or target order is rejected is flattened at once.
//
// Every trade goes to Documents\NinjaTrader 8\tradebot\paper_trades.csv and every decision to
// events.csv, for `python -m tradebot paper compare` and `paper status`.

#region Using declarations
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using System.Globalization;
using System.IO;
using NinjaTrader.Cbi;
using NinjaTrader.Data;
using NinjaTrader.NinjaScript;
using NinjaTrader.NinjaScript.Strategies.BredBotLib;
#endregion

namespace NinjaTrader.NinjaScript.Strategies
{
    public class BredBot : Strategy
    {
        const string EntryName = "BB entry", OrbLongName = "BB orb long", OrbShortName = "BB orb short";
        const string StopName = "BB stop", TargetName = "BB target", ExitName = "BB exit";
        const string TradesHeader = "logged_utc,account,bot,bot_id,bot_key,status,symbol,instrument,day,dir,entry_time,exit_time,entry,exit,stop,target,reason,pnl_usd,r,commission_usd";
        const string EventsHeader = "time_utc,time_et,account,bot,instrument,event,detail";

        BredBotCore core;
        ContractSpec spec;
        string slot, botId, botKey, botStatus, killPath, tradesPath, eventsPath, instanceKey;
        int interval = 5;
        bool failed, killed;
        DateTime curDay = DateTime.MinValue;
        int lastBarStart = -1;
        TimeZoneInfo etZone;
        System.Threading.Timer timer;

        Order entryOrder, orbLong, orbShort, stopOrder, targetOrder, exitOrder;
        EntryIntent pendingIntent;
        bool tradeOpen, flattening;
        string closeReason;
        int tradeDir;
        double tradeEntry, tradeStop0, tradeTarget, tradeRisk;
        DateTime tradeEntryEt;

        [NinjaScriptProperty]
        [Display(Name = "Bot", Description = "Which bot from bots.txt: B1, B2, ... (or its rank or breeder id)", Order = 1, GroupName = "Bred bot")]
        public string Bot { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Bots file", Description = "Leave empty for Documents\\NinjaTrader 8\\tradebot\\bots.txt", Order = 2, GroupName = "Bred bot")]
        public string BotsFilePath { get; set; }

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Name = "BredBot";
                Description = "Paper trades a bot bred by tradebot (python -m tradebot breed). Sim accounts only.";
                Calculate = Calculate.OnBarClose;
                IsUnmanaged = true;
                BarsRequiredToTrade = 0;
                IsExitOnSessionCloseStrategy = true;
                ExitOnSessionCloseSeconds = 60;
                TraceOrders = false;
                Bot = "B1";
                BotsFilePath = "";
            }
            else if (State == State.Configure)
            {
                AddDataSeries(BarsPeriodType.Day, 1);  // daily bars for ATR, trend and prior close
            }
            else if (State == State.DataLoaded)
            {
                Setup();
            }
            else if (State == State.Realtime)
            {
                if (failed) return;
                Event("live", "watching " + Instrument.FullName + " for " + slot + " (" + botId + ", " + botStatus + ") on " + Account.Name);
                timer = new System.Threading.Timer(_ => { try { TriggerCustomEvent(o => SafetyCheck(), null); } catch (Exception) { } },
                                                   null, 2000, 2000);
            }
            else if (State == State.Terminated)
            {
                if (timer != null) { timer.Dispose(); timer = null; }
                if (instanceKey != null) BredBotRisk.Unregister(instanceKey);
                if (tradeOpen) Print("BredBot " + slot + " " + (Instrument != null ? Instrument.FullName : "")
                                     + ": STOPPED WITH A POSITION OPEN. Flatten it in NinjaTrader (Control Center > Positions).");
            }
        }

        /// <summary>NinjaTrader's own simulator (Sim..., Playback...) or a Tradovate/NinjaTrader demo account
        /// (DEMO...). Live accounts are never named like these. There is no setting to widen this.</summary>
        public static bool IsPaperAccount(string acct)
        {
            return acct.StartsWith("Sim", StringComparison.Ordinal) || acct.StartsWith("Playback", StringComparison.Ordinal)
                   || acct.StartsWith("DEMO", StringComparison.Ordinal);
        }

        void Fail(string why)
        {
            failed = true;
            string msg = "BredBot " + (Bot ?? "") + " on " + (Instrument != null ? Instrument.FullName : "?") + " will NOT trade: " + why;
            Print(msg);
            if (eventsPath != null) Event("refused", why);
        }

        void Setup()
        {
            string dir = Path.Combine(NinjaTrader.Core.Globals.UserDataDir, "tradebot");
            killPath = Path.Combine(dir, "KILL");
            tradesPath = Path.Combine(dir, "paper_trades.csv");
            eventsPath = Path.Combine(dir, "events.csv");
            foreach (var id in new[] { "Eastern Standard Time", "America/New_York" })
            {
                try { etZone = TimeZoneInfo.FindSystemTimeZoneById(id); break; }
                catch (Exception) { }
            }
            if (etZone == null) { Fail("can't find the US Eastern time zone on this PC"); return; }

            // 1. Simulation accounts only. Nothing here can trade real money.
            string acct = Account != null ? Account.Name : "";
            if (!IsPaperAccount(acct))
            {
                Fail("account '" + acct + "' is not a simulation or demo account (Sim101, SimB1, DEMO1234567, ...). Paper trading only.");
                return;
            }
            // 2. The bot and its contract
            string path = string.IsNullOrEmpty(BotsFilePath) ? Path.Combine(dir, "bots.txt") : BotsFilePath;
            if (!File.Exists(path)) { Fail("no bots file at " + path + ". Run: python -m tradebot paper export"); return; }
            BotsFile bf;
            Dictionary<string, string> bot;
            Genome g;
            try
            {
                bf = BotsFile.Load(path);
                bot = bf.FindBot(Bot, out slot);
                if (bot == null) { Fail("bot '" + Bot + "' is not in " + path); return; }
                g = BotsFile.GenomeOf(bot);
            }
            catch (Exception e) { Fail("can't read " + path + ": " + e.Message); return; }
            botId = BotsFile.Get(bot, "id", slot);
            botKey = BotsFile.Get(bot, "key", "");
            botStatus = BotsFile.Get(bot, "status", "candidate");
            interval = int.Parse(BotsFile.Get(bf.General, "interval", "5"), CultureInfo.InvariantCulture);
            string root = Instrument.MasterInstrument.Name;
            foreach (var sym in g.Symbols)
            {
                var s = bf.SpecFor(sym);
                if (s != null && s.NtRoot == root) spec = s;
            }
            if (spec == null)
            {
                var roots = new List<string>();
                foreach (var sym in g.Symbols) { var s = bf.SpecFor(sym); if (s != null) roots.Add(s.NtRoot); }
                Fail(slot + " doesn't trade " + root + ". Its instruments: " + string.Join(", ", roots));
                return;
            }
            if (Math.Abs(Instrument.MasterInstrument.PointValue - spec.Multiplier) > 1e-6
                || Math.Abs(Instrument.MasterInstrument.TickSize - spec.Tick) > 1e-9)
            {
                Fail(root + " point value/tick " + Instrument.MasterInstrument.PointValue + "/" + Instrument.MasterInstrument.TickSize
                     + " don't match the backtest's " + spec.Multiplier + "/" + spec.Tick);
                return;
            }
            if (BarsPeriod.BarsPeriodType != BarsPeriodType.Minute || BarsPeriod.Value != interval)
            {
                Fail("use a " + interval + "-minute data series (the bot was bred on " + interval + "-minute bars)");
                return;
            }
            Func<string, double, double> num = (k, d) =>
                double.Parse(BotsFile.Get(bf.General, k, d.ToString(CultureInfo.InvariantCulture)), CultureInfo.InvariantCulture);
            BredBotRisk.Configure(num("bot_daily_loss_usd", 250), num("all_bots_daily_loss_usd", 600),
                                  (int)num("max_open_positions", 6), (int)num("max_trades_all_bots", 40));
            if (g.MaxTradesDay > 3) g.MaxTradesDay = 3;
            core = new BredBotCore(g, spec, interval);
            instanceKey = acct + "|" + slot + "|" + Instrument.FullName;
            BredBotRisk.Register(instanceKey, acct + "|" + slot);
            Print("BredBot " + slot + " (" + botId + ") ready on " + Instrument.FullName + " / " + acct + ": " + BotsFile.Get(bot, "about", ""));
        }

        // ------------------------------------------------------------------ time
        DateTime ToEt(DateTime t) { return TimeZoneInfo.ConvertTime(t, NinjaTrader.Core.Globals.GeneralOptions.TimeZoneInfo, etZone); }
        DateTime NowEt() { return TimeZoneInfo.ConvertTime(DateTime.UtcNow, TimeZoneInfo.Utc, etZone); }
        // The bot's clock for risk limits and the kill switch: the latest bar or fill time it has seen
        // (in real time that's within a few minutes of now), so limits roll over on the bars' day.
        DateTime lastEt = DateTime.MinValue;
        DateTime Clock() { return lastEt == DateTime.MinValue ? NowEt() : lastEt; }
        static int Min(DateTime t) { return t.Hour * 60 + t.Minute; }
        static string Hm(int m) { return (m / 60).ToString("00") + ":" + (m % 60).ToString("00"); }
        string BotKey { get { return Account.Name + "|" + slot; } }

        void Event(string what, string detail)
        {
            DateTime et = etZone != null ? NowEt() : DateTime.Now;
            PaperLog.Append(eventsPath, EventsHeader, DateTime.UtcNow.ToString("yyyy-MM-dd HH:mm:ss"), et.ToString("yyyy-MM-dd HH:mm:ss"),
                            Account != null ? Account.Name : "", slot ?? Bot, Instrument != null ? Instrument.FullName : "", what, detail);
            Print("BredBot " + (slot ?? Bot) + " " + (Instrument != null ? Instrument.FullName : "") + ": " + what + " " + detail);
        }

        DayContext DailyContext(DateTime etDay)
        {
            var before = new List<double[]>();
            var b = BarsArray[1];
            for (int k = 0; k < b.Count; k++)
            {
                if (b.GetTime(k).Date >= etDay.Date) continue;
                before.Add(new[] { b.GetOpen(k), b.GetHigh(k), b.GetLow(k), b.GetClose(k) });
            }
            return DayContext.FromDaily(before);
        }

        // ------------------------------------------------------------------ bars
        protected override void OnBarUpdate()
        {
            if (failed || core == null || BarsInProgress != 0) return;
            DateTime startEt = ToEt(Time[0]).AddMinutes(-interval);  // NinjaTrader stamps bars at their END
            if (State == State.Realtime) lastEt = startEt.AddMinutes(interval);
            int m = Min(startEt);
            bool live = State == State.Realtime;
            if (startEt.Date != curDay)
            {
                curDay = startEt.Date;
                lastBarStart = -1;
                var dc = DailyContext(curDay);
                int need = Math.Max(15, core.G.TrendLookback + 1);
                bool ok = core.BeginSession(dc);
                if (live || State == State.Historical && curDay >= NowEt().Date)
                {
                    if (!ok) Event("no-trade-day", "not enough daily bars for ATR (need " + need + "). Set 'Days to load' to 150.");
                    else if (!dc.Trend.ContainsKey(core.G.TrendLookback) && (core.G.TrendFilter != "off" || core.G.Family == "trend_pullback"))
                        Event("warning", "not enough daily bars for the " + core.G.TrendLookback + "-day trend; set 'Days to load' to 150");
                }
            }
            if (!core.InSessionWindow(m))
            {
                if (live && tradeOpen && !flattening && m >= spec.CloseMin) Flatten("flatten");
                if (live && m >= spec.CloseMin) CancelEntries();
                return;
            }
            lastBarStart = m;
            int next = m + interval;
            var dec = core.OnBarClose(m, Open[0], High[0], Low[0], Close[0], Volume[0], next);
            if (!live)
            {
                // Historical bars only build today's state (VWAP, opening range). No orders, no trades.
                if (dec.ExitReason != null) { core.OnExited(); core.AfterCloseExit(next); }
                return;
            }
            if (tradeOpen && !flattening)
                BredBotRisk.OpenPnl(Clock(), instanceKey, PositionPnl(Close[0]));
            SafetyCheck();
            if (tradeOpen && !flattening && core.Pos != null && dec.StopMoved) MoveStop(core.Pos.Stop);
            if (dec.ExitReason != null)
            {
                if (tradeOpen && !flattening) Flatten(dec.ExitReason);
                else core.OnExited();
                dec = core.AfterCloseExit(next);
            }
            string block = EntryBlock();
            if (dec.Entry != null)
            {
                if (block != null) Event("skip", (dec.Entry.Dir > 0 ? "long" : "short") + " signal at " + Hm(m + interval) + ": " + block);
                else if (!core.WouldAccept(dec.Entry.Dir, Close[0] + dec.Entry.Dir * spec.Tick, dec.Entry.Stop, dec.Entry.Target))
                    Event("skip", "signal fails the stop/target sanity check at the current price");
                else
                {
                    Event("signal", (dec.Entry.Dir > 0 ? "BUY" : "SELL SHORT") + " 1 at market after the " + Hm(m + interval) + " close");
                    if (tradeOpen) deferredEntry = dec.Entry;  // the exit goes first; the entry follows its fill
                    else SubmitEntry(dec.Entry);
                }
            }
            ManageOrb(next, block);
        }

        EntryIntent deferredEntry;

        void SubmitEntry(EntryIntent e)
        {
            pendingIntent = e;
            entryOrder = SubmitOrderUnmanaged(0, e.Dir > 0 ? OrderAction.Buy : OrderAction.SellShort, OrderType.Market, 1, 0, 0, "", EntryName);
        }

        string EntryBlock()
        {
            if (killed) return "kill switch";
            if ((tradeOpen && !flattening) || entryOrder != null || deferredEntry != null) return "a trade or order is already open";
            return BredBotRisk.EntryBlock(Clock(), instanceKey, BotKey);
        }

        void ManageOrb(int nextStart, string block)
        {
            bool want = core.OrbWorking(nextStart) && block == null && !tradeOpen && entryOrder == null && deferredEntry == null;
            if (!want) { CancelOrb(); return; }
            if (orbLong != null || orbShort != null) return;  // already working
            var orb = core.Orb;
            string oco = "BBORB" + Guid.NewGuid().ToString("N").Substring(0, 12);
            bool structure = core.G.StopMode == "structure";
            // Same sanity check the backtest applies on the fill (stop 2 ticks to 1.5 ATR away), done before
            // arming: otherwise a breakout would fill, fail it, exit, and re-arm all day.
            bool lg = core.Allowed(1) && core.WouldAccept(1, Math.Max(orb.LongTrigger, Close[0]) + spec.Tick,
                                                          structure ? (double?)orb.LongStop : null, null);
            bool sh = core.Allowed(-1) && core.WouldAccept(-1, Math.Min(orb.ShortTrigger, Close[0]) - spec.Tick,
                                                           structure ? (double?)orb.ShortStop : null, null);
            if (!lg && !sh) return;
            double px = Close[0];
            if (lg && px >= orb.LongTrigger)
            {
                // already through the trigger: the backtest fills at the open, so go at market
                pendingIntent = new EntryIntent { Dir = 1, Stop = core.G.StopMode == "structure" ? (double?)orb.LongStop : null };
                entryOrder = SubmitOrderUnmanaged(0, OrderAction.Buy, OrderType.Market, 1, 0, 0, "", EntryName);
                Event("signal", "BUY 1 at market: price is already above the breakout level " + orb.LongTrigger);
                return;
            }
            if (sh && px <= orb.ShortTrigger)
            {
                pendingIntent = new EntryIntent { Dir = -1, Stop = core.G.StopMode == "structure" ? (double?)orb.ShortStop : null };
                entryOrder = SubmitOrderUnmanaged(0, OrderAction.SellShort, OrderType.Market, 1, 0, 0, "", EntryName);
                Event("signal", "SELL SHORT 1 at market: price is already below the breakout level " + orb.ShortTrigger);
                return;
            }
            if (lg) orbLong = SubmitOrderUnmanaged(0, OrderAction.Buy, OrderType.StopMarket, 1, 0, orb.LongTrigger, sh ? oco : "", OrbLongName);
            if (sh) orbShort = SubmitOrderUnmanaged(0, OrderAction.SellShort, OrderType.StopMarket, 1, 0, orb.ShortTrigger, lg ? oco : "", OrbShortName);
            Event("orb-armed", (lg ? "buy stop " + orb.LongTrigger : "") + (lg && sh ? ", " : "") + (sh ? "sell stop " + orb.ShortTrigger : ""));
        }

        void CancelOrb()
        {
            if (orbLong != null && IsWorking(orbLong)) CancelOrder(orbLong);
            if (orbShort != null && IsWorking(orbShort)) CancelOrder(orbShort);
        }

        void CancelEntries()
        {
            deferredEntry = null;
            CancelOrb();
            if (entryOrder != null && IsWorking(entryOrder)) CancelOrder(entryOrder);
        }

        static bool IsWorking(Order o)
        {
            return o.OrderState == OrderState.Working || o.OrderState == OrderState.Accepted || o.OrderState == OrderState.Submitted
                   || o.OrderState == OrderState.ChangePending || o.OrderState == OrderState.ChangeSubmitted
                   || o.OrderState == OrderState.Initialized || o.OrderState == OrderState.TriggerPending;
        }

        static bool IsDone(OrderState s)
        {
            return s == OrderState.Filled || s == OrderState.Cancelled || s == OrderState.Rejected || s == OrderState.Unknown;
        }

        double PositionPnl(double price)
        {
            return tradeOpen ? tradeDir * (price - tradeEntry) * spec.Multiplier - spec.CommissionRt : 0;
        }

        // ------------------------------------------------------------------ safety
        void SafetyCheck()
        {
            if (failed || core == null || State != State.Realtime) return;
            DateTime now = Clock();
            bool killFile = File.Exists(killPath) || File.Exists(killPath + ".txt");  // Windows hides ".txt"
            if (!killed && killFile)
            {
                killed = true;
                BredBotRisk.HaltAll(now, "kill switch file");
                Event("KILL", "kill switch file found: cancelling orders and flattening");
            }
            if (killed && !killFile && now.Date > curDay) killed = false;  // resumes the next day
            string why = killed ? "kill switch" : BredBotRisk.MustFlatten(now, BotKey);
            if (why == null) return;
            CancelEntries();
            if (tradeOpen && !flattening)
            {
                Event("halt", why);
                Flatten("halt");
            }
        }

        void MoveStop(double price)
        {
            if (stopOrder == null || !IsWorking(stopOrder)) return;
            // A stop at or through the market would be rejected; the backtest would be stopped out.
            if ((tradeDir > 0 && price >= Close[0]) || (tradeDir < 0 && price <= Close[0])) { Flatten("stop"); return; }
            ChangeOrder(stopOrder, 1, 0, price);
            Event("stop-moved", price.ToString(CultureInfo.InvariantCulture));
        }

        void Flatten(string reason)
        {
            if (!tradeOpen || flattening) return;
            flattening = true;
            closeReason = reason == "stop" && core.Pos != null ? core.ExitLabel("stop") : reason;
            core.OnExited();
            bool waiting = false;
            if (stopOrder != null && IsWorking(stopOrder)) { CancelOrder(stopOrder); waiting = true; }
            if (targetOrder != null && IsWorking(targetOrder)) { CancelOrder(targetOrder); waiting = true; }
            if (!waiting) SubmitExit();
        }

        void SubmitExit()
        {
            if (!tradeOpen || exitOrder != null) return;
            exitOrder = SubmitOrderUnmanaged(0, tradeDir > 0 ? OrderAction.Sell : OrderAction.BuyToCover, OrderType.Market, 1, 0, 0, "", ExitName);
        }

        // ------------------------------------------------------------------ orders and fills
        protected override void OnOrderUpdate(Order order, double limitPrice, double stopPrice, int quantity, int filled,
                                              double averageFillPrice, OrderState orderState, DateTime time, ErrorCode error, string comment)
        {
            if (failed || core == null) return;
            string n = order.Name;
            if (n == EntryName) entryOrder = order;
            else if (n == OrbLongName) orbLong = order;
            else if (n == OrbShortName) orbShort = order;
            else if (n == StopName) stopOrder = order;
            else if (n == TargetName) targetOrder = order;
            else if (n == ExitName) exitOrder = order;
            else return;

            if (orderState == OrderState.Rejected)
                Event("rejected", n + ": " + error + " " + comment);
            if (!IsDone(orderState) || orderState == OrderState.Filled) return;  // fills are handled in OnExecutionUpdate

            // cancelled / rejected
            if (n == EntryName) { entryOrder = null; pendingIntent = null; }
            else if (n == OrbLongName) orbLong = null;
            else if (n == OrbShortName) orbShort = null;
            else if (n == ExitName) { exitOrder = null; if (tradeOpen) SubmitExit(); }
            else if (n == StopName || n == TargetName)
            {
                if (n == StopName) stopOrder = null; else targetOrder = null;
                if (tradeOpen && !flattening && orderState == OrderState.Rejected)
                {
                    Event("unprotected", n + " was rejected, flattening now");
                    Flatten("rejected_bracket");
                    return;
                }
                if (tradeOpen && flattening && stopOrder == null && targetOrder == null) SubmitExit();
            }
        }

        protected override void OnExecutionUpdate(Execution execution, string executionId, double price, int quantity,
                                                  MarketPosition marketPosition, string orderId, DateTime time)
        {
            if (failed || core == null || execution.Order == null) return;
            Order o = execution.Order;
            if (o.OrderState != OrderState.Filled) return;  // one contract: wait for the full fill
            string n = o.Name;
            double fill = o.AverageFillPrice;
            DateTime et = ToEt(time);
            if (et > lastEt) lastEt = et;
            if (n == EntryName || n == OrbLongName || n == OrbShortName)
            {
                int d = (o.OrderAction == OrderAction.Buy) ? 1 : -1;
                EntryIntent it = pendingIntent;
                double? st = null, tg = null;
                if (n == EntryName && it != null) { st = it.Stop; tg = it.Target; }
                else if (n != EntryName && core.G.StopMode == "structure" && core.Orb != null)
                    st = d > 0 ? core.Orb.LongStop : core.Orb.ShortStop;
                entryOrder = null; pendingIntent = null;
                if (n == OrbLongName) orbLong = null;
                if (n == OrbShortName) orbShort = null;
                int formingStart = lastBarStart >= 0 ? lastBarStart + interval : Min(et);
                tradeOpen = true; flattening = false; tradeDir = d; tradeEntry = fill; tradeEntryEt = et;
                closeReason = null; exitOrder = null;
                BredBotRisk.Opened(et, instanceKey);
                if (!core.OnEntryFilled(d, fill, core.BarCount, formingStart, st, tg))
                {
                    tradeStop0 = 0; tradeTarget = 0; tradeRisk = 0;
                    Event("exit-now", "filled at " + fill + " but the stop/target fail the sanity check, exiting");
                    flattening = true; closeReason = "sanity";
                    SubmitExit();
                    return;
                }
                tradeStop0 = core.Pos.Stop0; tradeTarget = core.Pos.Target; tradeRisk = core.Pos.Risk;
                string oco = "BBX" + Guid.NewGuid().ToString("N").Substring(0, 12);
                Event("entered", (d > 0 ? "long" : "short") + " 1 @ " + fill + ", stop " + tradeStop0 + ", target " + tradeTarget);
                Order so = SubmitOrderUnmanaged(0, d > 0 ? OrderAction.Sell : OrderAction.BuyToCover, OrderType.StopMarket, 1, 0, tradeStop0, oco, StopName);
                if (stopOrder == null && so != null && !IsDone(so.OrderState)) stopOrder = so;
                if (!tradeOpen || flattening) return;  // the stop was rejected and we're already getting out
                Order to = SubmitOrderUnmanaged(0, d > 0 ? OrderAction.Sell : OrderAction.BuyToCover, OrderType.Limit, 1, tradeTarget, 0, oco, TargetName);
                if (targetOrder == null && to != null && !IsDone(to.OrderState)) targetOrder = to;
                return;
            }
            if (!tradeOpen) return;
            string reason;
            if (n == StopName) reason = core.Pos != null ? core.ExitLabel("stop") : "stop";
            else if (n == TargetName) reason = "target";
            else if (n == ExitName) reason = closeReason ?? "exit";
            else if ((tradeDir > 0 && o.OrderAction == OrderAction.Sell) || (tradeDir < 0 && o.OrderAction == OrderAction.BuyToCover))
                reason = "ninjatrader_" + n.Replace(' ', '_');  // e.g. NinjaTrader's exit on session close
            else return;
            if (n == StopName) stopOrder = null;
            if (n == TargetName) targetOrder = null;
            if (n == ExitName) exitOrder = null;
            // the OCO partner gets cancelled by NinjaTrader; also cancel anything left
            if (stopOrder != null && IsWorking(stopOrder)) CancelOrder(stopOrder);
            if (targetOrder != null && IsWorking(targetOrder)) CancelOrder(targetOrder);
            RecordTrade(fill, reason, et, n == ExitName);
        }

        void RecordTrade(double exitPx, string reason, DateTime et, bool marketExit)
        {
            double pnl = tradeDir * (exitPx - tradeEntry) * spec.Multiplier - spec.CommissionRt;
            double riskUsd = tradeRisk * spec.Multiplier;
            double r = riskUsd > 0 ? pnl / riskUsd : 0;
            PaperLog.Append(tradesPath, TradesHeader, DateTime.UtcNow.ToString("yyyy-MM-dd HH:mm:ss"), Account.Name, slot, botId, botKey,
                            botStatus, spec.Symbol, Instrument.FullName, tradeEntryEt.ToString("yyyy-MM-dd"), tradeDir > 0 ? "long" : "short",
                            tradeEntryEt.ToString("HH:mm:ss"), et.ToString("HH:mm:ss"), tradeEntry, exitPx, tradeStop0, tradeTarget,
                            reason, Math.Round(pnl, 2), Math.Round(r, 4), spec.CommissionRt);
            Event("exited", reason + " @ " + exitPx + ": " + pnl.ToString("+0.00;-0.00", CultureInfo.InvariantCulture) + " USD, "
                  + r.ToString("+0.00;-0.00", CultureInfo.InvariantCulture) + "R");
            BredBotRisk.Closed(et, instanceKey, BotKey, pnl);
            tradeOpen = false; flattening = false; closeReason = null;
            if (core.Pos != null) core.OnExited();
            if (deferredEntry != null)
            {
                var e = deferredEntry;
                deferredEntry = null;
                if (EntryBlock() == null) SubmitEntry(e);
                else Event("skip", "entry after the exit blocked: " + EntryBlock());
            }
            // After a market exit (decided on the last close), the backtest's breakout orders are already
            // working on this bar, so arm them now rather than at the next close.
            if (marketExit && core.G.Family == "orb" && State == State.Realtime && lastBarStart >= 0)
                ManageOrb(lastBarStart + interval, EntryBlock());
        }
    }
}
