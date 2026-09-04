"""Why no paper order has ever filled: what feed-jump-detector calls a jump.

paper-fill-simulator refuses to fill any order on a symbol feed-jump-detector
has flagged, and live on 2026-09-04 every order it saw was either refused for a
feed jump (54) or still held in flight (57) -- filled 0, across 111 orders.

This replays what the detector does against the captured Upstox candle tape: a
closed bar's open against the previous closed bar's close for the same
(venue, symbol), flagged when the move exceeds feed_jump_threshold_fraction.

Only `I1` bars are read, because that is all the detector ever sees:
broker-candle-bridge republishes `upstox_candle_interval` alone. The feed
bundles a daily bar beside the minute bar in one message and the bridge used to
pass both through, which is a separate defect already fixed on 2026-09-02 --
reading the raw tape without that filter overstates this one roughly twofold.

The threshold it is measured against was fitted to crypto perpetuals
(BTCUSDT, ETHUSDT, DOGEUSDT, PUMPUSDT, TRUMPUSDT on 2026-08-23) and its own
note says to re-measure when the universe widens. This is that measurement.
"""

from __future__ import annotations

import collections
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from runtime.tape import read_tape_index

TAPE = pathlib.Path.home() / ".local/share/ajit-segment-bots/tape/upstox"
# feed_jump_threshold_fraction as it stands: 0.5% of price, fitted to perpetuals.
CRYPTO_FITTED_FRACTION = 0.005
# The one interval broker-candle-bridge republishes (upstox_candle_interval).
INTERVAL_ON_THE_WIRE = "I1"


def exchange_of(instrument_key: str) -> str:
    """NSE_FO, NSE_INDEX, ... -- the tape is keyed by Upstox instrument key, and
    the alphabetical head of it is all commodities, so nothing here is read
    without splitting by exchange first."""
    return instrument_key.split("|", 1)[0]


def close_to_open_moves(directory: pathlib.Path) -> list[float]:
    """Every |open - previous close| / previous close on one symbol's minute bars."""
    index_path = next(directory.glob("*.candle.index"), None)
    if index_path is None:
        return []
    records = read_tape_index(index_path)
    if len(records) == 0:
        return []

    moves: list[float] = []
    previous_close: float | None = None
    with open(index_path.with_suffix(".blob"), "rb") as blob:
        for record in records:
            blob.seek(int(record["blob_offset"]))
            try:
                candle = json.loads(blob.read(int(record["blob_length"])))
            except ValueError:
                continue
            if candle.get("interval") != INTERVAL_ON_THE_WIRE:
                continue
            open_price, close_price = candle.get("open"), candle.get("close")
            if not open_price or not close_price or close_price <= 0:
                continue
            if previous_close is not None and previous_close > 0:
                moves.append(abs(open_price - previous_close) / previous_close)
            previous_close = close_price
    return moves


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(fraction * len(ordered)))]


def main() -> int:
    moves_by_exchange: dict[str, list[float]] = collections.defaultdict(list)
    streams_by_exchange: collections.Counter = collections.Counter()
    barred_by_exchange: dict[str, set[str]] = collections.defaultdict(set)

    for directory in sorted(TAPE.iterdir()):
        if not directory.is_dir():
            continue
        moves = close_to_open_moves(directory)
        if not moves:
            continue
        exchange = exchange_of(directory.name)
        moves_by_exchange[exchange].extend(moves)
        streams_by_exchange[exchange] += 1
        if any(move > CRYPTO_FITTED_FRACTION for move in moves):
            barred_by_exchange[exchange].add(directory.name)

    print(f"Upstox {INTERVAL_ON_THE_WIRE} bars -- what feed-jump-detector actually sees.")
    print(f"Flagged against feed_jump_threshold_fraction = {CRYPTO_FITTED_FRACTION:.3%}.\n")
    header = (
        f"{'exchange':<11}{'compares':>10}{'flagged':>9}{'rate':>8}"
        f"{'streams':>9}{'barred':>8}{'p50':>9}{'p95':>9}{'p99':>9}"
    )
    print(header)
    print("-" * len(header))
    total = flagged_total = 0
    for exchange in sorted(moves_by_exchange, key=lambda name: -len(moves_by_exchange[name])):
        moves = moves_by_exchange[exchange]
        flagged = sum(1 for move in moves if move > CRYPTO_FITTED_FRACTION)
        total += len(moves)
        flagged_total += flagged
        print(
            f"{exchange:<11}{len(moves):>10}{flagged:>9}{flagged / len(moves):>7.1%}"
            f"{streams_by_exchange[exchange]:>9}{len(barred_by_exchange[exchange]):>8}"
            f"{percentile(moves, 0.50):>9.4%}"
            f"{percentile(moves, 0.95):>9.4%}"
            f"{percentile(moves, 0.99):>9.4%}"
        )
    print("-" * len(header))
    barred = sum(len(names) for names in barred_by_exchange.values())
    streams = sum(streams_by_exchange.values())
    print(
        f"{'all':<11}{total:>10}{flagged_total:>9}{flagged_total / total:>7.1%}"
        f"{streams:>9}{barred:>8}"
    )
    print()
    print(
        f"{barred} of {streams} streams ({barred / streams:.0%}) cross the threshold at "
        "least once. paper-fill-simulator.clear_feed_jump has no caller anywhere in the "
        "repository, so each of those is barred from filling for the life of the process."
    )
    print(
        "The two index underlyings this segment is about never cross it: an index moves "
        "in tenths of a percent a minute. An option's premium is small and levered, so "
        "the same move in the underlying is percent-scale in the contract -- which is a "
        "real price move and not a feed artefact."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
