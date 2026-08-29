"""venue-premium-stream-reader, on the premium frames both venues actually sent.

The part turns a venue's frames into whole premiums; `runtime.premium_assembly`
is what remembers the half a Bybit amend left out. The chain from captured bytes
to a premium `funding-rate-forecaster` can average is exercised end to end here,
because the reader, the assembler and the forecaster's new input were written at
once and a test of each link alone would not catch a mismatch between them.

RL-063: every payload comes from `tests/captured/`, never from a fixture typed
here. The two derived cases -- an index of zero, and a premium fed to the
forecaster -- are built with `dataclasses.replace` on a premium the venue really
sent, so the shape under test is the venue's and only the one field being
examined is ours. Each says so where it happens.
"""

import dataclasses
import importlib
import json

import pytest

from parts.market_data_feed.venue_premium_stream_reader import (
    CAPTURED_STREAM_KIND,
    PART_ID as READER_PART_ID,
    PremiumReaderStanding,
    describe_premium_reading,
)
from parts.prediction.funding_rate_forecaster import (
    NO_SYMBOL_PARAMETERS,
    FundingRateForecaster,
    describe_funding_forecasting,
)
from runtime.part_declaration import load_declaration_from_blueprint
from runtime.premium_assembly import PremiumAssembler
from runtime.tape import StreamKind
from runtime.venues.adapter_registry import load_venue_adapter
from runtime.venues.venue_adapter import StreamRequest

READER_MODULE = "parts.market_data_feed.venue_premium_stream_reader"

PREMIUM_FIXTURES = {
    # Every listed symbol in one frame, once a second: the all-market topic.
    "binance-usdm": "2026-08-28-ws-markprice-all-symbols.jsonl",
    # The same `tickers` topic the quote reader reads, captured through a delta
    # that amends one side of the premium and not the other.
    "bybit-linear": "2026-08-24-public-linear-tickers-through-one-sided-delta.jsonl",
}


def premiums_from(venue_id, read_captured_payloads):
    """Every whole premium in a venue's captured stream, through the real chain.

    The adapter decodes and the assembler completes -- the same two calls
    `VenuePremiumStreamReader.read_one_tick` makes, minus the socket.
    """
    adapter = load_venue_adapter(venue_id)
    assembler = PremiumAssembler(venue_id=venue_id)
    whole = [
        completed
        for _, payload in read_captured_payloads(venue_id, PREMIUM_FIXTURES[venue_id])
        for stated in adapter.read_premiums(payload)
        if (completed := assembler.apply(stated)) is not None
    ]
    return whole, assembler


# ---- the blueprint ---------------------------------------------------------------


def test_the_built_declaration_equals_the_blueprint():
    """RL-067: what is built matches the diagram, checked rather than trusted."""
    module = importlib.import_module(READER_MODULE)
    assert module.PART_DECLARATION == load_declaration_from_blueprint(READER_PART_ID)


def test_the_part_reads_the_stream_kind_the_planner_plans():
    """A reader assigned a kind the planner never plans opens nothing.

    That is exactly what happened before 2026-08-28: both adapters had answered
    for PREMIUM since 2026-08-26 and `stream_kinds` never named it, so this part
    would have had no assignment to open.
    """
    assert CAPTURED_STREAM_KIND is StreamKind.PREMIUM
    for venue_id in sorted(PREMIUM_FIXTURES):
        adapter = load_venue_adapter(venue_id)
        # Both halves of an assignment: where to dial, and what to ask it for.
        assert adapter.stream_endpoint_url(StreamKind.PREMIUM)
        assert adapter.subscription_topic(
            StreamRequest(stream_kind=StreamKind.PREMIUM, symbol="BTCUSDT")
        )


# ---- captured bytes become premiums ----------------------------------------------


@pytest.mark.parametrize("venue_id", sorted(PREMIUM_FIXTURES))
def test_a_captured_stream_becomes_premiums_a_funding_rate_can_be_averaged_from(
    venue_id, read_captured_payloads
):
    """The whole chain: venue bytes -> stated premiums -> whole premiums."""
    whole, assembler = premiums_from(venue_id, read_captured_payloads)

    assert whole, f"{venue_id} sent premium frames and none completed"
    assert assembler.symbols_held() > 0
    for premium in whole:
        assert premium.venue_id == venue_id
        assert premium.mark_price is not None and premium.mark_price > 0
        assert premium.index_price is not None and premium.index_price > 0
        assert premium.venue_time_ns > 0
        # mark minus index over index: a real number, and small, because a
        # perpetual that has come unmoored from its index is the rare case.
        assert premium.premium_fraction is not None
        assert abs(premium.premium_fraction) < 1.0


