"""`paper` commands: hand bred bots to NinjaTrader's simulator and check them against the backtest.

  python -m tradebot paper export            # top bots from the hall of fame -> bots.txt for NinjaTrader
  python -m tradebot paper install           # copy the BredBot strategy files into NinjaTrader
  python -m tradebot paper status            # today's paper trades, P&L, kill switch, recent events
  python -m tradebot paper compare           # paper results vs the backtest, trade by trade
  python -m tradebot paper kill              # kill switch: every BredBot flattens and stops
  python -m tradebot paper resume            # remove the kill switch (trading resumes next day)

Nothing here places orders. Orders are placed only by the BredBot strategy inside NinjaTrader,
and it refuses any account that isn't a NinjaTrader simulation account.
"""

from __future__ import annotations

import csv
import json
import math
import os
import shutil
import statistics
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from ..breeder import genome as gm
from ..breeder import halloffame as hf
from ..breeder.contracts import CONTRACTS, NT_ROOT, hhmm
from ..breeder.evolve import ci95

REPO = Path(__file__).resolve().parents[2]
NT_FILES = ("BredBot.cs", "BredBotCore.cs")


def nt_home() -> Path | None:
    """Documents\\NinjaTrader 8 (also under OneDrive), or $TRADEBOT_NT_HOME."""
    env = os.environ.get("TRADEBOT_NT_HOME")
    if env:
        return Path(env)
    home = Path.home()
    for p in (home / "Documents" / "NinjaTrader 8", home / "OneDrive" / "Documents" / "NinjaTrader 8"):
        if p.exists():
            return p
    return None


def default_dir() -> Path:
    h = nt_home()
    return (h / "tradebot") if h else Path("ninjatrader_out")


def add_parser(sub) -> None:
    p = sub.add_parser("paper", help="paper trade bred bots on NinjaTrader's simulator (export, compare, kill)")
    ps = p.add_subparsers(dest="paper_cmd", required=True)
    e = ps.add_parser("export", help="write bots.txt for the NinjaTrader BredBot strategy")
    e.add_argument("--hof", default="docs/breeder/hall_of_fame.json", help="hall of fame file")
    e.add_argument("--run", default="docs/breeder/latest.json", help="breeder run, used if there's no hall of fame")
    e.add_argument("--top", type=int, default=3, help="how many bots (3-5 is the plan)")
    e.add_argument("--promoted-only", action="store_true", help="only bots that passed every hall-of-fame check")
    e.add_argument("--allow-similar", action="store_true",
                   help="allow several bots of the same family on the same instruments (near-clones)")
    e.add_argument("--bot-loss", type=float, default=250, help="daily loss limit per bot, USD (all its instruments)")
    e.add_argument("--all-loss", type=float, default=600, help="daily loss limit for all bots together, USD")
    e.add_argument("--max-open", type=int, default=6, help="max open positions across all bots")
    e.add_argument("--max-trades", type=int, default=40, help="max trades a day across all bots")
    e.add_argument("--dir", default=None, help="output folder (default Documents\\NinjaTrader 8\\tradebot)")
    i = ps.add_parser("install", help="copy BredBot.cs and BredBotCore.cs into NinjaTrader's strategies folder")
    i.add_argument("--nt-home", default=None, help="the 'NinjaTrader 8' folder in Documents")
    for name, hlp in (("status", "today's paper trades and events"), ("kill", "kill switch: flatten and stop every BredBot"),
                      ("resume", "remove the kill switch")):
        q = ps.add_parser(name, help=hlp)
        q.add_argument("--dir", default=None)
    c = ps.add_parser("compare", help="paper results vs the backtest")
    c.add_argument("--dir", default=None, help="folder with paper_trades.csv and bots.json")
    c.add_argument("--no-replay", action="store_true", help="skip re-running the backtest on the paper days (needs Yahoo)")
    c.add_argument("--cache", default="backtest_cache/futures")
    c.add_argument("--archive", default=None, help="saved bar history (adds days Yahoo no longer serves)")


