"""Backtest engine, point-in-time snapshots, lesson library, time machine."""

from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone

import pytest

from tradebot.backtest.cost import estimate
from tradebot.backtest.data import SyntheticHistory, YahooHistory, session_bounds
from tradebot.backtest.engine import BacktestEngine, BacktestOptions, Order
from tradebot.backtest.library import Lesson, LessonLibrary
from tradebot.backtest.pit import LookaheadError, PitOptions, snapshot_at, visible_minutes
from tradebot.backtest.report import metrics, save, text_summary
from tradebot.backtest.rules_planner import RulesPlanner
from tradebot.backtest.timemachine import visit
from tradebot.clock import ET
from tradebot.config import Config
from tradebot.indicators import Bar

DAY = date(2026, 11, 18)  # a Wednesday
TICKERS = ["AAA", "BBB", "CCC"]


def at(h, m, d=DAY):
    return datetime.combine(d, time(h, m), ET).astimezone(timezone.utc)


class Poisoned(SyntheticHistory):
    """Synthetic data whose bars after `cut` are replaced with garbage."""

    def __init__(self, cut: datetime):
        super().__init__(seed=7)
        self.cut = cut

    def minutes(self, ticker, day):
        rows = super().minutes(ticker, day)
        return [(t, b if t < self.cut else Bar(b.open * 3, b.high * 3, b.low * 0.1, b.close * 0.5, b.volume * 50))
                for t, b in rows]

    def news(self, ticker, start, end):
        items = super().news(ticker, start, end)
        return items + [{"headline": "FUTURE NEWS", "summary": "", "source": "x",
                         "created_at": (self.cut + timedelta(minutes=5)).isoformat()}]


# --- no lookahead -----------------------------------------------------------

def test_minute_bar_visible_only_after_it_closes():
    rows = SyntheticHistory().minutes("AAA", DAY)
    seen = visible_minutes(rows, at(10, 30))
    assert seen[-1][0] == at(10, 29)
    assert all(t + timedelta(minutes=1) <= at(10, 30) for t, _ in seen)


def test_snapshot_ignores_future_bars_and_news():
    cfg, pit = Config(), PitOptions()
    now = at(11, 0)
    clean, poisoned = SyntheticHistory(seed=7), Poisoned(now)
    snaps = []
    for src in (clean, poisoned):
        m = src.minutes("AAA", DAY)
        dly = src.daily("AAA", DAY - timedelta(days=90), DAY)
        news = src.news("AAA", now - timedelta(hours=48), at(16, 0))
        snaps.append(snapshot_at("AAA", now, cfg, m, dly, news, None, pit).model_dump())
    assert snaps[0] == snaps[1]
    assert all("FUTURE" not in n["headline"] for n in snaps[1]["news"])


def test_daily_bars_exclude_today():
    cfg = Config()
    src = SyntheticHistory()
    dly = src.daily("AAA", DAY - timedelta(days=90), DAY)
    assert dly[-1][0] == DAY  # the source has today...
    snap = snapshot_at("AAA", at(11, 0), cfg, src.minutes("AAA", DAY), dly, [], None, PitOptions())
    yesterday = [b for d, b in dly if d < DAY][-1]
    assert snap.prior_close == yesterday.close  # ...but the snapshot only uses finished days


def test_backtest_decisions_before_cut_do_not_depend_on_later_bars():
    cfg = Config()
    cut = at(12, 0)
    runs = []
    for src in (SyntheticHistory(seed=7), Poisoned(cut)):
        e = BacktestEngine(cfg, src, RulesPlanner(cfg), BacktestOptions())
        e.run(TICKERS, DAY, DAY)
        runs.append([(p["ticker"], p["as_of"], p["entry"], p["stop"]) for p in e.priced_plans
                     if datetime.fromisoformat(p["as_of"]) <= cut])
    assert runs[0] == runs[1]


def test_library_hides_outcomes_not_yet_known():
    lib = LessonLibrary()
    f = {"rvol_log": 1.0, "vwap_dist_atr": 0.2, "time_of_day": 0.3, "gap_atr": 0.5, "rr": 2.0}
    lib.add(Lesson("AAA", at(10, 0).isoformat(), at(11, 0).isoformat(), "long", f, 2.0, "target"), persist=False)
    assert lib.similar(f, "long", at(10, 30)) == []
    assert len(lib.similar(f, "long", at(11, 0))) == 1
    assert lib.similar(f, "short", at(12, 0)) == []


