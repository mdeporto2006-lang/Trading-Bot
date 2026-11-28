"""The time machine: drop the bot at any past moment and see what it would do.

  1. Rewind: build the snapshot exactly as it was at that minute (pit.py):
     closed bars only, news published by then, the quote live then.
  2. Plan: the rules planner or Claude plans with only that snapshot, plus the
     most similar past setups whose outcomes were already known by then.
  3. Gate: the same risk gate as live.
  4. Fast-forward: replay the bars that followed to the 15:50 flatten and
     score the plan (stop, target, trailing stop, or flatten, in R).
  5. Remember: the scored setup goes into the lesson library, so future plans
     (live, backtest or time machine) can see how setups like it ended.

Caveat for Claude: it was trained on data up to some date, so for older
moments it may "remember" what happened next. The most recent dates are the
fairest test of its judgment; the rules planner has no such problem.
"""

from __future__ import annotations

import json
import random
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from .. import checklist as ck
from ..clock import ET, parse_hhmm
from ..config import Config
from ..risk import AccountState, evaluate_plan
from ..simulate import simulate_bracket
from .data import HistoricalSource, session_bounds
from .library import Lesson, LessonLibrary, features, summarize
from .pit import PitOptions, snapshot_at
from .rules_planner import direction_for


def visit(cfg: Config, src: HistoricalSource, ticker: str, at: datetime, planner,
          pit: PitOptions, library: LessonLibrary, equity: float = 100_000.0) -> dict:
    ticker = ticker.upper()
    day = at.astimezone(ET).date()
    open_utc, close_utc = session_bounds(day)
    minutes = src.minutes(ticker, day)
    dailies = src.daily(ticker, day - timedelta(days=90), day)
    news = []
    if pit.catalyst == "real" and src.has_news:
        news = src.news(ticker, at - timedelta(hours=cfg.setup.catalyst_max_age_hours * 2), at)
    quote = src.quote_at(ticker, at) if src.has_quotes else None
    snap = snapshot_at(ticker, at, cfg, minutes, dailies, news, quote, pit)

    similar = None
    d0 = direction_for(snap)
    if d0 and snap.atr and snap.last_price:
        lp, a = snap.last_price, snap.atr
        probe = features(snap, lp, lp - a if d0 == "long" else lp + a, lp + 2 * a if d0 == "long" else lp - 2 * a)
        similar = summarize(library.similar(probe, d0, at, exclude_ticker_day=(ticker, at.date().isoformat())))

    plan = planner.plan(snap, similar=similar)
    acct = AccountState(equity=equity, buying_power=equity * 4, start_of_day_equity=equity,
                        shortable={ticker: True})
    decision = evaluate_plan(plan, snap, cfg, acct, at)

    flat_t = parse_hhmm(cfg.exits.flatten_time_et)
    flat_at = datetime.combine(day, flat_t, ET).astimezone(timezone.utc)
    after = [b for t, b in minutes if at <= t < flat_at]
    nxt = None
    if after:
        nxt = {"price_then": snap.last_price, "high_after": max(b.high for b in after),
               "low_after": min(b.low for b in after), "close_at_flatten": after[-1].close}

    outcome = None
    if plan.action in ("BUY", "SHORT") and None not in (plan.entry_trigger, plan.stop, plan.target) and after:
        d = "long" if plan.action == "BUY" else "short"
        target = ck.effective_target(snap, d, plan.entry_trigger, plan.target)
        sim = simulate_bracket(d, plan.entry_trigger, plan.stop, target, after,
                               cfg.exits.trail_after_r, cfg.exits.trail_distance_r)
        outcome = {"direction": d, "triggered": sim.triggered, "exit_reason": sim.exit_reason,
                   "r_multiple": sim.r_multiple, "exit_price": sim.exit_price}
        if sim.triggered:
            library.add(Lesson(ticker=ticker, as_of=at.isoformat(), known_at=flat_at.isoformat(),
                               direction=d, features=features(snap, plan.entry_trigger, plan.stop, target),
                               r_multiple=sim.r_multiple, exit_reason=sim.exit_reason, source="timemachine"))

    return {
        "ticker": ticker,
        "at": at.isoformat(),
        "source": src.name,
        "planner": getattr(planner, "label", type(planner).__name__),
        "snapshot": snap.model_dump(mode="json"),
        "similar_setups": similar,
        "plan": plan.model_dump(mode="json"),
        "decision": decision.model_dump(mode="json"),
        "what_happened_next": nxt,
        "outcome": outcome,
    }


def random_moments(start: date, end: date, n: int, cfg: Config, seed: int = 1) -> list[datetime]:
    rng = random.Random(seed)
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    days = [d for d in days if d.weekday() < 5]
    lo, hi = parse_hhmm(cfg.session.no_entries_before_et), parse_hhmm(cfg.session.no_entries_after_et)
    span = (hi.hour * 60 + hi.minute) - (lo.hour * 60 + lo.minute)
    out = []
    for _ in range(n):
        d = rng.choice(days)
        m = lo.hour * 60 + lo.minute + rng.randrange(span)
        out.append(datetime.combine(d, time(m // 60, m % 60), ET).astimezone(timezone.utc))
    return sorted(out)


def append_log(path: str | Path, rec: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    slim = {k: v for k, v in rec.items() if k != "snapshot"}
    s = rec["snapshot"]
    slim["snapshot"] = {k: s.get(k) for k in ("last_price", "vwap", "atr", "relative_volume", "spread_pct",
                                              "avg_daily_volume", "supports", "resistances", "missing")}
    slim["snapshot"]["news"] = [{"headline": n["headline"][:160], "age_hours": n["age_hours"]}
                                for n in (s.get("news") or [])[:3]]
    with p.open("a") as f:
        f.write(json.dumps(slim, default=str) + "\n")
