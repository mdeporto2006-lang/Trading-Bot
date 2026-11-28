"""`backtest` and `timemachine` commands."""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timedelta, timezone

from ..clock import ET
from ..config import Config
from .data import AlpacaHistory, CachedSource, SyntheticHistory, YahooHistory
from .engine import BacktestEngine, BacktestOptions
from .library import LessonLibrary
from .pit import PitOptions
from .report import save, text_summary
from .rules_planner import RulesPlanner


def add_parsers(sub) -> None:
    p = sub.add_parser("backtest", help="replay past days with the same checklist, risk gate and exits")
    p.add_argument("tickers", nargs="*", help="defaults to trading.watchlist")
    p.add_argument("--start", help="first day, YYYY-MM-DD (default: 20 trading days ago)")
    p.add_argument("--end", help="last day, YYYY-MM-DD (default: yesterday)")
    p.add_argument("--days", type=int, default=20, help="trading days to test when --start is not given")
    _source_args(p)
    p.add_argument("--planner", choices=["rules", "claude"], default="rules",
                   help="rules: the code's checklist only, free. claude: Claude plans each candidate setup (costs money)")
    p.add_argument("--estimate", action="store_true", help="only estimate what --planner claude would cost, then stop")
    p.add_argument("--yes", action="store_true", help="run a Claude backtest even if it is estimated above backtest.max_cost_usd")
    p.add_argument("--max-calls", type=int, default=None, help="hard cap on Claude calls")
    p.add_argument("--no-library", action="store_true", help="don't show the planner similar past setups")
    p.add_argument("--learn", action="store_true", help="add this run's trades to the lesson library")

    t = sub.add_parser("timemachine", help="rewind to a past moment, plan with only what was known, then fast-forward and score")
    t.add_argument("tickers", nargs="*", help="defaults to trading.watchlist")
    t.add_argument("--at", action="append", default=[],
                   help='a past moment in ET, "YYYY-MM-DD HH:MM" (repeatable)')
    t.add_argument("--sample", type=int, default=0, help="visit this many random moments between --start and --end")
    t.add_argument("--start", help="YYYY-MM-DD for --sample")
    t.add_argument("--end", help="YYYY-MM-DD for --sample")
    t.add_argument("--seed", type=int, default=1)
    _source_args(t)
    t.add_argument("--planner", choices=["rules", "claude"], default="rules")
    t.add_argument("--json", action="store_true", help="print full JSON for each visit")


def _source_args(p) -> None:
    p.add_argument("--source", choices=["auto", "alpaca", "yahoo", "synthetic"], default="auto",
                   help="auto: Alpaca if ALPACA_API_KEY is set, else Yahoo (no keys, last ~30 days, no news). "
                        "synthetic: made-up prices for trying the tool offline")
    p.add_argument("--catalyst", choices=["auto", "real", "proxy", "skip"], default="auto",
                   help="real: needs a news source (Alpaca). proxy: a big opening gap stands in for news. "
                        "skip: don't check. auto: real if the source has news, else proxy")


def _source(args, cfg: Config):
    name = args.source
    if name == "auto":
        name = "alpaca" if os.environ.get("ALPACA_API_KEY") and os.environ.get("ALPACA_SECRET_KEY") else "yahoo"
    if name == "alpaca":
        key, secret = os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_SECRET_KEY")
        if not key or not secret:
            sys.exit("--source alpaca needs ALPACA_API_KEY and ALPACA_SECRET_KEY. See README > Backtesting.")
        inner = AlpacaHistory(key, secret, feed=cfg.backtest.data_feed)
    elif name == "yahoo":
        inner = YahooHistory()
    else:
        inner = SyntheticHistory()
    if name == "synthetic":
        return inner
    return CachedSource(inner, cfg.backtest.cache_dir)


