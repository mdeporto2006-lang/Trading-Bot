import pytest

from tradebot.broker import trailed_stop
from tradebot.config import (
    LIVE_CONFIRMATION_PHRASE, Config, LiveTradingNotAllowed, assert_mode_allowed,
)


def test_defaults_are_paper_and_prompt_examples():
    c = Config()
    assert c.trading.mode == "paper"
    assert c.setup.min_relative_volume == 2.0
    assert c.setup.min_avg_daily_volume == 1_000_000
    assert c.setup.max_spread_pct == 0.10
    assert c.setup.min_reward_risk == 2.0
    assert c.exits.stop_atr_multiple == 1.5
    assert c.exits.flatten_time_et == "15:50"


def test_load_yaml(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("setup:\n  min_relative_volume: 3\ntrading:\n  watchlist: [AAPL]\n")
    c = Config.load(p)
    assert c.setup.min_relative_volume == 3 and c.trading.watchlist == ["AAPL"]
    assert c.setup.min_reward_risk == 2.0  # untouched defaults stay


def test_live_needs_env_confirmation(monkeypatch):
    c = Config()
    c.trading.mode = "live"
    monkeypatch.delenv("TRADEBOT_ALLOW_LIVE", raising=False)
    with pytest.raises(LiveTradingNotAllowed):
        assert_mode_allowed(c)
    monkeypatch.setenv("TRADEBOT_ALLOW_LIVE", "yes")
    with pytest.raises(LiveTradingNotAllowed):
        assert_mode_allowed(c)
    monkeypatch.setenv("TRADEBOT_ALLOW_LIVE", LIVE_CONFIRMATION_PHRASE)
    assert_mode_allowed(c)


def test_trailing_starts_after_1r_and_never_widens():
    # long: entry 100, stop 98 (R=2)
    assert trailed_stop("long", 100, 98, 98, 101.5, 1, 1) is None  # < 1R
    assert trailed_stop("long", 100, 98, 98, 102.5, 1, 1) == 100.5
    assert trailed_stop("long", 100, 98, 100.5, 102.2, 1, 1) is None  # would loosen
    # short: entry 100, stop 102
    assert trailed_stop("short", 100, 102, 102, 97.5, 1, 1) == 99.5
    assert trailed_stop("short", 100, 102, 99.0, 97.5, 1, 1) is None
