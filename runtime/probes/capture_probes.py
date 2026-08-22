"""What the tape has actually captured, measured from the tape and nothing else.

The capture is the one thing in this project that cannot be caught up on later,
and until now it was the one thing the status board could not see: a board that
showed 321 blueprint tiles and nothing about whether bytes were landing. A
capture that quietly stopped would have looked exactly like a capture that was
running, which is the failure Rule 8 exists to prevent.

Every fact here is read from disk. The record counts come from the index files'
own sizes, the freshness from the newest record in them, and whether a reader is
alive from what that reader last wrote about itself. Nothing asks a process
whether it is well.

Three states, and the difference between them matters:

- **NOT BUILT** -- no tape root at all. Capture has never run here, which is a
  true and unalarming state for a fresh clone.
- **NOT MEASURED** -- the tape exists but something could not be read. Never a
  green tile, never an assumed zero.
- **FAILING** -- the tape exists, and the last thing written to it is older than
  a market ever goes quiet for. That is the tile that says a capture has stopped
  without anyone noticing.
"""

from __future__ import annotations

import json
import pathlib
import time
from dataclasses import dataclass

from runtime.settings_reader import (
    SettingsParseRefused,
    load_settings_document,
    settings_directory,
)
from runtime.tape import INDEX_SUFFIX, TAPE_RECORD_BYTES, TAPE_RECORD_DTYPE

OK = "OK"
NOT_BUILT = "NOT BUILT"
FAILING = "FAILING"
NOT_MEASURED = "NOT MEASURED"

# How stale the newest record on a venue's tape may be before the tile goes red.
# Not a market fact and not a venue fact: it is how long this board is willing to
# believe that a live capture of the thirty highest-volume symbols on a
# twenty-four-hour market has simply seen nothing. At those symbols both venues
# measured thousands of messages a minute on 2026-08-22, so a whole minute of
# silence across every one of them is a stopped reader, not a quiet market.
STALE_TAPE_SECONDS = 60.0

# The same, for a reader's own health log. run_part emits on part_health_interval
# (1 s as shipped), so a minute without a line is a process that is gone rather
# than one that is busy.
STALE_HEALTH_SECONDS = 60.0

NANOSECONDS_PER_SECOND = 1_000_000_000


@dataclass(frozen=True)
class CaptureProbeResult:
    """One measured fact about the capture, and the evidence it came from.

    Field names match the board's own ProbeResult by design, the same way
    substrate_probes does, so adapting one into the other is a construction
    rather than a translation.
    """

    label: str
    state: str
    value: str
    proof: str


def _read_tape_root() -> tuple[pathlib.Path | None, str]:
    """Where settings say the tape lives, expanded, with the proof of where that came from."""
    settings_path = settings_directory() / "runtime.toml"
    try:
        document = load_settings_document(settings_path, "runtime")
        configured = document.read_value("tape_root")
    except (SettingsParseRefused, KeyError, OSError) as failure:
        return None, f"{settings_path}: {type(failure).__name__}: {failure}"
    return pathlib.Path(str(configured)).expanduser(), f"{settings_path} tape_root"


def _index_files(tape_root: pathlib.Path) -> list[pathlib.Path]:
    return sorted(tape_root.glob(f"*/*/*{INDEX_SUFFIX}"))


def _newest_record_time_ns(index_path: pathlib.Path) -> int | None:
    """The `received_at_ns` of the last whole record in one index, read directly.

    Seeks to the final record rather than mapping the file: an index can be
    hundreds of thousands of records and this runs on every board build.
    """
    try:
        size = index_path.stat().st_size
        whole = size // TAPE_RECORD_BYTES
        if whole == 0:
            return None
        with open(index_path, "rb") as handle:
            handle.seek((whole - 1) * TAPE_RECORD_BYTES)
            record = handle.read(TAPE_RECORD_BYTES)
    except OSError:
        return None
    if len(record) < TAPE_RECORD_BYTES:
        return None
    import numpy

    return int(numpy.frombuffer(record, dtype=TAPE_RECORD_DTYPE, count=1)[0]["received_at_ns"])


