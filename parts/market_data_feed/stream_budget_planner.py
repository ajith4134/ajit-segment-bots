"""stream-budget-planner: which subscriptions ride which connection, and whether they can.

It is the only part that answers "does this fit on this machine". Every reader
takes the plan it produces and opens exactly what it was given, so the arithmetic
of scarcity happens once, here, against measured facts rather than assumed ones.

**It refuses to plan rather than guess** (spec §9). `hardware_facts` reports
`None` for a fact this machine does not publish -- a container with no CPU
topology, a NUMA tree that is not mounted -- and None is never a zero, a default,
or the other field's value. A connection budget invented from a core count that
was never measured is a budget the machine cannot honour, and the failure arrives
as a killed process under load rather than as a refusal at planning time.

Three things break as the symbol count grows, and each is checked here (§4.2):

1. **Subscription packing.** Asked of the adapter, never computed -- Binance caps
   a connection at 1024 streams, Bybit at 21,000 characters of subscribe payload,
   and the second scales with symbol-name length.
2. **Connection count.** Derived from the adapter's own limits and checked against
   the venue's stated concurrent-connection ceiling where it states one.
3. **Open files.** One tape is two files per (venue, symbol), plus a socket per
   connection. At the full 1,295-symbol universe that is 2,590 files against a
   1024 soft limit, which is why the full-universe path is blocked on this and
   says so rather than being discovered at three in the morning.

A venue whose standing withholds it is left out of the plan and *named* as left
out. A plan that silently omitted a venue would look exactly like a plan for a
world with one fewer venue in it.
"""

from __future__ import annotations

import resource
import time
from dataclasses import dataclass
from typing import Mapping, Sequence

from runtime.hardware_facts import HardwareFacts
from runtime.part_context import RUNTIME_SCOPE as RUNTIME_SCOPE_NAME
from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.stream_plan import ConnectionAssignment, StreamPlan
from runtime.symbol_universe import CapturableSymbol
from runtime.tape import StreamKind
from runtime.venues.venue_adapter import StreamRequest, VenueAdapter

PART_ID = "stream-budget-planner"

PART_DECLARATION = PartDeclaration(
    part_id="stream-budget-planner",
    consumes=("symbol-universe", "venue-standing", "hardware-capacity"),
    produces=("stream-plan", "part-health"),
    resource_class="compute-bound",
    rate_risk="latency-only",
    skipped_tick_effect="delays",
)

# One tape is an index and a blob, both open while a symbol is being captured.
# A wire-format fact of the tape (spec §2.2), not a decision.
TAPE_FILES_PER_SYMBOL = 2

# The hardware facts a plan cannot be made without. Named here so the refusal can
# say which one was missing rather than failing somewhere downstream on a None.
REQUIRED_HARDWARE_FACTS = ("logical_cpus", "physical_cores")


class PlanRefused(RuntimeError):
    """The machine or the venue cannot carry what was asked for, so nothing is planned.

    Refusing is the whole point. A plan trimmed to fit would drop symbols that
    nothing else captures, and the tape it produced would be indistinguishable
    from a complete one.
    """


@dataclass(frozen=True)
class StreamBudget:
    """A plan, and everything measured while making it. Nothing here is asserted.

    `venues_withheld` is a first-class field rather than an absence in the plan:
    Rule 8 says a thing that is not there renders as not-there, and a venue
    missing from a plan for a reason nobody recorded is the same as a venue
    nobody thought of.
    """

    plan: StreamPlan
    venues_withheld: tuple[str, ...]
    connections_by_venue: Mapping[str, int]
    subscriptions_by_venue: Mapping[str, int]
    open_files_required: int
    open_file_limit: int
    open_file_headroom: int
    hardware: HardwareFacts
    planned_at_ns: int


