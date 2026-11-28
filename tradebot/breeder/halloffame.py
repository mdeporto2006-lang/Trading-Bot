"""Hall of fame: bots that keep earning their place, night after night.

Every nightly breeding run nominates its best bots (each seed's champion, plus finalists that
also made money on the validate and holdout days). From then on, every night, every bot in the
hall is re-scored on all the data there is, and its results are kept day by day, so its record
grows past Yahoo's 60-day window.

A bot is PROMOTED (eligible for paper trading as proven, and later for one live micro contract)
only when all of these hold:

  forward      at least 20 trades on days AFTER it was first nominated (days that played no part
               in breeding or picking it), with expectancy above 0R and profit factor above 1.1
  record       at least 40 trades in total, +0.05R a trade or better
  costs        still positive with double slippage (2 ticks a fill)
  weeks        at least 4 weeks with trades, and 60% of them not losing
  regimes      positive in each market regime it traded at least 8 times in: S&P trend up vs down
               (20-day), and volatility high vs low (ATR vs its median)
  seeds        8 near-copies (each numeric gene nudged one step, random seeds 1-8): at least 6 of
               them still positive. A bot that only works with exactly these numbers is a fluke.

A promoted bot is RETIRED if it loses its edge (fails the record or forward checks after 30+
forward trades). A candidate is retired after 30 forward trades without a positive expectancy.
Retired bots stay in the file for the record but are never exported for trading.
"""

from __future__ import annotations

import json
import math
import random
import statistics
from datetime import date, datetime, timezone
from pathlib import Path

from . import genome as gm
from .data import Session
from .evolve import ci95
from .sim import run, wilson_low

RULES = {
    "forward_min_trades": 20, "forward_min_exp_r": 0.0, "forward_min_pf": 1.1,
    "record_min_trades": 40, "record_min_exp_r": 0.05,
    "min_weeks": 4, "weeks_ok_frac": 0.6,
    "regime_min_trades": 8,
    "jitter_n": 8, "jitter_ok": 6,
    "retire_after_forward": 30,
    "max_candidates": 60,
}
NOMINATE_PER_RUN = 4


def _stats(trades: list[list[float]]) -> dict:
    """trades: [r, pnl] pairs."""
    n = len(trades)
    if not n:
        return {"trades": 0, "wins": 0, "win_rate": 0.0, "exp_r": 0.0, "pf": 0.0, "total_r": 0.0, "total_usd": 0.0}
    wins = sum(1 for r, p in trades if p > 0)
    gw = sum(r for r, p in trades if p > 0)
    gl = -sum(r for r, p in trades if p <= 0)
    return {"trades": n, "wins": wins, "win_rate": round(wins / n, 4), "exp_r": round(sum(r for r, _ in trades) / n, 4),
            "pf": round(gw / gl, 3) if gl > 0 else (99.0 if gw > 0 else 0.0),
            "total_r": round(sum(r for r, _ in trades), 3), "total_usd": round(sum(p for _, p in trades), 2)}


def _by_day(trades) -> dict[str, list[list[float]]]:
    out: dict[str, list[list[float]]] = {}
    for t in trades:
        out.setdefault(t.day, []).append([round(t.r, 4), round(t.pnl, 2)])
    return out


def _flat(days: dict, keep=lambda d: True) -> list[list[float]]:
    return [x for d in sorted(days) if keep(d) for x in days[d]]


def day_regimes(data: dict[str, list[Session]]) -> dict[str, dict]:
    """Per day: S&P 20-day trend sign and whether volatility (ATR / price) was above its median."""
    es = data.get("ES=F") or next(iter(data.values()), [])
    vol = {s.day.isoformat(): (s.atr / s.prev_close) for s in es if s.atr and s.prev_close}
    med = statistics.median(vol.values()) if vol else 0
    out = {}
    for s in es:
        d = s.day.isoformat()
        tr = s.trend.get(20)
        out[d] = {"trend": "up" if (tr or 0) > 0 else "down",
                  "vol": "high" if vol.get(d, 0) > med else "low"}
    return out


def jitter(g: dict, rng: random.Random) -> dict:
    """A near-copy: each relevant numeric gene moves one step up or down (half of them)."""
    x = dict(g)
    for k, spec in gm.SPACE.items():
        if spec.kind == "choice" or not gm.relevant(k, g["family"]) or rng.random() < 0.5:
            continue
        x[k] = gm._snap(spec, g[k] + rng.choice((-1, 1)) * spec.step)
    return x


def load(path: str | Path) -> dict:
    p = Path(path)
    if p.exists():
        return json.loads(p.read_text())
    return {"version": 1, "rules": RULES, "bots": {}, "regimes": {}, "nights": []}


def save(hof: dict, path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(hof, indent=1, default=str))
    p.with_suffix(".md").write_text(markdown(hof))
    return p