def probe_tape_records() -> CaptureProbeResult:
    """How many records the tape holds, counted from the index files' own sizes."""
    tape_root, proof = _read_tape_root()
    if tape_root is None:
        return CaptureProbeResult("Tape records", NOT_MEASURED, "tape_root unreadable", proof)
    if not tape_root.exists():
        return CaptureProbeResult(
            "Tape records", NOT_BUILT, "no tape root on disk", f"{tape_root} does not exist"
        )

    per_venue: dict[str, int] = {}
    symbols = 0
    for index_path in _index_files(tape_root):
        venue = index_path.parent.parent.name
        records = index_path.stat().st_size // TAPE_RECORD_BYTES
        per_venue[venue] = per_venue.get(venue, 0) + records
        symbols += 1 if records else 0
    if not per_venue:
        return CaptureProbeResult(
            "Tape records", NOT_BUILT, "tape root exists, no venue has written", str(tape_root)
        )

    total = sum(per_venue.values())
    detail = ", ".join(f"{venue} {count:,}" for venue, count in sorted(per_venue.items()))
    return CaptureProbeResult(
        "Tape records",
        OK if total else NOT_BUILT,
        f"{total:,} records across {symbols} symbol tapes ({detail})",
        f"{len(_index_files(tape_root))} index files under {tape_root}, sized in whole records",
    )


def probe_tape_freshness() -> CaptureProbeResult:
    """How long ago the most recent record landed, per venue.

    The tile that answers "is it still recording". A venue whose newest record is
    older than a market ever goes quiet for has a reader that stopped, and the
    tape alone cannot tell that from a market that went silent -- so the board
    says stale rather than saying nothing.
    """
    tape_root, proof = _read_tape_root()
    if tape_root is None:
        return CaptureProbeResult("Tape freshness", NOT_MEASURED, "tape_root unreadable", proof)
    if not tape_root.exists():
        return CaptureProbeResult(
            "Tape freshness", NOT_BUILT, "no tape root on disk", f"{tape_root} does not exist"
        )

    now_ns = time.time_ns()
    newest_by_venue: dict[str, int] = {}
    for index_path in _index_files(tape_root):
        newest = _newest_record_time_ns(index_path)
        if newest is None:
            continue
        venue = index_path.parent.parent.name
        newest_by_venue[venue] = max(newest_by_venue.get(venue, 0), newest)

    if not newest_by_venue:
        return CaptureProbeResult(
            "Tape freshness", NOT_BUILT, "no venue has written a record yet", str(tape_root)
        )

    staleness = {
        venue: (now_ns - newest) / NANOSECONDS_PER_SECOND
        for venue, newest in newest_by_venue.items()
    }
    detail = ", ".join(f"{venue} {seconds:.0f}s ago" for venue, seconds in sorted(staleness.items()))
    stale = [venue for venue, seconds in staleness.items() if seconds > STALE_TAPE_SECONDS]
    if stale:
        return CaptureProbeResult(
            "Tape freshness",
            FAILING,
            f"{', '.join(sorted(stale))} stopped writing ({detail})",
            f"last record in each venue's newest index under {tape_root}, "
            f"against {STALE_TAPE_SECONDS:.0f}s",
        )
    return CaptureProbeResult(
        "Tape freshness",
        OK,
        detail,
        f"last record in each venue's newest index under {tape_root}",
    )


