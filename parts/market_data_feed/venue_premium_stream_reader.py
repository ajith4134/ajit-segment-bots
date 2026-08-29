"""venue-premium-stream-reader: what a perpetual is marked at against its index.

The number a funding rate is averaged from, and the one wire that carried it was
never opened. `market-data` is what printed and `market-quote` is what is
resting; a perpetual's mark price is neither, and its index price is not a price
on this venue at all -- it is the basket the venue marks to.

Without it `funding-rate-forecaster` had received 5,017,806 messages and made
zero forecasts, with zero premium observations and not one refusal: it never
reached the code that would refuse. Six parts consume `funding-forecast` and none
had ever seen one, which is why `tail-crowding-detector` reported NOT_MEASURED
for every candidate it was ever handed and the tailgating bot has never formed a
conviction.

Pure transport, deterministic by design (RL-060): this part judges nothing. It
subscribes to what `stream-plan` assigned it and publishes what arrived. Every
venue-specific fact it needs -- which endpoint carries the premium, what the
topic is called, which fields hold the mark and the index -- is the adapter's
answer, never this part's (T-4).

**It writes no tape**, for the reason the quote reader writes none. A premium is
what a symbol is marked at right now; it is priced against rather than learned
from, and the venue restates it every second whether or not it moved.

**One subscription per symbol, on both venues.** Binance offers an all-market
premium topic (`!markPrice@arr@1s`, measured at 740 symbols in one frame on
2026-08-26) and taking it would collapse fifty subscriptions into one. That is a
planner change rather than a reader change -- the plan is what names symbols --
and it is not made here, because the per-symbol form works on both venues and
Bybit has no all-market premium topic to match it with. Worth doing on a measured
subscription budget, not on the guess that fifty is too many.

The venue oddity this must survive is the same one the quote reader has, on the
other half of the same Bybit message: `tickers` amends rather than restates, so a
delta can carry a mark price and no index. `runtime.premium_assembly` is what
remembers the rest, and it dates a merged premium by its **stalest** half -- an
index quoted forty seconds ago must not be made to look fresh by a mark price
that moved a millisecond ago, because the premium between them is exactly as
current as the older number.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from runtime.part_context import RUNTIME_SCOPE as RUNTIME_SCOPE_NAME
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.premium_assembly import PremiumAssembler
from runtime.tape import StreamKind
from runtime.venues.stream_connection import VenueStreamConnection
from runtime.venues.venue_adapter import VenueAdapter

PART_ID = "venue-premium-stream-reader"

# Read without importing this module (RL-070). Its consumes and produces equal the
# blueprint's, which is what RL-067 requires and the wiring probe checks.
PART_DECLARATION = PartDeclaration(
    part_id="venue-premium-stream-reader",
    consumes=("stream-plan", "venue-standing"),
    produces=("venue-premium", "part-health"),
    resource_class="bandwidth-bound",
    rate_risk="changes-the-answer",
    # A premium is a level: the next message carries what is true now, and nothing
    # accumulates. A missed one is not history lost, which is why this stream is
    # not taped and the trade stream is.
    skipped_tick_effect="delays",
)

CAPTURED_STREAM_KIND = StreamKind.PREMIUM


@dataclass
class PremiumReaderStanding:
    """What one venue's premium reader has done, from its own counters."""

    venue_id: str
    connections: int = 0
    messages_drained: int = 0
    premiums_read: int = 0
    premiums_published: int = 0
    assembler: dict = field(default_factory=dict)