def nominees(runs: list) -> list[dict]:
    """From each breeding run: the champion, plus finalists that made money on validate AND holdout."""
    out = []
    for b, sel, _summ in runs:
        champ = b.bots[sel["champion"]]
        picks = [champ] if champ.get("passes") else []
        others = [b.bots[f] for f in sel["finalists"] if f != sel["champion"]]
        others = [x for x in others if x.get("val_passes") and (x.get("holdout") or {}).get("exp_r", 0) > 0]
        others.sort(key=lambda x: x.get("val_fitness", -9), reverse=True)
        picks += others[: NOMINATE_PER_RUN - len(picks)]
        for x in picks:
            out.append({"genes": x["genes"], "id": x["id"], "seed": b.opts.seed, "family": x["family"]})
    return out


def update(hof: dict, runs: list, data: dict[str, list[Session]], slip_ticks: float = 1.0,
           today: str | None = None, progress=lambda s: None) -> dict:
    """Add tonight's nominees, re-score every bot on all the data, promote and retire."""
    rules = {**RULES, **hof.get("rules", {})}
    hof["rules"] = rules
    days_all = sorted({s.day for ss in data.values() for s in ss})
    last_day = days_all[-1].isoformat() if days_all else today
    today = today or datetime.now(timezone.utc).date().isoformat()
    hof.setdefault("regimes", {}).update(day_regimes(data))
    bots = hof.setdefault("bots", {})
    new = 0
    for nom in nominees(runs):
        k = gm.key(nom["genes"])
        if k not in bots:
            bots[k] = {"key": k, "id": nom["id"], "family": nom["family"], "genes": nom["genes"],
                       "about": gm.describe(nom["genes"]), "first_nominated": last_day, "status": "candidate",
                       "nominations": [], "days": {}, "days_2tick": {}}
            new += 1
        bots[k]["nominations"].append({"night": today, "data_to": last_day, "seed": nom["seed"], "id": nom["id"]})
    data_days = {d.isoformat() for d in days_all}
    for k, b in bots.items():
        if b["status"] == "retired":
            continue
        g = b["genes"]
        tr = run(g, data, None, slip_ticks)
        # replace the days this data covers (a day with no trades is recorded as empty), keep older ones
        fresh = _by_day(tr)
        for d in data_days:
            b["days"][d] = fresh.get(d, [])
        fresh2 = _by_day(run(g, data, None, slip_ticks * 2))
        for d in data_days:
            b["days_2tick"][d] = fresh2.get(d, [])
        rng = random.Random(0)
        jit = []
        for s in range(rules["jitter_n"]):
            rng.seed(s + 1)
            jt = run(jitter(g, rng), data, None, slip_ticks)
            jit.append(round(sum(t.r for t in jt) / len(jt), 4) if jt else None)
        b["jitter_exp_r"] = jit
        b["checks"], b["stats"] = evaluate(b, hof["regimes"], rules)
        progress(f"hall of fame: {b['id']} {b['status']}: {b['stats']['record']['trades']} trades, "
                 f"forward {b['stats']['forward']['trades']}")
    for b in bots.values():
        if b["status"] == "retired":
            continue
        ok = all(c["ok"] for c in b["checks"].values())
        fwd = b["stats"]["forward"]
        if ok:
            if b["status"] != "promoted":
                b["promoted_on"] = today
            b["status"] = "promoted"
        elif fwd["trades"] >= rules["retire_after_forward"] and (
                b["status"] == "promoted" or fwd["exp_r"] <= 0):
            b["status"] = "retired"
            b["retired_on"] = today
            b["retired_why"] = ", ".join(n for n, c in b["checks"].items() if not c["ok"])
    # keep the hall a manageable size: drop the weakest candidates (never promoted or retired ones)
    cands = sorted((b for b in bots.values() if b["status"] == "candidate"), key=score, reverse=True)
    for b in cands[rules["max_candidates"]:]:
        del bots[b["key"]]
    hof["updated"] = today
    hof["data_to"] = last_day
    hof.setdefault("nights", []).append({"night": today, "data_to": last_day, "sessions": len(days_all),
                                         "nominated": new, "bots": len(bots),
                                         "promoted": sum(1 for b in bots.values() if b["status"] == "promoted")})
    hof["nights"] = hof["nights"][-200:]
    return hof


