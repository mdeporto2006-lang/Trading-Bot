"""Turn a finished backtest into numbers, files and a printable summary."""

from __future__ import annotations

import csv
import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .engine import BacktestEngine


def metrics(engine: BacktestEngine, start_equity: float) -> dict:
    tr = engine.trades
    n = len(tr)
    rs = [t.r_multiple for t in tr]
    pnl = [t.pnl for t in tr]
    wins = [t for t in tr if t.pnl > 0]
    losses = [t for t in tr if t.pnl <= 0]
    gross_win = sum(t.pnl for t in wins)
    gross_loss = -sum(t.pnl for t in losses)

    # Drawdown on the trade-by-trade equity path (in $ and in R).
    peak = eq = start_equity
    max_dd = max_dd_pct = 0.0
    for t in sorted(tr, key=lambda x: x.exit_time):
        eq += t.pnl
        peak = max(peak, eq)
        max_dd = max(max_dd, peak - eq)
        max_dd_pct = max(max_dd_pct, (peak - eq) / peak * 100 if peak else 0)
    peak_r = cum_r = dd_r = 0.0
    for r in rs:
        cum_r += r
        peak_r = max(peak_r, cum_r)
        dd_r = max(dd_r, peak_r - cum_r)

    days = len(engine.days) or 1
    end_equity = start_equity + sum(pnl)
    by_reason: dict[str, int] = {}
    for t in tr:
        by_reason[t.exit_reason] = by_reason.get(t.exit_reason, 0) + 1
    by_ticker: dict[str, dict] = {}
    for t in tr:
        b = by_ticker.setdefault(t.ticker, {"trades": 0, "wins": 0, "total_r": 0.0, "pnl": 0.0})
        b["trades"] += 1
        b["wins"] += t.pnl > 0
        b["total_r"] = round(b["total_r"] + t.r_multiple, 3)
        b["pnl"] = round(b["pnl"] + t.pnl, 2)
    hist = {}
    for r in rs:
        k = f"{max(-3, min(5, round(r * 2) / 2)):+.1f}"
        hist[k] = hist.get(k, 0) + 1

    return {
        "trades": n,
        "trading_days": len(engine.days),
        "trades_per_day": round(n / days, 2),
        "win_rate_pct": round(len(wins) / n * 100, 1) if n else 0.0,
        "avg_r": round(sum(rs) / n, 3) if n else 0.0,
        "total_r": round(sum(rs), 3),
        "best_r": round(max(rs), 3) if rs else 0.0,
        "worst_r": round(min(rs), 3) if rs else 0.0,
        "avg_win_r": round(sum(t.r_multiple for t in wins) / len(wins), 3) if wins else 0.0,
        "avg_loss_r": round(sum(t.r_multiple for t in losses) / len(losses), 3) if losses else 0.0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss else None,
        "net_pnl": round(sum(pnl), 2),
        "return_pct": round((end_equity - start_equity) / start_equity * 100, 3),
        "start_equity": start_equity,
        "end_equity": round(end_equity, 2),
        "max_drawdown": round(max_dd, 2),
        "max_drawdown_pct": round(max_dd_pct, 3),
        "max_drawdown_r": round(dd_r, 3),
        "exit_reasons": by_reason,
        "by_ticker": by_ticker,
        "r_histogram": dict(sorted(hist.items(), key=lambda kv: float(kv[0]))),
    }


def save(engine: BacktestEngine, meta: dict, out_dir: str | Path = "backtests") -> tuple[Path, dict]:
    out = Path(out_dir)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    run = out / run_id
    run.mkdir(parents=True, exist_ok=True)
    m = metrics(engine, meta["start_equity"])
    result = {
        "id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        **meta,
        "metrics": m,
        "decisions": asdict(engine.decisions),
        "equity_curve": engine.equity_curve,
        "trades": [asdict(t) for t in engine.trades],
        "priced_plans": [{k: v for k, v in p.items() if k != "snapshot"} for p in engine.priced_plans[-500:]],
    }
    (run / "results.json").write_text(json.dumps(result, indent=2, default=str))
    with (run / "trades.csv").open("w", newline="") as f:
        if engine.trades:
            w = csv.DictWriter(f, fieldnames=list(asdict(engine.trades[0])))
            w.writeheader()
            for t in engine.trades:
                w.writerow(asdict(t))
    (out / "latest.json").write_text(json.dumps(result, indent=2, default=str))
    return run, result


def text_summary(result: dict) -> str:
    m, d = result["metrics"], result["decisions"]
    lines = [
        f"Backtest {result['id']}  {result['start']} to {result['end']}  {', '.join(result['tickers'])}",
        f"  data: {result['source']}   planner: {result['planner']}   catalyst: {result['catalyst']}",
    ]
    for w in result.get("warnings", []):
        lines.append(f"  ! {w}")
    pf = m["profit_factor"]
    lines += [
        f"  trades {m['trades']} over {m['trading_days']} days ({m['trades_per_day']}/day)",
        f"  win rate {m['win_rate_pct']}%   avg {m['avg_r']:+.2f}R   total {m['total_r']:+.2f}R   "
        f"best {m['best_r']:+.2f}R   worst {m['worst_r']:+.2f}R",
        f"  P&L ${m['net_pnl']:,.2f} ({m['return_pct']:+.2f}%)   profit factor {pf if pf is not None else 'n/a'}",
        f"  max drawdown ${m['max_drawdown']:,.2f} ({m['max_drawdown_pct']:.2f}%, {m['max_drawdown_r']:.2f}R)",
        f"  exits: {m['exit_reasons']}",
        f"  scans {d['scans']}, passed the first four checks {d['prefilter_pass']}, priced {d['priced']}, "
        f"approved {d['approved']}" + (f", Claude calls {d['planner_calls']}" if d['planner_calls'] else ""),
    ]
    if d["reject_reasons"]:
        top = sorted(d["reject_reasons"].items(), key=lambda kv: -kv[1])[:5]
        lines.append("  top gate rejections: " + "; ".join(f"{k} x{v}" for k, v in top))
    return "\n".join(lines)
