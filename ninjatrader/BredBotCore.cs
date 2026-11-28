// BredBotCore.cs: the decision logic of a bred bot, with no NinjaTrader types in it.
//
// This is a line-by-line port of tradebot/breeder/sim.py (steps 4 and 5: managing an
// open trade on each bar close, and the entry signals). Fills are NOT done here: the
// NinjaTrader strategy (BredBot.cs) turns decisions into real sim orders, and the
// parity harness (tools/parity) fills them exactly like sim.py does, so CI can check
// that this file makes the same decisions as the Python backtest.
//
// Written for C# 6 and .NET Framework 4.8 (what NinjaTrader 8 compiles), so: no tuples,
// no pattern matching, no local functions, no GetValueOrDefault.
//
// Copy this file and BredBot.cs into Documents\NinjaTrader 8\bin\Custom\Strategies\.

using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;

namespace NinjaTrader.NinjaScript.Strategies.BredBotLib
{
    public sealed class Genome
    {
        public string Family;
        public string Direction = "both";
        public string TrendFilter = "off";
        public int TrendLookback = 20;
        public int EntryStartMin;
        public int EntryEndMin = 30;
        public int FlattenBeforeMin = 10;
        public int MaxTradesDay = 1;
        public string StopMode = "atr";
        public double StopAtr = 0.2;
        public string TargetMode = "r";
        public double TargetR = 1.0;
        public double BreakevenR;
        public double TrailAfterR;
        public double TrailDistR = 1.0;
        public int TimeStopBars;
        public int OrMinutes = 30;
        public int OrbBufferTicks = 1;
        public double OrMinAtr;
        public double OrMaxAtr = 1.0;
        public double DevK = 2.0;
        public bool VwapConfirm;
        public int VwapMinBars = 6;
        public int MomWindowMin = 30;
        public double MomThresholdAtr;
        public string MomFrom = "prev_close";
        public double PullTouchAtr;
        public int PullAboveBars = 6;
        public List<string> Symbols = new List<string>();

        static double D(string s) { return double.Parse(s, NumberStyles.Float, CultureInfo.InvariantCulture); }
        static int I(string s) { return (int)Math.Round(D(s)); }
        static bool B(string s) { s = s.Trim().ToLowerInvariant(); return s == "true" || s == "1" || s == "yes"; }

        /// <summary>Genes as written by `python -m tradebot paper export` (gene name = value).</summary>
        public static Genome FromDict(IDictionary<string, string> g)
        {
            var x = new Genome();
            foreach (var kv in g)
            {
                string v = kv.Value.Trim();
                switch (kv.Key)
                {
                    case "family": x.Family = v; break;
                    case "direction": x.Direction = v; break;
                    case "trend_filter": x.TrendFilter = v; break;
                    case "trend_lookback": x.TrendLookback = I(v); break;
                    case "entry_start_min": x.EntryStartMin = I(v); break;
                    case "entry_end_min": x.EntryEndMin = I(v); break;
                    case "flatten_before_min": x.FlattenBeforeMin = I(v); break;
                    case "max_trades_day": x.MaxTradesDay = I(v); break;
                    case "stop_mode": x.StopMode = v; break;
                    case "stop_atr": x.StopAtr = D(v); break;
                    case "target_mode": x.TargetMode = v; break;
                    case "target_r": x.TargetR = D(v); break;
                    case "breakeven_r": x.BreakevenR = D(v); break;
                    case "trail_after_r": x.TrailAfterR = D(v); break;
                    case "trail_dist_r": x.TrailDistR = D(v); break;
                    case "time_stop_bars": x.TimeStopBars = I(v); break;
                    case "or_minutes": x.OrMinutes = I(v); break;
                    case "orb_buffer_ticks": x.OrbBufferTicks = I(v); break;
                    case "or_min_atr": x.OrMinAtr = D(v); break;
                    case "or_max_atr": x.OrMaxAtr = D(v); break;
                    case "dev_k": x.DevK = D(v); break;
                    case "vwap_confirm": x.VwapConfirm = B(v); break;
                    case "vwap_min_bars": x.VwapMinBars = I(v); break;
                    case "mom_window_min": x.MomWindowMin = I(v); break;
                    case "mom_threshold_atr": x.MomThresholdAtr = D(v); break;
                    case "mom_from": x.MomFrom = v; break;
                    case "pull_touch_atr": x.PullTouchAtr = D(v); break;
                    case "pull_above_bars": x.PullAboveBars = I(v); break;
                    case "symbols":
                        x.Symbols = new List<string>();
                        foreach (var s in v.Split(','))
                            if (s.Trim().Length > 0) x.Symbols.Add(s.Trim());
                        break;
                }
            }
            if (x.Family != "orb" && x.Family != "vwap_revert" && x.Family != "intraday_mom" && x.Family != "trend_pullback")
                throw new ArgumentException("unknown family: " + x.Family);
            return x;
        }
    }

