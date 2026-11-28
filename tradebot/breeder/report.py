"""Turn breeder runs into one JSON file, a plain-English verdict, a markdown
summary, and a self-contained HTML page with the family tree."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from . import genome as gm
from .contracts import CONTRACTS
from .evolve import Breeder, ci95

STATIC = Path(__file__).parent / "static"


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{x * 100:.0f}%"


def _line(name: str, m: dict | None) -> str:
    if not m or not m.get("trades"):
        return f"| {name} | 0 | - | - | - | - |"
    lo, hi = ci95(m["wins"], m["trades"])
    return (f"| {name} | {m['trades']} | {_pct(m['win_rate'])} ({_pct(lo)}–{_pct(hi)}) | {m['exp_r']:+.3f}R | "
            f"{m['pf']:.2f} | {m['total_r']:+.1f}R (${m['total_usd']:+,.0f}) |")


def verdict(champ: dict, target: float, min_hold: int, null: dict | None) -> dict:
    h = champ.get("holdout") or {}
    tr, va = champ["train"], champ.get("validate") or {}
    n = h.get("trades", 0)
    lo, hi = ci95(h.get("wins", 0), n)
    money = n > 0 and h["exp_r"] > 0 and h["pf"] > 1
    reached = n >= min_hold and h.get("win_rate", 0) >= target and money
    lines = []
    lines.append(f"In-sample (TRAIN, the days it was bred on): {_pct(tr['win_rate'])} win rate over "
                 f"{tr['trades']} trades, {tr['exp_r']:+.3f}R per trade.")
    if va:
        lines.append(f"VALIDATE (used to pick the champion): {_pct(va['win_rate'])} over {va['trades']} trades, "
                     f"{va['exp_r']:+.3f}R per trade.")
    lines.append(f"HOLDOUT (never seen, the honest number): {_pct(h.get('win_rate'))} over {n} trades, "
                 f"{h.get('exp_r', 0):+.3f}R per trade, profit factor {h.get('pf', 0):.2f}. "
                 f"With only {n} trades the true win rate could be anywhere from {_pct(lo)} to {_pct(hi)} (95% range).")
    if reached and lo >= target:
        head = f"Reached {_pct(target)}+ out of sample, and the result is statistically solid."
    elif reached:
        head = (f"Hit {_pct(target)}+ on the holdout with positive expectancy, but on too few trades to call it "
                f"real: the 95% range dips to {_pct(lo)}. Treat it as a candidate for paper trading, not a proven bot.")
    elif money:
        head = (f"Did not reach {_pct(target)} out of sample. The champion made money on the holdout "
                f"({h['exp_r']:+.3f}R per trade) at a {_pct(h['win_rate'])} win rate.")
    else:
        head = (f"Did not reach {_pct(target)} out of sample, and the champion lost money on the holdout. "
                "Its in-sample edge did not survive new days.")
    if null:
        nc = null.get("champion") or {}
        nt, nh = nc.get("train") or {}, nc.get("holdout") or {}
        lines.append(f"Luck test: the same breeder run on scrambled prices (all real patterns destroyed) still "
                     f"bred a {_pct(nt.get('win_rate'))} in-sample bot ({nt.get('exp_r', 0):+.3f}R over "
                     f"{nt.get('trades', 0)} trades), which then "
                     + (f"did {_pct(nh['win_rate'])} ({nh['exp_r']:+.3f}R) over {nh['trades']} trades on its holdout. "
                        if nh.get("trades") else "made no trades on its holdout. ")
                     + "That is what luck alone can do in-sample, so only the holdout number counts.")
    return {"headline": head, "reached": reached, "solid": reached and lo >= target, "lines": lines,
            "holdout_ci95": [lo, hi]}


def _strip(b: dict, keep_genes: bool) -> dict:
    out = {k: b[k] for k in ("id", "gen", "parents", "how", "family", "fitness", "passes",
                             "alive_until", "children") if k in b}
    t = b["train"]
    out["t"] = {"n": t["trades"], "wr": t["win_rate"], "er": t["exp_r"], "pf": t["pf"]}
    for k in ("finalist", "champion", "on_champion_line", "val_fitness", "val_passes", "validate", "holdout"):
        if k in b:
            out[k] = b[k]
    if keep_genes:
        out.update(genes=b["genes"], about=b["about"], mutations=b["mutations"], train=b["train"],
                   elite_gens=b["elite_gens"])
    else:
        out["mutations"] = b["mutations"][:3]
    return out


def build(main: Breeder, sel: dict, seeds: list[dict], null: dict | None, meta: dict) -> dict:
    champ = main.bots[sel["champion"]]
    notable = {bid for bid, b in main.bots.items()
               if b["children"] or b["elite_gens"] or b.get("finalist") or b.get("on_champion_line")}
    bots = [_strip(b, bid in notable) for bid, b in main.bots.items()]
    gens = [{k: v for k, v in g.items() if k != "population"} for g in main.generations]
    v = verdict(champ, main.opts.target_win_rate, main.min_hold, null)
    textbook = [{"id": b["id"], "family": b["family"], "about": b["about"], "train": b["train"],
                 "validate": b.get("validate"), "holdout": b.get("holdout")}
                for b in main.bots.values() if b["how"] == "textbook"]
    return {
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "meta": meta | {"days": main.days(), "min_trades": {"train": main.opts.min_trades,
                                                             "validate": main.min_val, "holdout": main.min_hold},
                        "options": main.opts.__dict__,
                        "contracts": {s: c.__dict__ for s, c in CONTRACTS.items() if s in main.universe},
                        "families": list(gm.FAMILIES)},
        "verdict": v,
        "champion": _strip(champ, True) | {"trades": sel["champion_trades"]},
        "lineage": sel["lineage"],
        "finalists": [_strip(main.bots[b], True) for b in sel["finalists"]],
        "textbook": textbook,
        "generations": gens,
        "bots": bots,
        "seeds": seeds,
        "null": null,
    }


def markdown(rep: dict) -> str:
    c, v, m = rep["champion"], rep["verdict"], rep["meta"]
    d = m["days"]
    out = [f"## Bot breeder: {v['headline']}", ""]
    out += [f"**Champion {c['id']}** ({c['family']}): {c['about']}", ""]
    out += ["| Split | Trades | Win rate (95% range) | Per trade | Profit factor | Total |",
            "|---|---|---|---|---|---|",
            _line(f"Train {d['train'][0]}→{d['train'][-1]} ({len(d['train'])} days)", c["train"]),
            _line(f"Validate {d['validate'][0]}→{d['validate'][-1]} ({len(d['validate'])} days)", c.get("validate")),
            _line(f"**Holdout** {d['holdout'][0]}→{d['holdout'][-1]} ({len(d['holdout'])} days)", c.get("holdout")),
            ""]
    out += [f"- {x}" for x in v["lines"]]
    out += ["", "### Textbook versions (generation 0, no breeding)", "",
            "| Family | Train | Validate | Holdout |", "|---|---|---|---|"]
    for t in rep["textbook"]:
        f = lambda x: "-" if not x or not x["trades"] else f"{_pct(x['win_rate'])}, {x['exp_r']:+.2f}R, n={x['trades']}"
        out.append(f"| {t['family']} | {f(t['train'])} | {f(t.get('validate'))} | {f(t.get('holdout'))} |")
    if rep.get("seeds") and len(rep["seeds"]) > 1:
        out += ["", "### Every breeding run (different random seeds)", "",
                "| Seed | Champion | Train | Validate | Holdout |", "|---|---|---|---|---|"]
        for s in rep["seeds"]:
            f = lambda x: "-" if not x or not x["trades"] else f"{_pct(x['win_rate'])}, {x['exp_r']:+.2f}R, n={x['trades']}"
            out.append(f"| {s['seed']} | {s['family']} | {f(s['train'])} | {f(s['validate'])} | {f(s['holdout'])} |")
    out += ["", f"Data: {m['source']} {m['interval']}-minute bars, {', '.join(m['symbols'])}. "
            f"{len(rep['bots'])} bots bred over {len(rep['generations']) - 1} generations. "
            "Costs: 1 tick slippage on stops and market fills, round-trip commission per contract. "
            "A win is a trade with P&L above zero after costs."]
    out += [f"- {w}" for w in m.get("warnings", [])]
    return "\n".join(out) + "\n"


def html(rep: dict) -> str:
    page = (STATIC / "breeder.html").read_text()
    data = json.dumps(rep, separators=(",", ":"), default=str).replace("</", "<\\/")
    return page.replace("/*BREEDER_DATA*/null", data, 1)


def save(rep: dict, out_dir: str | Path) -> dict[str, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    paths = {"json": out / "latest.json", "md": out / "summary.md", "html": out / "tree.html"}
    paths["json"].write_text(json.dumps(rep, separators=(",", ":"), default=str))
    paths["md"].write_text(markdown(rep))
    paths["html"].write_text(html(rep))
    return paths
