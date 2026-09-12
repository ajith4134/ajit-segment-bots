"""news-tape-writer: append every news item to the tape.

**A story is not re-fetchable.** History accrues only in real time here for the
same reason it does for the market tape (RL-068), and with a sharper edge:
measured against the real Upstox news endpoint on 2026-09-12, one response
carried a rolling backlog of 167.1 hours and *nothing older*. There is no
"fetch me last month's news" call. An hour of news not captured is gone
permanently, exactly like an hour of ticks.

Every item is written under both of its timestamps, which is the block's own
design rule and not a convenience:

> The tape carries `published_at_ns` **and** `observed_at_ns` on every item.
> One timestamp is not enough: replaying on publish time alone hands a backtest
> information the live bot did not have until seconds later.

The tape record's `venue_time_ns` is the **published** time, because that is
the source's own clock — the same slot the market tape gives the venue's clock —
and the payload carries `observed_at_ns` inside it, so `news-history-reader` can
replay on either and `lookahead-auditor` can refuse the wrong one.

**The file a record lands in is the day it was SEEN**, which is the same rule
every tape here follows and matters more for news than for ticks: one fetch of
this endpoint returns stories published across eight different days. Filing by
publish date would scatter one fetch across eight files and claim the system
held a story on the 5th that it first saw on the 12th — exactly the lookahead
the two timestamps exist to prevent. The observation time is passed explicitly
rather than defaulting to the wall clock, so importing a captured response files
its items on the day they were observed, not the day of the import.

## Where it is written, and why the path looks like that

`runtime/tape.py` is keyed by venue and symbol, and news is not per symbol: one
story names several instruments and some name none. So the partition is by
**source** — `tape/news/<source id>/<day>.{index,blob}` — which is the same
shape one venue's one symbol has, and gives the reader one file per source per
day to walk. Writing it per instrument would duplicate a market-wrap story
across two hundred names and make "what did this source say on Tuesday"
unanswerable.

`StreamKind.NEWS` was appended to the tape vocabulary for this part, so every
tape written before it reads back unchanged.

## Half of this part's input does not exist yet, and it says so

The blueprint has it consume `raw-news-item` **and** `news-item` — the raw
sighting and the published news fact once the block has structured, resolved,
classified and scored it. Nothing produces `news-item` today: the structurer,
the symbol resolver, the classifiers and the credibility scorer are all
unbuilt, so that wire is dark. `news_items_written` staying at zero is the
honest evidence of that, and it sits beside `raw_items_written` rather than
being folded into one total which would read as a working chain.

That is also why a `news-item` is encoded from whatever fields it turns out to
carry rather than by naming them: the type has no definition in this codebase
yet, and a writer that named fields would be asserting a shape nobody has
built. When the structurer lands, this part needs no change to tape what it
publishes.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib

from runtime.part_context import RUNTIME_SCOPE as RUNTIME_SCOPE_NAME
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.tape import StreamKind, TapeWriter

PART_ID = "news-tape-writer"

PART_DECLARATION = PartDeclaration(
    part_id="news-tape-writer",
    consumes=("raw-news-item", "news-item"),
    produces=("part-health",),
    resource_class="io-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="corrupts",
)

# The tape's first key is a venue, and news has no venue. "news" stands in it
# so a news tape sits beside `tape/upstox/...` instead of pretending a source is
# an exchange -- the same file layout, one honest level of naming.
NEWS_TAPE_VENUE = "news"

# Where an item that does not name its source is written. It is still real news
# and dropping it would lose data; filing it under a source id that is visibly
# not a source is what keeps it from being counted as one.
SOURCE_UNKNOWN = "source-not-stated"


def payload_bytes_of(item) -> bytes:
    """One item as JSON, every field it carries, nothing interpreted.

    `default=str` for the same reason the market tape writer uses it: a date or
    a Decimal in a payload must round-trip as the string it prints as rather
    than crashing the writer that holds the only copy of this news.
    """
    if dataclasses.is_dataclass(item) and not isinstance(item, type):
        fields = dataclasses.asdict(item)
    elif isinstance(item, dict):
        fields = item
    else:
        fields = {"repr": repr(item)}
    return json.dumps(fields, default=str, sort_keys=True).encode("utf-8")


def source_id_of(item) -> str:
    """Which source's tape this item belongs on."""
    source_id = getattr(item, "source_id", None)
    if not source_id and isinstance(item, dict):
        source_id = item.get("source_id")
    return str(source_id) if source_id else SOURCE_UNKNOWN


def source_published_at_ns_of(fields: dict) -> int:
    """When the SOURCE says this was published, or 0 if it did not say.

    `earliest_published_at_ns` as well as `published_at_ns`, because a
    `distinct-news-item` carries the earliest time any outlet published the
    story and that is the tradable one.
    """
    for name in ("published_at_ns", "earliest_published_at_ns"):
        value = fields.get(name)
        if value:
            return int(value)
    return 0


def published_at_ns_of(item, fields: dict) -> int:
    """The source's own clock for this item, or the observation when it gave none.

    Falling back to the observation rather than to zero: zero is 1970, and a
    tape record stamped 1970 would sort before every other record in the file
    and be replayed first forever. Whether the fallback was needed is counted
    by the caller -- an item the source never dated is a fact about the source.
    """
    return source_published_at_ns_of(fields) or int(fields.get("observed_at_ns") or 0)


