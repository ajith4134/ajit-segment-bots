"""subscribed-instrument-listing-filter, and the conveyor it shares with the
catalogue reader.

The defect these were written against is not the filtering -- that part is a
dict lookup. It is the conveyor: a table handed over on every change restarts
its cycle, so it republishes the first slice at exactly the right rate forever
and never reaches the rest, while every counter climbs as though it were
working. Both tests below fail against a conveyor that restarts.
"""

import dataclasses
import json
import pathlib

from parts.broker_adapter.subscribed_instrument_listing_filter import (
    SubscribedInstrumentListings, describe_standing, start_part,
)
from runtime.brokers.broker_adapter import BrokerSubscriptionState
from runtime.brokers.upstox import UpstoxAdapter
from runtime.bus import Message
from runtime.restatement_conveyor import RestatementConveyor

CAPTURED = (
    pathlib.Path(__file__).resolve().parents[3]
    / "tests/captured/upstox/2026-09-04-nse-instrument-master-nifty-slice.json"
)


def _real_listings(count):
    """Real captured Upstox master rows, extended by re-keying (RL-063)."""
    adapter = UpstoxAdapter.__new__(UpstoxAdapter)
    real = tuple(
        UpstoxAdapter.read_instrument_listings(adapter, json.loads(CAPTURED.read_text()))
    )
    out = []
    copy = 0
    while len(out) < count:
        for listing in real:
            out.append(
                dataclasses.replace(
                    listing, instrument_key=f"{listing.instrument_key}#{copy}"
                )
            )
            if len(out) >= count:
                break
        copy += 1
    return tuple(out)


# ---- the conveyor ------------------------------------------------------------


def test_the_cycle_survives_the_table_being_handed_over_again():
    """The defect: a table rebuilt as its rows arrive is handed over often.

    Ten rows at one cycle a second. Handing the same table back between every
    slice must not send the conveyor back to the first row -- if it does, the
    only rows ever said are the first ones, at the right rate, forever.
    """
    rows = tuple(range(10))
    conveyor = RestatementConveyor(cycle_seconds=1.0)
    conveyor.hold(rows)

    said = []
    conveyor.due_slice(0.0)  # the first call has no elapsed time to owe against
    for step in range(1, 21):  # two cycles' worth of ticks
        conveyor.hold(rows)  # as a caller rebuilding its table every tick would
        said.extend(conveyor.due_slice(step * 0.1))

    assert set(said) == set(rows), (
        f"every row must be said; got {sorted(set(said))}. A conveyor that "
        f"restarts on each hand-over says only the first rows, forever."
    )


def test_a_shrunken_table_does_not_leave_the_cycle_past_its_end():
    conveyor = RestatementConveyor(cycle_seconds=1.0)
    conveyor.hold(tuple(range(10)))
    conveyor.due_slice(0.0)
    conveyor.due_slice(0.8)  # walk most of the way through

    conveyor.hold((0, 1))
    said = conveyor.due_slice(1.8)

    assert set(said) <= {0, 1}
    assert conveyor.standing()["position"] < 2


def test_a_cycle_of_zero_is_refused():
    """A cycle of zero is a burst wearing a rate's name."""
    try:
        RestatementConveyor(cycle_seconds=0.0)
    except ValueError as refusal:
        assert "cycle" in str(refusal)
    else:  # pragma: no cover - the point of the test
        raise AssertionError("a zero cycle was accepted")


# ---- what is subscribed ------------------------------------------------------


def _subscription(keys, broker_id="upstox", observed_at_ns=1):
    return BrokerSubscriptionState(
        broker_id=broker_id, instrument_keys=tuple(keys), observed_at_ns=observed_at_ns,
    )


def test_only_subscribed_instruments_are_restated():
    listings = _real_listings(50)
    held = SubscribedInstrumentListings()
    for listing in listings:
        held.observe_listing(listing)
    subscribed = [listing.instrument_key for listing in listings[:5]]
    held.observe_subscription(_subscription(subscribed))

    out = held.subscribed_listings()

    assert [listing.instrument_key for listing in out] == subscribed
    assert held.listings_known == 50
    assert held.subscribed_instruments == 5


def test_the_order_is_the_subscription_s_own():
    """The feed subscribes the universe first, then the chain. A consumer
    draining slowly must see them in that order, not in master order."""
    listings = _real_listings(10)
    held = SubscribedInstrumentListings()
    for listing in listings:
        held.observe_listing(listing)
    wanted = [listings[7].instrument_key, listings[1].instrument_key]
    held.observe_subscription(_subscription(wanted))

    assert [listing.instrument_key for listing in held.subscribed_listings()] == wanted


def test_a_subscribed_instrument_with_no_listing_yet_is_counted_not_invented():
    """The feed can subscribe from symbol-universe alone, so it can name an
    instrument whose master row has not come round the conveyor yet."""
    held = SubscribedInstrumentListings()
    held.observe_subscription(_subscription(["NSE_FO|54321"]))

    assert held.subscribed_listings() == ()
    assert held.unlisted_subscribed_instruments == 1