    /// <summary>One contract's numbers, from the export file (same as tradebot/breeder/contracts.py).</summary>
    public sealed class ContractSpec
    {
        public string Symbol;      // Yahoo symbol the bot was bred on, e.g. ES=F
        public string NtRoot;      // what NinjaTrader trades, e.g. MES
        public double Tick;
        public double Multiplier;  // dollars per 1.00 of price for NtRoot
        public double CommissionRt;
        public int OpenMin;        // session window, minutes after midnight ET
        public int CloseMin;
    }

    /// <summary>What was known before the session opened: last daily close, 14-day ATR, trend.</summary>
    public sealed class DayContext
    {
        public double? PrevClose;
        public double? Atr;
        public Dictionary<int, double> Trend = new Dictionary<int, double>();
        public static readonly int[] Lookbacks = { 5, 10, 20, 60 };

        /// <summary>Same math as tradebot/breeder/data.py: completed daily bars strictly before the day,
        /// oldest first. ATR is the plain mean of the last 14 true ranges over the last 30 days.</summary>
        public static DayContext FromDaily(IList<double[]> before)  // each: {open, high, low, close}
        {
            var dc = new DayContext();
            if (before.Count == 0) return dc;
            dc.PrevClose = before[before.Count - 1][3];
            int start = Math.Max(0, before.Count - 30);
            var last = new List<double[]>();
            for (int i = start; i < before.Count; i++) last.Add(before[i]);
            if (last.Count >= 15)
            {
                var trs = new List<double>();
                for (int i = 1; i < last.Count; i++)
                {
                    double h = last[i][1], l = last[i][2], pc = last[i - 1][3];
                    trs.Add(Math.Max(h - l, Math.Max(Math.Abs(h - pc), Math.Abs(l - pc))));
                }
                double sum = 0;
                for (int i = trs.Count - 14; i < trs.Count; i++) sum += trs[i];
                dc.Atr = sum / 14;
            }
            foreach (int lb in Lookbacks)
                if (before.Count > lb)
                    dc.Trend[lb] = (before[before.Count - 1][3] / before[before.Count - 1 - lb][3] - 1) * 100;
            return dc;
        }
    }

    public sealed class Position
    {
        public int Dir;            // +1 long, -1 short
        public double Entry;
        public double Stop;
        public double Stop0;
        public double Risk;        // price distance entry -> first stop
        public double Target;
        public int BarIndex;       // session bar index the entry filled on
        public int EntryT;         // minutes after midnight ET
        public bool Be;
    }

    /// <summary>A market entry decided on a bar close, to be sent for the next bar's open.</summary>
    public sealed class EntryIntent
    {
        public int Dir;
        public double? Stop;       // structure stop; null = ATR stop from the fill
        public double? Target;     // VWAP target; null = R target from the fill
    }

    /// <summary>Breakout stop-entry orders for the opening range breakout family.</summary>
    public sealed class OrbLevels
    {
        public double LongTrigger, ShortTrigger, LongStop, ShortStop;
        public bool Armed;         // false: the range filter failed, no trade today
    }

    public sealed class CloseDecision
    {
        public string ExitReason;  // "flatten" | "time" | null
        public bool StopMoved;
        public EntryIntent Entry;  // market entry for the next bar, or null
    }

    public sealed class BredBotCore
    {
        public readonly Genome G;
        public readonly ContractSpec Spec;
        public readonly int Interval;

        // session state
        DayContext ctx;
        double atr;
        int tsign;
        public int TradesToday { get; private set; }
        public OrbLevels Orb { get; private set; }
        bool momDone;
        public Position Pos;
        readonly List<double> o = new List<double>(), h = new List<double>(), l = new List<double>(), c = new List<double>();
        readonly List<int> t = new List<int>();
        readonly List<double> vw = new List<double>(), sd = new List<double>();
        double pv, vv, sq;
        public bool HasSession { get; private set; }

