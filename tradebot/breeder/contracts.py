"""Futures contract specs used by the breeder.

Prices come from Yahoo's continuous front-month series (ES=F and so on), but
P&L is counted per MICRO contract where one exists (MES, MNQ, ...), because
that is the size a small account would actually trade. Costs are per
contract: a round-trip commission plus exchange fees, and slippage in ticks on
every stop or market fill (limit targets fill at their price).

The day session is the window the bot trades in, in ET. Futures trade almost
24 hours, but most of the volume (and the published intraday research) is in
these windows. Everything is flat at the end of the window: no overnight risk.

Alpaca does not trade futures. Running these bots live would need a futures
broker (for example Interactive Brokers, Tradovate or NinjaTrader).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Contract:
    symbol: str  # Yahoo continuous symbol
    name: str
    trade_as: str  # the contract the P&L is counted in
    multiplier: float  # dollars per 1.00 move in price, for `trade_as`
    tick: float  # minimum price move
    commission_rt: float  # dollars per contract, round trip, incl. exchange fees
    session_et: tuple[str, str]  # day session the bot trades, ET

    @property
    def tick_value(self) -> float:
        return self.tick * self.multiplier


CONTRACTS: dict[str, Contract] = {c.symbol: c for c in [
    Contract("ES=F", "S&P 500 E-mini", "MES (micro)", 5.0, 0.25, 1.50, ("09:30", "16:00")),
    Contract("NQ=F", "Nasdaq-100 E-mini", "MNQ (micro)", 2.0, 0.25, 1.50, ("09:30", "16:00")),
    Contract("YM=F", "Dow E-mini", "MYM (micro)", 0.5, 1.0, 1.50, ("09:30", "16:00")),
    Contract("RTY=F", "Russell 2000 E-mini", "M2K (micro)", 5.0, 0.10, 1.50, ("09:30", "16:00")),
    Contract("CL=F", "WTI crude oil", "MCL (micro)", 100.0, 0.01, 1.50, ("09:00", "14:30")),
    Contract("GC=F", "Gold", "MGC (micro)", 10.0, 0.10, 1.50, ("08:20", "13:30")),
    Contract("ZN=F", "10-year T-note", "ZN (full size)", 1000.0, 1 / 64, 4.50, ("08:20", "15:00")),
    Contract("6E=F", "Euro FX", "M6E (micro)", 12_500.0, 0.0001, 1.00, ("08:20", "15:00")),
]}

UNIVERSE: list[str] = list(CONTRACTS)

# The NinjaTrader instrument root each bred symbol is paper traded as (one contract, micro where
# one exists). The multipliers above are for these, so the bot's dollar P&L matches the backtest.
NT_ROOT: dict[str, str] = {"ES=F": "MES", "NQ=F": "MNQ", "YM=F": "MYM", "RTY=F": "M2K", "CL=F": "MCL",
                           "GC=F": "MGC", "ZN=F": "ZN", "6E=F": "M6E"}


def hhmm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)
