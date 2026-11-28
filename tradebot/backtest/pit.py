"""Point-in-time snapshots: what the bot could have known at a given moment.

The no-lookahead rules, all enforced here:
- A 1-minute bar stamped t covers [t, t+1min). It is visible at `now` only if
  t + 1 minute <= now. So at 10:30:00 the last bar seen is the 10:29 bar.
- Daily bars are completed sessions strictly before today's date.
- News is visible only if it was published at or before `now`.
- The quote is the last one at or before `now`. Sources without quotes use
  the last visible close +/- an assumed spread, and say so in `missing`-free
  but clearly labeled settings (see BacktestOptions.assume_spread_pct).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Literal, Optional, Sequence

from ..clock import ET
from ..config import Config
from ..indicators import Bar
from ..market_data import build_snapshot
from ..models import MarketSnapshot, NewsItem
from .data import DailyRow, MinuteRow

ONE_MIN = timedelta(minutes=1)
CatalystMode = Literal["real", "proxy", "skip"]


class LookaheadError(AssertionError):
    pass


@dataclass
class PitOptions:
    catalyst: CatalystMode = "real"
    proxy_gap_pct: float = 2.0  # "proxy": a gap this big from the prior close stands in for news
    assume_spread_pct: float = 0.02  # used only when the source has no quotes


def visible_minutes(rows: Sequence[MinuteRow], now: datetime) -> list[MinuteRow]:
    return [(t, b) for t, b in rows if t + ONE_MIN <= now]


def visible_dailies(rows: Sequence[DailyRow], now: datetime, keep: int = 60) -> list[Bar]:
    today = now.astimezone(ET).date()
    return [b for d, b in rows if d < today][-keep:]


def visible_news(items: Sequence[dict], now: datetime, max_age_hours: float) -> list[NewsItem]:
    out = []
    for n in items:
        created = datetime.fromisoformat(n["created_at"]) if isinstance(n["created_at"], str) else n["created_at"]
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age = (now - created).total_seconds() / 3600
        if 0 <= age <= max_age_hours:
            out.append(NewsItem(headline=n["headline"], summary=n.get("summary", ""), source=n.get("source", ""),
                                created_at=created, age_hours=round(age, 2)))
    return out


def snapshot_at(
    ticker: str,
    now: datetime,
    cfg: Config,
    minutes_today: Sequence[MinuteRow],
    dailies: Sequence[DailyRow],
    news_items: Sequence[dict],
    quote: Optional[tuple[float, float]],
    opts: PitOptions,
) -> MarketSnapshot:
    intraday_rows = visible_minutes(minutes_today, now)
    for t, _ in intraday_rows:  # belt and braces
        if t + ONE_MIN > now:
            raise LookaheadError(f"{ticker}: bar {t} is not closed at {now}")
    intraday = [b for _, b in intraday_rows]
    daily = visible_dailies(dailies, now)

    if opts.catalyst == "real":
        news = visible_news(news_items, now, cfg.setup.catalyst_max_age_hours * 2)
    else:
        news = []
        if opts.catalyst == "skip":
            news.append(_proxy_item(now, "[catalyst check skipped in this backtest]"))
        elif intraday and daily:
            gap = (intraday[0].open - daily[-1].close) / daily[-1].close * 100
            if abs(gap) >= opts.proxy_gap_pct:
                news.append(_proxy_item(now, f"[proxy, not real news] gapped {gap:+.1f}% at the open"))

    bid = ask = None
    if quote:
        bid, ask = quote
    elif intraday:
        mid = intraday[-1].close
        half = mid * opts.assume_spread_pct / 100 / 2
        bid, ask = mid - half, mid + half
    return build_snapshot(ticker, now, daily, intraday, bid, ask, news, cfg)


def _proxy_item(now: datetime, text: str) -> NewsItem:
    open_et = now.astimezone(ET).replace(hour=9, minute=30, second=0, microsecond=0)
    created = min(now, open_et.astimezone(timezone.utc))
    return NewsItem(headline=text, source="backtest", created_at=created,
                    age_hours=round((now - created).total_seconds() / 3600, 2))