        public BredBotCore(Genome g, ContractSpec spec, int interval)
        {
            G = g; Spec = spec; Interval = interval;
        }

        public int WinFrom { get { return Spec.OpenMin + G.EntryStartMin; } }
        public int WinTo { get { return Spec.CloseMin - G.EntryEndMin; } }
        public int FlatAt { get { return Spec.CloseMin - G.FlattenBeforeMin; } }
        public double Atr { get { return atr; } }
        public int BarCount { get { return t.Count; } }

        /// <summary>A bar (by its START time) belongs to the session window.</summary>
        public bool InSessionWindow(int barStartMin)
        {
            return Spec.OpenMin <= barStartMin && barStartMin + Interval <= Spec.CloseMin;
        }

        /// <summary>Start a new session. Returns false when the day can't be traded (no ATR).</summary>
        public bool BeginSession(DayContext dc)
        {
            ctx = dc;
            HasSession = dc != null && dc.Atr.HasValue && dc.Atr.Value > 0;
            atr = HasSession ? dc.Atr.Value : 0;
            double tr;
            tsign = (dc != null && dc.Trend.TryGetValue(G.TrendLookback, out tr) && tr != 0) ? (tr > 0 ? 1 : -1) : 0;
            TradesToday = 0; Orb = null; momDone = false; Pos = null;
            o.Clear(); h.Clear(); l.Clear(); c.Clear(); t.Clear(); vw.Clear(); sd.Clear();
            pv = vv = sq = 0;
            return HasSession;
        }

        public bool Allowed(int d)
        {
            if (G.Direction == "long" && d < 0 || G.Direction == "short" && d > 0) return false;
            if (G.Family == "trend_pullback") return d == tsign;
            if (G.TrendFilter == "with") return d == tsign;
            if (G.TrendFilter == "against") return d == -tsign;
            return true;
        }

        public bool InWindow(int barStartMin)
        {
            return WinFrom <= barStartMin && barStartMin < WinTo && barStartMin + Interval <= FlatAt;
        }

        bool Sane(double risk) { return 2 * Spec.Tick <= risk && risk <= 1.5 * atr; }

        public static double RoundTick(double x, double tick)
        {
            return Math.Round(Math.Round(x / tick, MidpointRounding.ToEven) * tick, 10);
        }

        /// <summary>Would an entry at this price pass the sanity checks? (used before sending an order)</summary>
        public bool WouldAccept(int d, double fill, double? stop, double? target)
        {
            double st = RoundTick(stop.HasValue ? stop.Value : fill - d * G.StopAtr * atr, Spec.Tick);
            double risk = d * (fill - st);
            if (!Sane(risk)) return false;
            double tg = RoundTick(target.HasValue ? target.Value : fill + d * G.TargetR * risk, Spec.Tick);
            return d * (tg - fill) >= Spec.Tick;
        }

        /// <summary>An entry filled. Sets the stop and target (sim.py open_pos). Returns false if the
        /// trade fails the sanity checks, in which case the caller must exit at once.</summary>
        public bool OnEntryFilled(int d, double fill, int barIndex, int barStartMin, double? stop, double? target)
        {
            double st = RoundTick(stop.HasValue ? stop.Value : fill - d * G.StopAtr * atr, Spec.Tick);
            double risk = d * (fill - st);
            if (!Sane(risk)) return false;
            double tg = RoundTick(target.HasValue ? target.Value : fill + d * G.TargetR * risk, Spec.Tick);
            if (d * (tg - fill) < Spec.Tick) return false;
            Pos = new Position { Dir = d, Entry = fill, Stop = st, Stop0 = st, Risk = risk, Target = tg,
                                 BarIndex = barIndex, EntryT = barStartMin, Be = false };
            TradesToday++;
            return true;
        }

        public void OnExited() { Pos = null; }

        /// <summary>Exit label, as sim.py close(): a stop that had moved in our favor is
        /// "breakeven" or "trail".</summary>
        public string ExitLabel(string reason)
        {
            var p = Pos;
            if (p != null && reason == "stop" && ((p.Dir > 0 && p.Stop > p.Stop0) || (p.Dir < 0 && p.Stop < p.Stop0)))
                return p.Be && Math.Abs(p.Stop - p.Entry) < Spec.Tick / 2 ? "breakeven" : "trail";
            return reason;
        }

