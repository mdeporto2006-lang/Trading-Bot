// Parity harness: fills BredBotCore's decisions exactly the way tradebot/breeder/sim.py fills
// its own (steps 1-3: market entry at the next open, breakout stop orders, stop/target exits),
// so CI can compare the C# bot's trades with the Python backtest's, trade by trade.
//
//   python tools/parity/dump.py parity_in.json
//   dotnet run --project tools/parity -- parity_in.json parity_out.json
//   python tools/parity/compare.py parity_in.json parity_out.json
//
// Written in C# 6 (LangVersion 6 in the .csproj) like the NinjaTrader files.

using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Text;
using NinjaTrader.NinjaScript.Strategies.BredBotLib;

namespace Parity
{
    public static class Program
    {
        static string F(double x) { return x.ToString("R", CultureInfo.InvariantCulture); }

        static string Str(object e)
        {
            if (e is string) return (string)e;
            if (e is bool) return (bool)e ? "true" : "false";
            if (e == null) return "";
            return F((double)e);
        }

        static Dictionary<string, object> Obj(object e) { return (Dictionary<string, object>)e; }
        static List<object> List(object e) { return (List<object>)e; }
        static double Num(object e) { return (double)e; }
        static int Int(object e) { return (int)Math.Round((double)e); }

        static List<double> Arr(object e)
        {
            var r = new List<double>();
            foreach (var x in List(e)) r.Add(Num(x));
            return r;
        }

        public static int Main(string[] args)
        {
            var root = Obj(MiniJson.Parse(File.ReadAllText(args[0])));
            int interval = Int(root["interval"]);
            double slipTicks = Num(root["slip_ticks"]);
            var specs = new Dictionary<string, ContractSpec>();
            foreach (var p in Obj(root["specs"]))
            {
                var s = Obj(p.Value);
                specs[p.Key] = new ContractSpec { Symbol = p.Key, NtRoot = (string)s["nt_root"],
                    Tick = Num(s["tick"]), Multiplier = Num(s["multiplier"]), CommissionRt = Num(s["commission_rt"]),
                    OpenMin = Int(s["open_min"]), CloseMin = Int(s["close_min"]) };
            }
            // With a third argument (a bots.txt from `paper export`), genomes and contract specs are read
            // from it with the same code NinjaTrader uses, instead of from the JSON.
            BotsFile bf = args.Length > 2 ? BotsFile.Load(args[2]) : null;
            if (bf != null)
                foreach (var k in new List<string>(specs.Keys))
                {
                    var fs = bf.SpecFor(k);
                    if (fs != null) specs[k] = fs;
                }
            var sb = new StringBuilder();
            sb.Append("{\"trades\":{");
            bool firstG = true;
            foreach (var geo in List(root["genomes"]))
            {
                var ge = Obj(geo);
                var genes = new Dictionary<string, string>();
                foreach (var p in Obj(ge["genes"]))
                {
                    if (p.Key == "symbols")
                    {
                        var syms = new List<string>();
                        foreach (var x in List(p.Value)) syms.Add((string)x);
                        genes["symbols"] = string.Join(",", syms);
                    }
                    else genes[p.Key] = Str(p.Value);
                }
                var g = Genome.FromDict(genes);
                if (bf != null)
                {
                    string slot;
                    var bot = bf.FindBot((string)ge["id"], out slot);
                    if (bot == null) throw new Exception("bot " + ge["id"] + " missing from bots.txt");
                    g = BotsFile.GenomeOf(bot);
                }
                if (!firstG) sb.Append(",");
                firstG = false;
                sb.Append("\"").Append((string)ge["id"]).Append("\":[");
                bool firstT = true;
                foreach (var seo in List(root["sessions"]))
                {
                    var se = Obj(seo);
                    string sym = (string)se["symbol"];
                    if (!g.Symbols.Contains(sym)) continue;
                    foreach (var row in RunSession(g, specs[sym], interval, slipTicks, se))
                    {
                        if (!firstT) sb.Append(",");
                        firstT = false;
                        sb.Append(row);
                    }
                }
                sb.Append("]");
            }
            sb.Append("},\"daily\":[");
            bool firstD = true;
            foreach (var deo in List(root["daily_checks"]))
            {
                var de = Obj(deo);
                var bars = new List<double[]>();
                foreach (var b in List(de["daily"]))
                {
                    var a = Arr(b);
                    bars.Add(new[] { a[0], a[1], a[2], a[3] });
                }
                var dc = DayContext.FromDaily(bars);
                if (!firstD) sb.Append(",");
                firstD = false;
                sb.Append("{\"prev_close\":").Append(dc.PrevClose.HasValue ? F(dc.PrevClose.Value) : "null")
                  .Append(",\"atr\":").Append(dc.Atr.HasValue ? F(dc.Atr.Value) : "null").Append(",\"trend\":{");
                bool ft = true;
                foreach (var kv in dc.Trend)
                {
                    if (!ft) sb.Append(",");
                    ft = false;
                    sb.Append("\"").Append(kv.Key).Append("\":").Append(F(kv.Value));
                }
                sb.Append("}}");
            }
            sb.Append("]}");
            File.WriteAllText(args[1], sb.ToString());
            return 0;
        }

