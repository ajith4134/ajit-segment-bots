"""How many observations does one closed trade buy in bot-scorekeeper?

`evidence_weight_of` returns `abs(significance.standardised)` with no ceiling,
and `bot-scorekeeper` turns that into `max(1, round(weight))` repeated calls to
one `RateEstimator`. A trade that came out nine standard moves from the symbol's
own noise therefore counts as nine separate opinions, all of them the same
opinion about the same trade.

This measures the distribution on this project's own closed trades, exactly as
the part computes it:

    standardised = (realised_pnl / |entry_price x quantity|) / expected_noise
    expected_noise = daily_volatility x sqrt(holding_seconds / 86400)

Closed trades come from `position-recorder`'s journal; daily volatility is
measured off this project's captured tape, per symbol, as the standard deviation
of that session's trade-to-trade log returns scaled to a day (RL-063 -- the same
quantity `realised-vol-regressor` feeds the live part, re-measured here rather
than assumed).

Run: .venv/bin/python measurements/2026-09-16-evidence-weight-of-a-closed-trade/measure_evidence_weight_of_a_closed_trade.py
"""

from __future__ import annotations

import collections
import json
import math
import pathlib
import statistics
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from runtime.tape import read_payload, read_tape_index  # noqa: E402

STATE = pathlib.Path.home() / ".local/share/ajit-segment-bots"
TAPE = STATE / "tape/upstox"
SECONDS_IN_A_DAY = 86_400.0
QUANTILES = (0.5, 0.9, 0.99, 1.0)


def closed_trades():
    """Every closed trade the position journals hold, newest journal first."""
    for path in sorted(STATE.glob("journal.position-recorder*.sqlite")):
        if path.stat().st_size > 2_000_000_000:
            continue
        with path.open(errors="ignore") as lines:
            for line in lines:
                if '"closed-trade"' not in line:
                    continue
                try:
                    record = json.loads(line)
                except Exception:
                    continue
                if record.get("kind") == "closed-trade":
                    yield record["payload"]


def daily_volatility_of(symbol: str) -> float | None:
    """Standard deviation of one session's log returns, scaled to a day.

    The tape is written per instrument key, and a closed trade carries the
    trading symbol, so the key is resolved through the instrument master the
    same way every other reader resolves it.
    """
    directory = TAPE / symbol
    if not directory.is_dir():
        return None
    days = sorted(path.stem.split(".")[0] for path in directory.glob("*.index"))
    for day in reversed(days):
        index_path = directory / f"{day}.index"
        blob_path = directory / f"{day}.blob"
        if not index_path.exists() or index_path.stat().st_size == 0:
            continue
        prices, times = [], []
        for record in read_tape_index(index_path):
            try:
                payload = json.loads(read_payload(blob_path, record))
            except Exception:
                continue
            price = payload.get("last_traded_price")
            if price is None or float(price) <= 0:
                continue
            if prices and float(price) == prices[-1]:
                continue
            prices.append(float(price))
            times.append(int(record[0]))
        if len(prices) < 30:
            continue
        returns = [
            math.log(later / earlier)
            for earlier, later in zip(prices, prices[1:])
            if earlier > 0 and later > 0
        ]
        if len(returns) < 30:
            continue
        seconds = max(1e-9, (times[-1] - times[0]) / 1e9)
        per_observation = statistics.pstdev(returns)
        observations_per_day = len(returns) / seconds * SECONDS_IN_A_DAY
        return per_observation * math.sqrt(observations_per_day)
    return None


def main() -> int:
    trades = list(closed_trades())
    print(f"closed trades in the journals: {len(trades):,}\n")

    volatility_cache: dict[str, float | None] = {}
    weights, unmeasurable, flat = [], 0, 0
    by_symbol = collections.Counter()
    for trade in trades:
        notional = abs(trade["entry_price"] * trade["quantity"])
        holding = trade["holding_seconds"]
        if notional <= 0 or holding <= 0:
            unmeasurable += 1
            continue
        symbol = trade["symbol"]
        if symbol not in volatility_cache:
            volatility_cache[symbol] = daily_volatility_of(symbol)
        daily = volatility_cache[symbol]
        if daily is None or daily <= 0:
            unmeasurable += 1
            continue
        noise = daily * math.sqrt(holding / SECONDS_IN_A_DAY)
        if noise <= 0:
            unmeasurable += 1
            continue
        standardised = (trade["realised_pnl"] / notional) / noise
        if standardised == 0:
            flat += 1
            continue
        weights.append(abs(standardised))
        by_symbol[symbol] += 1

    print(f"measurable: {len(weights):,}   flat (no weight at all): {flat:,}   "
          f"no volatility or no holding time: {unmeasurable:,}")
    if not weights:
        print("nothing measurable -- no verdict")
        return 0

    ordered = sorted(weights)

    def quantile(fraction: float) -> float:
        return ordered[min(len(ordered) - 1, int(fraction * (len(ordered) - 1)))]

    print("\n|standardised| -- observations one closed trade buys")
    for fraction in QUANTILES:
        value = quantile(fraction)
        print(f"  p{fraction * 100:>5.1f}  {value:10.3f}   counted as "
              f"{max(1, round(value)):>4} observation(s)")

    total = sum(max(1, round(w)) for w in weights)
    print(f"\n{len(weights):,} trades are recorded as {total:,} observations "
          f"({total / len(weights):.2f} each)")
    heaviest = sorted(weights, reverse=True)[:10]
    print("the ten heaviest single trades: "
          + ", ".join(f"{w:,.0f}" for w in heaviest))
    share = sum(max(1, round(w)) for w in heaviest) / total
    print(f"they alone are {share:.1%} of every observation ever recorded")

    # What a ceiling would change, at the two the settings themselves suggest:
    # `learning_prior_weight` (4 -- what everything assumed before the first
    # trade is worth) and `learning_minimum_observations` (20 -- the sample at
    # which a rate is read as a frequency at all).
    print("\nwith a ceiling on what one trade can be worth")
    for ceiling in (4, 20):
        capped = [min(ceiling, w) for w in weights]
        capped_total = sum(max(1, round(w)) for w in capped)
        truncated = sum(1 for w in weights if w > ceiling)
        heaviest_share = sum(
            max(1, round(min(ceiling, w))) for w in heaviest
        ) / capped_total
        print(f"  ceiling {ceiling:>3}: {capped_total:,} observations from "
              f"{len(weights):,} trades ({capped_total / len(weights):.2f} each); "
              f"{truncated} trade(s) truncated ({truncated / len(weights):.1%}); "
              f"heaviest ten now {heaviest_share:.1%} of the record")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
