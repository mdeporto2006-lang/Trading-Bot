"""Test a candidate config on past plans.

For every scored plan, re-run the deterministic gate with the candidate's
thresholds and, if it would have traded, re-simulate the exit with the
candidate's trailing settings. Account limits and entry-time rules are not
replayed. Only plans Claude priced (BUY/SHORT) can be replayed, so this can
judge tightening a rule much better than loosening one.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from ..checklist import evaluate
from ..config import Config
from ..indicators import Bar
from ..models import MarketSnapshot
from ..simulate import simulate_bracket


@dataclass
class ReplayResult:
    plans: int  # scored plans available
    trades: int  # would have been taken and triggered
    wins: int
    total_r: float
    expectancy_r: float  # average R per trade
    max_drawdown_r: float

    def as_dict(self) -> dict:
        return asdict(self)


def replay(cfg: Config, records: list[dict], outcomes: dict[str, dict]) -> ReplayResult:
    rs: list[float] = []
    plans = 0
    for rec in sorted(records, key=lambda r: r.get("ts", "")):
        out = outcomes.get(rec.get("id", ""))
        if not out:
            continue
        plans += 1
        plan = rec["plan"]
        if plan.get("confidence", 0) < cfg.risk.min_confidence:
            continue
        snap = MarketSnapshot.model_validate(rec["snapshot"])
        if snap.missing:
            continue
        items = evaluate(snap, out["direction"], plan["entry_trigger"], plan["stop"], plan["target"], cfg)
        if any(i.result == "FAIL" for i in items):
            continue
        bars = [Bar(o, h, l, c, 0) for o, h, l, c in out["bars"]]
        sim = simulate_bracket(
            out["direction"], out["entry"], out["stop"], out["target"], bars,
            cfg.exits.trail_after_r, cfg.exits.trail_distance_r,
        )
        if sim.triggered:
            rs.append(sim.r_multiple)

    peak = equity = dd = 0.0
    for r in rs:
        equity += r
        peak = max(peak, equity)
        dd = max(dd, peak - equity)
    n = len(rs)
    return ReplayResult(
        plans=plans,
        trades=n,
        wins=sum(1 for r in rs if r > 0),
        total_r=round(sum(rs), 3),
        expectancy_r=round(sum(rs) / n, 4) if n else 0.0,
        max_drawdown_r=round(dd, 3),
    )


def passes(baseline: ReplayResult, candidate: ReplayResult, cfg: Config) -> tuple[bool, str]:
    imp = cfg.improvement
    if candidate.trades < imp.min_trades:
        return False, f"only {candidate.trades} replayed trades (need {imp.min_trades})"
    gain = candidate.expectancy_r - baseline.expectancy_r
    if gain < imp.min_expectancy_gain_r:
        return False, f"expectancy {candidate.expectancy_r:+.3f}R vs baseline {baseline.expectancy_r:+.3f}R (need +{imp.min_expectancy_gain_r}R)"
    if candidate.max_drawdown_r > baseline.max_drawdown_r + imp.max_drawdown_worsen_r:
        return False, f"drawdown {candidate.max_drawdown_r}R vs baseline {baseline.max_drawdown_r}R"
    if candidate.total_r <= 0:
        return False, f"total {candidate.total_r}R is not positive"
    return True, f"expectancy {candidate.expectancy_r:+.3f}R vs {baseline.expectancy_r:+.3f}R over {candidate.trades} trades"
