"""Order execution. DryRunBroker logs only; AlpacaBroker talks to Alpaca.

AlpacaBroker uses the paper endpoint unless the config says live AND the
TRADEBOT_ALLOW_LIVE confirmation is set (checked in config.assert_mode_allowed).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from .config import Config, assert_mode_allowed
from .models import RiskDecision
from .risk import AccountState

log = logging.getLogger(__name__)


@dataclass
class OpenPosition:
    symbol: str
    qty: float  # positive long, negative short
    avg_entry: float
    current_price: float


def trailed_stop(
    direction: str, entry: float, initial_stop: float, current_stop: float,
    price: float, trail_after_r: float, trail_distance_r: float,
) -> float | None:
    """New stop price if the trail should tighten, else None. Never widens."""
    r = abs(entry - initial_stop)
    if r <= 0:
        return None
    if direction == "long":
        if price - entry < trail_after_r * r:
            return None
        new = round(price - trail_distance_r * r, 2)
        return new if new > current_stop else None
    if entry - price < trail_after_r * r:
        return None
    new = round(price + trail_distance_r * r, 2)
    return new if new < current_stop else None


class Broker(Protocol):
    def account_state(self, trades_today: int) -> AccountState: ...
    def refresh_shortable(self, acct: AccountState, tickers: list[str]) -> None: ...
    def submit_bracket(self, d: RiskDecision) -> str | None: ...
    def positions(self) -> list[OpenPosition]: ...
    def current_stop(self, symbol: str) -> tuple[str, float] | None: ...
    def replace_stop(self, order_id: str, new_stop: float) -> None: ...
    def cancel_unfilled_entries(self) -> None: ...
    def flatten_all(self) -> None: ...
    def market_open(self) -> bool: ...


class DryRunBroker:
    """Plans and logs orders without sending anything anywhere."""

    def __init__(self, cfg: Config, equity: float = 100_000.0):
        self.cfg = cfg
        self.equity = equity
        self.orders: list[RiskDecision] = []

    def account_state(self, trades_today: int) -> AccountState:
        return AccountState(
            equity=self.equity, buying_power=self.equity * 2,
            start_of_day_equity=self.equity,
            open_symbols={o.ticker for o in self.orders}, trades_today=trades_today,
        )

    def refresh_shortable(self, acct: AccountState, tickers: list[str]) -> None:
        for t in tickers:
            acct.shortable.setdefault(t, True)

    def submit_bracket(self, d: RiskDecision) -> str | None:
        log.info("[dry-run] would %s %d %s @ %.2f stop %.2f target %.2f",
                 d.action, d.qty, d.ticker, d.entry, d.stop, d.target)
        self.orders.append(d)
        return f"dry-run-{len(self.orders)}"

    def positions(self) -> list[OpenPosition]:
        return []

    def current_stop(self, symbol: str):
        return None

    def replace_stop(self, order_id: str, new_stop: float) -> None:
        log.info("[dry-run] would move stop %s to %.2f", order_id, new_stop)

    def cancel_unfilled_entries(self) -> None:
        log.info("[dry-run] would cancel unfilled entry orders")

    def flatten_all(self) -> None:
        log.info("[dry-run] would cancel all orders and close all positions")
        self.orders.clear()

    def market_open(self) -> bool:
        return True


class AlpacaBroker:
    def __init__(self, api_key: str, secret_key: str, cfg: Config):
        from alpaca.trading.client import TradingClient

        assert_mode_allowed(cfg)
        self.cfg = cfg
        paper = cfg.trading.mode != "live"
        self.client = TradingClient(api_key, secret_key, paper=paper)
        log.info("Alpaca broker ready (%s)", "PAPER" if paper else "LIVE - REAL MONEY")

    def account_state(self, trades_today: int) -> AccountState:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        a = self.client.get_account()
        open_syms = {p.symbol for p in self.client.get_all_positions()}
        open_syms |= {
            o.symbol for o in self.client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))
        }
        return AccountState(
            equity=float(a.equity),
            buying_power=float(a.buying_power),
            start_of_day_equity=float(a.last_equity),
            open_symbols=open_syms,
            trades_today=trades_today,
        )

    def refresh_shortable(self, acct: AccountState, tickers: list[str]) -> None:
        for t in tickers:
            try:
                asset = self.client.get_asset(t)
                acct.shortable[t] = bool(asset.shortable and asset.easy_to_borrow)
            except Exception as e:
                log.warning("%s: asset lookup failed: %s", t, e)
                acct.shortable[t] = False

    def submit_bracket(self, d: RiskDecision) -> str | None:
        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
        from alpaca.trading.requests import StopLimitOrderRequest, StopLossRequest, TakeProfitRequest

        side = OrderSide.BUY if d.action == "BUY" else OrderSide.SELL
        slip = self.cfg.risk.entry_limit_slippage_pct / 100
        limit = d.entry * (1 + slip) if side == OrderSide.BUY else d.entry * (1 - slip)
        req = StopLimitOrderRequest(
            symbol=d.ticker,
            qty=d.qty,
            side=side,
            time_in_force=TimeInForce.DAY,
            order_class=OrderClass.BRACKET,
            stop_price=round(d.entry, 2),
            limit_price=round(limit, 2),
            take_profit=TakeProfitRequest(limit_price=round(d.target, 2)),
            stop_loss=StopLossRequest(stop_price=round(d.stop, 2)),
        )
        order = self.client.submit_order(req)
        log.info("submitted %s %d %s (order %s)", d.action, d.qty, d.ticker, order.id)
        return str(order.id)

    def positions(self) -> list[OpenPosition]:
        out = []
        for p in self.client.get_all_positions():
            qty = float(p.qty)
            if str(getattr(p.side, "value", p.side)).lower() == "short" and qty > 0:
                qty = -qty
            out.append(OpenPosition(p.symbol, qty, float(p.avg_entry_price), float(p.current_price)))
        return out

    def current_stop(self, symbol: str) -> tuple[str, float] | None:
        """The open stop-loss leg for a symbol: (order id, stop price)."""
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        orders = self.client.get_orders(
            GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol], nested=True)
        )
        for o in orders:
            for leg in [o, *(o.legs or [])]:
                otype = str(getattr(leg.order_type, "value", leg.order_type)).lower()
                status = str(getattr(leg.status, "value", leg.status)).lower()
                if otype == "stop" and leg.stop_price is not None and status not in ("filled", "canceled"):
                    return str(leg.id), float(leg.stop_price)
        return None

    def replace_stop(self, order_id: str, new_stop: float) -> None:
        from alpaca.trading.requests import ReplaceOrderRequest

        self.client.replace_order_by_id(order_id, ReplaceOrderRequest(stop_price=round(new_stop, 2)))
        log.info("moved stop %s to %.2f", order_id, new_stop)

    def cancel_unfilled_entries(self) -> None:
        """Cancel entry orders that never filled (no position yet)."""
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        held = {p.symbol for p in self.client.get_all_positions()}
        for o in self.client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN)):
            if o.symbol not in held:
                self.client.cancel_order_by_id(o.id)
                log.info("cancelled unfilled order for %s", o.symbol)

    def flatten_all(self) -> None:
        self.client.cancel_orders()
        self.client.close_all_positions(cancel_orders=True)
        log.info("flattened: all orders cancelled, all positions closed")

    def market_open(self) -> bool:
        return bool(self.client.get_clock().is_open)
