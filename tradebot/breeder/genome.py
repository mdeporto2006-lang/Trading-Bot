"""The genome: every setting a bot can have, and how genes mutate and mix.

Each bot belongs to one strategy family. The families are documented, public
strategy types used across institutional desks and in published research. They
are NOT any bank's actual proprietary strategies, which are not public.

  orb             Opening range breakout: trade a break of the first N
                  minutes' high or low (Crabel 1990; Zarattini, Barbon & Aziz
                  2023 on index futures).
  vwap_revert     VWAP mean reversion: fade stretches far from the session's
                  volume-weighted average price, target back toward it.
  intraday_mom    Intraday momentum: the first part of the day's move predicts
                  the rest (Gao, Han, Li & Zhou 2018, "Market intraday momentum").
  trend_pullback  Time-series momentum / trend following (Moskowitz, Ooi &
                  Pedersen 2012): in the direction of the multi-day trend, buy
                  pullbacks to VWAP (sell rallies in a downtrend).

Every family can also use the time-series momentum trend filter.
Carry (term-structure roll yield) is not here: it needs the second-month
contract's price, which the free continuous series doesn't have, and it's a
multi-week holding strategy, not a day trade.

Genes not used by a bot's family are carried along unused, so a family switch
by mutation or crossover keeps working.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass

from .contracts import UNIVERSE

FAMILIES = ("orb", "vwap_revert", "intraday_mom", "trend_pullback")


@dataclass(frozen=True)
class G:
    kind: str  # "choice" | "float" | "int"
    values: tuple = ()  # choice values
    lo: float = 0
    hi: float = 0
    step: float = 1
    family: str | None = None  # only matters for this family


SPACE: dict[str, G] = {
    # shared: what to trade, when, which way
    "family": G("choice", FAMILIES),
    "direction": G("choice", ("both", "long", "short")),
    "trend_filter": G("choice", ("off", "with", "against")),
    "trend_lookback": G("choice", (5, 10, 20, 60)),
    "entry_start_min": G("int", lo=0, hi=120, step=5),  # minutes after the session opens
    "entry_end_min": G("int", lo=30, hi=240, step=5),  # no new entries in the last N minutes
    "flatten_before_min": G("int", lo=5, hi=60, step=5),  # flat N minutes before the session ends
    "max_trades_day": G("int", lo=1, hi=3),
    # exits
    "stop_mode": G("choice", ("atr", "structure")),
    "stop_atr": G("float", lo=0.04, hi=0.6, step=0.02),  # stop distance as a fraction of daily ATR
    "target_mode": G("choice", ("r", "vwap")),  # vwap only used by vwap_revert
    "target_r": G("float", lo=0.5, hi=4.0, step=0.25),  # below 0.5R costs eat the edge
    "breakeven_r": G("choice", (0, 0.3, 0.5, 0.75, 1.0, 1.5)),  # move stop to entry after this many R (0 off)
    "trail_after_r": G("choice", (0, 0.5, 1.0, 1.5, 2.0, 3.0)),  # 0 off
    "trail_dist_r": G("float", lo=0.25, hi=2.0, step=0.25),
    "time_stop_bars": G("choice", (0, 3, 6, 12, 24, 48)),  # exit after N bars (0 off)
    # orb
    "or_minutes": G("choice", (5, 10, 15, 30, 45, 60), family="orb"),
    "orb_buffer_ticks": G("int", lo=0, hi=8, family="orb"),
    "or_min_atr": G("float", lo=0.0, hi=0.3, step=0.02, family="orb"),
    "or_max_atr": G("float", lo=0.2, hi=1.5, step=0.05, family="orb"),
    # vwap_revert
    "dev_k": G("float", lo=1.0, hi=3.5, step=0.1, family="vwap_revert"),
    "vwap_confirm": G("choice", (False, True), family="vwap_revert"),
    "vwap_min_bars": G("int", lo=3, hi=24, family="vwap_revert"),
    # intraday_mom
    "mom_window_min": G("choice", (15, 30, 45, 60, 90), family="intraday_mom"),
    "mom_threshold_atr": G("float", lo=0.0, hi=0.4, step=0.02, family="intraday_mom"),
    "mom_from": G("choice", ("open", "prev_close"), family="intraday_mom"),
    # trend_pullback
    "pull_touch_atr": G("float", lo=0.0, hi=0.1, step=0.01, family="trend_pullback"),
    "pull_above_bars": G("int", lo=2, hi=12, family="trend_pullback"),
}

# Hand-written starting points, one per family: the textbook versions.
TEXTBOOK: dict[str, dict] = {
    "orb": dict(family="orb", or_minutes=30, orb_buffer_ticks=1, stop_mode="structure", target_r=2.0,
                or_min_atr=0.05, or_max_atr=0.8, entry_start_min=0, entry_end_min=120),
    "vwap_revert": dict(family="vwap_revert", dev_k=2.0, vwap_confirm=True, target_mode="vwap",
                        stop_mode="atr", stop_atr=0.15, entry_start_min=30, entry_end_min=60),
    "intraday_mom": dict(family="intraday_mom", mom_window_min=30, mom_from="prev_close",
                         mom_threshold_atr=0.1, stop_mode="atr", stop_atr=0.25, target_r=2.0,
                         entry_start_min=0, entry_end_min=30),
    "trend_pullback": dict(family="trend_pullback", trend_lookback=20, pull_touch_atr=0.02,
                           pull_above_bars=6, stop_mode="atr", stop_atr=0.15, target_r=1.5,
                           entry_start_min=15, entry_end_min=60),
}


def _snap(g: G, x: float):
    x = min(g.hi, max(g.lo, x))
    x = g.lo + round((x - g.lo) / g.step) * g.step
    return int(round(x)) if g.kind == "int" else round(x, 4)


def random_gene(name: str, rng: random.Random):
    g = SPACE[name]
    if g.kind == "choice":
        return rng.choice(g.values)
    return _snap(g, rng.uniform(g.lo, g.hi))


def random_symbols(rng: random.Random, universe=UNIVERSE) -> list[str]:
    k = rng.randint(1, len(universe))
    return sorted(rng.sample(list(universe), k))


def random_genome(rng: random.Random, family: str | None = None, universe=UNIVERSE) -> dict:
    gen = {k: random_gene(k, rng) for k in SPACE}
    if family:
        gen["family"] = family
    gen["symbols"] = random_symbols(rng, universe)
    return gen


def textbook(family: str, rng: random.Random, universe=UNIVERSE) -> dict:
    gen = random_genome(rng, family, universe)
    gen.update(direction="both", trend_filter="off", flatten_before_min=10, max_trades_day=1,
               breakeven_r=0, trail_after_r=0, time_stop_bars=0, target_mode="r")
    gen.update(TEXTBOOK[family])
    gen["symbols"] = sorted(universe)
    return gen


def relevant(name: str, family: str) -> bool:
    g = SPACE.get(name)
    return g is None or g.family is None or g.family == family


def mutate(gen: dict, rng: random.Random, rate: float = 0.15, universe=UNIVERSE) -> tuple[dict, list[str]]:
    """Returns (child, list of human-readable mutations). Always at least one
    mutation that matters to the bot's family."""
    child = dict(gen)
    notes: list[str] = []
    names = [k for k in SPACE if relevant(k, gen["family"])] + ["symbols"]

    def one(name: str) -> None:
        old = child[name]
        if name == "symbols":
            s = set(child["symbols"])
            sym = rng.choice(list(universe))
            if sym in s and len(s) > 1:
                s.remove(sym)
                notes.append(f"dropped {sym}")
            elif sym not in s:
                s.add(sym)
                notes.append(f"added {sym}")
            child["symbols"] = sorted(s)
            return
        g = SPACE[name]
        if g.kind == "choice":
            new = rng.choice([v for v in g.values if v != old] or [old])
        else:
            span = (g.hi - g.lo) * rng.choice((0.1, 0.25, 0.5))
            new = _snap(g, old + rng.gauss(0, span))
            if new == old:
                new = _snap(g, old + rng.choice((-1, 1)) * g.step)
        if new != old:
            child[name] = new
            notes.append(f"{name} {old}→{new}")

    for name in names:
        if name != "family" and rng.random() < rate:
            one(name)
    if rng.random() < 0.03:  # rare: jump to another family
        one("family")
    while not notes:
        one(rng.choice(names[1:]))
    return child, notes


