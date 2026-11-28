"""Historical data sources for backtests and the time machine.

Each source returns plain data stamped with times, so the engine can cut it at
the decision moment and nothing from the future leaks in:

  daily(ticker, start, end)   -> [(session date, Bar)]     completed sessions
  minutes(ticker, day)        -> [(bar start UTC, Bar)]    09:30-16:00 ET 1-minute bars
  news(ticker, start, end)    -> [NewsItem-like dicts with created_at]
  quote_at(ticker, ts)        -> (bid, ask) live at ts, or None

Sources:
  AlpacaHistory   real bars (SIP feed works on the free plan for data older than
                  15 minutes), real news with publish times, real quotes. Needs
                  ALPACA_API_KEY / ALPACA_SECRET_KEY.
  YahooHistory    no keys. 1-minute bars for roughly the last 30 days only, no
                  news and no quotes, so the catalyst and spread checks have to
                  be stood in for (see engine.py --catalyst / --assume-spread).
  SyntheticHistory  made-up prices for tests and demos. Never a real result.

`CachedSource` wraps any of them and stores what it fetched under
backtest_cache/, so re-running a backtest costs nothing.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import random
import time as _time
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Protocol

from ..clock import ET
from ..indicators import Bar

log = logging.getLogger(__name__)

MinuteRow = tuple[datetime, Bar]
DailyRow = tuple[date, Bar]


class HistoricalSource(Protocol):
    name: str
    has_news: bool
    has_quotes: bool

    def daily(self, ticker: str, start: date, end: date) -> list[DailyRow]: ...
    def minutes(self, ticker: str, day: date) -> list[MinuteRow]: ...
    def news(self, ticker: str, start: datetime, end: datetime) -> list[dict]: ...
    def quote_at(self, ticker: str, ts: datetime) -> Optional[tuple[float, float]]: ...


def session_bounds(day: date) -> tuple[datetime, datetime]:
    o = datetime.combine(day, time(9, 30), ET)
    c = datetime.combine(day, time(16, 0), ET)
    return o.astimezone(timezone.utc), c.astimezone(timezone.utc)


def _in_session(ts: datetime, day: date) -> bool:
    o, c = session_bounds(day)
    return o <= ts < c


# --------------------------------------------------------------------------
# Alpaca
# --------------------------------------------------------------------------
class AlpacaHistory:
    name = "alpaca"
    has_news = True
    has_quotes = True

    def __init__(self, api_key: str, secret_key: str, feed: str = "sip"):
        from alpaca.data.historical import NewsClient, StockHistoricalDataClient

        self.feed = feed
        self.stocks = StockHistoricalDataClient(api_key, secret_key)
        self.news_client = NewsClient(api_key, secret_key)

    def _bars(self, ticker: str, timeframe: Any, start: datetime, end: datetime):
        from alpaca.data.enums import Adjustment, DataFeed
        from alpaca.data.requests import StockBarsRequest

        req = StockBarsRequest(
            symbol_or_symbols=ticker, timeframe=timeframe, start=start, end=end,
            feed=DataFeed(self.feed), adjustment=Adjustment.SPLIT,
        )
        barset = self.stocks.get_stock_bars(req)
        return barset.data.get(ticker, []) if hasattr(barset, "data") else []

    @staticmethod
    def _bar(b) -> Bar:
        return Bar(float(b.open), float(b.high), float(b.low), float(b.close), float(b.volume))

    def daily(self, ticker: str, start: date, end: date) -> list[DailyRow]:
        from alpaca.data.timeframe import TimeFrame

        s = datetime.combine(start, time(0), timezone.utc)
        e = datetime.combine(end, time(23, 59), timezone.utc)
        return [(b.timestamp.astimezone(ET).date(), self._bar(b)) for b in self._bars(ticker, TimeFrame.Day, s, e)]

    def minutes(self, ticker: str, day: date) -> list[MinuteRow]:
        from alpaca.data.timeframe import TimeFrame

        o, c = session_bounds(day)
        rows = self._bars(ticker, TimeFrame.Minute, o, c)
        return [(b.timestamp.astimezone(timezone.utc), self._bar(b)) for b in rows
                if _in_session(b.timestamp.astimezone(timezone.utc), day)]

    def news(self, ticker: str, start: datetime, end: datetime) -> list[dict]:
        from alpaca.data.requests import NewsRequest

        out: list[dict] = []
        token = None
        for _ in range(10):  # at most 500 items per window
            resp = self.news_client.get_news(NewsRequest(
                symbols=ticker, start=start, end=end, limit=50, page_token=token))
            data = resp.data.get("news", []) if isinstance(resp.data, dict) else resp.data
            for n in data:
                out.append({
                    "headline": n.headline, "summary": (n.summary or "")[:500],
                    "source": n.source or "", "created_at": n.created_at.astimezone(timezone.utc).isoformat(),
                })
            token = getattr(resp, "next_page_token", None)
            if not token:
                break
        return out

    def quote_at(self, ticker: str, ts: datetime) -> Optional[tuple[float, float]]:
        from alpaca.common.enums import Sort
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockQuotesRequest

        req = StockQuotesRequest(symbol_or_symbols=ticker, start=ts - timedelta(minutes=2), end=ts,
                                 limit=1, sort=Sort.DESC, feed=DataFeed(self.feed))
        qs = self.stocks.get_stock_quotes(req)
        rows = qs.data.get(ticker, []) if hasattr(qs, "data") else []
        if not rows:
            return None
        bid, ask = float(rows[0].bid_price or 0), float(rows[0].ask_price or 0)
        return (bid, ask) if bid > 0 and ask >= bid else None


# --------------------------------------------------------------------------
# Yahoo Finance chart API (free, no keys)
# --------------------------------------------------------------------------
class YahooRejected(RuntimeError):
    """Yahoo answered 400/422: it won't serve that window."""