def read_open_file_limit() -> int:
    """This process's soft ceiling on open file descriptors, read not assumed.

    The soft limit rather than the hard one: the soft limit is what a call
    actually fails against, and raising it is a deliberate act by whoever starts
    the process, not something a planner may assume has happened.
    """
    soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    return soft


def require_measured_hardware(hardware: HardwareFacts) -> None:
    """Refuse a plan built on a fact this machine never published (spec §9)."""
    missing = [name for name in REQUIRED_HARDWARE_FACTS if getattr(hardware, name) is None]
    if missing:
        raise PlanRefused(
            f"hardware-capacity is missing {', '.join(missing)} on this machine. A connection "
            f"budget derived from a core count that was never measured is a budget the machine "
            f"cannot honour, and it fails as a killed process under load rather than as this."
        )


def pack_requests_onto_connections(
    adapter: VenueAdapter, requests: Sequence[StreamRequest]
) -> tuple[ConnectionAssignment, ...]:
    """Fill connections with subscriptions until the adapter says one will not fit.

    Grouped by endpoint first, because one connection carries one route. Binance
    serves `@aggTrade` and `@kline_*` from `/market` and `@depth*` from
    `/public`, and a connection carrying both delivers only one half while
    staying open and erroring nothing.
    """
    by_endpoint: dict[str, list[StreamRequest]] = {}
    for request in requests:
        by_endpoint.setdefault(adapter.stream_endpoint_url(request.stream_kind), []).append(request)

    assignments: list[ConnectionAssignment] = []
    for _endpoint, endpoint_requests in sorted(by_endpoint.items()):
        current: list[StreamRequest] = []
        topics: list[str] = []
        for request in endpoint_requests:
            topic = adapter.subscription_topic(request)
            if current and not adapter.does_topic_fit_connection(topics, topic):
                assignments.append(
                    ConnectionAssignment(venue_id=adapter.venue_id, requests=tuple(current))
                )
                current, topics = [], []
            if not current and not adapter.does_topic_fit_connection([], topic):
                raise PlanRefused(
                    f"{adapter.venue_id}: {topic} does not fit even an empty connection. "
                    f"Nothing can carry it, so it would be a symbol with no tape and no error."
                )
            current.append(request)
            topics.append(topic)
        if current:
            assignments.append(
                ConnectionAssignment(venue_id=adapter.venue_id, requests=tuple(current))
            )
    return tuple(assignments)


def _requests_for_kind(
    adapter: VenueAdapter,
    stream_kind: StreamKind,
    symbols: Sequence[CapturableSymbol],
    candle_interval: str,
    book_depth_levels: int,
    book_symbols_when_thinnable: int | None = None,
    book_symbols_when_every_message_kept: int | None = None,
) -> tuple[StreamRequest, ...]:
    """One request per symbol, for every stream kind including quotes.

    An earlier version of this collapsed quotes to a venue's all-market topic
    where it had one, so the plan would claim one connection rather than many.
    Measured 2026-08-24 on Binance, same symbol and same 30-second window:

        per-symbol btcusdt@bookTicker   6,598 updates   (219.9/s)
        all-market !bookTicker              6 updates   (  0.2/s)

    The all-market stream is throttled by about a thousand times per symbol. It
    is a coverage tool, not a freshness tool -- and a quote's entire value is its
    age. Against the 1.0-1.5s a volatile symbol's own moves say a price may be
    believed, a quote arriving every five seconds is stale before it lands, which
    is why the quote fallback rescued only a twentieth of the refusals it was
    built to rescue.

    So the plan names the symbols decisions are actually made on. Whole-universe
    coverage remains available -- `every_symbol_quote_topic` is still the venue's
    answer for it -- but it is a different question from this one, and answering
    it here cost the freshness the feed exists for.
    """
    if not symbols:
        return ()
    # The book is the one kind capped below the universe, and the cap is about
    # bytes rather than about descriptors: a depth stream pushes a fresh ladder
    # every hundred milliseconds where a trade stream pushes only when somebody
    # trades, onto the disk that holds the only copy of the tape.
    #
    # But the two venues do not cost the same, and one number for both charged
    # every venue the worst venue's price. Measured off the tape on 2026-08-28,
    # over the same days and the same ten symbols a venue:
    #
    #     bybit-linear    0.377 GB/symbol/day   (2026-08-26, a busy day)
    #     binance-usdm    0.008 GB/symbol/day   (the same day)
    #
    # Forty-seven times apart, and the reason is already in this codebase: a venue
    # that resends the whole ladder can be thinned to the snapshot interval and
    # lose only resolution, while a delta stream must be recorded whole or every
    # later price is wrong. So the venue that can be thinned carries the wide
    # universe and the venue that cannot stays narrow -- and the adapter's own
    # answer decides which it is, never a venue named in a setting.
    if stream_kind is StreamKind.BOOK:
        cap = (
            book_symbols_when_thinnable
            if adapter.book_stream_delivers_full_depth()
            else book_symbols_when_every_message_kept
        )
        if cap is not None:
            symbols = symbols[:cap]
    return tuple(
        StreamRequest(
            stream_kind=stream_kind,
            symbol=entry.symbol,
            candle_interval=candle_interval,
            book_depth_levels=book_depth_levels,
        )
        for entry in symbols
    )