        public double PnlUsd(int d, double entry, double exit) { return d * (exit - entry) * Spec.Multiplier - Spec.CommissionRt; }

        /// <summary>Should the breakout stop-entry orders be working during the bar starting at barStartMin?</summary>
        public bool OrbWorking(int barStartMin)
        {
            return G.Family == "orb" && Orb != null && Orb.Armed && Pos == null
                   && TradesToday < G.MaxTradesDay && InWindow(barStartMin);
        }

        /// <summary>Feed one closed session bar (sim.py steps 4 and 5). Fills for this bar must already
        /// have been applied. nextBarStart: start of the next bar, or null if this is the last one.</summary>
        public CloseDecision OnBarClose(int barStart, double bo, double bh, double bl, double bc, double bv, int? nextBarStart)
        {
            var dec = new CloseDecision();
            int i = t.Count;
            t.Add(barStart); o.Add(bo); h.Add(bh); l.Add(bl); c.Add(bc);
            double tp = (bh + bl + bc) / 3;
            double vol = bv > 0 ? bv : 1.0;
            pv += tp * vol; vv += vol;
            double w = pv / vv;
            vw.Add(w);
            sq += (bc - w) * (bc - w);
            sd.Add(Math.Sqrt(sq / (i + 1)));
            if (!HasSession) return dec;
            double tick = Spec.Tick;
            int iv = Interval;

            // 4. manage the open trade on the close
            if (Pos != null)
            {
                int d = Pos.Dir;
                double risk = Pos.Risk;
                double moved = d * (bc - Pos.Entry);
                if (G.BreakevenR != 0 && !Pos.Be && moved >= G.BreakevenR * risk)
                {
                    if (d * (Pos.Entry - Pos.Stop) > 0) { Pos.Stop = Pos.Entry; dec.StopMoved = true; }
                    Pos.Be = true;
                }
                if (G.TrailAfterR != 0 && moved >= G.TrailAfterR * risk)
                {
                    double nw = RoundTick(bc - d * G.TrailDistR * risk, tick);
                    if (d * (nw - Pos.Stop) > 0) { Pos.Stop = nw; dec.StopMoved = true; }
                }
                if (barStart + iv >= FlatAt || !nextBarStart.HasValue)
                    dec.ExitReason = "flatten";
                else if (G.TimeStopBars != 0 && i - Pos.BarIndex + 1 >= G.TimeStopBars)
                    dec.ExitReason = "time";
                if (dec.ExitReason != null)
                    return dec;  // the caller closes at this bar's close; signals below see a flat book
            }
            return Signals(dec, i, nextBarStart);
        }

        /// <summary>Call after an exit decided by OnBarClose has been applied, to let step 5 run on the
        /// same bar (sim.py closes inside step 4 and then looks for a new signal).</summary>
        public CloseDecision AfterCloseExit(int? nextBarStart)
        {
            return Signals(new CloseDecision(), t.Count - 1, nextBarStart);
        }