def _body(r) -> str:
    try:
        return (r.text or "")[:300].replace("\n", " ")
    except Exception:
        return ""


class YahooHistory:
    """Yahoo's public chart endpoint. Minute bars exist only for about the last
    30 calendar days, fetched in windows of at most 7 days."""

    name = "yahoo"
    has_news = False
    has_quotes = False
    HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")
    PATH = "/v8/finance/chart/{t}"
    USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
    MIN_INTERVAL = 0.8  # seconds between requests, to stay under Yahoo's rate limit
    MAX_TRIES = 7

    def __init__(self, session=None, sleep=_time.sleep, clock=_time.monotonic):
        self.http = session or self._new_session()
        self.http.headers.update({
            "User-Agent": self.USER_AGENT,
            "Accept": "application/json,text/plain,*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": "https://finance.yahoo.com",
            "Referer": "https://finance.yahoo.com/",
        })
        self._sleep, self._clock = sleep, clock
        self._last = 0.0
        self._crumb: str | None = None
        self._crumb_tried = False
        self._minute_cache: dict[tuple[str, date], list[MinuteRow]] = {}

    @staticmethod
    def _new_session():
        # curl_cffi (if installed) looks like a real Chrome to Yahoo, which helps on
        # cloud machines (Codespaces, CI) that Yahoo throttles hard. Optional.
        try:
            from curl_cffi import requests as cffi

            return cffi.Session(impersonate="chrome")
        except Exception:
            import requests

            return requests.Session()

    def _pace(self) -> None:
        wait = self.MIN_INTERVAL - (self._clock() - self._last)
        if wait > 0:
            self._sleep(wait)
        self._last = self._clock()

    def _ensure_crumb(self) -> None:
        """Yahoo sometimes wants a session cookie + crumb. Best effort: get them once."""
        if self._crumb_tried:
            return
        self._crumb_tried = True
        try:
            self._pace()
            self.http.get("https://fc.yahoo.com", timeout=15, allow_redirects=True)
            self._pace()
            r = self.http.get(f"https://{self.HOSTS[1]}/v1/test/getcrumb", timeout=15)
            text = (r.text or "").strip()
            if r.status_code == 200 and text and "<" not in text and len(text) < 64:
                self._crumb = text
        except Exception as e:  # no crumb is fine; most requests work without
            log.debug("yahoo crumb fetch failed: %s", e)

    def _get(self, ticker: str, params: dict) -> dict:
        last_err = ""
        for attempt in range(self.MAX_TRIES):
            host = self.HOSTS[attempt % len(self.HOSTS)]  # alternate query1 / query2
            q = dict(params)
            if self._crumb:
                q["crumb"] = self._crumb
            self._pace()
            try:
                r = self.http.get(f"https://{host}{self.PATH.format(t=ticker)}", params=q, timeout=30)
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                self._sleep(min(30, 2 ** attempt))
                continue
            if r.status_code == 200:
                res = (r.json().get("chart") or {}).get("result") or []
                return res[0] if res else {}
            last_err = f"HTTP {r.status_code} from {host}"
            if r.status_code in (401, 403):
                self._ensure_crumb()  # then retry with cookie + crumb
            elif r.status_code == 404:
                raise RuntimeError(f"Yahoo has no data for {ticker} ({last_err})")
            elif r.status_code in (400, 422):
                # Yahoo rejects some date windows (e.g. minute bars older than ~30 days).
                raise YahooRejected(f"{last_err}: {_body(r)}")
            if r.status_code == 429 or r.status_code >= 500 or r.status_code in (401, 403):
                retry_after = r.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else min(60, 2 ** (attempt + 1))
                if attempt == 1:
                    self._ensure_crumb()  # a cookie session often clears a 429 wall
                log.info("Yahoo %s for %s, waiting %.0fs (try %d/%d)", last_err, ticker, delay,
                         attempt + 1, self.MAX_TRIES)
                self._sleep(delay + random.random())
                continue
            raise RuntimeError(f"Yahoo request for {ticker} failed: {last_err}: {_body(r)}")
        raise RuntimeError(
            f"Yahoo kept refusing requests for {ticker} ({last_err}) after {self.MAX_TRIES} tries. "
            "Yahoo throttles some cloud machines hard. Try: `pip install curl_cffi` and re-run, "
            "wait a few minutes, or use free Alpaca paper keys (see README > Backtesting)."
        )

    @staticmethod
    def parse(result: dict) -> list[tuple[datetime, Bar]]:
        ts = result.get("timestamp") or []
        q = ((result.get("indicators") or {}).get("quote") or [{}])[0]
        out = []
        for i, t in enumerate(ts):
            o, h, l, c, v = (q.get(k, [None] * len(ts))[i] for k in ("open", "high", "low", "close", "volume"))
            if None in (o, h, l, c) or any(isinstance(x, float) and math.isnan(x) for x in (o, h, l, c)):
                continue
            out.append((datetime.fromtimestamp(t, timezone.utc), Bar(float(o), float(h), float(l), float(c), float(v or 0))))
        return out

    def daily(self, ticker: str, start: date, end: date) -> list[DailyRow]:
        p1 = int(datetime.combine(start, time(0), timezone.utc).timestamp())
        p2 = int(datetime.combine(end + timedelta(days=1), time(0), timezone.utc).timestamp())
        try:
            res = self._get(ticker, {"period1": p1, "period2": p2, "interval": "1d", "includePrePost": "false"})
        except YahooRejected as e:
            log.info("Yahoo rejected the daily window for %s (%s); asking for the last year instead", ticker, e)
            res = self._get(ticker, {"range": "1y", "interval": "1d", "includePrePost": "false"})
        return [(ts.astimezone(ET).date(), b) for ts, b in self.parse(res) if start <= ts.astimezone(ET).date() <= end]

    def minutes(self, ticker: str, day: date) -> list[MinuteRow]:
        key = (ticker, day)
        if key not in self._minute_cache:
            # Fetch a week at a time and fill the cache for every day in it.
            start = day - timedelta(days=day.weekday())
            p1 = int(datetime.combine(start, time(0), ET).timestamp())
            p2 = min(int(datetime.combine(start + timedelta(days=7), time(0), ET).timestamp()), int(_time.time()) - 60)
            try:
                res = self._get(ticker, {"period1": p1, "period2": p2, "interval": "1m", "includePrePost": "false"})
            except YahooRejected as e:
                log.warning("Yahoo has no minute bars for %s in the week of %s (%s); skipping those days",
                            ticker, start, e)
                res = {}
            by_day: dict[date, list[MinuteRow]] = {}
            for ts, b in self.parse(res):
                d = ts.astimezone(ET).date()
                if _in_session(ts, d):
                    by_day.setdefault(d, []).append((ts, b))
            for i in range(7):
                d = start + timedelta(days=i)
                self._minute_cache[(ticker, d)] = by_day.get(d, [])
        return self._minute_cache[key]

    def news(self, ticker: str, start: datetime, end: datetime) -> list[dict]:
        return []

    def quote_at(self, ticker: str, ts: datetime) -> Optional[tuple[float, float]]:
        return None


