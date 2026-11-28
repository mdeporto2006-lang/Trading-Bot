from datetime import timedelta

import pytest

from conftest import NOW, daily_bars, intraday_bars, news
from tradebot import checklist as ck
from tradebot.indicators import Bar
from tradebot.market_data import build_snapshot
from tradebot.models import ChecklistItem, TradePlan
from tradebot.risk import AccountState, evaluate_plan, position_size


def plan(**kw):
    base = dict(
        ticker="ACME", mode="DAY", action="BUY",
        checklist=[ChecklistItem(item="x", value="y", result="PASS")],
        entry_trigger=103.05, stop=102.90, target=103.40,
        rr_ratio=2.3, invalidation="loses 102.98", confidence=0.8,
    )
    base.update(kw)
    return TradePlan(**base)


@pytest.fixture
def acct():
    return AccountState(equity=100_000, buying_power=200_000, start_of_day_equity=100_000)


def test_good_long_is_approved(snap, cfg, acct):
    d = evaluate_plan(plan(), snap, cfg, acct, NOW)
    assert d.approved, d.reasons
    assert d.action == "BUY"
    assert all(c.result == "PASS" for c in d.checklist)
    assert d.qty == 194  # capped by 20% max position size
    assert d.rr_ratio >= 2


def test_no_trade_plan_is_not_executed(snap, cfg, acct):
    d = evaluate_plan(plan(action="NO_TRADE"), snap, cfg, acct, NOW)
    assert not d.approved and d.action == "NO_TRADE"


def test_short_above_vwap_fails_trend(snap, cfg, acct):
    acct.shortable["ACME"] = True
    d = evaluate_plan(plan(action="SHORT", entry_trigger=102.9, stop=103.1, target=102.0), snap, cfg, acct, NOW)
    assert not d.approved
    assert "trend" in " ".join(d.reasons)


def test_stale_catalyst_fails(cfg, acct):
    last = intraday_bars()[-1].close
    s = build_snapshot("ACME", NOW, daily_bars(), intraday_bars(), last - 0.01, last + 0.01, news(30), cfg)
    d = evaluate_plan(plan(), s, cfg, acct, NOW)
    assert not d.approved and "catalyst" in " ".join(d.reasons)


def test_wide_spread_fails_liquidity(cfg, acct):
    last = intraday_bars()[-1].close
    s = build_snapshot("ACME", NOW, daily_bars(), intraday_bars(), last - 0.2, last + 0.2, news(), cfg)
    d = evaluate_plan(plan(), s, cfg, acct, NOW)
    assert not d.approved and "liquidity" in " ".join(d.reasons)


def test_missing_quote_means_no_trade(cfg, acct):
    s = build_snapshot("ACME", NOW, daily_bars(), intraday_bars(), None, None, news(), cfg)
    assert "quote" in s.missing
    d = evaluate_plan(plan(), s, cfg, acct, NOW)
    assert not d.approved and "missing data" in d.reasons[0]


def test_low_reward_risk_fails(snap, cfg, acct):
    d = evaluate_plan(plan(target=103.20), snap, cfg, acct, NOW)
    assert not d.approved and "reward_risk" in " ".join(d.reasons)


def test_target_is_capped_at_next_level(snap):
    # Entry below the 102.98 level: a target beyond it is capped to it.
    assert ck.effective_target(snap, "long", 102.5, 110.0) == 102.98


def test_stop_on_wrong_side_rejected(snap, cfg, acct):
    d = evaluate_plan(plan(stop=103.5), snap, cfg, acct, NOW)
    assert not d.approved


def test_low_confidence_rejected(snap, cfg, acct):
    d = evaluate_plan(plan(confidence=0.3), snap, cfg, acct, NOW)
    assert not d.approved and "confidence" in " ".join(d.reasons)


def test_outside_entry_window(snap, cfg, acct):
    late = NOW.replace(hour=15, minute=40)
    assert not evaluate_plan(plan(), snap, cfg, acct, late).approved
    early = NOW.replace(hour=9, minute=35)
    assert not evaluate_plan(plan(), snap, cfg, acct, early).approved


def test_daily_loss_limit(snap, cfg, acct):
    acct.equity = 97_900
    d = evaluate_plan(plan(), snap, cfg, acct, NOW)
    assert not d.approved and "daily loss" in " ".join(d.reasons)


def test_account_limits(snap, cfg, acct):
    acct.trades_today = cfg.risk.max_trades_per_day
    assert not evaluate_plan(plan(), snap, cfg, acct, NOW).approved
    acct.trades_today = 0
    acct.open_symbols = {"ACME"}
    assert not evaluate_plan(plan(), snap, cfg, acct, NOW).approved


def test_short_needs_borrow(cfg, acct):
    # Build a downtrend so the short passes trend; without borrow it is refused.
    bars = [b.__class__(200 - b.open, 200 - b.low, 200 - b.high, 200 - b.close, b.volume) for b in intraday_bars()]
    last = bars[-1].close
    s = build_snapshot("ACME", NOW, [Bar(101, 102, 100, 101, 2e6) for _ in range(30)], bars, last - 0.01, last + 0.01, news(), cfg)
    p = plan(action="SHORT", entry_trigger=96.95, stop=97.10, target=96.60)
    d = evaluate_plan(p, s, cfg, acct, NOW)
    assert not d.approved and "shortable" in " ".join(d.reasons)
    acct.shortable["ACME"] = True
    d = evaluate_plan(p, s, cfg, acct, NOW)
    assert d.approved, d.reasons
    assert d.action == "SHORT"


def test_position_size_by_risk(cfg):
    a = AccountState(equity=100_000, buying_power=1e9, start_of_day_equity=100_000)
    # $500 risk / $2.50 per share = 200 shares; 20% cap at $10 = 2000
    assert position_size(cfg, a, 10.0, 7.5) == 200
