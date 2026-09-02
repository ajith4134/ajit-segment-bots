"""The closed candles already on the tape, for a window that has just started.

Off-diagram substrate (RL-069). `kline-window-builder` keeps a bounded run of
candles per symbol and starts every process with none, so after a restart it
takes a full window of wall-clock minutes before anything downstream can run:
measured 2026-08-26, kronos-forecaster refused all 585 windows it was asked about
as WINDOW_TOO_SHORT, and would have gone on refusing for 64 minutes.

**The history comes from the tape, never from a venue's history endpoint.** That
is `historical-bar-store`'s rule and the reason is its own: a venue's historical
endpoint returns its current view of the past -- corrections, late trades,
re-labelled candles -- while the tape is what actually arrived. A window seeded
from the endpoint would be a series nobody was ever shown.

**This is history, not a replay (RL-071).** What is seeded is the model's
context: the candles that closed before the process started, which the same model
would have held had it been running. Every decision is still made when a live
message arrives, on the price it carries.

**Contiguity decides what is kept.** A seed is walked backwards from the newest
closed candle and stops at the first hole, because a window with a hole in it is
a different series from one without and a model handed it learns that time moves
at whatever rate the feed happened to deliver.

The index is scanned rather than the blob: a candle stream restates the open bar
on every update -- about 170 records per closed minute on this tape -- so the
records are bucketed by the minute their event time falls in and only the first
few of each bucket are read. Sixty-four candles cost about a hundred payload
reads instead of eleven thousand.
"""

from __future__ import annotations

import datetime
import json
import pathlib

from runtime.forecast_types import Candle
from runtime.tape import StreamKind, read_payload, read_tape_index, tape_paths_for

MILLISECONDS_TO_NANOSECONDS = 1_000_000

# How many records at the *start* of a minute's bucket to read before giving that
# minute up. The buckets are cut on event time, and a venue sends the closing
# update of a bar just after the bar ends -- so the close of minute M is the
# first record of bucket M+1, not the last of bucket M. Measured on this tape:
# taking the last records of each bucket found zero closed candles, because every
# one of them was an open-bar restatement of the minute in progress.
RECORDS_TRIED_PER_CANDLE = 3

# How many days back to look. Two, so a window that spans midnight is whole and a
# process started at 00:01 does not read as a market with no history.
DAYS_READ = 2


def _days_to_read(now: datetime.datetime) -> tuple[str, ...]:
    return tuple(
        (now - datetime.timedelta(days=offset)).strftime("%Y-%m-%d")
        for offset in range(DAYS_READ)
    )


def closed_candles_on_the_tape(
    tape_root: pathlib.Path,
    venue_id: str,
    symbol: str,
    interval_ns: int,
    wanted: int,
    read_candles,
    now: datetime.datetime | None = None,
) -> tuple:
    """The most recent `wanted` closed candles for one symbol, oldest first.

    `read_candles(payload)` is the venue adapter's own reader, passed in so this
    knows nothing about any venue's wire format. Returns an empty tuple when the
    tape has nothing, which is the ordinary answer on a symbol whose capture
    started a minute ago.
    """
    if wanted < 1 or interval_ns < 1:
        return ()
    stamp = now or datetime.datetime.now(datetime.UTC)
    by_open: dict[int, object] = {}
    for day in _days_to_read(stamp):
        index_path, blob_path = tape_paths_for(
            tape_root, venue_id, symbol, day, StreamKind.CANDLE
        )
        if not index_path.exists() or not blob_path.exists():
            continue
        records = read_tape_index(index_path)
        if len(records) == 0:
            continue
        # Bucket by the minute each record's event time falls in, keeping the
        # first few records of each bucket: a venue closes a bar just after the
        # bar ends, so the close of minute M leads bucket M+1.
        buckets: dict[int, list] = {}
        for position in range(len(records)):
            record = records[position]
            bucket = int(record["venue_time_ns"]) // interval_ns
            held = buckets.setdefault(bucket, [])
            if len(held) < RECORDS_TRIED_PER_CANDLE:
                held.append(record)
        for bucket in sorted(buckets, reverse=True):
            if len(by_open) >= wanted * DAYS_READ:
                break
            for record in buckets[bucket]:
                found = False
                for candle in read_candles(read_payload(blob_path, record)):
                    if candle.is_closed:
                        by_open[candle.open_time_ns] = candle
                        found = True
                if found:
                    break
        if len(by_open) >= wanted:
            break

    if not by_open:
        return ()

    # Backwards from the newest, stopping at the first hole: a seeded window with
    # a gap in it is a series the model was never shown.
    ordered = [by_open[key] for key in sorted(by_open)]
    contiguous = [ordered[-1]]
    for candle in reversed(ordered[:-1]):
        if contiguous[0].open_time_ns - candle.open_time_ns != interval_ns:
            break
        contiguous.insert(0, candle)
        if len(contiguous) >= wanted:
            break
    return tuple(contiguous)