def crossover(a: dict, b: dict, rng: random.Random) -> dict:
    """Uniform crossover. Family-specific genes come as a block from whichever
    parent supplies the family, so a working ORB setup isn't scrambled."""
    fam_from = a if rng.random() < 0.5 else b
    child = {}
    for k, g in SPACE.items():
        if k == "family" or g.family is not None:
            child[k] = fam_from[k]
        else:
            child[k] = (a if rng.random() < 0.5 else b)[k]
    syms = sorted({s for s in a["symbols"] + b["symbols"] if rng.random() < 0.5 or (s in a["symbols"] and s in b["symbols"])})
    child["symbols"] = syms or list(fam_from["symbols"])
    return child


def key(gen: dict) -> str:
    """Identity of the bot's behavior: unused genes don't count."""
    use = {k: v for k, v in gen.items() if relevant(k, gen["family"])}
    if gen["family"] != "vwap_revert":
        use.pop("target_mode", None)
    if gen["family"] == "trend_pullback":
        use.pop("trend_filter", None)
    if use.get("trail_after_r") == 0:
        use.pop("trail_dist_r", None)
    return hashlib.sha1(json.dumps(use, sort_keys=True, default=str).encode()).hexdigest()[:12]


def describe(gen: dict) -> str:
    f = gen["family"]
    parts = {
        "orb": f"{gen['or_minutes']}-min opening range breakout, {gen['orb_buffer_ticks']} tick buffer",
        "vwap_revert": f"fade {gen['dev_k']}σ from VWAP{' after a reversal bar' if gen['vwap_confirm'] else ''}",
        "intraday_mom": f"follow the first {gen['mom_window_min']} min move (from {gen['mom_from'].replace('_', ' ')}) "
                        f"if > {gen['mom_threshold_atr']} ATR",
        "trend_pullback": f"buy pullbacks to VWAP in the {gen['trend_lookback']}-day trend",
    }[f]
    stop = f"{gen['stop_atr']} ATR stop" if gen["stop_mode"] == "atr" else "structure stop"
    tgt = "target VWAP" if f == "vwap_revert" and gen["target_mode"] == "vwap" else f"{gen['target_r']}R target"
    extra = []
    if gen["direction"] != "both":
        extra.append(f"{gen['direction']} only")
    if f != "trend_pullback" and gen["trend_filter"] != "off":
        extra.append(f"{gen['trend_filter']} {gen['trend_lookback']}d trend")
    if gen["breakeven_r"]:
        extra.append(f"breakeven at {gen['breakeven_r']}R")
    if gen["trail_after_r"]:
        extra.append(f"trail {gen['trail_dist_r']}R after {gen['trail_after_r']}R")
    if gen["time_stop_bars"]:
        extra.append(f"time stop {gen['time_stop_bars']} bars")
    return f"{parts}; {stop}, {tgt}" + (f"; {', '.join(extra)}" if extra else "") + \
        f"; {' '.join(s.replace('=F', '') for s in gen['symbols'])}"