def test_binance_sends_every_listed_symbol_in_one_frame(read_captured_payloads):
    """One subscription, the whole market -- the fact the planner is uncapped on.

    A per-symbol premium stream at this width would be hundreds of descriptors;
    the reason PREMIUM needs no cap where BOOK does is that this arrives as one
    array a second.
    """
    adapter = load_venue_adapter("binance-usdm")
    widest = max(
        len(adapter.read_premiums(payload))
        for _, payload in read_captured_payloads(
            "binance-usdm", PREMIUM_FIXTURES["binance-usdm"]
        )
    )
    assert widest > 100


def test_the_estimated_settlement_price_is_never_read_as_the_index(read_captured_payloads):
    """`P` is the venue's own forecast; `i` is the basket it marks to.

    A premium computed against `P` is a premium against a number the venue made
    up. The two differ in the captured frames, so reading the wrong one would
    show here rather than in a live funding cost.
    """
    adapter = load_venue_adapter("binance-usdm")
    compared = 0
    for _, payload in read_captured_payloads("binance-usdm", PREMIUM_FIXTURES["binance-usdm"]):
        message = json.loads(payload)
        entries = message if isinstance(message, list) else [message]
        stated = {premium.symbol: premium for premium in adapter.read_premiums(payload)}
        for entry in entries:
            premium = stated.get(entry.get("s"))
            if premium is None or float(entry["i"]) == float(entry["P"]):
                continue
            assert premium.index_price == float(entry["i"])
            assert premium.index_price != float(entry["P"])
            compared += 1
    assert compared > 0, "no captured frame had an index and an estimate that differed"


# ---- what the assembler is for ---------------------------------------------------


def test_an_amend_naming_one_half_is_completed_from_what_was_remembered(
    read_captured_payloads,
):
    """Bybit's `tickers` amends: a delta can carry an index and no mark price.

    The captured stream ends on exactly that frame. Without the assembler it
    yields nothing, and the premium a whole snapshot established a moment earlier
    would be thrown away on every partial update afterwards.
    """
    records = read_captured_payloads("bybit-linear", PREMIUM_FIXTURES["bybit-linear"])
    adapter = load_venue_adapter("bybit-linear")

    one_sided = [
        (index, json.loads(payload)["data"])
        for index, (_, payload) in enumerate(records)
        if json.loads(payload).get("type") == "delta"
        and ("markPrice" in json.loads(payload)["data"])
        != ("indexPrice" in json.loads(payload)["data"])
    ]
    assert one_sided, "the capture no longer carries a one-sided premium amend"
    last_index, amend = one_sided[-1]

    assembler = PremiumAssembler(venue_id="bybit-linear")
    completed = None
    for _, payload in records[: last_index + 1]:
        for stated in adapter.read_premiums(payload):
            completed = assembler.apply(stated) or completed

    assert completed is not None
    # The half the amend carried is the venue's newest word on it.
    if "indexPrice" in amend:
        assert completed.index_price == float(amend["indexPrice"])
    else:
        assert completed.mark_price == float(amend["markPrice"])


def test_a_merged_premium_is_dated_by_its_stalest_half(read_captured_payloads):
    """An index quoted earlier must not be made fresh by a mark that just moved.

    The premium between two numbers is exactly as current as the older one. Taking
    the newer stamp would present a premium computed against a stale index as a
    measurement of now, which is the only thing a premium is for.
    """
    records = read_captured_payloads("bybit-linear", PREMIUM_FIXTURES["bybit-linear"])
    adapter = load_venue_adapter("bybit-linear")

    assembler = PremiumAssembler(venue_id="bybit-linear")
    dated_older_than_the_frame = 0
    for _, payload in records:
        message = json.loads(payload)
        for stated in adapter.read_premiums(payload):
            completed = assembler.apply(stated)
            if completed is None:
                continue
            assert completed.venue_time_ns <= stated.venue_time_ns
            data = message.get("data", {})
            if ("markPrice" in data) != ("indexPrice" in data):
                # One half is the venue's newest word, the other is remembered:
                # the merged premium must carry the remembered half's moment.
                assert completed.venue_time_ns < stated.venue_time_ns
                dated_older_than_the_frame += 1
    assert dated_older_than_the_frame > 0


def test_a_symbol_whose_other_half_never_arrived_has_no_premium_at_all(
    read_captured_payloads,
):
    """Missing is never zero. A premium of zero is a claim about the market."""
    records = read_captured_payloads("bybit-linear", PREMIUM_FIXTURES["bybit-linear"])
    adapter = load_venue_adapter("bybit-linear")

    one_sided = [
        payload
        for _, payload in records
        if json.loads(payload).get("type") == "delta"
        and ("markPrice" in json.loads(payload)["data"])
        != ("indexPrice" in json.loads(payload)["data"])
    ]
    assert one_sided

    assembler = PremiumAssembler(venue_id="bybit-linear")
    for payload in one_sided:
        for stated in adapter.read_premiums(payload):
            assert assembler.apply(stated) is None

    assert assembler.symbols_held() == 0
    assert assembler.describe()["messages_still_incomplete"] == len(one_sided)
    assert assembler.describe()["premiums_completed"] == 0