        CloseDecision Signals(CloseDecision dec, int i, int? nextBarStart)
        {
            double tick = Spec.Tick;
            int iv = Interval;
            string fam = G.Family;
            // 5. signals from this bar's close (act next bar)
            if (fam == "orb" && Orb == null && t[i] + iv >= Spec.OpenMin + G.OrMinutes)
            {
                double hi = double.MinValue, lo = double.MaxValue;
                for (int j = 0; j <= i; j++) { hi = Math.Max(hi, h[j]); lo = Math.Min(lo, l[j]); }
                double rng = hi - lo;
                if (G.OrMinAtr * atr <= rng && rng <= G.OrMaxAtr * atr)
                {
                    double b = G.OrbBufferTicks * tick;
                    Orb = new OrbLevels { LongTrigger = hi + b, ShortTrigger = lo - b, LongStop = lo - tick,
                                          ShortStop = hi + tick, Armed = true };
                }
                else
                    Orb = new OrbLevels { Armed = false };
            }
            if (Pos != null || !nextBarStart.HasValue || TradesToday >= G.MaxTradesDay || !InWindow(nextBarStart.Value))
                return dec;
            if (fam == "vwap_revert")
            {
                if (i + 1 >= G.VwapMinBars && sd[i] > 0)
                {
                    double dev = (c[i] - vw[i]) / sd[i];
                    int d = dev >= G.DevK ? -1 : (dev <= -G.DevK ? 1 : 0);
                    if (d != 0 && G.VwapConfirm && !(d * (c[i] - o[i]) > 0)) d = 0;
                    if (d != 0 && Allowed(d))
                    {
                        double? st = null;
                        if (G.StopMode == "structure")
                        {
                            int a = Math.Max(0, i - 1);
                            st = d > 0 ? Math.Min(l[a], l[i]) - tick : Math.Max(h[a], h[i]) + tick;
                        }
                        double? tg = G.TargetMode == "vwap" ? (double?)vw[i] : null;
                        dec.Entry = new EntryIntent { Dir = d, Stop = st, Target = tg };
                    }
                }
            }
            else if (fam == "intraday_mom")
            {
                if (!momDone && t[i] + iv >= Spec.OpenMin + G.MomWindowMin)
                {
                    momDone = true;
                    double rf = (G.MomFrom == "open" || !ctx.PrevClose.HasValue) ? o[0] : ctx.PrevClose.Value;
                    double move = c[i] - rf;
                    if (Math.Abs(move) >= G.MomThresholdAtr * atr && move != 0)
                    {
                        int d = move > 0 ? 1 : -1;
                        if (Allowed(d))
                        {
                            double? st = null;
                            if (G.StopMode == "structure")
                            {
                                double hi = double.MinValue, lo = double.MaxValue;
                                for (int j = 0; j <= i; j++) { hi = Math.Max(hi, h[j]); lo = Math.Min(lo, l[j]); }
                                st = d > 0 ? lo - tick : hi + tick;
                            }
                            dec.Entry = new EntryIntent { Dir = d, Stop = st, Target = null };
                        }
                    }
                }
            }
            else if (fam == "trend_pullback")
            {
                if (tsign != 0 && i >= G.PullAboveBars)
                {
                    int d = tsign;
                    bool priorOk = true;
                    for (int j = i - G.PullAboveBars; j < i; j++)
                        if (!(d * (c[j] - vw[j]) > 0)) { priorOk = false; break; }
                    bool touch = d > 0 ? (l[i] <= vw[i] + G.PullTouchAtr * atr) : (h[i] >= vw[i] - G.PullTouchAtr * atr);
                    if (priorOk && touch && d * (c[i] - vw[i]) > 0 && Allowed(d))
                    {
                        double? st = null;
                        if (G.StopMode == "structure")
                            st = d > 0 ? Math.Min(l[i], vw[i]) - tick : Math.Max(h[i], vw[i]) + tick;
                        dec.Entry = new EntryIntent { Dir = d, Stop = st, Target = null };
                    }
                }
            }
            return dec;
        }
    }

    /// <summary>Reads the file written by `python -m tradebot paper export`: [section] headers and
    /// key=value lines. Sections: [general], [bot B1], [symbol ES=F].</summary>
    public sealed class BotsFile
    {
        public Dictionary<string, string> General = new Dictionary<string, string>();
        public Dictionary<string, Dictionary<string, string>> Bots = new Dictionary<string, Dictionary<string, string>>();
        public Dictionary<string, Dictionary<string, string>> Symbols = new Dictionary<string, Dictionary<string, string>>();

        public static BotsFile Load(string path) { return Parse(File.ReadAllLines(path)); }

        public static BotsFile Parse(IEnumerable<string> lines)
        {
            var f = new BotsFile();
            Dictionary<string, string> cur = f.General;
            foreach (var raw in lines)
            {
                string line = raw.Trim();
                if (line.Length == 0 || line.StartsWith("#")) continue;
                if (line.StartsWith("[") && line.EndsWith("]"))
                {
                    string name = line.Substring(1, line.Length - 2).Trim();
                    cur = new Dictionary<string, string>();
                    if (name.StartsWith("bot ")) f.Bots[name.Substring(4).Trim()] = cur;
                    else if (name.StartsWith("symbol ")) f.Symbols[name.Substring(7).Trim()] = cur;
                    else f.General = cur;
                    continue;
                }
                int eq = line.IndexOf('=');
                if (eq <= 0) continue;
                cur[line.Substring(0, eq).Trim()] = line.Substring(eq + 1).Trim();
            }
            return f;
        }