def test_a_dropped_connection_withdraws_the_whole_set():
    listings = _real_listings(5)
    held = SubscribedInstrumentListings()
    for listing in listings:
        held.observe_listing(listing)
    held.observe_subscription(_subscription([l.instrument_key for l in listings]))
    assert len(held.subscribed_listings()) == 5

    held.observe_subscription(_subscription([]))

    assert held.subscribed_listings() == ()


def test_the_revision_moves_only_when_what_would_be_restated_changes():
    listings = _real_listings(5)
    held = SubscribedInstrumentListings()
    held.observe_subscription(_subscription([listings[0].instrument_key]))
    after_subscribing = held.revision

    held.observe_listing(listings[1])  # not subscribed -- nothing to restate
    assert held.revision == after_subscribing

    held.observe_listing(listings[0])  # subscribed -- now there is
    assert held.revision > after_subscribing
    moved_to = held.revision

    held.observe_listing(listings[0])  # the same row again
    assert held.revision == moved_to

    held.observe_subscription(_subscription([listings[0].instrument_key]))
    assert held.revision == moved_to, "an unchanged subscription is not a change"


# ---- the part, through its own start_part ------------------------------------


class _Bus:
    def __init__(self, by_type):
        self._by_type = by_type
        self.published = []

    def reader(self, data_type):
        def read():
            messages = self._by_type.get(data_type, [])
            self._by_type[data_type] = []
            return tuple(messages)
        return read

    def publisher_for(self, _data_type):
        def publish(items):
            self.published.extend(items)
        return publish


class _Context:
    def __init__(self, numbers, bus):
        self._numbers = numbers
        self.bus = bus
        self.control_socket = None
        self.health_interval_seconds = 1.0
        self.input_descriptors = ()
        self.tick_floor_seconds = 0.0
        self.emit_health = lambda *a, **k: None
        self.declaration = None

    def number(self, name):
        return self._numbers[name]


def _message(data_type, payload):
    import time as _time

    return Message(
        data_type=data_type, producer_part_id="a-test", sequence=0,
        published_at_ns=_time.time_ns(), payload=payload,
    )


def _running_filter(monkeypatch, inbox, cycle_seconds=1.0):
    import parts.broker_adapter.subscribed_instrument_listing_filter as module

    bus = _Bus(inbox)
    captured = {}
    monkeypatch.setattr(module, "run_part", lambda **kwargs: captured.update(kwargs) or 0)
    module.start_part(
        _Context(
            {
                "subscribed_listing_restatement_cycle_seconds": cycle_seconds,
                "broker_subscription_state_maximum_age": 4.0,
            },
            bus,
        )
    )
    return bus, captured["do_one_tick"], captured["read_standing"]


def test_the_running_part_restates_the_subscribed_set_and_nothing_else(monkeypatch):
    listings = _real_listings(40)
    subscribed = [listing.instrument_key for listing in listings[:8]]
    inbox = {
        "broker-instrument-listing": [
            _message("broker-instrument-listing", listing) for listing in listings
        ],
        "broker-subscription-state": [
            _message("broker-subscription-state", _subscription(subscribed))
        ],
    }
    bus, tick, standing = _running_filter(monkeypatch, inbox, cycle_seconds=1.0)

    # Enough ticks for a whole cycle of eight rows at eight a second. The clock
    # is real here, so the loop asks more often than the rate can owe -- which
    # is exactly how the part is driven on the spine.
    import time as _time

    deadline = _time.monotonic() + 2.0
    while _time.monotonic() < deadline:
        tick()

    published = {listing.instrument_key for listing in bus.published}
    assert published == set(subscribed), (
        "every subscribed instrument is restated, and only those"
    )
    reported = standing()
    assert reported["listings_known"] == 40
    assert reported["subscribed_instruments"] == 8
    assert reported["unlisted_subscribed_instruments"] == 0
    assert reported["listings_restated"] >= 8


def test_nothing_is_published_before_the_feed_says_what_it_subscribed(monkeypatch):
    """Absence of a subscription is not an empty subscription to guess around:
    with no statement from the feed there is nothing this part may restate."""
    listings = _real_listings(20)
    inbox = {
        "broker-instrument-listing": [
            _message("broker-instrument-listing", listing) for listing in listings
        ],
    }
    bus, tick, standing = _running_filter(monkeypatch, inbox)

    for _ in range(20):
        tick()

    assert bus.published == []
    assert standing()["listings_known"] == 20
    assert standing()["subscribed_instruments"] == 0


def test_the_standing_separates_nothing_subscribed_from_nothing_known():
    """Rule 8: a feed that has not connected and a filter that is broken are
    different facts, and the board must be able to tell them apart."""
    held = SubscribedInstrumentListings()
    reported = describe_standing(held, RestatementConveyor(1.0).standing())

    assert reported["listings_known"] == 0
    assert reported["subscribed_instruments"] == 0
    assert reported["subscription_statements_seen"] == 0