def plan_stream_budget(
    adapters: Mapping[str, VenueAdapter],
    symbol_universe: Mapping[str, Sequence[CapturableSymbol]],
    stream_kinds: Sequence[StreamKind],
    hardware: HardwareFacts,
    open_file_headroom: int,
    candle_interval: str,
    book_depth_levels: int,
    book_symbols_when_thinnable: int | None = None,
    book_symbols_when_every_message_kept: int | None = None,
    venues_withheld: Sequence[str] = (),
    open_file_limit: int | None = None,
) -> StreamBudget:
    """Assign every captured symbol's streams to connections, or refuse and say why."""
    require_measured_hardware(hardware)
    if not stream_kinds:
        raise PlanRefused(
            "no stream kinds were asked for. A plan for nothing is a plan that reads as "
            "satisfied while capturing nothing at all."
        )

    withheld = tuple(sorted(set(venues_withheld) & set(adapters)))
    limit = read_open_file_limit() if open_file_limit is None else open_file_limit

    assignments: list[ConnectionAssignment] = []
    connections_by_venue: dict[str, int] = {}
    subscriptions_by_venue: dict[str, int] = {}
    symbols_open = 0

    for venue_id in sorted(adapters):
        if venue_id in withheld:
            connections_by_venue[venue_id] = 0
            subscriptions_by_venue[venue_id] = 0
            continue
        adapter = adapters[venue_id]
        symbols = symbol_universe.get(venue_id, ())
        requests = [
            request
            for stream_kind in stream_kinds
            for request in _requests_for_kind(
                adapter=adapter,
                stream_kind=stream_kind,
                symbols=symbols,
                candle_interval=candle_interval,
                book_symbols_when_thinnable=book_symbols_when_thinnable,
                book_symbols_when_every_message_kept=book_symbols_when_every_message_kept,
                book_depth_levels=book_depth_levels,
            )
        ]
        venue_assignments = pack_requests_onto_connections(adapter, requests) if requests else ()
        _refuse_if_over_concurrent_limit(adapter, len(venue_assignments))

        assignments.extend(venue_assignments)
        connections_by_venue[venue_id] = len(venue_assignments)
        subscriptions_by_venue[venue_id] = len(requests)
        symbols_open += len(symbols)

    # Quote connections are counted as sockets and not as tape files: this stream
    # is priced against, never recorded, so it opens no {index, blob} pair per
    # symbol the way a trade capture does.
    open_files_required = symbols_open * TAPE_FILES_PER_SYMBOL + len(assignments)
    if open_files_required > limit - open_file_headroom:
        raise PlanRefused(
            f"this plan needs {open_files_required} open files -- {symbols_open} symbols at "
            f"{TAPE_FILES_PER_SYMBOL} tape files each plus {len(assignments)} sockets -- against a "
            f"soft limit of {limit} with {open_file_headroom} held back. Spec §4.2: the "
            f"full-universe path is blocked on this ceiling, and it is stated rather than "
            f"discovered as a capture that stopped opening tapes partway through the alphabet."
        )

    return StreamBudget(
        plan=StreamPlan(connections=tuple(assignments)),
        venues_withheld=withheld,
        connections_by_venue=connections_by_venue,
        subscriptions_by_venue=subscriptions_by_venue,
        open_files_required=open_files_required,
        open_file_limit=limit,
        open_file_headroom=open_file_headroom,
        hardware=hardware,
        planned_at_ns=time.time_ns(),
    )


