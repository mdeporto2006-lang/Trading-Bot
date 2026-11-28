"""Build the dashboard's view of the bot from the files the bot writes.

Nothing here talks to Alpaca or Claude. It reads:
  journal/trades.jsonl    every plan and risk-gate decision
  journal/outcomes.jsonl  replay scores of priced plans
  journal/status.json     account, positions, limits, activity (runner)
  journal/events.jsonl    event log (runner)
  tuning/                 active settings version, history, proposals
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from ..checklist import evaluate
from ..clock import to_et
from ..config import Config
from ..improve.tuning import TUNABLE, TuningError, TuningStore, get_param
from ..models import MarketSnapshot
from ..telemetry import read_status, tail_events

STALE_AFTER_SECONDS = 120
MAX_NEWS = 3


class _FileCache:
    """Re-parse a file only when its size or mtime changes."""

    def __init__(self):
        self._c: dict[str, tuple[tuple[int, int], Any]] = {}

    def get(self, path: Path, parse):
        try:
            st = path.stat()
        except FileNotFoundError:
            return parse(None)
        key = (st.st_size, st.st_mtime_ns)
        hit = self._c.get(str(path))
        if hit and hit[0] == key:
            return hit[1]
        val = parse(path)
        self._c[str(path)] = (key, val)
        return val


def _jsonl(path: Path | None) -> list[dict]:
    if path is None:
        return []
    rows = []
    with path.open() as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _trim_snapshot(snap: dict) -> dict:
    out = {k: v for k, v in snap.items() if k != "news"}
    out["news"] = [
        {"headline": (n.get("headline") or "")[:200], "age_hours": n.get("age_hours"),
         "source": n.get("source", "")}
        for n in (snap.get("news") or [])[:MAX_NEWS]
    ]
    return out


def code_checklist(rec: dict, cfg: Config) -> tuple[list[dict], str]:
    """Code's own checklist for a record. Uses the gate's result when it ran;
    for NO_TRADE plans (where the gate stops early) it recomputes the six
    checks for display, assuming the direction the price/VWAP side suggests."""
    dec = rec.get("decision") or {}
    if dec.get("checklist"):
        return dec["checklist"], "gate"
    try:
        snap = MarketSnapshot.model_validate(rec["snapshot"])
    except Exception:
        return [], "none"
    plan = rec.get("plan") or {}
    if plan.get("action") == "SHORT":
        direction = "short"
    elif plan.get("action") == "BUY":
        direction = "long"
    else:
        direction = "short" if (snap.last_price and snap.vwap and snap.last_price < snap.vwap) else "long"
    try:
        items = evaluate(snap, direction, plan.get("entry_trigger"), plan.get("stop"), plan.get("target"), cfg)
    except Exception:
        return [], "none"
    return [i.model_dump() for i in items], f"display ({direction})"


class DashboardState:
    def __init__(self, config_path: str | None):
        self.config_path = config_path
        self.cache = _FileCache()

    def config(self) -> tuple[Config, TuningStore]:
        base = Config.load(self.config_path)
        store = TuningStore(base.improvement.dir)
        try:
            cfg = store.layer(base)
        except TuningError:
            cfg = base
        return cfg, store

    def build(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        cfg, store = self.config()
        jpath = Path(cfg.trading.journal_path)
        jdir = jpath.parent

        records = self.cache.get(jpath, _jsonl)
        outcomes_raw = self.cache.get(jpath.with_name("outcomes.jsonl"), _jsonl)

        # Records from the latest ET trading day in the journal.
        day = None
        if records:
            day = to_et(datetime.fromisoformat(records[-1]["ts"])).date()
        todays = [r for r in records[-3000:] if to_et(datetime.fromisoformat(r["ts"])).date() == day]
        scans = []
        for r in todays[-400:]:
            checklist, source = code_checklist(r, cfg)
            scans.append({
                "id": r.get("id"),
                "ts": r["ts"],
                "ticker": r.get("ticker"),
                "snapshot": _trim_snapshot(r.get("snapshot") or {}),
                "plan": r.get("plan"),
                "decision": r.get("decision"),
                "code_checklist": checklist,
                "code_checklist_source": source,
                "order_id": r.get("order_id"),
            })

        cutoff = now - timedelta(days=cfg.improvement.lookback_days)
        outcomes = [
            {k: v for k, v in o.items() if k != "bars"}
            for o in outcomes_raw
            if datetime.fromisoformat(o["ts"]) >= cutoff
        ]

        status = read_status(jdir)
        stale = True
        if status and status.get("updated_at"):
            age = (now - datetime.fromisoformat(status["updated_at"])).total_seconds()
            stale = age > STALE_AFTER_SECONDS
            status["age_seconds"] = round(age)

        active = store.active()
        history = []
        if store.history.exists():
            for p in sorted(store.history.glob("v*.yaml")):
                try:
                    doc = yaml.safe_load(p.read_text()) or {}
                except yaml.YAMLError:
                    continue
                history.append({k: doc.get(k) for k in ("version", "adopted_at", "note", "changes", "lessons")})
        proposals = store.list_proposals()[-20:]

        watchlist = (status or {}).get("watchlist") or cfg.trading.watchlist
        backtest = self._backtest(cfg)
        tm = self.cache.get(jdir / "timemachine.jsonl", _jsonl)[-40:]
        lessons = self.cache.get(Path(cfg.backtest.library_path), _jsonl)
        return {
            "demo": False,
            "server_time": now.isoformat(),
            "trading_day": day.isoformat() if day else None,
            "stale": stale,
            "status": status,
            "watchlist": [t.upper() for t in watchlist],
            "config": {
                "mode": cfg.trading.mode,
                "setup": cfg.setup.model_dump(),
                "exits": cfg.exits.model_dump(),
                "session": cfg.session.model_dump(),
                "risk": cfg.risk.model_dump(),
                "model": cfg.planner.model,
                "lessons": cfg.improvement.lessons,
                "auto_apply": cfg.improvement.auto_apply,
            },
            "tunable": {p: {"current": get_param(cfg, p), "bounds": list(b)} for p, b in TUNABLE.items()},
            "scans": scans,
            "events": tail_events(jdir, 200),
            "outcomes": outcomes,
            "tuning": {"active": active, "history": history},
            "proposals": proposals,
            "backtest": backtest,
            "timemachine": tm,
            "library": {
                "count": len(lessons),
                "wins": sum(1 for l in lessons if (l.get("r_multiple") or 0) > 0),
                "avg_r": round(sum(l.get("r_multiple") or 0 for l in lessons) / len(lessons), 3) if lessons else None,
            },
        }

    def _backtest(self, cfg: Config) -> dict | None:
        """The latest backtest run (backtests/latest.json), trimmed for the page."""
        doc = self.cache.get(Path(cfg.backtest.results_dir) / "latest.json",
                             lambda p: json.loads(p.read_text()) if p else None)
        if not doc:
            return None
        keep = ("id", "created_at", "tickers", "start", "end", "source", "planner", "catalyst",
                "start_equity", "warnings", "metrics", "decisions", "equity_curve")
        out = {k: doc.get(k) for k in keep}
        out["trades"] = (doc.get("trades") or [])[-300:]
        return out
