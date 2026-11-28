"""Futures history for the breeder: day sessions of intraday bars, plus what
was known before each session opened (daily ATR and trend).

Free data, and how little of it there is:
- Yahoo serves 5-minute bars for only the last ~60 calendar days (about 40
  trading sessions) and 1-minute bars for only the last ~30 days. That is one
  market regime, a few weeks long. Anything bred on it can be luck.
- Daily bars go back years; they are used only for ATR and the trend filter,
  and only completed days strictly before the session.
- Prices are the continuous front-month series. Roll days can show a jump.

No-lookahead rules:
- A bar stamped t covers [t, t + interval). A signal on a bar's close acts
  at the next bar's open. Stop orders can fill only from the next bar on.
- ATR and trend for day D use daily bars dated before D only.
"""

from __future__ import annotations

import json
import logging
import math
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ..clock import ET
from .contracts import CONTRACTS, hhmm

log = logging.getLogger(__name__)

TREND_LOOKBACKS = (5, 10, 20, 60)


@dataclass
class Session:
    symbol: str
    day: date
    interval: int  # minutes per bar
    t: list[int]  # bar start, minutes after midnight ET
    o: list[float]
    h: list[float]
    l: list[float]
    c: list[float]
    v: list[float]
    open_min: int  # session window in minutes after midnight ET
    close_min: int
    prev_close: float | None = None  # last completed daily close before this day
    atr: float | None = None  # 14-day ATR from completed days
    trend: dict[int, float] = field(default_factory=dict)  # lookback days -> % return up to yesterday

    def __len__(self) -> int:
        return len(self.t)


def _atr(daily: list[tuple[float, float, float, float]], n: int = 14) -> float | None:
    if len(daily) < n + 1:
        return None
    trs = []
    for i in range(1, len(daily)):
        _, h, l, _ = daily[i]
        pc = daily[i - 1][3]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-n:]) / n


def build_sessions(symbol: str, intraday: list[tuple[int, float, float, float, float, float]],
                   daily: list[tuple[date, float, float, float, float]], interval: int,
                   min_bars_frac: float = 0.8) -> list[Session]:
    """intraday: (unix ts, o, h, l, c, v) bar starts. daily: (date, o, h, l, c)."""
    spec = CONTRACTS[symbol]
    w_open, w_close = hhmm(spec.session_et[0]), hhmm(spec.session_et[1])
    by_day: dict[date, list] = {}
    for ts, o, h, l, c, v in intraday:
        et = datetime.fromtimestamp(ts, timezone.utc).astimezone(ET)
        m = et.hour * 60 + et.minute
        if w_open <= m and m + interval <= w_close:
            by_day.setdefault(et.date(), []).append((m, o, h, l, c, v))
    daily = sorted(daily)
    want = (w_close - w_open) // interval
    out = []
    for d in sorted(by_day):
        rows = sorted(set(by_day[d]))
        if len(rows) < min_bars_frac * want or d.weekday() >= 5:
            continue  # holiday / half day / bad data
        before = [x for x in daily if x[0] < d]
        s = Session(symbol, d, interval, *map(list, zip(*rows)), open_min=w_open, close_min=w_close)
        if before:
            s.prev_close = before[-1][4]
            s.atr = _atr([x[1:] for x in before[-30:]])
            for lb in TREND_LOOKBACKS:
                if len(before) > lb:
                    s.trend[lb] = (before[-1][4] / before[-1 - lb][4] - 1) * 100
        if s.atr:
            out.append(s)
    return out


# ---------------------------------------------------------------------------
# Yahoo
# ---------------------------------------------------------------------------
def fetch_yahoo(symbols: list[str], interval: int, cache_dir: str | Path) -> dict[str, list[Session]]:
    """5m bars: range 60d. 1m bars: range 30d is refused, so ask for 7-day windows."""
    from ..backtest.data import YahooHistory

    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    y = YahooHistory()
    out: dict[str, list[Session]] = {}
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for sym in symbols:
        path = cache / f"{sym.replace('=', '_')}_{interval}m_{stamp}.json"
        if path.exists():
            raw = json.loads(path.read_text())
        else:
            intra = []
            if interval == 1:
                now = int(datetime.now(timezone.utc).timestamp())
                for k in range(4):
                    p2 = now - k * 7 * 86400
                    try:
                        res = y._get(sym, {"period1": p2 - 7 * 86400, "period2": p2, "interval": "1m",
                                           "includePrePost": "false"})
                    except Exception as e:  # older windows get refused
                        log.info("%s 1m window %d refused: %s", sym, k, e)
                        continue
                    intra += [(int(ts.timestamp()), b.open, b.high, b.low, b.close, b.volume)
                              for ts, b in y.parse(res)]
            else:
                res = y._get(sym, {"range": "60d", "interval": f"{interval}m", "includePrePost": "false"})
                intra = [(int(ts.timestamp()), b.open, b.high, b.low, b.close, b.volume) for ts, b in y.parse(res)]
            res = y._get(sym, {"range": "2y", "interval": "1d", "includePrePost": "false"})
            daily = [(ts.astimezone(ET).date().isoformat(), b.open, b.high, b.low, b.close) for ts, b in y.parse(res)]
            raw = {"intraday": sorted(set(map(tuple, intra))), "daily": daily}
            path.write_text(json.dumps(raw))
        daily = [(date.fromisoformat(d), o, h, l, c) for d, o, h, l, c in raw["daily"]]
        out[sym] = build_sessions(sym, [tuple(r) for r in raw["intraday"]], daily, interval)
        log.info("%s: %d sessions of %dm bars", sym, len(out[sym]), interval)
    return out