def evaluate(b: dict, regimes: dict, rules: dict) -> tuple[dict, dict]:
    days = b["days"]
    first = b["first_nominated"]
    rec = _flat(days)
    fwd = _flat(days, lambda d: d > first)
    two = _flat(b["days_2tick"])
    st = {"record": _stats(rec), "forward": _stats(fwd), "two_tick": _stats(two)}
    # weeks
    weeks: dict[str, float] = {}
    for d, xs in days.items():
        if xs:
            y, w, _ = date.fromisoformat(d).isocalendar()
            weeks[f"{y}-W{w:02d}"] = weeks.get(f"{y}-W{w:02d}", 0.0) + sum(r for r, _ in xs)
    good = sum(1 for v in weeks.values() if v >= 0)
    st["weeks"] = {"weeks": len(weeks), "not_losing": good}
    # regimes
    reg = {}
    for dim, labels in (("trend", ("up", "down")), ("vol", ("high", "low"))):
        for lab in labels:
            xs = _flat(days, lambda d, dim=dim, lab=lab: (regimes.get(d) or {}).get(dim) == lab)
            reg[f"{dim}_{lab}"] = _stats(xs)
    st["regimes"] = reg
    jit = [x for x in b.get("jitter_exp_r", []) if x is not None]
    st["seeds"] = {"tested": len(b.get("jitter_exp_r", [])), "positive": sum(1 for x in jit if x > 0)}
    f, r_ = st["forward"], st["record"]
    reg_ok = [v["exp_r"] > 0 for v in reg.values() if v["trades"] >= rules["regime_min_trades"]]
    checks = {
        "forward": {"ok": f["trades"] >= rules["forward_min_trades"] and f["exp_r"] > rules["forward_min_exp_r"]
                    and f["pf"] > rules["forward_min_pf"],
                    "why": f"{f['trades']} trades after {first} (need {rules['forward_min_trades']}), "
                           f"{f['exp_r']:+.3f}R, PF {f['pf']:.2f}"},
        "record": {"ok": r_["trades"] >= rules["record_min_trades"] and r_["exp_r"] >= rules["record_min_exp_r"],
                   "why": f"{r_['trades']} trades, {r_['exp_r']:+.3f}R a trade"},
        "costs": {"ok": st["two_tick"]["trades"] > 0 and st["two_tick"]["exp_r"] > 0,
                  "why": f"{st['two_tick']['exp_r']:+.3f}R with 2-tick slippage"},
        "weeks": {"ok": len(weeks) >= rules["min_weeks"] and good >= rules["weeks_ok_frac"] * len(weeks),
                  "why": f"{good} of {len(weeks)} weeks not losing"},
        "regimes": {"ok": len(reg_ok) >= 2 and all(reg_ok),
                    "why": ", ".join(f"{k} {v['exp_r']:+.2f}R/{v['trades']}" for k, v in reg.items() if v["trades"])},
        "seeds": {"ok": st["seeds"]["positive"] >= rules["jitter_ok"],
                  "why": f"{st['seeds']['positive']} of {st['seeds']['tested']} near-copies positive"},
    }
    return checks, st


def score(b: dict) -> tuple:
    """Ranking: promoted first, then forward evidence, then the whole record (lower confidence bound)."""
    st = b.get("stats") or {}
    rec, fwd = st.get("record") or {}, st.get("forward") or {}
    n_ok = sum(1 for c in (b.get("checks") or {}).values() if c["ok"])
    return ({"promoted": 2, "candidate": 1}.get(b["status"], 0), n_ok,
            round(wilson_low(fwd.get("wins", 0), fwd.get("trades", 0)) * min(1, fwd.get("trades", 0) / 20), 4),
            round(wilson_low(rec.get("wins", 0), rec.get("trades", 0)) + 0.3 * math.tanh(2 * rec.get("exp_r", 0)), 4))


def ranked(hof: dict, include_retired: bool = False) -> list[dict]:
    bs = [b for b in hof.get("bots", {}).values() if include_retired or b["status"] != "retired"]
    return sorted(bs, key=score, reverse=True)


def markdown(hof: dict) -> str:
    lines = [f"# Hall of fame ({hof.get('updated', '-')}, data to {hof.get('data_to', '-')})", ""]
    bots = ranked(hof)
    prom = [b for b in bots if b["status"] == "promoted"]
    lines.append(f"**{len(prom)} promoted**, {len(bots) - len(prom)} candidates, "
                 f"{sum(1 for b in hof.get('bots', {}).values() if b['status'] == 'retired')} retired.")
    lines.append("")
    lines.append("Promotion needs every check: forward (days after nomination), record, costs (2-tick slippage), "
                 "weeks, regimes and seeds (near-copies). See tradebot/breeder/halloffame.py.")
    lines.append("")
    lines.append("| # | bot | status | record | forward | win rate (95%) | checks passed | about |")
    lines.append("|---|-----|--------|--------|---------|----------------|---------------|-------|")
    for i, b in enumerate(bots[:25], 1):
        st = b.get("stats") or {}
        r, f = st.get("record", {}), st.get("forward", {})
        lo, hi = ci95(r.get("wins", 0), r.get("trades", 0))
        ok = [n for n, c in (b.get("checks") or {}).items() if c["ok"]]
        lines.append(f"| {i} | {b['id']} | {b['status']} | {r.get('trades', 0)} tr, {r.get('exp_r', 0):+.3f}R | "
                     f"{f.get('trades', 0)} tr, {f.get('exp_r', 0):+.3f}R | {r.get('win_rate', 0) * 100:.0f}% "
                     f"({lo * 100:.0f}–{hi * 100:.0f}%) | {len(ok)}/6 {' '.join(ok)} | {b['about']} |")
    lines.append("")
    return "\n".join(lines)
