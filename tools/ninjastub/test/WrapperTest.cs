// Drives the real BredBot.cs strategy through whole sessions against the fake broker in
// NinjaTraderStubs.cs, and checks: its trades match the parity harness (which matches the Python
// backtest), only sim accounts trade, the kill switch flattens and stops, loss limits halt, and a
// rejected stop order gets the position flattened.
//
//   python tools/parity/dump.py parity_in.json
//   mcs -langversion:6 -main:WrapperTest.Program -out:wt.exe tools/ninjastub/*.cs tools/ninjastub/test/*.cs \
//       tools/parity/Program.cs ninjatrader/BredBot.cs ninjatrader/BredBotCore.cs
//   mono wt.exe parity_in.json parity_in.bots.txt
using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using NinjaTrader.NinjaScript;
using NinjaTrader.NinjaScript.Strategies;
using NinjaTrader.NinjaScript.Strategies.BredBotLib;
using Parity;

namespace WrapperTest
{
    public static class Program
    {
        static TimeZoneInfo Et()
        {
            foreach (var id in new[] { "America/New_York", "Eastern Standard Time" })
                try { return TimeZoneInfo.FindSystemTimeZoneById(id); } catch (Exception) { }
            throw new Exception("no ET zone");
        }

        static string tmp;
        static int failures;

        static void Check(bool ok, string what)
        {
            Console.WriteLine((ok ? "  ok   " : "  FAIL ") + what);
            if (!ok) failures++;
        }

        sealed class Sess { public string Sym; public DateTime Day; public List<object> T, O, H, L, C, V; }

        static List<Sess> Sessions(Dictionary<string, object> root, string sym)
        {
            var r = new List<Sess>();
            foreach (var so in (List<object>)root["sessions"])
            {
                var s = (Dictionary<string, object>)so;
                if ((string)s["symbol"] != sym) continue;
                r.Add(new Sess { Sym = sym, Day = DateTime.ParseExact((string)s["day"], "yyyy-MM-dd", CultureInfo.InvariantCulture),
                                 T = (List<object>)s["t"], O = (List<object>)s["o"], H = (List<object>)s["h"],
                                 L = (List<object>)s["l"], C = (List<object>)s["c"], V = (List<object>)s["v"] });
            }
            return r;
        }

        /// <summary>Made-up daily bars ending before the first session, sized so ATR is near the
        /// sessions' own (the sim never sees these; both sides get the same DayContext).</summary>
        static void Daily(BredBot s, List<Sess> ss)
        {
            double p = (double)ss[0].O[0];
            double atr = Math.Abs((double)ss[0].H[0] - (double)ss[0].L[0]) * 8 + p * 0.004;
            var d0 = ss[0].Day.AddDays(-130);
            int k = 0;
            for (var d = d0; d <= ss[ss.Count - 1].Day; d = d.AddDays(1))
            {
                if (d.DayOfWeek == DayOfWeek.Saturday || d.DayOfWeek == DayOfWeek.Sunday) continue;
                double c = p * (1 + 0.03 * Math.Sin(k++ / 9.0));
                s.BarsArray[1].T.Add(d);
                s.BarsArray[1].B.Add(new[] { c, c + atr * 0.6, c - atr * 0.4, c });
            }
        }

        static BredBot Make(string account, string bot, string root, double pv, double tick, string botsPath)
        {
            var s = new BredBot();
            s.SetState(State.SetDefaults);
            s.Bot = bot;
            s.BotsFilePath = botsPath;
            s.Account.Name = account;
            s.Instrument.FullName = root + " 03-27";
            s.Instrument.MasterInstrument.Name = root;
            s.Instrument.MasterInstrument.PointValue = pv;
            s.Instrument.MasterInstrument.TickSize = tick;
            s.BarsPeriod.BarsPeriodType = NinjaTrader.Data.BarsPeriodType.Minute;
            s.BarsPeriod.Value = 5;
            s.Slip = tick;
            return s;
        }

        static void Feed(BredBot s, Sess ss, int interval, double tick, Func<int, bool> before)
        {
            for (int i = 0; i < ss.T.Count; i++)
            {
                if (before != null && !before(i)) return;
                int m = (int)Math.Round((double)ss.T[i]);
                DateTime start = ss.Day.AddMinutes(m);
                s.Now = start;
                double o = (double)ss.O[i], h = (double)ss.H[i], l = (double)ss.L[i], c = (double)ss.C[i];
                s.Match(o, h, l, tick);
                s.Time.Values.Add(start.AddMinutes(interval));  // NinjaTrader stamps bars at their end
                s.Open.Values.Add(o); s.High.Values.Add(h); s.Low.Values.Add(l); s.Close.Values.Add(c);
                s.Volume.Values.Add((double)ss.V[i]);
                s.Bar();
            }
        }