def _candle_from_broker_payload(open_time_ns: int, payload: dict) -> Candle:
    return Candle(
        open_time_ns=open_time_ns,
        open=payload["open"], high=payload["high"], low=payload["low"],
        close=payload["close"], volume=payload["volume"],
        # Same close*volume approximation broker-candle-bridge states for the
        # live path (RL-061) -- Upstox's OHLC entry carries no per-bar
        # turnover figure to read instead.
        quote_volume=payload["close"] * payload["volume"], trades=0,
        is_closed=True,
    )


def closed_broker_candles_on_the_tape(
    tape_root: pathlib.Path,
    venue_id: str,
    symbol: str,
    wanted_interval: str,
    interval_ns: int,
    wanted: int,
    now: datetime.datetime | None = None,
) -> tuple[Candle, ...]:
    """The most recent `wanted` closed broker candles for one symbol, oldest
    first -- `closed_candles_on_the_tape`'s counterpart for a broker tape,
    never reused directly against one, because the two tapes are shaped
    differently in a way that would silently mispick candles if they shared
    one heuristic.

    A crypto venue states a closed flag directly and stamps a candle
    record's own event time as its *arrival*, so a bar's closing update
    lands in the *next* minute's bucket -- `closed_candles_on_the_tape`'s
    "read the first few records of bucket M+1" is built on that fact.
    Upstox states no closed flag at all (runtime/brokers/upstox.py's
    `_read_ohlc`) and `broker-market-tape-writer` stamps a candle record's
    venue_time_ns as the bar's own *open* time (`venue_time_ns_of`), so
    every restatement of one forming bar shares one bucket with each other,
    not with the next bar. Reusing the crypto heuristic here would read an
    early, incomplete restatement as if it were the close.

    So this earns its own rule instead: a bucket's last-seen record, kept
    only once a later record proves a *different* bucket has started, is
    that bar's close -- the same fact broker-candle-bridge uses for the
    live path (wall time passing the bar's own interval), expressed here as
    "the tape moved on to the next bar" because every record already on the
    tape is, by definition, in the past. The bucket still open at the end of
    the scan is excluded: nothing on the tape yet proves it closed.
    """
    if wanted < 1 or interval_ns < 1:
        return ()
    stamp = now or datetime.datetime.now(datetime.UTC)
    days_oldest_first = tuple(reversed(_days_to_read(stamp)))

    last_in_bucket: dict[int, tuple[int, dict]] = {}
    current_bucket: int | None = None
    closed: dict[int, Candle] = {}

    def close_bucket(bucket: int) -> None:
        held = last_in_bucket.pop(bucket, None)
        if held is not None:
            open_time_ns, payload = held
            closed[open_time_ns] = _candle_from_broker_payload(open_time_ns, payload)

    for day in days_oldest_first:
        index_path, blob_path = tape_paths_for(
            tape_root, venue_id, symbol, day, StreamKind.CANDLE
        )
        if not index_path.exists() or not blob_path.exists():
            continue
        records = read_tape_index(index_path)
        for position in range(len(records)):
            record = records[position]
            payload = json.loads(read_payload(blob_path, record))
            if payload.get("interval") != wanted_interval:
                continue
            open_time_ns = int(payload["bar_time_ms"]) * MILLISECONDS_TO_NANOSECONDS
            bucket = open_time_ns // interval_ns
            if current_bucket is not None and bucket != current_bucket:
                close_bucket(current_bucket)
            last_in_bucket[bucket] = (open_time_ns, payload)
            current_bucket = bucket

    if not closed:
        return ()

    # Backwards from the newest, stopping at the first hole -- same rule
    # closed_candles_on_the_tape applies, for the same reason.
    ordered = [closed[key] for key in sorted(closed)]
    contiguous = [ordered[-1]]
    for candle in reversed(ordered[:-1]):
        if contiguous[0].open_time_ns - candle.open_time_ns != interval_ns:
            break
        contiguous.insert(0, candle)
        if len(contiguous) >= wanted:
            break
    return tuple(contiguous)
