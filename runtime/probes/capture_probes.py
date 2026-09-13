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

**A retired venue is named, never failing** (2026-09-13). Binance and Bybit were
retired on 2026-09-02; their tapes and health logs stay on disk, and until this
date both tiles went red on them for twenty days -- a red tile for a decision
rather than a fault, which trains a reader to ignore red. A venue is retired when
this build has an adapter module for it under `runtime/venues/` and
`captured_venues` does not name it: derived from the code and the operator's
setting, never a list typed here. It is left out of the FAILING decision and
stated in the tile's value, so the board still says it is there.

**A broker's tape is judged against the exchange session** (2026-09-13). The
sixty-second bound was fitted to a twenty-four-hour crypto market; on NSE it read
`upstox` FAILING every evening, weekend and holiday. The session is asked of
`market-session-calendar` -- read from its own standing in the heartbeat table,
the live spine's answer -- never worked out here, because a second calendar is a
second answer to disagree with the one the bot trades on. In session the bound
applies; out of session an old tape is expected and says so; with no fresh answer
from the calendar the tile is NOT MEASURED for that venue, never green. What this
cannot see: a feed that died mid-session reads red only until the close.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import pkgutil
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


def _retired_venues() -> frozenset[str]:
    """Venues this build has an adapter for that `captured_venues` does not name.

    The adapter modules are the ones `runtime.venues.adapter_registry` resolves a
    venue id to; the setting is the operator's. A venue in neither -- `upstox`,
    `news` -- is not a retired crypto venue and is judged as it always was.
    Settings that cannot be read retire nothing: failing towards judging a venue
    rather than towards excusing one.
    """
    import runtime.venues as venue_package
    from runtime.venues.adapter_registry import (
        ADAPTER_FACTORY_NAME,
        CAPTURED_VENUES_SETTING,
        VENUE_PACKAGE,
    )
    import importlib

    settings_path = settings_directory() / "runtime.toml"
    try:
        captured = set(
            load_settings_document(settings_path, "runtime").read_value(CAPTURED_VENUES_SETTING)
        )
    except (SettingsParseRefused, KeyError, OSError):
        return frozenset()
    with_an_adapter = set()
    for module_info in pkgutil.iter_modules(venue_package.__path__):
        try:
            module = importlib.import_module(f"{VENUE_PACKAGE}.{module_info.name}")
        except Exception:  # noqa: BLE001 -- a module that does not import is not an adapter
            continue
        if callable(getattr(module, ADAPTER_FACTORY_NAME, None)):
            with_an_adapter.add(module_info.name.replace("_", "-"))
    return frozenset(with_an_adapter - captured)


# The part whose standing answers "is NSE in session", and the package a tape
# venue must have a broker module in for that answer to govern its tape.
SESSION_CALENDAR_PART = "market-session-calendar"
BROKER_PACKAGE = "runtime.brokers"
IN_SESSION = "in session"
OUT_OF_SESSION = "out of session"


def _session_governed_venues(venues) -> frozenset[str]:
    """Tape venues written by a broker on an exchange with sessions: `upstox`, not `news`.

    Derived from the code, like `_retired_venues`: a venue is governed when
    `runtime/brokers/<venue>.py` exists. Nothing is imported to find out.
    """
    governed = set()
    for venue in venues:
        try:
            if importlib.util.find_spec(f"{BROKER_PACKAGE}.{venue.replace('-', '_')}") is not None:
                governed.add(venue)
        except (ImportError, ValueError):
            continue
    return frozenset(governed)


def _session_answer() -> tuple[str | None, str]:
    """`market-session-calendar`'s own answer, read from the heartbeat table, and its proof.

    None when there is no answer to trust: settings or table unreadable, the table
    older than `heartbeat_silent_after_seconds`, the calendar not reporting, or no
    holiday list read yet -- in which last case the calendar says CLOSED, and that
    CLOSED is an absence of measurement rather than a closed market.
    """
    settings_path = settings_directory() / "runtime.toml"
    try:
        document = load_settings_document(settings_path, "runtime")
        table_path = pathlib.Path(str(document.read_value("heartbeat_table_path"))).expanduser()
        silent_after = float(document.read_value("heartbeat_silent_after_seconds"))
        table = json.loads(table_path.read_text())
    except (SettingsParseRefused, KeyError, OSError, ValueError) as failure:
        return None, f"no session answer: {type(failure).__name__}: {failure}"

    age = (time.time_ns() - int(table.get("collected_at_ns", 0))) / NANOSECONDS_PER_SECOND
    if age > silent_after:
        return None, f"no session answer: {table_path} is {age:.0f}s old, past {silent_after:.0f}s"
    row = next(
        (row for row in table.get("heartbeats", []) if row.get("part_id") == SESSION_CALENDAR_PART),
        None,
    )
    if row is None or row.get("state") != "reporting":
        return None, f"no session answer: {SESSION_CALENDAR_PART} is not reporting in {table_path}"
    standing = row.get("standing") or {}
    if "is_open" not in standing or not standing.get("has_a_holiday_list"):
        return None, f"no session answer: {SESSION_CALENDAR_PART} has not read its holiday list"
    answer = IN_SESSION if standing["is_open"] else OUT_OF_SESSION
    return answer, f"{SESSION_CALENDAR_PART} standing in {table_path}"


