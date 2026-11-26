"""Configuration: every threshold from the trading rules lives here.

Values load from a YAML file (see config.example.yaml); anything not set falls
back to the defaults below. Secrets come only from environment variables.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

# The exact phrase required in TRADEBOT_ALLOW_LIVE before real money is used.
LIVE_CONFIRMATION_PHRASE = "I_UNDERSTAND_THIS_USES_REAL_MONEY"


class SetupThresholds(BaseModel):
    """The setup checklist. Every item must pass for a trade."""

    catalyst_max_age_hours: float = 24.0
    min_relative_volume: float = 2.0  # today's pace vs 20-day average daily volume
    relative_volume_lookback_days: int = 20
    min_avg_daily_volume: float = 1_000_000
    max_spread_pct: float = 0.10  # percent of mid price, so 0.10 means 0.1%
    min_reward_risk: float = 2.0
    # How far from entry (in ATRs) a support/resistance level may be and still
    # count as "clean" for the stop.
    max_level_distance_atr: float = 1.5


class ExitRules(BaseModel):
    """Exits are set at entry and are never widened."""

    atr_period: int = 14
    stop_atr_multiple: float = 1.5  # fallback stop when no level is usable
    target_r_multiple: float = 2.0  # default target if no resistance nearer
    trail_after_r: float = 1.0  # start trailing once price moves this many R
    trail_distance_r: float = 1.0  # trail distance once trailing, in R
    flatten_time_et: str = "15:50"  # DAY mode: flat by this time, no overnight


class SessionRules(BaseModel):
    no_entries_before_et: str = "09:45"  # let VWAP and opening range settle
    no_entries_after_et: str = "15:30"
    manage_interval_seconds: int = 30  # how often open positions are managed
    scan_interval_seconds: int = 300  # how often the watchlist is re-planned


class RiskLimits(BaseModel):
    risk_per_trade_pct: float = 0.5  # % of equity lost if the stop is hit
    max_position_pct: float = 20.0  # max % of equity in one position
    max_open_positions: int = 3
    max_trades_per_day: int = 5
    daily_loss_limit_pct: float = 2.0  # stop opening trades after this drawdown
    min_confidence: float = 0.6  # planner confidence needed to act
    entry_limit_slippage_pct: float = 0.10  # stop-limit band above/below trigger


class PlannerSettings(BaseModel):
    model: str = "claude-opus-5-5"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    max_tokens: int = 16000
    use_refusal_fallbacks: bool = True


class TradingSettings(BaseModel):
    # dry_run: plan and log only, no orders. paper: Alpaca paper account.
    # live: real money; also needs TRADEBOT_ALLOW_LIVE set to the phrase above.
    mode: Literal["dry_run", "paper", "live"] = "paper"
    data_feed: Literal["iex", "sip"] = "iex"  # free Alpaca plans get IEX
    watchlist: list[str] = Field(default_factory=list)
    journal_path: str = "journal/trades.jsonl"


class ImprovementSettings(BaseModel):
    """Self-improvement. Proposals are tested on past plans before adoption."""

    dir: str = "tuning"
    review_after_close: bool = True  # score the day and draft a proposal after flattening
    auto_apply: bool = False  # True: adopt proposals that pass replay without asking
    lookback_days: int = 30
    min_trades: int = 20  # replayed trades needed before any change can pass
    min_expectancy_gain_r: float = 0.05  # candidate must beat baseline by this much R/trade
    max_drawdown_worsen_r: float = 1.0
    reviewer_model: str = "claude-opus-5-5"
    reviewer_effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    # Adopted lessons from past trades, shown to the planner. Filled from
    # tuning/active.yaml; it cannot override the checklist (code still checks).
    lessons: list[str] = Field(default_factory=list)


class Config(BaseModel):
    setup: SetupThresholds = Field(default_factory=SetupThresholds)
    exits: ExitRules = Field(default_factory=ExitRules)
    session: SessionRules = Field(default_factory=SessionRules)
    risk: RiskLimits = Field(default_factory=RiskLimits)
    planner: PlannerSettings = Field(default_factory=PlannerSettings)
    trading: TradingSettings = Field(default_factory=TradingSettings)
    improvement: ImprovementSettings = Field(default_factory=ImprovementSettings)

    @classmethod
    def load(cls, path: str | os.PathLike | None = None) -> "Config":
        if path is None:
            path = os.environ.get("TRADEBOT_CONFIG", "config.yaml")
        p = Path(path)
        if not p.exists():
            return cls()
        data = yaml.safe_load(p.read_text()) or {}
        return cls.model_validate(data)


class LiveTradingNotAllowed(RuntimeError):
    pass


def assert_mode_allowed(cfg: Config) -> None:
    """Refuse live trading unless it is enabled in config AND confirmed in env."""
    if cfg.trading.mode != "live":
        return
    if os.environ.get("TRADEBOT_ALLOW_LIVE") != LIVE_CONFIRMATION_PHRASE:
        raise LiveTradingNotAllowed(
            "trading.mode is 'live' but TRADEBOT_ALLOW_LIVE is not set to "
            f"{LIVE_CONFIRMATION_PHRASE!r}. Refusing to trade real money."
        )
