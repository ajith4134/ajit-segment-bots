"""venue-quote-stream-reader: every symbol's resting bid and ask, as the venue sends it.

The price a symbol has when nobody is trading it. Measured on the live run of
2026-08-24, `instrument-selector` refused 525 of 9,945 intents for a price too old
and exactly zero for never having seen a price -- so the refusals were never a
coverage gap. They were symbols this system had captured, whose last trade was
simply old, because nobody had traded them. A quote exists anyway.

Pure transport, deterministic by design (RL-060): this part judges nothing. It
subscribes to what `stream-plan` assigned it, and publishes what arrived.

**It writes no tape.** A trade is what the venue printed and cannot be recovered
later, so it is recorded. A quote is what a symbol is worth right now, is priced
against rather than learned from, and at 1,190 updates a second on Bybit alone
would be more volume than every trade this system records. Taping quotes is a
separate decision with its own storage cost, and it is not this one.

The one venue oddity it must survive is handled upstream in the adapter and in
`runtime.quote_assembly`: Bybit amends rather than restates, so a message can name
a bid and no ask. The assembler is what remembers the other side, and it dates a
merged quote by its **stalest** half -- an active bid must never make a forgotten
ask look fresh, which is the failure of 2026-08-23 arrived at by a different road.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from runtime.part_context import RUNTIME_SCOPE as RUNTIME_SCOPE_NAME
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.quote_assembly import QuoteAssembler
from runtime.tape import StreamKind
from runtime.venues.stream_connection import VenueStreamConnection
from runtime.venues.venue_adapter import VenueAdapter

PART_ID = "venue-quote-stream-reader"

# Read without importing this module (RL-070). Its consumes and produces equal the
# blueprint's, which is what RL-067 requires and the wiring probe checks.
PART_DECLARATION = PartDeclaration(
    part_id="venue-quote-stream-reader",
    # No `venue-standing`, for the same reason as venue-trade-stream-reader:
    # declared, never bound, producer off, part being retired (2026-09-06,
    # docs/proposals/a-declared-input-must-actually-be-read.md).
    consumes=("stream-plan",),
    produces=("market-quote", "part-health"),
    resource_class="bandwidth-bound",
    rate_risk="changes-the-answer",
    # A quote is a level: the next message carries what is true now, and nothing
    # accumulates. Unlike a trade, a missed one is not history lost -- which is
    # exactly why this stream is not taped and the trade stream is.
    skipped_tick_effect="delays",
)

CAPTURED_STREAM_KIND = StreamKind.QUOTE


@dataclass
class QuoteReaderStanding:
    """What one venue's quote reader has done, from its own counters."""

    venue_id: str
    connections: int = 0
    messages_drained: int = 0
    changes_read: int = 0
    quotes_published: int = 0
    assembler: dict = field(default_factory=dict)


class VenueQuoteStreamReader:
    """One venue's quote connections, and the assembler that completes their quotes.

    Holds the assembler rather than a tape writer, which is the whole difference
    between this and the three readers built on `StreamTapeRecorder`.
    """

    def __init__(
        self,
        adapter: VenueAdapter,
        plan,
        reconnect_backoff_floor_seconds: float,
        reconnect_backoff_ceiling_seconds: float,
        drain_interval_seconds: float,
    ) -> None:
        self._adapter = adapter
        self._assembler = QuoteAssembler(
            venue_id=adapter.venue_id,
            amends_rather_than_restates=adapter.quote_stream_amends_rather_than_restates(),
        )
        self._connections = tuple(
            VenueStreamConnection(
                adapter=adapter,
                requests=assignment.requests,
                reconnect_backoff_floor_seconds=reconnect_backoff_floor_seconds,
                reconnect_backoff_ceiling_seconds=reconnect_backoff_ceiling_seconds,
                drain_interval_seconds=drain_interval_seconds,
            )
            for assignment in plan.assignments_for(adapter.venue_id, CAPTURED_STREAM_KIND)
        )
        self.standing = QuoteReaderStanding(
            venue_id=adapter.venue_id, connections=len(self._connections)
        )

    @property
    def venue_id(self) -> str:
        return self._adapter.venue_id

    def read_one_tick(self) -> tuple:
        """Drain every connection once and return the quotes that are now complete.

        A message that completes no quote returns nothing and is not an error:
        most of Bybit's tickers messages amend funding or open interest and touch
        neither side of the book.
        """
        completed = []
        for connection in self._connections:
            for payload in connection.drain():
                self.standing.messages_drained += 1
                for change in self._adapter.read_quote_changes(payload):
                    self.standing.changes_read += 1
                    quote = self._assembler.apply_change(change)
                    if quote is not None:
                        completed.append(quote)
        self.standing.quotes_published += len(completed)
        self.standing.assembler = self._assembler.describe()
        return tuple(completed)

    def close(self) -> None:
        for connection in self._connections:
            connection.close()


