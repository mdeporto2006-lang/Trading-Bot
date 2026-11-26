"""Market data from Alpaca, turned into a MarketSnapshot.

`build_snapshot` is pure (takes already-fetched data) so it can be tested with
fake bars. `AlpacaMarketData` does the fetching.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

from . import indicators as ind
from .clock import session_fraction_elapsed, session_open_dt, to_et
from .config import Config
from .models import MarketSnapshot, NewsItem

log = logging.getLogger(__name__)


def _aggregate(bars: Sequence[ind.Bar], n: int) -> list[ind.Bar]:
    out = []
    for i in range(0, len(bars), n):
        chunk = bars[i : i + n]
        out.append(
            ind.Bar(
                open=chunk[0].open,
                high=max(b.high for b in chunk),
                low=min(b.low for b in chunk),
                close=chunk[-1].close,
                volume=sum(b.volume for b in chunk),
            )
        )
    return out


def build_snapshot(
    ticker: str,
    now: datetime,
    daily_bars: Sequence[ind.Bar],
    intraday_bars: Sequence[ind.Bar],
    bid: float | None,
    ask: float | None,
    news: Iterable[NewsItem],
    cfg: Config,
) -> MarketSnapshot:
    """daily_bars: completed sessions, oldest first, NOT including today.
    intraday_bars: today's 1-minute regular-session bars, oldest first."""
    snap = MarketSnapshot(ticker=ticker, as_of=now)
    missing: list[str] = []

    # Quote / spread
    if bid and ask and ask >= bid > 0:
        snap.bid, snap.ask = bid, ask
        mid = (bid + ask) / 2
        snap.spread_pct = (ask - bid) / mid * 100
    else:
        missing.append("quote")

    # Daily stats
    lookback = cfg.setup.relative_volume_lookback_days
    snap.avg_daily_volume = ind.average_volume(daily_bars, lookback)
    if snap.avg_daily_volume is None:
        missing.append(f"{lookback} days of daily bars")
    snap.atr = ind.atr(daily_bars, cfg.exits.atr_period)
    if snap.atr is None:
        missing.append("ATR")
    if daily_bars:
        snap.prior_close = daily_bars[-1].close

    # Intraday stats
    if intraday_bars:
        snap.last_price = intraday_bars[-1].close
        snap.vwap = ind.vwap(intraday_bars)
        snap.today_volume = sum(b.volume for b in intraday_bars)
        snap.day_high = max(b.high for b in intraday_bars)
        snap.day_low = min(b.low for b in intraday_bars)
    else:
        missing.append("intraday bars")
    if snap.vwap is None:
        missing.append("VWAP")

    snap.session_fraction_elapsed = session_fraction_elapsed(now)
    if snap.today_volume is not None and snap.avg_daily_volume:
        snap.relative_volume = ind.relative_volume(
            snap.today_volume, snap.avg_daily_volume, snap.session_fraction_elapsed
        )
    if snap.relative_volume is None:
        missing.append("relative volume")

    # Levels: 5-minute swing pivots, today's range, prior day's range and close
    if snap.last_price is not None:
        lows, highs = ind.pivot_levels(_aggregate(intraday_bars, 5), window=2)
        extra = [snap.day_high, snap.day_low]
        if daily_bars:
            extra += [daily_bars[-1].high, daily_bars[-1].low, daily_bars[-1].close]
        snap.supports, snap.resistances = ind.nearest_levels(
            snap.last_price, lows, highs, [x for x in extra if x is not None]
        )

    snap.news = sorted(news, key=lambda n: n.age_hours)
    snap.missing = missing
    return snap


class AlpacaMarketData:
    """Fetches bars, quotes and news from Alpaca's market data API."""

    def __init__(self, api_key: str, secret_key: str, cfg: Config):
        from alpaca.data.historical import NewsClient, StockHistoricalDataClient

        self.cfg = cfg
        self.stocks = StockHistoricalDataClient(api_key, secret_key)
        self.news_client = NewsClient(api_key, secret_key)

    def _bars(self, ticker: str, timeframe: Any, start: datetime, end: datetime):
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockBarsRequest

        req = StockBarsRequest(
            symbol_or_symbols=ticker,
            timeframe=timeframe,
            start=start,
            end=end,
            feed=DataFeed(self.cfg.trading.data_feed),
        )
        barset = self.stocks.get_stock_bars(req)
        rows = barset.data.get(ticker, []) if hasattr(barset, "data") else []
        return rows

    def snapshot(self, ticker: str, now: datetime | None = None) -> MarketSnapshot:
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import NewsRequest, StockLatestQuoteRequest
        from alpaca.data.timeframe import TimeFrame

        now = now or datetime.now(timezone.utc)
        open_et = session_open_dt(now)

        def to_bar(b) -> ind.Bar:
            return ind.Bar(float(b.open), float(b.high), float(b.low), float(b.close), float(b.volume))

        daily, intraday, bid, ask, news = [], [], None, None, []
        try:
            raw = self._bars(ticker, TimeFrame.Day, now - timedelta(days=60), now)
            daily = [to_bar(b) for b in raw if to_et(b.timestamp).date() < to_et(now).date()]
        except Exception as e:  # data errors -> missing data -> NO_TRADE
            log.warning("%s: daily bars failed: %s", ticker, e)
        try:
            raw = self._bars(ticker, TimeFrame.Minute, open_et, now)
            intraday = [to_bar(b) for b in raw]
        except Exception as e:
            log.warning("%s: intraday bars failed: %s", ticker, e)
        try:
            q = self.stocks.get_stock_latest_quote(
                StockLatestQuoteRequest(
                    symbol_or_symbols=ticker, feed=DataFeed(self.cfg.trading.data_feed)
                )
            )[ticker]
            bid, ask = float(q.bid_price or 0) or None, float(q.ask_price or 0) or None
        except Exception as e:
            log.warning("%s: quote failed: %s", ticker, e)
        try:
            hours = self.cfg.setup.catalyst_max_age_hours * 2
            resp = self.news_client.get_news(
                NewsRequest(symbols=ticker, start=now - timedelta(hours=hours), end=now, limit=20)
            )
            data = resp.data.get("news", []) if isinstance(resp.data, dict) else resp.data
            for n in data:
                created = n.created_at
                news.append(
                    NewsItem(
                        headline=n.headline,
                        summary=(n.summary or "")[:500],
                        source=n.source or "",
                        created_at=created,
                        age_hours=round((now - created).total_seconds() / 3600, 2),
                    )
                )
        except Exception as e:
            log.warning("%s: news failed: %s", ticker, e)

        return build_snapshot(ticker, now, daily, intraday, bid, ask, news, self.cfg)
