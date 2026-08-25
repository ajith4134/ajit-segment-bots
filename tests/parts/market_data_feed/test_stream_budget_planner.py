"""The planner, against the real adapters and this machine's real capacity.

Nothing here is a fixture of a machine. `measure_hardware_facts()` reads this
box, the adapters are the ones the captures actually run on, and the symbol
universe is built from the real captured catalogues -- so what is tested is the
arithmetic against facts, not against numbers chosen to make it pass.

The refusals get the most attention, because refusing is the part's whole job.
A planner that trimmed to fit would drop symbols nothing else captures, and the
tape it produced would be indistinguishable from a complete one.
"""

import dataclasses

import pytest

from parts.market_data_feed.stream_budget_planner import (
    PART_DECLARATION,
    PART_ID,
    REQUIRED_HARDWARE_FACTS,
    TAPE_FILES_PER_SYMBOL,
    PlanRefused,
    describe_budget,
    pack_requests_onto_connections,
    plan_stream_budget,
    read_open_file_limit,
    require_measured_hardware,
)
from runtime.hardware_facts import measure_hardware_facts
from runtime.part_declaration import load_declaration_from_blueprint
from runtime.symbol_universe import CapturableSymbol
from runtime.tape import StreamKind
from runtime.venues.adapter_registry import load_venue_adapter
from runtime.venues.venue_adapter import StreamRequest

CANDLE_INTERVAL = "1m"
BOOK_DEPTH = 20
HEADROOM = 128
EVERY_KIND = (StreamKind.TRADE, StreamKind.CANDLE, StreamKind.BOOK)


@pytest.fixture
def adapters():
    return {
        venue_id: load_venue_adapter(venue_id)
        for venue_id in ("binance-usdm", "bybit-linear")
    }


@pytest.fixture
def hardware():
    return measure_hardware_facts()


def universe(venue_id, count, name=lambda index: f"SYM{index}USDT"):
    return [
        CapturableSymbol(
            venue_id=venue_id,
            symbol=name(index),
            contract_type="PERPETUAL",
            quote_volume_24h=float(count - index),
            price_increment=0.01,
        )
        for index in range(count)
    ]


def plan(adapters, hardware, symbols_per_venue=30, kinds=EVERY_KIND, **overrides):
    return plan_stream_budget(
        adapters=adapters,
        symbol_universe={
            venue_id: universe(venue_id, symbols_per_venue) for venue_id in adapters
        },
        stream_kinds=kinds,
        hardware=hardware,
        open_file_headroom=overrides.pop("headroom", HEADROOM),
        candle_interval=CANDLE_INTERVAL,
        book_depth_levels=BOOK_DEPTH,
        **overrides,
    )


def test_the_built_wiring_equals_the_blueprint():
    assert PART_DECLARATION == load_declaration_from_blueprint(PART_ID)


def test_a_plan_covers_every_symbol_and_every_stream_kind(adapters, hardware):
    budget = plan(adapters, hardware)
    planned = {
        (assignment.venue_id, request.stream_kind, request.symbol)
        for assignment in budget.plan.connections
        for request in assignment.requests
    }
    assert len(planned) == len(adapters) * 30 * len(EVERY_KIND)
    for venue_id in adapters:
        assert budget.subscriptions_by_venue[venue_id] == 30 * len(EVERY_KIND)
        assert budget.connections_by_venue[venue_id] >= 1


def test_binance_splits_its_routes_across_connections_and_bybit_does_not(adapters, hardware):
    """One connection carries one route, and only one venue has more than one.

    This is the §1.1 hazard expressed as arithmetic: a Binance plan that put
    `/market` and `/public` subscriptions on the same connection would deliver
    half of them and error nothing.
    """
    budget = plan(adapters, hardware)
    for venue_id, adapter in adapters.items():
        assignments = [a for a in budget.plan.connections if a.venue_id == venue_id]
        for assignment in assignments:
            routes = {adapter.stream_endpoint_url(r.stream_kind) for r in assignment.requests}
            assert len(routes) == 1, f"{venue_id} mixed routes {routes} on one connection"
    assert budget.connections_by_venue["binance-usdm"] == 2, "trades+candles on /market, book on /public"
    assert budget.connections_by_venue["bybit-linear"] == 1


