import argparse
import csv
import json
from datetime import datetime, timezone

from tradebot.breeder import genome as gm
from tradebot.breeder import halloffame as hf
from tradebot.breeder.data import ET, merge_archive, synthetic
from tradebot.breeder.evolve import Breeder, BreedOptions
from tradebot.paper import commands as pc


def _runs(data, seeds=(1, 2)):
    out = []
    for s in seeds:
        b = Breeder(data, BreedOptions(population=24, generations=3, seed=s, min_trades=8))
        b.evolve()
        sel = b.select()
        out.append((b, sel, {}))
    return out


def test_archive_grows_and_keeps_only_session_bars(tmp_path):
    def ts(d, hh, mm):
        return int(datetime(2026, 11, d, hh, mm, tzinfo=ET).timestamp())
    a = {"intraday": [[ts(2, 10, 0), 1, 2, 0.5, 1.5, 10], [ts(2, 3, 0), 1, 1, 1, 1, 1]], "daily": [["2026-11-02", 1, 2, 0, 1]]}
    b = {"intraday": [[ts(2, 10, 0), 9, 9, 9, 9, 9], [ts(3, 10, 0), 1, 2, 0.5, 1.5, 10]], "daily": [["2026-11-03", 1, 2, 0, 1]]}
    merge_archive("ES=F", 5, a, tmp_path)
    m = merge_archive("ES=F", 5, b, tmp_path)
    assert len(m["intraday"]) == 2  # the 3 am bar is outside the 09:30-16:00 window
    assert m["intraday"][0][1] == 9  # newer fetch wins
    assert [d[0] for d in m["daily"]] == ["2026-11-02", "2026-11-03"]


def test_hall_of_fame_nominates_scores_and_needs_forward_days(tmp_path):
    data = synthetic(["ES=F", "NQ=F", "CL=F"], days=30, seed=3)
    hof = hf.load(tmp_path / "hof.json")
    hf.update(hof, _runs(data), data, today="2026-11-28")
    assert hof["bots"], "nominees went in"
    for b in hof["bots"].values():
        assert b["stats"]["forward"]["trades"] == 0  # nothing after the nomination day yet
        assert not b["checks"]["forward"]["ok"] and b["status"] == "candidate"
        assert len(b["jitter_exp_r"]) == hf.RULES["jitter_n"]
        assert set(b["checks"]) == {"forward", "record", "costs", "weeks", "regimes", "seeds"}
    hf.save(hof, tmp_path / "hof.json")
    assert (tmp_path / "hof.md").read_text().startswith("# Hall of fame")
    # a second night on more data: old days are kept, new days count as forward
    more = synthetic(["ES=F", "NQ=F", "CL=F"], days=30, seed=3, end=datetime(2026, 12, 31).date())
    first = next(iter(hof["bots"].values()))
    first["first_nominated"] = "2026-11-27"
    hf.update(hof, [], more, today="2026-12-31")
    b = hof["bots"][first["key"]]
    assert any(d <= "2026-11-27" for d in b["days"]) and any(d > "2026-11-27" for d in b["days"])
    assert b["stats"]["forward"]["trades"] == len(hf._flat(b["days"], lambda d: d > "2026-11-27"))