class VenuePremiumStreamReader:
    """One venue's premium connections, and the assembler that completes their premiums."""

    def __init__(
        self,
        adapter: VenueAdapter,
        plan,
        reconnect_backoff_floor_seconds: float,
        reconnect_backoff_ceiling_seconds: float,
        drain_interval_seconds: float,
    ) -> None:
        self._adapter = adapter
        self._assembler = PremiumAssembler(venue_id=adapter.venue_id)
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
        self.standing = PremiumReaderStanding(
            venue_id=adapter.venue_id, connections=len(self._connections)
        )

    @property
    def venue_id(self) -> str:
        return self._adapter.venue_id

    def read_one_tick(self) -> tuple:
        """Drain every connection once and return the premiums that are now complete.

        A message that completes no premium returns nothing and is not an error:
        most of Bybit's tickers messages amend a quote or open interest and touch
        neither the mark price nor the index.
        """
        completed = []
        for connection in self._connections:
            for payload in connection.drain():
                self.standing.messages_drained += 1
                for premium in self._adapter.read_premiums(payload):
                    self.standing.premiums_read += 1
                    whole = self._assembler.apply(premium)
                    if whole is not None:
                        completed.append(whole)
        self.standing.premiums_published += len(completed)
        self.standing.assembler = self._assembler.describe()
        return tuple(completed)

    def close(self) -> None:
        for connection in self._connections:
            connection.close()


def describe_premium_reading(readers) -> dict:
    """Every venue's premium standing, merged into one -- the shape a part reports.

    Merged rather than one venue's, because a part reports one standing and this
    part carries every captured venue. A reader that reported only the first would
    make a dead second venue invisible.
    """
    standing = {
        "part_id": PART_ID,
        "venues": len(readers),
        "connections": 0,
        "messages_drained": 0,
        "premiums_read": 0,
        "premiums_published": 0,
        "symbols_held": 0,
        "messages_naming_no_premium": 0,
        "messages_still_incomplete": 0,
        "unusable_index_prices": 0,
    }
    for reader in readers.values():
        standing["connections"] += reader.standing.connections
        standing["messages_drained"] += reader.standing.messages_drained
        standing["premiums_read"] += reader.standing.premiums_read
        standing["premiums_published"] += reader.standing.premiums_published
        for key in (
            "symbols_held",
            "messages_naming_no_premium",
            "messages_still_incomplete",
            "unusable_index_prices",
        ):
            standing[key] += reader.standing.assembler.get(key, 0)
    return standing


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    **The plan comes from the bus, never from here.** This part consumes
    `stream-plan`, and until one arrives it subscribes to nothing and says so. A
    reader that built its own plan would be a part deciding what to capture, which
    is `stream-budget-planner`'s job.

    Normalisation happens here rather than downstream: the adapter turns a venue's
    phrasing into `VenuePremium`, the assembler turns amends into whole premiums,
    and no consumer ever sees `markPrice` or `i`.
    """
    from runtime.input_assembly import LatestValue
    from runtime.venues.adapter_registry import load_captured_venue_adapters

    settings = context.settings[RUNTIME_SCOPE_NAME]
    adapters = {adapter.venue_id: adapter for adapter in load_captured_venue_adapters(settings)}
    publish_premiums = context.bus.publisher_for("venue-premium")
    plans = LatestValue(read=context.bus.reader("stream-plan"))

    readers: dict[str, VenuePremiumStreamReader] = {}
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
                continue  # this plan gives that venue no premiums; not this part's call
            readers[venue_id] = VenuePremiumStreamReader(
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
            premiums = reader.read_one_tick()
            if premiums:
                publish_premiums(premiums)

    try:
        return run_part(
            declaration=PART_DECLARATION,
            control_socket=context.control_socket,
            do_one_tick=read_and_publish,
            emit_health=context.emit_health,
            health_interval_seconds=context.health_interval_seconds,
            input_descriptors=context.input_descriptors,
            tick_floor_seconds=context.tick_floor_seconds,
            read_standing=lambda: describe_premium_reading(readers),
        )
    finally:
        for reader in readers.values():
            reader.close()


__all__ = [
    "CAPTURED_STREAM_KIND",
    "PART_DECLARATION",
    "PART_ID",
    "PremiumReaderStanding",
    "VenuePremiumStreamReader",
    "describe_premium_reading",
    "start_part",
]