        public static string Get(Dictionary<string, string> d, string k, string dflt)
        {
            string v;
            return d != null && d.TryGetValue(k, out v) ? v : dflt;
        }

        /// <summary>Find a bot by slot (B1), rank (1) or breeder id (G48-4395).</summary>
        public Dictionary<string, string> FindBot(string want, out string slot)
        {
            want = (want ?? "").Trim();
            foreach (var kv in Bots)
            {
                if (string.Equals(kv.Key, want, StringComparison.OrdinalIgnoreCase)
                    || Get(kv.Value, "rank", "") == want || Get(kv.Value, "id", "") == want)
                {
                    slot = kv.Key;
                    return kv.Value;
                }
            }
            slot = null;
            return null;
        }

        public static Genome GenomeOf(Dictionary<string, string> bot)
        {
            var genes = new Dictionary<string, string>();
            foreach (var kv in bot)
                if (kv.Key.StartsWith("gene.")) genes[kv.Key.Substring(5)] = kv.Value;
            return Genome.FromDict(genes);
        }

        public ContractSpec SpecFor(string yahooSymbol)
        {
            Dictionary<string, string> s;
            if (!Symbols.TryGetValue(yahooSymbol, out s)) return null;
            Func<string, double> num = k => double.Parse(s[k], NumberStyles.Float, CultureInfo.InvariantCulture);
            return new ContractSpec { Symbol = yahooSymbol, NtRoot = s["nt_root"], Tick = num("tick"),
                                      Multiplier = num("multiplier"), CommissionRt = num("commission_rt"),
                                      OpenMin = (int)num("open_min"), CloseMin = (int)num("close_min") };
        }
    }
}

namespace NinjaTrader.NinjaScript.Strategies.BredBotLib
{
    /// <summary>Risk limits shared by every BredBot running in this NinjaTrader (static = one copy for
    /// all strategy instances). Resets each US Eastern trading day. A halt lasts for the rest of the day.</summary>
    public static class BredBotRisk
    {
        static readonly object Lk = new object();
        static DateTime day = DateTime.MinValue;
        static readonly Dictionary<string, double> realizedByBot = new Dictionary<string, double>();
        static readonly Dictionary<string, double> openPnlByInstance = new Dictionary<string, double>();
        static readonly Dictionary<string, string> botOfInstance = new Dictionary<string, string>();
        static readonly HashSet<string> openInstances = new HashSet<string>();
        static readonly Dictionary<string, string> haltedBots = new Dictionary<string, string>();
        static string haltAll;
        static int tradesAll;

        public static double BotDailyLossUsd = 250;
        public static double AllBotsDailyLossUsd = 600;
        public static int MaxOpenPositions = 6;
        public static int MaxTradesAllBots = 40;

        static void Roll(DateTime etDay)
        {
            if (etDay.Date == day) return;
            day = etDay.Date;
            realizedByBot.Clear();
            haltedBots.Clear();
            haltAll = null;
            tradesAll = 0;
        }

        public static void Configure(double botLoss, double allLoss, int maxOpen, int maxTrades)
        {
            lock (Lk)
            {
                // Never looser than these ceilings, whatever the file says.
                BotDailyLossUsd = Math.Min(Math.Max(botLoss, 0), 2000);
                AllBotsDailyLossUsd = Math.Min(Math.Max(allLoss, 0), 5000);
                MaxOpenPositions = Math.Min(Math.Max(maxOpen, 0), 20);
                MaxTradesAllBots = Math.Min(Math.Max(maxTrades, 0), 200);
            }
        }

        public static void Register(string instance, string bot) { lock (Lk) botOfInstance[instance] = bot; }

        public static void Unregister(string instance)
        {
            lock (Lk) { botOfInstance.Remove(instance); openInstances.Remove(instance); openPnlByInstance.Remove(instance); }
        }

        /// <summary>Null if a new entry is allowed, else the reason it isn't.</summary>
        public static string EntryBlock(DateTime etNow, string instance, string bot)
        {
            lock (Lk)
            {
                Roll(etNow);
                string r;
                if (haltAll != null) return "all bots halted: " + haltAll;
                if (haltedBots.TryGetValue(bot, out r)) return "bot halted: " + r;
                if (openInstances.Count >= MaxOpenPositions && !openInstances.Contains(instance))
                    return "max open positions across all bots (" + MaxOpenPositions + ")";
                if (tradesAll >= MaxTradesAllBots) return "max trades today across all bots (" + MaxTradesAllBots + ")";
                return null;
            }
        }

