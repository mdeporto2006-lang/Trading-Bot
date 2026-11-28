"""Score every priced plan in the journal by replaying it on the bars that
followed, whether or not it was traded. Runs after the close."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Protocol

from ..checklist import effective_target
from ..clock import parse_hhmm, to_et
from ..config import Config
from ..indicators import Bar
from ..journal import Journal
from ..models import MarketSnapshot
from ..simulate import simulate_bracket

log = logging.getLogger(__name__)


class BarSource(Protocol):
    def minute_bars(self, ticker: str, start: datetime, end: datetime) -> list[Bar]: ...


def score_journal(journal: Journal, bars: BarSource, cfg: Config, now: datetime) -> int:
    """Add outcomes for priced plans whose trading day is over. Returns how
    many new outcomes were recorded."""
    done = journal.outcomes()
    flatten_t = parse_hhmm(cfg.exits.flatten_time_et)
    added = 0
    for rec in journal.records():
        rid = rec.get("id")
        plan = rec.get("plan") or {}
        if not rid or rid in done or plan.get("action") not in ("BUY", "SHORT"):
            continue
        entry, stop, target = plan.get("entry_trigger"), plan.get("stop"), plan.get("target")
        if None in (entry, stop, target):
            continue
        ts = to_et(datetime.fromisoformat(rec["ts"]))
        day_end = ts.replace(hour=flatten_t.hour, minute=flatten_t.minute, second=0, microsecond=0)
        if to_et(now) < day_end:
            continue  # that session isn't finished yet
        direction = "long" if plan["action"] == "BUY" else "short"
        snap = MarketSnapshot.model_validate(rec["snapshot"])
        target = effective_target(snap, direction, entry, target)
        try:
            path = bars.minute_bars(rec["ticker"], ts, day_end)
        except Exception as e:
            log.warning("%s: could not fetch bars for scoring: %s", rec["ticker"], e)
            continue
        if not path:
            continue
        sim = simulate_bracket(
            direction, entry, stop, target, path, cfg.exits.trail_after_r, cfg.exits.trail_distance_r
        )
        journal.record_outcome(
            {
                "record_id": rid,
                "ticker": rec["ticker"],
                "ts": rec["ts"],
                "direction": direction,
                "entry": entry,
                "stop": stop,
                "target": target,
                "approved": bool((rec.get("decision") or {}).get("approved")),
                "traded": bool(rec.get("order_id")),
                "triggered": sim.triggered,
                "exit_reason": sim.exit_reason,
                "r_multiple": sim.r_multiple,
                "bars": [[b.open, b.high, b.low, b.close] for b in path],
            }
        )
        added += 1
    return added