def test_a_long_symbol_set_needs_more_connections_on_the_character_capped_venue(adapters, hardware):
    """Bybit's cap is characters, so how many fit depends on symbol-name length."""
    bybit = {"bybit-linear": adapters["bybit-linear"]}
    short = plan_stream_budget(
        adapters=bybit,
        symbol_universe={"bybit-linear": universe("bybit-linear", 700, lambda i: f"A{i}USDT")},
        stream_kinds=(StreamKind.TRADE,),
        hardware=hardware,
        open_file_headroom=HEADROOM,
        candle_interval=CANDLE_INTERVAL,
        book_depth_levels=BOOK_DEPTH,
    )
    long_names = plan_stream_budget(
        adapters=bybit,
        symbol_universe={
            "bybit-linear": universe("bybit-linear", 700, lambda i: f"AVERYLONGSYMBOLNAME{i}USDT")
        },
        stream_kinds=(StreamKind.TRADE,),
        hardware=hardware,
        open_file_headroom=HEADROOM,
        candle_interval=CANDLE_INTERVAL,
        book_depth_levels=BOOK_DEPTH,
    )
    assert long_names.connections_by_venue["bybit-linear"] > short.connections_by_venue["bybit-linear"]


def test_the_full_universe_is_planned_on_this_machine(adapters, hardware):
    """1,295 symbols is the number the user's condition asked to keep reachable.

    It is planned rather than refused here, which is a fact about this box: its
    descriptor soft limit is 524,288, not the 1024 spec §4.2 assumed. The
    ceiling is still checked, because the limit belongs to however the process
    was started rather than to the design.
    """
    binance, bybit = adapters["binance-usdm"], adapters["bybit-linear"]
    budget = plan_stream_budget(
        adapters=adapters,
        symbol_universe={
            "binance-usdm": universe("binance-usdm", 570),
            "bybit-linear": universe("bybit-linear", 725),
        },
        stream_kinds=EVERY_KIND,
        hardware=hardware,
        open_file_headroom=HEADROOM,
        candle_interval=CANDLE_INTERVAL,
        book_depth_levels=BOOK_DEPTH,
    )
    assert budget.open_files_required == 1295 * TAPE_FILES_PER_SYMBOL + len(budget.plan.connections)
    assert budget.open_files_required < budget.open_file_limit - HEADROOM
    del binance, bybit


def test_a_descriptor_ceiling_that_cannot_carry_the_plan_is_refused(adapters, hardware):
    """Spec §4.2's blocker, stated at planning time rather than found at 3 a.m."""
    with pytest.raises(PlanRefused) as refusal:
        plan(adapters, hardware, symbols_per_venue=30, open_file_limit=100)
    assert "open files" in str(refusal.value)
    assert "§4.2" in str(refusal.value) or "4.2" in str(refusal.value)


def test_a_missing_hardware_fact_refuses_the_plan_rather_than_defaulting(adapters, hardware):
    """Spec §9: None is never a zero, a default, or the other field's value."""
    for missing in REQUIRED_HARDWARE_FACTS:
        unmeasured = dataclasses.replace(hardware, **{missing: None})
        with pytest.raises(PlanRefused) as refusal:
            plan(adapters, unmeasured)
        assert missing in str(refusal.value)
        with pytest.raises(PlanRefused):
            require_measured_hardware(unmeasured)


def test_planning_no_stream_kinds_is_refused(adapters, hardware):
    """A plan for nothing reads as satisfied while capturing nothing at all."""
    with pytest.raises(PlanRefused):
        plan(adapters, hardware, kinds=())


def test_a_withheld_venue_is_left_out_and_named(adapters, hardware):
    """Rule 8: a venue absent from a plan for an unrecorded reason is a venue nobody thought of."""
    budget = plan(adapters, hardware, venues_withheld=("bybit-linear",))
    assert budget.venues_withheld == ("bybit-linear",)
    assert budget.connections_by_venue["bybit-linear"] == 0
    assert budget.subscriptions_by_venue["bybit-linear"] == 0
    assert all(a.venue_id != "bybit-linear" for a in budget.plan.connections)
    assert any(a.venue_id == "binance-usdm" for a in budget.plan.connections)


