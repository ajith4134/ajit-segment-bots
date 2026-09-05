"""subscribed-instrument-listing-filter: the instrument master, narrowed to the
instruments the feed is actually subscribed to.

Ten parts join a listing to something the feed delivers -- an LTP, a greek, an
open-interest reading, a depth snapshot. A listing for an instrument nobody
subscribed can never be joined to any of those, so those ten were being drained
at the full restatement rate of all 102,940 rows to use at most 2,000 of them.

Measured on the live spine 2026-09-05, 35 minutes after start:

    broker-instrument-catalogue-reader
      broker-instrument-listing  published 571,464   not delivered 77,477  (13.6%)
    broker-market-feed-reader
      subscribed_instruments 2,000

`broker-instrument-listing` was the second-worst undelivered type on the whole
spine, and nothing reported it: every drop is an inbox that was full when a
datagram arrived, which is a decision the socket is entitled to make.

**Not narrowed to `symbol-universe`.** The universe is ~153 symbols, ranked by
distance from the underlying's own price and capped per underlying, and
`expiry-day-zero-to-hero-detector` exists to find the far-out-of-the-money
contracts such a ranking excludes. The bound that is right for all ten is what
the feed subscribed, which is a fact only `broker-market-feed-reader` holds --
it takes the universe first and fills the rest of the connection from the master
under the adapter's own cap (docs/proposals/subscribed-instrument-listing-filter.md).

**The whole master is held, and the subscribed subset emitted from it.** Keeping
only the rows that happened to be subscribed when they passed would be cheaper
and wrong: an instrument subscribed a minute after its row went by would then
wait a full restatement cycle to reach anybody, and expiry-day contracts are
subscribed on expiry morning, which is exactly when the detector needs them.
"""

from __future__ import annotations

from runtime.part_declaration import PartDeclaration
from runtime.part_process import run_part
from runtime.restatement_conveyor import RestatementConveyor

PART_ID = "subscribed-instrument-listing-filter"

PART_DECLARATION = PartDeclaration(
    part_id="subscribed-instrument-listing-filter",
    consumes=("broker-instrument-listing", "broker-subscription-state"),
    produces=("broker-subscribed-instrument-listing", "part-health"),
    resource_class="bandwidth-bound",
    rate_risk="changes-the-answer",
    skipped_tick_effect="delays",
)


class SubscribedInstrumentListings:
    """Holds every listing seen, and hands back the ones that are subscribed."""

    def __init__(self) -> None:
        self._listing_by_key: dict[str, object] = {}
        # The order the subscription itself states, so a consumer draining
        # slowly sees the instruments in the order the feed decided mattered --
        # the universe first, then the chain (subscribe_the_universe_first).
        self._subscribed_keys: tuple[str, ...] = ()
        self._subscribed_key_set: set[str] = set()
        self._brokers_heard_from: set[str] = set()
        self.listings_seen = 0
        self.subscription_statements_seen = 0
        # Bumped only when what would be restated actually differs. The
        # conveyor restarts its cycle whenever it is handed a table, so a
        # caller that hands it one every tick pins it at the first slice
        # forever -- it would republish the same few rows at the right rate and
        # never reach the rest, while every counter climbed as if it were
        # working.
        self.revision = 0

    def observe_listing(self, listing) -> None:
        self.listings_seen += 1
        key = listing.instrument_key
        known = self._listing_by_key.get(key)
        self._listing_by_key[key] = listing
        if key in self._subscribed_key_set and listing != known:
            self.revision += 1

    def observe_subscription(self, state) -> None:
        """One broker's whole subscribed set, replacing what it said before.

        Replacing, never merging: the level is the set the connection carries
        now, so an instrument dropped from it must stop being restated. A
        connection that has dropped states an empty set, and an empty set means
        the filter says nothing -- not that it goes on restating the set from a
        connection that no longer exists.
        """
        self.subscription_statements_seen += 1
        self._brokers_heard_from.add(state.broker_id)
        keys = tuple(state.instrument_keys)
        if keys != self._subscribed_keys:
            self._subscribed_keys = keys
            self._subscribed_key_set = set(keys)
            self.revision += 1

    def subscribed_listings(self) -> tuple:
        """A listing for every subscribed instrument this filter has seen.

        A subscribed key with no listing yet is skipped rather than filled in:
        the feed can subscribe from `symbol-universe` alone, so it can name an
        instrument whose master row has not come round the conveyor yet, and
        `unlisted_subscribed_instruments` is how long that lasts.
        """
        return tuple(
            listing
            for key in self._subscribed_keys
            if (listing := self._listing_by_key.get(key)) is not None
        )

    @property
    def unlisted_subscribed_instruments(self) -> int:
        return sum(
            1 for key in self._subscribed_keys if key not in self._listing_by_key
        )

    @property
    def listings_known(self) -> int:
        return len(self._listing_by_key)

    @property
    def subscribed_instruments(self) -> int:
        return len(self._subscribed_keys)