def _pit(args, cfg: Config, src) -> PitOptions:
    mode = args.catalyst
    if mode == "auto":
        mode = "real" if src.has_news else "proxy"
    if mode == "real" and not src.has_news:
        sys.exit(f"--catalyst real needs news, and the {src.name} source has none. Use --catalyst proxy or Alpaca keys.")
    return PitOptions(catalyst=mode, proxy_gap_pct=cfg.backtest.proxy_gap_pct,
                      assume_spread_pct=cfg.backtest.assume_spread_pct)


def _warnings(src, pit: PitOptions, planner: str) -> list[str]:
    w = []
    if src.name == "synthetic":
        w.append("SYNTHETIC DATA: prices and news are made up. These numbers say nothing about real performance.")
    if pit.catalyst == "proxy":
        w.append(f"No news in this data: a gap of {pit.proxy_gap_pct:g}%+ at the open stands in for the catalyst check.")
    if pit.catalyst == "skip":
        w.append("Catalyst check skipped.")
    if not src.has_quotes:
        w.append(f"No quotes in this data: spread assumed {pit.assume_spread_pct:g}%.")
    if src.name == "yahoo":
        w.append("Yahoo volume is consolidated volume; Alpaca IEX volume (the live default feed) is far lower.")
    if planner == "claude":
        w.append("Claude may know how older dates played out from its training; recent dates are the fairest test.")
    return w


def _default_range(args) -> tuple[date, date]:
    end = date.fromisoformat(args.end) if args.end else datetime.now(ET).date() - timedelta(days=1)
    if args.start:
        return date.fromisoformat(args.start), end
    d, n = end, 0
    while n < args.days:
        if d.weekday() < 5:
            n += 1
        start = d
        d -= timedelta(days=1)
    return start, end


def _claude(cfg: Config):
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("--planner claude needs ANTHROPIC_API_KEY. See README > Backtesting.")
    from ..planner import ClaudePlanner

    p = ClaudePlanner(cfg)
    p.label = f"claude ({cfg.planner.model}, effort {cfg.planner.effort})"
    return p


def run_backtest(args, cfg: Config) -> None:
    from .cost import estimate

    tickers = [t.upper() for t in (args.tickers or cfg.trading.watchlist)]
    if not tickers:
        sys.exit("No tickers given and trading.watchlist is empty.")
    start, end = _default_range(args)
    src = _source(args, cfg)
    pit = _pit(args, cfg, src)
    bt = cfg.backtest
    opts = BacktestOptions(start_equity=bt.start_equity, slippage_bps=bt.slippage_bps, pit=pit,
                           use_library=bt.show_similar_setups and not args.no_library,
                           max_planner_calls=args.max_calls)
    library = LessonLibrary(bt.library_path) if opts.use_library else LessonLibrary()
    say = lambda s: print(s, file=sys.stderr)
    say(f"Backtesting {', '.join(tickers)} {start} to {end} on {src.name} data ({args.planner} planner)...")

    if args.planner == "claude" or args.estimate:
        # A free rules pass counts the setups Claude would be asked about.
        dry = BacktestEngine(cfg, src, RulesPlanner(cfg), opts, LessonLibrary(), progress=say)
        dry.run(tickers, start, end)
        sample = None
        est = estimate(cfg, dry.decisions.prefilter_pass, sample)
        say(f"Claude planner estimate: {est['calls']} calls x ~${est['usd_per_call']:.3f} "
            f"= ~${est['usd_total']:.2f} ({est['model']}, effort {est['effort']}, "
            f"~{est['input_tokens']} in / ~{est['output_tokens']} out tokens per call). "
            f"Only setups that pass catalyst, volume, liquidity and trend are sent to Claude.")
        if args.estimate:
            return
        if est["usd_total"] > bt.max_cost_usd and not args.yes:
            sys.exit(f"Estimated ${est['usd_total']:.2f} is above backtest.max_cost_usd (${bt.max_cost_usd:.2f}). "
                     f"Re-run with --yes, a shorter range, or --max-calls N.")
        planner = _claude(cfg)
    else:
        planner = RulesPlanner(cfg)
        planner.label = "rules (code checklist only)"

    eng = BacktestEngine(cfg, src, planner, opts, library, progress=say)
    eng.run(tickers, start, end)
    meta = {
        "tickers": tickers, "start": start.isoformat(), "end": end.isoformat(), "source": src.name,
        "planner": planner.label, "catalyst": pit.catalyst, "start_equity": bt.start_equity,
        "slippage_bps": bt.slippage_bps, "warnings": _warnings(src, pit, args.planner),
        "settings": {"setup": cfg.setup.model_dump(), "exits": cfg.exits.model_dump(),
                     "session": cfg.session.model_dump(), "risk": cfg.risk.model_dump()},
    }
    run_dir, result = save(eng, meta, bt.results_dir)
    if args.learn:
        n = eng.learn(LessonLibrary(bt.library_path))
        say(f"Added {n} trades to the lesson library ({bt.library_path}).")
    print(text_summary(result))
    print(f"  saved {run_dir}/results.json and trades.csv; the dashboard shows the latest run")


