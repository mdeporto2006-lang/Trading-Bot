from datetime import timedelta
from types import SimpleNamespace

import pytest

from conftest import NOW, intraday_bars
from test_planner import good_plan
from tradebot.indicators import Bar
from tradebot.improve.outcomes import score_journal
from tradebot.improve.replay import replay
from tradebot.improve.reviewer import Proposal, ParamChange, approve, run_review
from tradebot.improve.tuning import TuningError, TuningStore, apply_overrides, validate_changes
from tradebot.journal import Journal
from tradebot.models import RiskDecision
from tradebot.planner import system_prompt
from tradebot.simulate import simulate_bracket


def b(lo, hi, c=None):
    c = (lo + hi) / 2 if c is None else c
    return Bar(c, hi, lo, c, 0)


def test_sim_target_stop_untriggered_and_both_in_one_bar():
    assert simulate_bracket("long", 100, 99, 102, [b(99.5, 100.5), b(100, 102.1)], 1, 1).exit_reason == "target"
    r = simulate_bracket("long", 100, 99, 102, [b(99.5, 100.5), b(98.9, 100)], 1, 1)
    assert r.exit_reason == "stop" and r.r_multiple == -1
    assert not simulate_bracket("long", 100, 99, 102, [b(98, 99.9)], 1, 1).triggered
    # A bar touching both stop and target counts as a stop (conservative).
    assert simulate_bracket("long", 100, 99, 102, [b(98.5, 102.5)], 1, 1).exit_reason == "stop"


def test_sim_trailing_locks_profit_and_short_mirror():
    bars = [b(99.8, 100.2, 100.1), b(100.5, 101.6, 101.5), b(100.4, 101.0)]
    r = simulate_bracket("long", 100, 99, 105, bars, 1, 1)
    assert r.exit_reason == "stop" and r.r_multiple == pytest.approx(0.5)
    s = simulate_bracket("short", 100, 101, 98, [b(99.8, 100.1), b(97.9, 99)], 1, 1)
    assert s.exit_reason == "target" and s.r_multiple == 2


def test_tuning_bounds_and_forbidden_settings(cfg):
    with pytest.raises(TuningError):
        validate_changes({"setup.min_reward_risk": 1.5})  # looser than owner's 2:1
    with pytest.raises(TuningError):
        validate_changes({"risk.risk_per_trade_pct": 5})  # risk limits are not tunable
    with pytest.raises(TuningError):
        validate_changes({"trading.mode": 1})
    new = apply_overrides(cfg, {"setup.min_relative_volume": 3}, ["avoid gaps"])
    assert new.setup.min_relative_volume == 3 and cfg.setup.min_relative_volume == 2
    assert "avoid gaps" in system_prompt(new)


def test_store_versions_and_rollback(tmp_path, cfg):
    st = TuningStore(str(tmp_path))
    assert st.adopt({"setup.min_relative_volume": 3}, [], "a") == 1
    assert st.adopt({"risk.min_confidence": 0.7}, ["x"], "b") == 2
    c = st.layer(cfg)
    assert c.setup.min_relative_volume == 3 and c.risk.min_confidence == 0.7
    assert st.rollback() == 1
    assert st.layer(cfg).risk.min_confidence == cfg.risk.min_confidence
    assert st.rollback(0) == 0
    assert st.layer(cfg).setup.min_relative_volume == 2


def journal_with_outcomes(tmp_path, snap, cfg, n=25, win_every=2):
    """n identical approved plans; every `win_every`-th one hits target."""
    j = Journal(str(tmp_path / "trades.jsonl"))
    for i in range(n):
        j.record(NOW - timedelta(days=1, minutes=i), ticker="ACME",
                 snapshot=snap.model_dump(mode="json"),
                 plan=good_plan().model_dump(mode="json") | {"confidence": 0.65 if i % win_every else 0.9},
                 decision=RiskDecision(ticker="ACME", approved=True, action="BUY").model_dump(mode="json"),
                 order_id=None)
    win = [b(103.0, 103.1), b(103.0, 103.5)]
    loss = [b(103.0, 103.1), b(102.8, 103.0)]

    class Bars:
        calls = 0

        def minute_bars(self, t, s, e):
            Bars.calls += 1
            return win if (Bars.calls - 1) % win_every == 0 else loss

    assert score_journal(j, Bars(), cfg, NOW) == n
    assert score_journal(j, Bars(), cfg, NOW) == 0  # idempotent
    return j


def test_replay_shows_tighter_confidence_helps(tmp_path, snap, cfg):
    j = journal_with_outcomes(tmp_path, snap, cfg, n=50)
    base = replay(cfg, j.records(), j.outcomes())
    tight = replay(apply_overrides(cfg, {"risk.min_confidence": 0.8}), j.records(), j.outcomes())
    assert base.trades == 50 and tight.trades == 25
    assert tight.expectancy_r > base.expectancy_r


def fake_reviewer(proposal):
    m = SimpleNamespace(parse=lambda **kw: SimpleNamespace(stop_reason="end_turn", parsed_output=proposal))
    return SimpleNamespace(beta=SimpleNamespace(messages=m))


def test_review_pass_needs_approval_then_adopts(tmp_path, snap, cfg):
    j = journal_with_outcomes(tmp_path, snap, cfg, n=50)
    st = TuningStore(str(tmp_path / "tuning"))
    prop = Proposal(summary="low-confidence plans lose",
                    changes=[ParamChange(param="risk.min_confidence", value=0.8, rationale="data")], lessons=[])
    p = run_review(cfg, j, st, now=NOW, client=fake_reviewer(prop))
    assert p["status"] == "passed"
    assert st.active()["version"] == 0  # not adopted without approval
    assert approve(st, p["id"]) == 1
    assert st.layer(cfg).risk.min_confidence == 0.8


def test_review_rejects_out_of_bounds_and_failed_cannot_be_approved(tmp_path, snap, cfg):
    j = journal_with_outcomes(tmp_path, snap, cfg, n=50)
    st = TuningStore(str(tmp_path / "tuning"))
    bad = Proposal(summary="", changes=[ParamChange(param="risk.risk_per_trade_pct", value=3, rationale="")], lessons=[])
    p = run_review(cfg, j, st, now=NOW, client=fake_reviewer(bad))
    assert p["status"] == "rejected"
    worse = Proposal(summary="", changes=[ParamChange(param="setup.min_relative_volume", value=6, rationale="")], lessons=[])
    p2 = run_review(cfg, j, st, now=NOW, client=fake_reviewer(worse))
    assert p2["status"] == "failed"  # filters out every trade
    for pid in (p["id"], p2["id"]):
        with pytest.raises(TuningError):
            approve(st, pid)


def test_auto_apply_only_when_enabled(tmp_path, snap, cfg):
    cfg.improvement.auto_apply = True
    j = journal_with_outcomes(tmp_path, snap, cfg, n=50)
    st = TuningStore(str(tmp_path / "tuning"))
    prop = Proposal(summary="", changes=[ParamChange(param="risk.min_confidence", value=0.8, rationale="")], lessons=[])
    p = run_review(cfg, j, st, now=NOW, client=fake_reviewer(prop))
    assert p["status"] == "adopted" and st.active()["version"] == 1
