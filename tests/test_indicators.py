from tradebot import indicators as ind
from tradebot.indicators import Bar


def test_vwap_weights_by_volume():
    bars = [Bar(10, 10, 10, 10, 100), Bar(20, 20, 20, 20, 300)]
    assert ind.vwap(bars) == 17.5


def test_vwap_none_without_volume():
    assert ind.vwap([]) is None
    assert ind.vwap([Bar(1, 1, 1, 1, 0)]) is None


def test_atr_constant_range():
    bars = [Bar(100, 101, 99, 100, 1) for _ in range(20)]
    assert abs(ind.atr(bars, 14) - 2.0) < 1e-9


def test_atr_needs_enough_bars():
    assert ind.atr([Bar(1, 1, 1, 1, 1)] * 5, 14) is None


def test_relative_volume_is_pace_adjusted():
    # Half the session gone, half a normal day traded -> 1x
    assert ind.relative_volume(500, 1000, 0.5) == 1.0
    assert ind.relative_volume(500, 1000, 0.0) is None


def test_pivots_and_nearest_levels():
    closes = [5, 4, 3, 2, 3, 4, 5, 6, 7, 6, 5, 4, 5]
    bars = [Bar(c, c, c, c, 1) for c in closes]
    lows, highs = ind.pivot_levels(bars, window=2)
    assert 2 in lows and 7 in highs
    sup, res = ind.nearest_levels(5.5, lows, highs, extra=[4.0])
    assert sup[0] == 4.0 and res[0] == 7.0


def test_ema():
    assert ind.ema([1, 2, 3], 5) is None
    assert ind.ema([2] * 10, 5) == 2
