import json
import random
from datetime import date

from tradebot.breeder import genome as gm
from tradebot.breeder.data import Session, shuffled, split_days, synthetic
from tradebot.breeder.evolve import Breeder, BreedOptions, fitness, passes
from tradebot.breeder.report import build, html, markdown
from tradebot.breeder.sim import metrics, simulate_session


def _session(bars, atr=10.0, trend=1.0):
    t = [570 + 5 * i for i in range(len(bars))]
    o, h, l, c = (list(x) for x in zip(*bars))
    return Session("ES=F", date(2026, 11, 2), 5, t, o, h, l, c, [100.0] * len(bars), 570, 960,
                   prev_close=o[0], atr=atr, trend={5: trend, 10: trend, 20: trend, 60: trend})


def _genes(**kw):
    g = gm.textbook("intraday_mom", random.Random(1))
    g.update(symbols=["ES=F"], mom_window_min=15, mom_threshold_atr=0.0, mom_from="open", stop_mode="atr",
             stop_atr=0.5, target_r=1.0, entry_start_min=0, entry_end_min=30)
    g.update(kw)
    return g


def test_mutation_and_crossover_stay_in_the_space():
    rng = random.Random(3)
    a, b = gm.random_genome(rng, "orb"), gm.random_genome(rng, "vwap_revert")
    for _ in range(200):
        c, notes = gm.mutate(gm.crossover(a, b, rng), rng)
        assert notes and c["symbols"]
        for k, g in gm.SPACE.items():
            if g.kind == "choice":
                assert c[k] in g.values
            else:
                assert g.lo <= c[k] <= g.hi
        a = c


def test_signal_acts_on_next_bar_and_stop_wins_ties():
    # 3 bars up (signal on bar 2 close), bar 3 opens 100.5, then a bar that hits both stop and target.
    bars = [(100, 100.5, 99.9, 100.2), (100.2, 100.6, 100.1, 100.4), (100.4, 100.8, 100.3, 100.5),
            (100.5, 100.6, 100.4, 100.5), (100.5, 120, 80, 100)] + [(100, 100.1, 99.9, 100)] * 70
    tr = simulate_session(_genes(), _session(bars))
    assert len(tr) == 1
    t = tr[0]
    assert t.entry_t == 585 and t.entry == 100.5 + 0.25  # next bar open + 1 tick slippage
    assert t.reason == "stop" and t.pnl < 0  # both hit in one bar: the stop counts


def test_target_needs_a_trade_through():
    bars = [(100, 100.5, 99.9, 100.2), (100.2, 100.6, 100.1, 100.4), (100.4, 100.8, 100.3, 100.5),
            (100.5, 100.6, 100.4, 100.5)]
    # target = 100.75 + 5 = 105.75: touching exactly doesn't fill, one tick through does
    touch = bars + [(100.5, 105.75, 100.4, 101)] + [(101, 101.1, 100.9, 101)] * 70
    through = bars + [(100.5, 106.0, 100.4, 101)] + [(101, 101.1, 100.9, 101)] * 70
    assert simulate_session(_genes(), _session(touch))[0].reason != "target"
    assert simulate_session(_genes(), _session(through))[0].reason == "target"


def test_gates_reject_high_win_rate_that_loses_money():
    fake = {"trades": 100, "wins": 90, "win_rate": 0.9, "exp_r": -0.05, "pf": 0.8}
    real = {"trades": 100, "wins": 55, "win_rate": 0.55, "exp_r": 0.2, "pf": 1.5}
    assert not passes(fake, 30, True) and passes(real, 30, True)
    assert fitness(real, 30, True) > fitness(fake, 30, True)
    thin = {"trades": 100, "wins": 70, "win_rate": 0.7, "exp_r": 0.01, "pf": 1.02}
    assert not passes(thin, 30, strict=True) and passes(thin, 30)


def test_shuffle_keeps_days_and_sizes():
    data = synthetic(["ES=F"], days=5)
    sh = shuffled(data)
    a, b = data["ES=F"][0], sh["ES=F"][0]
    assert a.day == b.day and len(a) == len(b) and a.c != b.c


def test_end_to_end_breed_and_report_keeps_holdout_out_of_breeding():
    data = synthetic(["ES=F", "CL=F"], days=24)
    br = Breeder(data, BreedOptions(population=16, generations=3, min_trades=5, finalists=5))
    br.evolve()
    sel = br.select()
    train, val, hold = split_days(data)
    assert not set(train) & set(hold) and max(train) < min(val) <= max(val) < min(hold)
    champ = br.bots[sel["champion"]]
    assert "holdout" in champ and sel["lineage"][0] == champ["id"]
    for bid in sel["lineage"]:
        for p in br.bots[bid]["parents"]:
            assert br.bots[p]["gen"] < br.bots[bid]["gen"]
    rep = build(br, sel, [], None, {"source": "synthetic", "interval": 5, "symbols": list(data), "sessions": 24,
                                     "first_day": "x", "last_day": "y", "warnings": []})
    json.dumps(rep, default=str)
    assert "Holdout" in markdown(rep)
    page = html(rep)
    assert "/*BREEDER_DATA*/null" not in page and sel["champion"] in page