        public static List<string> RunSession(Genome g, ContractSpec spec, int interval, double slipTicks, Dictionary<string, object> se)
        {
            var t = new List<int>();
            foreach (var x in List(se["t"])) t.Add(Int(x));
            var o = Arr(se["o"]); var h = Arr(se["h"]);
            var l = Arr(se["l"]); var c = Arr(se["c"]); var v = Arr(se["v"]);
            var dc = new DayContext();
            if (se["prev_close"] != null) dc.PrevClose = Num(se["prev_close"]);
            dc.Atr = Num(se["atr"]);
            foreach (var p in Obj(se["trend"])) dc.Trend[int.Parse(p.Key)] = Num(p.Value);
            string day = (string)se["day"];

            var core = new BredBotCore(g, spec, interval);
            var outRows = new List<string>();
            core.BeginSession(dc);
            double tick = spec.Tick, slip = slipTicks * tick;
            int n = t.Count;
            EntryIntent pending = null;

            Action<double, string, int> close = (price, reason, atT) =>
            {
                var p = core.Pos;
                string label = core.ExitLabel(reason);
                double pnl = core.PnlUsd(p.Dir, p.Entry, price);
                double rd = p.Risk * spec.Multiplier;
                double r = rd != 0 ? pnl / rd : 0.0;
                outRows.Add("{\"symbol\":\"" + spec.Symbol + "\",\"day\":\"" + day + "\",\"dir\":" + p.Dir
                    + ",\"entry_t\":" + p.EntryT + ",\"exit_t\":" + atT + ",\"entry\":" + F(p.Entry)
                    + ",\"exit\":" + F(price) + ",\"stop\":" + F(p.Stop0) + ",\"target\":" + F(p.Target)
                    + ",\"reason\":\"" + label + "\",\"pnl\":" + F(pnl) + ",\"r\":" + F(r) + "}");
                core.OnExited();
            };

            for (int i = 0; i < n; i++)
            {
                bool filledByStop = false;
                // 1. market entry decided at the previous close
                if (pending != null && core.Pos == null)
                    core.OnEntryFilled(pending.Dir, o[i] + pending.Dir * slip, i, t[i], pending.Stop, pending.Target);
                pending = null;
                // 2. breakout stop orders
                if (core.OrbWorking(t[i]))
                {
                    var orb = core.Orb;
                    bool up = h[i] >= orb.LongTrigger && core.Allowed(1);
                    bool dn = l[i] <= orb.ShortTrigger && core.Allowed(-1);
                    if (up != dn)
                    {
                        int d = up ? 1 : -1;
                        double trig = up ? orb.LongTrigger : orb.ShortTrigger;
                        double px = up ? Math.Max(trig, o[i]) : Math.Min(trig, o[i]);
                        double? st = null;
                        if (g.StopMode == "structure") st = up ? orb.LongStop : orb.ShortStop;
                        filledByStop = core.OnEntryFilled(d, px + d * slip, i, t[i], st, null);
                    }
                }
                // 3. exits during this bar
                if (core.Pos != null)
                {
                    var p = core.Pos;
                    int d = p.Dir;
                    bool sameBar = p.BarIndex == i;
                    if (!sameBar && d * (o[i] - p.Stop) <= 0)
                        close(o[i] - d * slip, "stop", t[i]);
                    else if ((d > 0 && l[i] <= p.Stop) || (d < 0 && h[i] >= p.Stop))
                        close(p.Stop - d * slip, "stop", t[i] + interval);
                    else if (!filledByStop && ((d > 0 && h[i] >= p.Target + tick) || (d < 0 && l[i] <= p.Target - tick)))
                        close(p.Target, "target", t[i] + interval);
                }
                // 4 + 5. the core, on the close
                int? next = i + 1 < n ? (int?)t[i + 1] : null;
                var dec = core.OnBarClose(t[i], o[i], h[i], l[i], c[i], v[i], next);
                if (dec.ExitReason != null)
                {
                    close(c[i] - core.Pos.Dir * slip, dec.ExitReason, t[i] + interval);
                    dec = core.AfterCloseExit(next);
                }
                pending = dec.Entry;
            }
            return outRows;
        }
    }
}