def run_paper(args) -> None:
    d = Path(args.dir) if getattr(args, "dir", None) else default_dir()
    cmd = args.paper_cmd
    if cmd == "export":
        export(args, d)
    elif cmd == "install":
        install(Path(args.nt_home) if args.nt_home else nt_home())
    elif cmd == "kill":
        d.mkdir(parents=True, exist_ok=True)
        (d / "KILL").write_text(f"kill switch set {datetime.now(timezone.utc).isoformat()} by tradebot paper kill\n")
        print(f"Kill switch ON: {d / 'KILL'}\nEvery running BredBot cancels its orders and flattens within ~2 seconds,\n"
              f"and stops trading until you run `python -m tradebot paper resume` (it resumes the next day).\n"
              f"Belt and braces: in NinjaTrader, Control Center > Positions > right-click > Flatten everything.")
    elif cmd == "resume":
        ks = [k for k in (d / "KILL", d / "KILL.txt") if k.exists()]
        for k in ks:
            k.unlink()
        print(f"Kill switch removed ({', '.join(map(str, ks))}). Halted bots resume on the next trading day."
              if ks else f"No kill switch in {d}.")
    elif cmd == "status":
        status(d)
    elif cmd == "compare":
        print(compare(d, replay=not args.no_replay, cache=args.cache, archive=args.archive))


# ---------------------------------------------------------------------------- export
def pick_bots(hof_path: str, run_path: str, top: int, promoted_only: bool, allow_similar: bool = False) -> list[dict]:
    """Hall of fame order (promoted first). Without a hall of fame: the latest run's champion and
    the finalists that also made money on validate and holdout days."""
    out = []
    if Path(hof_path).exists():
        hof = hf.load(hof_path)
        for b in hf.ranked(hof):
            if promoted_only and b["status"] != "promoted":
                continue
            st = b.get("stats") or {}
            out.append({"id": b["id"], "key": b["key"], "status": b["status"], "genes": b["genes"],
                        "about": b["about"], "expect": st.get("record"), "forward": st.get("forward"),
                        "r_list": [r for d in sorted(b["days"]) for r, _ in b["days"][d]],
                        "checks": {k: v["ok"] for k, v in (b.get("checks") or {}).items()}})
    elif Path(run_path).exists() and not promoted_only:
        rep = json.loads(Path(run_path).read_text())
        champ = rep["champion"]
        bots = {b["id"]: b for b in rep.get("bots") or [] if isinstance(b, dict)}
        fins = [champ]
        for f in rep.get("finalists", []):
            f = f if isinstance(f, dict) else bots.get(f)
            if f and f["id"] != champ["id"]:
                fins.append(f)
        for b in fins:
            h = b.get("holdout") or {}
            if b is not champ and not (b.get("val_passes") and h.get("exp_r", 0) > 0):
                continue
            out.append({"id": b["id"], "key": gm.key(b["genes"]), "status": "candidate (not yet in the hall of fame)",
                        "genes": b["genes"], "about": b.get("about") or gm.describe(b["genes"]),
                        "expect": h or None, "forward": None, "r_list": [], "checks": {}})
    seen, same, kinds, uniq = set(), set(), set(), []
    for b in out:
        # skip exact repeats, bots whose every trade matches one already picked, and (unless asked)
        # near-clones: same strategy family on the same instruments. Paper trading three copies of one
        # idea tells you no more than one, and they'd all win and lose on the same days.
        trades = tuple(b["r_list"])
        kind = (b["genes"]["family"], tuple(sorted(b["genes"]["symbols"])))
        if b["key"] in seen or (trades and trades in same) or (kind in kinds and not allow_similar):
            continue
        seen.add(b["key"])
        same.add(trades)
        kinds.add(kind)
        uniq.append(b)
    return uniq[:max(1, min(top, 5))]


