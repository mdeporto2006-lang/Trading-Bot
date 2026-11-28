import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest
import yaml

from tradebot.broker import DryRunBroker, OpenPosition
from tradebot.dashboard.server import make_handler
from tradebot.dashboard.state import DashboardState
from tradebot.journal import Journal
from tradebot.planner import ClaudePlanner
from tradebot.runner import Runner
from tradebot.telemetry import read_status, tail_events

from conftest import NOW
from test_planner import fake_client, good_plan
from test_runner import FakeData
from types import SimpleNamespace


class HeldBroker(DryRunBroker):
    """Dry-run broker that reports one open long position with a stop order."""

    def positions(self):
        return [OpenPosition("ACME", 10, 103.0, 104.5)]

    def current_stop(self, symbol):
        return ("stop-1", 102.9)


def run_bot(snap, cfg, tmp_path, broker_cls=DryRunBroker):
    client, _ = fake_client(resp=SimpleNamespace(stop_reason="end_turn", parsed_output=good_plan()))
    j = Journal(str(tmp_path / "journal" / "trades.jsonl"))
    r = Runner(cfg, FakeData(snap), ClaudePlanner(cfg, client), broker_cls(cfg), j, clock=lambda: NOW)
    return r, j


def write_config(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump({
        "trading": {"journal_path": str(tmp_path / "journal" / "trades.jsonl"), "watchlist": ["ACME"]},
        "improvement": {"dir": str(tmp_path / "tuning")},
    }))
    return str(p)


def test_runner_writes_status_and_events(snap, cfg, tmp_path):
    r, j = run_bot(snap, cfg, tmp_path)
    r.scan_once(["ACME"])
    status = read_status(j.path.parent)
    assert status["phase"] == "entry_window"
    assert status["account"]["equity"] == 100_000
    assert status["limits"]["trades_today"] == 1
    kinds = [e["kind"] for e in tail_events(j.path.parent)]
    assert kinds == ["scan", "order", "decision"]


def test_manage_publishes_positions_with_trailed_stop(snap, cfg, tmp_path):
    r, j = run_bot(snap, cfg, tmp_path, HeldBroker)
    r.scan_once(["ACME"])
    r.manage_once()
    pos = read_status(j.path.parent)["positions"][0]
    assert pos["symbol"] == "ACME" and pos["direction"] == "long" and pos["managed"]
    assert pos["initial_stop"] == 102.9 and pos["target"] is not None
    assert pos["unrealized_pnl"] == pytest.approx(15.0)


def test_state_recomputes_checklist_for_no_trade(snap, cfg, tmp_path):
    client, _ = fake_client(resp=SimpleNamespace(stop_reason="refusal", parsed_output=None))
    j = Journal(str(tmp_path / "journal" / "trades.jsonl"))
    Runner(cfg, FakeData(snap), ClaudePlanner(cfg, client), DryRunBroker(cfg), j, clock=lambda: NOW).scan_once(["ACME"])
    st = DashboardState(write_config(tmp_path)).build(now=NOW)
    [scan] = st["scans"]
    assert scan["plan"]["action"] == "NO_TRADE"
    assert [c["item"] for c in scan["code_checklist"]] == [
        "catalyst", "relative_volume", "liquidity", "trend", "clean_level", "reward_risk"]
    assert scan["code_checklist_source"].startswith("display")
    assert st["watchlist"] == ["ACME"] and st["demo"] is False


@pytest.fixture
def server(snap, cfg, tmp_path):
    r, _ = run_bot(snap, cfg, tmp_path)
    r.scan_once(["ACME"])
    state = DashboardState(write_config(tmp_path))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state, "secret-token", demo=False))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def call(url, token=None, body=None):
    req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
                                 headers={"X-Token": token} if token else {})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_server_requires_token(server):
    code, html = call(server + "/")
    assert code == 200 and b"Tradebot Control Room" in html
    assert call(server + "/api/state")[0] == 401
    assert call(server + "/api/state", "wrong")[0] == 401
    code, body = call(server + "/api/state", "secret-token")
    assert code == 200 and json.loads(body)["scans"][0]["ticker"] == "ACME"
    assert call(server + "/api/rollback", None, {"to": 0})[0] == 401


def test_server_refuses_bad_approve_and_rollback(server):
    assert call(server + "/api/approve", "secret-token", {"id": "../etc"})[0] == 400
    assert call(server + "/api/approve", "secret-token", {"id": "nope"})[0] == 409
    assert call(server + "/api/rollback", "secret-token", {"to": None})[0] == 409  # already at v0
