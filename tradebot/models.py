"""Data shapes shared by the data layer, planner, risk gate and executor."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

Action = Literal["BUY", "SHORT", "NO_TRADE"]


class NewsItem(BaseModel):
    headline: str
    summary: str = ""
    source: str = ""
    created_at: datetime
    age_hours: float


class MarketSnapshot(BaseModel):
    """Everything the planner may use, all fetched during this run.

    A field left as None means the data was unavailable; the checklist treats
    it as a FAIL, so the outcome is NO_TRADE.
    """

    ticker: str
    as_of: datetime
    last_price: Optional[float] = None
    bid: Optional[float] = None
    ask: Optional[float] = None
    spread_pct: Optional[float] = None
    vwap: Optional[float] = None
    atr: Optional[float] = None
    avg_daily_volume: Optional[float] = None
    today_volume: Optional[float] = None
    session_fraction_elapsed: Optional[float] = None
    relative_volume: Optional[float] = None
    day_high: Optional[float] = None
    day_low: Optional[float] = None
    prior_close: Optional[float] = None
    supports: list[float] = Field(default_factory=list)  # nearest first
    resistances: list[float] = Field(default_factory=list)  # nearest first
    news: list[NewsItem] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)


class ChecklistItem(BaseModel):
    item: str
    value: str
    result: Literal["PASS", "FAIL"]


class TradePlan(BaseModel):
    """The planner's output contract (one per ticker)."""

    ticker: str
    mode: Literal["DAY", "SWING"]
    action: Action
    checklist: list[ChecklistItem]
    entry_trigger: Optional[float] = Field(
        default=None, description="Price that triggers entry; null for NO_TRADE"
    )
    stop: Optional[float] = None
    target: Optional[float] = None
    rr_ratio: Optional[float] = None
    invalidation: str
    confidence: float = Field(description="0 to 1")


class RiskDecision(BaseModel):
    """What the deterministic risk gate decided about a plan."""

    ticker: str
    approved: bool
    action: Action
    reasons: list[str] = Field(default_factory=list)
    checklist: list[ChecklistItem] = Field(default_factory=list)
    entry: Optional[float] = None
    stop: Optional[float] = None
    target: Optional[float] = None
    rr_ratio: Optional[float] = None
    qty: int = 0
