"""Write the parity test input: synthetic sessions, many genomes and the Python sim's trades.

The C# harness (tools/parity) replays the same sessions through ninjatrader/BredBotCore.cs and
tools/parity/compare.py checks both produce the same trades. Run from the repo root:

    python tools/parity/dump.py parity_in.json            # synthetic prices
    python tools/parity/dump.py parity_in.json yahoo      # real Yahoo bars (network)
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tradebot.breeder import genome as gm  # noqa: E402
from tradebot.breeder.contracts import CONTRACTS, NT_ROOT, hhmm  # noqa: E402
from tradebot.breeder.data import TREND_LOOKBACKS, _atr, synthetic  # noqa: E402
from tradebot.breeder.sim import simulate_session  # noqa: E402


def genomes(n_random: int = 400) -> list[dict]:
    rng = random.Random(2026)
    out = [{"id": f"textbook-{f}", "genes": gm.textbook(f, rng)} for f in gm.FAMILIES]
    for k in range(n_random):
        g = gm.random_genome(rng, gm.FAMILIES[k % 4])
        if k % 3 == 0:  # make trades likelier: wide windows, loose thresholds
            g.update(entry_start_min=0, entry_end_min=30, mom_threshold_atr=0.0, dev_k=1.2, or_min_atr=0.0,
                     or_max_atr=1.5, max_trades_day=3, pull_touch_atr=0.1)
        out.append({"id": f"random-{k}", "genes": g})
    for p in ("docs/breeder/latest.json", "docs/breeder/hall_of_fame.json"):
        f = Path(p)
        if f.exists():
            d = json.loads(f.read_text())
            if "champion" in d and isinstance(d["champion"], dict):
                out.append({"id": "champion", "genes": d["champion"]["genes"]})
            for k, b in (d.get("bots") or {}).items() if p.endswith("hall_of_fame.json") else []:
                out.append({"id": f"hof-{k}", "genes": b["genes"]})
    return out


def daily_checks(rng: random.Random) -> list[dict]:
    out = []
    for n in (3, 14, 15, 16, 25, 40, 61, 70, 120):
        px, rows = 100.0, []
        for _ in range(n):
            o = px * (1 + rng.gauss(0, 0.003))
            c = o * (1 + rng.gauss(0, 0.01))
            rows.append([o, max(o, c) * (1 + abs(rng.gauss(0, 0.004))), min(o, c) * (1 - abs(rng.gauss(0, 0.004))), c])
            px = c
        trend = {str(lb): (rows[-1][3] / rows[-1 - lb][3] - 1) * 100 for lb in TREND_LOOKBACKS if len(rows) > lb}
        out.append({"daily": rows, "prev_close": rows[-1][3], "atr": _atr(rows[-30:]), "trend": trend})
    return out


def main(path: str, source: str = "synthetic") -> None:
    if source == "yahoo":  # real bars (needs the network); CI runs both
        from tradebot.breeder.data import fetch_yahoo
        data = {k: v for k, v in fetch_yahoo(list(CONTRACTS), 5, "backtest_cache/futures").items() if v}
    else:
        data = synthetic(list(CONTRACTS), days=30, interval=5, seed=11)
    sessions = [s for ss in data.values() for s in ss]
    gs = genomes()
    trades = {}
    for g in gs:
        rows = []
        for s in sessions:
            if s.symbol in g["genes"]["symbols"]:
                for t in simulate_session(g["genes"], s, 1.0):
                    rows.append({"symbol": t.symbol, "day": t.day, "dir": t.direction, "entry_t": t.entry_t,
                                 "exit_t": t.exit_t, "entry": t.entry, "exit": t.exit, "stop": t.stop,
                                 "target": t.target, "reason": t.reason, "pnl": t.pnl, "r": t.r})
        trades[g["id"]] = rows
    specs = {sym: {"nt_root": NT_ROOT[sym], "tick": c.tick, "multiplier": c.multiplier,
                   "commission_rt": c.commission_rt, "open_min": hhmm(c.session_et[0]),
                   "close_min": hhmm(c.session_et[1])} for sym, c in CONTRACTS.items()}
    doc = {
        "interval": 5, "slip_ticks": 1.0, "specs": specs, "genomes": gs,
        "sessions": [{"symbol": s.symbol, "day": s.day.isoformat(), "t": s.t, "o": s.o, "h": s.h, "l": s.l,
                      "c": s.c, "v": s.v, "prev_close": s.prev_close, "atr": s.atr,
                      "trend": {str(k): v for k, v in s.trend.items()}} for s in sessions],
        "daily_checks": daily_checks(random.Random(5)),
        "expected": trades,
    }
    Path(path).write_text(json.dumps(doc))
    # The same genomes as a bots.txt, so the C# side also proves it reads the export format right.
    from tradebot.paper.commands import bots_txt
    txt = bots_txt([{"id": g["id"], "key": gm.key(g["genes"]), "status": "test", "genes": g["genes"],
                     "about": gm.describe(g["genes"]), "expect": None} for g in gs], 5, {"bot_daily_loss_usd": 250.0})
    Path(path).with_suffix(".bots.txt").write_text(txt)
    n = sum(len(v) for v in trades.values())
    print(f"{len(gs)} genomes, {len(sessions)} sessions, {n} Python trades -> {path}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "parity_in.json", sys.argv[2] if len(sys.argv) > 2 else "synthetic")