def _day_of(index_path: pathlib.Path) -> str:
    """The session day an index belongs to: `2026-09-13` of `2026-09-13.book.index`."""
    return index_path.name.split(".", 1)[0]


def _newest_day_index_files(tape_root: pathlib.Path) -> dict[str, list[pathlib.Path]]:
    """Per venue, only the index files of that venue's newest day.

    The newest record a venue wrote is in its newest day's files, so reading
    every older day's index answers nothing new. Measured 2026-09-13: 166,204
    index files, 162,754 of them Upstox's, and reading the last record of each
    took 415s off a cold page cache -- 777s on the live spine -- while the newest
    day alone is 9,789 files and gave the same answer for all four venues.
    """
    by_venue: dict[str, list[pathlib.Path]] = {}
    for index_path in _index_files(tape_root):
        by_venue.setdefault(index_path.parent.parent.name, []).append(index_path)
    newest: dict[str, list[pathlib.Path]] = {}
    for venue, paths in by_venue.items():
        latest = max(_day_of(path) for path in paths)
        newest[venue] = [path for path in paths if _day_of(path) == latest]
    return newest


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
    for venue, index_paths in _newest_day_index_files(tape_root).items():
        for index_path in index_paths:
            newest = _newest_record_time_ns(index_path)
            if newest is None:
                continue
            newest_by_venue[venue] = max(newest_by_venue.get(venue, 0), newest)

    if not newest_by_venue:
        return CaptureProbeResult(
            "Tape freshness", NOT_BUILT, "no venue has written a record yet", str(tape_root)
        )

    retired = _retired_venues()
    staleness = {
        venue: (now_ns - newest) / NANOSECONDS_PER_SECOND
        for venue, newest in newest_by_venue.items()
    }
    live = {venue: seconds for venue, seconds in staleness.items() if venue not in retired}
    retired_detail = _retired_note(
        {venue: seconds for venue, seconds in staleness.items() if venue in retired}
    )
    proof = (
        f"last record in each venue's newest day of index files under {tape_root}"
        + (f"; retired = an adapter under runtime/venues/ not named in captured_venues" if retired_detail else "")
    )
    if not live:
        return CaptureProbeResult(
            "Tape freshness", NOT_BUILT, f"no live venue has written a record{retired_detail}", proof
        )

    governed = _session_governed_venues(live)
    session, session_proof = _session_answer() if governed else (None, "")
    stale, unjudged, notes = [], [], []
    for venue, seconds in sorted(live.items()):
        if venue not in governed:
            notes.append(f"{venue} {seconds:.0f}s ago")
            if seconds > STALE_TAPE_SECONDS:
                stale.append(venue)
        elif session == IN_SESSION:
            notes.append(f"{venue} {seconds:.0f}s ago, NSE in session")
            if seconds > STALE_TAPE_SECONDS:
                stale.append(venue)
        elif session == OUT_OF_SESSION:
            notes.append(f"{venue} {seconds:.0f}s ago, NSE out of session")
        else:
            notes.append(f"{venue} {seconds:.0f}s ago, session not measured")
            if seconds > STALE_TAPE_SECONDS:
                unjudged.append(venue)
    detail = ", ".join(notes)
    if governed:
        proof = f"{proof}; session: {session_proof}"
    if stale:
        return CaptureProbeResult(
            "Tape freshness",
            FAILING,
            f"{', '.join(stale)} stopped writing ({detail}){retired_detail}",
            f"{proof}, against {STALE_TAPE_SECONDS:.0f}s",
        )
    if unjudged:
        return CaptureProbeResult(
            "Tape freshness",
            NOT_MEASURED,
            f"cannot tell whether {', '.join(unjudged)} stopped or NSE is shut ({detail}){retired_detail}",
            proof,
        )
    return CaptureProbeResult("Tape freshness", OK, f"{detail}{retired_detail}", proof)


def _retired_note(seconds_by_venue: dict[str, float]) -> str:
    """`; retired: binance-usdm (last wrote 1740231s ago)`, or nothing."""
    if not seconds_by_venue:
        return ""
    return "; retired: " + ", ".join(
        f"{venue} (last wrote {seconds:.0f}s ago)" for venue, seconds in sorted(seconds_by_venue.items())
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

    retired = _retired_venues()
    retired_detail = _retired_note(
        {venue: seconds for venue, seconds in reported.items() if venue in retired}
    )
    live = {venue: seconds for venue, seconds in reported.items() if venue not in retired}
    if not live:
        return CaptureProbeResult(
            "Capture readers",
            NOT_BUILT,
            f"no live reader reports here{retired_detail}",
            f"last line of each {health_directory}/*.jsonl; retired = an adapter under "
            f"runtime/venues/ not named in captured_venues",
        )
    silent = [venue for venue, seconds in live.items() if seconds > STALE_HEALTH_SECONDS]
    detail = ", ".join(f"{venue} {seconds:.0f}s ago" for venue, seconds in sorted(live.items()))
    if silent:
        return CaptureProbeResult(
            "Capture readers",
            FAILING,
            f"{', '.join(sorted(silent))} stopped reporting ({detail}){retired_detail}",
            f"last line of each {health_directory}/*.jsonl, against {STALE_HEALTH_SECONDS:.0f}s",
        )
    return CaptureProbeResult(
        "Capture readers",
        OK,
        f"{len(live)} reporting ({detail}){retired_detail}",
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
