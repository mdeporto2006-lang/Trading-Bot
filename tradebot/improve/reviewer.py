"""Post-trade review: Claude studies scored plans and proposes changes.

A proposal is only data until code has (1) checked it against the TUNABLE
allowlist and bounds, (2) replayed it against past plans, and (3) the owner has
approved it (unless improvement.auto_apply is on, and then only if it passed).
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field

from ..config import Config
from ..journal import Journal
from ..models import MarketSnapshot
from .replay import passes, replay
from .tuning import TUNABLE, TuningError, TuningStore, apply_overrides, get_param

log = logging.getLogger(__name__)


class ParamChange(BaseModel):
    param: str = Field(description="dotted setting name from the allowed list")
    value: float
    rationale: str


class Proposal(BaseModel):
    summary: str = Field(description="what the results show, in 2-4 sentences")
    changes: list[ParamChange] = Field(description="setting changes; empty if none are justified")
    lessons: list[str] = Field(
        description="the full new list of lessons for the planner (keep, edit or drop existing ones)"
    )


def recent(journal: Journal, now: datetime, days: int) -> tuple[list[dict], dict[str, dict]]:
    cutoff = now - timedelta(days=days)
    recs = [r for r in journal.records() if datetime.fromisoformat(r["ts"]) >= cutoff]
    return recs, journal.outcomes()


def trade_table(records: list[dict], outcomes: dict[str, dict], limit: int = 300) -> list[dict]:
    """Compact, numbers-only rows: one per scored plan. No news text, so
    nothing from outside can steer the review."""
    rows = []
    for rec in records:
        out = outcomes.get(rec.get("id", ""))
        if not out:
            continue
        snap = MarketSnapshot.model_validate(rec["snapshot"])
        plan = rec["plan"]
        entry, stop = out["entry"], out["stop"]
        rows.append(
            {
                "ts": rec["ts"],
                "ticker": rec["ticker"],
                "dir": out["direction"],
                "approved": out["approved"],
                "confidence": plan.get("confidence"),
                "rel_vol": snap.relative_volume and round(snap.relative_volume, 2),
                "spread_pct": snap.spread_pct and round(snap.spread_pct, 4),
                "avg_vol": snap.avg_daily_volume and round(snap.avg_daily_volume),
                "catalyst_age_h": snap.news[0].age_hours if snap.news else None,
                "stop_atr": snap.atr and round(abs(entry - stop) / snap.atr, 2),
                "planned_rr": plan.get("rr_ratio"),
                "exit": out["exit_reason"],
                "r": out["r_multiple"],
            }
        )
    return rows[-limit:]


def review_prompt(cfg: Config, baseline: dict, rows: list[dict]) -> str:
    current = {p: get_param(cfg, p) for p in TUNABLE}
    bounds = {p: list(b) for p, b in TUNABLE.items()}
    return f"""You review a day-trading bot's recent plans and propose careful improvements.

Each row is one plan Claude priced, scored by replaying it on the bars that followed
(r = result in R multiples; exit = stop/target/flatten/not_triggered). "approved"
means the rules let it trade.

Baseline replay with current settings: {json.dumps(baseline)}

Current settings: {json.dumps(current)}
Allowed settings and [min, max] bounds (anything else is rejected): {json.dumps(bounds)}
Current lessons: {json.dumps(cfg.improvement.lessons)}

Rules for your proposal:
- Only propose a change the data supports with a clear pattern across many rows. Small samples are noise; say so and propose nothing.
- Prefer one or two changes over many. Every change will be replay-tested and must beat the baseline by {cfg.improvement.min_expectancy_gain_r}R per trade over at least {cfg.improvement.min_trades} trades.
- Lessons are short, concrete observations (max 300 characters each, max 10) that help the planner judge setups. They can never relax the checklist. Return the full list you want, including existing lessons to keep.

<plans>
{json.dumps(rows)}
</plans>"""


def run_review(
    cfg: Config, journal: Journal, store: TuningStore, now: datetime | None = None,
    client: Any | None = None,
) -> dict:
    """Draft, validate and replay a proposal. Saves it and returns it."""
    now = now or datetime.now(timezone.utc)
    records, outcomes = recent(journal, now, cfg.improvement.lookback_days)
    base = replay(cfg, records, outcomes)
    rows = trade_table(records, outcomes)
    proposal: dict[str, Any] = {
        "id": now.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6],
        "created_at": now.isoformat(),
        "based_on_version": store.active().get("version", 0),
        "baseline": base.as_dict(),
        "status": "no_data",
    }
    if not rows:
        proposal["summary"] = "No scored plans yet."
        store.save_proposal(proposal)
        return proposal

    if client is None:
        import anthropic

        client = anthropic.Anthropic()
    imp = cfg.improvement
    try:
        resp = client.beta.messages.parse(
            model=imp.reviewer_model,
            max_tokens=16000,
            messages=[{"role": "user", "content": review_prompt(cfg, base.as_dict(), rows)}],
            output_config={"effort": imp.reviewer_effort},
            output_format=Proposal,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        draft: Optional[Proposal] = resp.parsed_output if resp.stop_reason not in ("refusal", "max_tokens") else None
    except Exception as e:
        log.exception("review call failed")
        draft = None
        proposal["error"] = f"{type(e).__name__}"
    if draft is None:
        proposal["status"] = "error"
        store.save_proposal(proposal)
        return proposal

    changes = {c.param: c.value for c in draft.changes}
    proposal.update(
        summary=draft.summary,
        changes=changes,
        rationales={c.param: c.rationale for c in draft.changes},
        lessons=draft.lessons,
    )
    try:
        candidate_cfg = apply_overrides(cfg, changes, draft.lessons)
    except TuningError as e:
        proposal.update(status="rejected", verdict=str(e))
        store.save_proposal(proposal)
        return proposal

    if changes:
        cand = replay(candidate_cfg, records, outcomes)
        ok, why = passes(base, cand, cfg)
        proposal.update(candidate=cand.as_dict(), verdict=why, status="passed" if ok else "failed")
    elif draft.lessons != cfg.improvement.lessons:
        proposal.update(status="lessons_only", verdict="lessons can't be replay-tested; owner approval required")
    else:
        proposal.update(status="no_change", verdict="no change proposed")

    if proposal["status"] == "passed" and imp.auto_apply:
        v = store.adopt(changes, draft.lessons, note=f"auto-applied proposal {proposal['id']}")
        proposal.update(status="adopted", adopted_version=v)
    store.save_proposal(proposal)
    return proposal


def approve(store: TuningStore, pid: str) -> int:
    """Owner approval. Failed or rejected proposals can't be adopted."""
    p = store.load_proposal(pid)
    if p["status"] not in ("passed", "lessons_only"):
        raise TuningError(f"proposal {pid} is {p['status']}; only passed or lessons_only can be adopted")
    if p.get("based_on_version", 0) != store.active().get("version", 0):
        raise TuningError("settings changed since this proposal was made; run a new review")
    v = store.adopt(p.get("changes", {}), p.get("lessons", []), note=f"approved proposal {pid}")
    p.update(status="adopted", adopted_version=v)
    store.save_proposal(p)
    return v
