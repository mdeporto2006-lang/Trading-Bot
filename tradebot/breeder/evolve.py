"""The breeder: a genetic algorithm over strategy genomes, with a family tree.

How a run works:
1. Split the sessions by date: TRAIN (first half), VALIDATE (next quarter),
   HOLDOUT (last quarter). Evolution only ever sees TRAIN.
2. Generation 0: the textbook version of each family plus random bots.
3. Each generation: the best bots survive unchanged (elites, at least one per
   family so no idea dies too early), the rest are replaced by children of
   tournament-picked parents: crossover and/or mutation. Every bot's parents,
   mutations and scores are recorded: that's the ancestry tree.
4. After the last generation the best distinct bots on TRAIN are the
   finalists. Each is scored once on VALIDATE; the best there is the champion.
5. Only the champion's HOLDOUT result is the honest number. Holdout days
   played no part in breeding or picking.

Fitness, built so the easy fake answer loses:
- A tiny target with a wide stop wins often and loses money. So a bot only
  scores as "passing" with expectancy after costs above +0.05R a trade,
  profit factor above 1.15 and at least `min_trades` trades (on validate and
  holdout: above 0R and 1.0). Failing bots always rank below
  passing ones.
- It must pass in both halves of the train days on its own (half the trade
  minimum each), and it's scored on its worse half.
- Passing bots are ranked by the lower edge of the win rate's confidence
  band (Wilson), so 14 wins of 20 doesn't beat 70 of 110, plus a bonus for
  expectancy in R.
"""

from __future__ import annotations

import logging
import math
import random
import statistics
from dataclasses import dataclass
from datetime import date

from . import genome as gm
from .data import Session, split_days
from .sim import metrics, run, wilson_low

log = logging.getLogger(__name__)

KEEP = ("trades", "wins", "win_rate", "exp_r", "exp_usd", "pf", "total_usd", "total_r", "max_dd_r",
        "avg_win_r", "avg_loss_r", "days_traded", "exits")


@dataclass
class BreedOptions:
    population: int = 80
    generations: int = 40
    seed: int = 1
    min_trades: int = 30  # on TRAIN; scaled down for the shorter splits
    elite_frac: float = 0.1
    crossover_rate: float = 0.5
    mutation_rate: float = 0.15
    tournament: int = 3
    finalists: int = 30
    slip_ticks: float = 1.0
    target_win_rate: float = 0.60


def passes(m: dict, min_trades: int, strict: bool = False) -> bool:
    """The money gates. On TRAIN (strict) a bot needs a margin of safety:
    +0.05R a trade and profit factor 1.15, because the breeder will find bots
    that scrape past zero by luck. On VALIDATE/HOLDOUT, above zero is enough."""
    er, pf = (0.05, 1.15) if strict else (0.0, 1.0)
    return m["trades"] >= min_trades and m["exp_r"] > er and m["pf"] > pf


def fitness(m: dict, min_trades: int, strict: bool = False) -> float:
    n = m["trades"]
    if not passes(m, min_trades, strict):
        return round(-1 + 0.3 * min(1.0, n / max(1, min_trades)) + 0.3 * min(1.0, m["pf"]) + 0.2 * m["win_rate"], 5)
    return round(wilson_low(m["wins"], n) + 0.3 * math.tanh(2 * m["exp_r"]), 5)


def _slim(m: dict) -> dict:
    return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in m.items() if k in KEEP}