def _fmt(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return repr(v)
    return str(v)


def bots_txt(bots: list[dict], interval: int, risk: dict) -> str:
    lines = ["# Bred bots for NinjaTrader's BredBot strategy. Written by: python -m tradebot paper export",
             f"# {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC. SIM ACCOUNTS ONLY. One contract per trade.",
             "", "[general]", f"interval={interval}"]
    lines += [f"{k}={_fmt(v)}" for k, v in risk.items()]
    syms = set()
    for i, b in enumerate(bots, 1):
        e = b.get("expect") or {}
        lines += ["", f"[bot B{i}]", f"rank={i}", f"id={b['id']}", f"key={b['key']}", f"status={b['status']}",
                  f"about={b['about']}",
                  f"backtest=trades {e.get('trades', 0)}, win rate {e.get('win_rate', 0):.2%}, {e.get('exp_r', 0):+.3f}R a trade"]
        for k in sorted(gm.SPACE):
            lines.append(f"gene.{k}={_fmt(b['genes'][k])}")
        lines.append("gene.symbols=" + ",".join(b["genes"]["symbols"]))
        syms.update(b["genes"]["symbols"])
    for s in sorted(syms):
        c = CONTRACTS[s]
        lines += ["", f"[symbol {s}]", f"nt_root={NT_ROOT[s]}", f"name={c.name}", f"tick={c.tick!r}",
                  f"multiplier={c.multiplier!r}", f"commission_rt={c.commission_rt!r}",
                  f"open_min={hhmm(c.session_et[0])}", f"close_min={hhmm(c.session_et[1])}",
                  f"session_et={c.session_et[0]}-{c.session_et[1]}"]
    return "\n".join(lines) + "\n"


def export(args, d: Path) -> None:
    bots = pick_bots(args.hof, args.run, args.top, args.promoted_only, getattr(args, "allow_similar", False))
    if not bots:
        sys.exit("No bots to export. Run the breeder first (Actions > Breed), or drop --promoted-only.")
    interval = 5
    if Path(args.run).exists():
        interval = int(json.loads(Path(args.run).read_text()).get("meta", {}).get("interval", 5))
    risk = {"bot_daily_loss_usd": float(args.bot_loss), "all_bots_daily_loss_usd": float(args.all_loss),
            "max_open_positions": int(args.max_open), "max_trades_all_bots": int(args.max_trades)}
    d.mkdir(parents=True, exist_ok=True)
    (d / "bots.txt").write_text(bots_txt(bots, interval, risk))
    (d / "bots.json").write_text(json.dumps({"exported": datetime.now(timezone.utc).isoformat(), "interval": interval,
                                             "risk": risk, "bots": {f"B{i}": b for i, b in enumerate(bots, 1)}},
                                            indent=1, default=str))
    print(f"Wrote {d / 'bots.txt'} ({len(bots)} bots). In NinjaTrader, add one BredBot strategy per bot and instrument")
    print(f"below ({sum(len(b['genes']['symbols']) for b in bots)} in all), each with a {interval}-minute data series, "
          "'Days to load' 150, and your DEMO (or Sim) account:\n")
    print(f"{'Bot':4} {'instruments':28} {'status':10} breeder id / what it does")
    for i, b in enumerate(bots, 1):
        roots = " ".join(NT_ROOT[s] for s in b["genes"]["symbols"])
        print(f"B{i:<3} {roots:28} {b['status'].split(' ')[0]:10} {b['id']}: {b['about']}")
    print(f"\nRisk: ${risk['bot_daily_loss_usd']:.0f} a day per bot, ${risk['all_bots_daily_loss_usd']:.0f} for all bots, "
          f"{risk['max_open_positions']} open positions max, {risk['max_trades_all_bots']} trades a day max, 1 contract a trade.")
    if any(not b["status"].startswith("promoted") for b in bots):
        print("Note: bots marked 'candidate' haven't passed every hall-of-fame check yet. The paper test IS their "
              "forward test; judge them with `paper compare` after 4-6 weeks.")


def install(home: Path | None) -> None:
    if home is None or not home.exists():
        sys.exit("Can't find Documents\\NinjaTrader 8. Install NinjaTrader 8 and start it once, "
                 "or pass --nt-home \"C:\\Users\\<you>\\Documents\\NinjaTrader 8\".")
    dst = home / "bin" / "Custom" / "Strategies"
    dst.mkdir(parents=True, exist_ok=True)
    for f in NT_FILES:
        shutil.copy2(REPO / "ninjatrader" / f, dst / f)
        print(f"copied {f} -> {dst / f}")
    print("Now in NinjaTrader: New > NinjaScript Editor, open BredBot, press F5 to compile.")


# ---------------------------------------------------------------------------- status
def _read_csv(p: Path) -> list[dict]:
    if not p.exists():
        return []
    with p.open(newline="") as f:
        return list(csv.DictReader(f))


def status(d: Path) -> None:
    kill = next((k for k in (d / "KILL", d / "KILL.txt") if k.exists()), None)
    print(f"Folder: {d}")
    print(f"Kill switch: {'ON (' + (kill.read_text().strip() or kill.name) + ')' if kill else 'off'}")
    trades = _read_csv(d / "paper_trades.csv")
    today = max((t["day"] for t in trades), default=None)
    if today:
        tt = [t for t in trades if t["day"] == today]
        print(f"\nLast trading day with trades: {today}")
        by: dict[str, list[dict]] = {}
        for t in tt:
            by.setdefault(t["bot"], []).append(t)
        for b, xs in sorted(by.items()):
            pnl = sum(float(x["pnl_usd"]) for x in xs)
            print(f"  {b}: {len(xs)} trades, {pnl:+.2f} USD, "
                  f"{sum(float(x['r']) for x in xs):+.2f}R  ({', '.join(x['instrument'] + ' ' + x['reason'] for x in xs)})")
        print(f"All paper trades so far: {len(trades)}, {sum(float(t['pnl_usd']) for t in trades):+.2f} USD")
    else:
        print("\nNo paper trades yet.")
    ev = _read_csv(d / "events.csv")
    if ev:
        print("\nRecent events:")
        for e in ev[-15:]:
            print(f"  {e['time_et']} ET  {e['bot']:3} {e['instrument']:12} {e['event']:12} {e['detail']}")


# ---------------------------------------------------------------------------- compare
def _band(rs: list[float], n: int) -> tuple[float, float, float] | None:
    """Mean R a trade the backtest expects, and the 95% range for the average of n paper trades."""
    if len(rs) < 5 or n == 0:
        return None
    mu, sd = statistics.fmean(rs), statistics.pstdev(rs)
    half = 1.96 * sd / math.sqrt(n)
    return mu, mu - half, mu + half


def compare(d: Path, replay: bool = True, cache: str = "backtest_cache/futures", archive: str | None = None) -> str:
    meta_p = d / "bots.json"
    trades = _read_csv(d / "paper_trades.csv")
    if not meta_p.exists():
        return f"No {meta_p}. Run `python -m tradebot paper export` first."
    meta = json.loads(meta_p.read_text())
    bots = meta["bots"]
    out = [f"# Paper vs backtest ({datetime.now(timezone.utc):%Y-%m-%d})", "",
           f"{len(trades)} paper trades in {d / 'paper_trades.csv'}.", ""]
    out += ["| bot | paper trades | win rate (95%) | R a trade | USD | backtest R a trade | 95% range for this many trades | verdict |",
            "|-----|---|---|---|---|---|---|---|"]
    for slot, b in bots.items():
        xs = [t for t in trades if t["bot"] == slot and t.get("bot_key", b["key"]) in (b["key"], "")]
        n = len(xs)
        rs = [float(t["r"]) for t in xs]
        wins = sum(1 for t in xs if float(t["pnl_usd"]) > 0)
        lo, hi = ci95(wins, n)
        exp = b.get("expect") or {}
        band = _band(b.get("r_list") or [], n)
        mean = statistics.fmean(rs) if rs else 0.0
        if n < 10:
            verdict = "too few trades to judge"
        elif band and mean < band[1]:
            verdict = "WORSE than the backtest (outside its range)"
        elif band and mean > band[2]:
            verdict = "better than the backtest"
        elif band:
            verdict = "in line with the backtest"
        else:
            verdict = "in line?" if mean > 0 else "losing"
        rng = f"{band[1]:+.2f} to {band[2]:+.2f}R" if band else "-"
        out.append(f"| {slot} {b['id']} | {n} | {wins / n if n else 0:.0%} ({lo:.0%}–{hi:.0%}) | {mean:+.3f}R | "
                   f"{sum(float(t['pnl_usd']) for t in xs):+,.0f} | {exp.get('exp_r', 0):+.3f}R "
                   f"({exp.get('trades', 0)} trades) | {rng} | {verdict} |")
    if replay and trades:
        out += ["", "## Same days, same bot, backtest vs paper", ""]
        try:
            out += replay_match(trades, bots, cache, archive, int(meta.get("interval", 5)))
        except Exception as e:  # network trouble shouldn't hide the table above
            out.append(f"(replay skipped: {e})")
    out += ["", "Judge after 4-6 weeks, not days. A bot that lands below its range should be dropped, not tuned."]
    text = "\n".join(out) + "\n"
    try:
        (d / "compare.md").write_text(text)
    except OSError:
        pass
    return text


def replay_match(trades: list[dict], bots: dict, cache: str, archive: str | None, interval: int = 5) -> list[str]:
    """Re-run the backtest on the days and instruments that were paper traded and pair the trades up."""
    from ..breeder.data import fetch_yahoo
    from ..breeder.sim import run

    days = sorted({t["day"] for t in trades})
    syms = sorted({s for b in bots.values() for s in b["genes"]["symbols"]})
    data = fetch_yahoo(syms, interval, cache, archive)
    have = {s.day.isoformat() for ss in data.values() for s in ss}
    want = {date.fromisoformat(x) for x in days if x in have}
    lines = [f"Backtest data covers {len(want)} of the {len(days)} paper days (Yahoo keeps ~60 days).", "",
             "| bot | matched | backtest only | paper only | avg entry slip (ticks) | paper R - backtest R |",
             "|---|---|---|---|---|---|"]
    for slot, b in bots.items():
        bt = run(b["genes"], data, want)
        pp = [t for t in trades if t["bot"] == slot and t["day"] in {d.isoformat() for d in want}]
        used, pairs = set(), []
        for p in pp:
            pm = int(p["entry_time"][:2]) * 60 + int(p["entry_time"][3:5])
            best = None
            for j, t in enumerate(bt):
                if j in used or t.symbol != p["symbol"] or t.day != p["day"] or (t.direction > 0) != (p["dir"] == "long"):
                    continue
                if abs(t.entry_t - pm) <= 15 and (best is None or abs(t.entry_t - pm) < abs(bt[best].entry_t - pm)):
                    best = j
            if best is not None:
                used.add(best)
                pairs.append((p, bt[best]))
        slips = [(float(p["entry"]) - t.entry) * t.direction / CONTRACTS[t.symbol].tick for p, t in pairs]
        dr = [float(p["r"]) - t.r for p, t in pairs]
        lines.append(f"| {slot} | {len(pairs)} | {len(bt) - len(pairs)} | {len(pp) - len(pairs)} | "
                     f"{statistics.fmean(slips) if slips else 0:+.1f} | {statistics.fmean(dr) if dr else 0:+.3f}R |")
    lines += ["", "Matched = same instrument, day and direction, entries within 15 minutes. Positive slip = paper "
              "filled worse. Many 'only' trades mean the live data or fills differ from Yahoo's bars."]
    return lines
