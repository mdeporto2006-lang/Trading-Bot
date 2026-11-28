"""Fast bar-by-bar simulation of one bot on futures day sessions.

Fill rules (conservative, same spirit as the stock backtester):
- A signal on bar i's close is acted on at bar i+1's open (market order,
  pays slippage). Breakout entries are stop orders that fill from the bar
  after they're placed, at the trigger or the open if it gapped past, plus
  slippage. If one bar hits both the long and short breakout, it is skipped.
- Stops pay slippage; a bar that opens through the stop exits at the open.
  Targets are limit orders and fill at their price, but only if the price
  trades at least one tick THROUGH them (a touch may not fill you).
- If a bar touches both stop and target, the stop counts. On the bar a stop
  order fills, only the stop is checked (we can't know the order inside it).
- Breakeven, trailing and time stops update on bar closes.
- Everything is flat before the session ends. One contract per trade.
- Every trade pays the round-trip commission. A trade is a WIN only if its
  P&L after all costs is above zero (a breakeven exit is a small loss).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .contracts import CONTRACTS
from .data import Session


@dataclass
class Trade:
    symbol: str
    day: str
    direction: int  # +1 long, -1 short
    entry_t: int  # minutes after midnight ET
    exit_t: int
    entry: float
    exit: float
    stop: float
    target: float
    reason: str  # stop | breakeven | trail | target | time | flatten
    pnl: float  # dollars per contract, after costs
    r: float  # pnl / initial risk in dollars

    def to_row(self) -> dict:
        return {"symbol": self.symbol, "day": self.day, "dir": "long" if self.direction > 0 else "short",
                "in": f"{self.entry_t // 60:02d}:{self.entry_t % 60:02d}",
                "out": f"{self.exit_t // 60:02d}:{self.exit_t % 60:02d}",
                "entry": round(self.entry, 6), "exit": round(self.exit, 6), "reason": self.reason,
                "pnl": round(self.pnl, 2), "r": round(self.r, 3)}


def _prep(s: Session) -> None:
    if getattr(s, "_vw", None) is not None:
        return
    pv = vv = 0.0
    sq = 0.0
    vw, sd = [], []
    for i in range(len(s)):
        tp = (s.h[i] + s.l[i] + s.c[i]) / 3
        v = s.v[i] if s.v[i] > 0 else 1.0
        pv += tp * v
        vv += v
        w = pv / vv
        vw.append(w)
        sq += (s.c[i] - w) ** 2
        sd.append(math.sqrt(sq / (i + 1)))
    s._vw, s._sd = vw, sd


def _rt(x: float, tick: float) -> float:
    return round(round(x / tick) * tick, 10)


def simulate_session(g: dict, s: Session, slip_ticks: float = 1.0) -> list[Trade]:
    spec = CONTRACTS[s.symbol]
    _prep(s)
    tick, mult = spec.tick, spec.multiplier
    slip = slip_ticks * tick
    atr = s.atr
    fam = g["family"]
    n, iv = len(s), s.interval
    o, h, l, c, t, vw, sd = s.o, s.h, s.l, s.c, s.t, s._vw, s._sd
    tr = s.trend.get(g["trend_lookback"])
    tsign = 0 if not tr else (1 if tr > 0 else -1)
    win_from = s.open_min + g["entry_start_min"]
    win_to = s.close_min - g["entry_end_min"]
    flat_at = s.close_min - g["flatten_before_min"]
    trades: list[Trade] = []

    def allowed(d: int) -> bool:
        if g["direction"] == "long" and d < 0 or g["direction"] == "short" and d > 0:
            return False
        if fam == "trend_pullback":
            return d == tsign
        if g["trend_filter"] == "with":
            return d == tsign
        if g["trend_filter"] == "against":
            return d == -tsign
        return True

    def in_window(i: int) -> bool:
        return win_from <= t[i] < win_to and t[i] + iv <= flat_at

    def sane(risk: float) -> bool:
        return 2 * tick <= risk <= 1.5 * atr

    pos = None  # dict
    pending = None  # market entry for the next bar: (direction, stop_price or None, target_price or None)
    trades_today = 0
    orb = None  # (long trigger, short trigger, long stop, short stop)
    mom_done = False

    def close(i: int, price: float, reason: str, at_t: int) -> None:
        nonlocal pos
        p = pos
        d = p["d"]
        pnl = d * (price - p["entry"]) * mult - spec.commission_rt
        risk_dollars = p["risk"] * mult
        if reason == "stop":
            if (d > 0 and p["stop"] > p["stop0"]) or (d < 0 and p["stop"] < p["stop0"]):
                reason = "breakeven" if p.get("be") and abs(p["stop"] - p["entry"]) < tick / 2 else "trail"
        trades.append(Trade(s.symbol, s.day.isoformat(), d, p["t"], at_t, p["entry"], price, p["stop0"],
                            p["target"], reason, pnl, pnl / risk_dollars if risk_dollars else 0.0))
        pos = None

    def open_pos(i: int, d: int, fill: float, stop: float | None, target: float | None) -> bool:
        nonlocal pos, trades_today
        if stop is None:
            stop = fill - d * g["stop_atr"] * atr
        stop = _rt(stop, tick)
        risk = d * (fill - stop)
        if not sane(risk):
            return False
        if target is None:
            target = fill + d * g["target_r"] * risk
        target = _rt(target, tick)
        if d * (target - fill) < tick:
            return False
        pos = {"d": d, "entry": fill, "stop": stop, "stop0": stop, "risk": risk, "target": target,
               "i": i, "t": t[i], "be": False}
        trades_today += 1
        return True

    for i in range(n):
        filled_by_stop_order = False
        # 1. market entry decided at the previous close
        if pending and pos is None:
            d, st, tg = pending
            open_pos(i, d, o[i] + d * slip, st, tg)
        pending = None

        # 2. breakout stop orders
        if fam == "orb" and orb and pos is None and trades_today < g["max_trades_day"] and in_window(i):
            lt, stt, lstop, sstop = orb
            up = h[i] >= lt and allowed(1)
            dn = l[i] <= stt and allowed(-1)
            if up != dn:
                d = 1 if up else -1
                trig = lt if up else stt
                px = max(trig, o[i]) if up else min(trig, o[i])
                st = (lstop if up else sstop) if g["stop_mode"] == "structure" else None
                filled_by_stop_order = open_pos(i, d, px + d * slip, st, None)

        # 3. exits during this bar
        if pos is not None:
            d, stop, tgt = pos["d"], pos["stop"], pos["target"]
            same_bar = pos["i"] == i
            if not same_bar and d * (o[i] - stop) <= 0:
                close(i, o[i] - d * slip, "stop", t[i])
            elif (d > 0 and l[i] <= stop) or (d < 0 and h[i] >= stop):
                close(i, stop - d * slip, "stop", t[i] + iv)
            elif not filled_by_stop_order and ((d > 0 and h[i] >= tgt + tick) or (d < 0 and l[i] <= tgt - tick)):
                close(i, tgt, "target", t[i] + iv)

        # 4. on the close: manage, time stop, flatten
        if pos is not None:
            d, risk = pos["d"], pos["risk"]
            moved = d * (c[i] - pos["entry"])
            if g["breakeven_r"] and not pos["be"] and moved >= g["breakeven_r"] * risk:
                if d * (pos["entry"] - pos["stop"]) > 0:
                    pos["stop"] = pos["entry"]
                pos["be"] = True
            if g["trail_after_r"] and moved >= g["trail_after_r"] * risk:
                new = _rt(c[i] - d * g["trail_dist_r"] * risk, tick)
                if d * (new - pos["stop"]) > 0:
                    pos["stop"] = new
            if t[i] + iv >= flat_at or i == n - 1:
                close(i, c[i] - d * slip, "flatten", t[i] + iv)
            elif g["time_stop_bars"] and i - pos["i"] + 1 >= g["time_stop_bars"]:
                close(i, c[i] - d * slip, "time", t[i] + iv)

        # 5. signals from this bar's close (act next bar)
        if fam == "orb" and orb is None and t[i] + iv >= s.open_min + g["or_minutes"]:
            hi, lo = max(h[: i + 1]), min(l[: i + 1])
            rng = hi - lo
            if g["or_min_atr"] * atr <= rng <= g["or_max_atr"] * atr:
                b = g["orb_buffer_ticks"] * tick
                orb = (hi + b, lo - b, lo - tick, hi + tick)
            else:
                orb = (math.inf, -math.inf, 0, 0)  # range filter failed: no trade today
        if pos is not None or i + 1 >= n or trades_today >= g["max_trades_day"] or not in_window(i + 1):
            continue
        if fam == "vwap_revert" and i + 1 >= g["vwap_min_bars"] and sd[i] > 0:
            dev = (c[i] - vw[i]) / sd[i]
            d = -1 if dev >= g["dev_k"] else (1 if dev <= -g["dev_k"] else 0)
            if d and g["vwap_confirm"] and not (d * (c[i] - o[i]) > 0):
                d = 0
            if d and allowed(d):
                st = (min(l[max(0, i - 1): i + 1]) - tick if d > 0 else max(h[max(0, i - 1): i + 1]) + tick) \
                    if g["stop_mode"] == "structure" else None
                tg = vw[i] if g["target_mode"] == "vwap" else None
                pending = (d, st, tg)
        elif fam == "intraday_mom" and not mom_done and t[i] + iv >= s.open_min + g["mom_window_min"]:
            mom_done = True
            ref = o[0] if g["mom_from"] == "open" or s.prev_close is None else s.prev_close
            move = c[i] - ref
            if abs(move) >= g["mom_threshold_atr"] * atr and move != 0:
                d = 1 if move > 0 else -1
                if allowed(d):
                    st = (min(l[: i + 1]) - tick if d > 0 else max(h[: i + 1]) + tick) \
                        if g["stop_mode"] == "structure" else None
                    pending = (d, st, None)
        elif fam == "trend_pullback" and tsign and i >= g["pull_above_bars"]:
            d = tsign
            prior_ok = all(d * (c[j] - vw[j]) > 0 for j in range(i - g["pull_above_bars"], i))
            touch = (l[i] <= vw[i] + g["pull_touch_atr"] * atr) if d > 0 else (h[i] >= vw[i] - g["pull_touch_atr"] * atr)
            if prior_ok and touch and d * (c[i] - vw[i]) > 0 and allowed(d):
                st = ((min(l[i], vw[i]) - tick) if d > 0 else (max(h[i], vw[i]) + tick)) \
                    if g["stop_mode"] == "structure" else None
                pending = (d, st, None)
    return trades


def run(g: dict, data: dict[str, list[Session]], days: set | None = None, slip_ticks: float = 1.0) -> list[Trade]:
    out: list[Trade] = []
    for sym in g["symbols"]:
        for s in data.get(sym, ()):
            if days is None or s.day in days:
                out.extend(simulate_session(g, s, slip_ticks))
    out.sort(key=lambda x: (x.day, x.entry_t, x.symbol))
    return out


def wilson_low(wins: int, n: int, z: float = 1.0) -> float:
    if n == 0:
        return 0.0
    p = wins / n
    den = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (centre - margin) / den


def metrics(trades: list[Trade]) -> dict:
    n = len(trades)
    if n == 0:
        return {"trades": 0, "wins": 0, "win_rate": 0.0, "exp_r": 0.0, "exp_usd": 0.0, "pf": 0.0,
                "total_usd": 0.0, "total_r": 0.0, "max_dd_r": 0.0, "avg_win_r": 0.0, "avg_loss_r": 0.0,
                "days_traded": 0, "exits": {}}
    wins = [x for x in trades if x.pnl > 0]
    losses = [x for x in trades if x.pnl <= 0]
    gw = sum(x.r for x in wins)
    gl = -sum(x.r for x in losses)
    eq = peak = dd = 0.0
    for x in trades:
        eq += x.r
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    exits: dict[str, int] = {}
    for x in trades:
        exits[x.reason] = exits.get(x.reason, 0) + 1
    return {
        "trades": n, "wins": len(wins), "win_rate": len(wins) / n,
        "exp_r": sum(x.r for x in trades) / n, "exp_usd": sum(x.pnl for x in trades) / n,
        "pf": (gw / gl) if gl > 0 else (99.0 if gw > 0 else 0.0),
        "total_usd": sum(x.pnl for x in trades), "total_r": sum(x.r for x in trades), "max_dd_r": dd,
        "avg_win_r": gw / len(wins) if wins else 0.0, "avg_loss_r": -gl / len(losses) if losses else 0.0,
        "days_traded": len({x.day for x in trades}), "exits": exits,
    }
