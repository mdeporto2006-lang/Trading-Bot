"""The trading loop: scan -> plan -> risk gate -> execute, then manage exits."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Callable

from .broker import Broker, trailed_stop
from .clock import in_entry_window, past_flatten_time, parse_hhmm, to_et
from .config import Config
from .journal import Journal
from .models import RiskDecision, TradePlan
from .planner import ClaudePlanner
from .risk import evaluate_plan

log = logging.getLogger(__name__)


class Runner:
    def __init__(
        self,
        cfg: Config,
        data,  # anything with .snapshot(ticker, now) -> MarketSnapshot
        planner: ClaudePlanner,
        broker: Broker,
        journal: Journal,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.cfg, self.data, self.planner, self.broker, self.journal = cfg, data, planner, broker, journal
        self.now = clock

    # ---- entries -------------------------------------------------------
    def scan_once(
        self, tickers: list[str], place_orders: bool = True
    ) -> list[tuple[TradePlan, RiskDecision]]:
        now = self.now()
        acct = self.broker.account_state(self.journal.trades_on(now))
        self.broker.refresh_shortable(acct, tickers)
        trades = self.journal.open_trades()
        results = []
        for t in tickers:
            t = t.upper()
            if t in acct.open_symbols:
                log.info("%s: already held or ordered, skipping", t)
                continue
            snap = self.data.snapshot(t, now)
            plan = self.planner.plan(snap)
            decision = evaluate_plan(plan, snap, self.cfg, acct, now)
            order_id = None
            if decision.approved and place_orders:
                try:
                    order_id = self.broker.submit_bracket(decision)
                except Exception as e:
                    log.exception("%s: order failed", t)
                    decision.reasons.append(f"order failed: {e}")
                if order_id:
                    acct.open_symbols.add(t)
                    acct.trades_today += 1
                    trades[t] = {
                        "direction": "long" if decision.action == "BUY" else "short",
                        "entry": decision.entry,
                        "initial_stop": decision.stop,
                        "target": decision.target,
                        "order_id": order_id,
                    }
            self.journal.record(
                now, ticker=t, snapshot=snap.model_dump(mode="json"),
                plan=plan.model_dump(mode="json"), decision=decision.model_dump(mode="json"),
                order_id=order_id,
            )
            log.info("%s: %s%s", t, decision.action if decision.approved else "NO_TRADE",
                     "" if decision.approved else f" ({'; '.join(decision.reasons)})")
            results.append((plan, decision))
        self.journal.save_open_trades(trades)
        return results

    # ---- exits ---------------------------------------------------------
    def manage_once(self) -> bool:
        """Trail stops, cancel stale entries, flatten at the cutoff.
        Returns True once the day is flattened."""
        now = self.now()
        if past_flatten_time(now, self.cfg.exits.flatten_time_et):
            self.broker.flatten_all()
            self.journal.save_open_trades({})
            return True
        if to_et(now).time() >= parse_hhmm(self.cfg.session.no_entries_after_et):
            self.broker.cancel_unfilled_entries()

        trades = self.journal.open_trades()
        ex = self.cfg.exits
        for pos in self.broker.positions():
            t = trades.get(pos.symbol)
            if not t:
                continue  # not opened by this bot; leave it alone
            cur = self.broker.current_stop(pos.symbol)
            if not cur:
                continue
            order_id, stop = cur
            new = trailed_stop(
                t["direction"], t["entry"], t["initial_stop"], stop,
                pos.current_price, ex.trail_after_r, ex.trail_distance_r,
            )
            if new is not None:
                try:
                    self.broker.replace_stop(order_id, new)
                except Exception:
                    log.exception("%s: could not move stop", pos.symbol)
        held = {p.symbol for p in self.broker.positions()}
        # Forget trades that are closed (no position) once entries are no longer pending.
        if to_et(now).time() >= parse_hhmm(self.cfg.session.no_entries_after_et):
            self.journal.save_open_trades({k: v for k, v in trades.items() if k in held})
        return False

    # ---- loop ----------------------------------------------------------
    def run(self, tickers: list[str]) -> None:
        s = self.cfg.session
        last_scan = 0.0
        log.info("runner started for %s in %s mode", ", ".join(tickers), self.cfg.trading.mode)
        while True:
            try:
                if not self.broker.market_open():
                    log.info("market closed; sleeping")
                    time.sleep(300)
                    continue
                if self.manage_once():
                    log.info("flat for the day; stopping")
                    return
                now = self.now()
                if (
                    in_entry_window(now, s.no_entries_before_et, s.no_entries_after_et)
                    and time.monotonic() - last_scan >= s.scan_interval_seconds
                ):
                    self.scan_once(tickers)
                    last_scan = time.monotonic()
            except KeyboardInterrupt:
                raise
            except Exception:
                log.exception("loop error; continuing")
            time.sleep(s.manage_interval_seconds)
