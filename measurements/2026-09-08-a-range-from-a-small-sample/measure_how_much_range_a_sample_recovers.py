"""How much of a window's true range a sample of n prints actually recovers.

`bull-exit-plan-proposer` and its bear peer measure the cold-start stop from the
range a symbol traded through over the horizon the detector claimed, and refuse
to measure one at all below `*_cold_start_minimum_prints` (20). On NSE that bar
refuses almost everything: measured on the live spine 2026-09-08, the bull
proposer refused **84,838 of 86,943** plan requests as
`too-few-prints-in-the-window-to-measure-a-range` and the bear **71,211 of
77,282**.

The 2026-09-07 measurement (measurements/2026-09-07-exit-plan-starvation/) said
the bar itself is statistically sound and the horizon is what does not survive
the pivot -- and named a second option it did not take: correct the small-sample
bias in the estimator, so a range from five prints is reported as the
underestimate it is rather than used raw. This re-derives that correction on this
project's own tape, because the earlier README quotes a table whose script was
never saved.

Method, which isolates sample size from span: hold the window fixed, take the
true range over every print inside it, then draw n of those prints at random and
ask what share of the true range the sample spans. Repeated over many draws and
many contracts.

    .venv/bin/python measurements/2026-09-08-a-range-from-a-small-sample/\
measure_how_much_range_a_sample_recovers.py 2026-09-08
"""

from __future__ import annotations

import json
import pathlib
import random
import statistics
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from runtime.tape import StreamKind, read_tape_index, tape_paths_for  # noqa: E402

TAPE_ROOT = pathlib.Path.home() / ".local/share/ajit-segment-bots/tape"
VENUE = "upstox"
WINDOW_SECONDS = 300.0
DRAWS_PER_CONTRACT = 40
SAMPLE_SIZES = (3, 4, 5, 6, 8, 10, 15, 20, 30)
# A window has to hold a good deal more than the largest sample for the draw to
# mean anything: sampling 30 of 31 prints measures nothing about sampling.
PRINTS_NEEDED_IN_THE_WINDOW = 60


def prints_for(symbol: str, day: str) -> list[tuple[int, float]]:
    index_path, blob_path = tape_paths_for(TAPE_ROOT, VENUE, symbol, day, StreamKind.TRADE)
    if not index_path.exists():
        return []
    records = read_tape_index(index_path)
    if len(records) == 0:
        return []
    prints = []
    with open(blob_path, "rb") as handle:
        for record in records:
            handle.seek(int(record["blob_offset"]))
            try:
                row = json.loads(handle.read(int(record["blob_length"])))
            except ValueError:
                continue
            price = row.get("last_traded_price")
            if price:
                prints.append(
                    (int(row.get("broker_time_ns") or record["venue_time_ns"]), float(price))
                )
    prints.sort()
    return prints


def windows_in(prints, seconds: float):
    """Every non-overlapping window of `seconds` that holds enough prints."""
    if not prints:
        return
    span_ns = int(seconds * 1e9)
    start = prints[0][0]
    inside: list[float] = []
    for at_ns, price in prints:
        if at_ns - start >= span_ns:
            if len(inside) >= PRINTS_NEEDED_IN_THE_WINDOW:
                yield inside
            start, inside = at_ns, []
        inside.append(price)
    if len(inside) >= PRINTS_NEEDED_IN_THE_WINDOW:
        yield inside


def recovered_share(window: list[float], sample_size: int, rng) -> float | None:
    """What share of the window's true range a sample of this size spans."""
    true_range = max(window) - min(window)
    if true_range <= 0:
        return None
    drawn = rng.sample(window, sample_size)
    return (max(drawn) - min(drawn)) / true_range


def main() -> int:
    day = sys.argv[1] if len(sys.argv) > 1 else "2026-09-08"
    wanted = int(sys.argv[2]) if len(sys.argv) > 2 else 400
    rng = random.Random(17)

    directories = sorted(
        path.name for path in (TAPE_ROOT / VENUE).iterdir() if path.is_dir()
    )
    rng.shuffle(directories)

    shares: dict[int, list[float]] = {size: [] for size in SAMPLE_SIZES}
    contracts = windows = 0
    for symbol in directories:
        if contracts >= wanted:
            break
        prints = prints_for(symbol, day)
        used = False
        for window in windows_in(prints, WINDOW_SECONDS):
            windows += 1
            used = True
            for size in SAMPLE_SIZES:
                if len(window) <= size:
                    continue
                for _ in range(DRAWS_PER_CONTRACT // len(SAMPLE_SIZES) + 1):
                    share = recovered_share(window, size, rng)
                    if share is not None:
                        shares[size].append(share)
        if used:
            contracts += 1

    print(
        f"{day}: {contracts} symbols, {windows} windows of {WINDOW_SECONDS:.0f}s "
        f"holding at least {PRINTS_NEEDED_IN_THE_WINDOW} prints\n"
    )
    print(f"{'prints':>7}  {'p25':>7}  {'p50':>7}  {'p75':>7}  {'draws':>8}")
    for size in SAMPLE_SIZES:
        drawn = sorted(shares[size])
        if not drawn:
            print(f"{size:>7}  {'no draw':>7}")
            continue
        quantiles = statistics.quantiles(drawn, n=4) if len(drawn) > 3 else [0, 0, 0]
        print(
            f"{size:>7}  {quantiles[0]:>6.1%}  {quantiles[1]:>6.1%}  "
            f"{quantiles[2]:>6.1%}  {len(drawn):>8}"
        )
    print("\nThe p50 column is the correction: a range measured over n prints is")
    print("divided by it to state what the window's whole range probably was.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
