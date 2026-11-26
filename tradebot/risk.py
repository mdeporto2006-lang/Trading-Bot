"""Deterministic risk gate. The last word on every plan.

A plan is executed only if all of these hold:
- the planner said BUY or SHORT with enough confidence,
- no data is missing,
- code's own checklist (checklist.py) passes every item,
- prices make sense (stop and target on the right sides, entry near price),
- we are inside the entry window and within account limits,
- position size works out to at least one share.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

from . import checklist as ck
from .clock import in_entry_window
from .config import Config
from .models import MarketSnapshot, RiskDecision, TradePlan


@dataclass
class AccountState:
    equity: float
    buying_power: float
    start_of_day_equity: float
    open_symbols: set[str] = field(default_factory=set)
    trades_today: int = 0
    shortable: dict[str, bool] = field(default_factory=dict)  # ticker -> easy to borrow

    @property
    def day_pnl_pct(self) -> float:
        if self.start_of_day_equity <= 0:
            return 0.0
        return (self.equity - self.start_of_day_equity) / self.start_of_day_equity * 100


def position_size(cfg: Config, acct: AccountState, entry: float, stop: float) -> int:
    per_share_risk = abs(entry - stop)
    if per_share_risk <= 0 or entry <= 0:
        return 0
    by_risk = acct.equity * cfg.risk.risk_per_trade_pct / 100 / per_share_risk
    by_size = acct.equity * cfg.risk.max_position_pct / 100 / entry
    by_bp = acct.buying_power / entry
    return max(0, math.floor(min(by_risk, by_size, by_bp)))


def evaluate_plan(
    plan: TradePlan,
    snap: MarketSnapshot,
    cfg: Config,
    acct: AccountState,
    now: datetime,
) -> RiskDecision:
    d = RiskDecision(ticker=snap.ticker, approved=False, action="NO_TRADE")
    reasons = d.reasons

    if plan.action == "NO_TRADE":
        reasons.append("planner chose NO_TRADE")
        return d
    if plan.mode != "DAY":
        reasons.append(f"plan mode {plan.mode} is not DAY")
        return d
    if snap.missing:
        reasons.append("missing data: " + ", ".join(snap.missing))
        return d

    direction: ck.Direction = "long" if plan.action == "BUY" else "short"
    entry, stop, target = plan.entry_trigger, plan.stop, plan.target
    if entry is None or stop is None or target is None:
        reasons.append("plan is missing entry, stop or target")
        return d

    # Price geometry
    if direction == "long" and not (stop < entry < target):
        reasons.append("BUY needs stop < entry < target")
    if direction == "short" and not (target < entry < stop):
        reasons.append("SHORT needs target < entry < stop")
    if snap.last_price and snap.atr and abs(entry - snap.last_price) > 0.5 * snap.atr:
        reasons.append(
            f"entry {entry:.2f} is more than 0.5 ATR from price {snap.last_price:.2f}"
        )

    # Code's own checklist
    eff_target = ck.effective_target(snap, direction, entry, target)
    d.checklist = ck.evaluate(snap, direction, entry, stop, target, cfg)
    failed = [c.item for c in d.checklist if c.result == "FAIL"]
    if failed:
        reasons.append("checklist failed: " + ", ".join(failed))

    if plan.confidence < cfg.risk.min_confidence:
        reasons.append(f"confidence {plan.confidence:.2f} < {cfg.risk.min_confidence:.2f}")

    # Session and account limits
    if not in_entry_window(now, cfg.session.no_entries_before_et, cfg.session.no_entries_after_et):
        reasons.append("outside entry window")
    if acct.day_pnl_pct <= -cfg.risk.daily_loss_limit_pct:
        reasons.append(f"daily loss limit hit ({acct.day_pnl_pct:.2f}%)")
    if snap.ticker in acct.open_symbols:
        reasons.append("already have a position or order in this ticker")
    if len(acct.open_symbols) >= cfg.risk.max_open_positions:
        reasons.append("max open positions reached")
    if acct.trades_today >= cfg.risk.max_trades_per_day:
        reasons.append("max trades per day reached")
    if direction == "short" and not acct.shortable.get(snap.ticker, False):
        reasons.append("not shortable / easy to borrow")

    qty = position_size(cfg, acct, entry, stop)
    if qty < 1:
        reasons.append("position size rounds to 0 shares")

    if reasons:
        return d

    d.approved = True
    d.action = plan.action
    d.entry = round(entry, 2)
    d.stop = round(stop, 2)
    d.target = round(eff_target, 2)
    d.rr_ratio = ck.reward_risk(direction, entry, stop, eff_target)
    d.qty = qty
    return d