def observed_at_ns_of(fields: dict) -> int:
    """When this system first saw the item, which decides the day it is filed on.

    The tape rolls by the day a record was RECEIVED, the same as every other
    tape here, and for news that is the honest partition: the backlog carries a
    week of publish times and a story first seen today belongs in today's file,
    because that is the day a replay could have acted on it. Passed explicitly
    rather than left to default to the wall clock, so importing a captured
    response files its items on the day they were observed and not on the day
    of the import.
    """
    for name in ("observed_at_ns", "first_observed_at_ns"):
        value = fields.get(name)
        if value:
            return int(value)
    return 0


class NewsTapeWriter:
    """One tape per source, opened on that source's first item."""

    def __init__(self, tape_root: pathlib.Path, writeback_interval_bytes: int) -> None:
        self._tape_root = pathlib.Path(tape_root)
        self._writeback_interval_bytes = writeback_interval_bytes
        self._writers: dict[str, TapeWriter] = {}
        self.raw_items_written = 0
        self.news_items_written = 0
        self.items_with_no_published_time = 0
        self.last_failure: str | None = None

    def write(self, item, is_raw: bool) -> None:
        """Append one item to its source's tape for the day it was observed."""
        fields = json.loads(payload_bytes_of(item))
        if not source_published_at_ns_of(fields):
            # Counted on the source's own field, BEFORE the fallback to the
            # observation -- counting afterwards made this read zero forever,
            # which is a health tile that can never report the thing it exists
            # to report.
            self.items_with_no_published_time += 1
        published_at_ns = published_at_ns_of(item, fields)
        observed_at_ns = observed_at_ns_of(fields)
        self._writer_for(source_id_of(item)).append(
            stream_kind=StreamKind.NEWS,
            payload=json.dumps(fields, sort_keys=True).encode("utf-8"),
            received_at_ns=observed_at_ns or None,
            venue_time_ns=published_at_ns,
        )
        if is_raw:
            self.raw_items_written += 1
        else:
            self.news_items_written += 1

    def _writer_for(self, source_id: str) -> TapeWriter:
        writer = self._writers.get(source_id)
        if writer is None:
            writer = TapeWriter(
                self._tape_root,
                NEWS_TAPE_VENUE,
                source_id,
                self._writeback_interval_bytes,
                stream_kind=StreamKind.NEWS,
            )
            self._writers[source_id] = writer
        return writer

    @property
    def open_tapes(self) -> int:
        return len(self._writers)

    def close(self) -> None:
        for writer in self._writers.values():
            writer.close()


def describe_writing(writer: NewsTapeWriter) -> dict:
    return {
        "part_id": PART_ID,
        "raw_items_written": writer.raw_items_written,
        # Zero until the structurer chain exists. Kept apart from the raw count
        # on purpose: one total would read as a working chain (Rule 8).
        "news_items_written": writer.news_items_written,
        "items_with_no_published_time": writer.items_with_no_published_time,
        "open_tapes": writer.open_tapes,
        "last_failure": writer.last_failure,
    }


def start_part(context) -> int:
    """The one entry point every part carries (T-1)."""
    settings = context.settings[RUNTIME_SCOPE_NAME]
    tape_root = pathlib.Path(str(settings.entries["tape_root"].value)).expanduser()
    writer = NewsTapeWriter(
        tape_root=tape_root,
        writeback_interval_bytes=int(context.number("writeback_interval")),
    )
    read_raw_items = context.bus.reader("raw-news-item")
    read_news_items = context.bus.reader("news-item")

    def write_all_pending() -> None:
        try:
            for message in read_raw_items():
                payload = message.payload
                for item in payload if isinstance(payload, tuple) else (payload,):
                    writer.write(item, is_raw=True)
            for message in read_news_items():
                payload = message.payload
                for item in payload if isinstance(payload, tuple) else (payload,):
                    writer.write(item, is_raw=False)
            writer.last_failure = None
        except OSError as failure:
            # The tape holds the only copy of this news, so a write that failed
            # is recorded and the tick ends rather than the part dying with the
            # reason only in a journal nobody joins up.
            writer.last_failure = f"{type(failure).__name__}: {failure}"

    try:
        return run_part(
            declaration=PART_DECLARATION,
            control_socket=context.control_socket,
            do_one_tick=write_all_pending,
            emit_health=context.emit_health,
            health_interval_seconds=context.health_interval_seconds,
            input_descriptors=context.input_descriptors,
            tick_floor_seconds=context.tick_floor_seconds,
            read_standing=lambda: describe_writing(writer),
        )
    finally:
        writer.close()


__all__ = [
    "NEWS_TAPE_VENUE",
    "NewsTapeWriter",
    "PART_DECLARATION",
    "PART_ID",
    "SOURCE_UNKNOWN",
    "describe_writing",
    "observed_at_ns_of",
    "payload_bytes_of",
    "published_at_ns_of",
    "source_id_of",
    "source_published_at_ns_of",
    "start_part",
]
