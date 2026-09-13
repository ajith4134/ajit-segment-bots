#!/usr/bin/env python3
"""How far an NSE option pulls back from its high before making a new one.

`profit-lock` trails a stop behind the best price by "how far winning trades on this
symbol ordinarily retrace before continuing", and uses a prior until it has measured
that per symbol. The prior was fitted to crypto on 2026-08-23 ("below the 1.5% largest
intraday move measured today"): 1%. This measures the Indian equivalent on this
project's own tape.

Definition, matching the part: walk each option contract's session of last-traded
prices; each time the price makes a new session high, the episode since the previous
high closes, and its retracement is the deepest fall below that previous high. Only
episodes that ended in a new high are counted -- a pullback followed by continuation,
which is exactly the noise a trailing stop must not be inside. Quantile 0.80 is the
one `profit-lock` uses (RETRACEMENT_QUANTILE).

    .venv/bin/python measurements/2026-09-13-indian-option-retracements/measure_retracements.py
"""
import json
import pathlib
import statistics
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
from runtime.tape import read_payload, read_tape_index  # noqa: E402

TAPE = pathlib.Path.home() / ".local/share/ajit-segment-bots/tape/upstox"
DAYS = ("2026-09-07", "2026-09-08")


def is_an_option(name: str) -> bool:
    parts = name.split()
    return len(parts) >= 4 and parts[2] in ("CE", "PE")


def quantile(values, q):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def main() -> int:
    retracements, contracts, sessions, prints = [], set(), 0, 0
    for root in sorted(p for p in TAPE.iterdir() if is_an_option(p.name)):
        for day in DAYS:
            index, blob = root / f"{day}.index", root / f"{day}.blob"
            if not index.exists():
                continue
            prices = []
            for record in read_tape_index(index):
                try:
                    price = json.loads(read_payload(blob, record)).get("last_traded_price")
                except (ValueError, OSError):
                    continue
                if price and (not prices or price != prices[-1]):
                    prices.append(float(price))
            if len(prices) < 3:
                continue
            sessions += 1
            contracts.add(root.name)
            prints += len(prices)
            high, deepest = prices[0], 0.0
            for price in prices[1:]:
                if price > high:
                    if deepest > 0:
                        retracements.append(deepest)
                    high, deepest = price, 0.0
                else:
                    deepest = max(deepest, (high - price) / high)
    print(f"tape {', '.join(DAYS)}; {len(contracts)} option contracts, {sessions} sessions, "
          f"{prints:,} distinct prints")
    print(f"continuation retracements measured: {len(retracements):,}")
    for q in (0.50, 0.80, 0.90, 0.95, 0.99):
        print(f"  p{int(q * 100):<3d} {quantile(retracements, q):.4%}")
    print(f"  mean {statistics.fmean(retracements):.4%}")
    print(f"  share of retracements deeper than the crypto prior of 1%: "
          f"{sum(1 for r in retracements if r > 0.01) / len(retracements):.1%}")
    print(f"  share deeper than the 10% maximum trail: "
          f"{sum(1 for r in retracements if r > 0.10) / len(retracements):.1%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