def test_an_index_of_zero_is_counted_rather_than_dividing_by_it(read_captured_payloads):
    """One unusable field must not stop a feed, and must not be silently used.

    Derived: the premium is a frame the venue really sent, with the index alone
    replaced. No venue has sent a zero index in anything captured, and waiting for
    one to arrive live is not a test.
    """
    whole, _ = premiums_from("binance-usdm", read_captured_payloads)
    real = whole[0]
    assembler = PremiumAssembler(venue_id=real.venue_id)

    unusable = dataclasses.replace(real, index_price=0.0, mark_price=None)
    assert assembler.apply(unusable) is None
    assert assembler.describe()["unusable_index_prices"] == 1
    assert assembler.symbols_held() == 0


# ---- the standing this part reports ----------------------------------------------


def test_the_reader_reports_every_venue_not_only_the_first():
    """A part reports one standing, and this part carries every captured venue.

    Reporting the first venue's counters would make a dead second venue invisible.
    """
    readers = {
        "binance-usdm": _StubReader("binance-usdm", connections=1, drained=40, read=18220),
        "bybit-linear": _StubReader("bybit-linear", connections=3, drained=7, read=3),
    }
    standing = describe_premium_reading(readers)

    assert standing["part_id"] == READER_PART_ID
    assert standing["venues"] == 2
    assert standing["connections"] == 4
    assert standing["messages_drained"] == 47
    assert standing["premiums_read"] == 18223
    assert standing["premiums_published"] == 18223
    assert standing["symbols_held"] == 12


def test_a_reader_that_has_read_nothing_reports_zeroes_rather_than_nothing():
    """Absence of evidence renders as its own number, never as a missing key."""
    standing = describe_premium_reading({})
    assert standing["venues"] == 0
    for key in (
        "connections",
        "messages_drained",
        "premiums_read",
        "premiums_published",
        "symbols_held",
        "messages_naming_no_premium",
        "messages_still_incomplete",
        "unusable_index_prices",
    ):
        assert standing[key] == 0


class _StubReader:
    """A reader that has already run, so the merge of standings can be checked alone.

    Not a stub of the venue: the connection, the adapter and the assembler are all
    driven by real payloads above. What is under test here is arithmetic across
    venues, and driving it with two live sockets would test the network.
    """

    def __init__(self, venue_id, connections, drained, read):
        self.standing = PremiumReaderStanding(
            venue_id=venue_id,
            connections=connections,
            messages_drained=drained,
            premiums_read=read,
            premiums_published=read,
            assembler={"symbols_held": 6, "messages_still_incomplete": 1},
        )


# ---- the reason this part exists -------------------------------------------------


def test_the_premiums_this_part_publishes_reach_the_forecaster(read_captured_payloads):
    """`funding-rate-forecaster` observed zero premiums across five million messages.

    Derived only in the call: the premiums are the venue's own, handed to the
    forecaster the way `start_part` hands them over. What is checked is that they
    are observable at all -- the counter that stayed at zero for the part's whole
    life.
    """
    whole, _ = premiums_from("binance-usdm", read_captured_payloads)
    forecaster = FundingRateForecaster(
        premium_window_observations=64, minimum_observations=2
    )
    for premium in whole:
        forecaster.observe_premium(
            premium.venue_id, premium.symbol, premium.mark_price, premium.index_price
        )

    standing = describe_funding_forecasting(forecaster)
    assert standing["premium_observations"] == len(whole)
    assert standing["symbols_tracked"] > 0


def test_without_a_venue_s_funding_formula_the_forecast_refuses_and_says_which(
    read_captured_payloads,
):
    """The premium is half of a funding rate; the venue's own formula is the other.

    This is the answer for a symbol whose parameters were never observed: a
    named refusal rather than a number. Pinned deliberately -- an honest
    refusal is a different state from the silent zero this part reported
    before, and it is what says the remaining gap is the formula, not the
    premium.
    """
    whole, _ = premiums_from("binance-usdm", read_captured_payloads)
    forecaster = FundingRateForecaster(
        premium_window_observations=64, minimum_observations=2
    )
    for premium in whole:
        forecaster.observe_premium(
            premium.venue_id, premium.symbol, premium.mark_price, premium.index_price
        )

    forecast = forecaster.forecast(whole[0].venue_id, whole[0].symbol)
    assert forecast.state == NO_SYMBOL_PARAMETERS
    assert forecast.predicted_rate is None
    assert describe_funding_forecasting(forecaster)["refused_no_symbol_parameters"] == 1