# --- engine behaviour -------------------------------------------------------

@pytest.fixture(scope="module")
def run():
    cfg = Config()
    e = BacktestEngine(cfg, SyntheticHistory(seed=7), RulesPlanner(cfg), BacktestOptions())
    e.run(TICKERS, date(2026, 11, 2), date(2026, 11, 20))
    return cfg, e


def test_backtest_trades_and_rules(run):
    cfg, e = run
    assert e.trades, "synthetic data should produce some trades"
    assert len(e.days) == 15
    for t in e.trades:
        entry = datetime.fromisoformat(t.entry_time).astimezone(ET).time()
        exit_ = datetime.fromisoformat(t.exit_time).astimezone(ET).time()
        assert time(9, 45) <= entry < time(15, 30)
        assert exit_ <= time(15, 50)
        assert t.exit_reason in {"stop", "trail_stop", "target", "flatten"}
    per_day = {}
    for t in e.trades:
        per_day[t.day] = per_day.get(t.day, 0) + 1
    assert max(per_day.values()) <= cfg.risk.max_trades_per_day


def test_risk_sizing_respected(run):
    cfg, e = run
    for t in e.trades:
        risk = abs(t.planned_entry - t.initial_stop) * t.qty
        assert risk <= cfg.backtest.start_equity * 1.2 * cfg.risk.risk_per_trade_pct / 100 + 1


def test_metrics_and_report(run, tmp_path):
    cfg, e = run
    m = metrics(e, 100_000)
    assert m["trades"] == len(e.trades)
    assert 0 <= m["win_rate_pct"] <= 100
    assert m["max_drawdown"] >= 0
    assert abs(m["end_equity"] - e.equity_curve[-1]["equity"]) < 0.05
    run_dir, res = save(e, {"tickers": TICKERS, "start": "a", "end": "b", "source": "synthetic",
                            "planner": "rules", "catalyst": "real", "start_equity": 100_000, "warnings": []},
                        tmp_path)
    assert (run_dir / "trades.csv").exists() and (tmp_path / "latest.json").exists()
    assert "win rate" in text_summary(res)


def _manual(cfg, bars, direction="long", entry=100.0, stop=99.0, target=102.0):
    """Drive one order through the per-minute loop with hand-made bars."""
    e = BacktestEngine(cfg, SyntheticHistory(), RulesPlanner(cfg), BacktestOptions(slippage_bps=0))
    o = Order("x", "AAA", direction, 10, entry, entry * 1.001 if direction == "long" else entry * 0.999,
              stop, target, at(10, 0), {"confidence": 0.7}, {})
    o_open, _ = session_bounds(DAY)
    rows = [(o_open + timedelta(minutes=i), Bar(100, 100.05, 99.95, 100, 1000)) for i in range(390)]
    for k, b in bars.items():
        rows[k] = (rows[k][0], b)
    first = {"done": False}  # place the order at the first scan, nothing after

    def decide(self, t, now, *a):
        if not first["done"] and t == "AAA":
            first["done"] = True
            return o
        return None

    e._decide = decide.__get__(e)
    e._run_day(DAY, ["AAA"], {"AAA": rows}, {"AAA": []}, 100_000)
    return e.trades


def test_stop_wins_when_bar_touches_both():
    cfg = Config()
    tr = _manual(cfg, {20: Bar(100, 102.5, 98.5, 100, 1000)})
    assert tr[0].exit_reason == "stop" and tr[0].r_multiple == pytest.approx(-1.0)


def test_gap_through_stop_exits_at_open():
    cfg = Config()
    tr = _manual(cfg, {20: Bar(100, 100.2, 99.9, 100.1, 1000), 21: Bar(98.0, 98.2, 97.5, 98.1, 1000)})
    assert tr[0].exit_reason == "stop" and tr[0].exit == pytest.approx(98.0)


def test_target_and_flatten():
    cfg = Config()
    tr = _manual(cfg, {20: Bar(100, 100.2, 99.9, 100.1, 1000), 30: Bar(101, 102.2, 100.9, 102, 1000)})
    assert tr[0].exit_reason == "target" and tr[0].r_multiple == pytest.approx(2.0)
    tr = _manual(cfg, {20: Bar(100, 100.2, 99.9, 100.1, 1000)})
    assert tr[0].exit_reason == "flatten"
    assert datetime.fromisoformat(tr[0].exit_time).astimezone(ET).time() == time(15, 50)