        /// <summary>Null if this instance may keep its position, else why it must flatten.</summary>
        public static string MustFlatten(DateTime etNow, string bot)
        {
            lock (Lk)
            {
                Roll(etNow);
                string r;
                if (haltAll != null) return haltAll;
                if (haltedBots.TryGetValue(bot, out r)) return r;
                return null;
            }
        }

        public static void Opened(DateTime etNow, string instance)
        {
            lock (Lk) { Roll(etNow); openInstances.Add(instance); tradesAll++; }
        }

        public static void Closed(DateTime etNow, string instance, string bot, double pnlUsd)
        {
            lock (Lk)
            {
                Roll(etNow);
                openInstances.Remove(instance);
                openPnlByInstance.Remove(instance);
                double x;
                realizedByBot[bot] = (realizedByBot.TryGetValue(bot, out x) ? x : 0) + pnlUsd;
                Check();
            }
        }

        /// <summary>Report the open trade's unrealized P&L; halts bots or everything past the limits.</summary>
        public static void OpenPnl(DateTime etNow, string instance, double usd)
        {
            lock (Lk) { Roll(etNow); openPnlByInstance[instance] = usd; Check(); }
        }

        public static void HaltAll(DateTime etNow, string reason) { lock (Lk) { Roll(etNow); if (haltAll == null) haltAll = reason; } }

        static void Check()
        {
            var byBot = new Dictionary<string, double>(realizedByBot);
            foreach (var kv in openPnlByInstance)
            {
                string b;
                if (!botOfInstance.TryGetValue(kv.Key, out b)) continue;
                double x;
                byBot[b] = (byBot.TryGetValue(b, out x) ? x : 0) + kv.Value;
            }
            double total = 0;
            foreach (var kv in byBot)
            {
                total += kv.Value;
                if (kv.Value <= -BotDailyLossUsd && !haltedBots.ContainsKey(kv.Key))
                    haltedBots[kv.Key] = "daily loss limit $" + BotDailyLossUsd.ToString("0", CultureInfo.InvariantCulture)
                                         + " hit (" + kv.Value.ToString("0.00", CultureInfo.InvariantCulture) + ")";
            }
            if (total <= -AllBotsDailyLossUsd && haltAll == null)
                haltAll = "all-bots daily loss limit $" + AllBotsDailyLossUsd.ToString("0", CultureInfo.InvariantCulture)
                          + " hit (" + total.ToString("0.00", CultureInfo.InvariantCulture) + ")";
        }

        /// <summary>Forget everything (the wrapper test runs many bots one after another).</summary>
        public static void ResetForTests() { lock (Lk) { day = DateTime.MinValue; Roll(DateTime.MaxValue.AddDays(-1)); openInstances.Clear(); openPnlByInstance.Clear(); } }

        public static double RealizedToday(string bot) { lock (Lk) { double x; return realizedByBot.TryGetValue(bot, out x) ? x : 0; } }
    }

    /// <summary>Appends CSV rows to the files the Python side reads (paper compare / status).</summary>
    public static class PaperLog
    {
        static readonly object Lk = new object();

        public static string Csv(object v)
        {
            if (v == null) return "";
            string s = v is double ? ((double)v).ToString("R", CultureInfo.InvariantCulture)
                     : Convert.ToString(v, CultureInfo.InvariantCulture);
            if (s.IndexOf(',') >= 0 || s.IndexOf('"') >= 0) s = "\"" + s.Replace("\"", "\"\"") + "\"";
            return s;
        }

        public static void Append(string path, string header, params object[] cols)
        {
            var parts = new string[cols.Length];
            for (int i = 0; i < cols.Length; i++) parts[i] = Csv(cols[i]);
            lock (Lk)
            {
                try
                {
                    Directory.CreateDirectory(Path.GetDirectoryName(path));
                    bool fresh = !File.Exists(path);
                    using (var w = new StreamWriter(path, true))
                    {
                        if (fresh) w.WriteLine(header);
                        w.WriteLine(string.Join(",", parts));
                    }
                }
                catch (IOException) { }  // a file open in Excel must not crash the bot
            }
        }
    }
}