# --------------------------------------------------------------------------
# Synthetic (tests and offline demos only)
# --------------------------------------------------------------------------
class SyntheticHistory:
    """Random-walk prices with occasional news days that trend and run heavy
    volume. Deterministic for a given seed. Results are NOT real."""

    name = "synthetic"
    has_news = True
    has_quotes = False

    def __init__(self, seed: int = 7, base_price: float = 100.0):
        self.seed = seed
        self.base = base_price

    def _rng(self, *parts) -> random.Random:
        h = hashlib.sha256(("|".join(map(str, (self.seed, *parts)))).encode()).hexdigest()
        return random.Random(int(h[:16], 16))

    def _is_news_day(self, ticker: str, day: date) -> bool:
        return self._rng("news", ticker, day).random() < 0.3

    def _open_price(self, ticker: str, day: date) -> float:
        # Smooth path of daily opens from a fixed anchor so days chain together.
        r = self._rng("anchor", ticker)
        anchor = self.base * (0.5 + r.random())
        n = (day - date(2020, 1, 1)).days
        return anchor * (1 + 0.15 * math.sin(n / 40 + r.random() * 6))

    def daily(self, ticker: str, start: date, end: date) -> list[DailyRow]:
        out = []
        d = start
        while d <= end:
            if d.weekday() < 5:
                bars = [b for _, b in self.minutes(ticker, d)]
                out.append((d, Bar(bars[0].open, max(b.high for b in bars), min(b.low for b in bars),
                                   bars[-1].close, sum(b.volume for b in bars))))
            d += timedelta(days=1)
        return out

    def minutes(self, ticker: str, day: date) -> list[MinuteRow]:
        if day.weekday() >= 5:
            return []
        r = self._rng("min", ticker, day)
        news = self._is_news_day(ticker, day)
        drift = (r.choice([-1, 1]) * 0.00025) if news else 0.0
        vol_scale = 3.0 if news else 1.0
        o, _ = session_bounds(day)
        p = self._open_price(ticker, day)
        out = []
        for i in range(390):
            shape = 1.8 if i < 30 or i > 360 else 1.0  # U-shaped volume
            ret = drift + r.gauss(0, 0.0012)
            c = p * (1 + ret)
            h = max(p, c) * (1 + abs(r.gauss(0, 0.0004)))
            l = min(p, c) * (1 - abs(r.gauss(0, 0.0004)))
            v = 5000 * shape * vol_scale * (0.5 + r.random())
            out.append((o + timedelta(minutes=i), Bar(p, h, l, c, v)))
            p = c
        return out

    def news(self, ticker: str, start: datetime, end: datetime) -> list[dict]:
        out = []
        d = start.astimezone(ET).date() - timedelta(days=1)
        while d <= end.astimezone(ET).date():
            if d.weekday() < 5 and self._is_news_day(ticker, d):
                ts = datetime.combine(d, time(7, 0), ET).astimezone(timezone.utc)
                if start <= ts <= end:
                    out.append({"headline": f"{ticker} synthetic catalyst", "summary": "", "source": "synthetic",
                                "created_at": ts.isoformat()})
            d += timedelta(days=1)
        return out

    def quote_at(self, ticker: str, ts: datetime) -> Optional[tuple[float, float]]:
        return None


