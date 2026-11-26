"""Command line entry point.

  python -m tradebot plan AAPL NVDA     # plan + risk-check, print JSON, no orders
  python -m tradebot run                 # trading loop for the configured watchlist
  python -m tradebot flatten             # cancel all orders, close all positions
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from .broker import AlpacaBroker, DryRunBroker
from .config import Config, LiveTradingNotAllowed, assert_mode_allowed
from .journal import Journal
from .market_data import AlpacaMarketData
from .planner import ClaudePlanner
from .runner import Runner


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        sys.exit(f"Missing environment variable {name}. See .env.example.")
    return value


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="tradebot")
    ap.add_argument("--config", default=None, help="path to config.yaml")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_plan = sub.add_parser("plan", help="plan tickers once and print JSON; never places orders")
    p_plan.add_argument("tickers", nargs="+")
    p_run = sub.add_parser("run", help="run the trading loop")
    p_run.add_argument("tickers", nargs="*", help="defaults to trading.watchlist")
    sub.add_parser("flatten", help="cancel all orders and close all positions")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = Config.load(args.config)
    try:
        assert_mode_allowed(cfg)
    except LiveTradingNotAllowed as e:
        sys.exit(str(e))

    key, secret = _env("ALPACA_API_KEY"), _env("ALPACA_SECRET_KEY")
    data = AlpacaMarketData(key, secret, cfg)
    journal = Journal(cfg.trading.journal_path)

    if args.cmd == "plan" or cfg.trading.mode == "dry_run":
        broker = DryRunBroker(cfg)
    else:
        broker = AlpacaBroker(key, secret, cfg)

    if args.cmd == "flatten":
        broker.flatten_all()
        return

    _env("ANTHROPIC_API_KEY")
    runner = Runner(cfg, data, ClaudePlanner(cfg), broker, journal)

    if args.cmd == "plan":
        for plan, decision in runner.scan_once(args.tickers, place_orders=False):
            out = {"plan": plan.model_dump(mode="json"), "risk_gate": decision.model_dump(mode="json")}
            print(json.dumps(out, indent=2))
        return

    tickers = [t.upper() for t in (args.tickers or cfg.trading.watchlist)]
    if not tickers:
        sys.exit("No tickers given and trading.watchlist is empty.")
    runner.run(tickers)


if __name__ == "__main__":
    main()