def probe_capture_readers() -> CaptureProbeResult:
    """Whether each reader is still reporting, read from what it last wrote.

    Not `pgrep`: a process that exists and has stopped reading is the failure
    worth catching, and only its own reports can tell the difference.
    """
    tape_root, proof = _read_tape_root()
    if tape_root is None:
        return CaptureProbeResult("Capture readers", NOT_MEASURED, "tape_root unreadable", proof)
    health_directory = tape_root.parent / "capture-health"
    if not health_directory.exists():
        return CaptureProbeResult(
            "Capture readers",
            NOT_BUILT,
            "no reader has ever reported here",
            f"{health_directory} does not exist",
        )

    logs = sorted(health_directory.glob("*.jsonl"))
    if not logs:
        return CaptureProbeResult(
            "Capture readers", NOT_BUILT, "no reader logs", str(health_directory)
        )

    now_ns = time.time_ns()
    reported: dict[str, float] = {}
    unreadable: list[str] = []
    for log in logs:
        last = _last_observation_ns(log)
        if last is None:
            unreadable.append(log.stem)
            continue
        reported[log.stem] = (now_ns - last) / NANOSECONDS_PER_SECOND

    if unreadable and not reported:
        return CaptureProbeResult(
            "Capture readers",
            NOT_MEASURED,
            f"could not read {', '.join(unreadable)}",
            str(health_directory),
        )

    silent = [venue for venue, seconds in reported.items() if seconds > STALE_HEALTH_SECONDS]
    detail = ", ".join(f"{venue} {seconds:.0f}s ago" for venue, seconds in sorted(reported.items()))
    if silent:
        return CaptureProbeResult(
            "Capture readers",
            FAILING,
            f"{', '.join(sorted(silent))} stopped reporting ({detail})",
            f"last line of each {health_directory}/*.jsonl, against {STALE_HEALTH_SECONDS:.0f}s",
        )
    return CaptureProbeResult(
        "Capture readers",
        OK,
        f"{len(reported)} reporting ({detail})",
        f"last line of each {health_directory}/*.jsonl",
    )


def _last_observation_ns(log_path: pathlib.Path) -> int | None:
    """The `observed_at_ns` of the last complete line, read from the file's tail.

    Read from the end rather than by parsing the whole file: these grow at one
    line a second per reader, and a board build must not get slower every hour
    the capture runs.
    """
    try:
        size = log_path.stat().st_size
        if size == 0:
            return None
        with open(log_path, "rb") as handle:
            window = min(size, 64 * 1024)
            handle.seek(size - window)
            tail = handle.read(window)
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        try:
            observed = json.loads(line).get("observed_at_ns")
        except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
            continue
        if isinstance(observed, int):
            return observed
    return None


def probe_tape_size() -> CaptureProbeResult:
    """How much disk the tape holds, and how long the free space would last.

    The runway figure is the one that matters operationally: the tape only grows,
    nothing prunes it in phase 1, and compression is deliberately deferred.
    """
    tape_root, proof = _read_tape_root()
    if tape_root is None:
        return CaptureProbeResult("Tape size", NOT_MEASURED, "tape_root unreadable", proof)
    if not tape_root.exists():
        return CaptureProbeResult(
            "Tape size", NOT_BUILT, "no tape root on disk", f"{tape_root} does not exist"
        )
    total_bytes = sum(path.stat().st_size for path in tape_root.rglob("*") if path.is_file())
    try:
        import shutil

        free_bytes = shutil.disk_usage(tape_root).free
    except OSError:
        return CaptureProbeResult(
            "Tape size",
            NOT_MEASURED,
            f"{total_bytes / 1e9:.2f} GB written, free space unreadable",
            str(tape_root),
        )
    return CaptureProbeResult(
        "Tape size",
        OK,
        f"{total_bytes / 1e9:.2f} GB written, {free_bytes / 1e9:.0f} GB free",
        f"summed file sizes under {tape_root}, and its filesystem's free space",
    )


CAPTURE_PROBES = (
    probe_capture_readers,
    probe_tape_freshness,
    probe_tape_records,
    probe_tape_size,
)


def run_all_capture_probes() -> list[CaptureProbeResult]:
    """Every capture fact, with a crashed probe reported as unmeasured rather than absent."""
    results: list[CaptureProbeResult] = []
    for probe in CAPTURE_PROBES:
        try:
            results.append(probe())
        except Exception as failure:  # a probe that crashes is unmeasured, never healthy
            results.append(
                CaptureProbeResult(
                    probe.__name__.replace("probe_", "").replace("_", " ").capitalize(),
                    NOT_MEASURED,
                    f"probe raised {type(failure).__name__}: {failure}",
                    f"runtime.probes.capture_probes.{probe.__name__}",
                )
            )
    return results
