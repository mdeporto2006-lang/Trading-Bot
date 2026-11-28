"""A planner that uses only the code's own rules. No model calls, no cost.

It plays the role Claude plays live: pick a direction from the trend (price vs
VWAP), put the entry one cent through the last price, the stop just beyond the
nearest clean level, and the target at the next level or 2R, whichever is
nearer. It returns NO_TRADE unless code's own checklist passes. The risk gate
then checks the plan exactly as it does live.

This is deliberately simple: it measures the checklist and exit rules, not
Claude's judgment. Use `--planner claude` to test Claude itself.
"""

from __future__ import annotations

from .. import checklist as ck
from ..config import Config
from ..models import MarketSnapshot, TradePlan
from ..planner import no_trade

PREFILTER_ITEMS = ("catalyst", "relative_volume", "liquidity", "trend")


def direction_for(snap: MarketSnapshot) -> ck.Direction | None:
    if snap.last_price is None or snap.vwap is None or snap.last_price == snap.vwap:
        return None
    return "long" if snap.last_price > snap.vwap else "short"


def prefilter(snap: MarketSnapshot, cfg: Config) -> tuple[bool, ck.Direction | None, list]:
    """Checks that don't depend on entry/stop/target. If these fail, the risk
    gate will reject any plan, so there's no point asking Claude."""
    if snap.missing:
        return False, None, []
    d = direction_for(snap)
    if d is None:
        return False, None, []
    items = ck.evaluate(snap, d, None, None, None, cfg)
    ok = all(i.result == "PASS" for i in items if i.item in PREFILTER_ITEMS)
    return ok, d, items


class RulesPlanner:
    confidence = 0.7

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def plan(self, snap: MarketSnapshot, similar=None) -> TradePlan:
        ok, d, items = prefilter(snap, self.cfg)
        if not ok or d is None:
            failed = [i.item for i in items if i.result == "FAIL" and i.item in PREFILTER_ITEMS]
            return no_trade(snap.ticker, "rules: " + (", ".join(failed) or "missing data or no trend"))
        lp, atr = snap.last_price, snap.atr
        entry = round(lp + 0.01, 2) if d == "long" else round(lp - 0.01, 2)
        level = ck.stop_level(snap, d, entry)
        if level is None:
            return no_trade(snap.ticker, "rules: no level for the stop")
        buf = max(0.01, 0.05 * atr)
        stop = round(level - buf, 2) if d == "long" else round(level + buf, 2)
        risk = abs(entry - stop)
        if risk <= 0:
            return no_trade(snap.ticker, "rules: bad stop")
        raw_target = entry + self.cfg.exits.target_r_multiple * risk * (1 if d == "long" else -1)
        target = round(ck.effective_target(snap, d, entry, raw_target), 2)
        items = ck.evaluate(snap, d, entry, stop, target, self.cfg)
        if any(i.result == "FAIL" for i in items):
            failed = [i.item for i in items if i.result == "FAIL"]
            plan = no_trade(snap.ticker, "rules: " + ", ".join(failed))
            plan.checklist = items
            return plan
        return TradePlan(
            ticker=snap.ticker, mode="DAY", action="BUY" if d == "long" else "SHORT",
            checklist=items, entry_trigger=entry, stop=stop, target=target,
            rr_ratio=ck.reward_risk(d, entry, stop, target),
            invalidation=f"price back through {level:.2f}", confidence=self.confidence,
        )
