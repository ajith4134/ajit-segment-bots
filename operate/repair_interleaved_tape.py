#!/usr/bin/env python3
"""Repair the tapes two writers interleaved, and split them by stream kind.

## What happened

`ccxt-venue-reader` started with phase 6 at 12:45 on 2026-08-25 and began
appending CANDLE records to `{venue}/{symbol}/{day}.blob` -- the same file
`venue-trade-stream-reader` was appending TRADE records to. A `TapeWriter` counts
its own blob position, so from that minute each writer's index offsets counted
only its own bytes. Every offset after the first interleaved append pointed into
the other writer's payloads.

Measured across the tape: 108 of 511 trade-named index files, all of them today's,
carry both kinds. 20.2 million records in those files, of which 9.1 million were
written before the first candle and are untouched.

Nothing detected it. Both parts were healthy, every board was green, and the
damage was visible only to something that tried to parse a payload.

## What this does

The index still carries the true **length**, kind, and timestamps of every
record -- only the offsets are wrong. The blob still holds every payload whole and
in the order it was appended. So the true offset of a record is the sum of the
lengths before it, and the repair is a cumulative sum with every record verified
against the venue's own reading of its bytes.

Verified, not assumed, and this is the whole point: a record is rewritten only if
the payload at the computed offset parses, and the venue adapter reads back the
same stream kind, the same symbol, and the same venue timestamp the index record
carries. Anything that fails is dropped and counted. A repair that guessed would
be worse than the corruption, because the corruption announces itself by failing
to parse and a plausible mispairing does not.

Where a payload's bytes were lost -- a process killed between its blob write and
its index write leaves an orphan -- the scan resynchronises by looking for the
next byte offset at which the expected record verifies, inside a bounded window.

The output is one file per kind, which is where the tape lives from 2026-08-25:
trades keep the bare `{day}.index`/`{day}.blob` and candles land in
`{day}.candle.index`/`{day}.candle.blob`.

## Running it

    python3 operate/repair_interleaved_tape.py                 # measure, write nothing
    python3 operate/repair_interleaved_tape.py --apply         # rewrite the tapes

**Stop the spine first when applying.** The trade reader holds these files open
and appends to them; replacing a file under a live writer sends every subsequent
record to an inode nobody will ever read.

    systemctl --user stop ajit-spine
    python3 operate/repair_interleaved_tape.py --apply
    systemctl --user start ajit-spine
"""

from __future__ import annotations

import argparse
import datetime
import json
import mmap
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import numpy  # noqa: E402

from runtime.tape import (  # noqa: E402
    NOT_SENT,
    TAPE_RECORD_DTYPE,
    StreamKind,
    read_tape_index,
    tape_paths_for,
)
from runtime.venues.adapter_registry import load_venue_adapter  # noqa: E402

TAPE_ROOT = pathlib.Path.home() / ".local/share/ajit-segment-bots/tape"

# How far to look for the next payload boundary when a record does not verify
# where the running total says it should. One lost payload is the failure this
# recovers from, and the largest payload observed on either venue is a few
# kilobytes; a window far past that would start finding coincidences.
RESYNC_WINDOW_BYTES = 262_144


class RepairedFile:
    """What one tape file's repair recovered, and what it could not."""

    def __init__(self, index_path: pathlib.Path) -> None:
        self.index_path = index_path
        self.records = 0
        self.verified_by_kind: dict[int, int] = {}
        self.resynchronised = 0
        self.dropped = 0
        self.dropped_first_at: int | None = None

    @property
    def verified(self) -> int:
        return sum(self.verified_by_kind.values())

    def describe(self) -> str:
        kinds = ", ".join(
            f"{StreamKind(kind).name.lower()} {count:,}"
            for kind, count in sorted(self.verified_by_kind.items())
        )
        return (
            f"{self.index_path.parent.parent.name}/{self.index_path.parent.name}/"
            f"{self.index_path.name}: {self.verified:,} of {self.records:,} verified "
            f"({kinds}); {self.resynchronised:,} resynchronised, {self.dropped:,} dropped"
        )


def is_mixed(records) -> bool:
    """Does this trade-named file hold more than one stream kind?"""
    kinds = numpy.unique(records["stream_kind"])
    return len(kinds) > 1 or (len(kinds) == 1 and int(kinds[0]) != int(StreamKind.TRADE))


def payload_verifies(adapter, payload: bytes, record) -> bool:
    """Does the venue read this payload back as the record says it should?

    Three agreements, not one: the bytes are a whole message, the venue reads the
    same stream kind out of it, and it carries the same venue timestamp. A payload
    that merely parses could still be the neighbouring record's.
    """
    if not payload:
        return False
    try:
        json.loads(payload)
    except ValueError:
        return False
    facts = adapter.read_message_facts(payload)
    if facts is None:
        return False
    if int(facts.stream_kind) != int(record["stream_kind"]):
        return False
    recorded_time = int(record["venue_time_ns"])
    if recorded_time != NOT_SENT and facts.venue_time_ns != recorded_time:
        return False
    return True