def test_withholding_a_venue_nobody_captures_names_nothing(adapters, hardware):
    budget = plan(adapters, hardware, venues_withheld=("okx-swap",))
    assert budget.venues_withheld == ()


def test_the_open_file_limit_is_read_from_the_process_not_assumed():
    limit = read_open_file_limit()
    assert isinstance(limit, int) and limit > 0


def test_packing_asks_the_adapter_and_never_counts_for_itself(adapters):
    """Every connection the packer produces must satisfy the adapter's own fit rule."""
    for venue_id, adapter in adapters.items():
        requests = [
            StreamRequest(StreamKind.TRADE, entry.symbol) for entry in universe(venue_id, 900)
        ]
        assignments = pack_requests_onto_connections(adapter, requests)
        assert sum(len(a.requests) for a in assignments) == len(requests)
        for assignment in assignments:
            topics = []
            for request in assignment.requests:
                topic = adapter.subscription_topic(request)
                assert adapter.does_topic_fit_connection(topics, topic)
                topics.append(topic)


def test_a_venue_over_its_own_concurrent_connection_ceiling_is_refused(adapters, hardware):
    """Bybit states 1,000 concurrent per IP; Binance states none, so none is checked."""
    bybit = adapters["bybit-linear"]
    assert bybit.connection_discipline().concurrent_connections == 1000
    assert adapters["binance-usdm"].connection_discipline().concurrent_connections is None

    from parts.market_data_feed.stream_budget_planner import _refuse_if_over_concurrent_limit

    _refuse_if_over_concurrent_limit(bybit, 1000)
    with pytest.raises(PlanRefused) as refusal:
        _refuse_if_over_concurrent_limit(bybit, 1001)
    assert "1000" in str(refusal.value)
    # The venue that publishes no ceiling has nothing to check against, and this
    # project does not invent one for it.
    _refuse_if_over_concurrent_limit(adapters["binance-usdm"], 10_000)


def test_the_description_carries_the_machine_it_was_planned_against(adapters, hardware):
    description = describe_budget(plan(adapters, hardware))
    assert description["part_id"] == PART_ID
    assert description["hardware"]["physical_cores"] == hardware.physical_cores
    assert description["hardware"]["measured_at_ns"] == hardware.measured_at_ns
    assert description["open_file_limit"] == read_open_file_limit()
    assert description["connections"] == sum(description["connections_by_venue"].values())


def test_the_book_is_planned_only_for_the_busiest_symbols(adapters, hardware):
    """The one stream kind capped below the universe, and why.

    Nothing planned a book stream until 2026-08-25, so order-book-reader
    subscribed to nothing and the bots' feature builders counted every vector
    incomplete for want of one -- which left the outlier rejector unable to judge
    any of them and the conviction model refusing every candidate it was handed.

    Capped because a depth stream pushes a ladder every hundred milliseconds
    whether or not anything trades: at the full universe it is more tape per day
    than the trades this system exists to keep.
    """
    budget = plan(
        adapters, hardware, symbols_per_venue=30,
        kinds=(StreamKind.TRADE, StreamKind.BOOK), book_symbols_per_venue=10,
    )
    requests = [r for assignment in budget.plan.connections for r in assignment.requests]
    for venue_id in adapters:
        venue_requests = [
            r for assignment in budget.plan.connections if assignment.venue_id == venue_id
            for r in assignment.requests
        ]
        books = [r for r in venue_requests if r.stream_kind is StreamKind.BOOK]
        trades = [r for r in venue_requests if r.stream_kind is StreamKind.TRADE]
        assert len(trades) == 30, f"{venue_id}: every symbol keeps its trade stream"
        assert len(books) == 10, f"{venue_id}: the book is capped"
        assert [r.symbol for r in books] == [f"SYM{n}USDT" for n in range(10)], (
            "the cap keeps the universe's own order, which is by volume"
        )
    assert requests


def test_a_book_cap_above_the_universe_plans_every_symbol(adapters, hardware):
    budget = plan(
        adapters, hardware, symbols_per_venue=4,
        kinds=(StreamKind.BOOK,), book_symbols_per_venue=10,
    )
    for venue_id in adapters:
        books = [
            r for assignment in budget.plan.connections if assignment.venue_id == venue_id
            for r in assignment.requests
        ]
        assert len(books) == 4