def test_trailing_stop_locks_in_gain():
    cfg = Config()
    tr = _manual(cfg, {20: Bar(100, 100.2, 99.9, 100.1, 1000),
                       25: Bar(100.1, 101.6, 100.1, 101.5, 1000),   # +1.5R close: stop trails to 100.5
                       26: Bar(101.5, 101.5, 100.4, 100.6, 1000)})  # back down through it
    assert tr[0].exit_reason == "trail_stop" and tr[0].r_multiple == pytest.approx(0.5)


# --- data, cost, time machine -----------------------------------------------

def test_yahoo_parse_skips_gaps():
    res = {"timestamp": [1763476200, 1763476260],
           "indicators": {"quote": [{"open": [1.0, None], "high": [1.2, None], "low": [0.9, None],
                                      "close": [1.1, None], "volume": [100, None]}]}}
    rows = YahooHistory.parse(res)
    assert len(rows) == 1 and rows[0][1].close == 1.1 and rows[0][0].tzinfo is not None


def test_cost_estimate_scales_with_calls():
    cfg = Config()
    a, b = estimate(cfg, 10, None), estimate(cfg, 20, None)
    assert a["usd_total"] > 0 and b["usd_total"] == pytest.approx(2 * a["usd_total"], abs=0.02)


def test_timemachine_visit_scores_with_later_bars_only():
    cfg = Config()
    lib = LessonLibrary()
    rec = visit(cfg, SyntheticHistory(seed=7), "AAA", at(11, 0), RulesPlanner(cfg), PitOptions(), lib)
    assert rec["snapshot"]["as_of"].startswith(at(11, 0).isoformat()[:16])
    assert rec["what_happened_next"]["price_then"] == rec["snapshot"]["last_price"]
    poisoned = visit(cfg, Poisoned(at(11, 0)), "AAA", at(11, 0), RulesPlanner(cfg), PitOptions(), LessonLibrary())
    assert poisoned["plan"] == rec["plan"]  # the plan can't see the future


def test_cli_backtest_synthetic(tmp_path, monkeypatch, capsys):
    from tradebot.cli import main

    monkeypatch.chdir(tmp_path)
    main(["backtest", "AAA", "BBB", "--source", "synthetic", "--start", "2026-11-16", "--end", "2026-11-20"])
    out = capsys.readouterr().out
    assert "SYNTHETIC DATA" in out and (tmp_path / "backtests" / "latest.json").exists()
    main(["backtest", "AAA", "--source", "synthetic", "--start", "2026-11-16", "--end", "2026-11-17", "--estimate"])
    assert "Claude planner estimate" in capsys.readouterr().err


class _Resp:
    def __init__(self, status, payload=None, headers=None, text=""):
        self.status_code, self._p, self.headers, self.text = status, payload or {}, headers or {}, text

    def json(self):
        return self._p


class _FakeHTTP:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.headers = {}
        self.urls = []

    def get(self, url, params=None, timeout=None, allow_redirects=True):
        self.urls.append(url)
        if "fc.yahoo.com" in url:
            return _Resp(404)
        if "getcrumb" in url:
            return _Resp(200, text="abc123")
        st = self.statuses.pop(0)
        ok = {"chart": {"result": [{"timestamp": [1763476200], "indicators": {"quote": [
            {"open": [1.0], "high": [1.1], "low": [0.9], "close": [1.05], "volume": [10]}]}}]}}
        return _Resp(st, ok if st == 200 else {})


def test_yahoo_retries_429_and_alternates_hosts():
    http = _FakeHTTP([429, 429, 200])
    y = YahooHistory(session=http, sleep=lambda s: None)
    rows = y.daily("AAPL", date(2026, 11, 17), date(2026, 11, 18))
    assert len(rows) == 1
    chart = [u for u in http.urls if "/v8/finance/chart/" in u]
    assert chart[0].startswith("https://query1.") and chart[1].startswith("https://query2.")
    assert http.headers["User-Agent"].startswith("Mozilla/5.0 (Windows")
    assert y._crumb == "abc123"  # picked up a cookie + crumb after repeated 429s


def test_yahoo_gives_up_with_clear_message():
    y = YahooHistory(session=_FakeHTTP([429] * 10), sleep=lambda s: None)
    with pytest.raises(RuntimeError, match="Alpaca"):
        y.daily("AAPL", date(2026, 11, 17), date(2026, 11, 18))