def test_promotion_needs_every_check():
    b = {"first_nominated": "2026-01-01", "days": {}, "days_2tick": {}, "jitter_exp_r": [0.1] * 8}
    # 60 winning trades spread over 10 weeks, all after nomination
    import datetime as dt
    d0 = dt.date(2026, 1, 5)
    for k in range(60):
        day = (d0 + dt.timedelta(days=k // 6 * 7 + k % 5)).isoformat()
        b["days"].setdefault(day, []).append([0.5 if k % 5 else -1.0, 50.0 if k % 5 else -100.0])
        b["days_2tick"].setdefault(day, []).append([0.45 if k % 5 else -1.05, 45.0 if k % 5 else -105.0])
    regimes = {d: {"trend": "up" if i % 2 else "down", "vol": "high" if i % 3 else "low"} for i, d in enumerate(sorted(b["days"]))}
    checks, st = hf.evaluate(b, regimes, hf.RULES)
    assert all(c["ok"] for c in checks.values()), checks
    b["jitter_exp_r"] = [0.1, -0.2, -0.1, 0.1, -0.3, 0.2, -0.1, 0.0]
    checks, _ = hf.evaluate(b, regimes, hf.RULES)
    assert not checks["seeds"]["ok"] and checks["forward"]["ok"]


def _export(tmp_path, hof_path, run_path="nope.json", top=3):
    args = argparse.Namespace(hof=str(hof_path), run=run_path, top=top, promoted_only=False, allow_similar=True, bot_loss=250,
                              all_loss=600, max_open=6, max_trades=40)
    pc.export(args, tmp_path)
    return (tmp_path / "bots.txt").read_text()


def test_export_writes_every_gene_and_contract(tmp_path):
    data = synthetic(["ES=F", "CL=F"], days=30, seed=4)
    hof = hf.load(tmp_path / "h.json")
    hf.update(hof, _runs(data, (1, 2, 3)), data, today="2026-11-28")
    hf.save(hof, tmp_path / "h.json")
    txt = _export(tmp_path, tmp_path / "h.json")
    sections = [l for l in txt.splitlines() if l.startswith("[")]
    assert sections[0] == "[general]" and "[bot B1]" in sections and "[symbol ES=F]" in txt
    assert "nt_root=MES" in txt and "multiplier=5.0" in txt and "open_min=570" in txt
    for k in gm.SPACE:
        assert f"gene.{k}=" in txt
    meta = json.loads((tmp_path / "bots.json").read_text())
    assert list(meta["bots"])[0] == "B1" and meta["risk"]["bot_daily_loss_usd"] == 250
    # no two exported bots make exactly the same trades
    lists = [tuple(b["r_list"]) for b in meta["bots"].values()]
    assert len(set(lists)) == len(lists)


def test_export_falls_back_to_the_latest_run(tmp_path):
    txt = _export(tmp_path, tmp_path / "missing.json", "docs/breeder/latest.json", top=2)
    assert "[bot B1]" in txt and "status=candidate" in txt


def test_compare_flags_a_bot_far_below_its_backtest(tmp_path):
    data = synthetic(["ES=F"], days=30, seed=4)
    hof = hf.load(tmp_path / "h.json")
    hf.update(hof, _runs(data), data, today="2026-11-28")
    hf.save(hof, tmp_path / "h.json")
    _export(tmp_path, tmp_path / "h.json", top=1)
    meta = json.loads((tmp_path / "bots.json").read_text())
    b1 = meta["bots"]["B1"]
    cols = "logged_utc,account,bot,bot_id,bot_key,status,symbol,instrument,day,dir,entry_time,exit_time,entry,exit,stop,target,reason,pnl_usd,r,commission_usd".split(",")
    with (tmp_path / "paper_trades.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for k in range(30):  # 30 straight full losses
            w.writerow(["", "SimB1", "B1", b1["id"], b1["key"], "candidate", "ES=F", "MES 03-27", f"2026-12-{k % 28 + 1:02d}",
                        "long", "10:00:00", "10:30:00", 6000, 5990, 5990, 6010, "stop", -51.5, -1.03, 1.5])
    rep = pc.compare(tmp_path, replay=False)
    assert "WORSE than the backtest" in rep or "losing" in rep
    assert (tmp_path / "compare.md").exists()


def test_kill_and_resume(tmp_path, capsys):
    args = argparse.Namespace(paper_cmd="kill", dir=str(tmp_path))
    pc.run_paper(args)
    assert (tmp_path / "KILL").exists()
    pc.run_paper(argparse.Namespace(paper_cmd="status", dir=str(tmp_path)))
    assert "Kill switch: ON" in capsys.readouterr().out
    pc.run_paper(argparse.Namespace(paper_cmd="resume", dir=str(tmp_path)))
    assert not (tmp_path / "KILL").exists()


def test_export_skips_near_clones_by_default(tmp_path):
    a = pc.pick_bots("missing.json", "docs/breeder/latest.json", 3, False)
    b = pc.pick_bots("missing.json", "docs/breeder/latest.json", 3, False, allow_similar=True)
    kinds = {(x["genes"]["family"], tuple(x["genes"]["symbols"])) for x in a}
    assert len(kinds) == len(a) and len(b) >= len(a)
