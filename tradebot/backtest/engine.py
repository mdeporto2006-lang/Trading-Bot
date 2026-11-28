"""The backtest engine: replays trading days minute by minute.

Same pieces as the live bot:
- the same snapshot math (market_data.build_snapshot), cut point in time (pit.py),
- the same planner contract (rules-only planner, or Claude),
- the same risk gate (risk.evaluate_plan) with a simulated account: risk per
  trade, max position size, max open positions, max trades a day, the daily
  loss limit, the entry window,
- the same exits: stop-limit entry, bracket with stop and target, the stop
  trails 1R behind once a trade is up 1R (only ever tightens), unfilled
  entries cancelled at 15:30 ET, everything flat at 15:50 ET.

Fill rules (conservative):
- Decisions at minute t use bars that closed by t. Orders can fill from the
  bar that starts at t onwards, never earlier.
- Entry fills at the trigger, or at the bar's open if it gapped through the
  trigger but stayed inside the stop-limit band; past the band it doesn't fill.
- If a bar touches both stop and target, the stop counts. A bar that opens
  through the stop exits at the open. The fill bar is checked for the stop too.
- Stops and the 15:50 flatten pay `slippage_bps`; targets are limit fills.
- Shorts are assumed borrowable (the live bot checks Alpaca's easy-to-borrow
  flag; history of that flag isn't available).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional

from .. import checklist as ck
from ..clock import ET, parse_hhmm
from ..config import Config
from ..indicators import Bar
from ..models import MarketSnapshot, TradePlan
from ..risk import AccountState, evaluate_plan
from .data import HistoricalSource, MinuteRow, session_bounds
from .library import Lesson, LessonLibrary, features, summarize
from .pit import PitOptions, snapshot_at
from .rules_planner import RulesPlanner, prefilter

log = logging.getLogger(__name__)


@dataclass
class BacktestOptions:
    start_equity: float = 100_000.0
    slippage_bps: float = 2.0  # 0.02% on market-style fills (stops, flatten)
    commission_per_share: float = 0.0
    pit: PitOptions = field(default_factory=PitOptions)
    use_library: bool = False  # show similar past setups to the planner
    max_planner_calls: Optional[int] = None  # hard stop for paid planner calls


@dataclass
class Order:
    id: str
    ticker: str
    direction: str
    qty: int
    trigger: float
    limit: float
    stop: float
    target: float
    placed_at: datetime
    plan: dict
    feats: dict


@dataclass
class Position:
    order: Order
    entry_time: datetime
    entry: float
    stop: float
    risk_per_share: float  # from the planned trigger and stop (what sizing used)


@dataclass
class Trade:
    id: str
    ticker: str
    day: str
    direction: str
    qty: int
    planned_entry: float
    entry: float
    initial_stop: float
    target: float
    entry_time: str
    exit_time: str
    exit: float
    exit_reason: str  # stop | trail_stop | target | flatten
    r_multiple: float
    pnl: float
    confidence: float


@dataclass
class DecisionLog:
    scans: int = 0
    no_data: int = 0
    prefilter_pass: int = 0
    planner_calls: int = 0
    priced: int = 0
    approved: int = 0
    reject_reasons: dict = field(default_factory=dict)


def _adverse(price: float, direction: str, bps: float, exiting: bool) -> float:
    # Buying (long entry, short exit) pays up; selling pays down.
    buying = (direction == "long") != exiting
    return price * (1 + bps / 10_000) if buying else price * (1 - bps / 10_000)


class BacktestEngine:
    def __init__(self, cfg: Config, source: HistoricalSource, planner=None,
                 opts: BacktestOptions | None = None, library: LessonLibrary | None = None,
                 progress: Callable[[str], None] | None = None):
        self.cfg = cfg
        self.src = source
        self.planner = planner or RulesPlanner(cfg)
        self.paid_planner = not isinstance(self.planner, RulesPlanner)
        self.opts = opts or BacktestOptions()
        self.library = library if library is not None else LessonLibrary()
        self.progress = progress or (lambda s: None)
        self.trades: list[Trade] = []
        self.decisions = DecisionLog()
        self.priced_plans: list[dict] = []  # every BUY/SHORT plan with its would-be outcome
        self.equity_curve: list[dict] = []
        self.days: list[str] = []
        self.lessons: list[Lesson] = []  # finished trades, as lesson-library entries

    # ------------------------------------------------------------------
    def run(self, tickers: list[str], start: date, end: date) -> dict:
        tickers = [t.upper() for t in tickers]
        dailies = {t: self.src.daily(t, start - timedelta(days=90), end) for t in tickers}
        equity = self.opts.start_equity
        d = start
        while d <= end:
            if d.weekday() < 5:
                minutes = {t: self.src.minutes(t, d) for t in tickers}
                if any(minutes.values()):
                    equity = self._run_day(d, tickers, minutes, dailies, equity)
                    self.days.append(d.isoformat())
                    self.equity_curve.append({"date": d.isoformat(), "equity": round(equity, 2)})
                    self.progress(f"{d}  equity {equity:,.2f}  trades so far {len(self.trades)}")
            d += timedelta(days=1)
        return {"equity": equity}

    # ------------------------------------------------------------------
    def _run_day(self, day: date, tickers: list[str], minutes: dict[str, list[MinuteRow]],
                 dailies: dict, equity: float) -> float:
        cfg, opts = self.cfg, self.opts
        open_utc, close_utc = session_bounds(day)
        bars_at: dict[str, dict[datetime, Bar]] = {t: dict(rows) for t, rows in minutes.items()}
        news = {}
        if opts.pit.catalyst == "real" and self.src.has_news:
            lookback = timedelta(hours=cfg.setup.catalyst_max_age_hours * 2)
            news = {t: self.src.news(t, open_utc - lookback, close_utc) for t in tickers}

        start_equity = equity
        realized = 0.0
        pending: dict[str, Order] = {}
        positions: dict[str, Position] = {}
        last_close: dict[str, float] = {}
        trades_today = 0
        scan_every = max(1, cfg.session.scan_interval_seconds // 60)
        t_entry_from = parse_hhmm(cfg.session.no_entries_before_et)
        t_entry_to = parse_hhmm(cfg.session.no_entries_after_et)
        t_flat = parse_hhmm(cfg.exits.flatten_time_et)
        flattened = False

        def mark_equity() -> float:
            unreal = 0.0
            for t, p in positions.items():
                px = last_close.get(t, p.entry)
                sign = 1 if p.order.direction == "long" else -1
                unreal += sign * (px - p.entry) * p.order.qty
            return start_equity + realized + unreal

        def close(t: str, now: datetime, price: float, reason: str) -> None:
            nonlocal realized
            p = positions.pop(t)
            o = p.order
            sign = 1 if o.direction == "long" else -1
            pnl = sign * (price - p.entry) * o.qty - opts.commission_per_share * o.qty * 2
            realized += pnl
            r = sign * (price - p.entry) / p.risk_per_share if p.risk_per_share else 0.0
            if reason == "stop" and ((o.direction == "long" and p.stop > o.stop) or
                                     (o.direction == "short" and p.stop < o.stop)):
                reason = "trail_stop"
            self.trades.append(Trade(
                id=o.id, ticker=t, day=day.isoformat(), direction=o.direction, qty=o.qty,
                planned_entry=o.trigger, entry=round(p.entry, 4), initial_stop=o.stop, target=o.target,
                entry_time=p.entry_time.isoformat(), exit_time=now.isoformat(), exit=round(price, 4),
                exit_reason=reason, r_multiple=round(r, 3), pnl=round(pnl, 2),
                confidence=float(o.plan.get("confidence") or 0),
            ))
            lesson = Lesson(ticker=t, as_of=o.placed_at.isoformat(), known_at=now.isoformat(),
                            direction=o.direction, features=o.feats, r_multiple=round(r, 3),
                            exit_reason=reason, source="backtest")
            self.lessons.append(lesson)
            if opts.use_library:
                # Known from `now` on, so only later decisions can see it.
                self.library.add(lesson, persist=False)

        for i in range(390):
            now = open_utc + timedelta(minutes=i)
            et = now.astimezone(ET).time()

            # 1. Flatten at 15:50 ET: cancel entries, exit at this bar's open.
            if et >= t_flat and not flattened:
                pending.clear()
                for t in list(positions):
                    b = bars_at[t].get(now)
                    px = b.open if b else last_close.get(t, positions[t].entry)
                    close(t, now, _adverse(px, positions[t].order.direction, opts.slippage_bps, True), "flatten")
                flattened = True
            if et >= t_entry_to:
                pending.clear()  # unfilled entries cancelled at 15:30

            # 2. Scan: plan with only what was known at `now`.
            minutes_since_open = i
            if (not flattened and t_entry_from <= et < t_entry_to and minutes_since_open % scan_every == 0):
                for t in tickers:
                    if t in positions or t in pending:
                        continue
                    acct = AccountState(
                        equity=mark_equity(), buying_power=mark_equity() * 4,
                        start_of_day_equity=start_equity,
                        open_symbols=set(positions) | set(pending), trades_today=trades_today,
                        shortable={x: True for x in tickers},
                    )
                    order = self._decide(t, now, minutes[t], dailies[t], news.get(t, []), acct)
                    if order:
                        pending[t] = order
                        trades_today += 1

            # 3. Let this minute's bar happen.
            for t in tickers:
                b = bars_at[t].get(now)
                if b is None:
                    continue
                if t in pending:
                    o = pending[t]
                    long_ = o.direction == "long"
                    hit = b.high >= o.trigger if long_ else b.low <= o.trigger
                    if hit:
                        px = max(o.trigger, b.open) if long_ else min(o.trigger, b.open)
                        if (long_ and px <= o.limit) or (not long_ and px >= o.limit):
                            fill = _adverse(px, o.direction, opts.slippage_bps, False)
                            positions[t] = Position(o, now, fill, o.stop, abs(o.trigger - o.stop))
                            del pending[t]
                if t in positions:
                    p = positions[t]
                    long_ = p.order.direction == "long"
                    sign = 1 if long_ else -1
                    gap_through = (b.open <= p.stop) if long_ else (b.open >= p.stop)
                    stop_hit = (b.low <= p.stop) if long_ else (b.high >= p.stop)
                    tgt_hit = (b.high >= p.order.target) if long_ else (b.low <= p.order.target)
                    if gap_through and p.entry_time != now:
                        close(t, now, _adverse(b.open, p.order.direction, opts.slippage_bps, True), "stop")
                    elif stop_hit:
                        close(t, now, _adverse(p.stop, p.order.direction, opts.slippage_bps, True), "stop")
                    elif tgt_hit:
                        close(t, now, p.order.target, "target")
                    else:
                        r = p.risk_per_share
                        moved = sign * (b.close - p.entry)
                        if r and moved >= cfg.exits.trail_after_r * r:
                            new = b.close - sign * cfg.exits.trail_distance_r * r
                            if (long_ and new > p.stop) or (not long_ and new < p.stop):
                                p.stop = new
                last_close[t] = b.close

        # Anything still open (missing 15:50 bar): close at the last price.
        end_now = close_utc
        for t in list(positions):
            close(t, end_now, _adverse(last_close.get(t, positions[t].entry), positions[t].order.direction,
                                       opts.slippage_bps, True), "flatten")
        return start_equity + realized

    # ------------------------------------------------------------------
    def _decide(self, t: str, now: datetime, minutes_today, dailies, news_items, acct: AccountState) -> Optional[Order]:
        cfg, dl = self.cfg, self.decisions
        dl.scans += 1
        quote = None
        snap = snapshot_at(t, now, cfg, minutes_today, dailies, news_items, None, self.opts.pit)
        if snap.missing:
            dl.no_data += 1
            return None
        ok, direction, _ = prefilter(snap, cfg)
        if not ok:
            return None
        if self.src.has_quotes:
            # Only fetch the real quote for setups that could trade (saves thousands of calls).
            quote = self.src.quote_at(t, now)
            snap = snapshot_at(t, now, cfg, minutes_today, dailies, news_items, quote, self.opts.pit)
            ok, direction, _ = prefilter(snap, cfg)
            if not ok:
                return None
        dl.prefilter_pass += 1

        similar = None
        if self.opts.use_library and direction:
            # Rough features for lookup before prices exist: use a 2R plan shape.
            lp, atr = snap.last_price, snap.atr or 0
            probe = features(snap, lp, lp - atr if direction == "long" else lp + atr,
                             lp + 2 * atr if direction == "long" else lp - 2 * atr)
            similar = summarize(self.library.similar(probe, direction, now))

        if self.paid_planner:
            if self.opts.max_planner_calls is not None and dl.planner_calls >= self.opts.max_planner_calls:
                return None
            dl.planner_calls += 1
        plan: TradePlan = self.planner.plan(snap, similar=similar)
        if plan.action == "NO_TRADE":
            self._reason("planner NO_TRADE")
            return None
        dl.priced += 1
        decision = evaluate_plan(plan, snap, cfg, acct, now)
        d = "long" if plan.action == "BUY" else "short"
        feats = features(snap, plan.entry_trigger, plan.stop, plan.target)
        self.priced_plans.append({
            "ticker": t, "as_of": now.isoformat(), "direction": d, "approved": decision.approved,
            "entry": plan.entry_trigger, "stop": plan.stop,
            "target": ck.effective_target(snap, d, plan.entry_trigger, plan.target),
            "confidence": plan.confidence, "reasons": decision.reasons, "features": feats,
            "snapshot": snap.model_dump(mode="json"),
        })
        if not decision.approved:
            for r in decision.reasons:
                self._reason(r.split(":")[0].split("(")[0].strip())
            return None
        dl.approved += 1
        band = cfg.risk.entry_limit_slippage_pct / 100
        limit = decision.entry * (1 + band) if d == "long" else decision.entry * (1 - band)
        return Order(
            id=uuid.uuid4().hex[:12], ticker=t, direction=d, qty=decision.qty, trigger=decision.entry,
            limit=limit, stop=decision.stop, target=decision.target, placed_at=now,
            plan=plan.model_dump(mode="json"), feats=feats,
        )

    def _reason(self, key: str) -> None:
        self.decisions.reject_reasons[key] = self.decisions.reject_reasons.get(key, 0) + 1

    # ------------------------------------------------------------------
    def learn(self, library: LessonLibrary) -> int:
        """Save this run's finished trades to a lesson library file."""
        for lesson in self.lessons:
            library.add(lesson)
        return len(self.lessons)
