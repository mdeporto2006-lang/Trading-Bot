"""Lesson library: past setups and how they actually turned out.

Every priced plan the time machine or a backtest scores lands here with a few
numeric features of its setup. When the planner looks at a new setup it is
shown the most similar past setups and their outcomes ("5 similar longs:
2 won, average -0.1R").

No lookahead: an entry is usable only once its outcome was known (its
`known_at`, the time the trade ended). In a backtest that means a setup from
11:00 today can't inform a decision at 10:30 today, or at 11:30 if it was
still open then.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from ..clock import session_fraction_elapsed
from ..models import MarketSnapshot

FEATURES = ("rvol_log", "vwap_dist_atr", "time_of_day", "gap_atr", "rr")


def features(snap: MarketSnapshot, entry: float, stop: float, target: float) -> dict[str, float]:
    atr = snap.atr or 0.0
    rvol = snap.relative_volume or 0.0
    risk = abs(entry - stop) or 1e-9
    gap = 0.0
    if snap.prior_close and snap.day_low is not None and atr:
        # Open isn't kept in the snapshot; the day's first visible range is a fair stand-in.
        gap = ((snap.day_high + snap.day_low) / 2 - snap.prior_close) / atr
    return {
        "rvol_log": math.log(max(rvol, 0.05)),
        "vwap_dist_atr": ((snap.last_price or 0) - (snap.vwap or 0)) / atr if atr else 0.0,
        "time_of_day": session_fraction_elapsed(snap.as_of),
        "gap_atr": gap,
        "rr": abs(target - entry) / risk,
    }


@dataclass
class Lesson:
    ticker: str
    as_of: str  # decision time
    known_at: str  # when the outcome was known (exit time)
    direction: str
    features: dict
    r_multiple: float
    exit_reason: str
    source: str = "backtest"  # backtest | timemachine | live

    def to_dict(self) -> dict:
        return self.__dict__.copy()


class LessonLibrary:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None
        self.items: list[Lesson] = []
        if self.path and self.path.exists():
            for line in self.path.read_text().splitlines():
                try:
                    self.items.append(Lesson(**json.loads(line)))
                except (json.JSONDecodeError, TypeError):
                    continue

    def add(self, lesson: Lesson, persist: bool = True) -> None:
        self.items.append(lesson)
        if persist and self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as f:
                f.write(json.dumps(lesson.to_dict()) + "\n")

    def similar(self, feats: dict, direction: str, now: datetime, k: int = 5,
                exclude_ticker_day: Optional[tuple[str, str]] = None) -> list[Lesson]:
        pool = [
            l for l in self.items
            if l.direction == direction and datetime.fromisoformat(l.known_at) <= now
            and not (exclude_ticker_day and (l.ticker, l.as_of[:10]) == exclude_ticker_day)
        ]
        if not pool:
            return []
        scale = {f: (_std([l.features.get(f, 0.0) for l in pool]) or 1.0) for f in FEATURES}

        def dist(l: Lesson) -> float:
            return math.sqrt(sum(((l.features.get(f, 0.0) - feats.get(f, 0.0)) / scale[f]) ** 2 for f in FEATURES))

        return sorted(pool, key=dist)[:k]


def _std(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def summarize(similar: Iterable[Lesson]) -> dict | None:
    s = list(similar)
    if not s:
        return None
    rs = [l.r_multiple for l in s]
    return {
        "count": len(s),
        "wins": sum(1 for r in rs if r > 0),
        "avg_r": round(sum(rs) / len(rs), 2),
        "examples": [
            {"ticker": l.ticker, "date": l.as_of[:16], "direction": l.direction,
             "result_r": round(l.r_multiple, 2), "exit": l.exit_reason,
             "setup": {k: round(v, 2) for k, v in l.features.items()}}
            for l in s
        ],
    }
