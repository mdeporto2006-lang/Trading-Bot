"""Compare the C# bot's trades (parity_out.json) with the Python sim's (parity_in.json).

Exits 1 on any difference, printing the first few."""

from __future__ import annotations

import json
import math
import sys


def close(a, b, tol=1e-6) -> bool:
    if a is None or b is None:
        return a is b
    return math.isclose(a, b, rel_tol=tol, abs_tol=tol)


def main(inp: str, out: str) -> int:
    exp = json.load(open(inp))
    got = json.load(open(out))
    bad, n = [], 0
    key = lambda r: (r["day"], r["symbol"], r["entry_t"])
    for gid, rows in exp["expected"].items():
        a = sorted(rows, key=key)
        b = sorted(got["trades"].get(gid, []), key=key)
        n += len(a)
        if len(a) != len(b):
            bad.append(f"{gid}: python {len(a)} trades, C# {len(b)}")
            for x, y in zip(a, b):
                if key(x) != key(y):
                    bad.append(f"   first difference: python {x}  C# {y}")
                    break
            continue
        for x, y in zip(a, b):
            diffs = [k for k in ("symbol", "day", "dir", "entry_t", "exit_t", "reason") if x[k] != y[k]]
            diffs += [k for k in ("entry", "exit", "stop", "target", "pnl", "r") if not close(x[k], y[k])]
            if diffs:
                bad.append(f"{gid} {x['symbol']} {x['day']}: {', '.join(diffs)} differ\n   python {x}\n   C#     {y}")
                break
    for i, (e, g) in enumerate(zip(exp["daily_checks"], got["daily"])):
        for k in ("prev_close", "atr"):
            if not close(e[k], g[k], 1e-9):
                bad.append(f"daily check {i}: {k} python {e[k]} C# {g[k]}")
        if set(e["trend"]) != set(g["trend"]) or any(not close(e["trend"][k], g["trend"][k], 1e-9) for k in e["trend"]):
            bad.append(f"daily check {i}: trend python {e['trend']} C# {g['trend']}")
    if bad:
        print(f"PARITY FAILED ({len(bad)} problems, {n} trades checked):")
        print("\n".join(bad[:25]))
        return 1
    print(f"parity OK: {len(exp['expected'])} bots, {n} trades identical, {len(exp['daily_checks'])} daily checks")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
