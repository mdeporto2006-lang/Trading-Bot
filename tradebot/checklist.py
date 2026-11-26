"""Deterministic setup checklist.

The planner fills in its own checklist, but code never trusts it: this module
recomputes every item from the snapshot. A trade only goes through if every
item here passes. Missing data is always a FAIL.
"""

from __future__ import annotations

from typing import Literal, Optional

from .config import Config
from .models import ChecklistItem, MarketSnapshot

Direction = Literal["long", "short"]


def _item(name: str, ok: bool, value: str) -> ChecklistItem:
    return ChecklistItem(item=name, value=value, result="PASS" if ok else "FAIL")


def _levels(snap: MarketSnapshot) -> list[float]:
    # Levels are split around the last price in the snapshot, but an entry
    # trigger can sit on either side of it (a breakout entry turns the level
    # just above price into the stop level), so use them all.
    return [*snap.supports, *snap.resistances]


def effective_target(
    snap: MarketSnapshot, direction: Direction, entry: float, target: float
) -> float:
    """Cap the target at the next opposing level: you can't count on reward
    beyond the nearest resistance (long) or support (short)."""
    levels = _levels(snap)
    if direction == "long":
        above = [x for x in levels if x > entry]
        return min(target, min(above)) if above else target
    below = [x for x in levels if x < entry]
    return max(target, max(below)) if below else target


def reward_risk(direction: Direction, entry: float, stop: float, target: float) -> Optional[float]:
    risk = entry - stop if direction == "long" else stop - entry
    reward = target - entry if direction == "long" else entry - target
    if risk <= 0 or reward <= 0:
        return None
    return reward / risk


def stop_level(snap: MarketSnapshot, direction: Direction, entry: float) -> Optional[float]:
    """Nearest level below entry (long) or above it (short)."""
    levels = _levels(snap)
    if direction == "long":
        below = [x for x in levels if x < entry]
        return max(below) if below else None
    above = [x for x in levels if x > entry]
    return min(above) if above else None


def evaluate(
    snap: MarketSnapshot,
    direction: Direction,
    entry: Optional[float],
    stop: Optional[float],
    target: Optional[float],
    cfg: Config,
) -> list[ChecklistItem]:
    s, ex = cfg.setup, cfg.exits
    items: list[ChecklistItem] = []

    # 1. Catalyst
    fresh = [n for n in snap.news if n.age_hours < s.catalyst_max_age_hours]
    if fresh:
        items.append(_item("catalyst", True, f"{fresh[0].age_hours:.1f}h old: {fresh[0].headline[:80]}"))
    else:
        items.append(_item("catalyst", False, f"no news < {s.catalyst_max_age_hours:g}h"))

    # 2. Relative volume
    rv = snap.relative_volume
    items.append(
        _item(
            "relative_volume",
            rv is not None and rv > s.min_relative_volume,
            "missing" if rv is None else f"{rv:.2f}x (need > {s.min_relative_volume:g}x)",
        )
    )

    # 3. Liquidity
    av, sp = snap.avg_daily_volume, snap.spread_pct
    ok = av is not None and sp is not None and av > s.min_avg_daily_volume and sp < s.max_spread_pct
    items.append(
        _item(
            "liquidity",
            ok,
            f"avg vol {'missing' if av is None else f'{av:,.0f}'}, "
            f"spread {'missing' if sp is None else f'{sp:.3f}%'} "
            f"(need > {s.min_avg_daily_volume:,.0f} and < {s.max_spread_pct:g}%)",
        )
    )

    # 4. Trend vs VWAP
    lp, vw = snap.last_price, snap.vwap
    if lp is None or vw is None:
        items.append(_item("trend", False, "price or VWAP missing"))
    elif direction == "long":
        items.append(_item("trend", lp > vw, f"price {lp:.2f} vs VWAP {vw:.2f} (need above)"))
    else:
        items.append(_item("trend", lp < vw, f"price {lp:.2f} vs VWAP {vw:.2f} (need below)"))

    # 5. Clean level for the stop
    atr = snap.atr
    if entry is None or stop is None or atr is None:
        items.append(_item("clean_level", False, "entry, stop or ATR missing"))
    else:
        level = stop_level(snap, direction, entry)
        dist_ok = level is not None and abs(entry - level) <= s.max_level_distance_atr * atr
        if direction == "long":
            beyond = level is not None and stop <= level
            sane = 0 < entry - stop <= max(ex.stop_atr_multiple * atr, (entry - (level or entry)) + 0.25 * atr)
        else:
            beyond = level is not None and stop >= level
            sane = 0 < stop - entry <= max(ex.stop_atr_multiple * atr, ((level or entry) - entry) + 0.25 * atr)
        items.append(
            _item(
                "clean_level",
                bool(dist_ok and beyond and sane),
                f"level {'none' if level is None else f'{level:.2f}'}, stop {stop:.2f}, "
                f"entry {entry:.2f}, ATR {atr:.2f}",
            )
        )

    # 6. Reward:risk using the stop distance, target capped at next level
    if entry is None or stop is None or target is None:
        items.append(_item("reward_risk", False, "entry, stop or target missing"))
    else:
        eff = effective_target(snap, direction, entry, target)
        rr = reward_risk(direction, entry, stop, eff)
        items.append(
            _item(
                "reward_risk",
                rr is not None and rr >= s.min_reward_risk,
                f"{'invalid' if rr is None else f'{rr:.2f}'}:1 to {eff:.2f} (need >= {s.min_reward_risk:g}:1)",
            )
        )

    # 7. (SWING only: no earnings before exit) does not apply in DAY mode.
    return items