def _refuse_if_over_concurrent_limit(adapter: VenueAdapter, connections: int) -> None:
    """Check the venue's own concurrent-connection ceiling, where it states one.

    Where it does not -- Binance publishes no such figure for futures at all --
    there is nothing to check against, and inventing one would be this project
    enforcing a limit the venue never set.
    """
    ceiling = adapter.connection_discipline().concurrent_connections
    if ceiling is not None and connections > ceiling:
        raise PlanRefused(
            f"{adapter.venue_id} would need {connections} connections against its stated "
            f"ceiling of {ceiling} concurrent connections per IP"
        )


def describe_budget(budget: StreamBudget) -> dict:
    """Everything the plan was measured against. No field here asserts health."""
    return {
        "part_id": PART_ID,
        "planned_at_ns": budget.planned_at_ns,
        "connections": len(budget.plan.connections),
        "connections_by_venue": dict(budget.connections_by_venue),
        "subscriptions_by_venue": dict(budget.subscriptions_by_venue),
        "venues_withheld": list(budget.venues_withheld),
        "open_files_required": budget.open_files_required,
        "open_file_limit": budget.open_file_limit,
        "open_file_headroom": budget.open_file_headroom,
        "hardware": {
            "physical_cores": budget.hardware.physical_cores,
            "logical_cpus": budget.hardware.logical_cpus,
            "numa_nodes": budget.hardware.numa_nodes,
            "available_ram_bytes": budget.hardware.available_ram_bytes,
            "measured_at_ns": budget.hardware.measured_at_ns,
        },
    }


def run_stream_budget_planner(
    adapters: Mapping[str, VenueAdapter],
    control_socket,
    read_symbol_universe,
    read_venues_withheld,
    stream_kinds: Sequence[StreamKind],
    open_file_headroom: int,
    candle_interval: str,
    book_depth_levels: int,
    book_symbols_when_thinnable: int,
    book_symbols_when_every_message_kept: int,
    publish_plan,
    health_interval_seconds: float,
    emit_health,
    input_descriptors: tuple[int, ...] = (),
    tick_floor_seconds: float = 0.0,
) -> int:
    """Re-plan on every tick and hand the result to whoever publishes `stream-plan`.

    The hardware is re-measured each time rather than read once at start: a
    plan's whole claim is that the machine can carry it, and a machine whose
    available memory has halved since start is a different machine.

    A refusal does not end the part. It is reported and the previous plan stands,
    because a reader already holding a workable plan should keep capturing while
    the reason for the refusal is looked at -- stopping would cost tape for a
    condition that may be a transient reading.
    """
    from runtime.hardware_facts import measure_hardware_facts

    last_budget: list = [None]
    refusals: list[int] = [0]

    def plan_once() -> None:
        try:
            budget = plan_stream_budget(
                adapters=adapters,
                symbol_universe=read_symbol_universe(),
                stream_kinds=stream_kinds,
                hardware=measure_hardware_facts(),
                open_file_headroom=open_file_headroom,
                candle_interval=candle_interval,
                book_depth_levels=book_depth_levels,
                book_symbols_when_thinnable=book_symbols_when_thinnable,
                book_symbols_when_every_message_kept=book_symbols_when_every_message_kept,
                venues_withheld=read_venues_withheld(),
            )
        except PlanRefused as refusal:
            refusals[0] += 1
            publish_plan(None, str(refusal))
            return
        last_budget[0] = budget
        publish_plan(budget, None)

    def read_standing() -> dict:
        # The last plan's numeric facts, flattened per venue: a heartbeat's
        # standing keeps top-level numbers only. Before a first plan there is
        # nothing to describe, and saying so beats an empty dict.
        budget = last_budget[0]
        if budget is None:
            return {"part_id": PART_ID, "plans_made": 0, "plans_refused": refusals[0]}
        described = describe_budget(budget)
        flat: dict = {
            "part_id": PART_ID,
            "plans_refused": refusals[0],
            "connections": described["connections"],
            "open_files_required": described["open_files_required"],
            "open_file_headroom": described["open_file_headroom"],
        }
        for venue, count in described["subscriptions_by_venue"].items():
            flat[f"subscriptions.{venue}"] = count
        return flat

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=control_socket,
        do_one_tick=plan_once,
        emit_health=emit_health,
        health_interval_seconds=health_interval_seconds,
        input_descriptors=input_descriptors,
        tick_floor_seconds=tick_floor_seconds,
        read_standing=read_standing,
    )


