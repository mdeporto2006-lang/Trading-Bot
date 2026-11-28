"""Command line entry point.

  python -m tradebot plan AAPL NVDA     # plan + risk-check, print JSON, no orders
  python -m tradebot run                 # trading loop for the configured watchlist
  python -m tradebot flatten             # cancel all orders, close all positions

Self-improvement:
  python -m tradebot score               # replay finished plans to score them
  python -m tradebot review              # Claude drafts a change; code replay-tests it
  python -m tradebot proposals           # list proposals and their test results
  python -m tradebot approve <id>        # adopt a passed proposal
  python -m tradebot rollback [--to N]   # go back to an earlier settings version

Dashboard:
  python -m tradebot dashboard           # live dashboard at http://127.0.0.1:8765
  python -m tradebot dashboard --demo    # simulated data, no keys needed
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone

from .broker import AlpacaBroker, DryRunBroker
from .config import Config, LiveTradingNotAllowed, assert_mode_allowed
from .journal import Journal
from .market_data import AlpacaMarketData
from .planner import ClaudePlanner
from .improve.outcomes import score_journal
from .improve.reviewer import approve, run_review
from .improve.tuning import TuningError, TuningStore
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
    sub.add_parser("score", help="score finished plans by replaying them on later bars")
    sub.add_parser("review", help="have Claude propose a settings change and replay-test it")
    sub.add_parser("proposals", help="list proposals")
    p_ap = sub.add_parser("approve", help="adopt a proposal that passed")
    p_ap.add_argument("proposal_id")
    p_rb = sub.add_parser("rollback", help="return to an earlier settings version")
    p_rb.add_argument("--to", type=int, default=None, help="version number (0 = config.yaml only)")
    p_db = sub.add_parser("dashboard", help="serve the live dashboard (reads the journal; needs no API keys)")
    p_db.add_argument("--host", default="127.0.0.1",
                      help="interface to listen on; use 0.0.0.0 to open it from your phone on the same Wi-Fi")
    p_db.add_argument("--port", type=int, default=8765)
    p_db.add_argument("--demo", action="store_true", help="show simulated data instead of the bot's files")
    p_db.add_argument("--token", default=None,
                      help="access token (default: $TRADEBOT_DASHBOARD_TOKEN or a new random one)")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.cmd == "dashboard":
        from .dashboard.server import serve

        serve(args.config, host=args.host, port=args.port, demo=args.demo,
              token=args.token or os.environ.get("TRADEBOT_DASHBOARD_TOKEN"))
        return
    base_cfg = Config.load(args.config)
    store = TuningStore(base_cfg.improvement.dir)
    try:
        cfg = store.layer(base_cfg)  # adopted self-improvements on top of config.yaml
    except TuningError as e:
        sys.exit(f"tuning/active.yaml is invalid ({e}); fix it or run `rollback --to 0`")
    try:
        assert_mode_allowed(cfg)
    except LiveTradingNotAllowed as e:
        sys.exit(str(e))

    journal = Journal(cfg.trading.journal_path)
    if args.cmd == "proposals":
        for p in store.list_proposals():
            print(f"{p['id']}  {p['status']:<13} {p.get('changes', {})}  {p.get('verdict', '')}")
        print(f"active settings version: v{store.active().get('version', 0)}")
        return
    if args.cmd == "approve":
        try:
            print(f"adopted as v{approve(store, args.proposal_id)}")
        except TuningError as e:
            sys.exit(str(e))
        return
    if args.cmd == "rollback":
        try:
            print(f"now on v{store.rollback(args.to)}")
        except TuningError as e:
            sys.exit(str(e))
        return

    key, secret = _env("ALPACA_API_KEY"), _env("ALPACA_SECRET_KEY")
    data = AlpacaMarketData(key, secret, cfg)

    if args.cmd == "score":
        print(f"scored {score_journal(journal, data, cfg, datetime.now(timezone.utc))} plans")
        return
    if args.cmd == "review":
        _env("ANTHROPIC_API_KEY")
        score_journal(journal, data, cfg, datetime.now(timezone.utc))
        print(json.dumps(run_review(cfg, journal, store), indent=2))
        return

    if args.cmd == "plan" or cfg.trading.mode == "dry_run":
        broker = DryRunBroker(cfg)
    else:
        broker = AlpacaBroker(key, secret, cfg)

    if args.cmd == "flatten":
        broker.flatten_all()
        return

    _env("ANTHROPIC_API_KEY")
    def end_of_day() -> None:
        n = score_journal(journal, data, cfg, datetime.now(timezone.utc))
        logging.info("scored %d plans", n)
        now = datetime.now(timezone.utc)
        runner.telemetry.event(now, "score", f"Scored {n} plans by replaying them on later bars")
        if cfg.improvement.review_after_close:
            p = run_review(cfg, journal, store)
            logging.info("review %s: %s (%s)", p["id"], p["status"], p.get("verdict", ""))
            runner.telemetry.event(
                datetime.now(timezone.utc), "review",
                f"Self-review {p['id']}: {p['status']} ({p.get('verdict', '')})"[:240],
                proposal_id=p["id"], status=p["status"],
            )

    runner = Runner(cfg, data, ClaudePlanner(cfg), broker, journal, on_close=end_of_day)

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
