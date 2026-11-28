"""Pure indicator math on plain lists of bars. No network, easy to test."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class Bar:
    open: float
    high: float
    low: float
    close: float
    volume: float


def vwap(bars: Sequence[Bar]) -> float | None:
    """Session VWAP from intraday bars, using typical price (H+L+C)/3."""
    total_vol = sum(b.volume for b in bars)
    if not bars or total_vol <= 0:
        return None
    return sum(((b.high + b.low + b.close) / 3) * b.volume for b in bars) / total_vol


def atr(bars: Sequence[Bar], period: int = 14) -> float | None:
    """Wilder's Average True Range. Needs period + 1 bars."""
    if len(bars) < period + 1:
        return None
    trs = []
    for prev, cur in zip(bars, bars[1:]):
        trs.append(
            max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close))
        )
    value = sum(trs[:period]) / period
    for tr in trs[period:]:
        value = (value * (period - 1) + tr) / period
    return value


def ema(values: Sequence[float], period: int) -> float | None:
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    value = sum(values[:period]) / period
    for v in values[period:]:
        value = v * k + value * (1 - k)
    return value


def average_volume(bars: Sequence[Bar], lookback: int) -> float | None:
    if len(bars) < lookback:
        return None
    window = bars[-lookback:]
    return sum(b.volume for b in window) / lookback


def relative_volume(
    today_volume: float, avg_daily_volume: float, session_fraction: float
) -> float | None:
    """Today's volume pace vs the average day.

    Compares volume so far with the average day's volume scaled by how much of
    the session has elapsed. Volume is front-loaded at the open, so this reads
    high early in the day; that is why entries wait until 09:45 by default.
    """
    if avg_daily_volume <= 0 or session_fraction <= 0:
        return None
    return today_volume / (avg_daily_volume * min(session_fraction, 1.0))


def pivot_levels(
    bars: Sequence[Bar], window: int = 3
) -> tuple[list[float], list[float]]:
    """Swing lows and highs: a bar whose low/high is the extreme of the
    `window` bars on each side. Returns (lows, highs) in time order."""
    lows, highs = [], []
    for i in range(window, len(bars) - window):
        seg = bars[i - window : i + window + 1]
        if bars[i].low == min(b.low for b in seg):
            lows.append(bars[i].low)
        if bars[i].high == max(b.high for b in seg):
            highs.append(bars[i].high)
    return lows, highs


def nearest_levels(
    price: float, lows: Sequence[float], highs: Sequence[float], extra: Sequence[float] = ()
) -> tuple[list[float], list[float]]:
    """Split candidate levels into supports below and resistances above the
    price, each sorted nearest first and de-duplicated to the cent."""
    candidates = {round(x, 2) for x in [*lows, *highs, *extra] if x and x > 0}
    supports = sorted((x for x in candidates if x < price), reverse=True)
    resistances = sorted(x for x in candidates if x > price)
    return supports, resistances