def run_timemachine(args, cfg: Config) -> None:
    from .timemachine import append_log, random_moments, visit

    tickers = [t.upper() for t in (args.tickers or cfg.trading.watchlist)]
    if not tickers:
        sys.exit("No tickers given and trading.watchlist is empty.")
    moments = []
    for s in args.at:
        try:
            moments.append(datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=ET).astimezone(timezone.utc))
        except ValueError:
            sys.exit(f'--at "{s}": use "YYYY-MM-DD HH:MM" in ET')
    if args.sample:
        if not (args.start and args.end):
            sys.exit("--sample needs --start and --end")
        moments += random_moments(date.fromisoformat(args.start), date.fromisoformat(args.end), args.sample, cfg, args.seed)
    if not moments:
        sys.exit('Give --at "YYYY-MM-DD HH:MM" (ET) or --sample N --start ... --end ...')
    now = datetime.now(timezone.utc)
    if any(m > now - timedelta(minutes=16) for m in moments):
        sys.exit("The time machine only goes to the past (at least 16 minutes ago).")

    src = _source(args, cfg)
    pit = _pit(args, cfg, src)
    planner = _claude(cfg) if args.planner == "claude" else RulesPlanner(cfg)
    if args.planner == "rules":
        planner.label = "rules (code checklist only)"
    library = LessonLibrary(cfg.backtest.library_path)
    log_path = os.path.join(os.path.dirname(cfg.trading.journal_path) or ".", "timemachine.jsonl")
    for m in moments:
        for t in tickers:
            rec = visit(cfg, src, t, m, planner, pit, library, cfg.backtest.start_equity)
            append_log(log_path, rec)
            if args.json:
                print(json.dumps(rec, indent=2, default=str))
            else:
                print(_line(rec))
    print(f"logged to {log_path}; scored setups added to {cfg.backtest.library_path}")


def _line(rec: dict) -> str:
    at = datetime.fromisoformat(rec["at"]).astimezone(ET).strftime("%Y-%m-%d %H:%M ET")
    p, d, o, nx = rec["plan"], rec["decision"], rec["outcome"], rec["what_happened_next"]
    head = f"{at}  {rec['ticker']:<5} {p['action']:<8}"
    if p["action"] == "NO_TRADE":
        why = (p.get("invalidation") or "")[:90]
        s = f"{head} {why}"
    else:
        s = (f"{head} entry {p['entry_trigger']} stop {p['stop']} target {p['target']}  "
             f"gate {'APPROVED' if d['approved'] else 'REJECTED: ' + '; '.join(d['reasons'])[:80]}")
        if o:
            s += f"  -> {o['exit_reason']} {o['r_multiple']:+.2f}R" if o["triggered"] else "  -> entry never triggered"
    if nx:
        s += f"  | then: high {nx['high_after']:.2f} low {nx['low_after']:.2f} 15:50 {nx['close_at_flatten']:.2f}"
    sim = rec.get("similar_setups")
    if sim:
        s += f"  | {sim['count']} similar past setups: {sim['wins']} won, avg {sim['avg_r']:+.2f}R"
    return s
