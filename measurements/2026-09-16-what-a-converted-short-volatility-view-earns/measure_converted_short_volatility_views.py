"""What does buying the opposite option earn when the view was "this option is overpriced"?

`volatility-gap-detector` raises a SHORT candidate on a contract when its implied
volatility sits above the forecast -- "protection is expensive", a view on
*volatility*, and it says so in its evidence (`trades_volatility_not_direction`).
Both option segments only buy, so `instrument-selector` turns a short of a call
into a bought put and a short of a put into a bought call. That is right for a
directional view. For a volatility view it buys the same rich implied volatility
the detector said was overpriced.

Nothing downstream reads `trades_volatility_not_direction`, so this measures what
the conversion does on real data rather than arguing it:

- **candidates** from `trade-lifecycle-recorder`'s journal, volatility-gap only;
- the **named contract** and its **counterpart** -- the opposite type, same
  underlying and expiry, nearest strike this project's tape holds;
- the return of *buying* each over the horizon, from the tape, gross and after
  the round trip, one observation per contract per minute so a detector firing
  on every tick does not count one move a hundred times.

Run: .venv/bin/python measurements/2026-09-16-what-a-converted-short-volatility-view-earns/measure_converted_short_volatility_views.py
"""

from __future__ import annotations

import bisect
import collections
import pathlib
import statistics
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "measurements/2026-09-16-does-the-conviction-model-have-signal"))

from measure_signal_in_the_conviction_features import (  # noqa: E402
    TAPE,
    candidates,
    price_at,
    price_series,
)

DETECTOR = "volatility-gap-detector"
HORIZONS = (300.0, 900.0)
# The measured round trip on an option premium, from the 2026-09-16 conviction
# finding (0.853% of premium); a gross move smaller than this is not an edge.
ROUND_TRIP_FRACTION = 0.00853


def parse_contract(symbol: str):
    """('BANKNIFTY', 56100.0, 'PE', '29 SEP 26') or None for a non-option."""
    parts = symbol.split()
    if len(parts) < 4 or parts[2] not in ("CE", "PE"):
        return None
    try:
        strike = float(parts[1])
    except ValueError:
        return None
    return parts[0], strike, parts[2], " ".join(parts[3:])


def contracts_on_tape():
    """(underlying, type, expiry) -> sorted [(strike, symbol)] for every contract captured."""
    chain = collections.defaultdict(list)
    for directory in TAPE.iterdir():
        parsed = parse_contract(directory.name)
        if parsed is not None:
            underlying, strike, kind, expiry = parsed
            chain[(underlying, kind, expiry)].append((strike, directory.name))
    for strikes in chain.values():
        strikes.sort()
    return chain


def counterpart_of(symbol, chain):
    underlying, strike, kind, expiry = parse_contract(symbol)
    opposite = "PE" if kind == "CE" else "CE"
    strikes = chain.get((underlying, opposite, expiry))
    if not strikes:
        return None
    return min(strikes, key=lambda pair: abs(pair[0] - strike))[1]


def main() -> int:
    chain = contracts_on_tape()
    series_cache: dict[str, tuple] = {}

    def series(symbol):
        if symbol not in series_cache:
            series_cache[symbol] = price_series(symbol)
        return series_cache[symbol]

    seen_minutes = set()
    returns = collections.defaultdict(list)   # (state, horizon, which) -> [return]
    for row in candidates():
        if row.get("detector") != DETECTOR:
            continue
        symbol = row["symbol"]
        if parse_contract(symbol) is None:
            continue
        at = int(row["detected_at_ns"])
        minute = (symbol, at // 60_000_000_000)
        if minute in seen_minutes:
            continue
        seen_minutes.add(minute)
        state = (row.get("evidence") or {}).get("state")
        other = counterpart_of(symbol, chain)
        for horizon in HORIZONS:
            later = at + int(horizon * 1e9)
            for which, name in (("named", symbol), ("counterpart", other)):
                if name is None:
                    continue
                times, prices = series(name)
                if not times or times[-1] < later:
                    continue
                entry, exit_ = price_at(times, prices, at), price_at(times, prices, later)
                if not entry or not exit_:
                    continue
                # A price older than the horizon itself says nothing about this moment.
                if times[max(0, bisect.bisect_right(times, at) - 1)] < at - int(horizon * 1e9):
                    continue
                returns[(state, horizon, which)].append(exit_ / entry - 1.0)

    print(f"{DETECTOR}: one observation per contract per minute\n")
    print(f"{'state':<14}{'horizon':>8}  {'bought':<12}{'n':>7}{'mean gross':>12}"
          f"{'median':>10}{'mean net':>10}{'wins net':>10}")
    for key in sorted(returns, key=lambda k: (str(k[0]), k[1], k[2])):
        values = returns[key]
        if len(values) < 30:
            continue
        net = [v - ROUND_TRIP_FRACTION for v in values]
        wins = sum(1 for v in net if v > 0) / len(net)
        state, horizon, which = key
        print(f"{str(state):<14}{horizon:>8.0f}  {which:<12}{len(values):>7,}"
              f"{statistics.fmean(values):>12.3%}{statistics.median(values):>10.3%}"
              f"{statistics.fmean(net):>10.3%}{wins:>10.1%}")
    print(
        "\nimplied-rich is a SHORT candidate: instrument-selector buys the counterpart.\n"
        "implied-cheap is a LONG candidate: instrument-selector buys the named contract."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