# --------------------------------------------------------------------------
# Disk cache
# --------------------------------------------------------------------------
class CachedSource:
    """Stores every fetch under `root/<source>/...json`. Past data does not
    change, so cached entries never expire. Days that are today or later are
    never cached (the session may not be over)."""

    def __init__(self, inner: HistoricalSource, root: str | Path = "backtest_cache"):
        self.inner = inner
        self.name, self.has_news, self.has_quotes = inner.name, inner.has_news, inner.has_quotes
        self.root = Path(root) / inner.name
        self.calls = 0  # network fetches made (for the progress line)

    def _path(self, *parts) -> Path:
        safe = [str(p).replace("/", "_").replace(":", "") for p in parts]
        return self.root.joinpath(*safe[:-1], safe[-1] + ".json")

    def _cached(self, path: Path, fetch, cacheable: bool):
        if path.exists():
            return json.loads(path.read_text())
        self.calls += 1
        val = fetch()
        if cacheable:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(val))
        return val

    @staticmethod
    def _past(d: date) -> bool:
        return d < datetime.now(ET).date()

    def daily(self, ticker: str, start: date, end: date) -> list[DailyRow]:
        raw = self._cached(
            self._path(ticker, "daily", f"{start}_{end}"),
            lambda: [[d.isoformat(), [b.open, b.high, b.low, b.close, b.volume]]
                     for d, b in self.inner.daily(ticker, start, end)],
            self._past(end),
        )
        return [(date.fromisoformat(d), Bar(*b)) for d, b in raw]

    def minutes(self, ticker: str, day: date) -> list[MinuteRow]:
        raw = self._cached(
            self._path(ticker, "minutes", day.isoformat()),
            lambda: [[ts.isoformat(), [b.open, b.high, b.low, b.close, b.volume]]
                     for ts, b in self.inner.minutes(ticker, day)],
            self._past(day),
        )
        return [(datetime.fromisoformat(t), Bar(*b)) for t, b in raw]

    def news(self, ticker: str, start: datetime, end: datetime) -> list[dict]:
        key = f"{start:%Y%m%dT%H%M}_{end:%Y%m%dT%H%M}"
        return self._cached(self._path(ticker, "news", key), lambda: self.inner.news(ticker, start, end),
                            self._past(end.astimezone(ET).date()))

    def quote_at(self, ticker: str, ts: datetime) -> Optional[tuple[float, float]]:
        if not self.inner.has_quotes:
            return None
        val = self._cached(self._path(ticker, "quotes", f"{ts:%Y%m%dT%H%M}"),
                           lambda: list(self.inner.quote_at(ticker, ts) or []),
                           self._past(ts.astimezone(ET).date()))
        return (val[0], val[1]) if val else None