def describe_quote_reading(readers) -> dict:
    """Every venue's quote standing, merged into one -- the shape a part reports.

    Merged rather than one venue's, because a part reports one standing and this
    part carries every captured venue. A reader that reported only the first
    would make a dead second venue invisible.
    """
    standing = {
        "part_id": PART_ID,
        "venues": len(readers),
        "connections": 0,
        "messages_drained": 0,
        "changes_read": 0,
        "quotes_published": 0,
        "symbols_held": 0,
        "changes_naming_no_side": 0,
        "changes_still_incomplete": 0,
        "restated_incompletely": 0,
    }
    for reader in readers.values():
        standing["connections"] += reader.standing.connections
        standing["messages_drained"] += reader.standing.messages_drained
        standing["changes_read"] += reader.standing.changes_read
        standing["quotes_published"] += reader.standing.quotes_published
        for key in (
            "symbols_held",
            "changes_naming_no_side",
            "changes_still_incomplete",
            "restated_incompletely",
        ):
            standing[key] += reader.standing.assembler.get(key, 0)
    return standing


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    **The plan comes from the bus, never from here.** This part consumes
    `stream-plan`, and until one arrives it subscribes to nothing and says so. A
    reader that built its own plan would be a part deciding what to capture, which
    is `stream-budget-planner`'s job -- and on a venue with an all-market quote
    topic that plan is a single subscription, which is why the planner asks the
    adapter rather than counting symbols.

    Normalisation happens here rather than in the parts downstream: the adapter
    turns a venue's phrasing into `QuoteChange`, the assembler turns those into
    whole quotes, and no consumer ever sees `bid1Price`.
    """
    from runtime.input_assembly import LatestValue
    from runtime.venues.adapter_registry import load_captured_venue_adapters

    settings = context.settings[RUNTIME_SCOPE_NAME]
    adapters = {adapter.venue_id: adapter for adapter in load_captured_venue_adapters(settings)}
    publish_quotes = context.bus.publisher_for("market-quote")
    plans = LatestValue(read=context.bus.reader("stream-plan"))

    readers: dict[str, VenueQuoteStreamReader] = {}
    planned = [None]

    def rebuild_readers_if_the_plan_changed() -> None:
        plan = plans.value()
        # Compared by value, never by identity -- the planner republishes an
        # unchanged plan on its health interval, and an identity check would tear
        # down and reopen every connection each time it did.
        if plan is None or plan == planned[0]:
            return
        for reader in readers.values():
            reader.close()
        readers.clear()
        for venue_id, adapter in sorted(adapters.items()):
            if not plan.assignments_for(venue_id, CAPTURED_STREAM_KIND):
                continue  # this plan gives that venue no quotes; not this part's call
            readers[venue_id] = VenueQuoteStreamReader(
                adapter=adapter,
                plan=plan,
                reconnect_backoff_floor_seconds=context.number("venue_reconnect_backoff_floor"),
                reconnect_backoff_ceiling_seconds=context.number(
                    "venue_reconnect_backoff_ceiling"
                ),
                drain_interval_seconds=context.number("stream_drain_interval"),
            )
        planned[0] = plan

    def read_and_publish() -> None:
        rebuild_readers_if_the_plan_changed()
        for reader in readers.values():
            quotes = reader.read_one_tick()
            if quotes:
                publish_quotes(quotes)

    try:
        return run_part(
            declaration=PART_DECLARATION,
            control_socket=context.control_socket,
            do_one_tick=read_and_publish,
            emit_health=context.emit_health,
            health_interval_seconds=context.health_interval_seconds,
            input_descriptors=context.input_descriptors,
            tick_floor_seconds=context.tick_floor_seconds,
            read_standing=lambda: describe_quote_reading(readers),
        )
    finally:
        for reader in readers.values():
            reader.close()


__all__ = [
    "CAPTURED_STREAM_KIND",
    "PART_DECLARATION",
    "PART_ID",
    "QuoteReaderStanding",
    "VenueQuoteStreamReader",
    "describe_quote_reading",
    "start_part",
]
