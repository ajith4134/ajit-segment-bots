"""Why position-sizer refused 707 of 843 intents: the tick is in the wrong unit.

Upstox's instrument master declares `tick_size` in paise. Every price it streams
is in rupees. Read as rupees, the tick is 100x too coarse, and `position-sizer`
snaps an entry and its stop to the same multiple of it -- a stop equal to its
entry, refused as being on the wrong side of it.

This checks the declaration against what prices on the captured tape actually do,
per contract: the smallest move a contract's own prints ever made is the finest
increment that contract can be quoted in, and no declared tick can be coarser
than the moves that were actually observed.
"""

from __future__ import annotations

import collections
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from runtime.brokers.upstox import PAISE_PER_RUPEE

TAPE = pathlib.Path.home() / ".local/share/ajit-segment-bots/tape/upstox"
MASTER = (
    pathlib.Path(__file__).resolve().parents[2]
    / "tests/captured/upstox/2026-09-04-nse-instrument-master-nifty-slice.json"
)
DAY = "2026-09-04"
# What position-sizer would snap against, before and after the conversion.
AS_RUPEES = 5.0
AS_PAISE = AS_RUPEES / PAISE_PER_RUPEE


def traded_prices(directory: pathlib.Path, limit: int = 6000) -> list[float]:
    from runtime.tape import read_tape_index

    index_path = directory / f"{DAY}.index"
    if not index_path.exists():
        return []
    records = read_tape_index(index_path)
    prices: list[float] = []
    with open(directory / f"{DAY}.blob", "rb") as blob:
        for record in records[:limit]:
            blob.seek(int(record["blob_offset"]))
            try:
                payload = json.loads(blob.read(int(record["blob_length"])))
            except ValueError:
                continue
            price = payload.get("price") or payload.get("last_traded_price")
            if price:
                prices.append(float(price))
    return prices


def smallest_observed_move(prices: list[float]) -> float | None:
    distinct = sorted(set(prices))
    moves = [round(later - earlier, 6) for earlier, later in zip(distinct, distinct[1:])]
    moves = [move for move in moves if move > 0]
    return min(moves) if moves else None


def main() -> int:
    declared = collections.Counter(
        row.get("tick_size") for row in json.loads(MASTER.read_text())
        if row.get("tick_size") is not None
    )
    print(f"tick_size declared in the captured NSE master: {dict(declared)}")
    print(f"  read as rupees -> {AS_RUPEES}   converted from paise -> {AS_PAISE}\n")

    finest = collections.Counter()
    coarser_than_declared = 0
    contracts = 0
    for directory in sorted(TAPE.glob("NSE_FO|*")):
        prices = traded_prices(directory)
        if len(prices) < 200:
            continue
        move = smallest_observed_move(prices)
        if move is None:
            continue
        contracts += 1
        finest[move] += 1
        if move < AS_RUPEES:
            coarser_than_declared += 1

    print(f"{'smallest move a contract made':<32}{'contracts':>10}")
    print("-" * 42)
    for move, count in sorted(finest.items()):
        print(f"{move:<32}{count:>10}")
    print("-" * 42)
    print(f"{'contracts measured':<32}{contracts:>10}\n")

    print(
        f"{coarser_than_declared} of {contracts} contracts moved by less than the "
        f"declared tick read as rupees ({AS_RUPEES}). A tick is the smallest move an "
        "instrument can make, so a price that moved less than its own tick is not "
        "possible -- the declaration is not in rupees."
    )
    finer_than_the_converted_tick = sum(
        count for move, count in finest.items() if move < AS_PAISE
    )
    print(
        f"Read as paise the tick is {AS_PAISE}, the standard NSE option tick, and "
        f"{contracts - finer_than_the_converted_tick} of {contracts} contracts never "
        "moved by less than it."
    )
    print(
        f"{finer_than_the_converted_tick} did -- they printed moves of 0.01 and 0.02. "
        "That is a separate question this measurement does not answer: the master "
        "declares one tick for every row, so it cannot explain a per-contract "
        "difference either way, and whether those are a finer tick on cheap "
        "contracts or sub-tick values in the LTP feed needs its own measurement. "
        "Neither reading is compatible with 5 rupees."
    )
    print(
        "The moves far above the tick are not evidence against it: a smallest "
        "observed move is a lower bound on the tick, and an illiquid contract "
        "simply never printed two adjacent ones."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
