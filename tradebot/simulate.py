"""Replay a bracket order against minute bars to see how it would have ended.

Used to score every priced plan (taken or not), so the bot can learn which
settings actually make money. Conservative by design: if a bar touches both the
stop and the target, the stop is assumed to have hit first.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional, Sequence

from .indicators import Bar

ExitReason = Literal["not_triggered", "stop", "target", "flatten"]


@dataclass(frozen=True)
class SimResult:
    triggered: bool
    exit_reason: ExitReason
    r_multiple: float  # 0.0 when not triggered
    exit_price: Optional[float] = None


def simulate_bracket(
    direction: Literal["long", "short"],
    entry: float,
    stop: float,
    target: float,
    bars: Sequence[Bar],
    trail_after_r: float,
    trail_distance_r: float,
) -> SimResult:
    """bars: minute bars from the decision time to the flatten time."""
    r = abs(entry - stop)
    if r <= 0 or not bars:
        return SimResult(False, "not_triggered", 0.0)
    sign = 1 if direction == "long" else -1

    filled = False
    cur_stop = stop
    for b in bars:
        if not filled:
            hit = b.high >= entry if direction == "long" else b.low <= entry
            if not hit:
                continue
            filled = True  # fill at the trigger; check exits on this same bar
        stop_hit = b.low <= cur_stop if direction == "long" else b.high >= cur_stop
        target_hit = b.high >= target if direction == "long" else b.low <= target
        if stop_hit:
            return SimResult(True, "stop", round(sign * (cur_stop - entry) / r, 3), cur_stop)
        if target_hit:
            return SimResult(True, "target", round(sign * (target - entry) / r, 3), target)
        # Trail on the bar's close; only ever tighten.
        moved = sign * (b.close - entry)
        if moved >= trail_after_r * r:
            new = b.close - sign * trail_distance_r * r
            if (direction == "long" and new > cur_stop) or (direction == "short" and new < cur_stop):
                cur_stop = new
    if not filled:
        return SimResult(False, "not_triggered", 0.0)
    last = bars[-1].close
    return SimResult(True, "flatten", round(sign * (last - entry) / r, 3), last)