        static List<string[]> Trades(string bot, string instrument) { return Trades(bot, instrument, null); }

        static List<string[]> Trades(string bot, string instrument, string account)
        {
            var r = new List<string[]>();
            string p = Path.Combine(tmp, "tradebot", "paper_trades.csv");
            if (!File.Exists(p)) return r;
            foreach (var line in File.ReadAllLines(p))
            {
                var c = line.Split(',');
                if (c[2] == bot && c[7] == instrument && (account == null || c[1] == account)) r.Add(c);
            }
            return r;
        }

        public static int Main(string[] args)
        {
            tmp = Path.Combine(Path.GetTempPath(), "bredbot-test-" + Guid.NewGuid().ToString("N").Substring(0, 8));
            Directory.CreateDirectory(Path.Combine(tmp, "tradebot"));
            NinjaTrader.Core.Globals.UserDataDir = tmp;
            NinjaTrader.Core.Globals.GeneralOptions.TimeZoneInfo = Et();
            var root = (Dictionary<string, object>)MiniJson.Parse(File.ReadAllText(args[0]));
            var bf = BotsFile.Load(args[1]);
            // loose shared limits, so the many bots below don't halt each other
            string botsPath = Path.Combine(tmp, "bots.txt");
            var lines = new List<string>(File.ReadAllLines(args[1]));
            lines.Insert(lines.IndexOf("[general]") + 1, "all_bots_daily_loss_usd=5000");
            lines.Insert(lines.IndexOf("[general]") + 1, "max_trades_all_bots=200");
            lines = lines.FindAll(x => !x.StartsWith("bot_daily_loss_usd"));
            lines.Insert(lines.IndexOf("[general]") + 1, "bot_daily_loss_usd=2000");
            File.WriteAllLines(botsPath, lines.ToArray());
            int interval = 5;

            Console.WriteLine("1. BredBot trades = parity harness trades (same fills as the Python backtest)");
            int nb = 0, nTrades = 0, entriesOk = 0, exitsOk = 0, exitsChecked = 0;
            var notes = new List<string>();
            int maxBots = args.Length > 2 ? int.Parse(args[2]) : 120;
            foreach (var kv in bf.Bots)
            {
                if (nb++ >= maxBots) break;
                var g = BotsFile.GenomeOf(kv.Value);
                foreach (var sym in g.Symbols)
                {
                    var spec = bf.SpecFor(sym);
                    var ss = Sessions(root, sym);
                    if (ss.Count == 0) continue;
                    BredBotRisk.ResetForTests();
                    var s = Make("SimB1", kv.Key, spec.NtRoot, spec.Multiplier, spec.Tick, botsPath);
                    Daily(s, ss);
                    s.SetState(State.Configure); s.SetState(State.DataLoaded); s.SetState(State.Realtime);
                    var expected = new List<string>();
                    foreach (var x in ss)
                    {
                        var before = new List<double[]>();
                        for (int k = 0; k < s.BarsArray[1].Count; k++)
                            if (s.BarsArray[1].T[k] < x.Day) before.Add(s.BarsArray[1].B[k]);
                        var dc = DayContext.FromDaily(before);
                        var se = new Dictionary<string, object> { { "t", x.T }, { "o", x.O }, { "h", x.H }, { "l", x.L },
                            { "c", x.C }, { "v", x.V }, { "prev_close", dc.PrevClose }, { "atr", dc.Atr },
                            { "day", x.Day.ToString("yyyy-MM-dd") } };
                        var tr = new Dictionary<string, object>();
                        foreach (var t in dc.Trend) tr[t.Key.ToString()] = t.Value;
                        se["trend"] = tr;
                        expected.AddRange(Parity.Program.RunSession(g, spec, interval, 1.0, se));
                        Feed(s, x, interval, spec.Tick, null);
                    }
                    s.SetState(State.Terminated);
                    var got = Trades(kv.Key, s.Instrument.FullName, "SimB1");
                    // pair by day + entry time
                    var exp = new List<Dictionary<string, object>>();
                    foreach (var e in expected) exp.Add((Dictionary<string, object>)MiniJson.Parse(e));
                    nTrades += exp.Count;
                    foreach (var e in exp)
                    {
                        int et = (int)(double)e["entry_t"];
                        string hm = (et / 60).ToString("00") + ":" + (et % 60).ToString("00") + ":00";
                        string[] m = got.Find(c => c[8] == (string)e["day"] && c[10] == hm);
                        if (m == null) { if (notes.Count < 12) notes.Add(kv.Key + " " + sym + " " + e["day"] + " " + hm + " missing in BredBot (" + e["reason"] + ")"); continue; }
                        bool entryOk = Math.Abs(double.Parse(m[12], CultureInfo.InvariantCulture) - (double)e["entry"]) < 1e-9
                                       && (m[9] == "long") == ((double)e["dir"] > 0);
                        if (entryOk) entriesOk++;
                        else if (notes.Count < 12) notes.Add(kv.Key + " " + sym + " entry differs: " + string.Join(",", m) + " vs " + MiniJsonDump(e));
                        string reason = (string)e["reason"];
                        if (reason != "flatten" && reason != "time")
                        {
                            exitsChecked++;
                            if (m[16] == reason && Math.Abs(double.Parse(m[13], CultureInfo.InvariantCulture) - (double)e["exit"]) < 1e-9) exitsOk++;
                            else if (notes.Count < 12) notes.Add(kv.Key + " " + sym + " exit differs: " + m[16] + " @" + m[13] + " vs " + reason + " @" + e["exit"]);
                        }
                    }
                    if (got.Count != exp.Count && notes.Count < 12)
                        notes.Add(kv.Key + " " + sym + ": BredBot " + got.Count + " trades, harness " + exp.Count);
                }
            }
            foreach (var n in notes) Console.WriteLine("     " + n);
            double er = nTrades > 0 ? (double)entriesOk / nTrades : 0, xr = exitsChecked > 0 ? (double)exitsOk / exitsChecked : 1;
            Console.WriteLine("     " + nTrades + " harness trades; entries matched " + entriesOk + " (" + (er * 100).ToString("0.0") +
                              "%), stop/target exits matched " + exitsOk + "/" + exitsChecked);
            // Known, intended differences: flatten/time exits go at the next open (the backtest uses the
            // close); a breakout bar that hits both levels trades live but is skipped in the backtest.
            Check(nTrades > 500, "enough trades to mean something");
            Check(er >= 0.97, "entries match (>= 97%)");
            Check(xr >= 0.97, "stop/target exits match (>= 97%)");

            Console.WriteLine("2. refuses anything that isn't a simulation account");
            {
                var first = new List<string>(bf.Bots.Keys)[0];
                var g = BotsFile.GenomeOf(bf.Bots[first]);
                var spec = bf.SpecFor(g.Symbols[0]);
                foreach (var acct in new[] { "1234567", "LiveAccount", "sim101", "demo123", "MyDEMO1" })
                {
                    var s = Make(acct, first, spec.NtRoot, spec.Multiplier, spec.Tick, botsPath);
                    s.SetState(State.Configure); s.SetState(State.DataLoaded);
                    Check(s.Printed.Exists(p => p.Contains("will NOT trade") && p.Contains("simulation or demo account")), "refused account " + acct);
                }
                Check(BredBot.IsPaperAccount("DEMO4512345") && BredBot.IsPaperAccount("Sim101"), "accepts Tradovate demo (DEMO...) and Sim accounts");
                var wrong = Make("Sim101", first, spec.NtRoot == "MES" ? "ES" : "MES", 50, 0.25, botsPath);
                wrong.SetState(State.Configure); wrong.SetState(State.DataLoaded);
                Check(wrong.Printed.Exists(p => p.Contains("will NOT trade")), "refused an instrument the bot doesn't trade / full-size contract");
            }

            Console.WriteLine("3. kill switch, loss limit, rejected stop");
            {
                // find a bot + symbol + day with a trade that lasts a few bars
                string bot = null; Sess day = null; ContractSpec spec = null; Genome g = null; int entryBar = -1;
                foreach (var kv in bf.Bots)
                {
                    g = BotsFile.GenomeOf(kv.Value);
                    if (g.Family != "intraday_mom") continue;
                    spec = bf.SpecFor(g.Symbols[0]);
                    BredBotRisk.ResetForTests();
                    var probe = Make("SimB2", kv.Key, spec.NtRoot, spec.Multiplier, spec.Tick, botsPath);
                    var ss = Sessions(root, g.Symbols[0]);
                    Daily(probe, ss);
                    probe.SetState(State.Configure); probe.SetState(State.DataLoaded); probe.SetState(State.Realtime);
                    foreach (var x in ss)
                    {
                        int bars = 0, at = -1;
                        Feed(probe, x, interval, spec.Tick, i => { if (probe.Net != 0) { bars++; if (at < 0) at = i; } return true; });
                        if (bars >= 4) { bot = kv.Key; day = x; entryBar = at; break; }
                    }
                    probe.SetState(State.Terminated);
                    if (bot != null) break;
                }
                Check(bot != null, "found a test trade");
                if (bot != null)
                {
                    var ss = Sessions(root, g.Symbols[0]);
                    // kill: create the KILL file one bar after the entry
                    BredBotRisk.ResetForTests();
                    var s = Make("SimB3", bot, spec.NtRoot, spec.Multiplier, spec.Tick, botsPath);
                    Daily(s, ss);
                    s.SetState(State.Configure); s.SetState(State.DataLoaded); s.SetState(State.Realtime);
                    string kill = Path.Combine(tmp, "tradebot", "KILL");
                    Feed(s, day, interval, spec.Tick, i => { if (i == entryBar + 1) File.WriteAllText(kill, "test"); return true; });
                    Check(s.Net == 0 && s.Working.Count == 0, "kill switch: flat, no working orders");
                    var t = Trades(bot, s.Instrument.FullName, "SimB3");
                    Check(t.Count == 1 && t[0][16] == "halt", "kill switch: the trade closed as 'halt' (" + (t.Count > 0 ? string.Join(",", t[t.Count - 1]) : "no trades") + ")");
                    int before = t.Count;
                    var next = ss[ss.IndexOf(day) + 1 < ss.Count ? ss.IndexOf(day) + 1 : 0];
                    Feed(s, next, interval, spec.Tick, null);
                    Check(Trades(bot, s.Instrument.FullName, "SimB3").Count == before && s.Net == 0, "kill switch: no trades while the file exists");
                    s.SetState(State.Terminated);
                    File.Delete(kill);

                    // loss limit: $1 a day per bot -> any losing open trade halts it
                    var tight = new List<string>(File.ReadAllLines(botsPath));
                    tight = tight.FindAll(x => !x.StartsWith("bot_daily_loss_usd"));
                    tight.Insert(tight.IndexOf("[general]") + 1, "bot_daily_loss_usd=1");
                    string tightPath = Path.Combine(tmp, "bots_tight.txt");
                    File.WriteAllLines(tightPath, tight.ToArray());
                    BredBotRisk.ResetForTests();
                    var s2 = Make("SimB4", bot, spec.NtRoot, spec.Multiplier, spec.Tick, tightPath);
                    Daily(s2, ss);
                    s2.SetState(State.Configure); s2.SetState(State.DataLoaded); s2.SetState(State.Realtime);
                    foreach (var x in ss) Feed(s2, x, interval, spec.Tick, null);
                    var t2 = Trades(bot, s2.Instrument.FullName, "SimB4");
                    bool anyLoss = t2.Exists(c => double.Parse(c[17], CultureInfo.InvariantCulture) < 0);
                    var perDay = new Dictionary<string, int>();
                    foreach (var c in t2)
                    {
                        int n; perDay[c[8]] = (perDay.TryGetValue(c[8], out n) ? n : 0) + 1;
                    }
                    bool noTradeAfterLoss = true;
                    var lossDays = new HashSet<string>();
                    foreach (var c in t2)
                    {
                        if (lossDays.Contains(c[8])) noTradeAfterLoss = false;
                        if (double.Parse(c[17], CultureInfo.InvariantCulture) < 0) lossDays.Add(c[8]);
                    }
                    Check(anyLoss && noTradeAfterLoss, "loss limit: no new trade the same day after a loss past the limit");
                    Check(s2.Net == 0, "loss limit: ends flat");
                    s2.SetState(State.Terminated);

                    // rejected stop order -> flatten at once
                    BredBotRisk.ResetForTests();
                    var s3 = Make("SimB5", bot, spec.NtRoot, spec.Multiplier, spec.Tick, botsPath);
                    s3.RejectStops = true;
                    Daily(s3, ss);
                    s3.SetState(State.Configure); s3.SetState(State.DataLoaded); s3.SetState(State.Realtime);
                    Feed(s3, day, interval, spec.Tick, null);
                    var t3 = Trades(bot, s3.Instrument.FullName, "SimB5");
                    Check(t3.Count >= 1 && t3[0][16] == "rejected_bracket", "rejected stop: closed as 'rejected_bracket'");
                    Check(s3.Net == 0, "rejected stop: ends flat");
                    s3.SetState(State.Terminated);
                }
            }
            Console.WriteLine(failures == 0 ? "ALL WRAPPER CHECKS PASSED" : failures + " WRAPPER CHECK(S) FAILED");
            return failures == 0 ? 0 : 1;
        }

        static string MiniJsonDump(Dictionary<string, object> d)
        {
            var parts = new List<string>();
            foreach (var kv in d) parts.Add(kv.Key + "=" + Convert.ToString(kv.Value, CultureInfo.InvariantCulture));
            return string.Join(" ", parts);
        }
    }
}