def repair_one_file(index_path: pathlib.Path, blob_path: pathlib.Path, adapter) -> tuple[
    RepairedFile, dict[int, list[tuple[bytes, numpy.void]]]
]:
    """Recompute every offset, verify every payload, and group by stream kind."""
    outcome = RepairedFile(index_path)
    records = read_tape_index(index_path)
    outcome.records = len(records)
    recovered: dict[int, list[tuple[bytes, numpy.void]]] = {}

    with open(blob_path, "rb") as handle:
        mapped = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            position = 0
            for number, record in enumerate(records):
                length = int(record["blob_length"])
                payload = mapped[position:position + length]
                if payload_verifies(adapter, payload, record):
                    position += length
                else:
                    found = resynchronise(mapped, position, length, record, adapter)
                    if found is None:
                        outcome.dropped += 1
                        if outcome.dropped_first_at is None:
                            outcome.dropped_first_at = number
                        continue
                    payload = mapped[found:found + length]
                    position = found + length
                    outcome.resynchronised += 1
                kind = int(record["stream_kind"])
                recovered.setdefault(kind, []).append((bytes(payload), record))
                outcome.verified_by_kind[kind] = outcome.verified_by_kind.get(kind, 0) + 1
        finally:
            mapped.close()
    return outcome, recovered


def resynchronise(mapped, position: int, length: int, record, adapter) -> int | None:
    """The next offset at which this record's payload verifies, or None.

    Payloads start with `{` on both venues, so only those offsets are tried --
    which is what keeps a bounded window cheap rather than byte-by-byte.
    """
    limit = min(len(mapped), position + RESYNC_WINDOW_BYTES)
    candidate = mapped.find(b"{", position, limit)
    while candidate != -1:
        if payload_verifies(adapter, mapped[candidate:candidate + length], record):
            return candidate
        candidate = mapped.find(b"{", candidate + 1, limit)
    return None


def set_the_damaged_tape_aside(index_path: pathlib.Path, blob_path: pathlib.Path) -> None:
    """Rename the interleaved files rather than overwriting them.

    The tape is the one thing here that cannot be rebuilt, so a repair that is
    wrong must be undoable. The damaged pair keeps every byte under
    `{day}.interleaved.index` / `.blob`, and the repair is judged against it
    rather than against a memory of it.
    """
    for path in (index_path, blob_path):
        if path.exists():
            path.replace(path.with_name(f"{path.stem}.interleaved{path.suffix}"))


def write_repaired(
    root: pathlib.Path, venue: str, symbol: str, day: str,
    recovered: dict[int, list[tuple[bytes, numpy.void]]],
) -> list[pathlib.Path]:
    """Write one index and one blob per stream kind, offsets recomputed from scratch."""
    written = []
    for kind, entries in sorted(recovered.items()):
        index_path, blob_path = tape_paths_for(root, venue, symbol, day, StreamKind(kind))
        index_temporary = index_path.with_suffix(index_path.suffix + ".repaired")
        blob_temporary = blob_path.with_suffix(blob_path.suffix + ".repaired")
        offset = 0
        rebuilt = numpy.zeros(len(entries), dtype=TAPE_RECORD_DTYPE)
        with open(blob_temporary, "wb") as blob:
            for position, (payload, record) in enumerate(entries):
                blob.write(payload)
                rebuilt[position] = record
                rebuilt[position]["blob_offset"] = offset
                rebuilt[position]["blob_length"] = len(payload)
                offset += len(payload)
        index_temporary.write_bytes(rebuilt.tobytes())
        # Renamed only once both files are complete, so an interrupted repair
        # leaves the damaged tape in place rather than half a repaired one.
        blob_temporary.replace(blob_path)
        index_temporary.replace(index_path)
        written.extend((index_path, blob_path))
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="rewrite the tapes; stop the spine first")
    parser.add_argument("--day", default=None, help="only this UTC day (default: every day found)")
    parser.add_argument("--root", default=str(TAPE_ROOT), help="tape root")
    arguments = parser.parse_args()

    root = pathlib.Path(arguments.root)
    # Built per venue directory found on the tape rather than from the settings:
    # a repair reads what was captured, including a venue the operator has since
    # turned off.
    adapters: dict[str, object] = {}

    print("interleaved tape repair --", "APPLYING" if arguments.apply else "measuring only")
    print(f"  tape root {root}")
    print()

    outcomes: list[RepairedFile] = []
    for index_path in sorted(root.glob("*/*/*.index")):
        if index_path.name.count(".") > 1:
            continue  # already a per-kind file
        day = index_path.stem
        if arguments.day and day != arguments.day:
            continue
        venue = index_path.parent.parent.name
        symbol = index_path.parent.name
        if venue not in adapters:
            try:
                adapters[venue] = load_venue_adapter(venue)
            except Exception as refusal:  # noqa: BLE001 -- reported, not swallowed
                print(f"  SKIPPED {venue}: {refusal}")
                adapters[venue] = None
        adapter = adapters[venue]
        if adapter is None:
            continue
        records = read_tape_index(index_path)
        if len(records) == 0 or not is_mixed(records):
            continue

        blob_path = index_path.with_suffix(".blob")
        outcome, recovered = repair_one_file(index_path, blob_path, adapter)
        outcomes.append(outcome)
        print("  " + outcome.describe(), flush=True)
        if arguments.apply:
            # Read first, then set aside, then write: the mmap is closed by now,
            # and nothing is written until the damaged pair is safely renamed.
            set_the_damaged_tape_aside(index_path, blob_path)
            write_repaired(root, venue, symbol, day, recovered)

    print()
    if not outcomes:
        print("  no interleaved tape files found")
        return 0
    total_records = sum(outcome.records for outcome in outcomes)
    total_verified = sum(outcome.verified for outcome in outcomes)
    print(f"  files          {len(outcomes):>12,}")
    print(f"  records        {total_records:>12,}")
    print(f"  verified       {total_verified:>12,}  ({total_verified / total_records:.1%})")
    print(f"  resynchronised {sum(o.resynchronised for o in outcomes):>12,}")
    print(f"  dropped        {sum(o.dropped for o in outcomes):>12,}")
    stamped = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"  measured at    {stamped}")
    if not arguments.apply:
        print()
        print("  nothing was written. Re-run with --apply, with the spine stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