__all__ = [
    "PART_DECLARATION",
    "PART_ID",
    "PlanRefused",
    "REQUIRED_HARDWARE_FACTS",
    "StreamBudget",
    "TAPE_FILES_PER_SYMBOL",
    "describe_budget",
    "pack_requests_onto_connections",
    "plan_stream_budget",
    "read_open_file_limit",
    "require_measured_hardware",
    "run_stream_budget_planner",
]


def start_part(context) -> int:
    """The one entry point every part carries (T-1).

    The universe arrives as one message per symbol and is regrouped here into the
    venue-to-symbols mapping the planner works in. `symbol-universe` is a level --
    these are the symbols we capture, now -- so the mapping is kept across ticks
    rather than rebuilt from whatever happened to arrive in the last one; a
    planner that saw an empty universe for one tick would refuse to plan and take
    the capture down with it.

    Keyed by (venue, symbol) rather than by symbol: the same ticker exists on both
    venues, and a map keyed by symbol alone would silently let one venue's listing
    overwrite the other's.
    """
    from runtime.input_assembly import Batch, LatestByKey
    from runtime.tape import StreamKind
    from runtime.venues.adapter_registry import load_captured_venue_adapters

    settings = context.settings[RUNTIME_SCOPE_NAME]
    adapters = {adapter.venue_id: adapter for adapter in load_captured_venue_adapters(settings)}
    if not adapters:
        raise RuntimeError(
            "captured_venues names no venue this build has an adapter for, so there is nothing "
            "to plan streams for."
        )

    universe = LatestByKey(
        read=context.bus.reader("symbol-universe"),
        key_of=lambda entry: (entry.venue_id, entry.symbol),
    )
    withheld = Batch(read=context.bus.reader("venue-standing"))
    publish_plan_messages = context.bus.publisher_for("stream-plan")

    def read_symbol_universe():
        by_venue: dict[str, list] = {}
        for entry in universe.values():
            by_venue.setdefault(entry.venue_id, []).append(entry)
        return by_venue

    def read_venues_withheld():
        return tuple(
            standing.venue_id
            for standing in withheld.payloads()
            if not getattr(standing, "may_request", True)
        )

    import time as _time

    last_published = [None, float("-inf")]
    republish_after = context.health_interval_seconds

    def publish_plan(budget, refusal) -> None:
        """A refusal is published as nothing, and recorded on the part's own standing.

        The plan type carries plans. A refusal is not a plan with zero connections
        -- a consumer reading that would subscribe to nothing and look healthy --
        so nothing is published and the reason stays where the board reads it.

        An unchanged plan is republished once per health interval, not on every
        tick. The part now wakes on each arriving message (2026-08-23) and
        re-plans every time, which sent the same plan to the feed reader thirty
        times a second; the reader's tick is a blocking socket drain, so it lost
        most of them and reported the loss. Once an interval keeps the property
        the old behaviour had -- a reader started after the planner still gets
        the plan within a second -- without the flood.
        """
        if budget is None:
            return
        now = _time.monotonic()
        if budget.plan == last_published[0] and now - last_published[1] < republish_after:
            return
        publish_plan_messages([budget.plan])
        last_published[0], last_published[1] = budget.plan, now

    return run_stream_budget_planner(
        adapters=adapters,
        control_socket=context.control_socket,
        read_symbol_universe=read_symbol_universe,
        read_venues_withheld=read_venues_withheld,
        # Quotes alongside trades. A trade is what the venue printed and is what
        # the tape keeps; a quote is what the symbol is worth right now and is
        # what a decision is sized against. A symbol that has not traded has only
        # the second, which is the whole reason this kind was added.
        #
        # Candles added 2026-08-25 with phase 6, and their absence was the reason
        # the whole prediction block ran and produced nothing. The chain is one
        # line long: this planner planned no candle stream, so ccxt-venue-reader
        # had nothing to subscribe to, so no NormalisedCandle ever reached
        # kline-window-builder -- which filters market-data for exactly that --
        # so no window, no vol-feature-set, no volatility forecast, and
        # stop-target-placer sized every stop it has ever placed as though no
        # forecast existed.
        #
        # Cost measured before adding rather than assumed: 50 symbols a venue is
        # 100 more subscriptions and about 100 more tape files, against a
        # RLIMIT_NOFILE soft limit of 524,288 on this box and 203 descriptors in
        # use. The descriptor ceiling is not what bounds this.
        #
        # The book added 2026-08-25, and its absence had the same shape as the
        # candles': nothing planned a book stream, so order-book-reader subscribed
        # to nothing, so no order-book-snapshot ever reached the bots' feature
        # builders -- which counted 56 of 56 vectors incomplete for want of one,
        # left the outlier rejector unable to judge any of them, and made
        # bull-conviction-model refuse every candidate it was ever handed. The
        # whole bull chain ran and formed no opinion, all the way from a detector
        # that had found 70 candidates.
        #
        # Capped below the universe, unlike every other kind: a depth stream
        # pushes a ladder every hundred milliseconds whether or not anything
        # trades. Two bounds rather than one, because the two venues cost 47x
        # apart -- a stream that resends the whole ladder can be thinned to the
        # snapshot interval, a delta stream cannot -- and one number charged both
        # the delta venue's price. Each note carries its measurement.
        # PREMIUM added 2026-08-28. Both adapters have answered for it since
        # 2026-08-26 -- endpoint, topic and sequence promise -- and nothing ever
        # planned it, so `venue-premium-stream-reader` had no assignment to open
        # and `funding-rate-forecaster` made zero forecasts across five million
        # messages. Uncapped, unlike BOOK: a premium is one message a second per
        # symbol, which is two orders of magnitude under a depth stream.
        stream_kinds=(
            StreamKind.TRADE, StreamKind.QUOTE, StreamKind.CANDLE, StreamKind.BOOK,
            StreamKind.PREMIUM,
        ),
        open_file_headroom=int(context.number("open_file_headroom")),
        candle_interval=settings.entries["candle_interval"].value,
        book_depth_levels=int(context.number("book_depth_levels")),
        book_symbols_when_thinnable=int(context.number("book_symbols_when_thinnable")),
        book_symbols_when_every_message_kept=int(
            context.number("book_symbols_when_every_message_kept")
        ),
        publish_plan=publish_plan,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        emit_health=context.emit_health,
    )