namespace Parity
{
    /// <summary>Just enough JSON reading for the parity file (no package needed, runs on Mono too).</summary>
    public static class MiniJson
    {
        public static object Parse(string s) { int i = 0; var v = Val(s, ref i); return v; }

        static void Ws(string s, ref int i) { while (i < s.Length && char.IsWhiteSpace(s[i])) i++; }

        static object Val(string s, ref int i)
        {
            Ws(s, ref i);
            char ch = s[i];
            if (ch == '{')
            {
                var d = new Dictionary<string, object>();
                i++; Ws(s, ref i);
                if (s[i] == '}') { i++; return d; }
                while (true)
                {
                    Ws(s, ref i);
                    string k = Strg(s, ref i);
                    Ws(s, ref i); i++;  // ':'
                    d[k] = Val(s, ref i);
                    Ws(s, ref i);
                    if (s[i] == ',') { i++; continue; }
                    i++; return d;  // '}'
                }
            }
            if (ch == '[')
            {
                var a = new List<object>();
                i++; Ws(s, ref i);
                if (s[i] == ']') { i++; return a; }
                while (true)
                {
                    a.Add(Val(s, ref i));
                    Ws(s, ref i);
                    if (s[i] == ',') { i++; continue; }
                    i++; return a;  // ']'
                }
            }
            if (ch == '"') return Strg(s, ref i);
            if (s.Substring(i, Math.Min(4, s.Length - i)) == "true") { i += 4; return true; }
            if (s.Substring(i, Math.Min(5, s.Length - i)) == "false") { i += 5; return false; }
            if (s.Substring(i, Math.Min(4, s.Length - i)) == "null") { i += 4; return null; }
            if (s.Substring(i, Math.Min(8, s.Length - i)) == "Infinity") { i += 8; return double.PositiveInfinity; }
            int j = i;
            while (j < s.Length && "+-0123456789.eE".IndexOf(s[j]) >= 0) j++;
            double x = double.Parse(s.Substring(i, j - i), NumberStyles.Float, CultureInfo.InvariantCulture);
            i = j;
            return x;
        }

        static string Strg(string s, ref int i)
        {
            var sb = new StringBuilder();
            i++;  // opening quote
            while (s[i] != '"')
            {
                if (s[i] == '\\')
                {
                    i++;
                    char e = s[i];
                    if (e == 'u') { sb.Append((char)Convert.ToInt32(s.Substring(i + 1, 4), 16)); i += 4; }
                    else if (e == 'n') sb.Append('\n');
                    else if (e == 't') sb.Append('\t');
                    else sb.Append(e);
                }
                else sb.Append(s[i]);
                i++;
            }
            i++;
            return sb.ToString();
        }
    }
}