def describe_standing(filter_: SubscribedInstrumentListings, conveyor: dict) -> dict:
    """What is held, what is subscribed, and how far round the cycle it is.

    `subscribed_instruments` reading 0 while `listings_known` climbs is the
    signature of a feed that has not connected, not of a filter that is broken --
    the two are different facts and are counted separately (Rule 8).
    """
    return {
        "part_id": PART_ID,
        "listings_known": filter_.listings_known,
        "listings_seen": filter_.listings_seen,
        "subscription_statements_seen": filter_.subscription_statements_seen,
        "subscribed_instruments": filter_.subscribed_instruments,
        "unlisted_subscribed_instruments": filter_.unlisted_subscribed_instruments,
        "listings_restated": conveyor.get("restated", 0),
        "cycle_position": conveyor.get("position", 0),
        "cycles_completed": conveyor.get("cycles", 0),
        "listings_per_second": conveyor.get("rate", 0.0),
    }


def start_part(context) -> int:
    """The one entry point every part carries (T-1)."""
    import time

    from runtime.input_assembly import Batch, LatestByKey

    listings = Batch(read=context.bus.reader("broker-instrument-listing"))
    # Bounded, like every level read here: a subscription nobody is restating is
    # a connection nobody is holding, and restating its instruments would be
    # asserting a subscription that has gone. The unbounded LatestByKey is the
    # trap this project has fallen into repeatedly -- 2026-08-26, four parts
    # read part-resource-usage with no bound and every part the governor had
    # ever switched off still counted as running.
    subscriptions = LatestByKey(
        read=context.bus.reader("broker-subscription-state"),
        key_of=lambda state: state.broker_id,
        maximum_age_seconds=context.number(
            "broker_subscription_state_maximum_age"
        ),
    )
    publish_listings = context.bus.publisher_for("broker-subscribed-instrument-listing")
    conveyor = RestatementConveyor(
        context.number("subscribed_listing_restatement_cycle_seconds")
    )
    held = SubscribedInstrumentListings()
    handed_over = {"revision": None}

    def tick() -> None:
        for listing in listings.payloads():
            held.observe_listing(listing)
        subscriptions.take_in_what_arrived()
        for state in subscriptions.mapping().values():
            held.observe_subscription(state)

        # Handed to the conveyor only when what would be restated has actually
        # changed. Handing it a table restarts its cycle, so doing that every
        # tick would pin it at the first slice forever: it would republish the
        # same few rows at exactly the right rate and never reach the rest,
        # while every counter here climbed as though it were working.
        if handed_over["revision"] != held.revision:
            conveyor.hold(held.subscribed_listings())
            handed_over["revision"] = held.revision
        slice_ = conveyor.due_slice(time.monotonic())
        if slice_:
            publish_listings(slice_)

    return run_part(
        declaration=PART_DECLARATION,
        control_socket=context.control_socket,
        do_one_tick=tick,
        emit_health=context.emit_health,
        health_interval_seconds=context.health_interval_seconds,
        input_descriptors=context.input_descriptors,
        tick_floor_seconds=context.tick_floor_seconds,
        read_standing=lambda: describe_standing(held, conveyor.standing()),
    )


__all__ = [
    "PART_DECLARATION",
    "PART_ID",
    "SubscribedInstrumentListings",
    "describe_standing",
    "start_part",
]
