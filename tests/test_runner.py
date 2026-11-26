from tradebot.broker import DryRunBroker
from tradebot.journal import Journal
from tradebot.runner import Runner

from conftest import NOW
from test_planner import fake_client, good_plan
from tradebot.planner import ClaudePlanner
from types import SimpleNamespace


class FakeData:
    def __init__(self, snap):
        self.snap = snap

    def snapshot(self, ticker, now):
        return self.snap


def make(snap, cfg, tmp_path, now=NOW):
    client, _ = fake_client(resp=SimpleNamespace(stop_reason="end_turn", parsed_output=good_plan()))
    broker = DryRunBroker(cfg)
    j = Journal(str(tmp_path / "trades.jsonl"))
    return Runner(cfg, FakeData(snap), ClaudePlanner(cfg, client), broker, j, clock=lambda: now), broker, j


def test_scan_places_order_and_journals(snap, cfg, tmp_path):
    r, broker, j = make(snap, cfg, tmp_path)
    [(plan, d)] = r.scan_once(["acme"])
    assert d.approved and len(broker.orders) == 1
    assert j.trades_on(NOW) == 1
    assert j.open_trades()["ACME"]["initial_stop"] == 102.9
    # Second scan skips the ticker: already ordered.
    assert r.scan_once(["ACME"]) == []


def test_plan_only_places_nothing(snap, cfg, tmp_path):
    r, broker, j = make(snap, cfg, tmp_path)
    [(plan, d)] = r.scan_once(["ACME"], place_orders=False)
    assert d.approved and broker.orders == [] and j.trades_on(NOW) == 0


def test_flattens_at_cutoff(snap, cfg, tmp_path):
    r, broker, _ = make(snap, cfg, tmp_path)
    r.scan_once(["ACME"])
    r.now = lambda: NOW.replace(hour=15, minute=50)
    assert r.manage_once() is True
    assert broker.orders == []
