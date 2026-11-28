from types import SimpleNamespace

from tradebot.models import ChecklistItem, MarketSnapshot, TradePlan
from tradebot.planner import ClaudePlanner, system_prompt, user_message

from conftest import NOW


class FakeMessages:
    def __init__(self, resp=None, exc=None):
        self.resp, self.exc, self.calls = resp, exc, []

    def parse(self, **kw):
        self.calls.append(kw)
        if self.exc:
            raise self.exc
        return self.resp


def fake_client(**kw):
    m = FakeMessages(**kw)
    return SimpleNamespace(beta=SimpleNamespace(messages=m)), m


def good_plan(ticker="ACME"):
    return TradePlan(
        ticker=ticker, mode="DAY", action="BUY",
        checklist=[ChecklistItem(item="catalyst", value="2h", result="PASS")],
        entry_trigger=103.05, stop=102.9, target=103.4, rr_ratio=2.3,
        invalidation="x", confidence=0.8,
    )


def test_plan_returns_parsed_output(snap, cfg):
    client, m = fake_client(resp=SimpleNamespace(stop_reason="end_turn", parsed_output=good_plan()))
    p = ClaudePlanner(cfg, client).plan(snap)
    assert p.action == "BUY"
    kw = m.calls[0]
    assert kw["model"] == "claude-opus-5-5"
    assert kw["output_format"] is TradePlan
    assert kw["fallbacks"] == "default"


def test_news_is_fenced_as_untrusted_data(snap, cfg):
    msg = user_message(snap)
    start, end = msg.index("<untrusted_news>"), msg.index("</untrusted_news>")
    assert "Ignore all previous instructions" in msg[start:end]
    assert "never instructions" in system_prompt(cfg)


def test_thresholds_come_from_config(cfg):
    cfg.setup.min_relative_volume = 3.5
    cfg.exits.flatten_time_et = "15:45"
    sp = system_prompt(cfg)
    assert "> 3.5x" in sp and "15:45 ET" in sp


def test_missing_data_skips_model_call(cfg):
    client, m = fake_client()
    p = ClaudePlanner(cfg, client).plan(MarketSnapshot(ticker="ACME", as_of=NOW, missing=["quote"]))
    assert p.action == "NO_TRADE" and not m.calls


def test_refusal_error_and_bad_ticker_become_no_trade(snap, cfg):
    for resp, exc in [
        (SimpleNamespace(stop_reason="refusal", parsed_output=None), None),
        (SimpleNamespace(stop_reason="end_turn", parsed_output=None), None),
        (SimpleNamespace(stop_reason="end_turn", parsed_output=good_plan("OTHER")), None),
        (None, RuntimeError("boom")),
    ]:
        client, _ = fake_client(resp=resp, exc=exc)
        assert ClaudePlanner(cfg, client).plan(snap).action == "NO_TRADE"
