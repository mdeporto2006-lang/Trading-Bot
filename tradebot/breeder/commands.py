"""`breed` command: evolve futures day-trading bots and report honestly."""

from __future__ import annotations

import sys
from dataclasses import replace

from .contracts import UNIVERSE
from .data import fetch_yahoo, shuffled, synthetic
from .evolve import Breeder, BreedOptions
from .report import build, save

WARNINGS = [
    "Free data is short: Yahoo keeps 5-minute bars for ~60 days (about 40 sessions) and 1-minute bars for ~30 days. "
    "That is a few weeks of one market regime; a bot bred on it can be luck.",
    "Continuous front-month prices: contract roll days can show a jump.",
    "Fills are simulated on bars: stop first if a bar hits both, targets need a trade through by one tick, "
    "1 tick slippage on stops and market orders. Real fills can be worse in fast markets.",
    "Each instrument trades one contract independently; no portfolio margin or overlap limits.",
    "Alpaca does not trade futures. Trading these bots live needs a futures broker (e.g. Interactive Brokers, "
    "Tradovate, NinjaTrader), and they should be paper traded first.",
    "Strategy families are public, documented types (opening range breakout, VWAP reversion, intraday momentum, "
    "trend following). They are not any bank's proprietary strategies, which aren't public.",
]


def add_parser(sub) -> None:
    p = sub.add_parser("breed", help="evolve futures day-trading bots (rules only, no API costs, no orders)")
    p.add_argument("--source", choices=["yahoo", "synthetic"], default="yahoo")
    p.add_argument("--symbols", nargs="*", default=UNIVERSE)
    p.add_argument("--interval", type=int, choices=[1, 5], default=5, help="bar size in minutes (5m: ~60 days, 1m: ~30)")
    p.add_argument("--population", type=int, default=100)
    p.add_argument("--generations", type=int, default=50)
    p.add_argument("--seeds", type=int, default=3, help="independent breeding runs; the champion is picked by VALIDATE")
    p.add_argument("--min-trades", type=int, default=30, help="trades needed on TRAIN to count as passing")
    p.add_argument("--target", type=float, default=0.60, help="win rate goal")
    p.add_argument("--no-null", action="store_true", help="skip the luck test on scrambled prices")
    p.add_argument("--cache", default="backtest_cache/futures")
    p.add_argument("--out", default="breeder_runs")
    p.add_argument("--archive", default=None,
                   help="folder of saved bars: each Yahoo fetch is merged in, so history grows past 60 days")
    p.add_argument("--hall-of-fame", default=None,
                   help="hall of fame JSON to update with this run (nominate, re-score, promote, retire)")


def _summ(b: Breeder, sel: dict, seed: int) -> dict:
    c = b.bots[sel["champion"]]
    return {"seed": seed, "id": c["id"], "family": c["family"], "about": c["about"], "train": c["train"],
            "validate": c.get("validate"), "holdout": c.get("holdout"), "val_passes": c.get("val_passes"),
            "val_fitness": c.get("val_fitness")}


def run_breed(args) -> None:
    say = lambda s: print(s, file=sys.stderr, flush=True)
    if args.source == "yahoo":
        data = fetch_yahoo(args.symbols, args.interval, args.cache, getattr(args, "archive", None))
    else:
        data = synthetic(args.symbols, days=40, interval=args.interval)
    data = {k: v for k, v in data.items() if v}
    if not data:
        sys.exit("No futures data came back. See the log above.")
    days = sorted({s.day for ss in data.values() for s in ss})
    say(f"{len(days)} sessions {days[0]} → {days[-1]} on {', '.join(data)}")
    opts = BreedOptions(population=args.population, generations=args.generations, min_trades=args.min_trades,
                        target_win_rate=args.target)
    runs = []
    for k in range(args.seeds):
        say(f"--- breeding run {k + 1}/{args.seeds} (seed {k + 1}) ---")
        b = Breeder(data, replace(opts, seed=k + 1), progress=say)
        b.evolve()
        sel = b.select()
        runs.append((b, sel, _summ(b, sel, k + 1)))
    main, sel, _ = max(runs, key=lambda r: (bool(r[2]["val_passes"]), r[2]["val_fitness"]))
    null = None
    if not args.no_null:
        say("--- luck test: same breeder on scrambled prices ---")
        nb = Breeder(shuffled(data), replace(opts, seed=1), progress=say)
        nb.evolve()
        nsel = nb.select()
        best_wr = max((x["train"]["win_rate"] for x in nb.bots.values() if x["passes"]), default=None)
        null = {"champion": _summ(nb, nsel, 1), "best_train_win_rate": best_wr,
                "passing_bots": sum(1 for x in nb.bots.values() if x["passes"]), "bots": len(nb.bots)}
    meta = {"source": args.source, "interval": args.interval, "symbols": list(data),
            "sessions": len(days), "first_day": days[0].isoformat(), "last_day": days[-1].isoformat(),
            "warnings": WARNINGS + (["SYNTHETIC DATA: made-up prices. Says nothing about real markets."]
                                    if args.source == "synthetic" else [])}
    rep = build(main, sel, [r[2] for r in runs], null, meta)
    paths = save(rep, args.out)
    print((paths["md"]).read_text())
    say(f"Saved {', '.join(str(p) for p in paths.values())}")
    if getattr(args, "hall_of_fame", None):
        from . import halloffame as hf
        hof = hf.load(args.hall_of_fame)
        hf.update(hof, [(b, s, m) for b, s, m in runs], data, opts.slip_ticks, progress=say)
        path = hf.save(hof, args.hall_of_fame)
        print(path.with_suffix(".md").read_text())
        say(f"Saved {path} and {path.with_suffix('.md')}")
