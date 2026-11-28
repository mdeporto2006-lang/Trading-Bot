"""Estimate what a Claude-planner backtest will cost before running it.

Price per million tokens (input, output). Opus 5.5 is $4 / $20.
Output is the big part: at `high` effort the planner thinks before writing its
JSON plan. 3,000 output tokens per call is a deliberately generous guess.
"""

from __future__ import annotations

from ..config import Config
from ..models import MarketSnapshot
from ..planner import system_prompt, user_message

PRICES = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}
OUTPUT_TOKENS_BY_EFFORT = {"low": 800, "medium": 1500, "high": 3000, "xhigh": 5000, "max": 8000}


def per_call(cfg: Config, sample: MarketSnapshot | None) -> dict:
    text = system_prompt(cfg) + (user_message(sample) if sample else "x" * 6000)
    in_tok = int(len(text) / 3.5) + 300  # rough; the schema adds a little
    out_tok = OUTPUT_TOKENS_BY_EFFORT.get(cfg.planner.effort, 3000)
    pin, pout = PRICES.get(cfg.planner.model, PRICES["claude-opus-5-5"])
    usd = in_tok / 1e6 * pin + out_tok / 1e6 * pout
    return {"model": cfg.planner.model, "effort": cfg.planner.effort, "input_tokens": in_tok,
            "output_tokens": out_tok, "usd": round(usd, 4)}


def estimate(cfg: Config, calls: int, sample: MarketSnapshot | None) -> dict:
    pc = per_call(cfg, sample)
    return {**pc, "calls": calls, "usd_per_call": pc["usd"], "usd_total": round(pc["usd"] * calls, 2)}
