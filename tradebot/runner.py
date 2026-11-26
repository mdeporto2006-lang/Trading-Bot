"""The trading loop: scan -> plan -> risk gate -> execute, then manage exits."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Callable

from .broker import Broker, trailed_stop
from .clock import in_entry_window, is_regular_hours, past_flatten_time, parse_hhmm, to_et
from .config import Config
from .journal import Journal
from .models import RiskDecision, TradePlan
from .planner import ClaudePlanner
from .risk import AccountState, evaluate_plan
from .telemetry import Telemetry

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
        on_close: Callable[[], None] | None = None,
    ):
        self.cfg, self.data, self.planner, self.broker, self.journal = cfg, data, planner, broker, journal
        self.now = clock
        self.on_close = on_close  # runs once after the day is flattened
        self.telemetry = Telemetry(journal.path.parent)
        self._cancel_logged_on = None
        self._last_scan_at: datetime | None = None

    # ---- dashboard telemetry (best effort, never raises) ---------------
    def _phase(self, now: datetime) -> str:
        s = self.cfg.session
        et = to_et(now)
        if past_flatten_time(now, self.cfg.exits.flatten_time_et) and is_regular_hours(now):
            return "flattened"
        if not is_regular_hours(now):
            return "closed"
        if et.time() < parse_hhmm(s.no_entries_before_et):
            return "warmup"
        if et.time() >= parse_hhmm(s.no_entries_after_et):
            return "managing_only"
        return "entry_window"

    def _publish(self, now: datetime, acct: AccountState | None = None, positions: list | None = None,
                 stops: dict[str, float] | None = None, **extra) -> None:
        try:
            cfg = self.cfg
            fields: dict = {
                "mode": cfg.trading.mode,
                "phase": self._phase(now),
                "scan_interval_seconds": cfg.session.scan_interval_seconds,
                "last_scan_at": self._last_scan_at.isoformat() if self._last_scan_at else None,
                **extra,
            }
            if acct is None:
                acct = self.broker.account_state(self.journal.trades_on(now))
            fields["account"] = {
                "equity": acct.equity,
                "buying_power": acct.buying_power,
                "start_of_day_equity": acct.start_of_day_equity,
                "day_pnl": acct.equity - acct.start_of_day_equity,
                "day_pnl_pct": acct.day_pnl_pct,
            }
            if positions is None:
                positions = self.broker.positions()
            trades = self.journal.open_trades()
            stops = stops or {}
            rows = []
            for p in positions:
                t = trades.get(p.symbol, {})
                entry = t.get("entry") or p.avg_entry
                init = t.get("initial_stop")
                stop = stops.get(p.symbol, init)
                long_ = p.qty > 0
                r = abs(entry - init) if init else None
                move = (p.current_price - p.avg_entry) if long_ else (p.avg_entry - p.current_price)
                rows.append({
                    "symbol": p.symbol,
                    "direction": "long" if long_ else "short",
                    "qty": p.qty,
                    "avg_entry": p.avg_entry,
                    "current_price": p.current_price,
                    "entry": entry,
                    "initial_stop": init,
                    "stop": stop,
                    "target": t.get("target"),
                    "unrealized_pnl": move * abs(p.qty),
                    "r_multiple": (move / r) if r else None,
                    "trailing": bool(init is not None and stop is not None and stop != init),
                    "managed": bool(t),
                })
            held = {p.symbol for p in positions}
            fields["positions"] = rows
            fields["pending_entries"] = [
                {"symbol": k, **v} for k, v in trades.items() if k not in held
            ]
            fields["limits"] = {
                "trades_today": acct.trades_today,
                "max_trades_per_day": cfg.risk.max_trades_per_day,
                "open_positions": len(acct.open_symbols | held),
                "max_open_positions": cfg.risk.max_open_positions,
                "daily_loss_limit_pct": cfg.risk.daily_loss_limit_pct,
                "risk_per_trade_pct": cfg.risk.risk_per_trade_pct,
                "max_position_pct": cfg.risk.max_position_pct,
                "open_risk": sum(
                    abs(r["qty"]) * abs(r["current_price"] - r["stop"])
                    for r in rows if r["stop"] is not None
                    and ((r["direction"] == "long" and r["current_price"] > r["stop"])
                         or (r["direction"] == "short" and r["current_price"] < r["stop"]))
                ),
                "exposure": sum(abs(r["qty"]) * r["current_price"] for r in rows),
            }
            self.telemetry.update(now, **fields)
        except Exception:
            log.exception("could not publish status")

    # ---- entries -------------------------------------------------------
    def scan_once(
        self, tickers: list[str], place_orders: bool = True
    ) -> list[tuple[TradePlan, RiskDecision]]:
        now = self.now()
        acct = self.broker.account_state(self.journal.trades_on(now))
        self.broker.refresh_shortable(acct, tickers)
        trades = self.journal.open_trades()
        results = []
        tel = self.telemetry
        tel.event(now, "scan", f"Scan started: {', '.join(x.upper() for x in tickers)}",
                  tickers=[x.upper() for x in tickers])
        for t in tickers:
            t = t.upper()
            if t in acct.open_symbols:
                log.info("%s: already held or ordered, skipping", t)
                tel.event(now, "skip", f"{t}: already held or ordered", ticker=t)
                continue
            tel.update(now, activity={"kind": "planning", "ticker": t, "since": now.isoformat()})
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
                    tel.event(now, "error", f"{t}: order failed: {type(e).__name__}", level="error", ticker=t)
                if order_id:
                    tel.event(
                        now, "order",
                        f"{t}: {decision.action} {decision.qty} @ {decision.entry} stop {decision.stop} target {decision.target}",
                        ticker=t, action=decision.action, qty=decision.qty, entry=decision.entry,
                        stop=decision.stop, target=decision.target, order_id=order_id,
                    )
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
            tel.event(
                now, "decision",
                f"{t}: {decision.action if decision.approved else 'NO_TRADE'}"
                + ("" if decision.approved else f" ({'; '.join(decision.reasons)[:160]})"),
                ticker=t, approved=decision.approved, planner_action=plan.action,
                confidence=plan.confidence,
            )
            results.append((plan, decision))
        self.journal.save_open_trades(trades)
        if place_orders:
            self._last_scan_at = now
            self._publish(now, acct=acct, activity={"kind": "idle", "since": now.isoformat()})
        return results

    # ---- exits ---------------------------------------------------------
    def manage_once(self) -> bool:
        """Trail stops, cancel stale entries, flatten at the cutoff.
        Returns True once the day is flattened."""
        now = self.now()
        if past_flatten_time(now, self.cfg.exits.flatten_time_et):
            self.broker.flatten_all()
            self.journal.save_open_trades({})
            self.telemetry.event(now, "flatten", f"Flattened everything at {self.cfg.exits.flatten_time_et} ET")
            self._publish(now, positions=[], activity={"kind": "flattened", "since": now.isoformat()})
            return True
        if to_et(now).time() >= parse_hhmm(self.cfg.session.no_entries_after_et):
            self.broker.cancel_unfilled_entries()
            if self._cancel_logged_on != to_et(now).date():
                self._cancel_logged_on = to_et(now).date()
                self.telemetry.event(now, "cancel", "Entry window closed: unfilled entries cancelled")

        trades = self.journal.open_trades()
        ex = self.cfg.exits
        positions = self.broker.positions()
        stops: dict[str, float] = {}
        for pos in positions:
            t = trades.get(pos.symbol)
            if not t:
                continue  # not opened by this bot; leave it alone
            cur = self.broker.current_stop(pos.symbol)
            if not cur:
                continue
            order_id, stop = cur
            stops[pos.symbol] = stop
            new = trailed_stop(
                t["direction"], t["entry"], t["initial_stop"], stop,
                pos.current_price, ex.trail_after_r, ex.trail_distance_r,
            )
            if new is not None:
                try:
                    self.broker.replace_stop(order_id, new)
                    stops[pos.symbol] = new
                    self.telemetry.event(
                        now, "trail", f"{pos.symbol}: stop trailed {stop:.2f} -> {new:.2f} (price {pos.current_price:.2f})",
                        ticker=pos.symbol, old=stop, new=new, price=pos.current_price,
                    )
                except Exception:
                    log.exception("%s: could not move stop", pos.symbol)
                    self.telemetry.event(now, "error", f"{pos.symbol}: could not move stop", level="error",
                                         ticker=pos.symbol)
        self._publish(now, positions=positions, stops=stops)
        held = {p.symbol for p in positions}
        # Forget trades that are closed (no position) once entries are no longer pending.
        if to_et(now).time() >= parse_hhmm(self.cfg.session.no_entries_after_et):
            self.journal.save_open_trades({k: v for k, v in trades.items() if k in held})
        return False

    # ---- loop ----------------------------------------------------------
    def run(self, tickers: list[str]) -> None:
        s = self.cfg.session
        last_scan = 0.0
        log.info("runner started for %s in %s mode", ", ".join(tickers), self.cfg.trading.mode)
        self.telemetry.event(self.now(), "start", f"Runner started in {self.cfg.trading.mode} mode",
                             tickers=tickers)
        self.telemetry.update(self.now(), watchlist=tickers, started_at=self.now().isoformat())
        while True:
            try:
                if not self.broker.market_open():
                    log.info("market closed; sleeping")
                    self._publish(self.now(), activity={"kind": "market_closed", "since": self.now().isoformat()})
                    time.sleep(300)
                    continue
                if self.manage_once():
                    log.info("flat for the day")
                    if self.on_close:
                        try:
                            self.on_close()
                        except Exception:
                            log.exception("end-of-day review failed")
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
            except Exception as e:
                log.exception("loop error; continuing")
                self.telemetry.event(self.now(), "error", f"Loop error: {type(e).__name__}: {str(e)[:200]}",
                                     level="error")
            time.sleep(s.manage_interval_seconds)