# ---------------------------------------------------------------------------
# Synthetic (tests, offline demos) and the null test
# ---------------------------------------------------------------------------
_BASE = {"ES=F": 6000, "NQ=F": 21000, "YM=F": 44000, "RTY=F": 2300, "CL=F": 70, "GC=F": 2600,
         "ZN=F": 110, "6E=F": 1.08}


def synthetic(symbols: list[str], days: int = 40, interval: int = 5, seed: int = 7,
              end: date = date(2026, 11, 27)) -> dict[str, list[Session]]:
    """Random walks. Made up: they say nothing about real markets."""
    rng = random.Random(seed)
    out = {}
    for sym in symbols:
        spec = CONTRACTS[sym]
        px = _BASE.get(sym, 100.0)
        daily_vol = 0.011
        w_open, w_close = hhmm(spec.session_et[0]), hhmm(spec.session_et[1])
        d = end - timedelta(days=int(days * 1.5) + 100)
        daily, intra = [], []
        while d <= end:
            if d.weekday() < 5:
                o = px * (1 + rng.gauss(0, daily_vol * 0.3))
                n = (w_close - w_open) // interval
                bar_vol = daily_vol / math.sqrt(n)
                p = o
                hi, lo = o, o
                start = datetime(d.year, d.month, d.day, tzinfo=ET)
                for i in range(n):
                    # a real path inside the bar, so wicks are prices that traded
                    path = [p]
                    for _ in range(6):
                        path.append(path[-1] * (1 + rng.gauss(0, bar_vol / math.sqrt(6))))
                    c, h, lw = path[-1], max(path), min(path)
                    ts = start + timedelta(minutes=w_open + i * interval)
                    intra.append((int(ts.timestamp()), p, h, lw, c, 1000.0))
                    hi, lo, p = max(hi, h), min(lo, lw), c
                px = p * (1 + rng.gauss(0, daily_vol * 0.3))
                daily.append((d, o, max(hi, px), min(lo, px), px))
            d += timedelta(days=1)
        out[sym] = build_sessions(sym, intra, daily, interval)[-days:]
    return out


def shuffled(data: dict[str, list[Session]], seed: int = 99) -> dict[str, list[Session]]:
    """The null test: same days, same bar sizes, but each session's bars are put
    in random order. Volatility and ranges stay; any real intraday pattern
    (momentum, mean reversion, opening drive) is destroyed. Whatever the
    breeder "finds" here is pure luck, which tells us how much in-sample
    results can be trusted."""
    rng = random.Random(seed)
    out = {}
    for sym, sessions in data.items():
        new = []
        for s in sessions:
            idx = list(range(len(s)))
            rng.shuffle(idx)
            p = s.o[0]
            o, h, l, c = [], [], [], []
            for i in idx:
                r = s.c[i] / s.o[i]
                up, dn = s.h[i] / max(s.o[i], s.c[i]), min(s.o[i], s.c[i]) / s.l[i]
                cc = p * r
                o.append(p); c.append(cc)
                h.append(max(p, cc) * up); l.append(min(p, cc) / dn)
                p = cc
            new.append(Session(s.symbol, s.day, s.interval, list(s.t), o, h, l, c, list(s.v),
                               s.open_min, s.close_min, s.prev_close, s.atr, dict(s.trend)))
        out[sym] = new
    return out


def split_days(data: dict[str, list[Session]], fracs=(0.5, 0.25, 0.25)) -> tuple[list[date], list[date], list[date]]:
    """Chronological train / validate / holdout split by calendar day."""
    days = sorted({s.day for ss in data.values() for s in ss})
    n = len(days)
    a = round(n * fracs[0])
    b = a + round(n * fracs[1])
    return days[:a], days[a:b], days[b:]