class Breeder:
    def __init__(self, data: dict[str, list[Session]], opts: BreedOptions | None = None,
                 universe: list[str] | None = None, progress=None):
        self.data = data
        self.opts = opts or BreedOptions()
        self.universe = universe or sorted(s for s, ss in data.items() if ss)
        self.rng = random.Random(self.opts.seed)
        self.train, self.val, self.hold = (set(x) for x in split_days(data))
        n_tr = max(1, len(self.train))
        tdays = sorted(self.train)
        self.train_mid = tdays[len(tdays) // 2].isoformat() if tdays else ""
        self.min_val = max(8, round(self.opts.min_trades * len(self.val) / n_tr))
        self.min_hold = max(8, round(self.opts.min_trades * len(self.hold) / n_tr))
        self.bots: dict[str, dict] = {}
        self.by_key: dict[str, str] = {}
        self.generations: list[dict] = []
        self.progress = progress or (lambda s: None)
        self._n = 0

    # ------------------------------------------------------------------
    def _score(self, g: dict, days: set) -> dict:
        return metrics(run(g, self.data, days, self.opts.slip_ticks))

    def _new_bot(self, g: dict, gen: int, parents: list[str], how: str, mutations: list[str]) -> str | None:
        k = gm.key(g)
        if k in self.by_key:
            return None
        self._n += 1
        bid = f"G{gen}-{self._n}"
        tr = run(g, self.data, self.train, self.opts.slip_ticks)
        m = metrics(tr)
        # Consistency: the bot must pass in BOTH halves of the train days, and
        # its fitness is the worse half. One lucky week can't carry it.
        half = self.opts.min_trades // 2
        ma = metrics([t for t in tr if t.day < self.train_mid])
        mb = metrics([t for t in tr if t.day >= self.train_mid])
        fit = min(fitness(ma, half, True), fitness(mb, half, True))
        ok = passes(m, self.opts.min_trades, True) and passes(ma, half, True) and passes(mb, half, True)
        self.bots[bid] = {
            "id": bid, "gen": gen, "parents": parents, "how": how, "mutations": mutations,
            "family": g["family"], "genes": g, "about": gm.describe(g), "train": _slim(m),
            "fitness": fit if ok else min(fit, fitness(m, self.opts.min_trades, True)), "passes": ok,
            "alive_until": gen, "children": 0, "elite_gens": [],
        }
        self.by_key[k] = bid
        for p in parents:
            self.bots[p]["children"] += 1
        return bid

    def _pick(self, pop: list[str]) -> str:
        cands = self.rng.sample(pop, min(self.opts.tournament, len(pop)))
        return max(cands, key=lambda b: self.bots[b]["fitness"])

    def _child(self, pop: list[str], gen: int) -> str | None:
        o = self.opts
        for _ in range(8):
            a = self._pick(pop)
            if self.rng.random() < o.crossover_rate:
                b = self._pick(pop)
                tries = 0
                while b == a and tries < 3:
                    b, tries = self._pick(pop), tries + 1
                g = gm.crossover(self.bots[a]["genes"], self.bots[b]["genes"], self.rng)
                parents = [a] if a == b else [a, b]
                how = "crossover"
                notes = [f"crossover of {a} × {b}"] if a != b else []
                if self.rng.random() < 0.7 or gm.key(g) in self.by_key:
                    g, more = gm.mutate(g, self.rng, o.mutation_rate, self.universe)
                    notes += more
            else:
                parents, how = [a], "mutation"
                g, notes = gm.mutate(self.bots[a]["genes"], self.rng, o.mutation_rate, self.universe)
            bid = self._new_bot(g, gen, parents, how, notes)
            if bid:
                return bid
        return None

    # ------------------------------------------------------------------
    def evolve(self) -> None:
        o = self.opts
        pop: list[str] = []
        for fam in gm.FAMILIES:
            bid = self._new_bot(gm.textbook(fam, self.rng, self.universe), 0, [], "textbook", [])
            if bid:
                pop.append(bid)
        i = 0
        while len(pop) < o.population:
            fam = gm.FAMILIES[i % len(gm.FAMILIES)]
            i += 1
            bid = self._new_bot(gm.random_genome(self.rng, fam, self.universe), 0, [], "random", [])
            if bid:
                pop.append(bid)
        self._gen_stats(0, pop)
        n_elite = max(len(gm.FAMILIES), round(o.population * o.elite_frac))
        for gen in range(1, o.generations + 1):
            ranked = sorted(pop, key=lambda b: self.bots[b]["fitness"], reverse=True)
            elites: list[str] = []
            for fam in gm.FAMILIES:  # keep the best of every family alive
                best = next((b for b in ranked if self.bots[b]["family"] == fam), None)
                if best:
                    elites.append(best)
            for b in ranked:
                if len(elites) >= n_elite:
                    break
                if b not in elites:
                    elites.append(b)
            for b in elites:
                self.bots[b]["alive_until"] = gen
                self.bots[b]["elite_gens"].append(gen)
            new = list(elites)
            while len(new) < o.population:
                c = self._child(ranked, gen)
                if c is None:
                    break
                new.append(c)
            pop = new
            self._gen_stats(gen, pop)
            st = self.generations[-1]
            self.progress(f"gen {gen:>3}: best fitness {st['best_fitness']:.3f}, best passing win rate "
                          f"{st['best_win_rate'] if st['best_win_rate'] is not None else '-'}, "
                          f"{st['passing']}/{len(pop)} pass the money gates")
        self.final_pop = pop

    def _gen_stats(self, gen: int, pop: list[str]) -> None:
        fits = [self.bots[b]["fitness"] for b in pop]
        ok = [self.bots[b] for b in pop if self.bots[b]["passes"]]
        fam = {f: sum(1 for b in pop if self.bots[b]["family"] == f) for f in gm.FAMILIES}
        best = max(pop, key=lambda b: self.bots[b]["fitness"])
        self.generations.append({
            "gen": gen, "best_fitness": max(fits), "median_fitness": statistics.median(fits),
            "best_win_rate": round(max(x["train"]["win_rate"] for x in ok), 4) if ok else None,
            "best_id": best, "passing": len(ok), "families": fam, "population": list(pop),
        })

    # ------------------------------------------------------------------
    def select(self) -> dict:
        """Finalists by TRAIN, champion by VALIDATE, then HOLDOUT once."""
        o = self.opts
        ranked = sorted((b for b in self.bots.values() if b["passes"]), key=lambda b: b["fitness"], reverse=True)
        finalists = ranked[: o.finalists]
        if not finalists:  # nothing passed: still report the best tries
            finalists = sorted(self.bots.values(), key=lambda b: b["fitness"], reverse=True)[:10]
        for b in finalists:
            mv = self._score(b["genes"], self.val)
            b["validate"] = _slim(mv)
            b["val_fitness"] = fitness(mv, self.min_val)
            b["val_passes"] = passes(mv, self.min_val)
            b["finalist"] = True
        champ = max(finalists, key=lambda b: (b["val_passes"], b["val_fitness"]))
        for b in finalists:  # shown for context, NOT used to pick the champion
            b["holdout"] = _slim(self._score(b["genes"], self.hold))
        champ["champion"] = True
        lineage = self.lineage(champ["id"])
        for bid in lineage:
            self.bots[bid]["on_champion_line"] = True
        for b in self.bots.values():
            if b["how"] == "textbook":
                for split, days in (("validate", self.val), ("holdout", self.hold)):
                    b.setdefault(split, _slim(self._score(b["genes"], days)))
        trades = {name: [t.to_row() for t in run(champ["genes"], self.data, days, o.slip_ticks)]
                  for name, days in (("train", self.train), ("validate", self.val), ("holdout", self.hold))}
        return {"champion": champ["id"], "finalists": [b["id"] for b in finalists],
                "lineage": lineage, "champion_trades": trades}

    def lineage(self, bid: str) -> list[str]:
        seen, stack = [], [bid]
        while stack:
            x = stack.pop()
            if x in seen:
                continue
            seen.append(x)
            stack.extend(self.bots[x]["parents"])
        return seen

    def days(self) -> dict:
        f = lambda s: sorted(d.isoformat() for d in s)
        return {"train": f(self.train), "validate": f(self.val), "holdout": f(self.hold)}


def ci95(wins: int, n: int) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    z = 1.96
    p = wins / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, centre - half), min(1.0, centre + half))
